from django import forms
from django.contrib.auth.password_validation import validate_password
from django.utils import timezone

from .models import PlanItem, User, _is_hh_mm

DATE = forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")
DATETIME = forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M")


class PatientCreateForm(forms.Form):
    full_name = forms.CharField(max_length=200)
    username = forms.CharField(max_length=150, help_text="Letters, numbers and @ . + - _ only.")
    password = forms.CharField(
        label="Temporary password", widget=forms.PasswordInput(render_value=False),
        help_text="Give this to the patient in person.",
    )
    password_again = forms.CharField(label="Temporary password again", widget=forms.PasswordInput)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        User.username_validator(username)
        if User.objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("This username is already taken.")
        return username

    def clean(self):
        data = super().clean()
        password = data.get("password")
        if password and password != data.get("password_again"):
            self.add_error("password_again", "The two passwords don't match.")
        elif password:
            candidate = User(username=data.get("username", ""), full_name=data.get("full_name", ""))
            try:
                validate_password(password, user=candidate)
            except forms.ValidationError as error:
                self.add_error("password", error)
        return data


class DoseTimesField(forms.CharField):
    """Comma-separated clock times, e.g. "08:00, 20:00" -> ["08:00", "20:00"]."""

    def prepare_value(self, value):
        return ", ".join(value) if isinstance(value, list) else value

    def to_python(self, value):
        value = super().to_python(value)
        times = [t.strip() for t in value.split(",") if t.strip()]
        times = [f"0{t}" if len(t) == 4 and t[1] == ":" else t for t in times]  # "8:00" -> "08:00"
        for t in times:
            if not _is_hh_mm(t):
                raise forms.ValidationError(f"'{t}' is not a time like 08:00.")
        if len(set(times)) != len(times):
            raise forms.ValidationError("Dose times must not repeat.")
        return sorted(times)


class PlanItemForm(forms.ModelForm):
    """Add or change a plan item. The fields shown depend on the item type."""

    FIELDS_BY_TYPE = {
        PlanItem.Type.MEDICINE: ["name", "dose_text", "dose_times", "start_date", "end_date", "notes"],
        PlanItem.Type.LAB_TEST: ["name", "start_date", "notes"],
        PlanItem.Type.FOLLOW_UP: ["name", "start_date", "notes"],
    }
    dose_times = DoseTimesField(label="Dose times", help_text="24-hour clock, comma-separated, e.g. 08:00, 20:00")

    class Meta:
        model = PlanItem
        fields = ["name", "dose_text", "dose_times", "start_date", "end_date", "notes"]
        widgets = {"start_date": DATE, "end_date": DATE, "notes": forms.Textarea(attrs={"rows": 2})}

    def __init__(self, *args, item_type, changing=False, **kwargs):
        instance = kwargs.get("instance") or PlanItem()
        instance.type = item_type
        kwargs["instance"] = instance
        super().__init__(*args, **kwargs)
        keep = list(self.FIELDS_BY_TYPE[item_type])
        if changing:
            keep.remove("name")  # a different medicine or test is a new item, not a change
        for name in list(self.fields):
            if name not in keep:
                del self.fields[name]

        labels = {
            PlanItem.Type.MEDICINE: {"name": "Medicine", "dose_text": "Dose", "start_date": "First day", "end_date": "Last day"},
            PlanItem.Type.LAB_TEST: {"name": "Lab test", "start_date": "Due date"},
            PlanItem.Type.FOLLOW_UP: {"name": "Reason", "start_date": "Due by"},
        }[item_type]
        for name, label in labels.items():
            if name in self.fields:
                self.fields[name].label = label
        if "dose_text" in self.fields:
            self.fields["dose_text"].required = True
            self.fields["dose_text"].widget.attrs["placeholder"] = "e.g. 500 mg"
        if "end_date" in self.fields:
            self.fields["end_date"].required = True
        if item_type == PlanItem.Type.FOLLOW_UP and "name" in self.fields and not self.instance.pk:
            self.fields["name"].initial = "Follow-up visit"

    def item_fields(self):
        """Cleaned values for the fields on this form, ready for the services."""
        return {name: self.cleaned_data[name] for name in self.fields}


class ReviewForm(forms.Form):
    reviewed_by_doctor = forms.CharField(
        label="Reviewed by", max_length=200, widget=forms.TextInput(attrs={"placeholder": "Dr. ..."})
    )
    reviewed_on = forms.DateField(label="Review date", widget=DATE)
    confirm = forms.BooleanField(label="I confirm the doctor reviewed this care plan.")

    def clean_reviewed_on(self):
        reviewed_on = self.cleaned_data["reviewed_on"]
        if reviewed_on > timezone.localdate():
            raise forms.ValidationError("The review date can't be in the future.")
        return reviewed_on


class AppointmentForm(forms.Form):
    scheduled_at = forms.DateTimeField(label="Date and time", widget=DATETIME, input_formats=["%Y-%m-%dT%H:%M"])


class LabReportForm(forms.Form):
    report = forms.FileField(
        label="Lab report (PDF, JPG or PNG, max 10 MB)",
        widget=forms.ClearableFileInput(attrs={"accept": ".pdf,.jpg,.jpeg,.png"}),
    )


class TriageForm(forms.Form):
    outcome = forms.CharField(
        label="Outcome", widget=forms.Textarea(attrs={"rows": 3}),
        help_text="What was done, e.g. called the patient, informed the doctor.",
    )
    doctors_advice = forms.CharField(
        label="Doctor's advice (as told to staff)", required=False, widget=forms.Textarea(attrs={"rows": 3})
    )


class HealthNoteForm(forms.Form):
    text = forms.CharField(
        label="How are you feeling?", max_length=2000, widget=forms.Textarea(attrs={"rows": 5}),
        help_text="Describe it in your own words.",
    )
    tags = forms.CharField(
        label="Tags (optional)", required=False, max_length=400,
        widget=forms.TextInput(attrs={"placeholder": "e.g. dizzy, tired"}),
        help_text="Short words, separated by commas.",
    )


class MessageForm(forms.Form):
    body = forms.CharField(label="Message", max_length=2000, widget=forms.Textarea(attrs={"rows": 3}))
    about = forms.ChoiceField(label="About (optional)", required=False)

    def __init__(self, *args, link_choices=(), **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["about"].choices = [("", "—"), *link_choices]


class ClinicSettingsForm(forms.Form):
    grace_window_minutes = forms.IntegerField(
        label="Grace window (minutes)", min_value=15, max_value=720,
        help_text="How long after a dose's time the patient can still tap Taken or Skipped before it counts as missed. 15–720.",
    )
    missed_dose_threshold = forms.IntegerField(
        label="Doses not taken in a row before an alert", min_value=1, max_value=10,
        help_text="Missed or skipped doses of the same medicine in a row that raise an alert. 1–10.",
    )


class TemporaryPasswordForm(forms.Form):
    password = forms.CharField(label="New temporary password", widget=forms.PasswordInput,
                               help_text="The patient must change it when they next log in.")
    password_again = forms.CharField(label="New temporary password again", widget=forms.PasswordInput)

    def __init__(self, *args, user, **kwargs):
        self.user = user
        super().__init__(*args, **kwargs)

    def clean(self):
        data = super().clean()
        password = data.get("password")
        if password and password != data.get("password_again"):
            self.add_error("password_again", "The two passwords don't match.")
        elif password:
            try:
                validate_password(password, user=self.user)
            except forms.ValidationError as error:
                self.add_error("password", error)
        return data
