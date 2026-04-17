import time

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from mailops.mail_indexing.runner import run_sync_cycle


class Command(BaseCommand):
    help = "Run periodic mail index sync cycles for indexed mailbox accounts."

    def add_arguments(self, parser):
        parser.add_argument("--account", default="", help="Restrict sync cycle to one mailbox account email.")
        parser.add_argument("--limit", type=int, default=settings.MAIL_INDEX_SYNC_LIMIT, help="Maximum messages per folder to scan.")
        parser.add_argument("--max-accounts", type=int, default=settings.MAIL_INDEX_SYNC_MAX_ACCOUNTS, help="Maximum accounts per cycle.")
        parser.add_argument(
            "--stale-after-seconds",
            type=int,
            default=settings.MAIL_INDEX_SYNC_STALE_AFTER_SECONDS,
            help="Refresh accounts whose index is older than this.",
        )
        parser.add_argument(
            "--failure-cooldown-seconds",
            type=int,
            default=settings.MAIL_INDEX_SYNC_FAILURE_COOLDOWN_SECONDS,
            help="Delay before retrying failed or partial syncs.",
        )
        parser.add_argument("--loop", action="store_true", help="Run sync cycles forever with a sleep interval.")
        parser.add_argument(
            "--interval-seconds",
            type=int,
            default=settings.MAIL_INDEX_SYNC_INTERVAL_SECONDS,
            help="Sleep interval between looped cycles.",
        )

    def handle(self, *args, **options):
        limit = _positive_int(options["limit"], "--limit")
        max_accounts = _positive_int(options["max_accounts"], "--max-accounts")
        stale_after_seconds = _positive_int(options["stale_after_seconds"], "--stale-after-seconds")
        failure_cooldown_seconds = _positive_int(options["failure_cooldown_seconds"], "--failure-cooldown-seconds")
        interval_seconds = _positive_int(options["interval_seconds"], "--interval-seconds")

        while True:
            result = run_sync_cycle(
                account_email=options["account"],
                limit=limit,
                max_accounts=max_accounts,
                stale_after_seconds=stale_after_seconds,
                failure_cooldown_seconds=failure_cooldown_seconds,
            )
            self.stdout.write(
                self.style.SUCCESS(
                    "Mail index sync cycle complete: "
                    f"scanned={result.scanned} selected={result.selected} synced={result.synced} "
                    f"failed={result.failed} skipped={result.skipped} elapsed={result.elapsed_seconds:.2f}s"
                )
            )
            if not options["loop"]:
                return
            time.sleep(interval_seconds)


def _positive_int(value, option_name):
    value = int(value)
    if value < 1:
        raise CommandError(f"{option_name} must be greater than zero")
    return value
