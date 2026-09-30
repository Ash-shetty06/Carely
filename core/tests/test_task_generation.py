from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.test import TestCase

from core.models import AuditLog, CarePlan, Clinic, PlanItem, Task, User
from core.services import activate_plan, generate_tasks, replace_plan_item

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")


def ist(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=IST)


class TaskGenerationTestBase(TestCase):
    def setUp(self):
        self.clinic = Clinic.objects.create(name="Test Clinic", timezone="Asia/Kolkata")
        self.staff = User.objects.create_user(
            username="staff", password="x", clinic=self.clinic, role=User.Role.STAFF, full_name="Staff One"
        )
        self.patient = User.objects.create_user(
            username="patient", password="x", clinic=self.clinic, role=User.Role.PATIENT, full_name="Pat Fake"
        )
        self.plan = CarePlan.objects.create(
            clinic=self.clinic, patient=self.patient, created_by=self.staff,
            reviewed_by_doctor="Dr. Test", reviewed_on=date(2026, 9, 30),
        )
        # Plan goes live just before the first dose.
        self.activation_time = ist(2026, 10, 1, 0, 0)

    def add_item(self, **fields):
        item = PlanItem(clinic=self.clinic, care_plan=self.plan, **fields)
        item.full_clean()
        item.save()
        return item

    def add_metformin(self):
        return self.add_item(
            type=PlanItem.Type.MEDICINE, name="Metformin", dose_text="500 mg",
            dose_times=["08:00", "20:00"], start_date=date(2026, 10, 1), end_date=date(2026, 10, 7),
        )

    def activate(self):
        activate_plan(self.plan, actor=self.staff, now=self.activation_time)


class MedicineTaskTests(TaskGenerationTestBase):
    def test_twice_daily_for_7_days_creates_14_pending_tasks(self):
        item = self.add_metformin()
        self.activate()

        tasks = list(item.tasks.all())
        self.assertEqual(len(tasks), 14)
        self.assertTrue(all(t.status == Task.Status.PENDING for t in tasks))
        self.assertTrue(all(t.patient_id == self.patient.pk and t.clinic_id == self.clinic.pk for t in tasks))

    def test_due_times_follow_clinic_timezone(self):
        item = self.add_metformin()
        self.activate()

        due = [t.due_at for t in item.tasks.all()]
        self.assertEqual(due[0], ist(2026, 10, 1, 8, 0))
        self.assertEqual(due[1], ist(2026, 10, 1, 20, 0))
        self.assertEqual(due[-1], ist(2026, 10, 7, 20, 0))
        # 08:00 IST is 02:30 UTC.
        self.assertEqual(due[0].astimezone(UTC).time(), time(2, 30))

    def test_dose_times_are_ordered_regardless_of_input_order(self):
        item = self.add_item(
            type=PlanItem.Type.MEDICINE, name="Drug", dose_text="1 tab",
            dose_times=["20:00", "08:00"], start_date=date(2026, 10, 1), end_date=date(2026, 10, 1),
        )
        self.activate()
        self.assertEqual([t.due_at.astimezone(IST).hour for t in item.tasks.all()], [8, 20])

    def test_generation_is_idempotent(self):
        item = self.add_metformin()
        self.activate()
        generate_tasks(item, now=self.activation_time)
        self.assertEqual(item.tasks.count(), 14)

    def test_doses_already_past_at_activation_are_not_created(self):
        item = self.add_metformin()
        self.activation_time = ist(2026, 10, 1, 12, 0)  # after the first 08:00 dose
        self.activate()
        self.assertEqual(item.tasks.count(), 13)
        self.assertEqual(item.tasks.first().due_at, ist(2026, 10, 1, 20, 0))

    def test_every_generated_task_is_audited(self):
        item = self.add_metformin()
        self.activate()
        logged = AuditLog.objects.filter(model="core.Task", action=AuditLog.Action.CREATE)
        self.assertEqual(set(logged.values_list("record_id", flat=True)), set(item.tasks.values_list("pk", flat=True)))
        self.assertTrue(all(log.actor_id == self.staff.pk for log in logged))


class SingleTaskItemTests(TaskGenerationTestBase):
    def test_lab_test_creates_one_task_due_end_of_day(self):
        item = self.add_item(type=PlanItem.Type.LAB_TEST, name="Fasting glucose", start_date=date(2026, 10, 7))
        self.activate()
        self.assertEqual(item.tasks.count(), 1)
        self.assertEqual(item.tasks.get().due_at, ist(2026, 10, 7, 23, 59))

    def test_follow_up_creates_one_task(self):
        item = self.add_item(type=PlanItem.Type.FOLLOW_UP, name="Follow-up visit", start_date=date(2026, 10, 10))
        self.activate()
        self.assertEqual(item.tasks.count(), 1)
        self.assertEqual(item.tasks.get().due_at, ist(2026, 10, 10, 23, 59))


class ActivationRuleTests(TaskGenerationTestBase):
    def test_draft_plan_does_not_generate_tasks(self):
        item = self.add_metformin()
        with self.assertRaises(ValueError):
            generate_tasks(item, now=self.activation_time)
        self.assertEqual(Task.objects.count(), 0)

    def test_activation_requires_doctor_review(self):
        self.plan.reviewed_by_doctor = ""
        self.plan.save()
        self.add_metformin()
        with self.assertRaises(ValidationError):
            self.activate()
        self.assertEqual(Task.objects.count(), 0)

    def test_database_rejects_active_plan_without_review(self):
        self.plan.status = CarePlan.Status.ACTIVE
        self.plan.reviewed_on = None
        with self.assertRaises(IntegrityError):
            self.plan.save()

    def test_patient_can_have_only_one_active_plan(self):
        self.add_metformin()
        self.activate()
        second = CarePlan(
            clinic=self.clinic, patient=self.patient, created_by=self.staff,
            status=CarePlan.Status.ACTIVE, reviewed_by_doctor="Dr. Test", reviewed_on=date(2026, 9, 30),
        )
        with self.assertRaises(IntegrityError):
            second.save()

    def test_medicine_requires_valid_dose_times(self):
        for bad in ([], ["8am"], ["25:00"], ["08:00", "08:00"]):
            with self.subTest(dose_times=bad), self.assertRaises(ValidationError):
                self.add_item(
                    type=PlanItem.Type.MEDICINE, name="Drug", dose_text="1 tab", dose_times=bad,
                    start_date=date(2026, 10, 1), end_date=date(2026, 10, 2),
                )

    def test_medicine_end_date_cannot_precede_start(self):
        with self.assertRaises(ValidationError):
            self.add_item(
                type=PlanItem.Type.MEDICINE, name="Drug", dose_text="1 tab", dose_times=["08:00"],
                start_date=date(2026, 10, 5), end_date=date(2026, 10, 1),
            )


class PlanChangeTests(TaskGenerationTestBase):
    def setUp(self):
        super().setUp()
        self.item = self.add_metformin()
        self.activate()
        # Day 1: morning dose taken, evening dose skipped. Day 2 morning missed.
        tasks = list(self.item.tasks.all())
        self._set(tasks[0], Task.Status.DONE, ist(2026, 10, 1, 8, 5))
        self._set(tasks[1], Task.Status.SKIPPED, ist(2026, 10, 1, 20, 10))
        self._set(tasks[2], Task.Status.MISSED, None)
        # Doctor changes the dose at noon on day 2 to once daily, 1000 mg.
        self.change_time = ist(2026, 10, 2, 12, 0)
        self.new_item = replace_plan_item(
            self.item, actor=self.staff, now=self.change_time, dose_text="1000 mg", dose_times=["08:00"]
        )

    @staticmethod
    def _set(task, status, completed_at):
        task.status = status
        task.completed_at = completed_at
        task.save()

    def test_past_tasks_are_never_rewritten(self):
        past = list(self.item.tasks.filter(due_at__lt=self.change_time))
        self.assertEqual([t.status for t in past], ["done", "skipped", "missed"])
        self.assertEqual(past[0].completed_at, ist(2026, 10, 1, 8, 5))

    def test_future_pending_tasks_of_old_item_are_cancelled(self):
        future = self.item.tasks.filter(due_at__gte=self.change_time)
        self.assertEqual(future.count(), 11)  # day 2 evening through day 7 evening
        self.assertTrue(all(t.status == Task.Status.CANCELLED for t in future))

    def test_new_version_generates_tasks_from_change_time(self):
        tasks = list(self.new_item.tasks.all())
        self.assertEqual(len(tasks), 5)  # 08:00 on days 3-7
        self.assertEqual(tasks[0].due_at, ist(2026, 10, 3, 8, 0))
        self.assertTrue(all(t.status == Task.Status.PENDING for t in tasks))

    def test_new_version_links_to_old_and_old_is_superseded(self):
        self.item.refresh_from_db()
        self.assertEqual(self.item.superseded_at, self.change_time)
        self.assertEqual(self.new_item.replaces_id, self.item.pk)
        self.assertEqual(self.new_item.dose_text, "1000 mg")
        self.assertEqual(self.new_item.name, "Metformin")

    def test_superseded_item_generates_no_more_tasks(self):
        self.item.refresh_from_db()
        self.assertEqual(generate_tasks(self.item, now=self.change_time), [])

    def test_superseded_item_cannot_be_replaced_again(self):
        self.item.refresh_from_db()
        with self.assertRaises(ValidationError):
            replace_plan_item(self.item, actor=self.staff, now=self.change_time, dose_text="250 mg")

    def test_cancellations_are_audited(self):
        cancelled_ids = set(self.item.tasks.filter(status=Task.Status.CANCELLED).values_list("pk", flat=True))
        logged_ids = set(
            AuditLog.objects.filter(model="core.Task", action=AuditLog.Action.STATUS_CHANGE)
            .values_list("record_id", flat=True)
        )
        self.assertEqual(cancelled_ids, logged_ids)
