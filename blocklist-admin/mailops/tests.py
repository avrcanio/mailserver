import json
from datetime import datetime, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from mail_integration.exceptions import MailAuthError, MailSendError
from mail_integration.schemas import MailAttachmentSummary, MailMessageDetail, MailMessageSummary

from .models import DeviceRegistration, PushNotificationLog


class MailboxSummariesApiTests(TestCase):
    def setUp(self):
        self.staff_user = get_user_model().objects.create_user(
            username="staff",
            password="secret",
            is_staff=True,
        )

    def test_mailbox_summaries_requires_staff_auth(self):
        response = self.client.post(reverse("mailops:mailbox_summaries"), data={}, content_type="application/json")

        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    @patch("mailops.views.MailboxService")
    def test_mailbox_summaries_returns_service_results(self, service_class):
        self.client.force_login(self.staff_user)
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

        response = self.client.post(
            reverse("mailops:mailbox_summaries"),
            data={
                "accountEmail": "USER@Example.COM",
                "password": "mail-secret",
                "folder": "INBOX",
                "limit": 25,
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["accountEmail"], "user@example.com")
        self.assertEqual(payload["messages"][0]["uid"], "42")
        self.assertEqual(payload["messages"][0]["date"], "2026-04-16T07:00:00+00:00")
        credentials = service.list_message_summaries.call_args.args[0]
        self.assertEqual(credentials.email, "user@example.com")
        self.assertEqual(credentials.password, "mail-secret")
        self.assertEqual(service.list_message_summaries.call_args.kwargs, {"folder": "INBOX", "limit": 25})

    def test_mailbox_summaries_validates_required_credentials(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("mailops:mailbox_summaries"),
            data={"accountEmail": "user@example.com"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "account_email_and_password_required")

    def test_mailbox_summaries_validates_limit(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("mailops:mailbox_summaries"),
            data={"accountEmail": "user@example.com", "password": "secret", "limit": 500},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "invalid_limit")

    @patch("mailops.views.MailboxService")
    def test_mailbox_summaries_maps_mail_errors(self, service_class):
        self.client.force_login(self.staff_user)
        service_class.return_value.list_message_summaries.side_effect = MailAuthError("bad credentials")

        response = self.client.post(
            reverse("mailops:mailbox_summaries"),
            data={"accountEmail": "user@example.com", "password": "bad"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "mail_auth_failed")

    @patch("mailops.views.MailboxService")
    def test_mailbox_detail_returns_service_result(self, service_class):
        self.client.force_login(self.staff_user)
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

        response = self.client.post(
            reverse("mailops:mailbox_detail"),
            data={
                "accountEmail": "user@example.com",
                "password": "mail-secret",
                "folder": "INBOX",
                "uid": "42",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["message"]["uid"], "42")
        self.assertEqual(payload["message"]["textBody"], "Plain body")
        self.assertEqual(payload["message"]["htmlBody"], "<p>HTML body</p>")
        self.assertEqual(payload["message"]["attachments"][0]["filename"], "report.pdf")
        credentials = service.get_message_detail.call_args.args[0]
        self.assertEqual(credentials.email, "user@example.com")
        self.assertEqual(service.get_message_detail.call_args.kwargs, {"folder": "INBOX", "uid": "42"})

    def test_mailbox_detail_requires_uid(self):
        self.client.force_login(self.staff_user)

        response = self.client.post(
            reverse("mailops:mailbox_detail"),
            data={"accountEmail": "user@example.com", "password": "secret"},
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "uid_required")

    @patch("mailops.views.MailboxService")
    def test_mailbox_send_calls_service_and_returns_message_id(self, service_class):
        self.client.force_login(self.staff_user)
        service = service_class.return_value
        service.send_mail.return_value = "<sent@example.com>"

        response = self.client.post(
            reverse("mailops:mailbox_send"),
            data={
                "accountEmail": "sender@example.com",
                "password": "mail-secret",
                "to": ["to@example.com"],
                "cc": ["copy@example.com"],
                "bcc": ["hidden@example.com"],
                "replyTo": "reply@example.com",
                "subject": "Status",
                "textBody": "Plain body",
                "htmlBody": "<p>HTML body</p>",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accountEmail": "sender@example.com", "status": "sent", "messageId": "<sent@example.com>"})
        credentials = service.send_mail.call_args.args[0]
        request = service.send_mail.call_args.args[1]
        self.assertEqual(credentials.email, "sender@example.com")
        self.assertEqual(request.to, ("to@example.com",))
        self.assertEqual(request.cc, ("copy@example.com",))
        self.assertEqual(request.bcc, ("hidden@example.com",))
        self.assertEqual(request.reply_to, "reply@example.com")
        self.assertEqual(request.subject, "Status")
        self.assertEqual(request.text_body, "Plain body")
        self.assertEqual(request.html_body, "<p>HTML body</p>")

    def test_mailbox_send_validates_required_fields(self):
        self.client.force_login(self.staff_user)

        missing_to = self.client.post(
            reverse("mailops:mailbox_send"),
            data={"accountEmail": "sender@example.com", "password": "secret", "subject": "Hi", "textBody": "Body"},
            content_type="application/json",
        )
        missing_subject = self.client.post(
            reverse("mailops:mailbox_send"),
            data={"accountEmail": "sender@example.com", "password": "secret", "to": ["to@example.com"], "textBody": "Body"},
            content_type="application/json",
        )
        missing_body = self.client.post(
            reverse("mailops:mailbox_send"),
            data={"accountEmail": "sender@example.com", "password": "secret", "to": ["to@example.com"], "subject": "Hi"},
            content_type="application/json",
        )

        self.assertEqual(missing_to.status_code, 400)
        self.assertEqual(missing_to.json()["error"], "to_required")
        self.assertEqual(missing_subject.status_code, 400)
        self.assertEqual(missing_subject.json()["error"], "subject_required")
        self.assertEqual(missing_body.status_code, 400)
        self.assertEqual(missing_body.json()["error"], "body_required")

    @patch("mailops.views.MailboxService")
    def test_mailbox_send_maps_send_errors(self, service_class):
        self.client.force_login(self.staff_user)
        service_class.return_value.send_mail.side_effect = MailSendError("rejected")

        response = self.client.post(
            reverse("mailops:mailbox_send"),
            data={
                "accountEmail": "sender@example.com",
                "password": "secret",
                "to": ["to@example.com"],
                "subject": "Hi",
                "textBody": "Body",
            },
            content_type="application/json",
        )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["error"], "mail_send_failed")


@override_settings(DEVICE_REGISTRATION_SECRET="device-secret", MAIL_NOTIFY_HOOK_SECRET="hook-secret")
class PushApiTests(TestCase):
    def test_register_device_rejects_missing_secret(self):
        response = self.client.post(reverse("mailops:register_device"), data={}, content_type="application/json")

        self.assertEqual(response.status_code, 401)

    def test_register_device_creates_or_updates_token(self):
        response = self.client.post(
            reverse("mailops:register_device"),
            data={
                "email": "USER@Example.COM",
                "fcmToken": "token-1",
                "platform": "Android",
                "appVersion": "1.0.0",
            },
            content_type="application/json",
            headers={"X-Device-Registration-Secret": "device-secret"},
        )

        self.assertEqual(response.status_code, 200)
        device = DeviceRegistration.objects.get(fcm_token="token-1")
        self.assertEqual(device.account_email, "user@example.com")
        self.assertEqual(device.platform, "android")
        self.assertTrue(device.enabled)

    def test_new_mail_rejects_missing_secret(self):
        response = self.client.post(reverse("mailops:new_mail"), data={}, content_type="application/json")

        self.assertEqual(response.status_code, 401)

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
        self.assertEqual(PushNotificationLog.objects.get().status, PushNotificationLog.STATUS_SUCCESS)
