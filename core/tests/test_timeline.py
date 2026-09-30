from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from django.test import TestCase
from django.urls import reverse

from core.communication import create_health_note
from core.models import Alert, Appointment, CarePlan, PlanItem, Task, User
from core.services import replace_plan_item, stop_plan_item
from core.timeline import build_timeline

from .helpers import make_active_plan, make_clinic, make_item, make_task, make_user

IST = ZoneInfo("Asia/Kolkata")


def ist(d, hh, mm=0):
    return datetime(2026, 10, d, hh, mm, tzinfo=IST)


def symbols(cell):
    return "".join(m.symbol for m in cell)


class TimelineTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.plan = make_active_plan(self.clinic, self.patient, self.staff)
        self.med = make_item(self.plan, name="Metformin", dose_text="500 mg", dose_times=["08:00", "20:00"],
                             start_date=date(2026, 10, 1), end_date=date(2026, 10, 4))
        self.doses = {}
        for d in (1, 2, 3, 4):
            for h in (8, 20):
                self.doses[(d, h)] = make_task(self.med, ist(d, h))

    def set(self, key, status):
        task = self.doses[key]
        task.status = status
        task.completed_at = task.due_at + timedelta(minutes=5) if status in ("done", "skipped") else None
        task.save()

    def row(self, timeline, label):
        return next(r for r in timeline["rows"] if r.label == label)

    def test_summary_before_any_dose_is_due(self):
        row = self.row(build_timeline(self.plan, now=ist(1, 7)), "Metformin")
        self.assertEqual(row.summary, "No doses due yet")

    def test_days_span_the_plan(self):
        timeline = build_timeline(self.plan, now=ist(2, 12))
        self.assertEqual(timeline["days"][0], date(2026, 10, 1))
        self.assertEqual(timeline["days"][-1], date(2026, 10, 4))
        self.assertEqual(timeline["today_index"], 1)

    def test_medicine_row_shows_each_dose_and_counts(self):
        self.set((1, 8), "done")
        self.set((1, 20), "missed")
        self.set((2, 8), "skipped")
        row = self.row(build_timeline(self.plan, now=ist(2, 12)), "Metformin")
        self.assertEqual([symbols(c) for c in row.cells], ["✓✗", "s·", "··", "··"])
        self.assertEqual(row.summary, "Taken 1 of 3 due so far · skipped 1 · missed 1")
        self.assertIn("08:00", row.cells[0][0].title)

    def test_changed_medicine_stays_on_one_row_with_marker(self):
        new = replace_plan_item(self.med, actor=self.staff, now=ist(2, 12), dose_text="1000 mg", dose_times=["08:00"])
        timeline = build_timeline(self.plan, now=ist(2, 12))
        medicine_rows = [r for r in timeline["rows"] if r.label == "Metformin"]
        self.assertEqual(len(medicine_rows), 1)
        row = medicine_rows[0]
        self.assertIn("▲", symbols(row.cells[1]))
        self.assertIn("500 mg at 08:00, 20:00 → 1000 mg at 08:00", row.detail)
        # Cancelled old doses are hidden; the new version's doses appear.
        self.assertEqual(symbols(row.cells[2]), "·")
        self.assertEqual(new.tasks.count(), 2)

    def test_stopped_medicine_marked(self):
        stop_plan_item(self.med, actor=self.staff, now=ist(3, 12))
        row = self.row(build_timeline(self.plan, now=ist(3, 12)), "Metformin")
        self.assertIn("■", symbols(row.cells[2]))
        self.assertIn("stopped 03 Oct", row.detail)

    def test_not_taken_alerts_counted_in_summary(self):
        self.set((1, 8), "missed")
        self.set((1, 20), "missed")
        Alert.objects.create(clinic=self.clinic, patient=self.patient, task=self.doses[(1, 20)],
                             source_rule=Alert.SourceRule.MISSED_DOSE_THRESHOLD)
        row = self.row(build_timeline(self.plan, now=ist(2, 12)), "Metformin")
        self.assertIn("1 not-taken alert", row.summary)

    def test_late_lab_report(self):
        lab = make_item(self.plan, type=PlanItem.Type.LAB_TEST, name="Glucose", start_date=date(2026, 10, 2))
        make_task(lab, ist(2, 23, 59), status=Task.Status.DONE, completed_at=ist(4, 9), lab_report="lab_reports/x.pdf")
        row = self.row(build_timeline(self.plan, now=ist(4, 12)), "Glucose")
        self.assertEqual(symbols(row.cells[1]), "✗")
        self.assertEqual(symbols(row.cells[3]), "✓")
        self.assertEqual(row.summary, "Report uploaded 04 Oct (late)")

    def test_follow_up_outcome_on_appointment_day(self):
        visit = make_item(self.plan, type=PlanItem.Type.FOLLOW_UP, name="Visit", start_date=date(2026, 10, 3))
        task = make_task(visit, ist(3, 23, 59), status=Task.Status.MISSED)
        Appointment.objects.create(clinic=self.clinic, patient=self.patient, plan_item=visit, task=task,
                                   scheduled_at=ist(5, 10), status=Appointment.Status.NOT_ATTENDED)
        timeline = build_timeline(self.plan, now=ist(6, 12))
        self.assertEqual(timeline["days"][-1], date(2026, 10, 5))  # extended to the appointment
        row = self.row(timeline, "Visit")
        self.assertEqual(symbols(row.cells[4]), "✗")
        self.assertTrue(row.summary.startswith("Not attended"))

    def test_notes_and_alerts_rows_link_to_alerts(self):
        note = create_health_note(patient=self.patient, text="Felt dizzy", now=ist(2, 9))
        note.created_at = ist(2, 9)
        note.save()
        timeline = build_timeline(self.plan, now=ist(2, 12))
        notes_row = self.row(timeline, "Health notes")
        alerts_row = self.row(timeline, "Alerts")
        self.assertEqual(sum(len(c) for c in notes_row.cells), 1)
        self.assertIn("Felt dizzy", next(m for c in notes_row.cells for m in c).title)
        alert = Alert.objects.get(health_note=note)
        self.assertTrue(all(m.url == reverse("staff_alert_detail", args=[alert.pk]) for c in alerts_row.cells for m in c))


class TimelinePageTests(TestCase):
    def setUp(self):
        self.clinic = make_clinic()
        self.staff = make_user(self.clinic, "staff", User.Role.STAFF)
        self.patient = make_user(self.clinic, "patient", User.Role.PATIENT)
        self.old_plan = make_active_plan(self.clinic, self.patient, self.staff)
        make_item(self.old_plan, name="Amoxicillin")
        CarePlan.objects.filter(pk=self.old_plan.pk).update(status=CarePlan.Status.COMPLETED)
        self.plan = make_active_plan(self.clinic, self.patient, self.staff)
        make_task(make_item(self.plan, name="Metformin"), ist(1, 8))
        self.url = reverse("staff_patient_detail", args=[self.patient.pk])

    def test_patient_page_shows_active_plan_timeline_by_default(self):
        self.client.force_login(self.staff)
        page = self.client.get(self.url)
        self.assertContains(page, "Prescribed vs actual")
        self.assertContains(page, '<table class="timeline">')
        self.assertContains(page, "Metformin")
        self.assertContains(page, "Choose care plan")

    def test_plan_picker_shows_earlier_plan(self):
        self.client.force_login(self.staff)
        page = self.client.get(self.url, {"plan": self.old_plan.pk})
        self.assertContains(page, f"Care plan #{self.old_plan.pk}</a>")
        self.assertContains(page, "Amoxicillin")

    def test_full_task_list_page(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse("staff_patient_tasks", args=[self.patient.pk]))
        self.assertContains(page, "All tasks")
        self.assertContains(page, "Metformin")

    def test_access(self):
        self.client.force_login(self.patient)
        self.assertEqual(self.client.get(reverse("staff_patient_tasks", args=[self.patient.pk])).status_code, 403)
        self.client.force_login(make_user(make_clinic(name="Other"), "otherstaff", User.Role.STAFF))
        self.assertEqual(self.client.get(reverse("staff_patient_tasks", args=[self.patient.pk])).status_code, 404)
        self.assertEqual(self.client.get(self.url, {"plan": self.plan.pk}).status_code, 404)
