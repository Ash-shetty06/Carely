import os
from datetime import datetime, time, timedelta
from itertools import groupby

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import PasswordChangeView
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.utils import timezone
from django.views.decorators.http import require_POST

from .access import patient_required, visible_tasks
from .accounts import password_changed
from .communication import create_health_note, link_options, mark_thread_read, send_message
from .forms import HealthNoteForm, LabReportForm, MessageForm
from .models import Appointment, CarePlan, HealthNote, Message, PlanItem, Task
from .services import can_respond_to_dose, grace_window, record_dose_response, upload_lab_report
from .timeline import build_timeline

UPCOMING_DAYS = 7  # how far ahead the patient's homepage looks


@login_required
def home(request):
    if request.user.is_patient:
        return redirect("today")
    if request.user.is_clinic_staff:
        return redirect("staff_patients")
    raise PermissionDenied


@patient_required
def today(request):
    now = timezone.now()
    tz = timezone.get_current_timezone()
    local_today = timezone.localdate(now)
    start = datetime.combine(local_today, time.min, tzinfo=tz)
    end = datetime.combine(local_today + timedelta(days=1), time.min, tzinfo=tz)
    grace = grace_window(request.user.clinic_id)

    upcoming_end = end + timedelta(days=UPCOMING_DAYS)

    live = (
        visible_tasks(request.user)
        .filter(plan_item__care_plan__status=CarePlan.Status.ACTIVE)
        .exclude(status=Task.Status.CANCELLED)
        .select_related("plan_item__care_plan", "plan_item__clinic")
        .order_by("due_at")
    )
    # Today's doses, plus late-evening doses from yesterday whose grace window is still open.
    doses = live.filter(plan_item__type=PlanItem.Type.MEDICINE).filter(
        Q(due_at__gte=start, due_at__lt=end) | Q(due_at__gte=now - grace, due_at__lt=start)
    )
    for task in doses:
        task.can_respond = can_respond_to_dose(task, now=now, grace=grace)
        task.respond_until = task.due_at + grace

    booked = Appointment.objects.filter(
        patient=request.user, status=Appointment.Status.BOOKED, scheduled_at__gte=start, scheduled_at__lt=upcoming_end
    ).select_related("plan_item").order_by("scheduled_at")
    todays_appointments = [a for a in booked if a.scheduled_at < end]

    # Lab reports to upload: due within the coming week, or overdue with no report yet.
    lab_tests = live.filter(plan_item__type=PlanItem.Type.LAB_TEST, lab_report="").filter(
        Q(status=Task.Status.PENDING, due_at__lt=upcoming_end) | Q(status=Task.Status.MISSED)
    )

    # Coming up: the next 7 days, grouped by day.
    coming = [
        (task.due_at, f"{task.plan_item.name} {task.plan_item.dose_text}", "dose")
        for task in live.filter(plan_item__type=PlanItem.Type.MEDICINE, status=Task.Status.PENDING,
                                due_at__gte=end, due_at__lt=upcoming_end)
    ]
    coming += [
        (task.due_at, f"{task.plan_item.name}: lab test due", "lab")
        for task in live.filter(plan_item__type=PlanItem.Type.LAB_TEST, status=Task.Status.PENDING,
                                due_at__gte=end, due_at__lt=upcoming_end)
    ]
    coming += [(a.scheduled_at, f"{a.plan_item.name}: appointment", "appointment") for a in booked if a.scheduled_at >= end]
    coming.sort(key=lambda entry: entry[0])
    coming_days = [
        (day, [{"at": at, "label": label, "kind": kind} for at, label, kind in entries])
        for day, entries in groupby(coming, key=lambda entry: timezone.localdate(entry[0]))
    ]

    return render(request, "core/today.html", {
        "tasks": doses, "todays_appointments": todays_appointments, "lab_tests": lab_tests,
        "coming_days": coming_days, "upcoming_days": UPCOMING_DAYS, "today": local_today,
    })


@patient_required
def my_plan(request):
    """The patient's full care plan and their own record. No alerts or staff notes."""
    plans = list(
        request.user.care_plans.filter(status__in=[CarePlan.Status.ACTIVE, CarePlan.Status.COMPLETED])
        .order_by("-activated_at")
    )
    chosen = next((p for p in plans if str(p.pk) == request.GET.get("plan")), None)
    chosen = chosen or next((p for p in plans if p.status == CarePlan.Status.ACTIVE), None) or next(iter(plans), None)
    if not chosen:
        return render(request, "core/my_plan.html", {"plan": None, "plans": plans})

    items = list(chosen.items.prefetch_related("tasks", "appointments", "replaced_by").order_by("type", "start_date"))
    now = timezone.now()
    for item in items:
        tasks = [t for t in item.tasks.all() if t.status != Task.Status.CANCELLED]
        item.task = tasks[0] if item.type != PlanItem.Type.MEDICINE and tasks else None
        item.appointment = next(iter(item.appointments.all()), None)
        item.replacement = next(iter(item.replaced_by.all()), None)
    return render(request, "core/my_plan.html", {
        "plan": chosen,
        "plans": plans,
        "medicines": [i for i in items if i.superseded_at is None and i.type == PlanItem.Type.MEDICINE],
        "lab_tests": [i for i in items if i.superseded_at is None and i.type == PlanItem.Type.LAB_TEST],
        "follow_ups": [i for i in items if i.superseded_at is None and i.type == PlanItem.Type.FOLLOW_UP],
        "changed_items": [i for i in items if i.superseded_at is not None],
        "timeline": build_timeline(chosen, now=now, for_patient=True),
    })


@require_POST
@patient_required
def respond_to_task(request, task_id):
    task = get_object_or_404(visible_tasks(request.user).select_related("plan_item__care_plan", "plan_item__clinic"), pk=task_id)
    action = request.POST.get("action")
    if action not in ("taken", "skipped"):
        return HttpResponseBadRequest("Unknown action.")
    try:
        record_dose_response(task, patient=request.user, taken=action == "taken")
    except ValidationError as error:
        messages.error(request, error.messages[0])
    else:
        messages.success(request, f"{task.plan_item.name} marked as {action}.")
    return redirect("today")


def _after_report_redirect(user, task):
    if user.is_clinic_staff:
        return redirect("staff_care_plan", plan_id=task.plan_item.care_plan_id)
    return redirect("today")


@require_POST
@login_required
def upload_report(request, task_id):
    """Patients upload for their own lab tests; staff for any in their clinic."""
    task = get_object_or_404(visible_tasks(request.user).select_related("plan_item"), pk=task_id)
    form = LabReportForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, "Please choose a file to upload.")
        return _after_report_redirect(request.user, task)
    try:
        upload_lab_report(task, actor=request.user, upload=form.cleaned_data["report"])
    except ValidationError as error:
        messages.error(request, error.messages[0])
    else:
        messages.success(request, f"Report uploaded for {task.plan_item.name}.")
    return _after_report_redirect(request.user, task)


@login_required
def download_report(request, task_id):
    """Serve a lab report only to its patient or their clinic's staff."""
    task = get_object_or_404(visible_tasks(request.user), pk=task_id)
    if not task.lab_report:
        raise Http404
    ext = os.path.splitext(task.lab_report.name)[1].lower()
    response = FileResponse(task.lab_report.open("rb"), filename=f"lab-report-{task.pk}{ext}")
    response["Cache-Control"] = "private, no-store"
    return response


@patient_required
def health_notes(request):
    form = HealthNoteForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            create_health_note(patient=request.user, text=form.cleaned_data["text"], tags=form.cleaned_data["tags"])
        except ValidationError as error:
            form.add_error(None, error)
        else:
            messages.success(request, "Your note was sent to the clinic.")
            return redirect("health_notes")
    notes = HealthNote.objects.filter(patient=request.user).prefetch_related("alerts")
    return render(request, "core/health_notes.html", {"form": form, "notes": notes})


@patient_required
def patient_messages(request):
    patient = request.user
    form = MessageForm(request.POST or None, link_choices=link_options(patient))
    if request.method == "POST" and form.is_valid():
        try:
            send_message(sender=patient, patient=patient, body=form.cleaned_data["body"], link=form.cleaned_data["about"])
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("patient_messages")
    mark_thread_read(reader=patient, patient=patient)
    thread = Message.objects.filter(patient=patient).select_related("sender", "plan_item", "appointment__plan_item", "health_note", "converted_note")
    return render(request, "core/messages.html", {"form": form, "thread": thread, "patient": patient})


class CarelyPasswordChangeView(PasswordChangeView):
    template_name = "registration/password_change.html"
    success_url = reverse_lazy("home")

    def form_valid(self, form):
        response = super().form_valid(form)
        password_changed(self.request.user)
        messages.success(self.request, "Your password has been changed.")
        return response
