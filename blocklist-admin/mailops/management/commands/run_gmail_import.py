from django.core.management.base import BaseCommand, CommandError
import time

from django.conf import settings

from mailops.gmail_import import GmailImportError, GmailImportService


class Command(BaseCommand):
    help = "Run bounded historical or incremental Gmail import batches."

    def add_arguments(self, parser):
        parser.add_argument("--account", default="", help="Source Gmail account email.")
        parser.add_argument("--target", default="", help="Target mailserver mailbox email.")
        parser.add_argument("--limit", type=int, default=settings.GMAIL_IMPORT_SYNC_LIMIT, help="Maximum Gmail messages to scan in this batch.")
        parser.add_argument("--since", default="", help="Optional Gmail after: search value, for example 2026/04/01.")
        parser.add_argument("--dry-run", action="store_true", help="List a bounded batch without appending, writing state, or deleting Gmail.")
        parser.add_argument("--no-delete", action="store_true", help="Do not delete Gmail source messages for this run.")
        parser.add_argument("--incremental", action="store_true", help="Use Gmail history/recent-rescan incremental sync instead of historical scan.")
        parser.add_argument("--all", action="store_true", help="Run incremental sync for configured accounts that completed historical import.")
        parser.add_argument("--max-accounts", type=int, default=settings.GMAIL_IMPORT_SYNC_MAX_ACCOUNTS, help="Maximum Gmail import accounts per cycle.")
        parser.add_argument("--loop", action="store_true", help="Run incremental cycles forever with a sleep interval.")
        parser.add_argument("--interval-seconds", type=int, default=settings.GMAIL_IMPORT_SYNC_INTERVAL_SECONDS, help="Sleep interval between looped cycles.")

    def handle(self, *args, **options):
        service = GmailImportService()
        if options["loop"] or options["all"]:
            if options["dry_run"]:
                raise CommandError("--dry-run is only supported for single-account historical import")
            if not options["incremental"]:
                raise CommandError("--all and --loop require --incremental")
            while True:
                try:
                    cycle = service.run_incremental_cycle(
                        limit=options["limit"],
                        max_accounts=options["max_accounts"],
                        no_delete=options["no_delete"],
                    )
                except GmailImportError as exc:
                    raise CommandError(str(exc)) from exc
                self.stdout.write(
                    self.style.SUCCESS(
                        "Gmail import sync cycle complete: "
                        f"scanned={cycle.scanned} selected={cycle.selected} synced={cycle.synced} "
                        f"failed={cycle.failed} skipped={cycle.skipped}"
                    )
                )
                if not options["loop"]:
                    return
                time.sleep(int(options["interval_seconds"]))
        if not options["account"]:
            raise CommandError("--account is required for single-account import")
        if not options["target"]:
            raise CommandError("--target is required for single-account import")
        try:
            if options["incremental"]:
                if options["dry_run"]:
                    raise CommandError("--dry-run is only supported for historical import")
                result = service.run_incremental_import(
                    gmail_email=options["account"],
                    target_mailbox_email=options["target"],
                    limit=options["limit"],
                    no_delete=options["no_delete"],
                )
            else:
                result = service.run_historical_import(
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
