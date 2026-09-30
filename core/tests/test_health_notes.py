from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.alerts import record_triage
from core.communication import create_health_note, parse_tags
from core.models import Alert, AuditLog, HealthNote, PlanItem, User
from core.services import replace_plan_item

from .helpers import make_active_plan, make_clinic, make_item, make_user

IST = ZoneInfo("Asia/Kolkata")


def ist(d, hh, mm=0):
    return datetime(2026, 10, d, hh, mm, tzinfo=IST)


class HealthNoteTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.patient.full_name = "Ravi Testcase"
        self.patient.save()
        self.plan = make_active_plan(self.clinic, self.patient, self.staff)
        # Metformin 1-7 Oct; Amlodipine only from 5 Oct; a lab test (not a medicine).
        self.metformin = make_item(self.plan, name="Metformin", dose_text="500 mg", start_date=date(2026, 10, 1), end_date=date(2026, 10, 7))
        make_item(self.plan, name="Amlodipine", dose_text="5 mg", start_date=date(2026, 10, 5), end_date=date(2026, 10, 9))
        make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="Glucose", start_date=date(2026, 10, 7))

    def note(self, text="Felt dizzy after the morning dose.", tags="dizzy, Morning", now=ist(3, 10)):
        return create_health_note(patient=self.patient, text=text, tags=tags, now=now)

    def test_note_records_text_tags_day_and_current_medicines(self):
        note = self.note()
        self.assertEqual(note.text, "Felt dizzy after the morning dose.")
        self.assertEqual(note.tags, ["dizzy", "Morning"])
        self.assertEqual(note.treatment_day, 3)
        self.assertEqual(note.current_medicines, ["Metformin 500 mg"])
        self.assertEqual(AuditLog.objects.get(model="core.HealthNote").actor, self.patient)

    def test_current_medicines_follow_dates_and_changes(self):
        self.assertEqual(self.note(now=ist(6, 10)).current_medicines, ["Amlodipine 5 mg", "Metformin 500 mg"])
        replace_plan_item(self.metformin, actor=self.staff, now=ist(6, 11), dose_text="1000 mg")
        self.assertEqual(self.note(now=ist(6, 12)).current_medicines, ["Amlodipine 5 mg", "Metformin 1000 mg"])

    def test_no_active_plan_means_no_treatment_context(self):
        self.plan.status = "completed"
        self.plan.save()
        note = self.note()
        self.assertIsNone(note.treatment_day)
        self.assertEqual(note.current_medicines, [])

    def test_note_before_treatment_starts_has_no_day(self):
        self.assertIsNone(self.note(now=datetime(2026, 9, 29, 10, tzinfo=IST)).treatment_day)

    def test_each_note_raises_one_alert(self):
        note = self.note()
        alert = Alert.objects.get()
        self.assertEqual((alert.source_rule, alert.health_note, alert.patient), (Alert.SourceRule.HEALTH_NOTE_SUBMITTED, note, self.patient))

    def test_triaging_the_alert_saves_outcome_on_the_note(self):
        note = self.note()
        record_triage(Alert.objects.get(), actor=self.staff, outcome="Called patient; advised by doctor to continue.")
        note.refresh_from_db()
        self.assertEqual(note.staff_outcome, "Called patient; advised by doctor to continue.")
        self.assertEqual(note.outcome_by, self.staff)
        self.assertIsNotNone(note.outcome_at)

    def test_tag_parsing(self):
        self.assertEqual(parse_tags(" tired ,, Tired, short  of breath "), ["tired", "short of breath"])
        self.assertEqual(parse_tags(""), [])
        with self.assertRaises(ValidationError):
            parse_tags("x" * 31)
        with self.assertRaises(ValidationError):
            parse_tags(",".join(f"t{i}" for i in range(11)))

    def test_blank_note_rejected(self):
        with self.assertRaises(ValidationError):
            self.note(text="   ")
        self.assertFalse(HealthNote.objects.exists())

    # --- pages ---

    def test_patient_writes_and_sees_notes(self):
        self.client.force_login(self.patient)
        response = self.client.post(reverse("health_notes"), {"text": "Headache since yesterday", "tags": "headache"})
        self.assertRedirects(response, reverse("health_notes"))
        page = self.client.get(reverse("health_notes"))
        self.assertContains(page, "Headache since yesterday")
        self.assertContains(page, "Not for emergencies")
        self.assertNotContains(page, "severity")

    def test_patient_sees_only_own_notes(self):
        other = make_user(self.clinic, "otherpatient", User.Role.PATIENT)
        create_health_note(patient=other, text="Other patient's private note")
        self.client.force_login(self.patient)
        self.assertNotContains(self.client.get(reverse("health_notes")), "private note")

    def test_staff_see_note_in_inbox_alert_and_patient_page(self):
        self.note()
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("staff_alerts")), "New health note")
        alert = Alert.objects.get()
        detail = self.client.get(reverse("staff_alert_detail", args=[alert.pk]))
        self.assertContains(detail, "Felt dizzy after the morning dose.")
        self.assertContains(detail, "Metformin 500 mg")
        self.assertContains(self.client.get(reverse("staff_patient_detail", args=[self.patient.pk])), "Felt dizzy")

    def test_staff_cannot_write_notes_and_other_clinic_cannot_see_them(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("health_notes")).status_code, 403)
        self.note()
        self.client.force_login(make_user(make_clinic(name="Other"), "otherstaff", User.Role.STAFF))
        self.assertNotContains(self.client.get(reverse("staff_alerts")), "Ravi Testcase")
        self.assertEqual(self.client.get(reverse("staff_patient_detail", args=[self.patient.pk])).status_code, 404)
