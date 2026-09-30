from django.contrib.auth import views as auth_views
from django.urls import path

from . import staff_views, views
from .accounts import LockoutAuthenticationForm

urlpatterns = [
    path("", views.home, name="home"),
    path("login/", auth_views.LoginView.as_view(redirect_authenticated_user=True, authentication_form=LockoutAuthenticationForm), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("password/", views.CarelyPasswordChangeView.as_view(), name="password_change"),
    # Patient
    path("today/", views.today, name="today"),
    path("plan/", views.my_plan, name="my_plan"),
    path("tasks/<int:task_id>/respond/", views.respond_to_task, name="respond_to_task"),
    path("notes/", views.health_notes, name="health_notes"),
    path("messages/", views.patient_messages, name="patient_messages"),
    # Patient or staff (scoped per role)
    path("tasks/<int:task_id>/report/", views.download_report, name="download_report"),
    path("tasks/<int:task_id>/report/upload/", views.upload_report, name="upload_report"),
    # Staff
    path("staff/patients/", staff_views.staff_patients, name="staff_patients"),
    path("staff/patients/new/", staff_views.add_patient, name="staff_add_patient"),
    path("staff/patients/<int:patient_id>/", staff_views.staff_patient_detail, name="staff_patient_detail"),
    path("staff/patients/<int:patient_id>/tasks/", staff_views.staff_patient_tasks, name="staff_patient_tasks"),
    path("staff/patients/<int:patient_id>/plans/new/", staff_views.new_care_plan, name="staff_new_care_plan"),
    path("staff/plans/<int:plan_id>/", staff_views.care_plan_detail, name="staff_care_plan"),
    path("staff/plans/<int:plan_id>/items/<str:item_type>/add/", staff_views.add_item, name="staff_add_item"),
    path("staff/plans/<int:plan_id>/activate/", staff_views.activate_care_plan, name="staff_activate_plan"),
    path("staff/plans/<int:plan_id>/end/", staff_views.end_care_plan, name="staff_end_plan"),
    path("staff/items/<int:item_id>/change/", staff_views.change_item, name="staff_change_item"),
    path("staff/items/<int:item_id>/stop/", staff_views.stop_item, name="staff_stop_item"),
    path("staff/items/<int:item_id>/remove/", staff_views.remove_item, name="staff_remove_item"),
    path("staff/tasks/<int:task_id>/appointment/", staff_views.book_appointment, name="staff_book_appointment"),
    path("staff/appointments/<int:appointment_id>/", staff_views.update_appointment, name="staff_update_appointment"),
    path("staff/alerts/", staff_views.alert_inbox, name="staff_alerts"),
    path("staff/alerts/<int:alert_id>/", staff_views.alert_detail, name="staff_alert_detail"),
    path("staff/settings/", staff_views.clinic_settings, name="staff_settings"),
    path("staff/patients/<int:patient_id>/password/", staff_views.reset_password, name="staff_reset_password"),
    path("staff/messages/", staff_views.message_threads, name="staff_messages"),
    path("staff/messages/<int:message_id>/to-note/", staff_views.message_to_note, name="staff_message_to_note"),
    path("staff/patients/<int:patient_id>/messages/", staff_views.staff_patient_messages, name="staff_patient_messages"),
]
