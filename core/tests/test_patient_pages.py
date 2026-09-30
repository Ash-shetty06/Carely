from datetime import date, timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.communication import create_health_note
from core.models import Alert, Appointment, CarePlan, PlanItem, Task, User
from core.services import replace_plan_item

from .helpers import make_active_plan, make_clinic, make_item, make_task, make_user


class PatientHomepageTests(TestCase):
    """The homepage shows today plus the next 7 days only."""

    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.plan = make_active_plan(self.clinic, self.patient, self.staff)
        self.now = timezone.now()
        self.client.force_login(self.patient)

    def get(self):
        return self.client.get(reverse("today"))

    def test_todays_dose_has_buttons_and_upcoming_dose_is_listed(self):
        make_task(make_item(self.plan, name="Metformin"), self.now)
        make_task(make_item(self.plan, name="Amlodipine"), self.now + timedelta(days=3))
        page = self.get()
        self.assertContains(page, 'value="taken"', count=1)
        self.assertContains(page, "Coming up")
        self.assertContains(page, "Amlodipine")

    def test_items_beyond_seven_days_are_not_on_the_homepage(self):
        make_task(make_item(self.plan, name="Atorvastatin"), self.now + timedelta(days=10))
        page = self.get()
        self.assertNotContains(page, "Atorvastatin")
        self.assertContains(page, "Nothing scheduled in the next 7 days")

    def test_lab_reports_to_upload(self):
        soon = make_task(make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="Glucose"), self.now + timedelta(days=2))
        overdue = make_task(make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="Lipids"),
                            self.now - timedelta(days=2), status=Task.Status.MISSED)
        make_task(make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="Thyroid"), self.now + timedelta(days=12))
        make_task(make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="HbA1c"), self.now - timedelta(days=1),
                  status=Task.Status.DONE, completed_at=self.now, lab_report="lab_reports/x.pdf")
        page = self.get()
        self.assertContains(page, reverse("upload_report", args=[soon.pk]))
        self.assertContains(page, reverse("upload_report", args=[overdue.pk]))
        self.assertContains(page, "Upload it if you have it")
        self.assertNotContains(page, "Thyroid")
        self.assertNotContains(page, "HbA1c")  # done: no longer on the homepage

    def test_appointments_today_and_coming_up(self):
        visit = make_item(self.plan, type=PlanItem.Type.FOLLOW_UP, name="Review visit")
        Appointment.objects.create(clinic=self.clinic, patient=self.patient, plan_item=visit,
                                   scheduled_at=self.now + timedelta(days=4))
        page = self.get()
        self.assertContains(page, "Review visit: appointment")

    def test_homepage_links_to_full_plan(self):
        self.assertContains(self.get(), reverse("my_plan"))


class MyPlanTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.old_plan = make_active_plan(self.clinic, self.patient, self.staff)
        make_item(self.old_plan, name="Amoxicillin")
        CarePlan.objects.filter(pk=self.old_plan.pk).update(status=CarePlan.Status.COMPLETED, activated_at=timezone.now() - timedelta(days=60))
        self.plan = make_active_plan(self.clinic, self.patient, self.staff)
        CarePlan.objects.filter(pk=self.plan.pk).update(activated_at=timezone.now() - timedelta(days=1))
        self.med = make_item(self.plan, name="Metformin", dose_text="500 mg", dose_times=["08:00", "20:00"],
                             start_date=date(2026, 10, 1), end_date=date(2026, 10, 7))
        make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="Glucose", start_date=date(2026, 10, 7))
        make_item(self.plan, type=PlanItem.Type.FOLLOW_UP, name="Review visit", start_date=date(2026, 10, 10))
        self.task = make_task(self.med, timezone.now() - timedelta(hours=2), status=Task.Status.MISSED)
        self.client.force_login(self.patient)

    def test_shows_full_current_plan(self):
        page = self.client.get(reverse("my_plan"))
        for text in ("Current plan", "Reviewed by Dr. Test", "Metformin", "500 mg", "08:00, 20:00",
                     "Glucose", "Review visit", "the clinic will book a time", "My record", '<table class="timeline">'):
            self.assertContains(page, text)
        self.assertNotContains(page, "Amoxicillin")

    def test_record_has_no_staff_information(self):
        Alert.objects.create(clinic=self.clinic, patient=self.patient, task=self.task,
                             source_rule=Alert.SourceRule.MISSED_DOSE_THRESHOLD)
        create_health_note(patient=self.patient, text="Private triage context")
        page = self.client.get(reverse("my_plan"))
        self.assertNotContains(page, "Alerts")
        self.assertNotContains(page, "not-taken alert")
        self.assertNotContains(page, "Private triage context")
        self.assertNotContains(page, reverse("staff_alerts"))

    def test_changed_items_are_listed(self):
        replace_plan_item(self.med, actor=self.staff, dose_text="1000 mg")
        page = self.client.get(reverse("my_plan"))
        self.assertContains(page, "1000 mg")
        self.assertContains(page, "Earlier versions and stopped items")

    def test_earlier_plans_can_be_opened(self):
        page = self.client.get(reverse("my_plan"))
        self.assertContains(page, "All my plans")
        page = self.client.get(reverse("my_plan"), {"plan": self.old_plan.pk})
        self.assertContains(page, "Finished plan")
        self.assertContains(page, "Amoxicillin")

    def test_cannot_open_someone_elses_plan(self):
        other = make_user(self.clinic, "otherpatient", User.Role.PATIENT)
        other_plan = make_active_plan(self.clinic, other, self.staff)
        make_item(other_plan, name="Warfarin")
        page = self.client.get(reverse("my_plan"), {"plan": other_plan.pk})
        self.assertNotContains(page, "Warfarin")
        self.assertContains(page, "Metformin")  # falls back to the patient's own plan

    def test_drafts_are_hidden_and_no_plan_message(self):
        newbie = make_user(self.clinic, "newbie", User.Role.PATIENT)
        draft = CarePlan.objects.create(clinic=self.clinic, patient=newbie, created_by=self.staff)
        make_item(draft, name="Secret draft item")
        self.client.force_login(newbie)
        page = self.client.get(reverse("my_plan"))
        self.assertContains(page, "You don't have a care plan yet")
        self.assertNotContains(page, "Secret draft item")

    def test_staff_cannot_use_patient_plan_page(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("my_plan")).status_code, 403)
