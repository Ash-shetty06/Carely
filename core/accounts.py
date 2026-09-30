"""Passwords, login lockout and clinic settings changes."""

from datetime import timedelta

from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from .audit import log_action
from .models import AuditLog, ClinicSettings, User

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
LOCKED_MESSAGE = "Too many failed attempts. This account is locked for 15 minutes. Staff can unlock it by resetting the password."


def is_locked(user, now=None):
    return bool(user.locked_until and user.locked_until > (now or timezone.now()))


def record_failed_login(user, now=None):
    now = now or timezone.now()
    User.objects.filter(pk=user.pk).update(failed_login_count=F("failed_login_count") + 1)
    user.refresh_from_db(fields=["failed_login_count"])
    if user.failed_login_count >= MAX_FAILED_LOGINS:
        user.locked_until = now + LOCKOUT
        user.failed_login_count = 0
        user.save(update_fields=["locked_until", "failed_login_count"])
        log_action(actor=None, action=AuditLog.Action.STATUS_CHANGE, obj=user,
                   changes={"locked_until": user.locked_until.isoformat(), "reason": "too many failed logins"})


def clear_failed_logins(user):
    if user.failed_login_count or user.locked_until:
        User.objects.filter(pk=user.pk).update(failed_login_count=0, locked_until=None)


class LockoutAuthenticationForm(AuthenticationForm):
    """Django's login form, plus a per-account lock after repeated failures.
    The same generic message is shown for unknown usernames and wrong passwords."""

    def clean(self):
        username = self.cleaned_data.get("username")
        user = User.objects.filter(username__iexact=username).first() if username else None
        if user and is_locked(user):
            raise ValidationError(LOCKED_MESSAGE, code="locked")
        try:
            data = super().clean()
        except ValidationError:
            if user:
                record_failed_login(user)
                if is_locked(user):
                    raise ValidationError(LOCKED_MESSAGE, code="locked")
            raise
        clear_failed_logins(self.get_user())
        return data


@transaction.atomic
def reset_patient_password(patient, *, staff, password):
    """Staff give a patient a new temporary password (which also unlocks the account)."""
    if not staff.is_clinic_staff or not patient.is_patient or patient.clinic_id != staff.clinic_id:
        raise PermissionDenied
    patient.set_password(password)
    patient.must_change_password = True
    patient.failed_login_count = 0
    patient.locked_until = None
    patient.save(update_fields=["password", "must_change_password", "failed_login_count", "locked_until"])
    log_action(actor=staff, action=AuditLog.Action.UPDATE, obj=patient, changes={"password": "reset by staff"})


def password_changed(user):
    """Called after a user sets their own password."""
    User.objects.filter(pk=user.pk).update(must_change_password=False)
    log_action(actor=user, action=AuditLog.Action.UPDATE, obj=user, changes={"password": "changed by user"})


@transaction.atomic
def update_clinic_settings(clinic, *, staff, grace_window_minutes, missed_dose_threshold):
    if not staff.is_clinic_staff or staff.clinic_id != clinic.pk:
        raise PermissionDenied
    settings, _ = ClinicSettings.objects.get_or_create(clinic=clinic)
    old = {"grace_window_minutes": settings.grace_window_minutes, "missed_dose_threshold": settings.missed_dose_threshold}
    new = {"grace_window_minutes": grace_window_minutes, "missed_dose_threshold": missed_dose_threshold}
    if old == new:
        return settings, False
    for name, value in new.items():
        setattr(settings, name, value)
    settings.full_clean()
    settings.save()
    log_action(actor=staff, action=AuditLog.Action.UPDATE, obj=settings,
               changes={k: [old[k], new[k]] for k in new if old[k] != new[k]})
    return settings, True
