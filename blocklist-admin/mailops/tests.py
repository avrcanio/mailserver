import importlib
import io
import json
from datetime import datetime, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.apps import apps
from django.contrib import admin as django_admin
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from mail_integration.exceptions import (
    MailAttachmentNotFoundError,
    MailAuthError,
    MailConnectionError,
    MailForwardAttachmentNotFoundError,
    MailForwardAttachmentNotVisibleError,
    MailInvalidOperationError,
    MailSendError,
)
from mail_integration.gmail_client import GmailHistoryMessage, GmailHistoryPage, GmailMessageRef, GmailRawMessage
from mail_integration.mailbox_service import MailboxService
from mail_integration.schemas import (
    MailAttachmentContent,
    MailAttachmentSummary,
    MailboxAccountSummary,
    MailConversationParticipant,
    MailConversationSummary,
    MailConversationSummaryPage,
    MailFolderSummary,
    MailUnifiedConversationSummary,
    MailUnifiedConversationSummaryPage,
    MailUnifiedMessageSummary,
    MailMessageDetail,
    MailMessageMoveFailure,
    MailMessageMoveToTrashResult,
    MailMessageRestoreResult,
    MailMessageSummary,
    MailMessageSummaryPage,
)

from .api import create_mailbox_token, mailbox_credentials_from_request, signed_gmail_oauth_state
from .credential_crypto import (
    ENCRYPTED_VALUE_PREFIX,
    CredentialEncryptionError,
    decrypt_mailbox_password,
    encrypt_credential_value,
    encrypt_mailbox_password,
)
from .gmail_import import GmailImportError, GmailImportService
from .mail_indexing import MailIndexService
from .mail_indexing.runner import run_sync_cycle, select_accounts_for_sync
from .mail_indexing.sync import FolderSyncResult, reconcile_recent_missing_messages
from .models import (
    DeviceRegistration,
    GmailImportAccount,
    GmailImportMessage,
    GmailImportRun,
    MailAccountIndex,
    MailConversationIndex,
    MailMessageIndex,
    MailboxTokenCredential,
    PushNotificationLog,
)
from .services import MailboxCleanupError, MailboxProvisioningError, create_mailbox_account, delete_mailbox_account, sanitize_mailbox_command_output


TEST_ENCRYPTION_KEY = "DhbKZLv4bil01DI7X2u09Q69vebV7py6A9m9q0gOCfg="


class FakeGmailClient:
    def __init__(self, refs=(), raw_messages=None, history_pages=None, events=None, delete_error=None, history_error=None):
        self.refs = tuple(refs)
        self.raw_messages = raw_messages or {}
        self.history_pages = list(history_pages or [])
        self.events = events if events is not None else []
        self.delete_error = delete_error
        self.history_error = history_error
        self.deleted = []
        self.list_calls = []
        self.history_calls = []

    def list_message_refs(self, query="", max_results=100, page_token=""):
        self.list_calls.append({"query": query, "max_results": max_results, "page_token": page_token})
        return self.refs[:max_results], ""

    def fetch_raw_message(self, gmail_message_id):
        self.events.append(f"fetch:{gmail_message_id}")
        return self.raw_messages[gmail_message_id]

    def list_history_page(self, start_history_id, page_token=""):
        self.history_calls.append({"start_history_id": start_history_id, "page_token": page_token})
        if self.history_error:
            raise self.history_error
        return self.history_pages.pop(0) if self.history_pages else GmailHistoryPage(history_id=start_history_id)

    def delete_message(self, gmail_message_id):
        self.events.append(f"delete:{gmail_message_id}")
        if self.delete_error:
            raise self.delete_error
        self.deleted.append(gmail_message_id)


class FakeImapClient:
    def __init__(self, events=None, append_error=None, sent_folder="Sent"):
        self.events = events if events is not None else []
        self.append_error = append_error
        self.sent_folder = sent_folder
        self.appended = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None

    def login(self, credentials):
        self.events.append(f"login:{credentials.email}")

    def _resolve_sent_folder(self):
        return self.sent_folder

    def append_message(self, folder, message_bytes, flags=(r"\Seen",)):
        self.events.append(f"append:{folder}")
        if self.append_error:
            raise self.append_error
        self.appended.append((folder, message_bytes, flags))


class MailboxProvisioningServiceTests(TestCase):
    @patch("mailops.services.docker.DockerClient")
    def test_create_mailbox_account_executes_setup_add(self, docker_client_class):
        container = Mock()
        container.exec_run.return_value = Mock(exit_code=0, output=b"created\n")
        docker_client_class.return_value.containers.get.return_value = container

        output = create_mailbox_account(" USER@Example.COM ", "secret-password")

        self.assertEqual(output, "created")
        container.exec_run.assert_called_once_with(["setup", "email", "add", "user@example.com", "secret-password"])

    @patch("mailops.services.docker.DockerClient")
    def test_create_mailbox_account_sanitizes_failure_output(self, docker_client_class):
        container = Mock()
        container.exec_run.return_value = Mock(exit_code=1, output=b"failed secret-password\n")
        docker_client_class.return_value.containers.get.return_value = container

        with self.assertRaises(MailboxProvisioningError) as context:
            create_mailbox_account("user@example.com", "secret-password")

        self.assertIn("[redacted-password]", str(context.exception))
        self.assertNotIn("secret-password", str(context.exception))

    @patch("mailops.services.docker.DockerClient")
    def test_delete_mailbox_account_executes_setup_del(self, docker_client_class):
        container = Mock()
        container.exec_run.return_value = Mock(exit_code=0, output=b"deleted\n")
        docker_client_class.return_value.containers.get.return_value = container

        output = delete_mailbox_account(" USER@Example.COM ", password="secret-password")

        self.assertEqual(output, "deleted")
        container.exec_run.assert_called_once_with(["setup", "email", "del", "-y", "user@example.com"])

    @patch("mailops.services.docker.DockerClient")
    def test_delete_mailbox_account_raises_cleanup_error(self, docker_client_class):
        container = Mock()
        container.exec_run.return_value = Mock(exit_code=1, output=b"delete failed secret-password\n")
        docker_client_class.return_value.containers.get.return_value = container

        with self.assertRaises(MailboxCleanupError) as context:
            delete_mailbox_account("user@example.com", password="secret-password")

        self.assertNotIn("secret-password", str(context.exception))
        self.assertIn("[redacted-password]", str(context.exception))

    def test_sanitize_mailbox_command_output_redacts_password(self):
        self.assertEqual(
            sanitize_mailbox_command_output("before secret-password after", password="secret-password"),
            "before [redacted-password] after",
        )


@override_settings(MAILBOX_AUTO_CREATE_FROM_USER_ADMIN=True, MAILBOX_AUTO_CREATE_SKIP_STAFF=True)
class MailboxUserAdminTests(TestCase):
    def setUp(self):
        self.admin_user = get_user_model().objects.create_superuser(
            username="admin",
            email="admin@example.com",
            password="admin-password",
        )
        self.client.force_login(self.admin_user)

    def post_add_user(self, username="user@example.com", email="USER@Example.COM", password="mail-secret", follow=False):
        return self.client.post(
            reverse("admin:auth_user_add"),
            data={
                "username": username,
                "email": email,
                "usable_password": "true",
                "password1": password,
                "password2": password,
                "_save": "Save",
            },
            follow=follow,
        )

    @patch("mailops.admin.create_mailbox_account")
    def test_user_admin_create_provisions_mailbox_with_raw_password(self, create_mailbox):
        response = self.post_add_user()

        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(email="user@example.com")
        self.assertTrue(user.check_password("mail-secret"))
        create_mailbox.assert_called_once_with("user@example.com", "mail-secret")

    @patch("mailops.admin.create_mailbox_account", side_effect=MailboxProvisioningError("setup failed"))
    def test_user_admin_provisioning_failure_rolls_back_user(self, create_mailbox):
        response = self.post_add_user(follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(email="user@example.com").exists())
        create_mailbox.assert_called_once_with("user@example.com", "mail-secret")

    @patch("mailops.admin.delete_mailbox_account")
    @patch("mailops.admin.create_mailbox_account")
    @patch("mailops.admin.MailboxUserAdmin.save_related", side_effect=RuntimeError("database failed"))
    def test_user_admin_later_failure_attempts_mailbox_cleanup(self, save_related, create_mailbox, delete_mailbox):
        response = self.post_add_user(follow=True)

        self.assertEqual(response.status_code, 200)
        self.assertFalse(get_user_model().objects.filter(email="user@example.com").exists())
        create_mailbox.assert_called_once_with("user@example.com", "mail-secret")
        delete_mailbox.assert_called_once_with("user@example.com")

    @patch("mailops.admin.create_mailbox_account")
    def test_user_admin_rejects_duplicate_email(self, create_mailbox):
        get_user_model().objects.create_user(username="existing", email="user@example.com")

        response = self.post_add_user()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_user_model().objects.filter(email__iexact="user@example.com").count(), 1)
        create_mailbox.assert_not_called()

    @override_settings(MAILBOX_AUTO_CREATE_FROM_USER_ADMIN=False)
    @patch("mailops.admin.create_mailbox_account")
    def test_feature_flag_disabled_preserves_user_create_without_mailbox(self, create_mailbox):
        response = self.post_add_user(email="", username="plain-user")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(get_user_model().objects.filter(username="plain-user").exists())
        create_mailbox.assert_not_called()

    def test_user_admin_change_blocks_non_staff_email_change(self):
        from .admin import MailboxUserChangeForm

        user = get_user_model().objects.create_user(username="mailbox-user", email="mailbox@example.com", password="secret")
        form = MailboxUserChangeForm(
            data={
                "username": "mailbox-user",
                "email": "other@example.com",
                "password": user.password,
                "is_active": "on",
            },
            instance=user,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("Email changes for mailbox-backed users are blocked in v1.", form.errors["email"])


@override_settings(MAILBOX_CREDENTIAL_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY)
class MailApiTests(TestCase):
    def setUp(self):
        self.account_email = "user@example.com"
        self.password = "mail-secret"

    def auth_headers(self):
        token = create_mailbox_token(self.account_email, self.password)
        return {"HTTP_AUTHORIZATION": f"Token {token.key}"}

    @patch("mailops.api.MailboxService")
    def test_login_success_returns_token(self, service_class):
        service_class.return_value.list_folders.return_value = [
            MailFolderSummary(name="INBOX", delimiter="/", flags=("HasNoChildren",)),
            MailFolderSummary(name="Sent", delimiter="/", flags=("Sent",)),
        ]

        response = self.client.post(
            reverse("mailops:api_login"),
            data={"email": "USER@Example.COM", "password": self.password},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["authenticated"], True)
        self.assertEqual(payload["account_email"], self.account_email)
        self.assertEqual(payload["folder_count"], 2)
        self.assertTrue(payload["token"])
        self.assertEqual(payload["user"]["email"], self.account_email)
        serialized_payload = json.dumps(payload)
        self.assertNotIn(self.password, serialized_payload)
        self.assertNotIn(ENCRYPTED_VALUE_PREFIX, serialized_payload)

        User = get_user_model()
        user = User.objects.get(username=self.account_email)
        self.assertEqual(user.email, self.account_email)
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertFalse(user.has_usable_password())
        token = Token.objects.get(user=user)
        self.assertEqual(payload["token"], token.key)
        credential = MailboxTokenCredential.objects.get(token=token)
        self.assertEqual(credential.mailbox_email, self.account_email)
        self.assertTrue(credential.mailbox_password.startswith(ENCRYPTED_VALUE_PREFIX))
        self.assertNotIn(self.password, credential.mailbox_password)
        self.assertEqual(credential.get_mailbox_password(), self.password)

    @patch("mailops.api.MailboxService")
    def test_login_reuses_token_and_updates_stored_password(self, service_class):
        service_class.return_value.list_folders.return_value = []
        first_response = self.client.post(
            reverse("mailops:api_login"),
            data={"email": self.account_email, "password": "old-password"},
            content_type="application/json",
        )
        second_response = self.client.post(
            reverse("mailops:api_login"),
            data={"email": self.account_email, "password": "new-password"},
            content_type="application/json",
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.json()["token"], second_response.json()["token"])
        self.assertEqual(Token.objects.count(), 1)
        credential = MailboxTokenCredential.objects.get()
        self.assertTrue(credential.mailbox_password.startswith(ENCRYPTED_VALUE_PREFIX))
        self.assertNotIn("new-password", credential.mailbox_password)
        self.assertEqual(credential.get_mailbox_password(), "new-password")

    @patch("mailops.api.MailboxService")
    def test_login_maps_bad_mailbox_credentials(self, service_class):
        service_class.return_value.list_folders.side_effect = MailAuthError("bad credentials")

        response = self.client.post(
            reverse("mailops:api_login"),
            data={"email": self.account_email, "password": "bad"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "mail_auth_failed")
        self.assertEqual(get_user_model().objects.count(), 0)
        self.assertEqual(Token.objects.count(), 0)
        self.assertEqual(MailboxTokenCredential.objects.count(), 0)

    def test_me_requires_token(self):
        response = self.client.get(reverse("mailops:api_me"))

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "not_authenticated")

    def test_me_returns_mailbox_identity(self):
        headers = self.auth_headers()

        response = self.client.get(reverse("mailops:api_me"), **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["authenticated"], True)
        self.assertEqual(payload["account_email"], self.account_email)
        self.assertEqual(payload["user"]["email"], self.account_email)
        serialized_payload = json.dumps(payload)
        self.assertNotIn(self.password, serialized_payload)
        self.assertNotIn(ENCRYPTED_VALUE_PREFIX, serialized_payload)

    def test_privacy_policy_is_public(self):
        response = self.client.get(reverse("mailops:privacy_policy"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pravila privatnosti")
        self.assertContains(response, "Finestar Mail")
        self.assertContains(response, "postmaster@finestar.hr")
        self.assertContains(response, "nije namijenjen djeci mlađoj od 13 godina")

    def test_mailbox_credentials_from_request_decrypts_password(self):
        token = create_mailbox_token(self.account_email, self.password)
        request = Mock(auth=token)

        credentials = mailbox_credentials_from_request(request)

        self.assertEqual(credentials.email, self.account_email)
        self.assertEqual(credentials.password, self.password)

    def test_legacy_plaintext_runtime_read_is_rejected(self):
        token = create_mailbox_token(self.account_email, self.password)
        MailboxTokenCredential.objects.filter(token=token).update(mailbox_password="legacy-plaintext")
        credential = MailboxTokenCredential.objects.get(token=token)

        with self.assertRaises(CredentialEncryptionError):
            credential.get_mailbox_password()

    def test_logout_revokes_current_token_and_mailbox_credentials(self):
        token = create_mailbox_token(self.account_email, self.password)
        DeviceRegistration.objects.create(
            account_email=self.account_email,
            fcm_token="token-1",
            platform=DeviceRegistration.PLATFORM_ANDROID,
            last_seen_at=timezone.now(),
        )
        headers = {"HTTP_AUTHORIZATION": f"Token {token.key}"}

        response = self.client.post(reverse("mailops:api_logout"), data={}, content_type="application/json", **headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"success": True})
        self.assertFalse(Token.objects.filter(pk=token.pk).exists())
        self.assertFalse(MailboxTokenCredential.objects.filter(token_id=token.pk).exists())

        user = get_user_model().objects.get(email=self.account_email)
        self.assertTrue(user.is_active)
        self.assertFalse(user.is_staff)
        self.assertTrue(DeviceRegistration.objects.get(fcm_token="token-1").enabled)

        me_response = self.client.get(reverse("mailops:api_me"), **headers)
        folders_response = self.client.get(reverse("mailops:api_mail_folders"), **headers)
        repeat_logout_response = self.client.post(reverse("mailops:api_logout"), data={}, content_type="application/json", **headers)

        self.assertEqual(me_response.status_code, 401)
        self.assertEqual(me_response.json()["error"], "not_authenticated")
        self.assertEqual(folders_response.status_code, 401)
        self.assertEqual(folders_response.json()["error"], "not_authenticated")
        self.assertEqual(repeat_logout_response.status_code, 401)
        self.assertEqual(repeat_logout_response.json()["error"], "not_authenticated")

    def test_logout_requires_token(self):
        missing_token = self.client.post(reverse("mailops:api_logout"), data={}, content_type="application/json")
        invalid_token = self.client.post(
            reverse("mailops:api_logout"),
            data={},
            content_type="application/json",
            HTTP_AUTHORIZATION="Token invalid",
        )

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(missing_token.json()["error"], "not_authenticated")
        self.assertEqual(invalid_token.status_code, 401)
        self.assertEqual(invalid_token.json()["error"], "not_authenticated")

    def test_credential_crypto_requires_valid_key(self):
        with override_settings(MAILBOX_CREDENTIAL_ENCRYPTION_KEY=""):
            with self.assertRaises(ImproperlyConfigured):
                encrypt_mailbox_password("secret")
        with override_settings(MAILBOX_CREDENTIAL_ENCRYPTION_KEY="not-a-fernet-key"):
            with self.assertRaises(ImproperlyConfigured):
                decrypt_mailbox_password(f"{ENCRYPTED_VALUE_PREFIX}bad")

    def test_gmail_import_account_encrypts_refresh_token_and_normalizes_email(self):
        account = GmailImportAccount(gmail_email=" USER@Gmail.COM ", target_mailbox_email=" TARGET@Example.COM ")
        account.set_refresh_token("refresh-secret")
        account.save()

        stored = GmailImportAccount.objects.get()
        self.assertEqual(stored.gmail_email, "user@gmail.com")
        self.assertEqual(stored.target_mailbox_email, "target@example.com")
        self.assertTrue(stored.refresh_token.startswith(ENCRYPTED_VALUE_PREFIX))
        self.assertNotIn("refresh-secret", stored.refresh_token)
        self.assertEqual(stored.get_refresh_token(), "refresh-secret")
        self.assertFalse(stored.delete_after_import)

    def test_user_scoped_gmail_import_account_matches_owner_email(self):
        user = get_user_model().objects.create_user(username="source", email=" Source@Example.COM ", password="secret")
        account = GmailImportAccount(user=user, gmail_email=" SOURCE@example.com ", target_mailbox_email=" source@example.com ")
        account.set_refresh_token("refresh-secret")
        account.save()

        stored = GmailImportAccount.objects.get()
        self.assertEqual(stored.user, user)
        self.assertEqual(stored.gmail_email, "source@example.com")
        self.assertEqual(stored.target_mailbox_email, "source@example.com")
        self.assertEqual(stored.get_refresh_token(), "refresh-secret")

    def test_user_scoped_gmail_import_account_rejects_mismatched_gmail_email(self):
        user = get_user_model().objects.create_user(username="source", email="source@example.com", password="secret")
        account = GmailImportAccount(user=user, gmail_email="other@example.com", target_mailbox_email="source@example.com")
        account.set_refresh_token("refresh-secret")

        with self.assertRaises(ValidationError) as ctx:
            account.save()

        self.assertIn("gmail_email", ctx.exception.message_dict)

    def test_user_scoped_gmail_import_account_rejects_mismatched_target_mailbox(self):
        user = get_user_model().objects.create_user(username="source", email="source@example.com", password="secret")
        account = GmailImportAccount(user=user, gmail_email="source@example.com", target_mailbox_email="target@example.com")
        account.set_refresh_token("refresh-secret")

        with self.assertRaises(ValidationError) as ctx:
            account.save()

        self.assertIn("target_mailbox_email", ctx.exception.message_dict)

    def test_legacy_gmail_import_account_can_remain_without_owner(self):
        account = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email="target@example.com")
        account.set_refresh_token("refresh-secret")
        account.save()

        stored = GmailImportAccount.objects.get()
        self.assertIsNone(stored.user)
        self.assertEqual(stored.gmail_email, "source@gmail.com")
        self.assertEqual(stored.target_mailbox_email, "target@example.com")

    def test_gmail_import_message_uses_gmail_id_as_unique_source_key(self):
        account = GmailImportAccount.objects.create(
            gmail_email="source@gmail.com",
            target_mailbox_email="target@example.com",
            refresh_token=encrypt_credential_value("refresh-secret"),
        )
        GmailImportMessage.objects.create(
            import_account=account,
            gmail_message_id=" gmail-1 ",
            gmail_thread_id=" thread-1 ",
            rfc_message_id="<same@example.com>",
            target_folder=" INBOX ",
            state=GmailImportMessage.STATE_COMMITTED,
            append_status=GmailImportMessage.STATUS_SUCCESS,
        )

        message = GmailImportMessage.objects.get()
        self.assertEqual(message.gmail_message_id, "gmail-1")
        self.assertEqual(message.gmail_thread_id, "thread-1")
        self.assertEqual(message.target_folder, "INBOX")
        self.assertEqual(message.cleanup_status, GmailImportMessage.STATUS_PENDING)
        with transaction.atomic():
            with self.assertRaises(ValidationError):
                GmailImportMessage.objects.create(import_account=account, gmail_message_id="gmail-1")

    def test_gmail_import_run_stores_operational_counters(self):
        account = GmailImportAccount.objects.create(
            gmail_email="source@gmail.com",
            target_mailbox_email="target@example.com",
            refresh_token=encrypt_credential_value("refresh-secret"),
        )
        run = GmailImportRun.objects.create(
            import_account=account,
            mode=GmailImportRun.MODE_HISTORICAL,
            status=GmailImportRun.STATUS_PARTIAL,
            scanned_count=10,
            appended_count=8,
            committed_count=8,
            cleaned_count=0,
            skipped_count=1,
            failed_count=1,
            error="one failed",
        )

        self.assertEqual(str(run).split(":")[0], "source@gmail.com")
        self.assertEqual(run.committed_count, 8)
        self.assertEqual(run.cleaned_count, 0)

    def test_gmail_import_admin_hides_refresh_token_field(self):
        account_admin = django_admin.site._registry[GmailImportAccount]

        self.assertIn("refresh_token", account_admin.exclude)
        self.assertIn("refresh_token_status", account_admin.readonly_fields)
        self.assertIn("user", account_admin.list_display)
        self.assertIn("user__email", account_admin.search_fields)

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="urn:ietf:wg:oauth:2.0:oob",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",),
    )
    @patch("mailops.management.commands.bootstrap_gmail_import_oauth.build_authorization_url", return_value="https://accounts.google.test/auth")
    def test_gmail_oauth_bootstrap_prints_consent_url_without_code(self, build_authorization_url):
        stdout = io.StringIO()

        call_command(
            "bootstrap_gmail_import_oauth",
            "--gmail",
            "source@gmail.com",
            "--target",
            "target@example.com",
            stdout=stdout,
        )

        self.assertIn("https://accounts.google.test/auth", stdout.getvalue())
        self.assertEqual(GmailImportAccount.objects.count(), 0)
        build_authorization_url.assert_called_once()

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="urn:ietf:wg:oauth:2.0:oob",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",),
    )
    @patch("mailops.management.commands.bootstrap_gmail_import_oauth.exchange_code_for_refresh_token", return_value="refresh-secret")
    def test_gmail_oauth_bootstrap_stores_encrypted_refresh_token(self, exchange_code):
        stdout = io.StringIO()

        call_command(
            "bootstrap_gmail_import_oauth",
            "--gmail",
            " SOURCE@Gmail.COM ",
            "--target",
            " TARGET@Example.COM ",
            "--code",
            "auth-code",
            stdout=stdout,
        )

        account = GmailImportAccount.objects.get()
        self.assertEqual(account.gmail_email, "source@gmail.com")
        self.assertEqual(account.target_mailbox_email, "target@example.com")
        self.assertTrue(account.refresh_token.startswith(ENCRYPTED_VALUE_PREFIX))
        self.assertNotIn("refresh-secret", account.refresh_token)
        self.assertEqual(account.get_refresh_token(), "refresh-secret")
        self.assertIn("Created Gmail import account", stdout.getvalue())
        exchange_code.assert_called_once()

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="https://mailadmin.example.com/oauth/gmail/callback",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",),
    )
    @patch("mailops.api.build_authorization_url", return_value="https://accounts.google.test/auth?state=signed")
    def test_gmail_connect_start_returns_signed_authorization_url(self, build_authorization_url):
        headers = self.auth_headers()

        response = self.client.post(reverse("mailops:api_gmail_connect_start"), data={}, content_type="application/json", **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["authorization_url"], "https://accounts.google.test/auth?state=signed")
        self.assertEqual(payload["account_email"], self.account_email)
        self.assertTrue(payload["state"])
        self.assertEqual(GmailImportAccount.objects.count(), 0)
        build_authorization_url.assert_called_once()

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="https://mailadmin.example.com/oauth/gmail/callback",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",),
    )
    @patch("mailops.api.fetch_gmail_profile_email", return_value="user@example.com")
    @patch("mailops.api.exchange_code_for_refresh_token", return_value="refresh-secret")
    def test_gmail_connect_complete_creates_user_scoped_account(self, exchange_code, fetch_profile_email):
        token = create_mailbox_token(self.account_email, self.password)
        oauth_state = signed_gmail_oauth_state(token.user)

        response = self.client.post(
            reverse("mailops:api_gmail_connect_complete"),
            data={"code": "auth-code", "state": oauth_state},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["connected"], True)
        self.assertEqual(payload["gmail_email"], self.account_email)
        self.assertEqual(payload["target_mailbox_email"], self.account_email)
        self.assertFalse(payload["delete_after_import"])
        account = GmailImportAccount.objects.get()
        self.assertEqual(account.user, token.user)
        self.assertEqual(account.gmail_email, self.account_email)
        self.assertEqual(account.target_mailbox_email, self.account_email)
        self.assertEqual(account.get_refresh_token(), "refresh-secret")
        exchange_code.assert_called_once()
        fetch_profile_email.assert_called_once()

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="https://mailadmin.example.com/oauth/gmail/callback",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",),
    )
    @patch("mailops.api.fetch_gmail_profile_email", return_value="other@example.com")
    @patch("mailops.api.exchange_code_for_refresh_token", return_value="refresh-secret")
    def test_gmail_connect_complete_rejects_mismatched_gmail_identity(self, exchange_code, fetch_profile_email):
        token = create_mailbox_token(self.account_email, self.password)
        oauth_state = signed_gmail_oauth_state(token.user)

        response = self.client.post(
            reverse("mailops:api_gmail_connect_complete"),
            data={"code": "auth-code", "state": oauth_state},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "gmail_identity_mismatch")
        self.assertEqual(GmailImportAccount.objects.count(), 0)
        exchange_code.assert_called_once()
        fetch_profile_email.assert_called_once()

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="https://mailadmin.example.com/oauth/gmail/callback",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",),
    )
    @patch("mailops.api.fetch_gmail_profile_email")
    @patch("mailops.api.exchange_code_for_refresh_token")
    def test_gmail_connect_complete_rejects_invalid_state(self, exchange_code, fetch_profile_email):
        headers = self.auth_headers()

        response = self.client.post(
            reverse("mailops:api_gmail_connect_complete"),
            data={"code": "auth-code", "state": "bad-state"},
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_oauth_state")
        exchange_code.assert_not_called()
        fetch_profile_email.assert_not_called()

    def test_gmail_connect_endpoints_require_authentication(self):
        start_response = self.client.post(reverse("mailops:api_gmail_connect_start"), data={}, content_type="application/json")
        complete_response = self.client.post(
            reverse("mailops:api_gmail_connect_complete"),
            data={"code": "auth-code", "state": "state"},
            content_type="application/json",
        )

        self.assertEqual(start_response.status_code, 401)
        self.assertEqual(start_response.json()["error"], "not_authenticated")
        self.assertEqual(complete_response.status_code, 401)
        self.assertEqual(complete_response.json()["error"], "not_authenticated")

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="https://mailadmin.example.com/oauth/gmail/callback",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",),
    )
    @patch("mailops.api.fetch_gmail_profile_email", return_value="user@example.com")
    @patch("mailops.api.exchange_code_for_refresh_token", return_value="new-refresh-secret")
    def test_gmail_connect_complete_updates_existing_user_account(self, exchange_code, fetch_profile_email):
        token = create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(user=token.user, gmail_email=self.account_email, target_mailbox_email=self.account_email)
        account.set_refresh_token("old-refresh-secret")
        account.save()
        oauth_state = signed_gmail_oauth_state(token.user)

        response = self.client.post(
            reverse("mailops:api_gmail_connect_complete"),
            data={"code": "auth-code", "state": oauth_state},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(GmailImportAccount.objects.count(), 1)
        account.refresh_from_db()
        self.assertEqual(account.get_refresh_token(), "new-refresh-secret")
        exchange_code.assert_called_once()
        fetch_profile_email.assert_called_once()

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="https://mailadmin.example.com/oauth/gmail/callback",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",),
    )
    @patch("mailops.api.fetch_gmail_profile_email", return_value="user@example.com")
    @patch("mailops.api.exchange_code_for_refresh_token", return_value="refresh-secret")
    def test_gmail_oauth_callback_connects_matching_user(self, exchange_code, fetch_profile_email):
        token = create_mailbox_token(self.account_email, self.password)
        oauth_state = signed_gmail_oauth_state(token.user)

        response = self.client.get(reverse("mailops:gmail_oauth_callback"), {"code": "auth-code", "state": oauth_state})

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Gmail connected", response.content)
        account = GmailImportAccount.objects.get()
        self.assertEqual(account.user, token.user)
        self.assertEqual(account.gmail_email, self.account_email)
        self.assertEqual(account.target_mailbox_email, self.account_email)
        self.assertEqual(account.get_refresh_token(), "refresh-secret")
        exchange_code.assert_called_once()
        fetch_profile_email.assert_called_once()

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="https://mailadmin.example.com/oauth/gmail/callback",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",),
    )
    @patch("mailops.api.fetch_gmail_profile_email", return_value="other@example.com")
    @patch("mailops.api.exchange_code_for_refresh_token", return_value="refresh-secret")
    def test_gmail_oauth_callback_rejects_mismatched_gmail_identity(self, exchange_code, fetch_profile_email):
        user = get_user_model().objects.create_user(username=self.account_email, email=self.account_email, password="secret")
        oauth_state = signed_gmail_oauth_state(user)

        response = self.client.get(reverse("mailops:gmail_oauth_callback"), {"code": "auth-code", "state": oauth_state})

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"Gmail connection rejected", response.content)
        self.assertEqual(GmailImportAccount.objects.count(), 0)
        exchange_code.assert_called_once()
        fetch_profile_email.assert_called_once()

    @patch("mailops.api.exchange_code_for_refresh_token")
    def test_gmail_oauth_callback_rejects_invalid_state(self, exchange_code):
        response = self.client.get(reverse("mailops:gmail_oauth_callback"), {"code": "auth-code", "state": "bad-state"})

        self.assertEqual(response.status_code, 400)
        self.assertIn(b"OAuth state is invalid or expired", response.content)
        exchange_code.assert_not_called()

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="https://mailadmin.example.com/oauth/gmail/callback",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",),
    )
    @patch("mailops.admin.build_authorization_url", return_value="https://accounts.google.test/auth")
    def test_admin_user_connect_gmail_redirects_to_google(self, build_authorization_url):
        admin_user = get_user_model().objects.create_superuser(username="admin", email="admin@example.com", password="secret")
        target_user = get_user_model().objects.create_user(username=self.account_email, email=self.account_email, password="secret")
        self.client.force_login(admin_user)

        response = self.client.get(reverse("admin:auth_user_connect_gmail", args=[target_user.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://accounts.google.test/auth")
        build_authorization_url.assert_called_once()

    def test_admin_user_connect_gmail_requires_staff_authentication(self):
        target_user = get_user_model().objects.create_user(username=self.account_email, email=self.account_email, password="secret")

        response = self.client.get(reverse("admin:auth_user_connect_gmail", args=[target_user.pk]))

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    def test_gmail_account_status_returns_disconnected_contract(self):
        response = self.client.get(reverse("mailops:api_gmail_account_status"), **self.auth_headers())

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["connected"], False)
        self.assertEqual(payload["provider"], "gmail")
        self.assertIsNone(payload["gmail_email"])
        self.assertIsNone(payload["target_mailbox_email"])

    def test_external_accounts_lists_only_authenticated_users_gmail_account(self):
        token = create_mailbox_token(self.account_email, self.password)
        other_token = create_mailbox_token("other@example.com", "other-secret")
        account = GmailImportAccount(
            user=token.user,
            gmail_email=self.account_email,
            target_mailbox_email=self.account_email,
            last_success_at=timezone.now(),
            historical_import_completed_at=timezone.now(),
        )
        account.set_refresh_token("refresh-secret")
        account.save()
        other_account = GmailImportAccount(user=other_token.user, gmail_email="other@example.com", target_mailbox_email="other@example.com")
        other_account.set_refresh_token("other-refresh-secret")
        other_account.save()

        response = self.client.get(reverse("mailops:api_external_accounts"), HTTP_AUTHORIZATION=f"Token {token.key}")

        self.assertEqual(response.status_code, 200)
        accounts = response.json()["accounts"]
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["provider"], "gmail")
        self.assertEqual(accounts[0]["gmail_email"], self.account_email)
        self.assertEqual(accounts[0]["target_mailbox_email"], self.account_email)
        self.assertTrue(accounts[0]["historical_import_completed"])
        self.assertNotIn("other@example.com", json.dumps(accounts))

    def test_gmail_disconnect_removes_only_authenticated_users_account(self):
        token = create_mailbox_token(self.account_email, self.password)
        other_token = create_mailbox_token("other@example.com", "other-secret")
        account = GmailImportAccount(user=token.user, gmail_email=self.account_email, target_mailbox_email=self.account_email)
        account.set_refresh_token("refresh-secret")
        account.save()
        other_account = GmailImportAccount(user=other_token.user, gmail_email="other@example.com", target_mailbox_email="other@example.com")
        other_account.set_refresh_token("other-refresh-secret")
        other_account.save()

        response = self.client.post(reverse("mailops:api_gmail_disconnect"), data={}, content_type="application/json", HTTP_AUTHORIZATION=f"Token {token.key}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"disconnected": True, "provider": "gmail"})
        self.assertFalse(GmailImportAccount.objects.filter(pk=account.pk).exists())
        self.assertTrue(GmailImportAccount.objects.filter(pk=other_account.pk).exists())

    @patch("mailops.api.GmailImportService")
    def test_gmail_sync_trigger_runs_historical_until_completed(self, service_class):
        token = create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(user=token.user, gmail_email=self.account_email, target_mailbox_email=self.account_email)
        account.set_refresh_token("refresh-secret")
        account.save()
        service_class.return_value.run_historical_import_for_user.return_value = Mock(
            scanned=2,
            appended=1,
            committed=1,
            cleaned=0,
            skipped=1,
            failed=0,
        )

        response = self.client.post(
            reverse("mailops:api_gmail_sync"),
            data={"mode": "auto", "limit": 2, "since": "2026/04/01", "no_delete": True},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "historical")
        self.assertEqual(payload["scanned"], 2)
        service_class.return_value.run_historical_import_for_user.assert_called_once_with(
            token.user,
            limit=2,
            since="2026/04/01",
            dry_run=False,
            no_delete=True,
        )
        service_class.return_value.run_incremental_import_for_user.assert_not_called()

    @patch("mailops.api.GmailImportService")
    def test_gmail_sync_trigger_runs_incremental_after_historical_completion(self, service_class):
        token = create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(
            user=token.user,
            gmail_email=self.account_email,
            target_mailbox_email=self.account_email,
            historical_import_completed_at=timezone.now(),
        )
        account.set_refresh_token("refresh-secret")
        account.save()
        service_class.return_value.run_incremental_import_for_user.return_value = Mock(
            scanned=3,
            appended=2,
            committed=2,
            cleaned=0,
            skipped=1,
            failed=0,
        )

        response = self.client.post(
            reverse("mailops:api_gmail_sync"),
            data={"mode": "auto", "limit": 3},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {token.key}",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["mode"], "incremental")
        self.assertEqual(payload["committed"], 2)
        service_class.return_value.run_incremental_import_for_user.assert_called_once_with(token.user, limit=3, no_delete=False)
        service_class.return_value.run_historical_import_for_user.assert_not_called()

    def test_gmail_sync_trigger_requires_connected_account(self):
        response = self.client.post(reverse("mailops:api_gmail_sync"), data={}, content_type="application/json", **self.auth_headers())

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "gmail_account_not_connected")

    def test_gmail_historical_import_dry_run_does_not_mutate_import_state(self):
        account = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email=self.account_email)
        account.set_refresh_token("refresh-secret")
        account.save()
        gmail_client = FakeGmailClient(refs=(GmailMessageRef(gmail_message_id="gmail-1"),))

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: FakeImapClient(),
        ).run_historical_import("source@gmail.com", self.account_email, limit=10, dry_run=True)

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.committed, 0)
        self.assertEqual(GmailImportRun.objects.count(), 0)
        self.assertEqual(GmailImportMessage.objects.count(), 0)
        self.assertEqual(gmail_client.deleted, [])
        self.assertIn("-in:drafts -in:spam -in:trash", gmail_client.list_calls[0]["query"])

    def test_gmail_historical_import_appends_then_commits_without_default_delete(self):
        token = create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email=self.account_email)
        account.set_refresh_token("refresh-secret")
        account.save()
        MailAccountIndex.objects.create(
            user=token.user,
            account_email=self.account_email,
            index_status=MailAccountIndex.STATUS_READY,
            last_indexed_at=timezone.now(),
        )
        events = []
        gmail_client = FakeGmailClient(
            refs=(GmailMessageRef(gmail_message_id="gmail-1", gmail_thread_id="thread-1"),),
            raw_messages={
                "gmail-1": GmailRawMessage(
                    gmail_message_id="gmail-1",
                    gmail_thread_id="thread-1",
                    history_id="7",
                    label_ids=("INBOX",),
                    raw_bytes=b"Message-ID: <one@example.com>\r\n\r\nBody",
                    rfc_message_id="<one@example.com>",
                )
            },
            events=events,
        )
        imap_client = FakeImapClient(events=events)

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: imap_client,
        ).run_historical_import("source@gmail.com", self.account_email, limit=10)

        self.assertEqual(result.appended, 1)
        self.assertEqual(result.committed, 1)
        self.assertEqual(result.cleaned, 0)
        self.assertEqual(events, ["login:user@example.com", "fetch:gmail-1", "append:INBOX"])
        message = GmailImportMessage.objects.get()
        self.assertEqual(message.state, GmailImportMessage.STATE_COMMITTED)
        self.assertEqual(message.append_status, GmailImportMessage.STATUS_SUCCESS)
        self.assertEqual(message.cleanup_status, GmailImportMessage.STATUS_PENDING)
        self.assertEqual(message.target_folder, "INBOX")
        self.assertEqual(message.rfc_message_id, "<one@example.com>")
        self.assertEqual(gmail_client.deleted, [])
        self.assertIsNone(MailAccountIndex.objects.get(account_email=self.account_email).last_indexed_at)
        run = GmailImportRun.objects.get()
        self.assertEqual(run.status, GmailImportRun.STATUS_SUCCESS)
        self.assertEqual(run.committed_count, 1)

    def test_user_scoped_gmail_historical_import_resolves_owner_mailbox(self):
        token = create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(user=token.user, gmail_email=self.account_email, target_mailbox_email=self.account_email)
        account.set_refresh_token("refresh-secret")
        account.save()
        events = []
        gmail_client = FakeGmailClient(
            refs=(GmailMessageRef(gmail_message_id="gmail-1"),),
            raw_messages={
                "gmail-1": GmailRawMessage(
                    gmail_message_id="gmail-1",
                    gmail_thread_id="thread-1",
                    history_id="7",
                    label_ids=("INBOX",),
                    raw_bytes=b"Message-ID: <one@example.com>\r\n\r\nBody",
                    rfc_message_id="<one@example.com>",
                )
            },
            events=events,
        )

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: FakeImapClient(events=events),
        ).run_historical_import_for_user(token.user, limit=10)

        self.assertEqual(result.committed, 1)
        self.assertEqual(events, ["login:user@example.com", "fetch:gmail-1", "append:INBOX"])
        self.assertEqual(GmailImportRun.objects.get().import_account, account)

    def test_user_scoped_gmail_import_rejects_other_user_without_connected_account(self):
        owner_token = create_mailbox_token(self.account_email, self.password)
        other_user = get_user_model().objects.create_user(username="other@example.com", email="other@example.com", password="secret")
        account = GmailImportAccount(user=owner_token.user, gmail_email=self.account_email, target_mailbox_email=self.account_email)
        account.set_refresh_token("refresh-secret")
        account.save()

        with self.assertRaisesRegex(GmailImportError, "No Gmail import account connected for other@example.com"):
            GmailImportService(
                gmail_client_factory=lambda refresh_token: FakeGmailClient(),
                imap_client_factory=lambda: FakeImapClient(),
            ).run_historical_import_for_user(other_user, limit=10)

    def test_user_scoped_gmail_import_rejects_mailbox_credentials_owned_by_another_user(self):
        owner = get_user_model().objects.create_user(username="user@example.com", email=self.account_email, password="secret")
        other_user = get_user_model().objects.create_user(username="other@example.com", email="other@example.com", password="secret")
        other_token = Token.objects.create(user=other_user)
        credential = MailboxTokenCredential(token=other_token, mailbox_email=self.account_email)
        credential.set_mailbox_password(self.password)
        credential.save()
        account = GmailImportAccount(user=owner, gmail_email=self.account_email, target_mailbox_email=self.account_email)
        account.set_refresh_token("refresh-secret")
        account.save()

        with self.assertRaisesRegex(GmailImportError, "not owned by the Gmail import account user"):
            GmailImportService(
                gmail_client_factory=lambda refresh_token: FakeGmailClient(refs=(GmailMessageRef(gmail_message_id="gmail-1"),)),
                imap_client_factory=lambda: FakeImapClient(),
            ).run_historical_import_for_user(owner, limit=10)

    @override_settings(GMAIL_IMPORT_OAUTH_SCOPES=("https://mail.google.com/",))
    def test_gmail_historical_import_deletes_only_after_commit_when_enabled(self):
        create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email=self.account_email, delete_after_import=True)
        account.set_refresh_token("refresh-secret")
        account.save()
        events = []
        gmail_client = FakeGmailClient(
            refs=(GmailMessageRef(gmail_message_id="gmail-1"),),
            raw_messages={
                "gmail-1": GmailRawMessage(
                    gmail_message_id="gmail-1",
                    gmail_thread_id="thread-1",
                    history_id="7",
                    label_ids=("SENT",),
                    raw_bytes=b"Message-ID: <sent@example.com>\r\n\r\nBody",
                    rfc_message_id="<sent@example.com>",
                )
            },
            events=events,
        )

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: FakeImapClient(events=events, sent_folder="Sent"),
        ).run_historical_import("source@gmail.com", self.account_email, limit=10)

        self.assertEqual(result.cleaned, 1)
        self.assertEqual(events, ["login:user@example.com", "fetch:gmail-1", "append:Sent", "delete:gmail-1"])
        message = GmailImportMessage.objects.get()
        self.assertEqual(message.state, GmailImportMessage.STATE_CLEANED)
        self.assertEqual(message.cleanup_status, GmailImportMessage.STATUS_SUCCESS)
        self.assertEqual(message.target_folder, "Sent")

    @override_settings(GMAIL_IMPORT_OAUTH_SCOPES=("https://mail.google.com/",))
    def test_gmail_historical_import_does_not_delete_when_append_fails(self):
        create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email=self.account_email, delete_after_import=True)
        account.set_refresh_token("refresh-secret")
        account.save()
        events = []
        gmail_client = FakeGmailClient(
            refs=(GmailMessageRef(gmail_message_id="gmail-1"),),
            raw_messages={
                "gmail-1": GmailRawMessage(
                    gmail_message_id="gmail-1",
                    gmail_thread_id="thread-1",
                    history_id="7",
                    label_ids=("INBOX",),
                    raw_bytes=b"Message-ID: <one@example.com>\r\n\r\nBody",
                    rfc_message_id="<one@example.com>",
                )
            },
            events=events,
        )

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: FakeImapClient(events=events, append_error=MailConnectionError("append failed")),
        ).run_historical_import("source@gmail.com", self.account_email, limit=10)

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.cleaned, 0)
        self.assertEqual(events, ["login:user@example.com", "fetch:gmail-1", "append:INBOX"])
        self.assertEqual(gmail_client.deleted, [])
        message = GmailImportMessage.objects.get()
        self.assertEqual(message.state, GmailImportMessage.STATE_FAILED)
        self.assertEqual(message.append_status, GmailImportMessage.STATUS_FAILED)
        self.assertIsNone(message.committed_at)

    @override_settings(GMAIL_IMPORT_OAUTH_SCOPES=("https://www.googleapis.com/auth/gmail.modify",))
    def test_gmail_cleanup_requires_permanent_delete_scope(self):
        create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email=self.account_email, delete_after_import=True)
        account.set_refresh_token("refresh-secret")
        account.save()

        with self.assertRaisesRegex(GmailImportError, "Gmail permanent cleanup requires"):
            GmailImportService(
                gmail_client_factory=lambda refresh_token: FakeGmailClient(refs=(GmailMessageRef(gmail_message_id="gmail-1"),)),
                imap_client_factory=lambda: FakeImapClient(),
            ).run_historical_import("source@gmail.com", self.account_email, limit=10)

        self.assertFalse(GmailImportMessage.objects.exists())
        run = GmailImportRun.objects.get(import_account=account)
        self.assertEqual(run.status, GmailImportRun.STATUS_FAILED)
        account.refresh_from_db()
        self.assertIn("https://mail.google.com/", account.last_error)

    @override_settings(GMAIL_IMPORT_OAUTH_SCOPES=("https://mail.google.com/",))
    def test_gmail_historical_import_recovers_appended_record_without_duplicate_append(self):
        create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email=self.account_email, delete_after_import=True)
        account.set_refresh_token("refresh-secret")
        account.save()
        GmailImportMessage.objects.create(
            import_account=account,
            gmail_message_id="gmail-1",
            state=GmailImportMessage.STATE_APPENDED,
            append_status=GmailImportMessage.STATUS_SUCCESS,
            target_folder="INBOX",
            appended_at=timezone.now(),
        )
        events = []
        gmail_client = FakeGmailClient(refs=(GmailMessageRef(gmail_message_id="gmail-1"),), events=events)
        imap_client = FakeImapClient(events=events)

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: imap_client,
        ).run_historical_import("source@gmail.com", self.account_email, limit=10)

        self.assertEqual(result.appended, 0)
        self.assertEqual(result.committed, 1)
        self.assertEqual(result.cleaned, 1)
        self.assertEqual(events, ["login:user@example.com", "delete:gmail-1"])
        message = GmailImportMessage.objects.get()
        self.assertEqual(message.state, GmailImportMessage.STATE_CLEANED)

    @patch("mailops.management.commands.run_gmail_import.GmailImportService")
    def test_run_gmail_import_command_prints_summary(self, service_class):
        service_class.return_value.run_historical_import.return_value = Mock(
            scanned=2,
            appended=1,
            committed=1,
            cleaned=0,
            skipped=1,
            failed=0,
        )
        stdout = io.StringIO()

        call_command(
            "run_gmail_import",
            "--account",
            "source@gmail.com",
            "--target",
            self.account_email,
            "--limit",
            "2",
            "--dry-run",
            "--no-delete",
            stdout=stdout,
        )

        self.assertIn("scanned=2 appended=1 committed=1 cleaned=0 skipped=1 failed=0", stdout.getvalue())
        service_class.return_value.run_historical_import.assert_called_once_with(
            gmail_email="source@gmail.com",
            target_mailbox_email=self.account_email,
            limit=2,
            since="",
            dry_run=True,
            no_delete=True,
        )

    def test_gmail_incremental_import_uses_history_and_advances_cursor_after_success(self):
        create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email=self.account_email, last_history_id="10")
        account.set_refresh_token("refresh-secret")
        account.historical_import_completed_at = timezone.now()
        account.save()
        gmail_client = FakeGmailClient(
            history_pages=(
                GmailHistoryPage(
                    history_id="12",
                    messages_added=(
                        GmailHistoryMessage(gmail_message_id="gmail-2", gmail_thread_id="thread-2", history_id="11"),
                    ),
                ),
            ),
            raw_messages={
                "gmail-2": GmailRawMessage(
                    gmail_message_id="gmail-2",
                    gmail_thread_id="thread-2",
                    history_id="11",
                    label_ids=("INBOX",),
                    raw_bytes=b"Message-ID: <two@example.com>\r\n\r\nBody",
                    rfc_message_id="<two@example.com>",
                )
            },
        )

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: FakeImapClient(),
        ).run_incremental_import("source@gmail.com", self.account_email, limit=10)

        self.assertEqual(result.committed, 1)
        self.assertEqual(result.history_id, "12")
        account.refresh_from_db()
        self.assertEqual(account.last_history_id, "12")
        self.assertEqual(account.consecutive_failures, 0)
        run = GmailImportRun.objects.get()
        self.assertEqual(run.mode, GmailImportRun.MODE_INCREMENTAL)
        self.assertEqual(run.status, GmailImportRun.STATUS_SUCCESS)
        self.assertEqual(gmail_client.history_calls[0]["start_history_id"], "10")
        self.assertEqual(gmail_client.list_calls, [])

    def test_user_scoped_gmail_incremental_import_uses_owner_account(self):
        token = create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(user=token.user, gmail_email=self.account_email, target_mailbox_email=self.account_email, last_history_id="10")
        account.set_refresh_token("refresh-secret")
        account.historical_import_completed_at = timezone.now()
        account.save()
        gmail_client = FakeGmailClient(history_pages=(GmailHistoryPage(history_id="10"),))

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: FakeImapClient(),
        ).run_incremental_import_for_user(token.user, limit=10)

        self.assertEqual(result.scanned, 0)
        self.assertEqual(GmailImportRun.objects.get().import_account, account)
        self.assertEqual(gmail_client.history_calls[0]["start_history_id"], "10")

    def test_gmail_incremental_import_does_not_advance_cursor_on_partial_failure(self):
        create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email=self.account_email, last_history_id="10")
        account.set_refresh_token("refresh-secret")
        account.historical_import_completed_at = timezone.now()
        account.save()
        gmail_client = FakeGmailClient(
            history_pages=(
                GmailHistoryPage(
                    history_id="12",
                    messages_added=(GmailHistoryMessage(gmail_message_id="gmail-2", gmail_thread_id="thread-2", history_id="11"),),
                ),
            ),
            raw_messages={
                "gmail-2": GmailRawMessage(
                    gmail_message_id="gmail-2",
                    gmail_thread_id="thread-2",
                    history_id="11",
                    label_ids=("INBOX",),
                    raw_bytes=b"Message-ID: <two@example.com>\r\n\r\nBody",
                    rfc_message_id="<two@example.com>",
                )
            },
        )

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: FakeImapClient(append_error=MailConnectionError("append failed")),
        ).run_incremental_import("source@gmail.com", self.account_email, limit=10)

        self.assertEqual(result.failed, 1)
        account.refresh_from_db()
        self.assertEqual(account.last_history_id, "10")
        self.assertIn("failed", account.last_error)
        self.assertEqual(GmailImportRun.objects.get().status, GmailImportRun.STATUS_PARTIAL)

    def test_gmail_incremental_import_falls_back_to_recent_rescan_when_history_unavailable(self):
        create_mailbox_token(self.account_email, self.password)
        account = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email=self.account_email, last_history_id="expired")
        account.set_refresh_token("refresh-secret")
        account.historical_import_completed_at = timezone.now()
        account.save()
        gmail_client = FakeGmailClient(
            refs=(GmailMessageRef(gmail_message_id="gmail-3"),),
            history_error=MailConnectionError("Gmail API returned HTTP 404"),
            raw_messages={
                "gmail-3": GmailRawMessage(
                    gmail_message_id="gmail-3",
                    gmail_thread_id="thread-3",
                    history_id="21",
                    label_ids=("INBOX",),
                    raw_bytes=b"Message-ID: <three@example.com>\r\n\r\nBody",
                    rfc_message_id="<three@example.com>",
                )
            },
        )

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: FakeImapClient(),
        ).run_incremental_import("source@gmail.com", self.account_email, limit=10)

        self.assertEqual(result.committed, 1)
        self.assertEqual(gmail_client.history_calls[0]["start_history_id"], "expired")
        self.assertIn("in:anywhere", gmail_client.list_calls[0]["query"])
        account.refresh_from_db()
        self.assertEqual(account.last_history_id, "21")

    def test_gmail_incremental_cycle_selects_completed_historical_accounts(self):
        create_mailbox_token(self.account_email, self.password)
        ready = GmailImportAccount(gmail_email="source@gmail.com", target_mailbox_email=self.account_email, last_history_id="10")
        ready.set_refresh_token("refresh-secret")
        ready.historical_import_completed_at = timezone.now()
        ready.save()
        incomplete = GmailImportAccount(gmail_email="other@gmail.com", target_mailbox_email="other@example.com")
        incomplete.set_refresh_token("refresh-secret")
        incomplete.save()
        gmail_client = FakeGmailClient(history_pages=(GmailHistoryPage(history_id="10"),))

        result = GmailImportService(
            gmail_client_factory=lambda refresh_token: gmail_client,
            imap_client_factory=lambda: FakeImapClient(),
        ).run_incremental_cycle(limit=10, max_accounts=10)

        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.selected, 1)
        self.assertEqual(result.synced, 1)
        self.assertEqual(result.failed, 0)

    @patch("mailops.management.commands.run_gmail_import.GmailImportService")
    def test_run_gmail_import_command_runs_incremental_cycle(self, service_class):
        service_class.return_value.run_incremental_cycle.return_value = Mock(scanned=2, selected=1, synced=1, failed=0, skipped=0)
        stdout = io.StringIO()

        call_command(
            "run_gmail_import",
            "--incremental",
            "--all",
            "--limit",
            "5",
            "--max-accounts",
            "2",
            "--no-delete",
            stdout=stdout,
        )

        self.assertIn("scanned=2 selected=1 synced=1 failed=0 skipped=0", stdout.getvalue())
        service_class.return_value.run_incremental_cycle.assert_called_once_with(limit=5, max_accounts=2, no_delete=True)

    def test_legacy_plaintext_migration_encrypts_existing_rows(self):
        migration = importlib.import_module("mailops.migrations.0004_encrypt_mailbox_token_credentials")
        user = get_user_model().objects.create_user(username="legacy@example.com", email="legacy@example.com")
        token = Token.objects.create(user=user)
        encrypted_user = get_user_model().objects.create_user(username="encrypted@example.com", email="encrypted@example.com")
        encrypted_token = Token.objects.create(user=encrypted_user)
        MailboxTokenCredential.objects.create(
            token=token,
            mailbox_email="legacy@example.com",
            mailbox_password="legacy-secret",
        )
        already_encrypted = encrypt_mailbox_password("already-secret")
        MailboxTokenCredential.objects.create(
            token=encrypted_token,
            mailbox_email="encrypted@example.com",
            mailbox_password=already_encrypted,
        )

        migration.encrypt_legacy_mailbox_passwords(apps, None)
        migration.encrypt_legacy_mailbox_passwords(apps, None)

        legacy_credential = MailboxTokenCredential.objects.get(token=token)
        encrypted_credential = MailboxTokenCredential.objects.get(token=encrypted_token)
        self.assertTrue(legacy_credential.mailbox_password.startswith(ENCRYPTED_VALUE_PREFIX))
        self.assertNotIn("legacy-secret", legacy_credential.mailbox_password)
        self.assertEqual(legacy_credential.get_mailbox_password(), "legacy-secret")
        self.assertEqual(encrypted_credential.mailbox_password, already_encrypted)

    def test_legacy_plaintext_migration_requires_encryption_key(self):
        migration = importlib.import_module("mailops.migrations.0004_encrypt_mailbox_token_credentials")

        with override_settings(MAILBOX_CREDENTIAL_ENCRYPTION_KEY=""):
            with self.assertRaises(ImproperlyConfigured):
                migration.encrypt_legacy_mailbox_passwords(apps, None)

    def test_mail_endpoint_rejects_invalid_token(self):
        response = self.client.get(reverse("mailops:api_me"), HTTP_AUTHORIZATION="Token invalid")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "not_authenticated")

    def test_mail_endpoint_rejects_token_without_mailbox_credentials(self):
        user = get_user_model().objects.create_user(username="empty@example.com", email="empty@example.com")
        token = Token.objects.create(user=user)

        response = self.client.get(reverse("mailops:api_me"), HTTP_AUTHORIZATION=f"Token {token.key}")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "mailbox_credentials_missing")

    @patch("mailops.api.MailboxService")
    def test_mail_folders_returns_service_results(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.list_folders.return_value = [
            MailFolderSummary(name="INBOX", delimiter="/", flags=("HasNoChildren",)),
        ]

        response = self.client.get(reverse("mailops:api_mail_folders"), **headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["folders"][0],
            {
                "name": "INBOX",
                "path": "INBOX",
                "display_name": "INBOX",
                "parent_path": None,
                "depth": 0,
                "delimiter": "/",
                "flags": ["HasNoChildren"],
                "selectable": True,
            },
        )
        credentials = service_class.return_value.list_folders.call_args.args[0]
        self.assertEqual(credentials.email, self.account_email)
        self.assertEqual(credentials.password, self.password)

    @patch("mailops.api.MailboxService")
    def test_mail_folders_returns_nested_folder_metadata(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.list_folders.return_value = [
            MailFolderSummary(name="INBOX", delimiter="/", flags=("HasChildren",)),
            MailFolderSummary(name="INBOX/Invoices", delimiter="/", flags=("HasChildren",)),
            MailFolderSummary(name="INBOX/Invoices/2026", delimiter="/", flags=("HasNoChildren",)),
            MailFolderSummary(name="Archive", delimiter="/", flags=("Noselect",)),
        ]

        response = self.client.get(reverse("mailops:api_mail_folders"), **headers)

        self.assertEqual(response.status_code, 200)
        folders = response.json()["folders"]
        self.assertEqual(folders[2]["name"], "INBOX/Invoices/2026")
        self.assertEqual(folders[2]["path"], "INBOX/Invoices/2026")
        self.assertEqual(folders[2]["display_name"], "2026")
        self.assertEqual(folders[2]["parent_path"], "INBOX/Invoices")
        self.assertEqual(folders[2]["depth"], 2)
        self.assertTrue(folders[2]["selectable"])
        self.assertFalse(folders[3]["selectable"])

    def test_legacy_mailbox_path_no_longer_accepts_post_password_payload(self):
        response = self.client.post(
            "/api/mail/messages/",
            data={"accountEmail": self.account_email, "password": self.password},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 404)

    @patch("mailops.api.MailboxService")
    def test_mail_messages_returns_service_results(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        service.list_message_summary_page.return_value = MailMessageSummaryPage(
            messages=(
                MailMessageSummary(
                    uid="42",
                    folder="INBOX",
                    subject="Hello",
                    sender="Sender <sender@example.com>",
                    to=("user@example.com",),
                    cc=("copy@example.com",),
                    date=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
                    message_id="<m1@example.com>",
                    flags=("Seen",),
                    size=1234,
                    has_attachments=True,
                    has_visible_attachments=True,
                ),
            ),
            has_more=True,
            next_before_uid="42",
        )

        response = self.client.get(reverse("mailops:api_mail_messages"), {"folder": "INBOX", "limit": 25, "before_uid": "50"}, **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["account_email"], self.account_email)
        self.assertEqual(payload["messages"][0]["uid"], "42")
        self.assertTrue(payload["messages"][0]["has_attachments"])
        self.assertTrue(payload["messages"][0]["has_visible_attachments"])
        self.assertEqual(payload["has_more"], True)
        self.assertEqual(payload["next_before_uid"], "42")
        self.assertIn("2026-04-16T07:00:00", payload["messages"][0]["date"])
        credentials = service.list_message_summary_page.call_args.args[0]
        self.assertEqual(credentials.email, self.account_email)
        self.assertEqual(credentials.password, self.password)
        self.assertEqual(service.list_message_summary_page.call_args.kwargs, {"folder": "INBOX", "limit": 25, "before_uid": "50"})

    def test_mail_messages_requires_token_and_validates_limit(self):
        missing_token = self.client.get(reverse("mailops:api_mail_messages"))
        headers = self.auth_headers()
        invalid_limit = self.client.get(reverse("mailops:api_mail_messages"), {"limit": 500}, **headers)
        invalid_before_uid = self.client.get(reverse("mailops:api_mail_messages"), {"before_uid": "abc"}, **headers)

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(missing_token.json()["error"], "not_authenticated")
        self.assertEqual(invalid_limit.status_code, 400)
        self.assertEqual(invalid_limit.json()["error"], "invalid_limit")
        self.assertEqual(invalid_before_uid.status_code, 400)
        self.assertEqual(invalid_before_uid.json()["error"], "invalid_before_uid")

    @patch("mailops.api.MailboxService")
    def test_mail_messages_maps_mail_errors(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.list_message_summary_page.side_effect = MailAuthError("bad credentials")

        response = self.client.get(reverse("mailops:api_mail_messages"), **headers)

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "mail_auth_failed")

    @patch("mailops.api.MailboxService")
    def test_mail_conversations_returns_structured_participants_and_aggregate_attachments(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        root = MailMessageSummary(
            uid="42",
            folder="INBOX",
            subject="Hello",
            sender="Sender Name <sender@example.com>",
            to=("user@example.com",),
            cc=(),
            date=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
            message_id="<m1@example.com>",
            flags=("Seen",),
            size=1234,
            has_attachments=False,
            has_visible_attachments=False,
        )
        reply = MailMessageSummary(
            uid="43",
            folder="INBOX",
            subject="Re: Hello",
            sender="Reply Person <reply@example.com>",
            to=("sender@example.com",),
            cc=(),
            date=datetime(2026, 4, 16, 8, 0, tzinfo=dt_timezone.utc),
            message_id="<m2@example.com>",
            flags=(),
            size=2345,
            has_attachments=True,
            has_visible_attachments=True,
        )
        service.list_conversations.return_value = MailConversationSummaryPage(
            conversations=(
                MailConversationSummary(
                    conversation_id="thread-1",
                    message_count=2,
                    reply_count=1,
                    has_unread=True,
                    has_attachments=True,
                    has_visible_attachments=True,
                    participants=(
                        MailConversationParticipant(name="Sender Name", email="sender@example.com"),
                        MailConversationParticipant(name="", email="user@example.com"),
                    ),
                    root_message=root,
                    replies=(reply,),
                    latest_date=datetime(2026, 4, 16, 8, 0, tzinfo=dt_timezone.utc),
                ),
            )
        )

        response = self.client.get(reverse("mailops:api_mail_conversations"), {"folder": "INBOX", "limit": 25}, **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        conversation = payload["conversations"][0]
        self.assertEqual(payload["account_email"], self.account_email)
        self.assertEqual(conversation["conversation_id"], "thread-1")
        self.assertEqual(conversation["message_count"], 2)
        self.assertEqual(conversation["reply_count"], 1)
        self.assertTrue(conversation["has_unread"])
        self.assertTrue(conversation["has_attachments"])
        self.assertTrue(conversation["has_visible_attachments"])
        self.assertEqual(conversation["participants"], [{"name": "Sender Name", "email": "sender@example.com"}, {"name": "", "email": "user@example.com"}])
        self.assertEqual(conversation["root_message"]["uid"], "42")
        self.assertEqual(conversation["replies"][0]["uid"], "43")
        self.assertTrue(conversation["replies"][0]["has_attachments"])
        self.assertIn("2026-04-16T08:00:00", conversation["latest_date"])
        self.assertEqual(service.list_conversations.call_args.kwargs, {"folder": "INBOX", "limit": 25})

    def test_mail_conversations_requires_token_and_validates_limit(self):
        missing_token = self.client.get(reverse("mailops:api_mail_conversations"))
        headers = self.auth_headers()
        invalid_limit = self.client.get(reverse("mailops:api_mail_conversations"), {"limit": 500}, **headers)

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(missing_token.json()["error"], "not_authenticated")
        self.assertEqual(invalid_limit.status_code, 400)
        self.assertEqual(invalid_limit.json()["error"], "invalid_limit")

    @patch("mailops.api.MailboxService")
    def test_mail_conversations_maps_mail_errors(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.list_conversations.side_effect = MailConnectionError("down")

        response = self.client.get(reverse("mailops:api_mail_conversations"), **headers)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "mail_connection_failed")

    @patch("mailops.api.MailboxService")
    def test_mail_unified_conversations_returns_timeline_messages_with_directions(self, service_class):
        headers = self.auth_headers()
        token = Token.objects.get(user__email=self.account_email)
        service = service_class.return_value
        inbound = MailMessageSummary(
            uid="42",
            folder="INBOX",
            subject="Hello",
            sender="Sender Name <sender@example.com>",
            to=("user@example.com",),
            cc=(),
            date=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
            message_id="<m1@example.com>",
            flags=("Seen",),
            size=1234,
            has_attachments=False,
            has_visible_attachments=False,
        )
        outbound = MailMessageSummary(
            uid="7",
            folder="Sent",
            subject="Re: Hello",
            sender="User <user@example.com>",
            to=("sender@example.com",),
            cc=(),
            date=datetime(2026, 4, 16, 8, 0, tzinfo=dt_timezone.utc),
            message_id="<m2@example.com>",
            flags=(),
            size=2345,
            has_attachments=True,
            has_visible_attachments=True,
        )
        service.list_unified_conversations.return_value = MailUnifiedConversationSummaryPage(
            folders=("INBOX", "Sent"),
            conversations=(
                MailUnifiedConversationSummary(
                    conversation_id="thread-1",
                    message_count=2,
                    reply_count=1,
                    has_unread=False,
                    has_attachments=True,
                    has_visible_attachments=True,
                    participants=(MailConversationParticipant(name="Sender Name", email="sender@example.com"),),
                    messages=(
                        MailUnifiedMessageSummary(summary=inbound, direction="inbound"),
                        MailUnifiedMessageSummary(summary=outbound, direction="outbound"),
                    ),
                    latest_date=datetime(2026, 4, 16, 8, 0, tzinfo=dt_timezone.utc),
                ),
            ),
        )

        response = self.client.get(reverse("mailops:api_mail_unified_conversations"), {"limit": 25}, **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        conversation = payload["conversations"][0]
        self.assertEqual(payload["folders"], ["INBOX", "Sent"])
        self.assertEqual(conversation["participants"], [{"name": "Sender Name", "email": "sender@example.com"}])
        self.assertFalse(conversation["has_unread"])
        self.assertTrue(conversation["has_attachments"])
        self.assertTrue(conversation["has_visible_attachments"])
        self.assertEqual([message["folder"] for message in conversation["messages"]], ["INBOX", "Sent"])
        self.assertEqual([message["uid"] for message in conversation["messages"]], ["42", "7"])
        self.assertEqual([message["direction"] for message in conversation["messages"]], ["inbound", "outbound"])
        self.assertEqual(service.list_unified_conversations.call_args.kwargs, {"limit": 25, "user": token.user})

    def test_mail_unified_conversations_requires_token_and_validates_limit(self):
        missing_token = self.client.get(reverse("mailops:api_mail_unified_conversations"))
        headers = self.auth_headers()
        invalid_limit = self.client.get(reverse("mailops:api_mail_unified_conversations"), {"limit": 500}, **headers)

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(missing_token.json()["error"], "not_authenticated")
        self.assertEqual(invalid_limit.status_code, 400)
        self.assertEqual(invalid_limit.json()["error"], "invalid_limit")

    @patch("mailops.api.MailboxService")
    def test_mail_unified_conversations_maps_mail_errors(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.list_unified_conversations.side_effect = MailConnectionError("down")

        response = self.client.get(reverse("mailops:api_mail_unified_conversations"), **headers)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "mail_connection_failed")

    def test_mail_unified_conversations_reads_usable_index_before_live_imap(self):
        headers = self.auth_headers()
        token = Token.objects.get(user__email=self.account_email)
        inbound = MailMessageSummary(
            uid="42",
            folder="INBOX",
            subject="Hello",
            sender="Sender Name <sender@example.com>",
            to=(self.account_email,),
            cc=(),
            date=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
            message_id="<m1@example.com>",
            flags=("Seen",),
            size=1234,
            has_attachments=False,
            has_visible_attachments=False,
        )
        outbound = MailMessageSummary(
            uid="7",
            folder="Sent",
            subject="Re: Hello",
            sender=f"User <{self.account_email}>",
            to=("sender@example.com",),
            cc=(),
            date=datetime(2026, 4, 16, 8, 0, tzinfo=dt_timezone.utc),
            message_id="<m2@example.com>",
            in_reply_to=("<m1@example.com>",),
            references=("<m1@example.com>",),
            flags=(),
            size=2345,
            has_attachments=True,
            has_visible_attachments=True,
        )
        MailIndexService().index_summaries(
            user=token.user,
            account_email=self.account_email,
            sent_folder="Sent",
            summaries_by_folder={"INBOX": (inbound,), "Sent": (outbound,)},
        )

        response = self.client.get(reverse("mailops:api_mail_unified_conversations"), {"limit": 25}, **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        conversation = payload["conversations"][0]
        self.assertEqual(payload["folders"], ["INBOX", "Sent"])
        self.assertEqual(conversation["participants"], [{"name": "Sender Name", "email": "sender@example.com"}, {"name": "", "email": self.account_email}])
        self.assertEqual([message["folder"] for message in conversation["messages"]], ["INBOX", "Sent"])
        self.assertEqual([message["uid"] for message in conversation["messages"]], ["42", "7"])
        self.assertEqual([message["direction"] for message in conversation["messages"]], ["inbound", "outbound"])
        self.assertTrue(conversation["has_attachments"])
        self.assertTrue(conversation["has_visible_attachments"])

    def test_mailbox_service_falls_back_to_live_imap_when_index_missing(self):
        token = create_mailbox_token(self.account_email, self.password)
        credentials = mailbox_credentials_from_request(Mock(auth=token))
        imap_client = Mock()
        imap_client.__enter__ = Mock(return_value=Mock())
        imap_client.__exit__ = Mock(return_value=None)
        entered = imap_client.__enter__.return_value
        entered.fetch_unified_conversation_page.return_value = MailUnifiedConversationSummaryPage(folders=("INBOX",), conversations=())

        page = MailboxService(imap_client_factory=lambda: imap_client).list_unified_conversations(credentials, limit=6, user=token.user)

        self.assertEqual(page.folders, ("INBOX",))
        entered.login.assert_called_once_with(credentials)
        entered.fetch_unified_conversation_page.assert_called_once_with(account_email=self.account_email, limit=6)

    def test_mail_index_upsert_is_idempotent_and_dedupes_duplicate_message_ids(self):
        token = create_mailbox_token(self.account_email, self.password)
        inbound = MailMessageSummary(
            uid="101",
            folder="INBOX",
            subject="Receipt",
            sender="Shop <shop@example.com>",
            to=(self.account_email,),
            date=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
            message_id="<same@example.com>",
            flags=(),
            size=100,
            has_attachments=True,
            has_visible_attachments=True,
        )
        sent_copy = MailMessageSummary(
            uid="202",
            folder="Sent",
            subject="Receipt",
            sender="Shop <shop@example.com>",
            to=(self.account_email,),
            date=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
            message_id="<same@example.com>",
            flags=("Seen",),
            size=100,
            has_attachments=True,
            has_visible_attachments=True,
        )

        service = MailIndexService()
        service.index_summaries(
            user=token.user,
            account_email=self.account_email,
            sent_folder="Sent",
            summaries_by_folder={"INBOX": (inbound,), "Sent": (sent_copy,)},
        )
        service.index_summaries(
            user=token.user,
            account_email=self.account_email,
            sent_folder="Sent",
            summaries_by_folder={"INBOX": (inbound,), "Sent": (sent_copy,)},
        )

        account = MailAccountIndex.objects.get(account_email=self.account_email)
        conversation = MailConversationIndex.objects.get(account=account)
        self.assertEqual(MailMessageIndex.objects.filter(account=account).count(), 2)
        self.assertEqual(conversation.message_count, 1)
        self.assertTrue(conversation.has_unread)
        self.assertEqual(conversation.participants_json, [{"name": "Shop", "email": "shop@example.com"}, {"name": "", "email": self.account_email}])

    def test_mail_index_groups_offer_subject_when_parent_message_is_missing(self):
        token = create_mailbox_token(self.account_email, self.password)
        original_forward = MailMessageSummary(
            uid="222",
            folder="INBOX",
            subject="Fwd: Ponuda br. 121714",
            sender="Ante Vrcan <avrcanus@gmail.com>",
            to=(self.account_email,),
            date=datetime(2026, 4, 18, 21, 33, tzinfo=dt_timezone.utc),
            message_id="<gmail-forward@example.com>",
            in_reply_to=("<missing-original@example.com>",),
            references=("<missing-original@example.com>",),
        )
        reply_with_note = MailMessageSummary(
            uid="223",
            folder="INBOX",
            subject="Re: Fwd: Ponuda br. 121714 razlika",
            sender="Ante Vrcan <avrcanus@gmail.com>",
            to=(self.account_email,),
            date=datetime(2026, 4, 19, 8, 47, tzinfo=dt_timezone.utc),
            message_id="<gmail-reply@example.com>",
            in_reply_to=("<missing-local-reply@example.com>",),
            references=("<missing-local-reply@example.com>",),
        )

        MailIndexService().index_summaries(
            user=token.user,
            account_email=self.account_email,
            sent_folder="Sent",
            summaries_by_folder={"INBOX": (original_forward, reply_with_note)},
        )

        account = MailAccountIndex.objects.get(account_email=self.account_email)
        conversation = MailConversationIndex.objects.get(account=account)
        self.assertEqual(conversation.thread_key, "subject:ponuda br. 121714")
        self.assertEqual(conversation.message_count, 2)

    def test_mail_index_groups_offer_sent_copy_without_parent_headers(self):
        token = create_mailbox_token(self.account_email, self.password)
        original_forward = MailMessageSummary(
            uid="222",
            folder="INBOX",
            subject="Fwd: Ponuda br. 121714",
            sender="Ante Vrcan <avrcanus@gmail.com>",
            to=(self.account_email,),
            date=datetime(2026, 4, 18, 21, 33, tzinfo=dt_timezone.utc),
            message_id="<gmail-forward@example.com>",
            in_reply_to=("<missing-original@example.com>",),
            references=("<missing-original@example.com>",),
        )
        sent_reply = MailMessageSummary(
            uid="58",
            folder="Sent",
            subject="Re: Fwd: Ponuda br. 121714 razlika",
            sender=f"Ante Vrcan <{self.account_email}>",
            to=("avrcanus@gmail.com",),
            date=datetime(2026, 4, 19, 9, 15, tzinfo=dt_timezone.utc),
            message_id="<sent-reply@example.com>",
        )

        MailIndexService().index_summaries(
            user=token.user,
            account_email=self.account_email,
            sent_folder="Sent",
            summaries_by_folder={"INBOX": (original_forward,), "Sent": (sent_reply,)},
        )

        account = MailAccountIndex.objects.get(account_email=self.account_email)
        conversation = MailConversationIndex.objects.get(account=account)
        self.assertEqual(conversation.thread_key, "subject:ponuda br. 121714")
        self.assertEqual(conversation.message_count, 2)
        self.assertEqual(conversation.folders_json, ["INBOX", "Sent"])

    def test_mail_index_groups_generic_sent_reply_without_parent_headers(self):
        token = create_mailbox_token(self.account_email, self.password)
        original = MailMessageSummary(
            uid="224",
            folder="INBOX",
            subject="Prbno za brisanje",
            sender="Ante Vrcan <dalekopro@gmail.com>",
            to=(self.account_email,),
            date=datetime(2026, 4, 20, 5, 58, tzinfo=dt_timezone.utc),
            message_id="<gmail-original@example.com>",
        )
        sent_reply = MailMessageSummary(
            uid="60",
            folder="Sent",
            subject="Re: Prbno za brisanje",
            sender=f"Ante Vrcan <{self.account_email}>",
            to=("dalekopro@gmail.com",),
            date=datetime(2026, 4, 20, 6, 10, tzinfo=dt_timezone.utc),
            message_id="<sent-reply@example.com>",
        )

        MailIndexService().index_summaries(
            user=token.user,
            account_email=self.account_email,
            sent_folder="Sent",
            summaries_by_folder={"INBOX": (original,), "Sent": (sent_reply,)},
        )

        account = MailAccountIndex.objects.get(account_email=self.account_email)
        conversation = MailConversationIndex.objects.get(account=account)
        self.assertEqual(conversation.thread_key, "subject:prbno za brisanje")
        self.assertEqual(conversation.message_count, 2)
        self.assertEqual(conversation.folders_json, ["INBOX", "Sent"])

    def test_mail_index_recent_missing_reconcile_does_not_delete_by_default(self):
        token = create_mailbox_token(self.account_email, self.password)
        account = MailAccountIndex.objects.create(user=token.user, account_email=self.account_email)
        MailMessageIndex.objects.create(
            account=account,
            folder="INBOX",
            uid=101,
            direction=MailMessageIndex.DIRECTION_INBOUND,
            thread_key="uid:101",
            subject="Still indexed",
            sender_raw="Sender <sender@example.com>",
            sent_at=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
            dedupe_key="uid:inbox:101",
        )
        touched_thread_keys = set()

        reconcile_recent_missing_messages(
            account,
            FolderSyncResult(folder="INBOX", present_uids=(102, 103)),
            touched_thread_keys,
        )

        self.assertTrue(MailMessageIndex.objects.filter(account=account, uid=101).exists())
        self.assertEqual(touched_thread_keys, set())

    def test_mail_index_status_returns_account_and_folder_state(self):
        headers = self.auth_headers()
        token = Token.objects.get(user__email=self.account_email)
        account = MailAccountIndex.objects.create(
            user=token.user,
            account_email=self.account_email,
            index_status=MailAccountIndex.STATUS_READY,
            last_indexed_at=datetime(2026, 4, 17, 13, 40, tzinfo=dt_timezone.utc),
            last_sync_started_at=datetime(2026, 4, 17, 13, 39, 50, tzinfo=dt_timezone.utc),
            last_sync_finished_at=datetime(2026, 4, 17, 13, 40, tzinfo=dt_timezone.utc),
            last_sync_error="",
        )
        account.folder_states.create(
            folder="Sent",
            uidvalidity="67890",
            highest_indexed_uid=120,
            last_synced_at=datetime(2026, 4, 17, 13, 39, 58, tzinfo=dt_timezone.utc),
        )
        account.folder_states.create(
            folder="INBOX",
            uidvalidity="12345",
            highest_indexed_uid=500,
            last_synced_at=datetime(2026, 4, 17, 13, 40, tzinfo=dt_timezone.utc),
        )

        response = self.client.get(reverse("mailops:api_mail_index_status"), **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["account_email"], self.account_email)
        self.assertEqual(payload["index_status"], MailAccountIndex.STATUS_READY)
        self.assertEqual(payload["last_sync_error"], "")
        self.assertIn("2026-04-17T13:40:00", payload["last_indexed_at"])
        self.assertEqual([folder["folder"] for folder in payload["folders"]], ["INBOX", "Sent"])
        self.assertEqual(payload["folders"][0]["uidvalidity"], "12345")
        self.assertEqual(payload["folders"][0]["highest_indexed_uid"], 500)

    def test_mail_index_status_supports_all_statuses_without_folder_rows(self):
        headers = self.auth_headers()
        token = Token.objects.get(user__email=self.account_email)
        statuses = [
            MailAccountIndex.STATUS_EMPTY,
            MailAccountIndex.STATUS_SYNCING,
            MailAccountIndex.STATUS_READY,
            MailAccountIndex.STATUS_PARTIAL,
            MailAccountIndex.STATUS_FAILED,
        ]
        for index, index_status in enumerate(statuses):
            email = f"status-{index}@example.com"
            MailAccountIndex.objects.create(
                user=token.user,
                account_email=email,
                index_status=index_status,
                last_sync_error="stored operational status" if index_status == MailAccountIndex.STATUS_FAILED else "",
            )

            response = self.client.get(reverse("mailops:api_mail_index_status"), {"account_email": email}, **headers)

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["account_email"], email)
            self.assertEqual(payload["index_status"], index_status)
            self.assertEqual(payload["folders"], [])
            if index_status == MailAccountIndex.STATUS_FAILED:
                self.assertEqual(payload["last_sync_error"], "stored operational status")

    def test_mail_index_status_requires_token_and_mailbox_credentials(self):
        missing_token = self.client.get(reverse("mailops:api_mail_index_status"))
        User = get_user_model()
        user = User.objects.create_user(username="no-mailbox", email="no-mailbox@example.com")
        token, _ = Token.objects.get_or_create(user=user)

        missing_credentials = self.client.get(reverse("mailops:api_mail_index_status"), HTTP_AUTHORIZATION=f"Token {token.key}")

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(missing_token.json()["error"], "not_authenticated")
        self.assertEqual(missing_credentials.status_code, 401)
        self.assertEqual(missing_credentials.json()["error"], "mailbox_credentials_missing")

    def test_mail_index_status_validates_account_and_returns_not_found(self):
        headers = self.auth_headers()
        token = Token.objects.get(user__email=self.account_email)
        MailAccountIndex.objects.create(user=token.user, account_email=self.account_email)
        invalid = self.client.get(reverse("mailops:api_mail_index_status"), {"account_email": "not-an-email"}, **headers)
        missing = self.client.get(reverse("mailops:api_mail_index_status"), {"account_email": "missing@example.com"}, **headers)

        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["error"], "invalid_account_email")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["error"], "mail_index_not_found")

    def test_mail_index_sync_runner_selects_due_accounts_and_skips_fresh_or_active_syncing(self):
        token = create_mailbox_token(self.account_email, self.password)
        now = timezone.now()
        due_never = MailAccountIndex.objects.create(user=token.user, account_email=self.account_email)
        due_stale = MailAccountIndex.objects.create(
            user=token.user,
            account_email="stale@example.com",
            index_status=MailAccountIndex.STATUS_READY,
            last_indexed_at=now - timezone.timedelta(minutes=30),
        )
        MailAccountIndex.objects.create(
            user=token.user,
            account_email="fresh@example.com",
            index_status=MailAccountIndex.STATUS_READY,
            last_indexed_at=now,
        )
        MailAccountIndex.objects.create(
            user=token.user,
            account_email="syncing@example.com",
            index_status=MailAccountIndex.STATUS_SYNCING,
            last_sync_started_at=now,
        )

        selected = select_accounts_for_sync(now=now, stale_after_seconds=600)

        self.assertEqual([account.pk for account in selected], [due_never.pk, due_stale.pk])

    def test_mail_index_sync_runner_retries_stale_syncing_and_respects_failure_cooldown(self):
        token = create_mailbox_token(self.account_email, self.password)
        now = timezone.now()
        stale_syncing = MailAccountIndex.objects.create(
            user=token.user,
            account_email="stale-syncing@example.com",
            index_status=MailAccountIndex.STATUS_SYNCING,
            last_sync_started_at=now - timezone.timedelta(minutes=30),
        )
        retry_failed = MailAccountIndex.objects.create(
            user=token.user,
            account_email="retry-failed@example.com",
            index_status=MailAccountIndex.STATUS_FAILED,
            last_indexed_at=now - timezone.timedelta(hours=2),
            last_sync_finished_at=now - timezone.timedelta(hours=2),
        )
        MailAccountIndex.objects.create(
            user=token.user,
            account_email="cooldown@example.com",
            index_status=MailAccountIndex.STATUS_FAILED,
            last_indexed_at=now - timezone.timedelta(hours=2),
            last_sync_finished_at=now,
        )

        selected = select_accounts_for_sync(now=now, stale_after_seconds=600, failure_cooldown_seconds=1800)

        self.assertEqual([account.pk for account in selected], [stale_syncing.pk, retry_failed.pk])

    def test_mail_index_sync_cycle_syncs_selected_accounts_and_skips_missing_credentials(self):
        token = create_mailbox_token(self.account_email, self.password)
        MailAccountIndex.objects.create(user=token.user, account_email=self.account_email)
        MailAccountIndex.objects.create(user=token.user, account_email="missing@example.com")
        service = Mock()

        result = run_sync_cycle(mail_index_service=service)

        self.assertEqual(result.scanned, 2)
        self.assertEqual(result.selected, 2)
        self.assertEqual(result.synced, 1)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(result.failed, 0)
        self.assertEqual(service.sync_account.call_count, 1)
        args, kwargs = service.sync_account.call_args
        self.assertEqual(args[0], token.user)
        self.assertEqual(args[1].email, self.account_email)
        self.assertEqual(kwargs, {"limit": 500, "incremental": True})

    def test_mail_index_sync_cycle_seeds_account_indexes_from_credentials(self):
        token = create_mailbox_token(self.account_email, self.password)
        service = Mock()

        result = run_sync_cycle(mail_index_service=service)

        self.assertEqual(result.scanned, 1)
        self.assertEqual(result.selected, 1)
        self.assertEqual(result.synced, 1)
        account = MailAccountIndex.objects.get(account_email=self.account_email)
        self.assertEqual(account.user, token.user)
        service.sync_account.assert_called_once()

    @patch("mailops.mail_indexing.runner.run_sync_cycle")
    def test_run_mail_index_sync_cycle_command_outputs_summary(self, run_cycle):
        run_cycle.return_value = Mock(scanned=2, selected=1, synced=1, failed=0, skipped=1, elapsed_seconds=0.12)
        output = io.StringIO()

        call_command("run_mail_index_sync_cycle", "--max-accounts", "3", "--limit", "25", stdout=output)

        self.assertIn("scanned=2 selected=1 synced=1 failed=0 skipped=1", output.getvalue())
        self.assertEqual(run_cycle.call_args.kwargs["max_accounts"], 3)
        self.assertEqual(run_cycle.call_args.kwargs["limit"], 25)

    @patch("mailops.api.MailboxService")
    def test_mail_message_detail_returns_service_result(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        service.get_message_detail.return_value = MailMessageDetail(
            uid="42",
            folder="INBOX",
            subject="Hello",
            sender="Sender <sender@example.com>",
            to=("user@example.com",),
            cc=(),
            date=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
            message_id="<m1@example.com>",
            flags=("Seen",),
            size=2048,
            text_body="Plain body",
            html_body="<p>HTML body</p>",
            attachments=(
                MailAttachmentSummary(
                    id="att_1",
                    filename="report.pdf",
                    content_type="application/pdf",
                    size=12345,
                    disposition="attachment",
                    is_inline=False,
                    content_id="",
                    is_visible=True,
                ),
                MailAttachmentSummary(
                    id="att_2",
                    filename="logo.png",
                    content_type="image/png",
                    size=234,
                    disposition="inline",
                    is_inline=True,
                    content_id="logo123",
                    is_visible=False,
                ),
            ),
            has_visible_attachments=True,
        )

        response = self.client.get(reverse("mailops:api_mail_message_detail", kwargs={"uid": "42"}), {"folder": "INBOX"}, **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["message"]["uid"], "42")
        self.assertEqual(payload["message"]["text_body"], "Plain body")
        self.assertEqual(payload["message"]["html_body"], "<p>HTML body</p>")
        self.assertEqual(payload["message"]["attachments"][0]["id"], "att_1")
        self.assertEqual(payload["message"]["attachments"][0]["filename"], "report.pdf")
        self.assertEqual(payload["message"]["attachments"][0]["content_id"], "")
        self.assertTrue(payload["message"]["attachments"][0]["is_visible"])
        self.assertFalse(payload["message"]["attachments"][0]["is_inline"])
        self.assertEqual(payload["message"]["attachments"][1]["content_id"], "logo123")
        self.assertTrue(payload["message"]["attachments"][1]["is_inline"])
        self.assertFalse(payload["message"]["attachments"][1]["is_visible"])
        self.assertTrue(payload["message"]["has_visible_attachments"])
        credentials = service.get_message_detail.call_args.args[0]
        self.assertEqual(credentials.email, self.account_email)
        self.assertEqual(service.get_message_detail.call_args.kwargs, {"folder": "INBOX", "uid": "42"})

    @patch("mailops.api.MailboxService")
    def test_mail_message_detail_marks_indexed_message_read(self, service_class):
        headers = self.auth_headers()
        token = Token.objects.get(user__email=self.account_email)
        indexed_message = MailMessageSummary(
            uid="42",
            folder="INBOX",
            subject="Hello",
            sender="Sender <sender@example.com>",
            to=(self.account_email,),
            date=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
            message_id="<m1@example.com>",
            flags=(),
        )
        MailIndexService().index_summaries(
            user=token.user,
            account_email=self.account_email,
            sent_folder="Sent",
            summaries_by_folder={"INBOX": (indexed_message,)},
        )
        service_class.return_value.get_message_detail.return_value = MailMessageDetail(
            uid="42",
            folder="INBOX",
            subject="Hello",
            sender="Sender <sender@example.com>",
            to=(self.account_email,),
            date=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
            message_id="<m1@example.com>",
            flags=("Seen",),
            text_body="Plain body",
        )

        response = self.client.get(reverse("mailops:api_mail_message_detail", kwargs={"uid": "42"}), {"folder": "INBOX"}, **headers)

        self.assertEqual(response.status_code, 200)
        account = MailAccountIndex.objects.get(account_email=self.account_email)
        message = MailMessageIndex.objects.get(account=account, folder="INBOX", uid=42)
        conversation = MailConversationIndex.objects.get(account=account)
        self.assertTrue(message.is_read)
        self.assertIn("Seen", message.flags_json)
        self.assertFalse(conversation.has_unread)

    @patch("mailops.api.MailboxService")
    def test_mail_message_detail_maps_connection_errors(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.get_message_detail.side_effect = MailConnectionError("down")

        response = self.client.get(reverse("mailops:api_mail_message_detail", kwargs={"uid": "42"}), **headers)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "mail_connection_failed")

    @patch("mailops.api.MailboxService")
    def test_mail_attachment_download_returns_binary_response(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        service.get_attachment.return_value = MailAttachmentContent(
            summary=MailAttachmentSummary(
                id="att_1",
                filename="report.pdf",
                content_type="application/pdf",
                size=11,
                disposition="attachment",
                is_inline=False,
            ),
            content=b"pdf content",
        )

        response = self.client.get(reverse("mailops:api_mail_attachment", kwargs={"uid": "42", "attachment_id": "att_1"}), {"folder": "INBOX"}, **headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"pdf content")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("report.pdf", response["Content-Disposition"])
        credentials = service.get_attachment.call_args.args[0]
        self.assertEqual(credentials.email, self.account_email)
        self.assertEqual(service.get_attachment.call_args.kwargs, {"folder": "INBOX", "uid": "42", "attachment_id": "att_1"})

    @patch("mailops.api.MailboxService")
    def test_mail_attachment_download_validates_folder_and_not_found(self, service_class):
        headers = self.auth_headers()
        missing_folder = self.client.get(reverse("mailops:api_mail_attachment", kwargs={"uid": "42", "attachment_id": "att_1"}), **headers)
        service_class.return_value.get_attachment.side_effect = MailAttachmentNotFoundError("missing")
        missing_attachment = self.client.get(
            reverse("mailops:api_mail_attachment", kwargs={"uid": "42", "attachment_id": "att_99"}),
            {"folder": "INBOX"},
            **headers,
        )

        self.assertEqual(missing_folder.status_code, 400)
        self.assertEqual(missing_folder.json()["error"], "invalid_folder")
        self.assertEqual(missing_attachment.status_code, 404)
        self.assertEqual(missing_attachment.json()["error"], "attachment_not_found")

    @patch("mailops.api.MailboxService")
    def test_mail_messages_delete_batch_returns_move_result(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        service.move_messages_to_trash.return_value = MailMessageMoveToTrashResult(
            trash_folder="Trash",
            moved_to_trash=("123", "124"),
            failed=(),
        )

        response = self.client.post(
            reverse("mailops:api_mail_messages_delete"),
            data={"folder": "INBOX", "uids": [123, "124"]},
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "account_email": self.account_email,
                "folder": "INBOX",
                "trash_folder": "Trash",
                "success": True,
                "partial": False,
                "moved_to_trash": ["123", "124"],
                "failed": [],
            },
        )
        credentials = service.move_messages_to_trash.call_args.args[0]
        self.assertEqual(credentials.email, self.account_email)
        self.assertEqual(credentials.password, self.password)
        self.assertEqual(service.move_messages_to_trash.call_args.kwargs, {"folder": "INBOX", "uids": ("123", "124")})

    @patch("mailops.api.MailboxService")
    def test_mail_message_delete_single_uses_query_folder(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.move_messages_to_trash.return_value = MailMessageMoveToTrashResult(
            trash_folder="Trash",
            moved_to_trash=("42",),
            failed=(),
        )

        response = self.client.post(f'{reverse("mailops:api_mail_message_delete", kwargs={"uid": "42"})}?folder=Archive', **headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["moved_to_trash"], ["42"])
        self.assertEqual(service_class.return_value.move_messages_to_trash.call_args.kwargs, {"folder": "Archive", "uids": ("42",)})

    @patch("mailops.api.MailboxService")
    def test_mail_message_delete_detail_endpoint_defaults_to_inbox_and_removes_index_row(self, service_class):
        headers = self.auth_headers()
        token = Token.objects.get(user__email=self.account_email)
        indexed_message = MailMessageSummary(
            uid="42",
            folder="INBOX",
            subject="Hello",
            sender="Sender <sender@example.com>",
            to=(self.account_email,),
            date=datetime(2026, 4, 16, 7, 0, tzinfo=dt_timezone.utc),
            message_id="<m1@example.com>",
        )
        MailIndexService().index_summaries(
            user=token.user,
            account_email=self.account_email,
            sent_folder="Sent",
            summaries_by_folder={"INBOX": (indexed_message,)},
        )
        service_class.return_value.move_messages_to_trash.return_value = MailMessageMoveToTrashResult(
            trash_folder="Trash",
            moved_to_trash=("42",),
            failed=(),
        )

        response = self.client.delete(reverse("mailops:api_mail_message_detail", kwargs={"uid": "42"}), **headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["moved_to_trash"], ["42"])
        self.assertEqual(service_class.return_value.move_messages_to_trash.call_args.kwargs, {"folder": "INBOX", "uids": ("42",)})
        account = MailAccountIndex.objects.get(account_email=self.account_email)
        self.assertFalse(MailMessageIndex.objects.filter(account=account, folder="INBOX", uid=42).exists())
        self.assertFalse(MailConversationIndex.objects.filter(account=account).exists())

    @patch("mailops.api.MailboxService")
    def test_mail_messages_delete_serializes_partial_failures(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.move_messages_to_trash.return_value = MailMessageMoveToTrashResult(
            trash_folder="Trash",
            moved_to_trash=("123",),
            failed=(MailMessageMoveFailure(uid="124", error="move_failed", detail="IMAP move failed for UID 124"),),
        )

        response = self.client.post(
            reverse("mailops:api_mail_messages_delete"),
            data={"folder": "INBOX", "uids": ["123", "124"]},
            content_type="application/json",
            **headers,
        )

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["success"])
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["moved_to_trash"], ["123"])
        self.assertEqual(payload["failed"][0]["uid"], "124")
        self.assertEqual(payload["failed"][0]["error"], "move_failed")

    def test_mail_messages_delete_requires_token_and_validates_payload(self):
        missing_token = self.client.post(reverse("mailops:api_mail_messages_delete"), data={}, content_type="application/json")
        headers = self.auth_headers()
        missing_folder = self.client.post(
            reverse("mailops:api_mail_messages_delete"),
            data={"uids": ["1"]},
            content_type="application/json",
            **headers,
        )
        empty_uids = self.client.post(
            reverse("mailops:api_mail_messages_delete"),
            data={"folder": "INBOX", "uids": []},
            content_type="application/json",
            **headers,
        )
        invalid_uid = self.client.post(
            reverse("mailops:api_mail_messages_delete"),
            data={"folder": "INBOX", "uids": ["abc"]},
            content_type="application/json",
            **headers,
        )
        single_missing_folder = self.client.post(reverse("mailops:api_mail_message_delete", kwargs={"uid": "42"}), **headers)

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(missing_token.json()["error"], "not_authenticated")
        self.assertEqual(missing_folder.status_code, 400)
        self.assertEqual(missing_folder.json()["error"], "invalid_folder")
        self.assertEqual(empty_uids.status_code, 400)
        self.assertEqual(empty_uids.json()["error"], "empty_uid_list")
        self.assertEqual(invalid_uid.status_code, 400)
        self.assertEqual(invalid_uid.json()["error"], "invalid_uid")
        self.assertEqual(single_missing_folder.status_code, 400)
        self.assertEqual(single_missing_folder.json()["error"], "invalid_folder")

    @patch("mailops.api.MailboxService")
    def test_mail_messages_delete_maps_mail_errors_and_trash_guard(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.move_messages_to_trash.side_effect = MailInvalidOperationError("trash")
        trash_response = self.client.post(
            reverse("mailops:api_mail_messages_delete"),
            data={"folder": "Trash", "uids": ["1"]},
            content_type="application/json",
            **headers,
        )

        service_class.return_value.move_messages_to_trash.side_effect = MailConnectionError("down")
        connection_response = self.client.post(
            reverse("mailops:api_mail_messages_delete"),
            data={"folder": "INBOX", "uids": ["1"]},
            content_type="application/json",
            **headers,
        )

        self.assertEqual(trash_response.status_code, 400)
        self.assertEqual(trash_response.json()["error"], "delete_from_trash_not_supported")
        self.assertEqual(connection_response.status_code, 502)
        self.assertEqual(connection_response.json()["error"], "mail_connection_failed")

    @patch("mailops.api.MailboxService")
    def test_mail_messages_restore_batch_returns_restore_result(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        service.restore_messages_from_trash.return_value = MailMessageRestoreResult(
            target_folder="INBOX",
            restored=("123", "124"),
            failed=(),
        )

        response = self.client.post(
            reverse("mailops:api_mail_messages_restore"),
            data={"folder": "Trash", "target_folder": "INBOX", "uids": [123, "124"]},
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "account_email": self.account_email,
                "folder": "Trash",
                "target_folder": "INBOX",
                "success": True,
                "partial": False,
                "restored": ["123", "124"],
                "failed": [],
            },
        )
        credentials = service.restore_messages_from_trash.call_args.args[0]
        self.assertEqual(credentials.email, self.account_email)
        self.assertEqual(credentials.password, self.password)
        self.assertEqual(service.restore_messages_from_trash.call_args.kwargs, {"folder": "Trash", "target_folder": "INBOX", "uids": ("123", "124")})

    @patch("mailops.api.MailboxService")
    def test_mail_message_restore_single_uses_query_folders(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.restore_messages_from_trash.return_value = MailMessageRestoreResult(
            target_folder="Archive",
            restored=("42",),
            failed=(),
        )

        response = self.client.post(f'{reverse("mailops:api_mail_message_restore", kwargs={"uid": "42"})}?folder=Trash&target_folder=Archive', **headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["restored"], ["42"])
        self.assertEqual(
            service_class.return_value.restore_messages_from_trash.call_args.kwargs,
            {"folder": "Trash", "target_folder": "Archive", "uids": ("42",)},
        )

    @patch("mailops.api.MailboxService")
    def test_mail_messages_restore_serializes_partial_failures(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.restore_messages_from_trash.return_value = MailMessageRestoreResult(
            target_folder="INBOX",
            restored=("123",),
            failed=(MailMessageMoveFailure(uid="124", error="restore_failed", detail="IMAP restore failed for UID 124"),),
        )

        response = self.client.post(
            reverse("mailops:api_mail_messages_restore"),
            data={"folder": "Trash", "target_folder": "INBOX", "uids": ["123", "124"]},
            content_type="application/json",
            **headers,
        )

        payload = response.json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["success"])
        self.assertTrue(payload["partial"])
        self.assertEqual(payload["restored"], ["123"])
        self.assertEqual(payload["failed"][0]["uid"], "124")
        self.assertEqual(payload["failed"][0]["error"], "restore_failed")

    def test_mail_messages_restore_requires_token_and_validates_payload(self):
        missing_token = self.client.post(reverse("mailops:api_mail_messages_restore"), data={}, content_type="application/json")
        headers = self.auth_headers()
        missing_folder = self.client.post(
            reverse("mailops:api_mail_messages_restore"),
            data={"target_folder": "INBOX", "uids": ["1"]},
            content_type="application/json",
            **headers,
        )
        missing_target = self.client.post(
            reverse("mailops:api_mail_messages_restore"),
            data={"folder": "Trash", "uids": ["1"]},
            content_type="application/json",
            **headers,
        )
        empty_uids = self.client.post(
            reverse("mailops:api_mail_messages_restore"),
            data={"folder": "Trash", "target_folder": "INBOX", "uids": []},
            content_type="application/json",
            **headers,
        )
        invalid_uid = self.client.post(
            reverse("mailops:api_mail_messages_restore"),
            data={"folder": "Trash", "target_folder": "INBOX", "uids": ["abc"]},
            content_type="application/json",
            **headers,
        )
        single_missing_target = self.client.post(f'{reverse("mailops:api_mail_message_restore", kwargs={"uid": "42"})}?folder=Trash', **headers)

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(missing_token.json()["error"], "not_authenticated")
        self.assertEqual(missing_folder.status_code, 400)
        self.assertEqual(missing_folder.json()["error"], "invalid_folder")
        self.assertEqual(missing_target.status_code, 400)
        self.assertEqual(missing_target.json()["error"], "invalid_target_folder")
        self.assertEqual(empty_uids.status_code, 400)
        self.assertEqual(empty_uids.json()["error"], "empty_uid_list")
        self.assertEqual(invalid_uid.status_code, 400)
        self.assertEqual(invalid_uid.json()["error"], "invalid_uid")
        self.assertEqual(single_missing_target.status_code, 400)
        self.assertEqual(single_missing_target.json()["error"], "invalid_target_folder")

    @patch("mailops.api.MailboxService")
    def test_mail_messages_restore_maps_invalid_operations_and_mail_errors(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.restore_messages_from_trash.side_effect = MailInvalidOperationError("restore_source_not_trash")
        source_response = self.client.post(
            reverse("mailops:api_mail_messages_restore"),
            data={"folder": "INBOX", "target_folder": "Archive", "uids": ["1"]},
            content_type="application/json",
            **headers,
        )

        service_class.return_value.restore_messages_from_trash.side_effect = MailInvalidOperationError("restore_target_is_trash")
        target_response = self.client.post(
            reverse("mailops:api_mail_messages_restore"),
            data={"folder": "Trash", "target_folder": "Trash", "uids": ["1"]},
            content_type="application/json",
            **headers,
        )

        service_class.return_value.restore_messages_from_trash.side_effect = MailConnectionError("down")
        connection_response = self.client.post(
            reverse("mailops:api_mail_messages_restore"),
            data={"folder": "Trash", "target_folder": "INBOX", "uids": ["1"]},
            content_type="application/json",
            **headers,
        )

        self.assertEqual(source_response.status_code, 400)
        self.assertEqual(source_response.json()["error"], "restore_source_not_trash")
        self.assertEqual(target_response.status_code, 400)
        self.assertEqual(target_response.json()["error"], "restore_target_is_trash")
        self.assertEqual(connection_response.status_code, 502)
        self.assertEqual(connection_response.json()["error"], "mail_connection_failed")

    @patch("mailops.api.MailboxService")
    def test_mail_send_calls_service_and_returns_message_id(self, service_class):
        headers = self.auth_headers()
        token = Token.objects.get(user__email=self.account_email)
        index = MailAccountIndex.objects.create(
            user=token.user,
            account_email=self.account_email,
            index_status=MailAccountIndex.STATUS_READY,
            last_indexed_at=timezone.now(),
        )
        service = service_class.return_value
        service.send_mail.return_value = "<sent@example.com>"

        response = self.client.post(
            reverse("mailops:api_mail_send"),
            data={
                "to": ["Recipient Name <to@example.com>"],
                "cc": ["Copy Person <copy@example.com>"],
                "bcc": ["Hidden Person <hidden@example.com>"],
                "reply_to": "Reply Person <reply@example.com>",
                "in_reply_to": "<root@example.com>",
                "references": ["<first@example.com>", "<root@example.com>"],
                "subject": "Status",
                "text_body": "Plain body",
                "html_body": "<p>HTML body</p>",
                "from_display_name": "Sender Name",
            },
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"account_email": self.account_email, "status": "sent", "message_id": "<sent@example.com>"})
        credentials = service.send_mail.call_args.args[0]
        request = service.send_mail.call_args.args[1]
        self.assertEqual(credentials.email, self.account_email)
        self.assertEqual(request.to, ("to@example.com",))
        self.assertEqual(request.cc, ("copy@example.com",))
        self.assertEqual(request.bcc, ("hidden@example.com",))
        self.assertEqual(request.reply_to, "reply@example.com")
        self.assertEqual(request.in_reply_to, "<root@example.com>")
        self.assertEqual(request.references, ("<first@example.com>", "<root@example.com>"))
        self.assertEqual(request.subject, "Status")
        self.assertEqual(request.text_body, "Plain body")
        self.assertEqual(request.html_body, "<p>HTML body</p>")
        self.assertEqual(request.from_display_name, "Sender Name")
        self.assertEqual(request.attachments, ())
        index.refresh_from_db()
        self.assertIsNone(index.last_indexed_at)

    @override_settings(
        GMAIL_IMPORT_GOOGLE_CLIENT_ID="client-id",
        GMAIL_IMPORT_GOOGLE_CLIENT_SECRET="client-secret",
        GMAIL_IMPORT_OAUTH_REDIRECT_URI="https://mailadmin.example.com/oauth/gmail/callback",
        GMAIL_IMPORT_OAUTH_SCOPES=("https://mail.google.com/", "https://www.googleapis.com/auth/gmail.modify"),
    )
    @patch("mailops.gmail_send.GmailClient")
    @patch("mailops.api.MailboxService")
    def test_mail_send_for_connected_gmail_account_uses_gmail_api(self, service_class, gmail_client_class):
        headers = self.auth_headers()
        token = Token.objects.get(user__email=self.account_email)
        account = GmailImportAccount(user=token.user, gmail_email=self.account_email, target_mailbox_email=self.account_email, delete_after_import=True)
        account.set_refresh_token("refresh-secret")
        account.save()
        service = service_class.return_value
        service.prepare_send_request.side_effect = lambda credentials, send_request: send_request
        gmail_client = gmail_client_class.return_value
        gmail_client.send_raw_message.return_value = GmailMessageRef(gmail_message_id="gmail-sent-1", gmail_thread_id="thread-1")

        response = self.client.post(
            reverse("mailops:api_mail_send"),
            data={
                "to": ["to@example.com"],
                "bcc": ["hidden@example.com"],
                "subject": "Via Gmail",
                "text_body": "Body",
                "from_display_name": "Sender",
            },
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message_id"].startswith("<"), True)
        service.send_mail.assert_not_called()
        service.prepare_send_request.assert_called_once()
        service.append_sent_copy.assert_called_once()
        raw_message = gmail_client.send_raw_message.call_args.args[0]
        self.assertIn(b"From: Sender <user@example.com>", raw_message)
        self.assertIn(b"To: to@example.com", raw_message)
        self.assertIn(b"Bcc: hidden@example.com", raw_message)
        sent_copy = service.append_sent_copy.call_args.args[1]
        self.assertNotIn("Bcc", sent_copy)
        gmail_client.delete_message.assert_called_once_with("gmail-sent-1")
        message = GmailImportMessage.objects.get(import_account=account, gmail_message_id="gmail-sent-1")
        self.assertEqual(message.state, GmailImportMessage.STATE_CLEANED)
        self.assertEqual(message.cleanup_status, GmailImportMessage.STATUS_SUCCESS)
        self.assertEqual(message.target_folder, "Sent")
        self.assertEqual(message.rfc_message_id, response.json()["message_id"])

    @patch("mailops.api.MailboxService")
    def test_mail_send_accepts_multipart_attachments(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        service.send_mail.return_value = "<sent@example.com>"
        attachment = SimpleUploadedFile("report.txt", b"report content", content_type="text/plain")

        response = self.client.post(
            reverse("mailops:api_mail_send"),
            data={
                "to": "Recipient Name <to@example.com>",
                "cc": "copy@example.com, other@example.com",
                "subject": "Status",
                "text_body": "Plain body",
                "attachments": attachment,
            },
            **headers,
        )

        self.assertEqual(response.status_code, 200)
        request = service.send_mail.call_args.args[1]
        self.assertEqual(request.to, ("to@example.com",))
        self.assertEqual(request.cc, ("copy@example.com", "other@example.com"))
        self.assertEqual(len(request.attachments), 1)
        self.assertEqual(request.attachments[0].filename, "report.txt")
        self.assertEqual(request.attachments[0].content_type, "text/plain")
        self.assertEqual(request.attachments[0].content, b"report content")

    @patch("mailops.api.MailboxService")
    def test_mail_send_accepts_forward_source_message(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        service.send_mail.return_value = "<sent@example.com>"

        response = self.client.post(
            reverse("mailops:api_mail_send"),
            data={
                "to": ["to@example.com"],
                "subject": "Forward",
                "text_body": "Forwarded body",
                "forward_source_message": {
                    "folder": "INBOX",
                    "uid": "42",
                    "attachment_ids": ["att_4", "att_3"],
                },
            },
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 200)
        request = service.send_mail.call_args.args[1]
        self.assertEqual(request.forward_source_message.folder, "INBOX")
        self.assertEqual(request.forward_source_message.uid, "42")
        self.assertEqual(request.forward_source_message.attachment_ids, ("att_4", "att_3"))

    @patch("mailops.api.MailboxService")
    def test_mail_send_accepts_multipart_forward_source_message_and_uploads(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        service.send_mail.return_value = "<sent@example.com>"
        attachment = SimpleUploadedFile("manual.txt", b"manual content", content_type="text/plain")

        response = self.client.post(
            reverse("mailops:api_mail_send"),
            data={
                "to": "to@example.com",
                "subject": "Forward",
                "text_body": "Forwarded body",
                "forward_source_message": json.dumps(
                    {
                        "folder": "Archive",
                        "uid": "99",
                        "attachment_ids": ["att_2", "att_1"],
                    }
                ),
                "attachments": attachment,
            },
            **headers,
        )

        self.assertEqual(response.status_code, 200)
        request = service.send_mail.call_args.args[1]
        self.assertEqual(request.forward_source_message.folder, "Archive")
        self.assertEqual(request.forward_source_message.uid, "99")
        self.assertEqual(request.forward_source_message.attachment_ids, ("att_2", "att_1"))
        self.assertEqual(len(request.attachments), 1)
        self.assertEqual(request.attachments[0].filename, "manual.txt")

    @patch("mailops.api.MailboxService")
    def test_mail_send_maps_forward_attachment_input_errors_to_http_400(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        payload = {
            "to": ["to@example.com"],
            "subject": "Forward",
            "text_body": "Forwarded body",
            "forward_source_message": {"folder": "INBOX", "uid": "42", "attachment_ids": ["att_1"]},
        }
        service.send_mail.side_effect = MailForwardAttachmentNotVisibleError("att_1 hidden")
        hidden_response = self.client.post(reverse("mailops:api_mail_send"), data=payload, content_type="application/json", **headers)

        service.send_mail.side_effect = MailForwardAttachmentNotFoundError("att_99 missing")
        missing_payload = {
            **payload,
            "forward_source_message": {"folder": "INBOX", "uid": "42", "attachment_ids": ["att_99"]},
        }
        missing_response = self.client.post(reverse("mailops:api_mail_send"), data=missing_payload, content_type="application/json", **headers)

        self.assertEqual(hidden_response.status_code, 400)
        self.assertEqual(hidden_response.json()["error"], "forward_attachment_not_visible")
        self.assertEqual(missing_response.status_code, 400)
        self.assertEqual(missing_response.json()["error"], "forward_attachment_not_found")

    def test_mail_send_rejects_oversized_multipart_attachments(self):
        headers = self.auth_headers()
        with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=40 * 1024 * 1024):
            too_large = SimpleUploadedFile("large.bin", b"x" * (10 * 1024 * 1024 + 1), content_type="application/octet-stream")
            single_response = self.client.post(
                reverse("mailops:api_mail_send"),
                data={"to": "to@example.com", "subject": "Hi", "text_body": "Body", "attachments": too_large},
                **headers,
            )

            files = [
                SimpleUploadedFile(f"part-{index}.bin", b"x" * (6 * 1024 * 1024), content_type="application/octet-stream")
                for index in range(5)
            ]
            total_response = self.client.post(
                reverse("mailops:api_mail_send"),
                data={"to": "to@example.com", "subject": "Hi", "text_body": "Body", "attachments": files},
                **headers,
            )

        self.assertEqual(single_response.status_code, 400)
        self.assertEqual(single_response.json()["error"], "attachment_too_large")
        self.assertEqual(total_response.status_code, 400)
        self.assertEqual(total_response.json()["error"], "attachments_too_large")

    def test_mail_send_normalizes_display_name_recipients_and_rejects_invalid_addresses(self):
        headers = self.auth_headers()

        invalid_recipient = self.client.post(
            reverse("mailops:api_mail_send"),
            data={"to": ["bad recipient"], "subject": "Hi", "text_body": "Body"},
            content_type="application/json",
            **headers,
        )
        multiple_recipients = self.client.post(
            reverse("mailops:api_mail_send"),
            data={"to": ["One <one@example.com>, Two <two@example.com>"], "subject": "Hi", "text_body": "Body"},
            content_type="application/json",
            **headers,
        )
        malformed_reply_to = self.client.post(
            reverse("mailops:api_mail_send"),
            data={"to": ["to@example.com"], "reply_to": "Reply <reply@example.com", "subject": "Hi", "text_body": "Body"},
            content_type="application/json",
            **headers,
        )

        self.assertEqual(invalid_recipient.status_code, 400)
        self.assertIn("to", invalid_recipient.json())
        self.assertEqual(multiple_recipients.status_code, 400)
        self.assertIn("to", multiple_recipients.json())
        self.assertEqual(malformed_reply_to.status_code, 400)
        self.assertIn("reply_to", malformed_reply_to.json())

    def test_mail_send_requires_token_and_validates_required_fields(self):
        missing_token = self.client.post(reverse("mailops:api_mail_send"), data={}, content_type="application/json")
        headers = self.auth_headers()

        missing_to = self.client.post(
            reverse("mailops:api_mail_send"),
            data={"subject": "Hi", "text_body": "Body"},
            content_type="application/json",
            **headers,
        )
        missing_subject = self.client.post(
            reverse("mailops:api_mail_send"),
            data={"to": ["to@example.com"], "text_body": "Body"},
            content_type="application/json",
            **headers,
        )
        missing_body = self.client.post(
            reverse("mailops:api_mail_send"),
            data={"to": ["to@example.com"], "subject": "Hi"},
            content_type="application/json",
            **headers,
        )

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(missing_token.json()["error"], "not_authenticated")
        self.assertEqual(missing_to.status_code, 400)
        self.assertIn("to", missing_to.json())
        self.assertEqual(missing_subject.status_code, 400)
        self.assertIn("subject", missing_subject.json())
        self.assertEqual(missing_body.status_code, 400)
        self.assertIn("body", missing_body.json())

    @patch("mailops.api.MailboxService")
    def test_mail_send_maps_send_errors(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.send_mail.side_effect = MailSendError("rejected")

        response = self.client.post(
            reverse("mailops:api_mail_send"),
            data={
                "to": ["to@example.com"],
                "subject": "Hi",
                "text_body": "Body",
            },
            content_type="application/json",
            **headers,
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "mail_send_failed")

    def test_schema_and_docs_endpoints_load(self):
        schema = self.client.get(reverse("schema"))
        docs = self.client.get(reverse("swagger-ui"))
        redoc = self.client.get(reverse("redoc"))

        self.assertEqual(schema.status_code, 200)
        self.assertContains(schema, "/api/auth/login")
        self.assertContains(schema, "/api/auth/logout")
        self.assertContains(schema, "/api/mail/messages/delete")
        self.assertContains(schema, "/api/mail/messages/{uid}/delete")
        self.assertContains(schema, "/api/mail/messages/restore")
        self.assertContains(schema, "/api/mail/messages/{uid}/restore")
        self.assertContains(schema, "/api/mail/messages/{uid}/attachments/{attachment_id}")
        self.assertContains(schema, "/api/mail/conversations")
        self.assertContains(schema, "/api/mail/unified-conversations")
        self.assertContains(schema, "/api/mail/index-status")
        self.assertContains(schema, "/api/mail/send")
        self.assertContains(schema, "/api/devices/")
        self.assertContains(schema, "/api/accounts/summaries")
        self.assertContains(schema, "/api/mail/new/")
        self.assertEqual(docs.status_code, 200)
        self.assertEqual(redoc.status_code, 200)

    def test_spectacular_schema_generation_command_runs(self):
        call_command("spectacular", file="/tmp/test-mailadmin-schema.yaml", validate=True)


@override_settings(
    DEVICE_REGISTRATION_SECRET="device-secret",
    MAIL_NOTIFY_HOOK_SECRET="hook-secret",
    MAILBOX_CREDENTIAL_ENCRYPTION_KEY=TEST_ENCRYPTION_KEY,
)
class PushApiTests(TestCase):
    def setUp(self):
        self.account_email = "user@example.com"
        self.password = "mail-secret"

    def auth_headers(self, account_email=None):
        token = create_mailbox_token(account_email or self.account_email, self.password)
        return {
            "Authorization": f"Token {token.key}",
            "X-Device-Registration-Secret": "device-secret",
        }

    def test_register_device_requires_token(self):
        response = self.client.post(
            reverse("mailops:register_device"),
            data={"fcmToken": "token-1"},
            content_type="application/json",
            headers={"X-Device-Registration-Secret": "device-secret"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "not_authenticated")

    def test_register_device_rejects_invalid_secret(self):
        response = self.client.post(
            reverse("mailops:register_device"),
            data={"fcmToken": "token-1"},
            content_type="application/json",
            headers={"Authorization": self.auth_headers()["Authorization"], "X-Device-Registration-Secret": "bad"},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "unauthorized")

    def test_register_device_rejects_token_without_mailbox_credentials(self):
        user = get_user_model().objects.create_user(username="empty@example.com", email="empty@example.com")
        token = Token.objects.create(user=user)

        response = self.client.post(
            reverse("mailops:register_device"),
            data={"fcmToken": "token-1"},
            content_type="application/json",
            headers={"Authorization": f"Token {token.key}", "X-Device-Registration-Secret": "device-secret"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "mailbox_credentials_missing")

    def test_register_device_creates_or_updates_token(self):
        first_seen = timezone.now()
        response = self.client.post(
            reverse("mailops:register_device"),
            data={
                "email": "USER@Example.COM",
                "fcmToken": "token-1",
                "platform": "Android",
                "appVersion": "1.0.0",
            },
            content_type="application/json",
            headers=self.auth_headers(account_email="USER@Example.COM"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["account_email"], "user@example.com")
        device = DeviceRegistration.objects.get(fcm_token="token-1")
        self.assertEqual(device.account_email, "user@example.com")
        self.assertEqual(device.platform, "android")
        self.assertEqual(device.app_version, "1.0.0")
        self.assertTrue(device.enabled)
        self.assertGreaterEqual(device.last_seen_at, first_seen)

        DeviceRegistration.objects.filter(pk=device.pk).update(enabled=False)
        second_response = self.client.post(
            reverse("mailops:register_device"),
            data={"accountEmail": "user@example.com", "fcm_token": "token-1", "platform": "Android", "app_version": "1.0.1"},
            content_type="application/json",
            headers=self.auth_headers(),
        )

        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.json()["created"], False)
        device.refresh_from_db()
        self.assertEqual(device.app_version, "1.0.1")
        self.assertTrue(device.enabled)

    def test_register_device_allows_same_token_for_multiple_normalized_accounts(self):
        first_response = self.client.post(
            reverse("mailops:register_device"),
            data={"account_email": " USER@Example.COM ", "fcmToken": " shared-token "},
            content_type="application/json",
            headers=self.auth_headers(account_email="USER@Example.COM"),
        )
        second_response = self.client.post(
            reverse("mailops:register_device"),
            data={"account_email": "SECOND@Example.COM", "fcmToken": " shared-token "},
            content_type="application/json",
            headers=self.auth_headers(account_email="SECOND@Example.COM"),
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(DeviceRegistration.objects.filter(fcm_token="shared-token").count(), 2)
        self.assertTrue(DeviceRegistration.objects.filter(account_email="user@example.com", fcm_token="shared-token").exists())
        self.assertTrue(DeviceRegistration.objects.filter(account_email="second@example.com", fcm_token="shared-token").exists())

    def test_register_device_rejects_account_email_mismatch(self):
        response = self.client.post(
            reverse("mailops:register_device"),
            data={"account_email": "other@example.com", "fcmToken": "token-1"},
            content_type="application/json",
            headers=self.auth_headers(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "account_email_mismatch")

    @patch("mailops.api.MailboxService")
    def test_accounts_summaries_returns_fcm_linked_accounts_with_inbox_counts(self, service_class):
        headers = self.auth_headers(account_email="USER@Example.COM")
        create_mailbox_token("second@example.com", self.password)
        DeviceRegistration.objects.create(
            account_email="user@example.com",
            fcm_token="shared-token",
            platform=DeviceRegistration.PLATFORM_ANDROID,
            last_seen_at=timezone.now(),
        )
        DeviceRegistration.objects.create(
            account_email="second@example.com",
            fcm_token="shared-token",
            platform=DeviceRegistration.PLATFORM_ANDROID,
            last_seen_at=timezone.now(),
        )
        DeviceRegistration.objects.create(
            account_email="other@example.com",
            fcm_token="other-token",
            platform=DeviceRegistration.PLATFORM_ANDROID,
            last_seen_at=timezone.now(),
        )
        service_class.return_value.get_account_summary.side_effect = [
            MailboxAccountSummary(unread_count=4, important_count=1),
            MailboxAccountSummary(unread_count=0, important_count=2),
        ]

        response = self.client.get(
            reverse("mailops:api_accounts_summaries"),
            {"fcmToken": " shared-token "},
            headers={"Authorization": headers["Authorization"]},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "accounts": [
                    {"account_email": "second@example.com", "display_name": "", "unread_count": 4, "important_count": 1},
                    {"account_email": "user@example.com", "display_name": "", "unread_count": 0, "important_count": 2},
                ]
            },
        )
        called_emails = [call.args[0].email for call in service_class.return_value.get_account_summary.call_args_list]
        self.assertEqual(called_emails, ["second@example.com", "user@example.com"])

    def test_accounts_summaries_requires_valid_linked_fcm_token(self):
        headers = self.auth_headers()
        DeviceRegistration.objects.create(
            account_email="other@example.com",
            fcm_token="other-token",
            platform=DeviceRegistration.PLATFORM_ANDROID,
            last_seen_at=timezone.now(),
        )

        missing = self.client.get(
            reverse("mailops:api_accounts_summaries"),
            headers={"Authorization": headers["Authorization"]},
        )
        unlinked = self.client.get(
            reverse("mailops:api_accounts_summaries"),
            {"fcm_token": "other-token"},
            headers={"Authorization": headers["Authorization"]},
        )

        self.assertEqual(missing.status_code, 400)
        self.assertEqual(unlinked.status_code, 403)
        self.assertEqual(unlinked.json()["error"], "fcm_token_not_linked")

    def test_new_mail_rejects_missing_secret(self):
        response = self.client.post(reverse("mailops:new_mail"), data={}, content_type="application/json")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "unauthorized")

    @patch("mailops.services.get_firebase_app")
    @patch("mailops.services.messaging.send_each_for_multicast")
    def test_new_mail_sends_minimal_push_payload(self, send_multicast, get_app):
        get_app.return_value = Mock()
        send_multicast.return_value = Mock(success_count=1, failure_count=0, responses=[Mock(success=True)])
        DeviceRegistration.objects.create(
            account_email="user@example.com",
            fcm_token="token-1",
            platform=DeviceRegistration.PLATFORM_ANDROID,
            last_seen_at=timezone.now(),
        )

        response = self.client.post(
            reverse("mailops:new_mail"),
            data={
                "accountEmail": "user@example.com",
                "sender": "Sender Name <sender@example.com>",
                "subject": "Hello",
                "body": "This must never be forwarded",
                "messageId": "<m1@example.com>",
                "receivedAt": "2026-04-16T07:00:00Z",
                "folder": "INBOX",
                "uid": "42",
            },
            content_type="application/json",
            headers={"X-Mail-Hook-Secret": "hook-secret"},
        )

        self.assertEqual(response.status_code, 200)
        message = send_multicast.call_args.args[0]
        serialized_payload = json.dumps({"title": message.notification.title, "body": message.notification.body, "data": message.data})
        self.assertNotIn("This must never be forwarded", serialized_payload)
        self.assertEqual(message.notification.title, "Sender Name <sender@example.com>")
        self.assertEqual(message.notification.body, "Hello")
        self.assertEqual(message.data["accountEmail"], "user@example.com")
        self.assertEqual(message.data["folder"], "INBOX")
        self.assertEqual(message.data["uid"], "42")
        self.assertEqual(message.data["messageId"], "<m1@example.com>")
        self.assertEqual(PushNotificationLog.objects.get().status, PushNotificationLog.STATUS_SUCCESS)

    @patch("mailops.services.get_firebase_app")
    @patch("mailops.services.messaging.send_each_for_multicast")
    def test_new_mail_marks_existing_index_stale(self, send_multicast, get_app):
        get_app.return_value = Mock()
        send_multicast.return_value = Mock(success_count=1, failure_count=0, responses=[Mock(success=True)])
        user = get_user_model().objects.create_user(username="user@example.com", email="user@example.com")
        index = MailAccountIndex.objects.create(
            user=user,
            account_email="user@example.com",
            index_status=MailAccountIndex.STATUS_READY,
            last_indexed_at=timezone.now(),
        )
        DeviceRegistration.objects.create(
            account_email="user@example.com",
            fcm_token="token-1",
            platform=DeviceRegistration.PLATFORM_ANDROID,
            last_seen_at=timezone.now(),
        )

        response = self.client.post(
            reverse("mailops:new_mail"),
            data={
                "accountEmail": "user@example.com",
                "sender": "Sender Name <sender@example.com>",
                "subject": "Hello",
                "messageId": "<m1@example.com>",
                "receivedAt": "2026-04-16T07:00:00Z",
                "folder": "INBOX",
                "uid": "42",
            },
            content_type="application/json",
            headers={"X-Mail-Hook-Secret": "hook-secret"},
        )

        self.assertEqual(response.status_code, 200)
        index.refresh_from_db()
        self.assertIsNone(index.last_indexed_at)

    def test_new_mail_without_devices_is_successful_noop(self):
        response = self.client.post(
            reverse("mailops:new_mail"),
            data={
                "accountEmail": "user@example.com",
                "sender": "Sender",
                "subject": "Hello",
                "receivedAt": "2026-04-16T07:00:00Z",
            },
            content_type="application/json",
            headers={"X-Mail-Hook-Secret": "hook-secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "skipped", "deviceCount": 0, "successCount": 0, "failureCount": 0})
        self.assertEqual(PushNotificationLog.objects.get().status, PushNotificationLog.STATUS_SKIPPED)

    @patch("mailops.services.get_firebase_app")
    @patch("mailops.services.messaging.send_each_for_multicast")
    def test_new_mail_disables_invalid_fcm_tokens_without_failing_delivery(self, send_multicast, get_app):
        class FcmError(Exception):
            code = "UNREGISTERED"

        get_app.return_value = Mock()
        send_multicast.return_value = Mock(
            success_count=1,
            failure_count=1,
            responses=[
                Mock(success=False, exception=FcmError("registration-token-not-registered")),
                Mock(success=True),
            ],
        )
        invalid_device = DeviceRegistration.objects.create(
            account_email="user@example.com",
            fcm_token="invalid-token",
            platform=DeviceRegistration.PLATFORM_ANDROID,
            last_seen_at=timezone.now(),
        )
        valid_device = DeviceRegistration.objects.create(
            account_email="user@example.com",
            fcm_token="valid-token",
            platform=DeviceRegistration.PLATFORM_ANDROID,
            last_seen_at=timezone.now(),
        )

        response = self.client.post(
            reverse("mailops:new_mail"),
            data={
                "accountEmail": "user@example.com",
                "sender": "Sender",
                "subject": "Hello",
                "receivedAt": "2026-04-16T07:00:00Z",
            },
            content_type="application/json",
            headers={"X-Mail-Hook-Secret": "hook-secret"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], PushNotificationLog.STATUS_PARTIAL)
        invalid_device.refresh_from_db()
        valid_device.refresh_from_db()
        self.assertFalse(invalid_device.enabled)
        self.assertTrue(valid_device.enabled)
        log = PushNotificationLog.objects.get()
        self.assertEqual(log.status, PushNotificationLog.STATUS_PARTIAL)
        self.assertEqual(log.success_count, 1)
        self.assertEqual(log.failure_count, 1)
