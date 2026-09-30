"""Care plans, task generation, dose responses, appointments and lab reports.

Rules:
- Every prescribed item becomes tasks with a due time.
- Tasks are only generated from `now` onwards, so activating or changing a plan
  never creates tasks that are already overdue.
- When a plan item changes or stops, its future pending tasks are cancelled
  (and a new item version generates fresh tasks). Past tasks are never rewritten.
- Every change is written to the audit log.
"""

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .alerts import raise_alert
from .audit import log_action
from .clinic_config import grace_window
from .models import Alert, Appointment, AuditLog, CarePlan, Clinic, PlanItem, Task, User

__all__ = ["log_action", "grace_window"]  # re-exported for existing callers

# Lab tests and follow-ups are due by the end of their due date (clinic local time).
END_OF_DAY = time(23, 59)
# A lab test with no report becomes missed once its due date is over (midnight).
LAB_TEST_MISSED_AFTER = timedelta(minutes=1)

# Statuses a patient may still switch between while the grace window is open.
RESPONDABLE_STATUSES = {Task.Status.PENDING, Task.Status.DONE, Task.Status.SKIPPED}

# PlanItem fields copied onto a new version when an item is changed.
_VERSIONED_FIELDS = ["type", "name", "dose_text", "dose_times", "start_date", "end_date", "notes"]

# Lab report uploads: allowed types, checked by extension and file signature.
MAX_REPORT_BYTES = 10 * 1024 * 1024
_REPORT_SIGNATURES = {
    ".pdf": [b"%PDF-"],
    ".png": [b"\x89PNG\r\n\x1a\n"],
    ".jpg": [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
}


def _jsonable(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


# --- patients ---------------------------------------------------------------


@transaction.atomic
def create_patient(*, staff, full_name, username, password):
    """Staff create a patient login with a temporary password."""
    if not staff.is_clinic_staff:
        raise PermissionDenied
    patient = User.objects.create_user(
        username=username, password=password, clinic_id=staff.clinic_id,
        role=User.Role.PATIENT, full_name=full_name, must_change_password=True,
    )
    log_action(
        actor=staff, action=AuditLog.Action.CREATE, obj=patient,
        changes={"username": username, "full_name": full_name, "role": "patient"},
    )
    return patient


# --- care plans and items -------------------------------------------------------


def due_times(item):
    """All due datetimes (UTC-aware) prescribed by a plan item, ignoring `now`."""
    tz = ZoneInfo(item.clinic.timezone)
    if item.type == PlanItem.Type.MEDICINE:
        clock_times = [time.fromisoformat(t) for t in sorted(item.dose_times)]
        days = (item.end_date - item.start_date).days + 1
        local = [
            datetime.combine(item.start_date + timedelta(days=d), t, tzinfo=tz)
            for d in range(days)
            for t in clock_times
        ]
    else:
        local = [datetime.combine(item.start_date, END_OF_DAY, tzinfo=tz)]
    return [dt.astimezone(ZoneInfo("UTC")) for dt in local]


def generate_tasks(item, *, actor=None, now=None):
    """Create pending tasks for a live plan item. Safe to call more than once."""
    if item.care_plan.status != CarePlan.Status.ACTIVE:
        raise ValueError("Tasks can only be generated for an active care plan.")
    if item.superseded_at is not None:
        return []

    now = now or timezone.now()
    existing = set(item.tasks.values_list("due_at", flat=True))
    tasks = Task.objects.bulk_create(
        Task(
            clinic_id=item.clinic_id,
            patient_id=item.care_plan.patient_id,
            plan_item=item,
            due_at=due_at,
        )
        for due_at in due_times(item)
        if due_at >= now and due_at not in existing
    )
    AuditLog.objects.bulk_create(
        AuditLog(
            clinic_id=item.clinic_id,
            actor=actor,
            action=AuditLog.Action.CREATE,
            model=Task._meta.label,
            record_id=task.pk,
            changes={"plan_item": item.pk, "due_at": task.due_at.isoformat()},
        )
        for task in tasks
    )
    return tasks


@transaction.atomic
def create_care_plan(*, patient, staff):
    """Start a draft plan. A patient can have one draft or active plan at a time."""
    if not staff.is_clinic_staff or patient.clinic_id != staff.clinic_id or not patient.is_patient:
        raise PermissionDenied
    open_statuses = [CarePlan.Status.DRAFT, CarePlan.Status.ACTIVE]
    if patient.care_plans.filter(status__in=open_statuses).exists():
        raise ValidationError("This patient already has a draft or active care plan.")
    plan = CarePlan.objects.create(clinic_id=staff.clinic_id, patient=patient, created_by=staff)
    log_action(actor=staff, action=AuditLog.Action.CREATE, obj=plan, changes={"patient": patient.pk})
    return plan


@transaction.atomic
def add_plan_item(plan, *, actor, now=None, **fields):
    """Add an item to a draft plan, or to a live plan (tasks are generated at once)."""
    if plan.status not in (CarePlan.Status.DRAFT, CarePlan.Status.ACTIVE):
        raise ValidationError("Items can only be added to a draft or active care plan.")
    item = PlanItem(clinic_id=plan.clinic_id, care_plan=plan, **fields)
    item.full_clean()
    item.save()
    log_action(
        actor=actor, action=AuditLog.Action.CREATE, obj=item,
        changes={k: _jsonable(v) for k, v in fields.items()},
    )
    if plan.status == CarePlan.Status.ACTIVE:
        generate_tasks(item, actor=actor, now=now)
    return item


@transaction.atomic
def remove_draft_item(item, *, actor):
    """Delete an item from a plan that is still a draft (it has no tasks yet)."""
    if item.care_plan.status != CarePlan.Status.DRAFT:
        raise ValidationError("Items of a live plan can't be deleted; stop them instead.")
    log_action(
        actor=actor, action=AuditLog.Action.DELETE, obj=item,
        changes={"type": item.type, "name": item.name},
    )
    item.delete()


@transaction.atomic
def activate_plan(plan, *, actor, reviewed_by_doctor=None, reviewed_on=None, now=None):
    """Record the doctor's review, make the draft plan live and generate its tasks."""
    if plan.status != CarePlan.Status.DRAFT:
        raise ValidationError("Only a draft care plan can be activated.")
    if reviewed_by_doctor is not None:
        plan.reviewed_by_doctor = reviewed_by_doctor
    if reviewed_on is not None:
        plan.reviewed_on = reviewed_on
    if not plan.reviewed_by_doctor or not plan.reviewed_on:
        raise ValidationError("A doctor's review (name and date) is required before activation.")
    if not plan.items.exists():
        raise ValidationError("Add at least one item before making the plan live.")
    if CarePlan.objects.filter(patient_id=plan.patient_id, status=CarePlan.Status.ACTIVE).exists():
        raise ValidationError("This patient already has an active care plan. End it first.")

    now = now or timezone.now()
    plan.status = CarePlan.Status.ACTIVE
    plan.activated_at = now
    plan.full_clean()
    plan.save()
    log_action(
        actor=actor,
        action=AuditLog.Action.STATUS_CHANGE,
        obj=plan,
        changes={
            "status": ["draft", "active"],
            "reviewed_by_doctor": plan.reviewed_by_doctor,
            "reviewed_on": plan.reviewed_on.isoformat(),
        },
    )
    for item in plan.items.filter(superseded_at__isnull=True):
        generate_tasks(item, actor=actor, now=now)
    return plan


def _cancel_pending_tasks(tasks, *, actor, reason):
    for task in tasks.filter(status=Task.Status.PENDING):
        task.status = Task.Status.CANCELLED
        task.save(update_fields=["status"])
        log_action(
            actor=actor,
            action=AuditLog.Action.STATUS_CHANGE,
            obj=task,
            changes={"status": ["pending", "cancelled"], "reason": reason},
        )


def _check_can_supersede(item):
    if item.superseded_at is not None:
        raise ValidationError("This plan item has already been changed or stopped.")
    if item.care_plan.status != CarePlan.Status.ACTIVE:
        raise ValidationError("Only items of an active care plan can be changed or stopped.")
    if Appointment.objects.filter(plan_item=item, status=Appointment.Status.BOOKED).exists():
        raise ValidationError("This follow-up has a booked appointment. Reschedule the appointment instead.")


def _supersede(item, *, actor, now):
    item.superseded_at = now
    item.save(update_fields=["superseded_at"])
    log_action(actor=actor, action=AuditLog.Action.UPDATE, obj=item, changes={"superseded_at": now.isoformat()})


@transaction.atomic
def replace_plan_item(item, *, actor, now=None, **changes):
    """Change a live plan item by superseding it with a new version.

    Pending tasks of the old item due from `now` onwards are cancelled. Tasks
    already due (whatever their status) are left exactly as they are.
    """
    _check_can_supersede(item)
    unknown = set(changes) - set(_VERSIONED_FIELDS)
    if unknown:
        raise ValidationError(f"Cannot change: {', '.join(sorted(unknown))}")
    if all(getattr(item, field) == value for field, value in changes.items()):
        raise ValidationError("Nothing was changed.")

    now = now or timezone.now()
    new_item = PlanItem(
        clinic_id=item.clinic_id,
        care_plan=item.care_plan,
        replaces=item,
        **{field: getattr(item, field) for field in _VERSIONED_FIELDS},
    )
    for field, value in changes.items():
        setattr(new_item, field, value)
    new_item.full_clean()

    _supersede(item, actor=actor, now=now)
    new_item.save()
    log_action(
        actor=actor,
        action=AuditLog.Action.CREATE,
        obj=new_item,
        changes={"replaces": item.pk, **{k: _jsonable(v) for k, v in changes.items()}},
    )
    _cancel_pending_tasks(
        item.tasks.filter(due_at__gte=now), actor=actor, reason=f"replaced by plan item {new_item.pk}"
    )
    generate_tasks(new_item, actor=actor, now=now)
    return new_item


@transaction.atomic
def stop_plan_item(item, *, actor, now=None):
    """Stop a live item early: its pending tasks from now on are cancelled."""
    _check_can_supersede(item)
    now = now or timezone.now()
    _supersede(item, actor=actor, now=now)
    _cancel_pending_tasks(item.tasks.filter(due_at__gte=now), actor=actor, reason="item stopped")
    return item


@transaction.atomic
def end_plan(plan, *, actor, now=None):
    """Mark an active plan completed. Anything still pending is cancelled."""
    if plan.status != CarePlan.Status.ACTIVE:
        raise ValidationError("Only an active care plan can be ended.")
    if Appointment.objects.filter(plan_item__care_plan=plan, status=Appointment.Status.BOOKED).exists():
        raise ValidationError("Record the outcome of booked follow-up appointments before ending the plan.")
    plan.status = CarePlan.Status.COMPLETED
    plan.save(update_fields=["status"])
    log_action(actor=actor, action=AuditLog.Action.STATUS_CHANGE, obj=plan, changes={"status": ["active", "completed"]})
    _cancel_pending_tasks(Task.objects.filter(plan_item__care_plan=plan), actor=actor, reason="care plan ended")
    return plan


@transaction.atomic
def cancel_draft_plan(plan, *, actor):
    if plan.status != CarePlan.Status.DRAFT:
        raise ValidationError("Only a draft care plan can be discarded.")
    plan.status = CarePlan.Status.CANCELLED
    plan.save(update_fields=["status"])
    log_action(actor=actor, action=AuditLog.Action.STATUS_CHANGE, obj=plan, changes={"status": ["draft", "cancelled"]})
    return plan


# --- doses --------------------------------------------------------------------


def can_respond_to_dose(task, *, now=None, grace=None):
    """A patient can tap Taken/Skipped on a dose from the start of its due day
    (clinic time) until the grace window after its due time closes."""
    now = now or timezone.now()
    grace = grace if grace is not None else grace_window(task.clinic_id)
    if task.plan_item.type != PlanItem.Type.MEDICINE or task.status not in RESPONDABLE_STATUSES:
        return False
    if task.plan_item.care_plan.status != CarePlan.Status.ACTIVE:
        return False
    tz = ZoneInfo(task.plan_item.clinic.timezone)
    start_of_due_day = datetime.combine(task.due_at.astimezone(tz).date(), time.min, tzinfo=tz)
    return start_of_due_day <= now <= task.due_at + grace


@transaction.atomic
def record_dose_response(task, *, patient, taken, now=None):
    """Record the patient's Taken/Skipped tap. The answer can be changed until
    the grace window closes; every change is audited."""
    if task.patient_id != patient.pk:
        raise PermissionDenied
    now = now or timezone.now()
    if not can_respond_to_dose(task, now=now):
        raise ValidationError("This dose can no longer be updated.")

    old_status = task.status
    new_status = Task.Status.DONE if taken else Task.Status.SKIPPED
    if old_status == new_status:
        return task
    # Conditional update so a concurrent "missed" run can't be overwritten.
    updated = Task.objects.filter(pk=task.pk, status=old_status).update(status=new_status, completed_at=now)
    if not updated:
        raise ValidationError("This dose was updated elsewhere. Please reload the page.")
    log_action(
        actor=patient,
        action=AuditLog.Action.STATUS_CHANGE,
        obj=task,
        changes={"status": [old_status, new_status]},
    )
    task.refresh_from_db()
    return task


def mark_missed_tasks(*, now=None):
    """Mark overdue pending tasks as missed. Safe to run repeatedly.

    - Doses: missed once the clinic's grace window after the due time has passed.
    - Lab tests: missed once the due date is over with no report uploaded
      (and a lab_test_missed alert is raised).
    - Follow-ups are not touched: their status follows the appointment outcome.
    Returns the number of tasks marked missed.
    """
    now = now or timezone.now()
    live_pending = Task.objects.filter(status=Task.Status.PENDING, plan_item__care_plan__status=CarePlan.Status.ACTIVE)
    marked = 0
    for clinic_id in Clinic.objects.values_list("pk", flat=True):
        overdue = live_pending.filter(clinic_id=clinic_id).select_related("plan_item")
        doses = overdue.filter(plan_item__type=PlanItem.Type.MEDICINE, due_at__lt=now - grace_window(clinic_id))
        labs = overdue.filter(
            plan_item__type=PlanItem.Type.LAB_TEST, lab_report="", due_at__lte=now - LAB_TEST_MISSED_AFTER
        )
        for task in [*doses, *labs]:
            # Conditional update so a patient's tap in the same instant wins cleanly.
            if Task.objects.filter(pk=task.pk, status=Task.Status.PENDING).update(status=Task.Status.MISSED):
                log_action(
                    actor=None,
                    action=AuditLog.Action.STATUS_CHANGE,
                    obj=task,
                    changes={"status": ["pending", "missed"], "reason": "automatic: overdue"},
                )
                marked += 1
                if task.plan_item.type == PlanItem.Type.LAB_TEST:
                    raise_alert(Alert.SourceRule.LAB_TEST_MISSED, task=task)
    return marked


# --- follow-up appointments ---------------------------------------------------------


@transaction.atomic
def book_appointment(task, *, actor, scheduled_at):
    if task.plan_item.type != PlanItem.Type.FOLLOW_UP:
        raise ValidationError("Appointments can only be booked for follow-ups.")
    if task.status != Task.Status.PENDING:
        raise ValidationError("This follow-up is no longer pending.")
    if Appointment.objects.filter(task=task).exists():
        raise ValidationError("This follow-up already has an appointment.")
    appointment = Appointment.objects.create(
        clinic_id=task.clinic_id, patient_id=task.patient_id, plan_item=task.plan_item,
        task=task, scheduled_at=scheduled_at,
    )
    log_action(
        actor=actor, action=AuditLog.Action.CREATE, obj=appointment,
        changes={"task": task.pk, "scheduled_at": scheduled_at.isoformat()},
    )
    return appointment


@transaction.atomic
def reschedule_appointment(appointment, *, actor, scheduled_at):
    if appointment.status != Appointment.Status.BOOKED:
        raise ValidationError("Only a booked appointment can be rescheduled.")
    old = appointment.scheduled_at
    appointment.scheduled_at = scheduled_at
    appointment.save(update_fields=["scheduled_at"])
    log_action(
        actor=actor, action=AuditLog.Action.UPDATE, obj=appointment,
        changes={"scheduled_at": [old.isoformat(), scheduled_at.isoformat()]},
    )
    return appointment


@transaction.atomic
def record_attendance(appointment, *, actor, attended, now=None):
    """Staff record the outcome. The follow-up task becomes done or missed;
    not attended raises a follow_up_not_attended alert."""
    if appointment.status != Appointment.Status.BOOKED:
        raise ValidationError("The outcome of this appointment has already been recorded.")
    now = now or timezone.now()
    tz = ZoneInfo(appointment.plan_item.clinic.timezone)
    if now.astimezone(tz).date() < appointment.scheduled_at.astimezone(tz).date():
        raise ValidationError("The outcome can be recorded from the day of the appointment.")

    new_status = Appointment.Status.ATTENDED if attended else Appointment.Status.NOT_ATTENDED
    appointment.status = new_status
    appointment.save(update_fields=["status"])
    log_action(
        actor=actor, action=AuditLog.Action.STATUS_CHANGE, obj=appointment,
        changes={"status": ["booked", new_status]},
    )

    task = appointment.task
    if task and task.status == Task.Status.PENDING:
        task.status = Task.Status.DONE if attended else Task.Status.MISSED
        task.completed_at = now if attended else None
        task.save(update_fields=["status", "completed_at"])
        log_action(
            actor=actor, action=AuditLog.Action.STATUS_CHANGE, obj=task,
            changes={"status": ["pending", task.status], "reason": f"appointment {new_status}"},
        )
    if not attended and task:
        raise_alert(Alert.SourceRule.FOLLOW_UP_NOT_ATTENDED, task=task)
    return appointment


# --- lab reports --------------------------------------------------------------------


def validate_lab_report(upload):
    ext = os.path.splitext(upload.name)[1].lower()
    if ext not in _REPORT_SIGNATURES:
        raise ValidationError("Please upload a PDF, JPG or PNG file.")
    if upload.size > MAX_REPORT_BYTES:
        raise ValidationError("The file is too large (maximum 10 MB).")
    upload.seek(0)
    head = upload.read(16)
    upload.seek(0)
    if not any(head.startswith(sig) for sig in _REPORT_SIGNATURES[ext]):
        raise ValidationError("The file doesn't look like a valid PDF, JPG or PNG.")


@transaction.atomic
def upload_lab_report(task, *, actor, upload, now=None):
    """Attach a lab report (by the patient or clinic staff). The task becomes
    done, even when the upload is late; any alert already raised stays in the
    inbox for staff."""
    allowed = actor.clinic_id == task.clinic_id and (
        actor.is_clinic_staff or (actor.is_patient and task.patient_id == actor.pk)
    )
    if not allowed:
        raise PermissionDenied
    if task.plan_item.type != PlanItem.Type.LAB_TEST:
        raise ValidationError("Reports can only be uploaded for lab tests.")
    if task.status == Task.Status.CANCELLED:
        raise ValidationError("This lab test was cancelled.")
    validate_lab_report(upload)

    now = now or timezone.now()
    old_status = task.status
    task.lab_report.save(upload.name, upload, save=False)
    task.status = Task.Status.DONE
    task.completed_at = now
    task.save(update_fields=["lab_report", "status", "completed_at"])
    log_action(
        actor=actor, action=AuditLog.Action.STATUS_CHANGE, obj=task,
        changes={"status": [old_status, "done"], "lab_report": task.lab_report.name},
    )
    return task
