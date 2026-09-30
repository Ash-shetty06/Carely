from datetime import date

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse

from core.communication import convert_message_to_note, create_health_note, link_options, send_message, unread_count
from core.models import Alert, AuditLog, HealthNote, Message, User

from .helpers import make_active_plan, make_clinic, make_item, make_user


class MessagingTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.staff.full_name = "Priya Staffer"
        self.staff.save()
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.patient.full_name = "Ravi Testcase"
        self.patient.save()
        self.other_patient = make_user(self.clinic, "otherpatient", User.Role.PATIENT)
        plan = make_active_plan(self.clinic, self.patient, self.staff)
        self.medicine = make_item(plan, name="Metformin", dose_text="500 mg", start_date=date(2026, 10, 1), end_date=date(2026, 10, 7))

    # --- services ---

    def test_patient_and_staff_can_message_in_the_patients_thread(self):
        send_message(sender=self.patient, patient=self.patient, body="Can I take it with food?")
        send_message(sender=self.staff, patient=self.patient, body="The doctor will call you today.")
        self.assertEqual(Message.objects.filter(patient=self.patient).count(), 2)

    def test_nobody_else_can_post_in_the_thread(self):
        with self.assertRaises(PermissionDenied):
            send_message(sender=self.other_patient, patient=self.patient, body="hi")
        other_staff = make_user(make_clinic(name="Other"), "otherstaff", User.Role.STAFF)
        with self.assertRaises(PermissionDenied):
            send_message(sender=other_staff, patient=self.patient, body="hi")

    def test_message_can_link_to_patients_own_medicine_or_note(self):
        note = create_health_note(patient=self.patient, text="Tired")
        m1 = send_message(sender=self.patient, patient=self.patient, body="About this", link=f"item:{self.medicine.pk}")
        m2 = send_message(sender=self.staff, patient=self.patient, body="Re your note", link=f"note:{note.pk}")
        self.assertEqual(m1.plan_item, self.medicine)
        self.assertEqual(m2.health_note, note)

    def test_cannot_link_to_someone_elses_record(self):
        other_note = create_health_note(patient=self.other_patient, text="Other")
        with self.assertRaises(ValidationError):
            send_message(sender=self.patient, patient=self.patient, body="x", link=f"note:{other_note.pk}")
        self.assertNotIn(f"note:{other_note.pk}", dict(link_options(self.patient)))

    def test_blank_message_rejected_and_content_kept_out_of_audit(self):
        with self.assertRaises(ValidationError):
            send_message(sender=self.patient, patient=self.patient, body="  ")
        send_message(sender=self.patient, patient=self.patient, body="secret words")
        self.assertNotIn("secret words", str(AuditLog.objects.get(model="core.Message").changes))

    def test_messages_do_not_create_alerts(self):
        send_message(sender=self.patient, patient=self.patient, body="Hello")
        self.assertFalse(Alert.objects.exists())

    def test_unread_counts_for_each_side(self):
        send_message(sender=self.patient, patient=self.patient, body="One")
        send_message(sender=self.patient, patient=self.patient, body="Two")
        send_message(sender=self.staff, patient=self.patient, body="Reply")
        self.assertEqual(unread_count(self.staff), 2)
        self.assertEqual(unread_count(self.patient), 1)
        self.assertEqual(unread_count(self.other_patient), 0)

    # --- pages ---

    def test_opening_the_thread_marks_the_other_sides_messages_read(self):
        send_message(sender=self.patient, patient=self.patient, body="Question")
        send_message(sender=self.staff, patient=self.patient, body="Answer")
        self.client.force_login(self.staff)
        self.client.get(reverse("staff_patient_messages", args=[self.patient.pk]))
        self.assertIsNotNone(Message.objects.get(body="Question").read_at)
        self.assertIsNone(Message.objects.get(body="Answer").read_at)
        self.client.force_login(self.patient)
        self.client.get(reverse("patient_messages"))
        self.assertIsNotNone(Message.objects.get(body="Answer").read_at)

    def test_patient_sends_message_and_sees_staff_name(self):
        send_message(sender=self.staff, patient=self.patient, body="Hello from the clinic")
        self.client.force_login(self.patient)
        response = self.client.post(reverse("patient_messages"), {"body": "Thanks!", "about": f"item:{self.medicine.pk}"})
        self.assertRedirects(response, reverse("patient_messages"))
        page = self.client.get(reverse("patient_messages"))
        self.assertContains(page, "Priya Staffer · Clinic")
        self.assertContains(page, "About: Metformin 500 mg")
        self.assertContains(page, "Not for emergencies")

    def test_nav_shows_unread_badges(self):
        send_message(sender=self.patient, patient=self.patient, body="Hi")
        self.client.force_login(self.staff)
        page = self.client.get(reverse("staff_patients"))
        self.assertContains(page, 'Messages <span class="badge">1</span>', html=False)

    def test_staff_conversation_list_and_reply(self):
        send_message(sender=self.patient, patient=self.patient, body="Is my test tomorrow?")
        self.client.force_login(self.staff)
        listing = self.client.get(reverse("staff_messages"))
        self.assertContains(listing, "Ravi Testcase")
        self.assertContains(listing, "Is my test tomorrow?")
        self.client.post(reverse("staff_patient_messages", args=[self.patient.pk]), {"body": "Yes, 9am.", "about": ""})
        self.assertTrue(Message.objects.filter(sender=self.staff, body="Yes, 9am.").exists())

    def test_access_rules(self):
        send_message(sender=self.patient, patient=self.patient, body="Private question")
        # Another patient sees only their own (empty) thread.
        self.client.force_login(self.other_patient)
        self.assertNotContains(self.client.get(reverse("patient_messages")), "Private question")
        self.assertEqual(self.client.get(reverse("staff_patient_messages", args=[self.patient.pk])).status_code, 403)
        # Staff from another clinic get a 404 and don't see the conversation.
        self.client.force_login(make_user(make_clinic(name="Other"), "otherstaff", User.Role.STAFF))
        self.assertEqual(self.client.get(reverse("staff_patient_messages", args=[self.patient.pk])).status_code, 404)
        self.assertNotContains(self.client.get(reverse("staff_messages")), "Ravi Testcase")
        # Staff can't use the patient page.
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("patient_messages")).status_code, 403)


class MessageToNoteTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        plan = make_active_plan(self.clinic, self.patient, self.staff)
        make_item(plan, name="Metformin", dose_text="500 mg", start_date=date(2020, 1, 1), end_date=date(2099, 1, 1))
        self.message = send_message(sender=self.patient, patient=self.patient, body="I get a rash after the tablet.")
        self.url = reverse("staff_message_to_note", args=[self.message.pk])

    def test_staff_turn_patient_message_into_note_with_alert(self):
        self.client.force_login(self.staff)
        response = self.client.post(self.url)
        note = HealthNote.objects.get()
        alert = Alert.objects.get(health_note=note)
        self.assertRedirects(response, reverse("staff_alert_detail", args=[alert.pk]))
        self.assertEqual((note.patient, note.text, note.source_message), (self.patient, self.message.body, self.message))
        self.assertEqual(note.current_medicines, ["Metformin 500 mg"])
        self.assertEqual(AuditLog.objects.get(model="core.HealthNote").actor, self.staff)
        self.message.refresh_from_db()
        self.assertEqual(self.message.body, "I get a rash after the tablet.")  # message itself unchanged

    def test_converting_twice_is_refused(self):
        self.client.force_login(self.staff)
        self.client.post(self.url)
        self.client.post(self.url)
        self.assertEqual(HealthNote.objects.count(), 1)
        self.assertContains(
            self.client.get(reverse("staff_patient_messages", args=[self.patient.pk])), "Saved as a health note"
        )

    def test_staff_messages_cannot_be_converted(self):
        reply = send_message(sender=self.staff, patient=self.patient, body="Please stop and call us.")
        with self.assertRaises(ValidationError):
            convert_message_to_note(reply, staff=self.staff)

    def test_button_shown_to_staff_only_on_patient_messages(self):
        send_message(sender=self.staff, patient=self.patient, body="Staff reply")
        self.client.force_login(self.staff)
        page = self.client.get(reverse("staff_patient_messages", args=[self.patient.pk]))
        self.assertContains(page, "Turn into health note", count=1)
        self.client.force_login(self.patient)
        self.assertNotContains(self.client.get(reverse("patient_messages")), "Turn into health note")

    def test_patient_sees_hint_and_converted_note(self):
        self.client.force_login(self.patient)
        self.assertContains(self.client.get(reverse("patient_messages")), "Write a health note")
        convert_message_to_note(self.message, staff=self.staff)
        self.assertContains(self.client.get(reverse("health_notes")), "Added by the clinic from your message")

    def test_only_same_clinic_staff_can_convert(self):
        self.client.force_login(self.patient)
        self.assertEqual(self.client.post(self.url).status_code, 403)
        self.client.force_login(make_user(make_clinic(name="Other"), "otherstaff", User.Role.STAFF))
        self.assertEqual(self.client.post(self.url).status_code, 404)
        self.assertFalse(HealthNote.objects.exists())
