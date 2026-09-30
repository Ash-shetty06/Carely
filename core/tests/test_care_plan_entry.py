from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog, CarePlan, PlanItem, Task, User

from .helpers import make_clinic, make_user

IST = ZoneInfo("Asia/Kolkata")


def local_today():
    return timezone.now().astimezone(IST).date()


class CarePlanEntryTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.client.force_login(self.staff)
        self.tomorrow = local_today() + timedelta(days=1)

    # --- helpers ---

    def new_plan(self):
        self.client.post(reverse("staff_new_care_plan", args=[self.patient.pk]))
        return CarePlan.objects.get(patient=self.patient, status=CarePlan.Status.DRAFT)

    def add(self, plan, item_type, **data):
        payload = {f"{item_type}-{k}": v for k, v in data.items()}
        return self.client.post(reverse("staff_add_item", args=[plan.pk, item_type]), payload)

    def add_metformin(self, plan):
        return self.add(
            plan, "medicine", name="Metformin", dose_text="500 mg", dose_times="20:00, 8:00",
            start_date=self.tomorrow.isoformat(), end_date=(self.tomorrow + timedelta(days=6)).isoformat(),
        )

    def activate(self, plan, **overrides):
        data = {"reviewed_by_doctor": "Dr. Test", "reviewed_on": local_today().isoformat(), "confirm": "on"}
        data.update(overrides)
        return self.client.post(reverse("staff_activate_plan", args=[plan.pk]), data)

    def live_plan(self):
        plan = self.new_plan()
        self.add_metformin(plan)
        self.activate(plan)
        plan.refresh_from_db()
        return plan

    # --- patients ---

    def test_staff_can_add_a_patient(self):
        response = self.client.post(reverse("staff_add_patient"), {
            "full_name": "Meena Example", "username": "meena", "password": "a-long-temp-pass-9", "password_again": "a-long-temp-pass-9",
        })
        patient = User.objects.get(username="meena")
        self.assertRedirects(response, reverse("staff_patient_detail", args=[patient.pk]))
        self.assertEqual((patient.role, patient.clinic_id), (User.Role.PATIENT, self.clinic.pk))
        self.assertTrue(patient.check_password("a-long-temp-pass-9"))
        log = AuditLog.objects.get(model="core.User", record_id=patient.pk)
        self.assertNotIn("password", str(log.changes))

    def test_add_patient_rejects_taken_username_and_weak_or_mismatched_passwords(self):
        cases = [
            {"username": "PATIENT", "password": "a-long-temp-pass-9", "password_again": "a-long-temp-pass-9"},
            {"username": "new1", "password": "a-long-temp-pass-9", "password_again": "different-pass-9"},
            {"username": "new2", "password": "123", "password_again": "123"},
        ]
        for data in cases:
            with self.subTest(data=data):
                response = self.client.post(reverse("staff_add_patient"), {"full_name": "X Y", **data})
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "field-error")
        self.assertEqual(User.objects.filter(role=User.Role.PATIENT).count(), 1)

    # --- drafts ---

    def test_new_plan_is_a_draft_and_only_one_open_plan_allowed(self):
        plan = self.new_plan()
        self.assertEqual(plan.created_by, self.staff)
        self.client.post(reverse("staff_new_care_plan", args=[self.patient.pk]))
        self.assertEqual(CarePlan.objects.filter(patient=self.patient).count(), 1)

    def test_add_medicine_parses_and_sorts_dose_times(self):
        plan = self.new_plan()
        self.assertRedirects(self.add_metformin(plan), reverse("staff_care_plan", args=[plan.pk]))
        item = plan.items.get()
        self.assertEqual(item.dose_times, ["08:00", "20:00"])
        self.assertEqual(Task.objects.count(), 0)  # drafts have no tasks

    def test_invalid_item_shows_errors(self):
        plan = self.new_plan()
        response = self.add(plan, "medicine", name="Metformin", dose_text="500 mg", dose_times="8am",
                            start_date=self.tomorrow.isoformat(), end_date=self.tomorrow.isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "is not a time like 08:00", status_code=400)
        response = self.add(plan, "medicine", name="Metformin", dose_text="500 mg", dose_times="08:00",
                            start_date=self.tomorrow.isoformat(), end_date=local_today().isoformat())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(plan.items.exists())

    def test_lab_test_and_follow_up_have_only_a_due_date(self):
        plan = self.new_plan()
        self.add(plan, "lab_test", name="Fasting glucose", start_date=self.tomorrow.isoformat())
        self.add(plan, "follow_up", name="Follow-up visit", start_date=self.tomorrow.isoformat())
        self.assertEqual(sorted(plan.items.values_list("type", flat=True)), ["follow_up", "lab_test"])
        self.assertTrue(all(i.end_date is None and i.dose_times == [] for i in plan.items.all()))

    def test_remove_item_from_draft(self):
        plan = self.new_plan()
        self.add_metformin(plan)
        self.client.post(reverse("staff_remove_item", args=[plan.items.get().pk]))
        self.assertFalse(plan.items.exists())

    def test_discard_draft(self):
        plan = self.new_plan()
        self.client.post(reverse("staff_end_plan", args=[plan.pk]))
        plan.refresh_from_db()
        self.assertEqual(plan.status, CarePlan.Status.CANCELLED)

    # --- going live ---

    def test_review_confirmation_is_required(self):
        plan = self.new_plan()
        self.add_metformin(plan)
        response = self.activate(plan, confirm="")
        self.assertEqual(response.status_code, 400)
        plan.refresh_from_db()
        self.assertEqual(plan.status, CarePlan.Status.DRAFT)

    def test_review_date_cannot_be_in_the_future(self):
        plan = self.new_plan()
        self.add_metformin(plan)
        response = self.activate(plan, reviewed_on=(local_today() + timedelta(days=2)).isoformat())
        self.assertContains(response, "can&#x27;t be in the future", status_code=400)

    def test_empty_plan_cannot_go_live(self):
        plan = self.new_plan()
        self.activate(plan)
        plan.refresh_from_db()
        self.assertEqual(plan.status, CarePlan.Status.DRAFT)

    def test_going_live_records_review_and_creates_tasks(self):
        plan = self.live_plan()
        self.assertEqual(plan.status, CarePlan.Status.ACTIVE)
        self.assertEqual(plan.reviewed_by_doctor, "Dr. Test")
        self.assertEqual(plan.reviewed_on, local_today())
        self.assertEqual(Task.objects.filter(patient=self.patient).count(), 14)

    # --- live changes ---

    def test_add_item_to_live_plan_creates_tasks(self):
        plan = self.live_plan()
        self.add(plan, "lab_test", name="Fasting glucose", start_date=self.tomorrow.isoformat())
        lab = plan.items.get(type=PlanItem.Type.LAB_TEST)
        self.assertEqual(lab.tasks.count(), 1)

    def test_change_item_regenerates_future_tasks(self):
        plan = self.live_plan()
        item = plan.items.get()
        response = self.client.post(reverse("staff_change_item", args=[item.pk]), {
            "dose_text": "1000 mg", "dose_times": "08:00",
            "start_date": item.start_date.isoformat(), "end_date": item.end_date.isoformat(), "notes": "",
        })
        self.assertRedirects(response, reverse("staff_care_plan", args=[plan.pk]))
        item.refresh_from_db()
        new_item = item.replaced_by.get()
        self.assertIsNotNone(item.superseded_at)
        self.assertEqual(new_item.dose_text, "1000 mg")
        self.assertEqual(item.tasks.exclude(status=Task.Status.CANCELLED).count(), 0)
        self.assertEqual(new_item.tasks.count(), 7)

    def test_change_with_no_differences_is_rejected(self):
        plan = self.live_plan()
        item = plan.items.get()
        response = self.client.post(reverse("staff_change_item", args=[item.pk]), {
            "dose_text": item.dose_text, "dose_times": ", ".join(item.dose_times),
            "start_date": item.start_date.isoformat(), "end_date": item.end_date.isoformat(), "notes": "",
        }, follow=True)
        self.assertContains(response, "Nothing was changed")
        item.refresh_from_db()
        self.assertIsNone(item.superseded_at)

    def test_stop_item_cancels_future_tasks(self):
        plan = self.live_plan()
        item = plan.items.get()
        self.client.post(reverse("staff_stop_item", args=[item.pk]))
        self.assertEqual(item.tasks.filter(status=Task.Status.CANCELLED).count(), 14)

    def test_end_plan_cancels_pending_tasks(self):
        plan = self.live_plan()
        self.client.post(reverse("staff_end_plan", args=[plan.pk]))
        plan.refresh_from_db()
        self.assertEqual(plan.status, CarePlan.Status.COMPLETED)
        self.assertFalse(Task.objects.filter(status=Task.Status.PENDING).exists())
        # A new plan can now be started.
        self.assertTrue(self.new_plan())

    def test_plan_page_renders_for_draft_and_live(self):
        plan = self.new_plan()
        self.add_metformin(plan)
        self.assertContains(self.client.get(reverse("staff_care_plan", args=[plan.pk])), "Make plan live")
        self.activate(plan)
        response = self.client.get(reverse("staff_care_plan", args=[plan.pk]))
        self.assertContains(response, "Pending 14")
        self.assertNotContains(response, "Make plan live")

    # --- access ---

    def test_other_clinic_staff_cannot_touch_the_plan(self):
        plan = self.new_plan()
        other = make_user(make_clinic(name="Other"), "otherstaff", User.Role.STAFF)
        self.client.force_login(other)
        self.assertEqual(self.client.get(reverse("staff_care_plan", args=[plan.pk])).status_code, 404)
        self.assertEqual(self.add_metformin(plan).status_code, 404)
        self.assertEqual(self.client.post(reverse("staff_new_care_plan", args=[self.patient.pk])).status_code, 404)

    def test_patients_cannot_use_care_plan_pages(self):
        plan = self.new_plan()
        self.client.force_login(self.patient)
        self.assertEqual(self.client.get(reverse("staff_care_plan", args=[plan.pk])).status_code, 403)
        self.assertEqual(self.client.get(reverse("staff_add_patient")).status_code, 403)
