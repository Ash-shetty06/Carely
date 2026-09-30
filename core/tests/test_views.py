from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Task, User

from .helpers import make_active_plan, make_clinic, make_item, make_task, make_user


class ViewAccessTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.other_patient = make_user(self.clinic, "otherpatient", User.Role.PATIENT)

        other_clinic = make_clinic(name="Other Clinic")
        self.other_clinic_staff = make_user(other_clinic, "otherstaff", User.Role.STAFF)
        self.other_clinic_patient = make_user(other_clinic, "otherclinicpatient", User.Role.PATIENT)

        now = timezone.now()
        self.task = make_task(make_item(make_active_plan(self.clinic, self.patient, self.staff), name="Metformin"), now)
        self.other_task = make_task(
            make_item(make_active_plan(self.clinic, self.other_patient, self.staff), name="Amoxicillin"), now
        )

    def login(self, user):
        self.client.force_login(user)

    def respond_url(self, task):
        return reverse("respond_to_task", args=[task.pk])

    # --- login and routing ---

    def test_login_page_renders(self):
        self.assertContains(self.client.get(reverse("login")), "Log in")

    def test_anonymous_users_are_sent_to_login(self):
        for name in ("home", "today", "staff_patients"):
            with self.subTest(name):
                response = self.client.get(reverse(name))
                self.assertRedirects(response, f"{reverse('login')}?next={reverse(name)}")

    def test_home_redirects_by_role(self):
        self.login(self.patient)
        self.assertRedirects(self.client.get(reverse("home")), reverse("today"))
        self.login(self.staff)
        self.assertRedirects(self.client.get(reverse("home")), reverse("staff_patients"))

    def test_logout_requires_post_and_logs_out(self):
        self.login(self.patient)
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))
        self.assertRedirects(self.client.get(reverse("today")), f"{reverse('login')}?next={reverse('today')}")

    # --- patient pages ---

    def test_patient_sees_only_own_tasks_today(self):
        self.login(self.patient)
        response = self.client.get(reverse("today"))
        self.assertContains(response, "Metformin")
        self.assertNotContains(response, "Amoxicillin")
        self.assertContains(response, 'value="taken"')

    def test_patient_can_mark_dose_taken(self):
        self.login(self.patient)
        response = self.client.post(self.respond_url(self.task), {"action": "taken"})
        self.assertRedirects(response, reverse("today"))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.DONE)

    def test_patient_cannot_respond_to_another_patients_task(self):
        self.login(self.patient)
        response = self.client.post(self.respond_url(self.other_task), {"action": "taken"})
        self.assertEqual(response.status_code, 404)
        self.other_task.refresh_from_db()
        self.assertEqual(self.other_task.status, Task.Status.PENDING)

    def test_respond_rejects_get_and_unknown_actions(self):
        self.login(self.patient)
        self.assertEqual(self.client.get(self.respond_url(self.task)).status_code, 405)
        self.assertEqual(self.client.post(self.respond_url(self.task), {"action": "delete"}).status_code, 400)

    def test_locked_dose_shows_error_and_is_unchanged(self):
        self.task.due_at = timezone.now() - timedelta(hours=3)
        self.task.save()
        self.login(self.patient)
        response = self.client.post(self.respond_url(self.task), {"action": "taken"}, follow=True)
        self.assertContains(response, "can no longer be updated")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.PENDING)

    def test_patient_cannot_open_staff_pages(self):
        self.login(self.patient)
        self.assertEqual(self.client.get(reverse("staff_patients")).status_code, 403)
        self.assertEqual(
            self.client.get(reverse("staff_patient_detail", args=[self.patient.pk])).status_code, 403
        )

    # --- staff pages ---

    def test_staff_cannot_use_patient_pages(self):
        self.login(self.staff)
        self.assertEqual(self.client.get(reverse("today")).status_code, 403)
        self.assertEqual(self.client.post(self.respond_url(self.task), {"action": "taken"}).status_code, 403)

    def test_staff_sees_only_own_clinic_patients(self):
        self.login(self.staff)
        response = self.client.get(reverse("staff_patients"))
        self.assertContains(response, self.patient.full_name)
        self.assertContains(response, self.other_patient.full_name)
        self.assertNotContains(response, self.other_clinic_patient.full_name)

    def test_staff_sees_patient_tasks(self):
        self.login(self.staff)
        response = self.client.get(reverse("staff_patient_detail", args=[self.patient.pk]))
        self.assertContains(response, "Metformin")
        self.assertContains(response, "Reviewed by Dr. Test")
        self.assertNotContains(response, "Amoxicillin")

    def test_staff_cannot_open_patient_from_another_clinic(self):
        self.login(self.other_clinic_staff)
        response = self.client.get(reverse("staff_patient_detail", args=[self.patient.pk]))
        self.assertEqual(response.status_code, 404)

    def test_staff_detail_404_for_staff_user_id(self):
        self.login(self.staff)
        response = self.client.get(reverse("staff_patient_detail", args=[self.staff.pk]))
        self.assertEqual(response.status_code, 404)
