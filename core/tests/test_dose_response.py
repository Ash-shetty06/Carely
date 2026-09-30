from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from core.models import AuditLog, ClinicSettings, PlanItem, Task, User
from core.services import record_dose_response

from .helpers import make_active_plan, make_clinic, make_item, make_task, make_user

IST = ZoneInfo("Asia/Kolkata")


def ist(d, hh, mm=0):
    return datetime(2026, 10, d, hh, mm, tzinfo=IST)


class DoseResponseTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic(grace_minutes=60)
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.plan = make_active_plan(self.clinic, self.patient, self.staff)
        self.item = make_item(self.plan)
        self.task = make_task(self.item, ist(1, 8))  # due 08:00, grace until 09:00

    def respond(self, taken, now, task=None, patient=None):
        return record_dose_response(task or self.task, patient=patient or self.patient, taken=taken, now=now)

    def test_taken_marks_done_with_time_and_audit(self):
        self.respond(True, ist(1, 8, 10))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.DONE)
        self.assertEqual(self.task.completed_at, ist(1, 8, 10))
        log = AuditLog.objects.get(model="core.Task", record_id=self.task.pk)
        self.assertEqual(log.actor, self.patient)
        self.assertEqual(log.changes["status"], ["pending", "done"])

    def test_skipped_marks_skipped(self):
        self.respond(False, ist(1, 8, 10))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.SKIPPED)

    def test_answer_can_be_changed_within_grace_window(self):
        self.respond(True, ist(1, 8, 10))
        self.respond(False, ist(1, 8, 50))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.SKIPPED)
        self.assertEqual(self.task.completed_at, ist(1, 8, 50))
        self.assertEqual(AuditLog.objects.filter(model="core.Task", record_id=self.task.pk).count(), 2)

    def test_can_tap_earlier_on_the_due_day(self):
        self.respond(True, ist(1, 6))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.DONE)

    def test_cannot_tap_on_an_earlier_day(self):
        with self.assertRaises(ValidationError):
            self.respond(True, datetime(2026, 9, 30, 23, 0, tzinfo=IST))

    def test_locked_after_grace_window(self):
        self.respond(True, ist(1, 8, 30))
        with self.assertRaises(ValidationError):
            self.respond(False, ist(1, 9, 1))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.DONE)

    def test_uses_clinic_grace_setting(self):
        ClinicSettings.objects.filter(clinic=self.clinic).update(grace_window_minutes=30)
        with self.assertRaises(ValidationError):
            self.respond(True, ist(1, 8, 45))

    def test_late_evening_dose_can_be_tapped_after_midnight_within_grace(self):
        late = make_task(self.item, ist(1, 23, 30))
        self.respond(True, ist(2, 0, 15), task=late)
        late.refresh_from_db()
        self.assertEqual(late.status, Task.Status.DONE)

    def test_missed_dose_cannot_be_changed(self):
        self.task.status = Task.Status.MISSED
        self.task.save()
        with self.assertRaises(ValidationError):
            self.respond(True, ist(1, 8, 30))

    def test_lab_test_task_cannot_be_tapped(self):
        lab = make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="Glucose")
        lab_task = make_task(lab, ist(1, 23, 59))
        with self.assertRaises(ValidationError):
            self.respond(True, ist(1, 10), task=lab_task)

    def test_another_patient_cannot_respond(self):
        other = make_user(self.clinic, "other", User.Role.PATIENT)
        with self.assertRaises(PermissionDenied):
            self.respond(True, ist(1, 8, 10), patient=other)
