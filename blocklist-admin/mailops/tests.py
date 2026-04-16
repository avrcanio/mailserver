import json
from datetime import datetime, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework.authtoken.models import Token

from mail_integration.exceptions import MailAuthError, MailConnectionError, MailSendError
from mail_integration.schemas import MailAttachmentSummary, MailFolderSummary, MailMessageDetail, MailMessageSummary

from .api import create_mailbox_token
from .models import DeviceRegistration, MailboxTokenCredential, PushNotificationLog


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
        self.assertEqual(credential.mailbox_password, self.password)

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
        self.assertEqual(MailboxTokenCredential.objects.get().mailbox_password, "new-password")

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
        service.list_message_summaries.return_value = [
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
            )
        ]

        response = self.client.get(reverse("mailops:api_mail_messages"), {"folder": "INBOX", "limit": 25}, **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["account_email"], self.account_email)
        self.assertEqual(payload["messages"][0]["uid"], "42")
        self.assertIn("2026-04-16T07:00:00", payload["messages"][0]["date"])
        credentials = service.list_message_summaries.call_args.args[0]
        self.assertEqual(credentials.email, self.account_email)
        self.assertEqual(credentials.password, self.password)
        self.assertEqual(service.list_message_summaries.call_args.kwargs, {"folder": "INBOX", "limit": 25})

    def test_mail_messages_requires_token_and_validates_limit(self):
        missing_token = self.client.get(reverse("mailops:api_mail_messages"))
        headers = self.auth_headers()
        invalid_limit = self.client.get(reverse("mailops:api_mail_messages"), {"limit": 500}, **headers)

        self.assertEqual(missing_token.status_code, 401)
        self.assertEqual(missing_token.json()["error"], "not_authenticated")
        self.assertEqual(invalid_limit.status_code, 400)
        self.assertEqual(invalid_limit.json()["error"], "invalid_limit")

    @patch("mailops.api.MailboxService")
    def test_mail_messages_maps_mail_errors(self, service_class):
        headers = self.auth_headers()
        service_class.return_value.list_message_summaries.side_effect = MailAuthError("bad credentials")

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
                    filename="report.pdf",
                    content_type="application/pdf",
                    size=12345,
                    disposition="attachment",
                ),
            ),
        )

        response = self.client.get(reverse("mailops:api_mail_message_detail", kwargs={"uid": "42"}), {"folder": "INBOX"}, **headers)

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["message"]["uid"], "42")
        self.assertEqual(payload["message"]["text_body"], "Plain body")
        self.assertEqual(payload["message"]["html_body"], "<p>HTML body</p>")
        self.assertEqual(payload["message"]["attachments"][0]["filename"], "report.pdf")
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
    def test_mail_send_calls_service_and_returns_message_id(self, service_class):
        headers = self.auth_headers()
        service = service_class.return_value
        service.send_mail.return_value = "<sent@example.com>"

        response = self.client.post(
            reverse("mailops:api_mail_send"),
            data={
                "to": ["to@example.com"],
                "cc": ["copy@example.com"],
                "bcc": ["hidden@example.com"],
                "reply_to": "reply@example.com",
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
        self.assertContains(schema, "/api/mail/send")
        self.assertContains(schema, "/api/devices/")
        self.assertContains(schema, "/api/mail/new/")
        self.assertEqual(docs.status_code, 200)
        self.assertEqual(redoc.status_code, 200)

    def test_spectacular_schema_generation_command_runs(self):
        call_command("spectacular", file="/tmp/test-mailadmin-schema.yaml", validate=True)


@override_settings(DEVICE_REGISTRATION_SECRET="device-secret", MAIL_NOTIFY_HOOK_SECRET="hook-secret")
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
