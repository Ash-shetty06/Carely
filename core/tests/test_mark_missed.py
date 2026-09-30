from datetime import datetime
from io import StringIO
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.test import TestCase

from core.models import AuditLog, CarePlan, ClinicSettings, PlanItem, Task, User
from core.services import mark_missed_tasks

from .helpers import make_active_plan, make_clinic, make_item, make_task, make_user

IST = ZoneInfo("Asia/Kolkata")


def ist(d, hh, mm=0, ss=0):
    return datetime(2026, 10, d, hh, mm, ss, tzinfo=IST)


class MarkMissedTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic(grace_minutes=60)
        staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.plan = make_active_plan(self.clinic, self.patient, staff)
        self.item = make_item(self.plan)
        self.dose = make_task(self.item, ist(1, 8))

    def status(self, task):
        task.refresh_from_db()
        return task.status

    def test_dose_within_grace_stays_pending(self):
        self.assertEqual(mark_missed_tasks(now=ist(1, 9, 0)), 0)
        self.assertEqual(self.status(self.dose), Task.Status.PENDING)

    def test_dose_after_grace_becomes_missed_and_is_audited(self):
        self.assertEqual(mark_missed_tasks(now=ist(1, 9, 1)), 1)
        self.assertEqual(self.status(self.dose), Task.Status.MISSED)
        log = AuditLog.objects.get(model="core.Task", record_id=self.dose.pk)
        self.assertIsNone(log.actor)
        self.assertEqual(log.changes["status"], ["pending", "missed"])

    def test_running_twice_changes_nothing_more(self):
        mark_missed_tasks(now=ist(1, 9, 1))
        self.assertEqual(mark_missed_tasks(now=ist(1, 9, 6)), 0)

    def test_answered_doses_are_untouched(self):
        self.dose.status = Task.Status.DONE
        self.dose.completed_at = ist(1, 8, 5)
        self.dose.save()
        mark_missed_tasks(now=ist(2, 9))
        self.assertEqual(self.status(self.dose), Task.Status.DONE)

    def test_uses_clinic_grace_setting(self):
        ClinicSettings.objects.filter(clinic=self.clinic).update(grace_window_minutes=30)
        mark_missed_tasks(now=ist(1, 8, 31))
        self.assertEqual(self.status(self.dose), Task.Status.MISSED)

    def test_lab_test_missed_only_after_its_due_date_ends(self):
        lab = make_task(make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="Glucose"), ist(1, 23, 59))
        mark_missed_tasks(now=ist(1, 23, 59, 30))
        self.assertEqual(self.status(lab), Task.Status.PENDING)
        mark_missed_tasks(now=ist(2, 0, 0))
        self.assertEqual(self.status(lab), Task.Status.MISSED)

    def test_lab_test_with_report_is_not_missed(self):
        lab = make_task(
            make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="Glucose"), ist(1, 23, 59),
            lab_report="lab_reports/2026/10/fake.pdf",
        )
        mark_missed_tasks(now=ist(3, 0))
        self.assertEqual(self.status(lab), Task.Status.PENDING)

    def test_follow_up_is_left_to_the_appointment(self):
        visit = make_task(make_item(self.plan, type=PlanItem.Type.FOLLOW_UP, name="Visit"), ist(1, 23, 59))
        mark_missed_tasks(now=ist(5, 0))
        self.assertEqual(self.status(visit), Task.Status.PENDING)

    def test_tasks_of_inactive_plans_are_untouched(self):
        CarePlan.objects.filter(pk=self.plan.pk).update(status=CarePlan.Status.COMPLETED)
        mark_missed_tasks(now=ist(2, 9))
        self.assertEqual(self.status(self.dose), Task.Status.PENDING)

    def test_management_command(self):
        out = StringIO()
        call_command("mark_missed_tasks", stdout=out)
        self.assertRegex(out.getvalue(), r"Marked \d+ task\(s\) as missed\.")
