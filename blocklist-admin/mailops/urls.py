from django.urls import path

from . import views


app_name = "mailops"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("apply/", views.apply_blocklist_view, name="apply"),
    path("api/devices/", views.register_device_view, name="register_device"),
    path("api/mail/new/", views.new_mail_view, name="new_mail"),
]
