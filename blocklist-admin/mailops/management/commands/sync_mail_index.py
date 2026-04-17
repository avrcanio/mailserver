from django.core.management.base import BaseCommand, CommandError

from mail_integration.schemas import MailboxCredentials

from mailops.mail_indexing import MailIndexService
from mailops.models import MailboxTokenCredential


class Command(BaseCommand):
    help = "Sync mailbox metadata into the Django mail index."

    def add_arguments(self, parser):
        parser.add_argument("--account", required=True, help="Mailbox account email to index.")
        parser.add_argument("--limit", type=int, default=500, help="Maximum messages per folder to scan. Defaults to 500.")
        parser.add_argument("--full", action="store_true", help="Run a bounded initial-style sync instead of incremental UID windowing.")

    def handle(self, *args, **options):
        account_email = str(options["account"] or "").strip().lower()
        limit = int(options["limit"])
        if limit < 1:
            raise CommandError("--limit must be greater than zero")
        try:
            credential = MailboxTokenCredential.objects.select_related("token__user").get(mailbox_email=account_email)
        except MailboxTokenCredential.DoesNotExist as exc:
            raise CommandError(f"No mailbox token credential found for {account_email}") from exc

        credentials = MailboxCredentials(email=credential.mailbox_email, password=credential.get_mailbox_password())
        index_account = MailIndexService().sync_account(credential.token.user, credentials, limit=limit, incremental=not options["full"])
        self.stdout.write(
            self.style.SUCCESS(
                f"Indexed {index_account.account_email}: "
                f"{index_account.messages.count()} messages, {index_account.conversations.count()} conversations"
            )
        )
