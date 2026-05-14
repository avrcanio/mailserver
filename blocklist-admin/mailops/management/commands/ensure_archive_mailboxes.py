import logging

from django.core.management.base import BaseCommand

from mailops.services import MailboxProvisioningError, ensure_archive_mailbox, list_mailserver_mailbox_emails

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Ensure each docker-mailserver mailbox has an Archive folder (doveadm)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List mailboxes and intended actions without calling doveadm create.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        try:
            emails = list_mailserver_mailbox_emails()
        except MailboxProvisioningError as exc:
            raise SystemExit(str(exc)) from exc

        if not emails:
            self.stdout.write(self.style.WARNING("No mailboxes returned by setup email list."))
            return

        self.stdout.write(f"Found {len(emails)} mailbox(es).")
        created = 0
        skipped = 0
        failed = 0

        for email in emails:
            if dry_run:
                self.stdout.write(f"[dry-run] would ensure Archive for {email}")
                continue
            try:
                status = ensure_archive_mailbox(email)
                if status == "created":
                    created += 1
                    self.stdout.write(self.style.SUCCESS(f"Archive created: {email}"))
                else:
                    skipped += 1
                    self.stdout.write(f"Archive already present: {email}")
            except MailboxProvisioningError as exc:
                failed += 1
                self.stdout.write(self.style.ERROR(f"Failed {email}: {exc}"))
                logger.warning("ensure_archive_mailbox failed for %s: %s", email, exc)

        if dry_run:
            self.stdout.write(self.style.SUCCESS(f"Dry run complete ({len(emails)} mailboxes)."))
            return

        self.stdout.write(
            self.style.SUCCESS(f"Done. processed={len(emails)} created={created} skipped={skipped} failed={failed}")
        )
        if failed:
            raise SystemExit(1)
