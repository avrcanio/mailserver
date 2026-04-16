from .imap_client import ImapClient
from .smtp_client import SmtpClient


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

    def list_message_summary_page(self, credentials, folder="INBOX", limit=50, before_uid=None):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.fetch_message_summary_page(folder=folder, limit=limit, before_uid=before_uid)

    def get_message_detail(self, credentials, folder, uid):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.fetch_message_detail(folder=folder, uid=uid)

    def move_messages_to_trash(self, credentials, folder, uids):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.move_messages_to_trash(folder=folder, uids=uids)

    def restore_messages_from_trash(self, credentials, folder, target_folder, uids):
        with self.imap_client_factory() as client:
            client.login(credentials)
            return client.restore_messages_from_trash(folder=folder, target_folder=target_folder, uids=uids)

    def send_mail(self, credentials, request):
        with self.smtp_client_factory() as client:
            client.login(credentials)
            return client.send_mail(credentials, request)
