from dataclasses import replace

from .exceptions import MailAttachmentLimitError, MailForwardAttachmentNotFoundError, MailForwardAttachmentNotVisibleError
from .imap_client import ImapClient
from .schemas import SendMailAttachment
from .smtp_client import SmtpClient


MAX_SEND_ATTACHMENT_SIZE_BYTES = 10 * 1024 * 1024
MAX_SEND_ATTACHMENTS_TOTAL_BYTES = 25 * 1024 * 1024


class MailboxService:
    def __init__(self, imap_client_factory=ImapClient, smtp_client_factory=SmtpClient):
        self.imap_client_factory = imap_client_factory
        self.smtp_client_factory = smtp_client_factory

    def list_folders(self, credentials):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.list_folders()

    def list_message_summaries(self, credentials, folder="INBOX", limit=50):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.fetch_message_summaries(folder=folder, limit=limit)

    def get_account_summary(self, credentials):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.fetch_account_summary()

    def list_message_summary_page(self, credentials, folder="INBOX", limit=50, before_uid=None):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.fetch_message_summary_page(folder=folder, limit=limit, before_uid=before_uid)

    def list_conversations(self, credentials, folder="INBOX", limit=50):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.fetch_conversation_page(folder=folder, limit=limit)

    def get_message_detail(self, credentials, folder, uid):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.fetch_message_detail(folder=folder, uid=uid)

    def get_attachment(self, credentials, folder, uid, attachment_id):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.fetch_attachment(folder=folder, uid=uid, attachment_id=attachment_id)

    def get_attachments(self, credentials, folder, uid):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.fetch_attachments(folder=folder, uid=uid)

    def move_messages_to_trash(self, credentials, folder, uids):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.move_messages_to_trash(folder=folder, uids=uids)

    def restore_messages_from_trash(self, credentials, folder, target_folder, uids):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.restore_messages_from_trash(folder=folder, target_folder=target_folder, uids=uids)

    def send_mail(self, credentials, request):
        request = self._resolve_forwarded_attachments(credentials, request)
        _validate_attachment_limits(request.attachments)
        with self.smtp_client_factory() as client:
            client.login(credentials)
            return client.send_mail(credentials, request)

    def _resolve_forwarded_attachments(self, credentials, request):
        source = request.forward_source_message
        if source is None:
            return request
        with self.imap_client_factory() as client:
            client.login(credentials)
            source_attachments = client.fetch_attachments(folder=source.folder, uid=source.uid)
        by_id = {attachment.summary.id: attachment for attachment in source_attachments}
        forwarded = []
        for attachment_id in source.attachment_ids:
            attachment = by_id.get(attachment_id)
            if attachment is None:
                raise MailForwardAttachmentNotFoundError(f"Forwarded attachment {attachment_id} was not found")
            if not attachment.summary.is_visible:
                raise MailForwardAttachmentNotVisibleError(f"Forwarded attachment {attachment_id} is not visible")
            filename = (attachment.summary.filename or "").strip()
            if not filename:
                raise MailForwardAttachmentNotFoundError(f"Forwarded attachment {attachment_id} has no filename")
            forwarded.append(
                SendMailAttachment(
                    filename=filename,
                    content_type=attachment.summary.content_type or "application/octet-stream",
                    content=attachment.content,
                )
            )
        return replace(request, attachments=tuple(forwarded) + tuple(request.attachments))


def _validate_attachment_limits(attachments):
    total_size = 0
    for attachment in attachments:
        size = len(attachment.content or b"")
        if size > MAX_SEND_ATTACHMENT_SIZE_BYTES:
            raise MailAttachmentLimitError("attachment_too_large")
        total_size += size
        if total_size > MAX_SEND_ATTACHMENTS_TOTAL_BYTES:
            raise MailAttachmentLimitError("attachments_too_large")
