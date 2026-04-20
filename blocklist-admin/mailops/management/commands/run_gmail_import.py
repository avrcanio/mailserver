from django.core.management.base import BaseCommand, CommandError

from mailops.gmail_import import GmailImportError, GmailImportService


class Command(BaseCommand):
    help = "Run a bounded historical Gmail import batch into a mailserver mailbox."

    def add_arguments(self, parser):
        parser.add_argument("--account", required=True, help="Source Gmail account email.")
        parser.add_argument("--target", required=True, help="Target mailserver mailbox email.")
        parser.add_argument("--limit", type=int, default=100, help="Maximum Gmail messages to scan in this batch.")
        parser.add_argument("--since", default="", help="Optional Gmail after: search value, for example 2026/04/01.")
        parser.add_argument("--dry-run", action="store_true", help="List a bounded batch without appending, writing state, or deleting Gmail.")
        parser.add_argument("--no-delete", action="store_true", help="Do not delete Gmail source messages for this run.")

    def handle(self, *args, **options):
        try:
            result = GmailImportService().run_historical_import(
                gmail_email=options["account"],
                target_mailbox_email=options["target"],
                limit=options["limit"],
                since=options["since"],
                dry_run=options["dry_run"],
                no_delete=options["no_delete"],
            )
        except GmailImportError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                "Gmail import complete: "
                f"scanned={result.scanned} appended={result.appended} committed={result.committed} "
                f"cleaned={result.cleaned} skipped={result.skipped} failed={result.failed}"
            )
        )
