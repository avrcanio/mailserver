import json
import importlib
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

from mail_integration.exceptions import MailAttachmentNotFoundError, MailAuthError, MailConnectionError, MailInvalidOperationError, MailSendError
from mail_integration.schemas import (
    MailAttachmentContent,
    MailAttachmentSummary,
    MailFolderSummary,
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
from .models import DeviceRegistration, MailboxTokenCredential, PushNotificationLog


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
        self.assertEqual(response.json()["folders"][0], {"name": "INBOX", "delimiter": "/", "flags": ["HasNoChildren"]})
        credentials = service_class.return_value.list_folders.call_args.args[0]
        self.assertEqual(credentials.email, self.account_email)
        self.assertEqual(credentials.password, self.password)

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
                ),
            ),
        )

        response = self.client.get(reverse("mailops:api_mail_message_detail", kwargs={"uid": "42"}), {"folder": "INBOX"}, **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["message"]["uid"], "42")
        self.assertEqual(payload["message"]["text_body"], "Plain body")
        self.assertEqual(payload["message"]["html_body"], "<p>HTML body</p>")
        self.assertEqual(payload["message"]["attachments"][0]["id"], "att_1")
        self.assertEqual(payload["message"]["attachments"][0]["filename"], "report.pdf")
        self.assertFalse(payload["message"]["attachments"][0]["is_inline"])
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
        self.assertContains(schema, "/api/mail/send")
        self.assertContains(schema, "/api/devices/")
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

    def test_register_device_rejects_account_email_mismatch(self):
        response = self.client.post(
            reverse("mailops:register_device"),
            data={"account_email": "other@example.com", "fcmToken": "token-1"},
            content_type="application/json",
            headers=self.auth_headers(),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"], "account_email_mismatch")

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
