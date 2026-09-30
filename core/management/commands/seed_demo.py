"""Seed one fictional clinic, staff user, patient and active care plan.

All names are made up. Demo users get the password from DEMO_PASSWORD.
"""

import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from core.models import Appointment, CarePlan, Clinic, ClinicSettings, PlanItem, User
from core.services import activate_plan, log_action

CLINIC_NAME = "Sunrise Demo Clinic"
CLINIC_TZ = "Asia/Kolkata"


class Command(BaseCommand):
    help = "Create fake demo data: one clinic, one staff user, one patient with a care plan."

    @transaction.atomic
    def handle(self, *args, **options):
        password = os.environ.get("DEMO_PASSWORD")
        if not password:
            raise CommandError("Set DEMO_PASSWORD in your .env before seeding.")
        if Clinic.objects.filter(name=CLINIC_NAME).exists():
            raise CommandError(f"'{CLINIC_NAME}' already exists; demo data is already seeded.")

        clinic = Clinic.objects.create(name=CLINIC_NAME, timezone=CLINIC_TZ)
        ClinicSettings.objects.create(clinic=clinic, grace_window_minutes=60, missed_dose_threshold=2)

        staff = User.objects.create_user(
            username="staff_demo", password=password,
            clinic=clinic, role=User.Role.STAFF, full_name="Priya Demo (Staff)",
        )
        patient = User.objects.create_user(
            username="patient_demo", password=password,
            clinic=clinic, role=User.Role.PATIENT, full_name="Ravi Sample",
        )

        today = timezone.now().astimezone(ZoneInfo(CLINIC_TZ)).date()
        start = today + timedelta(days=1)

        plan = CarePlan.objects.create(
            clinic=clinic, patient=patient, created_by=staff,
            reviewed_by_doctor="Dr. Asha Fictional", reviewed_on=today,
        )
        log_action(actor=staff, action="create", obj=plan)

        items = [
            PlanItem(
                clinic=clinic, care_plan=plan, type=PlanItem.Type.MEDICINE,
                name="Metformin", dose_text="500 mg", dose_times=["08:00", "20:00"],
                start_date=start, end_date=start + timedelta(days=6), notes="Twice daily for 7 days",
            ),
            PlanItem(
                clinic=clinic, care_plan=plan, type=PlanItem.Type.LAB_TEST,
                name="Fasting blood glucose", start_date=start + timedelta(days=6),
            ),
            PlanItem(
                clinic=clinic, care_plan=plan, type=PlanItem.Type.FOLLOW_UP,
                name="Follow-up visit", start_date=start + timedelta(days=9),
            ),
        ]
        for item in items:
            item.full_clean()
            item.save()
            log_action(actor=staff, action="create", obj=item)

        activate_plan(plan, actor=staff)

        follow_up = items[2]
        appointment = Appointment.objects.create(
            clinic=clinic, patient=patient, plan_item=follow_up,
            task=follow_up.tasks.get(),
            scheduled_at=datetime.combine(follow_up.start_date, time(10, 30), tzinfo=ZoneInfo(CLINIC_TZ)),
        )
        log_action(actor=staff, action="create", obj=appointment)

        self.stdout.write(self.style.SUCCESS(
            f"Seeded {CLINIC_NAME}: logins 'staff_demo' and 'patient_demo' (password = DEMO_PASSWORD), "
            f"care plan #{plan.pk} with {patient.tasks.count()} tasks."
        ))
