import imaplib
import socket
import smtplib
from email.message import EmailMessage
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from .exceptions import MailAuthError, MailConnectionError, MailProtocolError, MailSendError, MailTimeoutError
from .imap_client import ImapClient
from .mailbox_service import MailboxService
from .schemas import MailboxCredentials, SendMailRequest
from .smtp_client import SmtpClient


@override_settings(
    MAIL_IMAP_HOST="mail.finestar.test",
    MAIL_IMAP_PORT=993,
    MAIL_IMAP_USE_SSL=True,
    MAIL_SMTP_HOST="mail.finestar.test",
    MAIL_SMTP_PORT=587,
    MAIL_SMTP_USE_STARTTLS=True,
    MAIL_CLIENT_TIMEOUT_SECONDS=15,
)
class ImapClientTests(SimpleTestCase):
    def test_login_logout_lifecycle(self):
        connection = Mock()
        connection.login.return_value = ("OK", [b"Logged in"])

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection) as imap_ssl:
            client = ImapClient().connect().login(MailboxCredentials("user@example.com", "secret"))
            client.logout()

        imap_ssl.assert_called_once()
        connection.login.assert_called_once_with("user@example.com", "secret")
        connection.logout.assert_called_once()

    def test_list_folders_maps_raw_imap_folder_lines(self):
        connection = Mock()
        connection.list.return_value = (
            "OK",
            [
                b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasChildren \\Noselect) "/" "Archive"',
                b'(\\HasNoChildren \\Sent) NIL Sent',
                b'(\\HasNoChildren \\Junk) "/" "Junk Mail"',
            ],
        )

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            folders = ImapClient().connect().list_folders()

        self.assertEqual(folders[0].name, "INBOX")
        self.assertEqual(folders[0].delimiter, "/")
        self.assertEqual(folders[0].flags, ("HasNoChildren",))
        self.assertEqual(folders[1].flags, ("HasChildren", "Noselect"))
        self.assertEqual(folders[2].name, "Sent")
        self.assertIsNone(folders[2].delimiter)
        self.assertEqual(folders[2].flags, ("HasNoChildren", "Sent"))
        self.assertEqual(folders[3].name, "Junk Mail")

    def test_list_folders_rejects_malformed_imap_lines(self):
        connection = Mock()
        connection.list.return_value = ("OK", [b"not a list response"])

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(MailProtocolError):
                ImapClient().connect().list_folders()

    def test_fetch_message_summaries_parses_metadata_and_headers(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"2"])
        connection.uid.side_effect = [
            ("OK", [b"101 102"]),
            (
                "OK",
                [
                    (
                        b"102 (UID 102 FLAGS (\\Seen) RFC822.SIZE 1234 BODY[HEADER.FIELDS ...] {180}",
                        (
                            b"Subject: =?utf-8?q?Hello_=C4=8Cakovec?=\r\n"
                            b"From: Sender <sender@example.com>\r\n"
                            b"To: User <user@example.com>\r\n"
                            b"Cc: Copy <copy@example.com>\r\n"
                            b"Date: Thu, 16 Apr 2026 07:00:00 +0000\r\n"
                            b"Message-ID: <m1@example.com>\r\n\r\n"
                        ),
                    )
                ],
            ),
            (
                "OK",
                [
                    (
                        b"101 (UID 101 FLAGS () RFC822.SIZE 99 BODY[HEADER.FIELDS ...] {120}",
                        b"Subject: Older\r\nFrom: old@example.com\r\n\r\n",
                    )
                ],
            ),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            summaries = ImapClient().connect().fetch_message_summaries(folder="Archive", limit=2)

        self.assertEqual([summary.uid for summary in summaries], ["102", "101"])
        self.assertEqual(summaries[0].folder, "Archive")
        self.assertEqual(summaries[0].subject, "Hello Čakovec")
        self.assertEqual(summaries[0].sender, "Sender <sender@example.com>")
        self.assertEqual(summaries[0].to, ("user@example.com",))
        self.assertEqual(summaries[0].cc, ("copy@example.com",))
        self.assertEqual(summaries[0].flags, ("Seen",))
        self.assertEqual(summaries[0].size, 1234)
        self.assertEqual(summaries[0].message_id, "<m1@example.com>")
        connection.select.assert_called_once_with("Archive", readonly=True)

    def test_fetch_message_summaries_respects_zero_limit_without_fetching_messages(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"2"])

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            summaries = ImapClient().connect().fetch_message_summaries(folder="INBOX", limit=0)

        self.assertEqual(summaries, [])
        connection.uid.assert_not_called()

    def test_fetch_message_detail_extracts_bodies_and_attachment_metadata(self):
        raw_message = _raw_detail_message(text_body="Plain body", html_body="<p><strong>HTML body</strong></p>", attach=True)
        connection = Mock()
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.return_value = (
            "OK",
            [(b"7 (UID 7 FLAGS (\\Seen \\Answered) RFC822.SIZE 2048 RFC822 {999}", raw_message)],
        )

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            detail = ImapClient().connect().fetch_message_detail("INBOX", "7")

        self.assertEqual(detail.uid, "7")
        self.assertEqual(detail.subject, "Message detail")
        self.assertIn("Plain body", detail.text_body)
        self.assertIn("<strong>HTML body</strong>", detail.html_body)
        self.assertEqual(detail.attachments[0].filename, "report.txt")
        self.assertEqual(detail.attachments[0].content_type, "text/plain")
        self.assertEqual(detail.attachments[0].disposition, "attachment")
        connection.select.assert_called_once_with("INBOX", readonly=True)
        connection.uid.assert_called_once_with("fetch", "7", "(FLAGS RFC822.SIZE RFC822)")

    def test_fetch_message_detail_extracts_plain_text_only(self):
        detail = _detail_from_raw_message(_raw_detail_message(text_body="Plain only"))

        self.assertIn("Plain only", detail.text_body)
        self.assertEqual(detail.html_body, "")
        self.assertEqual(detail.attachments, ())

    def test_fetch_message_detail_extracts_html_only(self):
        detail = _detail_from_raw_message(_raw_detail_message(html_body="<p>HTML only</p>"))

        self.assertEqual(detail.text_body, "")
        self.assertIn("<p>HTML only</p>", detail.html_body)
        self.assertEqual(detail.attachments, ())

    def test_fetch_message_detail_extracts_multipart_alternative(self):
        detail = _detail_from_raw_message(_raw_detail_message(text_body="Plain alt", html_body="<p>HTML alt</p>"))

        self.assertIn("Plain alt", detail.text_body)
        self.assertIn("<p>HTML alt</p>", detail.html_body)
        self.assertEqual(detail.attachments, ())

    def test_auth_timeout_connection_and_protocol_errors_are_normalized(self):
        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", side_effect=socket.timeout):
            with self.assertRaises(MailTimeoutError):
                ImapClient().connect()

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", side_effect=OSError("refused")):
            with self.assertRaises(MailConnectionError):
                ImapClient().connect()

        connection = Mock()
        connection.login.side_effect = imaplib.IMAP4.error("bad credentials")
        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(MailAuthError):
                ImapClient().connect().login(MailboxCredentials("user@example.com", "bad"))

        connection = Mock()
        connection.list.return_value = ("NO", [b"not allowed"])
        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(MailProtocolError):
                ImapClient().connect().list_folders()

        connection = Mock()
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.return_value = ("OK", [b"not a fetch tuple"])
        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(MailProtocolError):
                ImapClient().connect().fetch_message_detail("INBOX", "7")

        connection = Mock()
        connection.select.side_effect = OSError("lost network")
        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(MailConnectionError):
                ImapClient().connect().fetch_message_detail("INBOX", "7")


@override_settings(
    MAIL_SMTP_HOST="mail.finestar.test",
    MAIL_SMTP_PORT=587,
    MAIL_SMTP_USE_STARTTLS=True,
    MAIL_CLIENT_TIMEOUT_SECONDS=15,
)
class SmtpClientTests(SimpleTestCase):
    def test_send_mail_builds_message_and_recipient_envelope(self):
        connection = Mock()

        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            client = SmtpClient().connect().login(MailboxCredentials("sender@example.com", "secret"))
            message_id = client.send_mail(
                MailboxCredentials("sender@example.com", "secret"),
                SendMailRequest(
                    to=("to@example.com",),
                    cc=("cc@example.com",),
                    bcc=("bcc@example.com",),
                    reply_to="reply@example.com",
                    subject="Status",
                    text_body="Plain body",
                    html_body="<p>HTML body</p>",
                ),
            )

        connection.starttls.assert_called_once()
        connection.login.assert_called_once_with("sender@example.com", "secret")
        sent_message = connection.send_message.call_args.args[0]
        self.assertEqual(sent_message["From"], "sender@example.com")
        self.assertEqual(sent_message["To"], "to@example.com")
        self.assertEqual(sent_message["Cc"], "cc@example.com")
        self.assertEqual(sent_message["Reply-To"], "reply@example.com")
        self.assertNotIn("Bcc", sent_message)
        self.assertEqual(connection.send_message.call_args.kwargs["to_addrs"], ["to@example.com", "cc@example.com", "bcc@example.com"])
        self.assertEqual(message_id, sent_message["Message-ID"])

    def test_smtp_errors_are_normalized(self):
        with patch("mail_integration.smtp_client.smtplib.SMTP", side_effect=socket.timeout):
            with self.assertRaises(MailTimeoutError):
                SmtpClient().connect()

        with patch("mail_integration.smtp_client.smtplib.SMTP", side_effect=OSError("refused")):
            with self.assertRaises(MailConnectionError):
                SmtpClient().connect()

        connection = Mock()
        connection.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            with self.assertRaises(MailAuthError):
                SmtpClient().connect().login(MailboxCredentials("sender@example.com", "bad"))

        connection = Mock()
        connection.send_message.side_effect = smtplib.SMTPRecipientsRefused({})
        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            with self.assertRaises(MailSendError):
                SmtpClient().connect().send_mail(
                    MailboxCredentials("sender@example.com", "secret"),
                    SendMailRequest(to=("to@example.com",), subject="Hi", text_body="Body"),
                )


class MailboxServiceTests(SimpleTestCase):
    def test_service_uses_stable_internal_client_interfaces(self):
        credentials = MailboxCredentials("user@example.com", "secret")
        send_request = SendMailRequest(to=("to@example.com",), subject="Hi", text_body="Body")
        imap_client = _context_client()
        smtp_client = _context_client()
        imap_client.__enter__.return_value.list_folders.return_value = ["INBOX"]
        smtp_client.__enter__.return_value.send_mail.return_value = "<m1@example.com>"

        service = MailboxService(imap_client_factory=lambda: imap_client, smtp_client_factory=lambda: smtp_client)

        self.assertEqual(service.list_folders(credentials), ["INBOX"])
        self.assertEqual(service.send_mail(credentials, send_request), "<m1@example.com>")
        imap_client.__enter__.return_value.login.assert_called_once_with(credentials)
        smtp_client.__enter__.return_value.login.assert_called_once_with(credentials)

    def test_service_read_methods_route_to_imap_client(self):
        credentials = MailboxCredentials("user@example.com", "secret")
        imap_client = _context_client()
        entered = imap_client.__enter__.return_value
        entered.list_folders.return_value = ["INBOX"]
        entered.fetch_message_summaries.return_value = ["summary"]
        entered.fetch_message_detail.return_value = "detail"

        service = MailboxService(imap_client_factory=lambda: imap_client)

        self.assertEqual(service.list_folders(credentials), ["INBOX"])
        self.assertEqual(service.list_message_summaries(credentials, folder="Archive", limit=10), ["summary"])
        self.assertEqual(service.get_message_detail(credentials, folder="Archive", uid="99"), "detail")
        self.assertEqual(entered.login.call_count, 3)
        entered.login.assert_called_with(credentials)
        entered.fetch_message_summaries.assert_called_once_with(folder="Archive", limit=10)
        entered.fetch_message_detail.assert_called_once_with(folder="Archive", uid="99")


def _raw_detail_message(text_body="", html_body="", attach=False):
    message = EmailMessage()
    message["Subject"] = "Message detail"
    message["From"] = "Sender <sender@example.com>"
    message["To"] = "User <user@example.com>"
    message["Date"] = "Thu, 16 Apr 2026 07:00:00 +0000"
    message["Message-ID"] = "<detail@example.com>"
    if text_body and html_body:
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
    elif html_body:
        message.add_alternative(html_body, subtype="html")
    else:
        message.set_content(text_body)
    if attach:
        message.add_attachment(b"report content", maintype="text", subtype="plain", filename="report.txt")
    return bytes(message)


def _detail_from_raw_message(raw_message):
    connection = Mock()
    connection.select.return_value = ("OK", [b"1"])
    connection.uid.return_value = (
        "OK",
        [(b"7 (UID 7 FLAGS (\\Seen) RFC822.SIZE 2048 RFC822 {999}", raw_message)],
    )
    with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
        return ImapClient().connect().fetch_message_detail("INBOX", "7")


def _context_client():
    client = Mock()
    entered = Mock()
    client.__enter__ = Mock(return_value=entered)
    client.__exit__ = Mock(return_value=None)
    return client
