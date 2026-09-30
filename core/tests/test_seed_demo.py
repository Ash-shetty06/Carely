from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase

from core.models import Appointment, CarePlan, PlanItem, Task, User


@mock.patch.dict("os.environ", {"DEMO_PASSWORD": "demo-test-password"})
class SeedDemoTests(TestCase):
    def test_seeds_one_patient_with_active_plan_and_tasks(self):
        call_command("seed_demo", stdout=mock.MagicMock())

        patient = User.objects.get(username="patient_demo")
        plan = CarePlan.objects.get(patient=patient)
        self.assertEqual(plan.status, CarePlan.Status.ACTIVE)
        self.assertEqual(
            sorted(plan.items.values_list("type", flat=True)),
            sorted([PlanItem.Type.MEDICINE, PlanItem.Type.LAB_TEST, PlanItem.Type.FOLLOW_UP]),
        )
        self.assertEqual(Task.objects.filter(patient=patient).count(), 14 + 1 + 1)
        appointment = Appointment.objects.get(patient=patient)
        self.assertEqual(appointment.task.plan_item.type, PlanItem.Type.FOLLOW_UP)
        self.assertTrue(patient.check_password("demo-test-password"))

    def test_refuses_to_seed_twice(self):
        call_command("seed_demo", stdout=mock.MagicMock())
        with self.assertRaises(CommandError):
            call_command("seed_demo", stdout=mock.MagicMock())
