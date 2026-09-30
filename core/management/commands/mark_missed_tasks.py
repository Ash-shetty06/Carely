"""Scheduled check: mark overdue tasks as missed and raise missed-dose alerts.
Schedule this to run every 5 minutes."""

from django.core.management.base import BaseCommand
from django.utils import timezone

from core.alerts import raise_missed_dose_alerts
from core.services import mark_missed_tasks


class Command(BaseCommand):
    help = "Mark overdue tasks as missed and raise missed-dose alerts."

    def handle(self, *args, **options):
        now = timezone.now()
        marked = mark_missed_tasks(now=now)
        raised = raise_missed_dose_alerts(now=now)
        self.stdout.write(f"Marked {marked} task(s) as missed. Raised {raised} missed-dose alert(s).")
