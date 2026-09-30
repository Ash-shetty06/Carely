"""Small builders for fake test data."""

from datetime import date

from core.models import CarePlan, Clinic, ClinicSettings, PlanItem, Task, User


def make_clinic(name="Test Clinic", tz="Asia/Kolkata", grace_minutes=60):
    clinic = Clinic.objects.create(name=name, timezone=tz)
    ClinicSettings.objects.create(clinic=clinic, grace_window_minutes=grace_minutes)
    return clinic


def make_user(clinic, username, role):
    return User.objects.create_user(
        username=username, password="test-pass-123", clinic=clinic, role=role, full_name=username.title()
    )


def make_active_plan(clinic, patient, staff):
    return CarePlan.objects.create(
        clinic=clinic, patient=patient, created_by=staff, status=CarePlan.Status.ACTIVE,
        reviewed_by_doctor="Dr. Test", reviewed_on=date(2026, 9, 30),
    )


def make_item(plan, **fields):
    defaults = {"type": PlanItem.Type.MEDICINE, "name": "Metformin", "dose_text": "500 mg",
                "dose_times": ["08:00"], "start_date": date(2026, 10, 1), "end_date": date(2026, 10, 7)}
    if fields.get("type", PlanItem.Type.MEDICINE) != PlanItem.Type.MEDICINE:
        defaults.update(dose_text="", dose_times=[], end_date=None)
    defaults.update(fields)
    return PlanItem.objects.create(clinic=plan.clinic, care_plan=plan, **defaults)


def make_task(item, due_at, **fields):
    return Task.objects.create(
        clinic=item.clinic, patient=item.care_plan.patient, plan_item=item, due_at=due_at, **fields
    )
