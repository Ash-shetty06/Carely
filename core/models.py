"""Carely data model.

Every table except Clinic carries clinic_id so that all queries can be scoped
to a single clinic on the server.
"""

import os
import uuid
from zoneinfo import ZoneInfo

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import FileExtensionValidator, MinValueValidator
from django.db import models
from django.db.models import Q


class TimeStamped(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class Clinic(TimeStamped):
    name = models.CharField(max_length=200)
    # IANA name, e.g. "Asia/Kolkata". Dose times are interpreted in this zone.
    timezone = models.CharField(max_length=64, default="UTC")

    @property
    def tzinfo(self):
        return ZoneInfo(self.timezone)

    def __str__(self):
        return self.name


class ClinicScoped(TimeStamped):
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="+")

    class Meta:
        abstract = True


class ClinicSettings(TimeStamped):
    """Clinic-configurable thresholds used by the task and alert rules."""

    clinic = models.OneToOneField(Clinic, on_delete=models.CASCADE, related_name="settings")
    # Minutes after a dose's due time before it is automatically marked missed.
    grace_window_minutes = models.PositiveIntegerField(default=60, validators=[MinValueValidator(1)])
    # Consecutive missed doses of the same medicine that raise a staff alert.
    missed_dose_threshold = models.PositiveSmallIntegerField(default=2, validators=[MinValueValidator(1)])

    class Meta:
        verbose_name_plural = "clinic settings"


class User(AbstractUser):
    class Role(models.TextChoices):
        PATIENT = "patient"
        STAFF = "staff"

    # Replace Django's first/last name with a single full name.
    first_name = None
    last_name = None

    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT, related_name="users")
    role = models.CharField(max_length=10, choices=Role.choices)
    full_name = models.CharField(max_length=200)
    # Set when staff give the patient a temporary password; cleared once they choose their own.
    must_change_password = models.BooleanField(default=False)
    # Login safety: repeated failed logins lock the account for a while.
    failed_login_count = models.PositiveSmallIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)

    REQUIRED_FIELDS = ["clinic", "role", "full_name"]

    @property
    def is_patient(self):
        return self.role == self.Role.PATIENT

    @property
    def is_clinic_staff(self):
        return self.role == self.Role.STAFF

    def get_full_name(self):
        return self.full_name

    def get_short_name(self):
        return self.full_name

    def __str__(self):
        return f"{self.full_name} ({self.role})"


class CarePlan(ClinicScoped):
    class Status(models.TextChoices):
        DRAFT = "draft"
        ACTIVE = "active"
        COMPLETED = "completed"
        CANCELLED = "cancelled"

    patient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="care_plans")
    created_by = models.ForeignKey(User, on_delete=models.PROTECT, related_name="created_care_plans")
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    # Entered by staff from the doctor's notes; the doctor has no login.
    reviewed_by_doctor = models.CharField(max_length=200, blank=True)
    reviewed_on = models.DateField(null=True, blank=True)
    activated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="careplan_live_requires_review",
                condition=Q(status__in=["draft", "cancelled"])
                | (Q(reviewed_on__isnull=False) & ~Q(reviewed_by_doctor="")),
            ),
            models.UniqueConstraint(
                fields=["patient"],
                condition=Q(status="active"),
                name="careplan_one_active_per_patient",
            ),
        ]

    def clean(self):
        if self.patient_id and not self.patient.is_patient:
            raise ValidationError({"patient": "Care plans can only belong to a patient."})
        if self.created_by_id and not self.created_by.is_clinic_staff:
            raise ValidationError({"created_by": "Care plans are created by staff."})
        if self.patient_id and self.patient.clinic_id != self.clinic_id:
            raise ValidationError({"patient": "Patient belongs to a different clinic."})

    def __str__(self):
        return f"Care plan #{self.pk} for {self.patient.full_name} ({self.status})"


class PlanItem(ClinicScoped):
    """One prescribed item. Items are never edited once live: a change creates a
    new item that `replaces` the old one, and the old one gets `superseded_at`."""

    class Type(models.TextChoices):
        MEDICINE = "medicine"
        LAB_TEST = "lab_test", "Lab test"
        FOLLOW_UP = "follow_up", "Follow-up"

    care_plan = models.ForeignKey(CarePlan, on_delete=models.PROTECT, related_name="items")
    type = models.CharField(max_length=10, choices=Type.choices)
    name = models.CharField(max_length=200)  # medicine name, test name, or follow-up reason
    dose_text = models.CharField(max_length=100, blank=True)  # e.g. "500 mg"; medicines only
    # Local clock times, e.g. ["08:00", "20:00"]; medicines only.
    dose_times = models.JSONField(default=list, blank=True)
    # Medicines: first and last day. Lab tests / follow-ups: start_date is the due date.
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    notes = models.TextField(blank=True)
    replaces = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="replaced_by"
    )
    superseded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                name="planitem_end_after_start",
                condition=Q(end_date__isnull=True) | Q(end_date__gte=models.F("start_date")),
            ),
            models.CheckConstraint(
                name="planitem_end_date_only_for_medicine",
                condition=(Q(type="medicine") & Q(end_date__isnull=False))
                | (~Q(type="medicine") & Q(end_date__isnull=True)),
            ),
        ]

    def clean(self):
        if self.type == self.Type.MEDICINE:
            if not self.dose_text:
                raise ValidationError({"dose_text": "Medicines need a dose."})
            if not self.dose_times:
                raise ValidationError({"dose_times": "Medicines need at least one dose time."})
            for value in self.dose_times:
                if not _is_hh_mm(value):
                    raise ValidationError({"dose_times": f"'{value}' is not a valid HH:MM time."})
            if len(set(self.dose_times)) != len(self.dose_times):
                raise ValidationError({"dose_times": "Dose times must not repeat."})
        elif self.dose_times:
            raise ValidationError({"dose_times": "Only medicines have dose times."})

    def __str__(self):
        return f"{self.get_type_display()}: {self.name}"


def _is_hh_mm(value):
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return False
    hh, mm = value[:2], value[3:]
    return hh.isdigit() and mm.isdigit() and int(hh) < 24 and int(mm) < 60


def lab_report_path(task, filename):
    """Store reports under a random name so file names never reveal patient details."""
    ext = os.path.splitext(filename)[1].lower()
    return f"lab_reports/clinic_{task.clinic_id}/{uuid.uuid4().hex}{ext}"


class Task(ClinicScoped):
    class Status(models.TextChoices):
        PENDING = "pending"
        DONE = "done"
        SKIPPED = "skipped"
        MISSED = "missed"
        CANCELLED = "cancelled"

    patient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="tasks")
    plan_item = models.ForeignKey(PlanItem, on_delete=models.PROTECT, related_name="tasks")
    due_at = models.DateTimeField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    completed_at = models.DateTimeField(null=True, blank=True)  # when Taken/Skipped was tapped
    lab_report = models.FileField(
        upload_to=lab_report_path,
        blank=True,
        validators=[FileExtensionValidator(["pdf", "jpg", "jpeg", "png"])],
    )

    class Meta:
        ordering = ["due_at"]
        constraints = [
            models.UniqueConstraint(fields=["plan_item", "due_at"], name="task_unique_due_per_item"),
            models.CheckConstraint(
                name="task_done_or_skipped_has_completed_at",
                condition=~Q(status__in=["done", "skipped"]) | Q(completed_at__isnull=False),
            ),
        ]
        indexes = [models.Index(fields=["status", "due_at"], name="task_status_due_idx")]

    def __str__(self):
        return f"{self.plan_item.name} due {self.due_at:%Y-%m-%d %H:%M} UTC ({self.status})"


class Appointment(ClinicScoped):
    class Status(models.TextChoices):
        BOOKED = "booked"
        ATTENDED = "attended"
        NOT_ATTENDED = "not_attended", "Not attended"

    patient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="appointments")
    plan_item = models.ForeignKey(PlanItem, on_delete=models.PROTECT, related_name="appointments")
    # The follow-up task this appointment fulfils; its outcome drives the task status.
    task = models.OneToOneField(
        Task, on_delete=models.PROTECT, null=True, blank=True, related_name="appointment"
    )
    scheduled_at = models.DateTimeField()
    status = models.CharField(max_length=15, choices=Status.choices, default=Status.BOOKED)

    class Meta:
        ordering = ["scheduled_at"]


class HealthNote(ClinicScoped):
    """Free-text note from the patient. There is intentionally no severity field."""

    patient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="health_notes")
    text = models.TextField()
    tags = models.JSONField(default=list, blank=True)  # free-text tags
    treatment_day = models.PositiveIntegerField(null=True, blank=True)  # day N of the active plan
    current_medicines = models.JSONField(default=list, blank=True)  # snapshot at time of writing
    staff_outcome = models.TextField(blank=True)
    outcome_by = models.ForeignKey(
        User, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    outcome_at = models.DateTimeField(null=True, blank=True)
    # Set when staff turned a patient's message into this note.
    source_message = models.OneToOneField(
        "Message", on_delete=models.PROTECT, null=True, blank=True, related_name="converted_note"
    )

    class Meta:
        ordering = ["-created_at"]


class Alert(ClinicScoped):
    class SourceRule(models.TextChoices):
        MISSED_DOSE_THRESHOLD = "missed_dose_threshold", "Doses not taken in a row"
        LAB_TEST_MISSED = "lab_test_missed", "Lab test missed"
        FOLLOW_UP_NOT_ATTENDED = "follow_up_not_attended", "Follow-up not attended"
        HEALTH_NOTE_SUBMITTED = "health_note_submitted", "New health note"

    class Status(models.TextChoices):
        NEW = "new"
        SEEN = "seen"
        ACTED_ON = "acted_on", "Acted on"

    patient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="alerts")
    source_rule = models.CharField(max_length=30, choices=SourceRule.choices)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, null=True, blank=True, related_name="alerts")
    health_note = models.ForeignKey(
        HealthNote, on_delete=models.PROTECT, null=True, blank=True, related_name="alerts"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.NEW)
    outcome = models.TextField(blank=True)
    # Recorded by staff from the doctor; never generated by the system.
    doctors_advice = models.TextField(blank=True)
    seen_at = models.DateTimeField(null=True, blank=True)
    acted_by = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    acted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                name="alert_exactly_one_source",
                condition=Q(task__isnull=False, health_note__isnull=True)
                | Q(task__isnull=True, health_note__isnull=False),
            ),
            models.UniqueConstraint(
                fields=["source_rule", "task"],
                condition=Q(task__isnull=False),
                name="alert_unique_rule_per_task",
            ),
            models.UniqueConstraint(
                fields=["source_rule", "health_note"],
                condition=Q(health_note__isnull=False),
                name="alert_unique_rule_per_note",
            ),
        ]


class Message(ClinicScoped):
    """A message in a patient's thread with clinic staff."""

    patient = models.ForeignKey(User, on_delete=models.PROTECT, related_name="message_thread")
    sender = models.ForeignKey(User, on_delete=models.PROTECT, related_name="sent_messages")
    body = models.TextField()
    plan_item = models.ForeignKey(PlanItem, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    appointment = models.ForeignKey(
        Appointment, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    health_note = models.ForeignKey(
        HealthNote, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        constraints = [
            models.CheckConstraint(
                name="message_at_most_one_link",
                condition=~(Q(plan_item__isnull=False) & Q(appointment__isnull=False))
                & ~(Q(plan_item__isnull=False) & Q(health_note__isnull=False))
                & ~(Q(appointment__isnull=False) & Q(health_note__isnull=False)),
            ),
        ]


class AuditLog(ClinicScoped):
    class Action(models.TextChoices):
        CREATE = "create"
        UPDATE = "update"
        STATUS_CHANGE = "status_change"
        DELETE = "delete"

    actor = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=15, choices=Action.choices)
    model = models.CharField(max_length=50)  # e.g. "core.Task"
    record_id = models.PositiveBigIntegerField()
    changes = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["model", "record_id"], name="audit_record_idx")]
