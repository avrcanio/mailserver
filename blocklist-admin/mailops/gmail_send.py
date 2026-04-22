from email import policy as email_policy

from django.utils import timezone

from mail_integration.exceptions import MailProtocolError
from mail_integration.gmail_client import GmailClient, oauth_config_from_settings
from mail_integration.mailbox_service import MailboxService
from mail_integration.smtp_client import build_email_message

from .models import GmailImportAccount, GmailImportMessage


class GmailOutboundSendService:
    def __init__(self, mailbox_service=None, gmail_client_factory=None):
        self.mailbox_service = mailbox_service or MailboxService()
        self.gmail_client_factory = gmail_client_factory or (lambda account: GmailClient(account.get_refresh_token(), oauth_config=oauth_config_from_settings()))

    def can_send_for(self, user, mailbox_email):
        return self._account_for(user, mailbox_email) is not None

    def send_mail(self, user, credentials, request):
        account = self._account_for(user, credentials.email)
        if account is None:
            return None

        request = self.mailbox_service.prepare_send_request(credentials, request)
        try:
            message = build_email_message(credentials.email, request, include_bcc=True)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise MailProtocolError(f"Could not build Gmail message: {exc}") from exc

        raw_bytes = message.as_bytes(policy=email_policy.SMTP)
        gmail_client = self.gmail_client_factory(account)
        gmail_ref = gmail_client.send_raw_message(raw_bytes)
        if "Bcc" in message:
            del message["Bcc"]
        self.mailbox_service.append_sent_copy(credentials, message)
        self._commit_sent_record(account, gmail_ref, message)
        if account.delete_after_import:
            self._clean_sent_source(account, gmail_ref.gmail_message_id, gmail_client)
        return message["Message-ID"]

    def _account_for(self, user, mailbox_email):
        if not user or not getattr(user, "is_authenticated", False):
            return None
        return (
            GmailImportAccount.objects.filter(user=user, target_mailbox_email=(mailbox_email or "").strip().lower())
            .select_related("user")
            .first()
        )

    def _commit_sent_record(self, account, gmail_ref, message):
        message_id = str(message.get("Message-ID", "") or "")
        now = timezone.now()
        record, _ = GmailImportMessage.objects.get_or_create(
            import_account=account,
            gmail_message_id=gmail_ref.gmail_message_id,
            defaults={"fetched_at": now},
        )
        record.gmail_thread_id = gmail_ref.gmail_thread_id
        record.rfc_message_id = message_id
        record.target_folder = "Sent"
        record.state = GmailImportMessage.STATE_COMMITTED
        record.append_status = GmailImportMessage.STATUS_SUCCESS
        record.appended_at = record.appended_at or now
        record.committed_at = record.committed_at or now
        record.error = ""
        record.save(
            update_fields=[
                "gmail_thread_id",
                "rfc_message_id",
                "target_folder",
                "state",
                "append_status",
                "appended_at",
                "committed_at",
                "error",
                "updated_at",
            ]
        )

    def _clean_sent_source(self, account, gmail_message_id, gmail_client):
        record = GmailImportMessage.objects.get(import_account=account, gmail_message_id=gmail_message_id)
        try:
            gmail_client.delete_message(gmail_message_id)
        except Exception as exc:
            record.state = GmailImportMessage.STATE_COMMITTED
            record.cleanup_status = GmailImportMessage.STATUS_FAILED
            record.error = str(exc)[:2000]
            record.save(update_fields=["state", "cleanup_status", "error", "updated_at"])
            return
        record.state = GmailImportMessage.STATE_CLEANED
        record.cleanup_status = GmailImportMessage.STATUS_SUCCESS
        record.cleaned_at = timezone.now()
        record.error = ""
        record.save(update_fields=["state", "cleanup_status", "cleaned_at", "error", "updated_at"])
