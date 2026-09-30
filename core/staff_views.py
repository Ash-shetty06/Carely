"""Staff pages: patients, care plan entry, follow-up appointments, alert inbox.

Every object is fetched through a clinic-scoped queryset from access.py, so
records from another clinic are a 404.
"""

from collections import Counter
from itertools import groupby

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import services
from .accounts import is_locked, reset_patient_password, update_clinic_settings
from .access import (
    clinic_alerts, clinic_appointments, clinic_care_plans, clinic_patients, clinic_plan_items,
    staff_required, visible_tasks,
)
from .alerts import mark_alert_seen, record_triage
from .clinic_config import clinic_setting
from .communication import convert_message_to_note, link_options, mark_thread_read, send_message
from .timeline import build_timeline
from .forms import (
    AppointmentForm, ClinicSettingsForm, LabReportForm, MessageForm, PatientCreateForm, PlanItemForm, ReviewForm,
    TemporaryPasswordForm, TriageForm,
)
from .models import Alert, CarePlan, ClinicSettings, HealthNote, Message, PlanItem, Task, User

OPEN_ALERT_STATUSES = [Alert.Status.NEW, Alert.Status.SEEN]


def _error(request, error):
    messages.error(request, " ".join(error.messages))


# --- patients ----------------------------------------------------------------


@staff_required
def staff_patients(request):
    active_plan = CarePlan.objects.filter(patient=OuterRef("pk"), status=CarePlan.Status.ACTIVE)
    patients = (
        clinic_patients(request.user)
        .annotate(
            has_active_plan=Exists(active_plan),
            missed_count=Count("tasks", filter=Q(tasks__status=Task.Status.MISSED), distinct=True),
            open_alert_count=Count("alerts", filter=Q(alerts__status__in=OPEN_ALERT_STATUSES), distinct=True),
        )
        .order_by("full_name")
    )
    return render(request, "core/staff_patients.html", {"patients": patients})


@staff_required
def add_patient(request):
    form = PatientCreateForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        patient = services.create_patient(
            staff=request.user,
            full_name=form.cleaned_data["full_name"],
            username=form.cleaned_data["username"],
            password=form.cleaned_data["password"],
        )
        messages.success(request, f"Patient {patient.full_name} added. Give them their username and temporary password.")
        return redirect("staff_patient_detail", patient_id=patient.pk)
    return render(request, "core/staff_add_patient.html", {"form": form})


@staff_required
def staff_patient_detail(request, patient_id):
    patient = get_object_or_404(clinic_patients(request.user), pk=patient_id)
    plans = patient.care_plans.order_by("-created_at")
    can_start_plan = not plans.filter(status__in=[CarePlan.Status.DRAFT, CarePlan.Status.ACTIVE]).exists()
    alerts = (
        clinic_alerts(request.user)
        .filter(patient=patient, status__in=OPEN_ALERT_STATUSES)
        .select_related("task__plan_item", "health_note")
    )
    notes = HealthNote.objects.filter(patient=patient).select_related("outcome_by", "source_message").prefetch_related("alerts")[:20]

    # Timeline: the chosen plan, else the active one, else the most recent live one.
    live_plans = [p for p in plans if p.status in (CarePlan.Status.ACTIVE, CarePlan.Status.COMPLETED)]
    chosen = next((p for p in live_plans if str(p.pk) == request.GET.get("plan")), None)
    chosen = chosen or next((p for p in live_plans if p.status == CarePlan.Status.ACTIVE), None)
    chosen = chosen or next(iter(live_plans), None)
    timeline = build_timeline(chosen) if chosen else None
    return render(request, "core/staff_patient_detail.html", {
        "patient": patient, "plans": plans, "can_start_plan": can_start_plan, "alerts": alerts,
        "notes": notes, "live_plans": live_plans, "timeline_plan": chosen, "timeline": timeline,
        "locked": is_locked(patient),
    })


@staff_required
def staff_patient_tasks(request, patient_id):
    """Every task for the patient, day by day (the detail behind the timeline)."""
    patient = get_object_or_404(clinic_patients(request.user), pk=patient_id)
    tasks = visible_tasks(request.user).filter(patient=patient).select_related("plan_item").order_by("due_at")
    days = [(day, list(day_tasks)) for day, day_tasks in groupby(tasks, key=lambda t: timezone.localdate(t.due_at))]
    return render(request, "core/staff_patient_tasks.html", {"patient": patient, "days": days})


# --- care plans ----------------------------------------------------------------


@require_POST
@staff_required
def new_care_plan(request, patient_id):
    patient = get_object_or_404(clinic_patients(request.user), pk=patient_id)
    try:
        plan = services.create_care_plan(patient=patient, staff=request.user)
    except ValidationError as error:
        _error(request, error)
        return redirect("staff_patient_detail", patient_id=patient.pk)
    return redirect("staff_care_plan", plan_id=plan.pk)


def _add_forms(bound_type=None, bound_form=None):
    forms = {t: PlanItemForm(item_type=t, prefix=t) for t in PlanItem.Type.values}
    if bound_type:
        forms[bound_type] = bound_form
    return forms


def _render_plan(request, plan, *, add_forms=None, review_form=None, status=200):
    items = list(
        plan.items.select_related("replaces").prefetch_related("tasks__appointment").order_by("superseded_at", "type", "start_date")
    )
    for item in items:
        tasks = [t for t in item.tasks.all()]
        item.status_counts = Counter(t.status for t in tasks)
        # Lab tests and follow-ups have a single task.
        item.single_task = next((t for t in tasks if t.status != Task.Status.CANCELLED), None) if item.type != PlanItem.Type.MEDICINE else None
        item.appointment = getattr(item.single_task, "appointment", None) if item.single_task else None
    current = [i for i in items if i.superseded_at is None]
    history = [i for i in items if i.superseded_at is not None]
    return render(request, "core/staff_care_plan.html", {
        "plan": plan,
        "patient": plan.patient,
        "current_items": current,
        "history_items": history,
        "add_forms": add_forms or _add_forms(),
        "review_form": review_form or ReviewForm(),
        "report_form": LabReportForm(),
        "is_draft": plan.status == CarePlan.Status.DRAFT,
        "is_active": plan.status == CarePlan.Status.ACTIVE,
        "now": timezone.now(),
    }, status=status)


@staff_required
def care_plan_detail(request, plan_id):
    plan = get_object_or_404(clinic_care_plans(request.user).select_related("patient"), pk=plan_id)
    return _render_plan(request, plan)


@require_POST
@staff_required
def add_item(request, plan_id, item_type):
    plan = get_object_or_404(clinic_care_plans(request.user).select_related("patient"), pk=plan_id)
    if item_type not in PlanItem.Type.values:
        return redirect("staff_care_plan", plan_id=plan.pk)
    form = PlanItemForm(request.POST, item_type=item_type, prefix=item_type)
    if form.is_valid():
        try:
            item = services.add_plan_item(plan, actor=request.user, type=item_type, **form.item_fields())
        except ValidationError as error:
            _error(request, error)
        else:
            messages.success(request, f"Added {item.name}.")
            return redirect("staff_care_plan", plan_id=plan.pk)
    return _render_plan(request, plan, add_forms=_add_forms(item_type, form), status=400)


@require_POST
@staff_required
def remove_item(request, item_id):
    item = get_object_or_404(clinic_plan_items(request.user), pk=item_id)
    try:
        services.remove_draft_item(item, actor=request.user)
    except ValidationError as error:
        _error(request, error)
    else:
        messages.success(request, f"Removed {item.name}.")
    return redirect("staff_care_plan", plan_id=item.care_plan_id)


@require_POST
@staff_required
def activate_care_plan(request, plan_id):
    plan = get_object_or_404(clinic_care_plans(request.user).select_related("patient"), pk=plan_id)
    form = ReviewForm(request.POST)
    if not form.is_valid():
        return _render_plan(request, plan, review_form=form, status=400)
    try:
        services.activate_plan(
            plan, actor=request.user,
            reviewed_by_doctor=form.cleaned_data["reviewed_by_doctor"],
            reviewed_on=form.cleaned_data["reviewed_on"],
        )
    except ValidationError as error:
        _error(request, error)
    else:
        messages.success(request, "The care plan is live. Tasks have been created for the patient.")
    return redirect("staff_care_plan", plan_id=plan.pk)


@require_POST
@staff_required
def end_care_plan(request, plan_id):
    plan = get_object_or_404(clinic_care_plans(request.user), pk=plan_id)
    try:
        if plan.status == CarePlan.Status.DRAFT:
            services.cancel_draft_plan(plan, actor=request.user)
            messages.success(request, "Draft discarded.")
        else:
            services.end_plan(plan, actor=request.user)
            messages.success(request, "Care plan ended. Pending tasks were cancelled.")
    except ValidationError as error:
        _error(request, error)
    return redirect("staff_care_plan", plan_id=plan.pk)


@staff_required
def change_item(request, item_id):
    item = get_object_or_404(clinic_plan_items(request.user).select_related("care_plan__patient"), pk=item_id)
    # Bind the form to an unsaved copy so the live item is never modified in place.
    draft = PlanItem(
        clinic_id=item.clinic_id, care_plan=item.care_plan, type=item.type, name=item.name,
        dose_text=item.dose_text, dose_times=list(item.dose_times), start_date=item.start_date,
        end_date=item.end_date, notes=item.notes,
    )
    form = PlanItemForm(request.POST or None, instance=draft, item_type=item.type, changing=True)
    if request.method == "POST" and form.is_valid():
        try:
            services.replace_plan_item(item, actor=request.user, **form.item_fields())
        except ValidationError as error:
            _error(request, error)
        else:
            messages.success(request, f"{item.name} changed. Future tasks were updated; past tasks are unchanged.")
            return redirect("staff_care_plan", plan_id=item.care_plan_id)
    return render(request, "core/staff_change_item.html", {"item": item, "form": form, "plan": item.care_plan})


@require_POST
@staff_required
def stop_item(request, item_id):
    item = get_object_or_404(clinic_plan_items(request.user), pk=item_id)
    try:
        services.stop_plan_item(item, actor=request.user)
    except ValidationError as error:
        _error(request, error)
    else:
        messages.success(request, f"{item.name} stopped. Its future tasks were cancelled.")
    return redirect("staff_care_plan", plan_id=item.care_plan_id)


# --- follow-up appointments ---------------------------------------------------------


@require_POST
@staff_required
def book_appointment(request, task_id):
    task = get_object_or_404(visible_tasks(request.user).select_related("plan_item"), pk=task_id)
    form = AppointmentForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Please enter a valid date and time.")
    else:
        try:
            services.book_appointment(task, actor=request.user, scheduled_at=form.cleaned_data["scheduled_at"])
        except ValidationError as error:
            _error(request, error)
        else:
            messages.success(request, "Appointment booked.")
    return redirect("staff_care_plan", plan_id=task.plan_item.care_plan_id)


@require_POST
@staff_required
def update_appointment(request, appointment_id):
    appointment = get_object_or_404(
        clinic_appointments(request.user).select_related("plan_item__clinic", "task"), pk=appointment_id
    )
    action = request.POST.get("action")
    try:
        if action == "reschedule":
            form = AppointmentForm(request.POST)
            if not form.is_valid():
                raise ValidationError("Please enter a valid date and time.")
            services.reschedule_appointment(appointment, actor=request.user, scheduled_at=form.cleaned_data["scheduled_at"])
            messages.success(request, "Appointment rescheduled.")
        elif action in ("attended", "not_attended"):
            services.record_attendance(appointment, actor=request.user, attended=action == "attended")
            messages.success(request, f"Recorded as {'attended' if action == 'attended' else 'not attended'}.")
        else:
            raise ValidationError("Unknown action.")
    except ValidationError as error:
        _error(request, error)
    return redirect("staff_care_plan", plan_id=appointment.plan_item.care_plan_id)


# --- alerts ---------------------------------------------------------------------------


@staff_required
def alert_inbox(request):
    show = request.GET.get("show", "open")
    alerts = clinic_alerts(request.user).select_related("patient", "task__plan_item", "health_note", "acted_by")
    if show == "done":
        alerts = alerts.filter(status=Alert.Status.ACTED_ON).order_by("-acted_at")
    else:
        show = "open"
        alerts = alerts.filter(status__in=OPEN_ALERT_STATUSES).order_by("-created_at")
    return render(request, "core/staff_alerts.html", {"alerts": alerts, "show": show})


@staff_required
def alert_detail(request, alert_id):
    alert = get_object_or_404(
        clinic_alerts(request.user).select_related("patient", "task__plan_item__care_plan", "health_note", "acted_by"),
        pk=alert_id,
    )
    if request.method == "POST":
        form = TriageForm(request.POST)
        if form.is_valid():
            record_triage(
                alert, actor=request.user,
                outcome=form.cleaned_data["outcome"], doctors_advice=form.cleaned_data["doctors_advice"],
            )
            messages.success(request, "Outcome recorded.")
            return redirect("staff_alerts")
    else:
        mark_alert_seen(alert, actor=request.user)
        form = TriageForm(initial={"outcome": alert.outcome, "doctors_advice": alert.doctors_advice})

    recent_tasks = []
    if alert.task:
        recent_tasks = (
            visible_tasks(request.user)
            .filter(plan_item__care_plan=alert.task.plan_item.care_plan, plan_item__name=alert.task.plan_item.name,
                    due_at__lte=alert.task.due_at)
            .exclude(status=Task.Status.CANCELLED)
            .select_related("plan_item")
            .order_by("-due_at")[:10]
        )
    return render(request, "core/staff_alert_detail.html", {"alert": alert, "form": form, "recent_tasks": recent_tasks})


# --- messages ----------------------------------------------------------------------------


@staff_required
def message_threads(request):
    """One row per patient who has messages, newest first, with unread counts."""
    last_message = Message.objects.filter(patient=OuterRef("pk")).order_by("-created_at")
    patients = (
        clinic_patients(request.user)
        .annotate(
            last_message_at=Subquery(last_message.values("created_at")[:1]),
            last_message_body=Subquery(last_message.values("body")[:1]),
            unread=Count(
                "message_thread",
                filter=Q(message_thread__read_at__isnull=True, message_thread__sender__role=User.Role.PATIENT),
            ),
        )
        .filter(last_message_at__isnull=False)
        .order_by("-last_message_at")
    )
    return render(request, "core/staff_messages.html", {"patients": patients})


@staff_required
def staff_patient_messages(request, patient_id):
    patient = get_object_or_404(clinic_patients(request.user), pk=patient_id)
    form = MessageForm(request.POST or None, link_choices=link_options(patient))
    if request.method == "POST" and form.is_valid():
        try:
            send_message(sender=request.user, patient=patient, body=form.cleaned_data["body"], link=form.cleaned_data["about"])
        except ValidationError as error:
            form.add_error(None, error)
        else:
            return redirect("staff_patient_messages", patient_id=patient.pk)
    mark_thread_read(reader=request.user, patient=patient)
    thread = Message.objects.filter(patient=patient).select_related("sender", "plan_item", "appointment__plan_item", "health_note", "converted_note")
    return render(request, "core/messages.html", {"form": form, "thread": thread, "patient": patient})


@require_POST
@staff_required
def message_to_note(request, message_id):
    message = get_object_or_404(Message.objects.filter(clinic_id=request.user.clinic_id), pk=message_id)
    try:
        note = convert_message_to_note(message, staff=request.user)
    except ValidationError as error:
        _error(request, error)
    else:
        alert = note.alerts.first()
        messages.success(request, "Saved as a health note. It is now in the alert inbox for triage.")
        if alert:
            return redirect("staff_alert_detail", alert_id=alert.pk)
    return redirect("staff_patient_messages", patient_id=message.patient_id)


# --- clinic settings and patient passwords -------------------------------------------------


@staff_required
def clinic_settings(request):
    clinic = request.user.clinic
    current = ClinicSettings.objects.filter(clinic=clinic).first()
    initial = {
        "grace_window_minutes": current.grace_window_minutes if current else clinic_setting(clinic.pk, "grace_window_minutes"),
        "missed_dose_threshold": current.missed_dose_threshold if current else clinic_setting(clinic.pk, "missed_dose_threshold"),
    }
    form = ClinicSettingsForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        _, changed = update_clinic_settings(clinic, staff=request.user, **form.cleaned_data)
        messages.success(request, "Settings saved. They apply from now on." if changed else "No changes to save.")
        return redirect("staff_settings")
    return render(request, "core/staff_settings.html", {"form": form, "clinic": clinic})


@staff_required
def reset_password(request, patient_id):
    patient = get_object_or_404(clinic_patients(request.user), pk=patient_id)
    form = TemporaryPasswordForm(request.POST or None, user=patient)
    if request.method == "POST" and form.is_valid():
        reset_patient_password(patient, staff=request.user, password=form.cleaned_data["password"])
        messages.success(request, f"Temporary password set for {patient.full_name}. Give it to them in person.")
        return redirect("staff_patient_detail", patient_id=patient.pk)
    return render(request, "core/staff_reset_password.html", {"form": form, "patient": patient, "locked": is_locked(patient)})
