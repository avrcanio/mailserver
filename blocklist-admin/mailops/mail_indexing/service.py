from mailops.mail_indexing.query import get_unified_conversation_page_from_index
from mailops.mail_indexing.sync import ensure_account, index_folder_results, sync_account


class MailIndexService:
    def __init__(self, imap_client_factory=None):
        if imap_client_factory is None:
            from mail_integration.imap_client import ImapClient

            imap_client_factory = ImapClient
        self.imap_client_factory = imap_client_factory

    def ensure_account(self, user, account_email, imap_host="", sent_folder=""):
        return ensure_account(user, account_email, imap_host=imap_host, sent_folder=sent_folder)

    def sync_account(self, user, credentials, limit=500, incremental=True):
        return sync_account(
            user=user,
            credentials=credentials,
            imap_client_factory=self.imap_client_factory,
            limit=limit,
            incremental=incremental,
        )

    def index_summaries(self, user, account_email, summaries_by_folder, imap_host="", sent_folder=""):
        folder_results = []
        from mailops.mail_indexing.sync import FolderSyncResult

        for folder, summaries in summaries_by_folder.items():
            folder_results.append(
                FolderSyncResult(folder=folder, summaries=tuple(summaries))
            )
        return index_folder_results(
            user=user,
            account_email=account_email,
            imap_host=imap_host,
            sent_folder=sent_folder,
            folder_results=folder_results,
        )

    def get_unified_conversation_page(self, user, account_email, limit=50):
        return get_unified_conversation_page_from_index(user=user, account_email=account_email, limit=limit)
