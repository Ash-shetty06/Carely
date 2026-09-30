"""Server-side access rules.

Patients see only their own records; staff see only their own clinic's records.
Views must fetch objects through these scoped querysets so that anything
outside the user's scope is a 404, not just hidden in the page.
"""

from functools import wraps

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied

from .models import Alert, Appointment, CarePlan, PlanItem, Task, User


def role_required(role):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if request.user.role != role:
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapper

    return decorator


patient_required = role_required(User.Role.PATIENT)
staff_required = role_required(User.Role.STAFF)


def clinic_patients(user):
    """Patients a staff user may see."""
    if not user.is_clinic_staff:
        return User.objects.none()
    return User.objects.filter(clinic_id=user.clinic_id, role=User.Role.PATIENT)


def visible_tasks(user):
    """Tasks a user may see: their own (patient) or their clinic's (staff)."""
    tasks = Task.objects.filter(clinic_id=user.clinic_id)
    if user.is_patient:
        return tasks.filter(patient=user)
    if user.is_clinic_staff:
        return tasks
    return Task.objects.none()


def _staff_scope(user, model):
    if not user.is_clinic_staff:
        return model.objects.none()
    return model.objects.filter(clinic_id=user.clinic_id)


def clinic_care_plans(user):
    return _staff_scope(user, CarePlan)


def clinic_plan_items(user):
    return _staff_scope(user, PlanItem)


def clinic_appointments(user):
    return _staff_scope(user, Appointment)


def clinic_alerts(user):
    return _staff_scope(user, Alert)
