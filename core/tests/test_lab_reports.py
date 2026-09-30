import shutil
import tempfile
from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core import services
from core.models import Alert, PlanItem, Task, User

from .helpers import make_active_plan, make_clinic, make_item, make_task, make_user

PDF = b"%PDF-1.4\n% fake test report\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20

TEMP_MEDIA = tempfile.mkdtemp(prefix="carely-test-media-")


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class LabReportTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.other_patient = make_user(self.clinic, "otherpatient", User.Role.PATIENT)
        plan = make_active_plan(self.clinic, self.patient, self.staff)
        self.lab = make_task(
            make_item(plan, type=PlanItem.Type.LAB_TEST, name="Fasting glucose"), timezone.now() + timedelta(days=1)
        )

    def upload(self, user, content=PDF, name="Ravi glucose result.pdf"):
        self.client.force_login(user)
        return self.client.post(
            reverse("upload_report", args=[self.lab.pk]), {"report": SimpleUploadedFile(name, content)}
        )

    def test_patient_upload_marks_lab_test_done(self):
        response = self.upload(self.patient)
        self.assertRedirects(response, reverse("today"))
        self.lab.refresh_from_db()
        self.assertEqual(self.lab.status, Task.Status.DONE)
        self.assertIsNotNone(self.lab.completed_at)
        # Stored under a random name, not the patient's file name.
        self.assertTrue(self.lab.lab_report.name.startswith(f"lab_reports/clinic_{self.clinic.pk}/"))
        self.assertNotIn("Ravi", self.lab.lab_report.name)

    def test_staff_can_upload(self):
        response = self.upload(self.staff, PNG, "scan.png")
        self.assertRedirects(response, reverse("staff_care_plan", args=[self.lab.plan_item.care_plan_id]))
        self.lab.refresh_from_db()
        self.assertEqual(self.lab.status, Task.Status.DONE)

    def test_rejects_wrong_type_and_disguised_files(self):
        for name, content in [("report.docx", PDF), ("report.pdf", b"MZ not really a pdf"), ("photo.png", PDF)]:
            with self.subTest(name=name):
                response = self.upload(self.patient, content, name)
                self.assertRedirects(response, reverse("today"))
                self.lab.refresh_from_db()
                self.assertEqual(self.lab.status, Task.Status.PENDING)
                self.assertFalse(self.lab.lab_report)

    def test_rejects_files_over_10_mb(self):
        original = services.MAX_REPORT_BYTES
        services.MAX_REPORT_BYTES = 10
        try:
            response = self.upload(self.patient)
        finally:
            services.MAX_REPORT_BYTES = original
        self.assertRedirects(response, reverse("today"))
        self.lab.refresh_from_db()
        self.assertEqual(self.lab.status, Task.Status.PENDING)

    def test_other_patient_cannot_upload_or_download(self):
        self.assertEqual(self.upload(self.other_patient).status_code, 404)
        self.upload(self.patient)
        self.client.force_login(self.other_patient)
        self.assertEqual(self.client.get(reverse("download_report", args=[self.lab.pk])).status_code, 404)

    def test_download_for_patient_and_staff_only_in_clinic(self):
        self.upload(self.patient)
        url = reverse("download_report", args=[self.lab.pk])
        for user in (self.patient, self.staff):
            self.client.force_login(user)
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(b"".join(response.streaming_content), PDF)
            self.assertIn("no-store", response["Cache-Control"])
        self.client.force_login(make_user(make_clinic(name="Other"), "otherstaff", User.Role.STAFF))
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)

    def test_download_without_report_is_404(self):
        self.client.force_login(self.patient)
        self.assertEqual(self.client.get(reverse("download_report", args=[self.lab.pk])).status_code, 404)

    def test_late_upload_after_missed_marks_done_and_keeps_alert(self):
        self.lab.status = Task.Status.MISSED
        self.lab.save()
        Alert.objects.create(clinic=self.clinic, patient=self.patient, task=self.lab,
                             source_rule=Alert.SourceRule.LAB_TEST_MISSED)
        self.upload(self.patient)
        self.lab.refresh_from_db()
        self.assertEqual(self.lab.status, Task.Status.DONE)
        self.assertEqual(Alert.objects.get().status, Alert.Status.NEW)

    def test_patient_sees_lab_test_with_upload_form(self):
        self.client.force_login(self.patient)
        response = self.client.get(reverse("today"))
        self.assertContains(response, "Fasting glucose")
        self.assertContains(response, reverse("upload_report", args=[self.lab.pk]))
