from datetime import datetime
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse

from core.alerts import raise_missed_dose_alerts, record_triage
from core.models import Alert, ClinicSettings, PlanItem, Task, User
from core.services import mark_missed_tasks, replace_plan_item

from .helpers import make_active_plan, make_clinic, make_item, make_task, make_user

IST = ZoneInfo("Asia/Kolkata")


def ist(d, hh, mm=0):
    return datetime(2026, 10, d, hh, mm, tzinfo=IST)


class MissedDoseAlertTests(TestCase):
    """Threshold 2 (default), grace 60 minutes, doses at 08:00 and 20:00."""

    def setUp(self):
        self.clinic = make_clinic(grace_minutes=60)
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.plan = make_active_plan(self.clinic, self.patient, self.staff)
        self.item = make_item(self.plan, dose_times=["08:00", "20:00"])
        self.doses = [make_task(self.item, ist(d, h)) for d in (1, 2, 3) for h in (8, 20)]

    def set_statuses(self, *statuses):
        for task, status in zip(self.doses, statuses):
            task.status = status
            task.completed_at = task.due_at if status in ("done", "skipped") else None
            task.save()

    def alerts(self):
        return list(Alert.objects.filter(source_rule=Alert.SourceRule.MISSED_DOSE_THRESHOLD).order_by("task__due_at"))

    def test_two_in_a_row_raises_one_alert_on_the_second(self):
        self.set_statuses("done", "missed", "missed")
        self.assertEqual(raise_missed_dose_alerts(now=ist(3, 12)), 1)
        [alert] = self.alerts()
        self.assertEqual(alert.task, self.doses[2])
        self.assertEqual((alert.patient, alert.status), (self.patient, Alert.Status.NEW))

    def test_skipped_counts_as_not_taken(self):
        self.set_statuses("skipped", "missed")
        self.assertEqual(raise_missed_dose_alerts(now=ist(2, 12)), 1)

    def test_taken_dose_resets_the_count(self):
        self.set_statuses("missed", "done", "missed", "done", "missed")
        self.assertEqual(raise_missed_dose_alerts(now=ist(3, 12)), 0)

    def test_running_again_does_not_duplicate(self):
        self.set_statuses("missed", "missed")
        raise_missed_dose_alerts(now=ist(2, 12))
        self.assertEqual(raise_missed_dose_alerts(now=ist(2, 13)), 0)
        self.assertEqual(len(self.alerts()), 1)

    def test_a_new_run_after_an_alert_raises_another(self):
        self.set_statuses("missed", "missed", "missed", "missed")
        raise_missed_dose_alerts(now=ist(3, 12))
        self.assertEqual([a.task for a in self.alerts()], [self.doses[1], self.doses[3]])

    def test_dose_still_inside_grace_window_is_not_counted(self):
        # Second dose was skipped at 20:05; the patient can still change it until 21:00.
        self.set_statuses("missed", "skipped")
        self.assertEqual(raise_missed_dose_alerts(now=ist(1, 20, 30)), 0)
        self.assertEqual(raise_missed_dose_alerts(now=ist(1, 21, 1)), 1)

    def test_uses_clinic_threshold(self):
        ClinicSettings.objects.filter(clinic=self.clinic).update(missed_dose_threshold=3)
        self.set_statuses("missed", "missed")
        self.assertEqual(raise_missed_dose_alerts(now=ist(2, 12)), 0)
        self.set_statuses("missed", "missed", "skipped")
        self.assertEqual(raise_missed_dose_alerts(now=ist(2, 12)), 1)

    def test_changed_medicine_counts_as_the_same_medicine(self):
        self.set_statuses("done", "missed")  # 1 Oct 20:00 missed
        for task in self.doses[2:]:
            task.delete()
        new_item = replace_plan_item(self.item, actor=self.staff, now=ist(1, 21), dose_text="1000 mg")
        first_new = new_item.tasks.order_by("due_at").first()  # 2 Oct 08:00
        first_new.status = Task.Status.MISSED
        first_new.save()
        self.assertEqual(raise_missed_dose_alerts(now=ist(2, 12)), 1)
        self.assertEqual(self.alerts()[0].task, first_new)

    def test_other_medicines_do_not_mix(self):
        other = make_item(self.plan, name="Amlodipine", dose_times=["08:00"])
        make_task(other, ist(1, 8), status=Task.Status.MISSED)
        self.set_statuses("done", "missed")  # one miss each, never two of the same medicine
        self.assertEqual(raise_missed_dose_alerts(now=ist(1, 22)), 0)

    def test_scheduled_check_marks_missed_then_alerts(self):
        # Nobody tapped anything on day 1.
        mark_missed_tasks(now=ist(1, 21, 30))
        self.assertEqual(raise_missed_dose_alerts(now=ist(1, 21, 30)), 1)


class OtherAlertRuleTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.plan = make_active_plan(self.clinic, self.patient, staff)

    def test_missed_lab_test_raises_alert_once(self):
        lab = make_task(make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="Glucose"), ist(1, 23, 59))
        mark_missed_tasks(now=ist(2, 0, 5))
        mark_missed_tasks(now=ist(2, 0, 10))
        alert = Alert.objects.get()
        self.assertEqual((alert.source_rule, alert.task), (Alert.SourceRule.LAB_TEST_MISSED, lab))


class TriageTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.patient.full_name = "Ravi Testcase"  # distinct from the "Patient" column heading
        self.patient.save()
        plan = make_active_plan(self.clinic, self.patient, self.staff)
        self.task = make_task(make_item(plan), ist(1, 8), status=Task.Status.MISSED)
        self.alert = Alert.objects.create(
            clinic=self.clinic, patient=self.patient, task=self.task, source_rule=Alert.SourceRule.MISSED_DOSE_THRESHOLD
        )
        self.client.force_login(self.staff)

    def test_inbox_lists_open_alerts_and_nav_shows_count(self):
        response = self.client.get(reverse("staff_alerts"))
        self.assertContains(response, "Doses not taken in a row")
        self.assertContains(response, self.patient.full_name)
        self.assertContains(response, '<span class="badge">1</span>', html=True)

    def test_opening_an_alert_marks_it_seen(self):
        self.client.get(reverse("staff_alert_detail", args=[self.alert.pk]))
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, Alert.Status.SEEN)
        self.assertIsNotNone(self.alert.seen_at)

    def test_recording_outcome_closes_the_alert(self):
        response = self.client.post(reverse("staff_alert_detail", args=[self.alert.pk]), {
            "outcome": "Called patient; they forgot doses while travelling.",
            "doctors_advice": "Continue as prescribed.",
        })
        self.assertRedirects(response, reverse("staff_alerts"))
        self.alert.refresh_from_db()
        self.assertEqual(self.alert.status, Alert.Status.ACTED_ON)
        self.assertEqual(self.alert.acted_by, self.staff)
        self.assertEqual(self.alert.doctors_advice, "Continue as prescribed.")
        self.assertNotContains(self.client.get(reverse("staff_alerts")), self.patient.full_name)
        self.assertContains(self.client.get(reverse("staff_alerts") + "?show=done"), self.patient.full_name)

    def test_outcome_is_required(self):
        response = self.client.post(reverse("staff_alert_detail", args=[self.alert.pk]), {"outcome": "  "})
        self.assertEqual(response.status_code, 200)
        self.alert.refresh_from_db()
        self.assertNotEqual(self.alert.status, Alert.Status.ACTED_ON)

    def test_record_triage_service_rejects_blank_outcome(self):
        with self.assertRaises(ValidationError):
            record_triage(self.alert, actor=self.staff, outcome="")

    def test_other_clinic_staff_cannot_see_alert(self):
        self.client.force_login(make_user(make_clinic(name="Other"), "otherstaff", User.Role.STAFF))
        self.assertEqual(self.client.get(reverse("staff_alert_detail", args=[self.alert.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("staff_alerts")), self.patient.full_name)

    def test_patient_cannot_see_alerts(self):
        self.client.force_login(self.patient)
        self.assertEqual(self.client.get(reverse("staff_alerts")).status_code, 403)
        self.assertEqual(self.client.get(reverse("staff_alert_detail", args=[self.alert.pk])).status_code, 403)
