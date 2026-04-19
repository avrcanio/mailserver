import importlib
import io
import json
from datetime import datetime, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ImproperlyConfigured
from django.core.files.uploadedfile import SimpleUploadedFile
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

from .api import create_mailbox_token, mailbox_credentials_from_request
from .credential_crypto import (
    ENCRYPTED_VALUE_PREFIX,
    CredentialEncryptionError,
    decrypt_mailbox_password,
    encrypt_mailbox_password,
)
from .mail_indexing import MailIndexService
from .mail_indexing.runner import run_sync_cycle, select_accounts_for_sync
from .mail_indexing.sync import FolderSyncResult, reconcile_recent_missing_messages
from .models import DeviceRegistration, MailAccountIndex, MailConversationIndex, MailMessageIndex, MailboxTokenCredential, PushNotificationLog


TEST_ENCRYPTION_KEY = "DhbKZLv4bil01DI7X2u09Q69vebV7py6A9m9q0gOCfg="


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
        service = service_class.return_value
        service.send_mail.return_value = "<sent@example.com>"

        response = self.client.post(
            reverse("mailops:api_mail_send"),
            data={
                "to": ["Recipient Name <to@example.com>"],
                "cc": ["Copy Person <copy@example.com>"],
                "bcc": ["Hidden Person <hidden@example.com>"],
                "reply_to": "Reply Person <reply@example.com>",
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
        self.assertEqual(request.subject, "Status")
        self.assertEqual(request.text_body, "Plain body")
        self.assertEqual(request.html_body, "<p>HTML body</p>")
        self.assertEqual(request.from_display_name, "Sender Name")
        self.assertEqual(request.attachments, ())

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
