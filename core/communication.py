"""Health notes from patients and messages between a patient and clinic staff.

Carely never interprets a note: it stores what the patient wrote (no severity
rating), adds context (treatment day, current medicines) and raises a
"new health note" alert so staff read it.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from .alerts import raise_alert
from .audit import log_action
from .models import Alert, Appointment, AuditLog, CarePlan, HealthNote, Message, PlanItem, User

MAX_TEXT = 2000
MAX_TAGS = 10
MAX_TAG_LENGTH = 30


def parse_tags(raw):
    """"dizzy, Tired, dizzy" -> ["dizzy", "Tired"] (comma-separated, de-duplicated)."""
    tags, seen = [], set()
    for tag in (raw or "").split(","):
        tag = " ".join(tag.split())
        if tag and tag.lower() not in seen:
            if len(tag) > MAX_TAG_LENGTH:
                raise ValidationError(f"Tags can be at most {MAX_TAG_LENGTH} characters.")
            seen.add(tag.lower())
            tags.append(tag)
    if len(tags) > MAX_TAGS:
        raise ValidationError(f"Please use at most {MAX_TAGS} tags.")
    return tags


def _clean_text(text, what):
    text = (text or "").strip()
    if not text:
        raise ValidationError(f"Please write a {what}.")
    if len(text) > MAX_TEXT:
        raise ValidationError(f"Please keep the {what} under {MAX_TEXT} characters.")
    return text


def treatment_context(patient, *, today):
    """(treatment day, current medicines) from the patient's active plan.
    Day 1 is the first day of the plan's earliest item."""
    plan = patient.care_plans.filter(status=CarePlan.Status.ACTIVE).first()
    if not plan:
        return None, []
    items = list(plan.items.all())
    first_day = min((i.start_date for i in items), default=None)
    treatment_day = (today - first_day).days + 1 if first_day and first_day <= today else None
    medicines = [
        f"{i.name} {i.dose_text}".strip()
        for i in items
        if i.type == PlanItem.Type.MEDICINE and i.superseded_at is None and i.start_date <= today <= i.end_date
    ]
    return treatment_day, sorted(medicines)


@transaction.atomic
def create_health_note(*, patient, text, tags="", now=None, actor=None, source_message=None):
    """Save a patient's health note and raise a "new health note" alert.
    `now` is when the patient reported it (used for treatment day and medicines)."""
    if not patient.is_patient:
        raise PermissionDenied
    now = now or timezone.now()
    today = timezone.localdate(now, timezone=patient.clinic.tzinfo)
    treatment_day, medicines = treatment_context(patient, today=today)
    note = HealthNote.objects.create(
        clinic_id=patient.clinic_id,
        patient=patient,
        text=_clean_text(text, "note"),
        tags=parse_tags(tags),
        treatment_day=treatment_day,
        current_medicines=medicines,
        source_message=source_message,
    )
    changes = {"tags": note.tags}
    if source_message:
        changes["source_message"] = source_message.pk
    log_action(actor=actor or patient, action=AuditLog.Action.CREATE, obj=note, changes=changes)
    raise_alert(Alert.SourceRule.HEALTH_NOTE_SUBMITTED, health_note=note)
    return note


def convert_message_to_note(message, *, staff):
    """Staff turn a patient's message that describes a symptom into a health
    note, so it is alerted and triaged like any other note. The message is
    left unchanged; the note links back to it."""
    if not staff.is_clinic_staff or staff.clinic_id != message.clinic_id:
        raise PermissionDenied
    if message.sender_id != message.patient_id:
        raise ValidationError("Only messages written by the patient can become health notes.")
    if HealthNote.objects.filter(source_message=message).exists():
        raise ValidationError("This message has already been turned into a health note.")
    return create_health_note(
        patient=message.patient, text=message.body, now=message.created_at,
        actor=staff, source_message=message,
    )


# --- messages ----------------------------------------------------------------------


def link_options(patient):
    """Things a message can optionally be about: current medicines, appointments, recent notes."""
    options = []
    plan = patient.care_plans.filter(status=CarePlan.Status.ACTIVE).first()
    if plan:
        for item in plan.items.filter(type=PlanItem.Type.MEDICINE, superseded_at__isnull=True).order_by("name"):
            options.append((f"item:{item.pk}", f"Medicine: {item.name} {item.dose_text}"))
    for appt in Appointment.objects.filter(patient=patient).select_related("plan_item").order_by("-scheduled_at")[:5]:
        local = timezone.localtime(appt.scheduled_at, patient.clinic.tzinfo)
        options.append((f"appointment:{appt.pk}", f"Appointment: {appt.plan_item.name}, {local:%d %b %H:%M}"))
    for note in HealthNote.objects.filter(patient=patient)[:5]:
        local = timezone.localtime(note.created_at, patient.clinic.tzinfo)
        options.append((f"note:{note.pk}", f"Health note of {local:%d %b %H:%M}"))
    return options


def resolve_link(patient, value):
    """Turn an option value like "item:5" into link fields, checking it belongs to the patient."""
    if not value:
        return {}
    if value not in {v for v, _ in link_options(patient)}:
        raise ValidationError("Please choose what the message is about from the list.")
    kind, pk = value.split(":")
    if kind == "item":
        return {"plan_item": PlanItem.objects.get(pk=pk, care_plan__patient=patient)}
    if kind == "appointment":
        return {"appointment": Appointment.objects.get(pk=pk, patient=patient)}
    return {"health_note": HealthNote.objects.get(pk=pk, patient=patient)}


@transaction.atomic
def send_message(*, sender, patient, body, link="", now=None):
    """Send a message in `patient`'s thread, by the patient or by their clinic's staff."""
    allowed = patient.is_patient and sender.clinic_id == patient.clinic_id and (
        sender.is_clinic_staff or sender.pk == patient.pk
    )
    if not allowed:
        raise PermissionDenied
    message = Message.objects.create(
        clinic_id=patient.clinic_id,
        patient=patient,
        sender=sender,
        body=_clean_text(body, "message"),
        **resolve_link(patient, link),
    )
    # Log that a message was sent, but keep its content out of the audit log.
    log_action(actor=sender, action=AuditLog.Action.CREATE, obj=message, changes={"patient": patient.pk})
    return message


def _unread_from_other_side(reader, messages):
    if reader.is_patient:
        return messages.filter(read_at__isnull=True, sender__role=User.Role.STAFF)
    return messages.filter(read_at__isnull=True, sender__role=User.Role.PATIENT)


def mark_thread_read(*, reader, patient, now=None):
    """Mark messages from the other side as read when the reader opens the thread."""
    messages = Message.objects.filter(patient=patient, clinic_id=reader.clinic_id)
    return _unread_from_other_side(reader, messages).update(read_at=now or timezone.now())


def unread_count(user):
    if user.is_patient:
        return _unread_from_other_side(user, Message.objects.filter(patient=user)).count()
    if user.is_clinic_staff:
        return _unread_from_other_side(user, Message.objects.filter(clinic_id=user.clinic_id)).count()
    return 0
