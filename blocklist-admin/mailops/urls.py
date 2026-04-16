from django.urls import path

from . import views


app_name = "mailops"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("apply/", views.apply_blocklist_view, name="apply"),
    path("api/mail/messages/", views.mailbox_summaries_view, name="mailbox_summaries"),
    path("api/mail/message/", views.mailbox_detail_view, name="mailbox_detail"),
    path("api/mail/send/", views.mailbox_send_view, name="mailbox_send"),
    path("api/devices/", views.register_device_view, name="register_device"),
    path("api/mail/new/", views.new_mail_view, name="new_mail"),
]
