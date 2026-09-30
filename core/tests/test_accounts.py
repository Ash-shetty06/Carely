from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.accounts import MAX_FAILED_LOGINS
from core.clinic_config import grace_window, missed_dose_threshold
from core.models import AuditLog, ClinicSettings, User

from .helpers import make_clinic, make_user

PASSWORD = "test-pass-123"  # set by helpers.make_user


class LoginLockoutTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)

    def login(self, password):
        return self.client.post(reverse("login"), {"username": "patient", "password": password})

    def test_account_locks_after_repeated_failures(self):
        for _ in range(MAX_FAILED_LOGINS - 1):
            self.assertContains(self.login("wrong"), "Username or password is incorrect.")
        self.assertContains(self.login("wrong"), "locked for 15 minutes")
        self.patient.refresh_from_db()
        self.assertIsNotNone(self.patient.locked_until)
        # Even the right password is refused while locked.
        self.assertContains(self.login(PASSWORD), "locked for 15 minutes")
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertTrue(AuditLog.objects.filter(model="core.User", record_id=self.patient.pk).exists())

    def test_lock_expires(self):
        self.patient.locked_until = timezone.now() - timedelta(seconds=1)
        self.patient.save()
        self.assertRedirects(self.login(PASSWORD), reverse("home"), fetch_redirect_response=False)

    def test_successful_login_resets_the_count(self):
        for _ in range(MAX_FAILED_LOGINS - 1):
            self.login("wrong")
        self.login(PASSWORD)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.failed_login_count, 0)

    def test_unknown_username_gets_the_generic_message(self):
        response = self.client.post(reverse("login"), {"username": "nobody", "password": "x"})
        self.assertContains(response, "Username or password is incorrect.")


class PasswordTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.patient.full_name = "Ravi Testcase"
        self.patient.save()

    def test_new_patient_must_change_temporary_password(self):
        self.client.force_login(self.staff)
        self.client.post(reverse("staff_add_patient"), {
            "full_name": "Meena Example", "username": "meena",
            "password": "a-long-temp-pass-9", "password_again": "a-long-temp-pass-9",
        })
        meena = User.objects.get(username="meena")
        self.assertTrue(meena.must_change_password)
        self.client.force_login(meena)
        self.assertRedirects(self.client.get(reverse("today")), reverse("password_change"))
        response = self.client.post(reverse("password_change"), {
            "old_password": "a-long-temp-pass-9",
            "new_password1": "meenas-own-pass-42",
            "new_password2": "meenas-own-pass-42",
        })
        self.assertRedirects(response, reverse("home"), fetch_redirect_response=False)
        meena.refresh_from_db()
        self.assertFalse(meena.must_change_password)
        self.assertTrue(meena.check_password("meenas-own-pass-42"))
        self.assertEqual(self.client.get(reverse("today")).status_code, 200)  # still logged in

    def test_forced_change_still_allows_logout(self):
        self.patient.must_change_password = True
        self.patient.save()
        self.client.force_login(self.patient)
        self.assertRedirects(self.client.post(reverse("logout")), reverse("login"))

    def test_staff_reset_sets_temporary_password_and_unlocks(self):
        self.patient.locked_until = timezone.now() + timedelta(minutes=10)
        self.patient.save()
        self.client.force_login(self.staff)
        page = self.client.get(reverse("staff_patient_detail", args=[self.patient.pk]))
        self.assertContains(page, "This account is locked")
        response = self.client.post(reverse("staff_reset_password", args=[self.patient.pk]), {
            "password": "fresh-temp-pass-31", "password_again": "fresh-temp-pass-31",
        })
        self.assertRedirects(response, reverse("staff_patient_detail", args=[self.patient.pk]))
        self.patient.refresh_from_db()
        self.assertTrue(self.patient.check_password("fresh-temp-pass-31"))
        self.assertTrue(self.patient.must_change_password)
        self.assertIsNone(self.patient.locked_until)
        log = AuditLog.objects.get(model="core.User", record_id=self.patient.pk, actor=self.staff)
        self.assertNotIn("fresh-temp-pass-31", str(log.changes))

    def test_reset_rejects_weak_or_mismatched_passwords(self):
        self.client.force_login(self.staff)
        url = reverse("staff_reset_password", args=[self.patient.pk])
        for data in ({"password": "123", "password_again": "123"},
                     {"password": "fresh-temp-pass-31", "password_again": "other-pass-31"}):
            self.assertContains(self.client.post(url, data), "field-error")
        self.patient.refresh_from_db()
        self.assertTrue(self.patient.check_password(PASSWORD))

    def test_only_same_clinic_staff_can_reset(self):
        url = reverse("staff_reset_password", args=[self.patient.pk])
        data = {"password": "fresh-temp-pass-31", "password_again": "fresh-temp-pass-31"}
        self.client.force_login(self.patient)
        self.assertEqual(self.client.post(url, data).status_code, 403)
        self.client.force_login(make_user(make_clinic(name="Other"), "otherstaff", User.Role.STAFF))
        self.assertEqual(self.client.post(url, data).status_code, 404)
        self.client.force_login(self.staff)
        self.assertEqual(self.client.post(reverse("staff_reset_password", args=[self.staff.pk]), data).status_code, 404)

    def test_everyone_can_change_their_own_password(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("password_change")), "Change password")
        self.client.post(reverse("password_change"), {
            "old_password": PASSWORD, "new_password1": "staff-new-pass-77", "new_password2": "staff-new-pass-77",
        })
        self.staff.refresh_from_db()
        self.assertTrue(self.staff.check_password("staff-new-pass-77"))


class ClinicSettingsTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic(grace_minutes=60)
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.client.force_login(self.staff)

    def test_staff_update_settings_and_change_is_audited(self):
        self.assertContains(self.client.get(reverse("staff_settings")), 'value="60"')
        response = self.client.post(reverse("staff_settings"), {"grace_window_minutes": 30, "missed_dose_threshold": 3})
        self.assertRedirects(response, reverse("staff_settings"))
        self.assertEqual(grace_window(self.clinic.pk), timedelta(minutes=30))
        self.assertEqual(missed_dose_threshold(self.clinic.pk), 3)
        log = AuditLog.objects.get(model="core.ClinicSettings")
        self.assertEqual(log.changes["grace_window_minutes"], [60, 30])

    def test_out_of_range_values_rejected(self):
        for data in ({"grace_window_minutes": 5, "missed_dose_threshold": 2},
                     {"grace_window_minutes": 60, "missed_dose_threshold": 0},
                     {"grace_window_minutes": 60, "missed_dose_threshold": 11}):
            self.assertContains(self.client.post(reverse("staff_settings"), data), "field-error")
        self.assertEqual(ClinicSettings.objects.get(clinic=self.clinic).grace_window_minutes, 60)

    def test_settings_are_per_clinic_and_staff_only(self):
        other_clinic = make_clinic(name="Other", grace_minutes=90)
        self.client.post(reverse("staff_settings"), {"grace_window_minutes": 30, "missed_dose_threshold": 2})
        self.assertEqual(grace_window(other_clinic.pk), timedelta(minutes=90))
        self.client.force_login(make_user(self.clinic, "patient", User.Role.PATIENT))
        self.assertEqual(self.client.get(reverse("staff_settings")).status_code, 403)
