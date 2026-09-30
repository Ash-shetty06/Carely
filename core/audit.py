from .models import AuditLog


def log_action(*, actor, action, obj, changes=None):
    return AuditLog.objects.create(
        clinic_id=obj.clinic_id,
        actor=actor,
        action=action,
        model=obj._meta.label,
        record_id=obj.pk,
        changes=changes or {},
    )
