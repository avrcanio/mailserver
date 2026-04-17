from django.urls import path

from . import api
from . import views


app_name = "mailops"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("apply/", views.apply_blocklist_view, name="apply"),
    path("api/auth/login", api.LoginView.as_view(), name="api_login"),
    path("api/auth/me", api.MeView.as_view(), name="api_me"),
    path("api/auth/logout", api.LogoutView.as_view(), name="api_logout"),
    path("api/mail/folders", api.FolderListView.as_view(), name="api_mail_folders"),
    path("api/mail/conversations", api.ConversationListView.as_view(), name="api_mail_conversations"),
    path("api/mail/unified-conversations", api.UnifiedConversationListView.as_view(), name="api_mail_unified_conversations"),
    path("api/mail/messages", api.MessageListView.as_view(), name="api_mail_messages"),
    path("api/mail/messages/delete", api.DeleteMessagesView.as_view(), name="api_mail_messages_delete"),
    path("api/mail/messages/restore", api.RestoreMessagesView.as_view(), name="api_mail_messages_restore"),
    path("api/mail/messages/<str:uid>/delete", api.DeleteMessageView.as_view(), name="api_mail_message_delete"),
    path("api/mail/messages/<str:uid>/restore", api.RestoreMessageView.as_view(), name="api_mail_message_restore"),
    path("api/mail/messages/<str:uid>/attachments/<str:attachment_id>", api.AttachmentDownloadView.as_view(), name="api_mail_attachment"),
    path("api/mail/messages/<str:uid>", api.MessageDetailView.as_view(), name="api_mail_message_detail"),
    path("api/mail/send", api.SendMailView.as_view(), name="api_mail_send"),
    path("api/devices/", api.DeviceRegistrationView.as_view(), name="register_device"),
    path("api/accounts/summaries", api.AccountSummariesView.as_view(), name="api_accounts_summaries"),
    path("api/mail/new/", api.NewMailHookView.as_view(), name="new_mail"),
]
