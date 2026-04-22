from django.urls import path

from . import api
from . import views


app_name = "mailops"

urlpatterns = [
    path("privacy/", views.privacy_policy, name="privacy_policy"),
    path("", views.dashboard, name="dashboard"),
    path("apply/", views.apply_blocklist_view, name="apply"),
    path("api/auth/login", api.LoginView.as_view(), name="api_login"),
    path("api/auth/me", api.MeView.as_view(), name="api_me"),
    path("api/auth/logout", api.LogoutView.as_view(), name="api_logout"),
    path("api/external-accounts", api.ExternalAccountsView.as_view(), name="api_external_accounts"),
    path("api/external-accounts/gmail", api.GmailAccountStatusView.as_view(), name="api_gmail_account_status"),
    path("api/external-accounts/gmail/connect/start", api.GmailConnectStartView.as_view(), name="api_gmail_connect_start"),
    path("api/external-accounts/gmail/connect/complete", api.GmailConnectCompleteView.as_view(), name="api_gmail_connect_complete"),
    path("api/external-accounts/gmail/disconnect", api.GmailDisconnectView.as_view(), name="api_gmail_disconnect"),
    path("api/external-accounts/gmail/sync", api.GmailSyncTriggerView.as_view(), name="api_gmail_sync"),
    path("oauth/gmail/callback", api.GmailOAuthCallbackView.as_view(), name="gmail_oauth_callback"),
    path("api/mail/folders", api.FolderListView.as_view(), name="api_mail_folders"),
    path("api/mail/conversations", api.ConversationListView.as_view(), name="api_mail_conversations"),
    path("api/mail/unified-conversations", api.UnifiedConversationListView.as_view(), name="api_mail_unified_conversations"),
    path("api/mail/index-status", api.MailIndexStatusView.as_view(), name="api_mail_index_status"),
    path("api/mail/messages", api.MessageListView.as_view(), name="api_mail_messages"),
    path("api/mail/messages/delete", api.DeleteMessagesView.as_view(), name="api_mail_messages_delete"),
    path("api/mail/messages/restore", api.RestoreMessagesView.as_view(), name="api_mail_messages_restore"),
    path("api/mail/messages/<str:uid>/delete", api.DeleteMessageView.as_view(), name="api_mail_message_delete"),
    path("api/mail/messages/<str:uid>/restore", api.RestoreMessageView.as_view(), name="api_mail_message_restore"),
    path("api/mail/messages/<str:uid>/attachments/<str:attachment_id>", api.AttachmentDownloadView.as_view(), name="api_mail_attachment"),
    path("api/mail/messages/<str:uid>", api.MessageDetailView.as_view(), name="api_mail_message_detail"),
    path("api/contacts", api.ContactListCreateView.as_view(), name="api_contacts"),
    path("api/contacts/suggest", api.ContactSuggestView.as_view(), name="api_contacts_suggest"),
    path("api/contacts/<int:contact_id>", api.ContactDetailView.as_view(), name="api_contact_detail"),
    path("api/mail/send", api.SendMailView.as_view(), name="api_mail_send"),
    path("api/devices/", api.DeviceRegistrationView.as_view(), name="register_device"),
    path("api/accounts/summaries", api.AccountSummariesView.as_view(), name="api_accounts_summaries"),
    path("api/mail/new/", api.NewMailHookView.as_view(), name="new_mail"),
]
