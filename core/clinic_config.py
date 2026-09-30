"""Clinic-configurable settings, with the model defaults as fallback."""

from datetime import timedelta

from .models import ClinicSettings


def clinic_setting(clinic_id, name):
    value = ClinicSettings.objects.filter(clinic_id=clinic_id).values_list(name, flat=True).first()
    return value if value is not None else ClinicSettings._meta.get_field(name).default


def grace_window(clinic_id):
    """How long after a dose's due time the patient can still respond."""
    return timedelta(minutes=clinic_setting(clinic_id, "grace_window_minutes"))


def missed_dose_threshold(clinic_id):
    """Doses in a row not taken (missed or skipped) that raise an alert."""
    return clinic_setting(clinic_id, "missed_dose_threshold")
