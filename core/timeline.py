"""Prescribed vs Actual timeline for one care plan.

Builds a grid: one row per prescribed item (a medicine and all its changed
versions share one row), one column per day of the plan, plus rows for health
notes and alerts. Everything is grouped by day in the clinic's timezone.
Summaries are plain counts; Carely makes no clinical judgement.
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

from django.urls import reverse
from django.utils import timezone

from .models import Alert, Appointment, HealthNote, PlanItem, Task

MAX_DAYS = 180  # keep the grid readable; longer plans show their first 180 days

MEDICINE_MARKS = {
    Task.Status.DONE: ("✓", "taken"),
    Task.Status.SKIPPED: ("s", "skipped"),
    Task.Status.MISSED: ("✗", "missed"),
    Task.Status.PENDING: ("·", "pending"),
}


@dataclass
class Mark:
    symbol: str
    css: str  # taken / skipped / missed / pending / change / booked / note / alert
    title: str
    url: str = ""


@dataclass
class Row:
    label: str
    detail: str = ""
    summary: str = ""
    kind: str = "item"
    cells: list = field(default_factory=list)  # one list of Marks per day


def _local(dt, tz):
    return timezone.localtime(dt, tz)


def _version_text(item):
    return f"{item.dose_text} at {', '.join(item.dose_times)}"


def _medicine_lineages(items):
    """Group medicine items so that each changed version joins its original."""
    by_id = {i.pk: i for i in items}

    def root(item):
        while item.replaces_id in by_id:
            item = by_id[item.replaces_id]
        return item.pk

    groups = {}
    for item in sorted(items, key=lambda i: (i.created_at, i.pk)):
        groups.setdefault(root(item), []).append(item)
    return list(groups.values())


def plan_days(plan, items, appointments, tz):
    starts = [i.start_date for i in items]
    ends = [i.end_date or i.start_date for i in items]
    ends += [_local(a.scheduled_at, tz).date() for a in appointments]
    if not starts:
        return []
    first, last = min(starts), max(ends)
    count = min((last - first).days + 1, MAX_DAYS)
    return [first + timedelta(days=n) for n in range(count)]


def build_timeline(plan, *, now=None, for_patient=False):
    """`for_patient` leaves out alerts and health-note rows (staff information)."""
    now = now or timezone.now()
    tz = plan.clinic.tzinfo
    items = list(plan.items.all())
    tasks = list(
        Task.objects.filter(plan_item__care_plan=plan).exclude(status=Task.Status.CANCELLED).order_by("due_at")
    )
    appointments = list(Appointment.objects.filter(plan_item__care_plan=plan).select_related("task"))
    days = plan_days(plan, items, appointments, tz)
    index = {day: n for n, day in enumerate(days)}
    today = _local(now, tz).date()

    def empty_cells():
        return [[] for _ in days]

    def put(cells, when, mark):
        day = when if isinstance(when, date) and not hasattr(when, "hour") else _local(when, tz).date()
        if day in index:
            cells[index[day]].append(mark)

    tasks_by_item = {}
    for task in tasks:
        tasks_by_item.setdefault(task.plan_item_id, []).append(task)

    alerts = list(Alert.objects.filter(patient=plan.patient, task__plan_item__care_plan=plan).select_related("task"))
    rows = []

    # Medicines: one row per medicine, across its versions.
    for versions in _medicine_lineages([i for i in items if i.type == PlanItem.Type.MEDICINE]):
        cells = empty_cells()
        lineage_tasks = [t for v in versions for t in tasks_by_item.get(v.pk, [])]
        for task in sorted(lineage_tasks, key=lambda t: t.due_at):
            symbol, css = MEDICINE_MARKS[task.status]
            local_due = _local(task.due_at, tz)
            title = f"{local_due:%d %b %H:%M} · {css}"
            if task.completed_at and task.status in (Task.Status.DONE, Task.Status.SKIPPED):
                title += f" (tapped {_local(task.completed_at, tz):%H:%M})"
            put(cells, task.due_at, Mark(symbol, css, title))
        for old, new in zip(versions, versions[1:]):
            put(cells, old.superseded_at, Mark("▲", "change", f"Changed: {_version_text(old)} → {_version_text(new)}"))
        last = versions[-1]
        if last.superseded_at:
            put(cells, last.superseded_at, Mark("■", "change", f"Stopped {_local(last.superseded_at, tz):%d %b}"))

        due_so_far = [t for t in lineage_tasks if t.due_at <= now]
        taken = sum(t.status == Task.Status.DONE for t in due_so_far)
        skipped = sum(t.status == Task.Status.SKIPPED for t in due_so_far)
        missed = sum(t.status == Task.Status.MISSED for t in due_so_far)
        runs = sum(
            1 for a in alerts
            if a.source_rule == Alert.SourceRule.MISSED_DOSE_THRESHOLD and a.task.plan_item_id in {v.pk for v in versions}
        )
        if due_so_far:
            summary = f"Taken {taken} of {len(due_so_far)} due so far · skipped {skipped} · missed {missed}"
        else:
            summary = "No doses due yet"
        if runs and not for_patient:
            summary += f" · {runs} not-taken alert{'s' if runs != 1 else ''}"
        detail = " → ".join(
            _version_text(v) + (f" (from {_local(v.created_at, tz):%d %b})" if v.replaces_id else "")
            for v in versions
        )
        if last.superseded_at:
            detail += f" · stopped {_local(last.superseded_at, tz):%d %b}"
        rows.append(Row(label=versions[0].name, detail=detail, summary=summary, cells=cells))

    # Lab tests: the due day, plus the day a report arrived.
    for item in (i for i in items if i.type == PlanItem.Type.LAB_TEST):
        cells = empty_cells()
        task = next(iter(tasks_by_item.get(item.pk, [])), None)
        summary = "Not live" if task is None else {
            Task.Status.DONE: "Report uploaded",
            Task.Status.MISSED: "Missed: no report by the due date",
            Task.Status.PENDING: "Waiting for report",
        }.get(task.status, "")
        if task:
            if task.status == Task.Status.DONE and task.completed_at:
                reported = _local(task.completed_at, tz)
                late = reported.date() > item.start_date
                put(cells, task.completed_at, Mark("✓", "taken", f"Report uploaded {reported:%d %b %H:%M}"))
                summary = f"Report uploaded {reported:%d %b}" + (" (late)" if late else "")
                if late:
                    put(cells, item.start_date, Mark("✗", "missed", "Due date passed without a report"))
            else:
                symbol, css = {Task.Status.MISSED: ("✗", "missed")}.get(task.status, ("·", "pending"))
                put(cells, item.start_date, Mark(symbol, css, f"Due {item.start_date:%d %b} · {summary.lower()}"))
        if item.superseded_at:
            summary += f" · {'changed' if item.replaced_by.exists() else 'stopped'} {_local(item.superseded_at, tz):%d %b}"
        rows.append(Row(label=item.name, detail=f"Lab test due {item.start_date:%d %b}", summary=summary, cells=cells))

    # Follow-ups: the due-by day and the appointment itself.
    for item in (i for i in items if i.type == PlanItem.Type.FOLLOW_UP):
        cells = empty_cells()
        appointment = next((a for a in appointments if a.plan_item_id == item.pk), None)
        if appointment:
            symbol, css, summary = {
                Appointment.Status.BOOKED: ("◷", "booked", "Booked"),
                Appointment.Status.ATTENDED: ("✓", "taken", "Attended"),
                Appointment.Status.NOT_ATTENDED: ("✗", "missed", "Not attended"),
            }[appointment.status]
            local = _local(appointment.scheduled_at, tz)
            summary = f"{summary} · {local:%d %b %H:%M}"
            put(cells, appointment.scheduled_at, Mark(symbol, css, f"Appointment {local:%d %b %H:%M} · {css}"))
        else:
            summary = "No appointment booked"
            if item.start_date < today:
                summary += " (due date passed)"
            put(cells, item.start_date, Mark("·", "pending", f"Due by {item.start_date:%d %b} · not booked"))
        rows.append(Row(label=item.name, detail=f"Follow-up due by {item.start_date:%d %b}", summary=summary, cells=cells))

    if not for_patient:
        # Health notes and alerts (staff only).
        note_cells = empty_cells()
        notes = HealthNote.objects.filter(patient=plan.patient).prefetch_related("alerts")
        for note in notes:
            alert = next(iter(note.alerts.all()), None)
            url = reverse("staff_alert_detail", args=[alert.pk]) if alert else ""
            put(note_cells, note.created_at, Mark("✎", "note", f"{_local(note.created_at, tz):%d %b %H:%M}: {note.text[:80]}", url))
        alert_cells = empty_cells()
        all_alerts = Alert.objects.filter(patient=plan.patient).select_related("task__plan_item")
        for alert in all_alerts:
            if alert.task_id and alert.task.plan_item.care_plan_id != plan.pk:
                continue
            put(alert_cells, alert.created_at, Mark(
                "!", "alert", f"{alert.get_source_rule_display()} · {alert.get_status_display()}",
                reverse("staff_alert_detail", args=[alert.pk]),
            ))
        rows.append(Row(label="Health notes", kind="events", cells=note_cells))
        rows.append(Row(label="Alerts", kind="events", cells=alert_cells))

    first_day = days[0] if days else None
    return {
        "days": days,
        "rows": rows,
        "today": today,
        "today_index": index.get(today, -1),
        "truncated": bool(days) and len(days) == MAX_DAYS,
        "day_numbers": [(d, (d - first_day).days + 1) for d in days],
    }
