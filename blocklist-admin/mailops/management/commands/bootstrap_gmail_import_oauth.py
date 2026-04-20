from django.core.management.base import BaseCommand, CommandError

from mail_integration.exceptions import MailAuthError, MailIntegrationError
from mail_integration.gmail_client import build_authorization_url, exchange_code_for_refresh_token, oauth_config_from_settings

from mailops.models import GmailImportAccount


class Command(BaseCommand):
    help = "Bootstrap OAuth credentials for one Gmail import account."

    def add_arguments(self, parser):
        parser.add_argument("--gmail", required=True, help="Source Gmail address to import from.")
        parser.add_argument("--target", required=True, help="Target mailserver mailbox email.")
        parser.add_argument("--code", default="", help="OAuth authorization code returned by Google.")
        parser.add_argument("--state", default="", help="Optional OAuth state value for the consent URL.")

    def handle(self, *args, **options):
        gmail_email = _normalize_email(options["gmail"], "--gmail")
        target_mailbox_email = _normalize_email(options["target"], "--target")
        code = str(options.get("code") or "").strip()

        try:
            oauth_config = oauth_config_from_settings()
        except MailIntegrationError as exc:
            raise CommandError(str(exc)) from exc

        if not code:
            self.stdout.write("Open this URL to authorize Gmail import access:")
            self.stdout.write(build_authorization_url(oauth_config, state=str(options.get("state") or "").strip()))
            self.stdout.write("")
            self.stdout.write("Then rerun this command with --code <authorization-code>.")
            return

        try:
            refresh_token = exchange_code_for_refresh_token(code, oauth_config)
        except MailAuthError as exc:
            raise CommandError(str(exc)) from exc
        except MailIntegrationError as exc:
            raise CommandError(str(exc)) from exc

        account = GmailImportAccount.objects.filter(gmail_email=gmail_email).first()
        created = account is None
        if account is None:
            account = GmailImportAccount(gmail_email=gmail_email, target_mailbox_email=target_mailbox_email)
        account.target_mailbox_email = target_mailbox_email
        account.set_refresh_token(refresh_token)
        account.last_error = ""
        account.save()

        action = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{action} Gmail import account: {gmail_email} -> {target_mailbox_email}"))


def _normalize_email(value, option_name):
    email = str(value or "").strip().lower()
    if not email or "@" not in email:
        raise CommandError(f"{option_name} must be a valid email address")
    return email
