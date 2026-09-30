"""Rule-based alerts for clinic staff. No AI, no diagnosis: each alert only
states which rule fired and on which task or note.

Rules:
- missed_dose_threshold: N doses of the same medicine in a row not taken
  (missed or skipped). N is the clinic's missed_dose_threshold. Changed
  versions of a medicine count as the same medicine.
- lab_test_missed: a lab test's due date passed with no report uploaded.
- follow_up_not_attended: staff recorded that a follow-up was not attended.
- health_note_submitted: the patient sent a health note.
"""

from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from .audit import log_action
from .clinic_config import grace_window, missed_dose_threshold
from .models import Alert, AuditLog, CarePlan, Clinic, PlanItem, Task

NOT_TAKEN = {Task.Status.MISSED, Task.Status.SKIPPED}


def raise_alert(rule, *, task=None, health_note=None):
    """Create an alert once per rule and source. Returns (alert, created)."""
    source = task or health_note
    lookup = {"source_rule": rule, "task": task, "health_note": health_note}
    existing = Alert.objects.filter(**lookup).first()
    if existing:
        return existing, False
    try:
        with transaction.atomic():
            alert = Alert.objects.create(clinic_id=source.clinic_id, patient_id=source.patient_id, **lookup)
    except IntegrityError:  # raised concurrently by another run
        return Alert.objects.get(**lookup), False
    log_action(actor=None, action=AuditLog.Action.CREATE, obj=alert, changes={"source_rule": rule})
    return alert, True


def raise_missed_dose_alerts(*, now=None):
    """Check every live medicine for runs of doses not taken. Safe to run
    repeatedly: the same run of doses never raises a second alert.

    Only doses whose response window has closed are counted, so a patient who
    taps Skipped and then changes to Taken within the grace window is not
    counted. Returns the number of new alerts.
    """
    now = now or timezone.now()
    raised = 0
    for clinic_id in Clinic.objects.values_list("pk", flat=True):
        threshold = missed_dose_threshold(clinic_id)
        items = {
            item.pk: item
            for item in PlanItem.objects.filter(
                clinic_id=clinic_id, type=PlanItem.Type.MEDICINE, care_plan__status=CarePlan.Status.ACTIVE
            )
        }

        def medicine_key(item):
            # Follow `replaces` back to the first version so changes don't reset the count.
            while item.replaces_id in items:
                item = items[item.replaces_id]
            return item.pk

        finished = Task.objects.filter(
            plan_item_id__in=items,
            status__in=[Task.Status.DONE, *NOT_TAKEN],
            due_at__lt=now - grace_window(clinic_id),
        ).order_by("due_at")

        run_length = defaultdict(int)
        for task in finished:
            key = medicine_key(items[task.plan_item_id])
            if task.status == Task.Status.DONE:
                run_length[key] = 0
                continue
            run_length[key] += 1
            if run_length[key] >= threshold:
                _, created = raise_alert(Alert.SourceRule.MISSED_DOSE_THRESHOLD, task=task)
                raised += created
                run_length[key] = 0
    return raised


def mark_alert_seen(alert, *, actor, now=None):
    if alert.status != Alert.Status.NEW:
        return alert
    alert.status = Alert.Status.SEEN
    alert.seen_at = now or timezone.now()
    alert.save(update_fields=["status", "seen_at"])
    log_action(actor=actor, action=AuditLog.Action.STATUS_CHANGE, obj=alert, changes={"status": ["new", "seen"]})
    return alert


@transaction.atomic
def record_triage(alert, *, actor, outcome, doctors_advice="", now=None):
    """Staff record what they did. Doctor's advice is typed in by staff."""
    outcome = outcome.strip()
    if not outcome:
        raise ValidationError("Please describe the outcome.")
    now = now or timezone.now()
    old_status = alert.status
    alert.status = Alert.Status.ACTED_ON
    alert.outcome = outcome
    alert.doctors_advice = doctors_advice.strip()
    alert.seen_at = alert.seen_at or now
    alert.acted_by = actor
    alert.acted_at = now
    alert.save()
    if alert.health_note_id:
        # The outcome of a "new health note" alert is also the note's staff outcome.
        note = alert.health_note
        note.staff_outcome, note.outcome_by, note.outcome_at = alert.outcome, actor, now
        note.save(update_fields=["staff_outcome", "outcome_by", "outcome_at"])
    log_action(
        actor=actor,
        action=AuditLog.Action.STATUS_CHANGE,
        obj=alert,
        changes={"status": [old_status, "acted_on"], "outcome": alert.outcome, "doctors_advice": alert.doctors_advice},
    )
    return alert
