from zoneinfo import ZoneInfo

from django.utils import timezone


class ClinicTimezoneMiddleware:
    """Show all times in the logged-in user's clinic timezone."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and user.clinic_id:
            timezone.activate(ZoneInfo(user.clinic.timezone))
        else:
            timezone.deactivate()
        return self.get_response(request)


class ForcePasswordChangeMiddleware:
    """Send users with a temporary password to the change-password page first."""

    ALLOWED = ("/password/", "/logout/", "/static/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and user.must_change_password and not request.path.startswith(self.ALLOWED):
            from django.shortcuts import redirect

            return redirect("password_change")
        return self.get_response(request)
