from .communication import unread_count
from .models import Alert


def staff_nav(request):
    """Counts shown in the navigation bar: new alerts (staff) and unread messages."""
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated and (user.is_patient or user.is_clinic_staff)):
        return {}
    counts = {"unread_message_count": unread_count(user)}
    if user.is_clinic_staff:
        counts["new_alert_count"] = Alert.objects.filter(clinic_id=user.clinic_id, status=Alert.Status.NEW).count()
    return counts
