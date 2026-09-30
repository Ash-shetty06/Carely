from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import Alert, Appointment, PlanItem, Task, User
from core.services import book_appointment, record_attendance, reschedule_appointment, stop_plan_item

from .helpers import make_active_plan, make_clinic, make_item, make_task, make_user

IST = ZoneInfo("Asia/Kolkata")


def ist(d, hh, mm=0):
    return datetime(2026, 10, d, hh, mm, tzinfo=IST)


class AppointmentTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.plan = make_active_plan(self.clinic, self.patient, self.staff)
        self.item = make_item(self.plan, type=PlanItem.Type.FOLLOW_UP, name="Follow-up visit")
        self.task = make_task(self.item, ist(10, 23, 59))

    def book(self, when=None):
        return book_appointment(self.task, actor=self.staff, scheduled_at=when or ist(10, 10, 30))

    def test_booking_links_appointment_to_task(self):
        appointment = self.book()
        self.assertEqual((appointment.task, appointment.status), (self.task, Appointment.Status.BOOKED))
        with self.assertRaises(ValidationError):
            self.book()

    def test_only_follow_ups_can_be_booked(self):
        dose = make_task(make_item(self.plan), ist(1, 8))
        with self.assertRaises(ValidationError):
            book_appointment(dose, actor=self.staff, scheduled_at=ist(1, 9))

    def test_reschedule(self):
        appointment = reschedule_appointment(self.book(), actor=self.staff, scheduled_at=ist(11, 9))
        self.assertEqual(appointment.scheduled_at, ist(11, 9))

    def test_attended_marks_task_done(self):
        record_attendance(self.book(), actor=self.staff, attended=True, now=ist(10, 11))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.DONE)
        self.assertEqual(self.task.completed_at, ist(10, 11))
        self.assertFalse(Alert.objects.exists())

    def test_not_attended_marks_task_missed_and_raises_alert(self):
        record_attendance(self.book(), actor=self.staff, attended=False, now=ist(10, 18))
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.Status.MISSED)
        alert = Alert.objects.get()
        self.assertEqual((alert.source_rule, alert.task), (Alert.SourceRule.FOLLOW_UP_NOT_ATTENDED, self.task))

    def test_outcome_cannot_be_recorded_before_the_day_or_twice(self):
        appointment = self.book()
        with self.assertRaises(ValidationError):
            record_attendance(appointment, actor=self.staff, attended=True, now=ist(9, 18))
        record_attendance(appointment, actor=self.staff, attended=True, now=ist(10, 11))
        with self.assertRaises(ValidationError):
            record_attendance(appointment, actor=self.staff, attended=False, now=ist(10, 12))

    def test_follow_up_with_booked_appointment_cannot_be_stopped(self):
        self.book()
        with self.assertRaises(ValidationError):
            stop_plan_item(self.item, actor=self.staff, now=ist(5, 9))

    # --- views ---

    def test_book_and_record_outcome_through_pages(self):
        self.client.force_login(self.staff)
        plan_url = reverse("staff_care_plan", args=[self.plan.pk])
        response = self.client.post(reverse("staff_book_appointment", args=[self.task.pk]), {"scheduled_at": "2026-10-10T10:30"})
        self.assertRedirects(response, plan_url)
        appointment = Appointment.objects.get()
        self.assertEqual(appointment.scheduled_at, ist(10, 10, 30))  # entered in clinic time
        self.assertContains(self.client.get(plan_url), "Appointment booked for")

        self.client.post(reverse("staff_update_appointment", args=[appointment.pk]),
                         {"action": "reschedule", "scheduled_at": "2026-10-11T09:00"})
        appointment.refresh_from_db()
        self.assertEqual(appointment.scheduled_at, ist(11, 9))

    def test_patient_sees_upcoming_appointment(self):
        self.task.due_at = timezone.now() + timedelta(days=3)
        self.task.save()
        book_appointment(self.task, actor=self.staff, scheduled_at=timezone.now() + timedelta(days=3))
        self.client.force_login(self.patient)
        self.assertContains(self.client.get(reverse("today")), "Follow-up visit")

    def test_patient_and_other_clinic_cannot_book(self):
        self.client.force_login(self.patient)
        url = reverse("staff_book_appointment", args=[self.task.pk])
        self.assertEqual(self.client.post(url, {"scheduled_at": "2026-10-10T10:30"}).status_code, 403)
        self.client.force_login(make_user(make_clinic(name="Other"), "otherstaff", User.Role.STAFF))
        self.assertEqual(self.client.post(url, {"scheduled_at": "2026-10-10T10:30"}).status_code, 404)
        self.assertFalse(Appointment.objects.exists())
