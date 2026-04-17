import imaplib
import socket
import smtplib
import ssl
from email.message import EmailMessage
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from .exceptions import (
    MailAttachmentNotFoundError,
    MailAuthError,
    MailConnectionError,
    MailForwardAttachmentNotFoundError,
    MailForwardAttachmentNotVisibleError,
    MailInvalidOperationError,
    MailProtocolError,
    MailSendError,
    MailTimeoutError,
)
from .imap_client import ImapClient
from .mailbox_service import MailboxService
from .schemas import (
    ForwardSourceMessage,
    MailAttachmentContent,
    MailAttachmentSummary,
    MailboxAccountSummary,
    MailboxCredentials,
    MailConversationParticipant,
    MailConversationSummary,
    MailConversationSummaryPage,
    SendMailAttachment,
    SendMailRequest,
)
from .smtp_client import SmtpClient, build_email_message


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
        self.assertEqual(folders[0].path, "INBOX")
        self.assertEqual(folders[0].display_name, "INBOX")
        self.assertIsNone(folders[0].parent_path)
        self.assertEqual(folders[0].depth, 0)
        self.assertTrue(folders[0].selectable)
        self.assertEqual(folders[0].delimiter, "/")
        self.assertEqual(folders[0].flags, ("HasNoChildren",))
        self.assertEqual(folders[1].flags, ("HasChildren", "Noselect"))
        self.assertFalse(folders[1].selectable)
        self.assertEqual(folders[2].name, "Sent")
        self.assertIsNone(folders[2].delimiter)
        self.assertEqual(folders[2].flags, ("HasNoChildren", "Sent"))
        self.assertEqual(folders[3].name, "Junk Mail")

    def test_list_folders_preserves_nested_inbox_paths(self):
        connection = Mock()
        connection.list.return_value = (
            "OK",
            [
                b'(\\HasChildren) "/" "INBOX"',
                b'(\\HasNoChildren) "/" "INBOX/Clients"',
                b'(\\HasChildren) "/" "INBOX/Invoices"',
                b'(\\HasNoChildren) "/" "INBOX/Invoices/2026"',
            ],
        )

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            folders = ImapClient().connect().list_folders()

        self.assertEqual([folder.name for folder in folders], ["INBOX", "INBOX/Clients", "INBOX/Invoices", "INBOX/Invoices/2026"])
        self.assertEqual(folders[1].path, "INBOX/Clients")
        self.assertEqual(folders[1].display_name, "Clients")
        self.assertEqual(folders[1].parent_path, "INBOX")
        self.assertEqual(folders[1].depth, 1)
        self.assertTrue(folders[1].selectable)
        self.assertEqual(folders[3].path, "INBOX/Invoices/2026")
        self.assertEqual(folders[3].display_name, "2026")
        self.assertEqual(folders[3].parent_path, "INBOX/Invoices")
        self.assertEqual(folders[3].depth, 2)

    def test_list_folders_uses_imap_delimiter_for_hierarchy(self):
        connection = Mock()
        connection.list.return_value = (
            "OK",
            [
                b'(\\HasChildren) "." "INBOX"',
                b'(\\HasNoChildren) "." "INBOX.Clients"',
            ],
        )

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            folders = ImapClient().connect().list_folders()

        self.assertEqual(folders[1].path, "INBOX.Clients")
        self.assertEqual(folders[1].display_name, "Clients")
        self.assertEqual(folders[1].parent_path, "INBOX")
        self.assertEqual(folders[1].depth, 1)

    def test_list_folders_decodes_modified_utf7_mailbox_names(self):
        connection = Mock()
        connection.list.return_value = (
            "OK",
            [
                b'(\\HasNoChildren) "." "Nabava.TSH &AQw-akovec"',
                b'(\\HasNoChildren) "." "Ponude.Ante Sladi&AQc-"',
            ],
        )

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            folders = ImapClient().connect().list_folders()

        self.assertEqual(folders[0].name, "Nabava.TSH Čakovec")
        self.assertEqual(folders[0].path, "Nabava.TSH Čakovec")
        self.assertEqual(folders[0].display_name, "TSH Čakovec")
        self.assertEqual(folders[0].parent_path, "Nabava")
        self.assertEqual(folders[1].name, "Ponude.Ante Sladić")
        self.assertEqual(folders[1].display_name, "Ante Sladić")

    def test_fetch_message_summary_encodes_unicode_folder_for_imap_select(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.side_effect = [
            ("OK", [b"101"]),
            ("OK", [(b"101 (UID 101 FLAGS () RFC822.SIZE 10 BODY[HEADER.FIELDS ...] {20}", b"Subject: TSH\r\n\r\n")]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            ImapClient().connect().fetch_message_summaries(folder="Nabava.TSH Čakovec", limit=1)

        connection.select.assert_called_once_with(b'"Nabava.TSH &AQw-akovec"', readonly=True)

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
                        (
                            b'102 (UID 102 FLAGS (\\Seen) RFC822.SIZE 1234 BODYSTRUCTURE '
                            b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1 NIL NIL NIL)'
                            b'("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 123 NIL '
                            b'("ATTACHMENT" ("FILENAME" "report.pdf")) NIL) "MIXED") BODY[HEADER.FIELDS ...] {180}'
                        ),
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
        self.assertTrue(summaries[0].has_attachments)
        self.assertTrue(summaries[0].has_visible_attachments)
        self.assertFalse(summaries[1].has_attachments)
        self.assertFalse(summaries[1].has_visible_attachments)
        connection.select.assert_called_once_with(b'"Archive"', readonly=True)

    def test_fetch_message_summaries_respects_zero_limit_without_fetching_messages(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"2"])

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            summaries = ImapClient().connect().fetch_message_summaries(folder="INBOX", limit=0)

        self.assertEqual(summaries, [])
        connection.uid.assert_not_called()

    def test_fetch_account_summary_counts_inbox_unseen_and_flagged(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"5"])
        connection.uid.side_effect = [
            ("OK", [b"101 103 105"]),
            ("OK", [b"102 105"]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            summary = ImapClient().connect().fetch_account_summary()

        self.assertEqual(summary.unread_count, 3)
        self.assertEqual(summary.important_count, 2)
        connection.select.assert_called_once_with(b'"INBOX"', readonly=True)
        connection.uid.assert_any_call("search", None, "UNSEEN")
        connection.uid.assert_any_call("search", None, "FLAGGED")

    def test_fetch_message_summary_page_uses_before_uid_cursor(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"5"])
        connection.uid.side_effect = [
            ("OK", [b"101 102 103 104 105"]),
            ("OK", [(b"102 (UID 102 FLAGS () RFC822.SIZE 20 BODY[HEADER.FIELDS ...] {20}", b"Subject: 102\r\n\r\n")]),
            ("OK", [(b"101 (UID 101 FLAGS () RFC822.SIZE 10 BODY[HEADER.FIELDS ...] {20}", b"Subject: 101\r\n\r\n")]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            page = ImapClient().connect().fetch_message_summary_page(folder="INBOX", limit=2, before_uid="103")

        self.assertEqual([summary.uid for summary in page.messages], ["102", "101"])
        self.assertFalse(page.has_more)
        self.assertIsNone(page.next_before_uid)
        connection.uid.assert_any_call("search", None, "UNDELETED")
        connection.uid.assert_any_call("fetch", b"102", "(FLAGS RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO CC DATE MESSAGE-ID)])")
        connection.uid.assert_any_call("fetch", b"101", "(FLAGS RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO CC DATE MESSAGE-ID)])")

    def test_fetch_message_summaries_detects_inline_cid_parts_without_visible_attachments(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.side_effect = [
            ("OK", [b"200"]),
            (
                "OK",
                [
                    (
                        (
                            b'200 (UID 200 FLAGS () RFC822.SIZE 321 BODYSTRUCTURE '
                            b'(("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1 NIL NIL NIL)'
                            b'("IMAGE" "PNG" ("NAME" "logo.png") "<logo>" NIL "BASE64" 20 NIL '
                            b'("INLINE" ("FILENAME" "logo.png")) NIL) "RELATED") BODY[HEADER.FIELDS ...] {20}'
                        ),
                        b"Subject: Inline\r\n\r\n",
                    )
                ],
            ),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            summaries = ImapClient().connect().fetch_message_summaries(folder="INBOX", limit=1)

        self.assertTrue(summaries[0].has_attachments)
        self.assertFalse(summaries[0].has_visible_attachments)

    def test_fetch_message_summaries_detects_mixed_inline_and_visible_attachments(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.side_effect = [
            ("OK", [b"201"]),
            (
                "OK",
                [
                    (
                        (
                            b'201 (UID 201 FLAGS () RFC822.SIZE 654 BODYSTRUCTURE '
                            b'(("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1 NIL NIL NIL)'
                            b'("IMAGE" "PNG" ("NAME" "logo.png") "<logo>" NIL "BASE64" 20 NIL '
                            b'("INLINE" ("FILENAME" "logo.png")) NIL)'
                            b'("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 100 NIL '
                            b'("ATTACHMENT" ("FILENAME" "report.pdf")) NIL) "MIXED") BODY[HEADER.FIELDS ...] {20}'
                        ),
                        b"Subject: Mixed\r\n\r\n",
                    )
                ],
            ),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            summaries = ImapClient().connect().fetch_message_summaries(folder="INBOX", limit=1)

        self.assertTrue(summaries[0].has_attachments)
        self.assertTrue(summaries[0].has_visible_attachments)

    def test_fetch_message_summaries_refines_duplicate_inline_image_visibility(self):
        raw_message = _raw_duplicate_signature_image_message()
        connection = Mock()
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.side_effect = [
            ("OK", [b"202"]),
            (
                "OK",
                [
                    (
                        (
                            b'202 (UID 202 FLAGS () RFC822.SIZE 1000 BODYSTRUCTURE '
                            b'(("TEXT" "HTML" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1 NIL NIL NIL)'
                            b'("IMAGE" "PNG" ("NAME" "Outlook-logo.png") "<unreferenced-logo>" NIL "BASE64" 20 NIL '
                            b'("INLINE" ("FILENAME" "Outlook-logo.png")) NIL)'
                            b'("IMAGE" "PNG" ("NAME" "Outlook-logo.png") "<referenced-logo>" NIL "BASE64" 20 NIL '
                            b'("INLINE" ("FILENAME" "Outlook-logo.png")) NIL)'
                            b'("IMAGE" "PNG" ("NAME" "Outlook-logo.png") "<duplicate-logo>" NIL "BASE64" 20 NIL '
                            b'("ATTACHMENT" ("FILENAME" "Outlook-logo.png")) NIL) "MIXED") BODY[HEADER.FIELDS ...] {20}'
                        ),
                        b"Subject: Signature\r\n\r\n",
                    )
                ],
            ),
            ("OK", [(b"202 (UID 202 FLAGS () RFC822.SIZE 1000 RFC822 {999}", raw_message)]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            summaries = ImapClient().connect().fetch_message_summaries(folder="INBOX", limit=1)

        self.assertTrue(summaries[0].has_attachments)
        self.assertFalse(summaries[0].has_visible_attachments)

    def test_fetch_message_summary_page_returns_pagination_metadata(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"5"])
        connection.uid.side_effect = [
            ("OK", [b"101 102 103 104 105"]),
            ("OK", [(b"105 (UID 105 FLAGS () RFC822.SIZE 50 BODY[HEADER.FIELDS ...] {20}", b"Subject: 105\r\n\r\n")]),
            ("OK", [(b"104 (UID 104 FLAGS () RFC822.SIZE 40 BODY[HEADER.FIELDS ...] {20}", b"Subject: 104\r\n\r\n")]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            page = ImapClient().connect().fetch_message_summary_page(folder="INBOX", limit=2)

        self.assertEqual([summary.uid for summary in page.messages], ["105", "104"])
        self.assertTrue(page.has_more)
        self.assertEqual(page.next_before_uid, "104")

    def test_fetch_conversation_page_groups_by_references_and_uses_structured_participants(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"2"])
        connection.uid.side_effect = [
            ("OK", [b"101 102"]),
            _conversation_fetch(
                "102",
                subject="Re: Project",
                sender="Bob Builder <bob@example.com>",
                to="Alice Example <alice@example.com>",
                date="Thu, 16 Apr 2026 08:00:00 +0000",
                message_id="<reply@example.com>",
                in_reply_to="<root@example.com>",
                references="<root@example.com>",
                flags="",
                attach=True,
            ),
            _conversation_fetch(
                "101",
                subject="Project",
                sender="Alice Example <alice@example.com>",
                to="User <user@example.com>",
                date="Thu, 16 Apr 2026 07:00:00 +0000",
                message_id="<root@example.com>",
                flags="\\Seen",
            ),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            page = ImapClient().connect().fetch_conversation_page(folder="INBOX", limit=10)

        self.assertEqual(len(page.conversations), 1)
        conversation = page.conversations[0]
        self.assertEqual(conversation.root_message.uid, "101")
        self.assertEqual([reply.uid for reply in conversation.replies], ["102"])
        self.assertEqual(conversation.message_count, 2)
        self.assertEqual(conversation.reply_count, 1)
        self.assertTrue(conversation.has_unread)
        self.assertTrue(conversation.has_attachments)
        self.assertTrue(conversation.has_visible_attachments)
        self.assertEqual(
            conversation.participants,
            (
                MailConversationParticipant(name="Alice Example", email="alice@example.com"),
                MailConversationParticipant(name="", email="user@example.com"),
                MailConversationParticipant(name="Bob Builder", email="bob@example.com"),
            ),
        )

    def test_fetch_conversation_page_falls_back_to_earliest_date_then_uid_for_incomplete_chain_root(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"2"])
        connection.uid.side_effect = [
            ("OK", [b"110 111"]),
            _conversation_fetch(
                "111",
                subject="Re: Missing parent",
                sender="Reply <reply@example.com>",
                to="user@example.com",
                date="Thu, 16 Apr 2026 09:00:00 +0000",
                message_id="<reply@example.com>",
                in_reply_to="<missing@example.com>",
                references="<missing@example.com>",
            ),
            _conversation_fetch(
                "110",
                subject="Re: Missing parent",
                sender="Earlier <earlier@example.com>",
                to="user@example.com",
                date="Thu, 16 Apr 2026 08:00:00 +0000",
                message_id="<earlier@example.com>",
                in_reply_to="<missing@example.com>",
                references="<missing@example.com>",
            ),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            page = ImapClient().connect().fetch_conversation_page(folder="INBOX", limit=10)

        self.assertEqual(len(page.conversations), 1)
        self.assertEqual(page.conversations[0].root_message.uid, "110")

        connection = Mock()
        connection.select.return_value = ("OK", [b"2"])
        connection.uid.side_effect = [
            ("OK", [b"120 121"]),
            _conversation_fetch(
                "121",
                subject="Re: Missing dates",
                sender="High <high@example.com>",
                to="user@example.com",
                message_id="<high@example.com>",
                in_reply_to="<missing@example.com>",
                references="<missing@example.com>",
            ),
            _conversation_fetch(
                "120",
                subject="Re: Missing dates",
                sender="Low <low@example.com>",
                to="user@example.com",
                message_id="<low@example.com>",
                in_reply_to="<missing@example.com>",
                references="<missing@example.com>",
            ),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            page = ImapClient().connect().fetch_conversation_page(folder="INBOX", limit=10)

        self.assertEqual(len(page.conversations), 1)
        self.assertEqual(page.conversations[0].root_message.uid, "120")

    def test_fetch_conversation_page_uses_subject_fallback_only_for_orphan_messages(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"3"])
        connection.uid.side_effect = [
            ("OK", [b"201 202 203"]),
            _conversation_fetch("203", subject="Re: Report", sender="Id <id@example.com>", to="user@example.com", message_id="<id@example.com>"),
            _conversation_fetch("202", subject="Fwd: Re: Report", sender="No Id 2 <orphan2@example.com>", to="user@example.com"),
            _conversation_fetch("201", subject="Report", sender="No Id 1 <orphan1@example.com>", to="user@example.com"),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            page = ImapClient().connect().fetch_conversation_page(folder="INBOX", limit=10)

        conversation_uids = [sorted([conversation.root_message.uid, *(reply.uid for reply in conversation.replies)]) for conversation in page.conversations]
        self.assertIn(["201", "202"], conversation_uids)
        self.assertIn(["203"], conversation_uids)

    def test_fetch_conversation_page_scans_more_messages_than_returned_conversation_limit(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"3"])
        connection.uid.side_effect = [
            ("OK", [b"301 302 303"]),
            _conversation_fetch("303", subject="Newest", sender="a@example.com", to="user@example.com", message_id="<303@example.com>"),
            _conversation_fetch("302", subject="Middle", sender="b@example.com", to="user@example.com", message_id="<302@example.com>"),
            _conversation_fetch("301", subject="Oldest", sender="c@example.com", to="user@example.com", message_id="<301@example.com>"),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            page = ImapClient().connect().fetch_conversation_page(folder="INBOX", limit=1)

        self.assertEqual(len(page.conversations), 1)
        self.assertEqual(connection.uid.call_count, 4)

    def test_fetch_message_summary_page_empty_when_no_older_messages_remain(self):
        connection = Mock()
        connection.select.return_value = ("OK", [b"2"])
        connection.uid.return_value = ("OK", [b"101 102"])

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            page = ImapClient().connect().fetch_message_summary_page(folder="INBOX", limit=2, before_uid="101")

        self.assertEqual(page.messages, ())
        self.assertFalse(page.has_more)
        self.assertIsNone(page.next_before_uid)

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
        self.assertEqual(detail.attachments[0].id, "att_1")
        self.assertEqual(detail.attachments[0].filename, "report.txt")
        self.assertEqual(detail.attachments[0].content_type, "text/plain")
        self.assertEqual(detail.attachments[0].disposition, "attachment")
        self.assertFalse(detail.attachments[0].is_inline)
        self.assertEqual(detail.attachments[0].content_id, "")
        self.assertTrue(detail.has_visible_attachments)
        connection.select.assert_called_once_with(b'"INBOX"', readonly=True)
        connection.uid.assert_called_once_with("fetch", "7", "(FLAGS RFC822.SIZE RFC822)")

    def test_fetch_message_detail_extracts_inline_content_id_metadata(self):
        message = EmailMessage()
        message["Subject"] = "Inline CID"
        message["From"] = "Sender <sender@example.com>"
        message["To"] = "User <user@example.com>"
        message.set_content("Plain fallback")
        message.add_alternative('<p><img src="cid:logo123"></p>', subtype="html")
        html_part = message.get_payload()[1]
        html_part.add_related(b"png bytes", maintype="image", subtype="png", cid="<logo123>", filename="logo.png")
        html_part.get_payload()[1].replace_header("Content-Disposition", 'inline; filename="logo.png"')

        detail = _detail_from_raw_message(bytes(message))

        self.assertTrue(detail.attachments)
        self.assertTrue(detail.attachments[0].is_inline)
        self.assertEqual(detail.attachments[0].content_id, "logo123")
        self.assertEqual(detail.attachments[0].filename, "logo.png")
        self.assertFalse(detail.attachments[0].is_visible)
        self.assertTrue(bool(detail.attachments))
        self.assertFalse(detail.has_visible_attachments)

    def test_fetch_message_detail_hides_duplicate_signature_image_attachments(self):
        detail = _detail_from_raw_message(_raw_duplicate_signature_image_message())

        self.assertEqual(len(detail.attachments), 3)
        self.assertEqual([attachment.content_id for attachment in detail.attachments], ["unreferenced-logo", "referenced-logo", "duplicate-logo"])
        self.assertEqual([attachment.is_visible for attachment in detail.attachments], [False, False, False])
        self.assertFalse(detail.has_visible_attachments)

    def test_fetch_message_detail_keeps_non_duplicate_image_attachment_visible(self):
        message = EmailMessage()
        message["Subject"] = "Inline and image attachment"
        message["From"] = "Sender <sender@example.com>"
        message["To"] = "User <user@example.com>"
        message.set_content("Plain fallback")
        message.add_alternative('<p><img src="cid:logo"></p>', subtype="html")
        html_part = message.get_payload()[1]
        html_part.add_related(b"inline png", maintype="image", subtype="png", cid="<logo>", filename="logo.png")
        html_part.get_payload()[1].replace_header("Content-Disposition", 'inline; filename="logo.png"')
        message.add_attachment(b"different png", maintype="image", subtype="png", filename="photo.png")

        detail = _detail_from_raw_message(bytes(message))

        self.assertEqual([attachment.is_visible for attachment in detail.attachments], [False, True])
        self.assertTrue(detail.has_visible_attachments)

    def test_fetch_message_detail_hides_referenced_cid_images_without_disposition(self):
        message = EmailMessage()
        message["Subject"] = "CID images and PDFs"
        message["From"] = "Sender <sender@example.com>"
        message["To"] = "User <user@example.com>"
        message.set_content("Plain fallback")
        message.add_alternative(
            '<p><img src="cid:image002.jpg@01DCB149.77FF6EF0">'
            '<img src="cid:image004.gif@01DCB149.77FF6EF0"></p>',
            subtype="html",
        )
        html_part = message.get_payload()[1]
        html_part.add_related(
            b"jpg bytes",
            maintype="image",
            subtype="jpeg",
            cid="<image002.jpg@01DCB149.77FF6EF0>",
            filename="image002.jpg",
        )
        _remove_content_disposition(html_part.get_payload()[-1], "image002.jpg")
        html_part.add_related(
            b"gif bytes",
            maintype="image",
            subtype="gif",
            cid="<image004.gif@01DCB149.77FF6EF0>",
            filename="image004.gif",
        )
        _remove_content_disposition(html_part.get_payload()[-1], "image004.gif")
        message.add_attachment(b"delivery note", maintype="application", subtype="pdf", filename="Otpremnice.pdf")
        message.add_attachment(b"invoice", maintype="application", subtype="pdf", filename="194-1-26.pdf")

        detail = _detail_from_raw_message(bytes(message))

        self.assertEqual(
            [attachment.filename for attachment in detail.attachments],
            ["image002.jpg", "image004.gif", "Otpremnice.pdf", "194-1-26.pdf"],
        )
        self.assertEqual(
            [attachment.content_id for attachment in detail.attachments],
            ["image002.jpg@01DCB149.77FF6EF0", "image004.gif@01DCB149.77FF6EF0", "", ""],
        )
        self.assertEqual([attachment.is_visible for attachment in detail.attachments], [False, False, True, True])
        self.assertTrue(detail.has_visible_attachments)

    def test_fetch_message_detail_hides_only_referenced_cid_images_without_disposition(self):
        message = EmailMessage()
        message["Subject"] = "Only CID images"
        message["From"] = "Sender <sender@example.com>"
        message["To"] = "User <user@example.com>"
        message.set_content("Plain fallback")
        message.add_alternative('<p><img src="cid:image002.jpg@01DCB149.77FF6EF0"></p>', subtype="html")
        html_part = message.get_payload()[1]
        html_part.add_related(
            b"jpg bytes",
            maintype="image",
            subtype="jpeg",
            cid="<image002.jpg@01DCB149.77FF6EF0>",
            filename="image002.jpg",
        )
        _remove_content_disposition(html_part.get_payload()[-1], "image002.jpg")

        detail = _detail_from_raw_message(bytes(message))

        self.assertEqual(len(detail.attachments), 1)
        self.assertEqual(detail.attachments[0].filename, "image002.jpg")
        self.assertFalse(detail.attachments[0].is_visible)
        self.assertTrue(bool(detail.attachments))
        self.assertFalse(detail.has_visible_attachments)

    def test_fetch_attachment_returns_selected_attachment_content(self):
        raw_message = _raw_detail_message(text_body="Plain body", attach=True)
        connection = Mock()
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.return_value = (
            "OK",
            [(b"7 (UID 7 FLAGS () RFC822.SIZE 2048 RFC822 {999}", raw_message)],
        )

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            attachment = ImapClient().connect().fetch_attachment("INBOX", "7", "att_1")

        self.assertEqual(attachment.summary.id, "att_1")
        self.assertEqual(attachment.summary.filename, "report.txt")
        self.assertEqual(attachment.content, b"report content")

    def test_fetch_attachments_uses_detail_visibility_for_forward_sources(self):
        raw_message = _raw_telwin_message()
        connection = Mock()
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.return_value = (
            "OK",
            [(b"7 (UID 7 FLAGS () RFC822.SIZE 2048 RFC822 {999}", raw_message)],
        )

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            attachments = ImapClient().connect().fetch_attachments("INBOX", "7")

        self.assertEqual([attachment.summary.filename for attachment in attachments], ["image002.jpg", "image004.gif", "Otpremnice.pdf", "194-1-26.pdf"])
        self.assertEqual([attachment.summary.is_visible for attachment in attachments], [False, False, True, True])
        self.assertEqual([attachment.summary.content_type for attachment in attachments[2:]], ["application/pdf", "application/pdf"])

    def test_fetch_attachment_guesses_pdf_content_type_from_filename(self):
        message = EmailMessage()
        message["Subject"] = "PDF attachment"
        message["From"] = "Sender <sender@example.com>"
        message["To"] = "User <user@example.com>"
        message.set_content("Plain body")
        message.add_attachment(
            b"%PDF-1.7\n",
            maintype="application",
            subtype="octet-stream",
            filename="statement.pdf",
        )

        detail = _detail_from_raw_message(bytes(message))

        self.assertEqual(detail.attachments[0].filename, "statement.pdf")
        self.assertEqual(detail.attachments[0].content_type, "application/pdf")

    def test_fetch_attachment_raises_not_found_for_unknown_attachment_id(self):
        raw_message = _raw_detail_message(text_body="Plain body", attach=True)
        connection = Mock()
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.return_value = (
            "OK",
            [(b"7 (UID 7 FLAGS () RFC822.SIZE 2048 RFC822 {999}", raw_message)],
        )

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(MailAttachmentNotFoundError):
                ImapClient().connect().fetch_attachment("INBOX", "7", "att_99")

    def test_fetch_message_detail_extracts_plain_text_only(self):
        detail = _detail_from_raw_message(_raw_detail_message(text_body="Plain only"))

        self.assertIn("Plain only", detail.text_body)
        self.assertEqual(detail.html_body, "")
        self.assertEqual(detail.attachments, ())

    def test_fetch_message_detail_keeps_inline_text_as_body(self):
        message = EmailMessage()
        message["Subject"] = "Inline text"
        message["From"] = "Sender <sender@example.com>"
        message["To"] = "User <user@example.com>"
        message.set_content("Postovani,\nPBZ izvadak", charset="iso-8859-2")
        message.add_header("Content-Disposition", "inline")
        message.add_attachment(b"%PDF-1.7\n", maintype="application", subtype="pdf", filename="statement.pdf")

        detail = _detail_from_raw_message(bytes(message))

        self.assertIn("Postovani", detail.text_body)
        self.assertEqual(detail.html_body, "")
        self.assertEqual(len(detail.attachments), 1)
        self.assertEqual(detail.attachments[0].filename, "statement.pdf")

    def test_fetch_message_detail_extracts_html_only(self):
        detail = _detail_from_raw_message(_raw_detail_message(html_body="<p>HTML only</p>"))

        self.assertEqual(detail.text_body, "HTML only")
        self.assertIn("<p>HTML only</p>", detail.html_body)
        self.assertEqual(detail.attachments, ())

    def test_fetch_message_detail_builds_text_fallback_for_table_html(self):
        raw_message = _raw_detail_message(
            html_body=(
                "<html><head><style>.hidden{display:none}</style></head>"
                "<body><table><tr><td>Nuvola Studio</td></tr>"
                "<tr><td>Vaša narudžba je poslana</td></tr></table></body></html>"
            )
        )

        detail = _detail_from_raw_message(raw_message)

        self.assertIn("Nuvola Studio", detail.text_body)
        self.assertIn("Vaša narudžba je poslana", detail.text_body)
        self.assertNotIn("display:none", detail.text_body)

    def test_fetch_message_detail_extracts_multipart_alternative(self):
        detail = _detail_from_raw_message(_raw_detail_message(text_body="Plain alt", html_body="<p>HTML alt</p>"))

        self.assertIn("Plain alt", detail.text_body)
        self.assertIn("<p>HTML alt</p>", detail.html_body)
        self.assertEqual(detail.attachments, ())

    def test_move_messages_to_trash_resolves_special_use_folder_and_uses_uid_move(self):
        connection = Mock()
        connection.list.return_value = (
            "OK",
            [
                b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasNoChildren \\Trash) "/" "Deleted Messages"',
            ],
        )
        connection.select.return_value = ("OK", [b"2"])
        connection.uid.side_effect = [
            ("OK", [b"moved"]),
            ("OK", [b"moved"]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            result = ImapClient().connect().move_messages_to_trash("INBOX", ("123", "124"))

        self.assertEqual(result.trash_folder, "Deleted Messages")
        self.assertEqual(result.moved_to_trash, ("123", "124"))
        self.assertEqual(result.failed, ())
        connection.select.assert_called_once_with(b'"INBOX"', readonly=False)
        connection.uid.assert_any_call("MOVE", "123", b'"Deleted Messages"')
        connection.uid.assert_any_call("MOVE", "124", b'"Deleted Messages"')

    def test_move_messages_to_trash_falls_back_to_copy_and_deleted_flag(self):
        connection = Mock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Trash"'])
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.side_effect = [
            ("NO", [b"MOVE unsupported"]),
            ("OK", [b"copied"]),
            ("OK", [b"stored"]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            result = ImapClient().connect().move_messages_to_trash("INBOX", ("123",))

        self.assertEqual(result.trash_folder, "Trash")
        self.assertEqual(result.moved_to_trash, ("123",))
        self.assertEqual(result.failed, ())
        connection.uid.assert_any_call("MOVE", "123", b'"Trash"')
        connection.uid.assert_any_call("COPY", "123", b'"Trash"')
        connection.uid.assert_any_call("STORE", "123", "+FLAGS.SILENT", r"(\Deleted)")

    def test_move_messages_to_trash_reports_partial_failures(self):
        connection = Mock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Trash"'])
        connection.select.return_value = ("OK", [b"2"])
        connection.uid.side_effect = [
            ("OK", [b"moved"]),
            ("NO", [b"MOVE unsupported"]),
            ("NO", [b"copy failed"]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            result = ImapClient().connect().move_messages_to_trash("INBOX", ("123", "124"))

        self.assertEqual(result.moved_to_trash, ("123",))
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0].uid, "124")
        self.assertEqual(result.failed[0].error, "move_failed")
        self.assertIn("copy", result.failed[0].detail.lower())

    def test_move_messages_to_trash_rejects_trash_source_and_missing_trash_folder(self):
        connection = Mock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren \\Trash) "/" "Trash"'])

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(MailInvalidOperationError):
                ImapClient().connect().move_messages_to_trash("Trash", ("123",))

        connection = Mock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"'])

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(MailProtocolError):
                ImapClient().connect().move_messages_to_trash("INBOX", ("123",))

    def test_move_messages_to_trash_normalizes_timeout_errors(self):
        connection = Mock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Trash"'])
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.side_effect = socket.timeout

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(MailTimeoutError):
                ImapClient().connect().move_messages_to_trash("INBOX", ("123",))

    def test_restore_messages_from_trash_resolves_source_and_uses_uid_move(self):
        connection = Mock()
        connection.list.return_value = (
            "OK",
            [
                b'(\\HasNoChildren) "/" "INBOX"',
                b'(\\HasNoChildren \\Trash) "/" "Trash"',
            ],
        )
        connection.select.return_value = ("OK", [b"2"])
        connection.uid.side_effect = [
            ("OK", [b"restored"]),
            ("OK", [b"restored"]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            result = ImapClient().connect().restore_messages_from_trash("Trash", "INBOX", ("123", "124"))

        self.assertEqual(result.target_folder, "INBOX")
        self.assertEqual(result.restored, ("123", "124"))
        self.assertEqual(result.failed, ())
        connection.select.assert_called_once_with(b'"Trash"', readonly=False)
        connection.uid.assert_any_call("MOVE", "123", b'"INBOX"')
        connection.uid.assert_any_call("MOVE", "124", b'"INBOX"')

    def test_restore_messages_from_trash_falls_back_to_copy_and_deleted_flag(self):
        connection = Mock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Trash"'])
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.side_effect = [
            ("NO", [b"MOVE unsupported"]),
            ("OK", [b"copied"]),
            ("OK", [b"stored"]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            result = ImapClient().connect().restore_messages_from_trash("Trash", "INBOX", ("123",))

        self.assertEqual(result.target_folder, "INBOX")
        self.assertEqual(result.restored, ("123",))
        self.assertEqual(result.failed, ())
        connection.uid.assert_any_call("MOVE", "123", b'"INBOX"')
        connection.uid.assert_any_call("COPY", "123", b'"INBOX"')
        connection.uid.assert_any_call("STORE", "123", "+FLAGS.SILENT", r"(\Deleted)")

    def test_restore_messages_from_trash_reports_partial_failures(self):
        connection = Mock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Trash"'])
        connection.select.return_value = ("OK", [b"2"])
        connection.uid.side_effect = [
            ("OK", [b"restored"]),
            ("NO", [b"MOVE unsupported"]),
            ("NO", [b"copy failed"]),
        ]

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            result = ImapClient().connect().restore_messages_from_trash("Trash", "INBOX", ("123", "124"))

        self.assertEqual(result.restored, ("123",))
        self.assertEqual(len(result.failed), 1)
        self.assertEqual(result.failed[0].uid, "124")
        self.assertEqual(result.failed[0].error, "restore_failed")
        self.assertIn("copy", result.failed[0].detail.lower())

    def test_restore_messages_from_trash_rejects_non_trash_source_and_trash_target(self):
        connection = Mock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Trash) "/" "Trash"'])

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaisesRegex(MailInvalidOperationError, "restore_source_not_trash"):
                ImapClient().connect().restore_messages_from_trash("INBOX", "Archive", ("123",))

        connection = Mock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren \\Trash) "/" "Trash"'])

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaisesRegex(MailInvalidOperationError, "restore_target_is_trash"):
                ImapClient().connect().restore_messages_from_trash("Trash", "Trash", ("123",))

    def test_restore_messages_from_trash_normalizes_timeout_errors(self):
        connection = Mock()
        connection.list.return_value = ("OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\HasNoChildren) "/" "Trash"'])
        connection.select.return_value = ("OK", [b"1"])
        connection.uid.side_effect = socket.timeout

        with patch("mail_integration.imap_client.imaplib.IMAP4_SSL", return_value=connection):
            with self.assertRaises(MailTimeoutError):
                ImapClient().connect().restore_messages_from_trash("Trash", "INBOX", ("123",))

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
    def test_login_and_quit_lifecycle(self):
        connection = Mock()

        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection) as smtp:
            client = SmtpClient().connect().login(MailboxCredentials("sender@example.com", "secret"))
            client.quit()

        smtp.assert_called_once_with("mail.finestar.test", 587, timeout=15)
        connection.starttls.assert_called_once()
        connection.login.assert_called_once_with("sender@example.com", "secret")
        connection.quit.assert_called_once()

    def test_plain_text_send_builds_single_text_message(self):
        connection = Mock()

        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            SmtpClient().connect().send_mail(
                MailboxCredentials("sender@example.com", "secret"),
                SendMailRequest(to=("to@example.com",), subject="Plain", text_body="Plain body"),
            )

        sent_message = connection.send_message.call_args.args[0]
        self.assertFalse(sent_message.is_multipart())
        self.assertEqual(sent_message.get_content_type(), "text/plain")
        self.assertEqual(sent_message.get_content().strip(), "Plain body")

    def test_html_send_builds_single_html_message(self):
        connection = Mock()

        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            SmtpClient().connect().send_mail(
                MailboxCredentials("sender@example.com", "secret"),
                SendMailRequest(to=("to@example.com",), subject="HTML", html_body="<p>HTML body</p>"),
            )

        sent_message = connection.send_message.call_args.args[0]
        self.assertFalse(sent_message.is_multipart())
        self.assertEqual(sent_message.get_content_type(), "text/html")
        self.assertEqual(sent_message.get_content().strip(), "<p>HTML body</p>")

    def test_multipart_send_builds_alternative_message_and_recipient_envelope(self):
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
                    subject="Status Čakovec",
                    text_body="Plain body",
                    html_body="<p>HTML body</p>",
                    from_display_name="Finestar Čakovec",
                ),
            )

        connection.starttls.assert_called_once()
        connection.login.assert_called_once_with("sender@example.com", "secret")
        sent_message = connection.send_message.call_args.args[0]
        self.assertEqual(str(sent_message["From"]), "Finestar Čakovec <sender@example.com>")
        self.assertEqual(sent_message["To"], "to@example.com")
        self.assertEqual(sent_message["Cc"], "cc@example.com")
        self.assertEqual(sent_message["Reply-To"], "reply@example.com")
        self.assertEqual(str(sent_message["Subject"]), "Status Čakovec")
        self.assertIn("Date", sent_message)
        self.assertNotIn("Bcc", sent_message)
        self.assertTrue(sent_message.is_multipart())
        self.assertEqual(sent_message.get_content_type(), "multipart/alternative")
        self.assertEqual([part.get_content_type() for part in sent_message.iter_parts()], ["text/plain", "text/html"])
        self.assertEqual(sum(1 for _ in sent_message.walk() if "MIME-Version" in _), 1)
        self.assertEqual(connection.send_message.call_args.kwargs["to_addrs"], ["to@example.com", "cc@example.com", "bcc@example.com"])
        self.assertEqual(message_id, sent_message["Message-ID"])

    def test_send_with_attachments_builds_multipart_mixed_message(self):
        connection = Mock()

        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            SmtpClient().connect().send_mail(
                MailboxCredentials("sender@example.com", "secret"),
                SendMailRequest(
                    to=("to@example.com",),
                    subject="With attachment",
                    text_body="Plain body",
                    html_body="<p>HTML body</p>",
                    attachments=(
                        SendMailAttachment(filename="report.txt", content_type="text/plain", content=b"report content"),
                        SendMailAttachment(filename="data.bin", content_type="application/octet-stream", content=b"\x00\x01"),
                    ),
                ),
            )

        sent_message = connection.send_message.call_args.args[0]
        self.assertEqual(sent_message.get_content_type(), "multipart/mixed")
        part_types = [part.get_content_type() for part in sent_message.walk()]
        self.assertIn("multipart/alternative", part_types)
        self.assertIn("text/plain", part_types)
        self.assertIn("text/html", part_types)
        attachments = list(sent_message.iter_attachments())
        self.assertEqual([attachment.get_filename() for attachment in attachments], ["report.txt", "data.bin"])
        self.assertEqual(attachments[0].get_content(), "report content")
        self.assertEqual(attachments[1].get_payload(decode=True), b"\x00\x01")

    def test_smtp_errors_are_normalized(self):
        with patch("mail_integration.smtp_client.smtplib.SMTP", side_effect=socket.timeout):
            with self.assertRaises(MailTimeoutError):
                SmtpClient().connect()

        with patch("mail_integration.smtp_client.smtplib.SMTP", side_effect=OSError("refused")):
            with self.assertRaises(MailConnectionError):
                SmtpClient().connect()

        connection = Mock()
        connection.starttls.side_effect = ssl.SSLError("tls failed")
        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            with self.assertRaises(MailConnectionError):
                SmtpClient().connect()

        connection = Mock()
        connection.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            with self.assertRaises(MailAuthError):
                SmtpClient().connect().login(MailboxCredentials("sender@example.com", "bad"))

        connection = Mock()
        connection.send_message.side_effect = socket.timeout
        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            with self.assertRaises(MailTimeoutError):
                SmtpClient().connect().send_mail(
                    MailboxCredentials("sender@example.com", "secret"),
                    SendMailRequest(to=("to@example.com",), subject="Hi", text_body="Body"),
                )

        connection = Mock()
        connection.send_message.side_effect = smtplib.SMTPRecipientsRefused({})
        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            with self.assertRaises(MailSendError):
                SmtpClient().connect().send_mail(
                    MailboxCredentials("sender@example.com", "secret"),
                    SendMailRequest(to=("to@example.com",), subject="Hi", text_body="Body"),
                )

        connection = Mock()
        with patch("mail_integration.smtp_client.smtplib.SMTP", return_value=connection):
            with self.assertRaises(MailProtocolError):
                SmtpClient().connect().send_mail(
                    MailboxCredentials("not-an-email", "secret"),
                    SendMailRequest(to=("to@example.com",), subject="Hi", text_body="Body", from_display_name="Sender"),
                )

    def test_message_building_rejects_missing_recipients_or_body(self):
        with self.assertRaises(ValueError):
            build_email_message("sender@example.com", SendMailRequest(to=(), subject="Hi", text_body="Body"))
        with self.assertRaises(ValueError):
            build_email_message("sender@example.com", SendMailRequest(to=("to@example.com",), subject="Hi"))


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
        entered.fetch_account_summary.return_value = MailboxAccountSummary(unread_count=2, important_count=1)
        entered.fetch_message_summary_page.return_value = "summary-page"
        entered.fetch_conversation_page.return_value = "conversation-page"
        entered.fetch_message_detail.return_value = "detail"
        entered.fetch_attachment.return_value = "attachment"
        entered.fetch_attachments.return_value = "attachments"
        entered.move_messages_to_trash.return_value = "move-result"
        entered.restore_messages_from_trash.return_value = "restore-result"

        service = MailboxService(imap_client_factory=lambda: imap_client)

        self.assertEqual(service.list_folders(credentials), ["INBOX"])
        self.assertEqual(service.list_message_summaries(credentials, folder="Archive", limit=10), ["summary"])
        self.assertEqual(service.get_account_summary(credentials), MailboxAccountSummary(unread_count=2, important_count=1))
        self.assertEqual(service.list_message_summary_page(credentials, folder="Archive", limit=10, before_uid="99"), "summary-page")
        self.assertEqual(service.list_conversations(credentials, folder="Archive", limit=5), "conversation-page")
        self.assertEqual(service.get_message_detail(credentials, folder="Archive", uid="99"), "detail")
        self.assertEqual(service.get_attachment(credentials, folder="Archive", uid="99", attachment_id="att_1"), "attachment")
        self.assertEqual(service.get_attachments(credentials, folder="Archive", uid="99"), "attachments")
        self.assertEqual(service.move_messages_to_trash(credentials, folder="Archive", uids=("99",)), "move-result")
        self.assertEqual(service.restore_messages_from_trash(credentials, folder="Trash", target_folder="INBOX", uids=("99",)), "restore-result")
        self.assertEqual(entered.login.call_count, 10)
        entered.login.assert_called_with(credentials)
        entered.fetch_message_summaries.assert_called_once_with(folder="Archive", limit=10)
        entered.fetch_account_summary.assert_called_once_with()
        entered.fetch_message_summary_page.assert_called_once_with(folder="Archive", limit=10, before_uid="99")
        entered.fetch_conversation_page.assert_called_once_with(folder="Archive", limit=5)
        entered.fetch_message_detail.assert_called_once_with(folder="Archive", uid="99")
        entered.fetch_attachment.assert_called_once_with(folder="Archive", uid="99", attachment_id="att_1")
        entered.fetch_attachments.assert_called_once_with(folder="Archive", uid="99")
        entered.move_messages_to_trash.assert_called_once_with(folder="Archive", uids=("99",))
        entered.restore_messages_from_trash.assert_called_once_with(folder="Trash", target_folder="INBOX", uids=("99",))

    def test_service_send_method_routes_to_smtp_client(self):
        credentials = MailboxCredentials("sender@example.com", "secret")
        request = SendMailRequest(to=("to@example.com",), subject="Hi", text_body="Body")
        smtp_client = _context_client()
        smtp_client.__enter__.return_value.send_mail.return_value = "<sent@example.com>"

        service = MailboxService(smtp_client_factory=lambda: smtp_client)

        self.assertEqual(service.send_mail(credentials, request), "<sent@example.com>")
        smtp_client.__enter__.return_value.login.assert_called_once_with(credentials)
        smtp_client.__enter__.return_value.send_mail.assert_called_once_with(credentials, request)

    def test_service_send_resolves_forwarded_visible_attachments_in_client_order(self):
        credentials = MailboxCredentials("sender@example.com", "secret")
        request = SendMailRequest(
            to=("to@example.com",),
            subject="Hi",
            text_body="Body",
            attachments=(SendMailAttachment(filename="manual.txt", content_type="text/plain", content=b"manual"),),
            forward_source_message=ForwardSourceMessage(folder="INBOX", uid="7", attachment_ids=("att_4", "att_3")),
        )
        imap_client = _context_client()
        smtp_client = _context_client()
        imap_client.__enter__.return_value.fetch_attachments.return_value = _telwin_attachment_contents()
        smtp_client.__enter__.return_value.send_mail.return_value = "<sent@example.com>"

        service = MailboxService(imap_client_factory=lambda: imap_client, smtp_client_factory=lambda: smtp_client)

        self.assertEqual(service.send_mail(credentials, request), "<sent@example.com>")
        sent_request = smtp_client.__enter__.return_value.send_mail.call_args.args[1]
        self.assertEqual([attachment.filename for attachment in sent_request.attachments], ["194-1-26.pdf", "Otpremnice.pdf", "manual.txt"])
        self.assertEqual([attachment.content_type for attachment in sent_request.attachments[:2]], ["application/pdf", "application/pdf"])
        self.assertEqual([attachment.content for attachment in sent_request.attachments], [b"invoice", b"delivery note", b"manual"])
        imap_client.__enter__.return_value.fetch_attachments.assert_called_once_with(folder="INBOX", uid="7")

    def test_service_send_rejects_hidden_forward_attachment_as_invalid_input(self):
        credentials = MailboxCredentials("sender@example.com", "secret")
        request = SendMailRequest(
            to=("to@example.com",),
            subject="Hi",
            text_body="Body",
            forward_source_message=ForwardSourceMessage(folder="INBOX", uid="7", attachment_ids=("att_1",)),
        )
        imap_client = _context_client()
        smtp_client = _context_client()
        imap_client.__enter__.return_value.fetch_attachments.return_value = _telwin_attachment_contents()

        service = MailboxService(imap_client_factory=lambda: imap_client, smtp_client_factory=lambda: smtp_client)

        with self.assertRaises(MailForwardAttachmentNotVisibleError):
            service.send_mail(credentials, request)
        smtp_client.__enter__.return_value.send_mail.assert_not_called()

    def test_service_send_rejects_unknown_forward_attachment_as_invalid_input(self):
        credentials = MailboxCredentials("sender@example.com", "secret")
        request = SendMailRequest(
            to=("to@example.com",),
            subject="Hi",
            text_body="Body",
            forward_source_message=ForwardSourceMessage(folder="INBOX", uid="7", attachment_ids=("att_99",)),
        )
        imap_client = _context_client()
        smtp_client = _context_client()
        imap_client.__enter__.return_value.fetch_attachments.return_value = _telwin_attachment_contents()

        service = MailboxService(imap_client_factory=lambda: imap_client, smtp_client_factory=lambda: smtp_client)

        with self.assertRaises(MailForwardAttachmentNotFoundError):
            service.send_mail(credentials, request)
        smtp_client.__enter__.return_value.send_mail.assert_not_called()


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


def _raw_duplicate_signature_image_message():
    message = EmailMessage()
    message["Subject"] = "Forwarded signature"
    message["From"] = "Sender <sender@example.com>"
    message["To"] = "User <user@example.com>"
    message.set_content("Plain fallback")
    message.add_alternative('<p><img src="cid:referenced-logo"></p>', subtype="html")
    html_part = message.get_payload()[1]
    image_content = b"same png bytes"
    html_part.add_related(image_content, maintype="image", subtype="png", cid="<unreferenced-logo>", filename="Outlook-logo.png")
    html_part.get_payload()[1].replace_header("Content-Disposition", 'inline; filename="Outlook-logo.png"')
    html_part.add_related(image_content, maintype="image", subtype="png", cid="<referenced-logo>", filename="Outlook-logo.png")
    html_part.get_payload()[2].replace_header("Content-Disposition", 'inline; filename="Outlook-logo.png"')
    message.add_attachment(image_content, maintype="image", subtype="png", filename="Outlook-logo.png", cid="<duplicate-logo>")
    return bytes(message)


def _raw_telwin_message():
    message = EmailMessage()
    message["Subject"] = "TELWIN attachments"
    message["From"] = "Sender <sender@example.com>"
    message["To"] = "User <user@example.com>"
    message.set_content("Plain fallback")
    message.add_alternative(
        '<p><img src="cid:image002.jpg@01DCB149.77FF6EF0">'
        '<img src="cid:image004.gif@01DCB149.77FF6EF0"></p>',
        subtype="html",
    )
    html_part = message.get_payload()[1]
    html_part.add_related(
        b"jpg bytes",
        maintype="image",
        subtype="jpeg",
        cid="<image002.jpg@01DCB149.77FF6EF0>",
        filename="image002.jpg",
    )
    _remove_content_disposition(html_part.get_payload()[-1], "image002.jpg")
    html_part.add_related(
        b"gif bytes",
        maintype="image",
        subtype="gif",
        cid="<image004.gif@01DCB149.77FF6EF0>",
        filename="image004.gif",
    )
    _remove_content_disposition(html_part.get_payload()[-1], "image004.gif")
    message.add_attachment(b"delivery note", maintype="application", subtype="pdf", filename="Otpremnice.pdf")
    message.add_attachment(b"invoice", maintype="application", subtype="pdf", filename="194-1-26.pdf")
    return bytes(message)


def _telwin_attachment_contents():
    return (
        MailAttachmentContent(
            summary=MailAttachmentSummary(
                id="att_1",
                filename="image002.jpg",
                content_type="image/jpeg",
                size=9,
                content_id="image002.jpg@01DCB149.77FF6EF0",
                is_visible=False,
            ),
            content=b"jpg bytes",
        ),
        MailAttachmentContent(
            summary=MailAttachmentSummary(
                id="att_2",
                filename="image004.gif",
                content_type="image/gif",
                size=9,
                content_id="image004.gif@01DCB149.77FF6EF0",
                is_visible=False,
            ),
            content=b"gif bytes",
        ),
        MailAttachmentContent(
            summary=MailAttachmentSummary(id="att_3", filename="Otpremnice.pdf", content_type="application/pdf", size=13, is_visible=True),
            content=b"delivery note",
        ),
        MailAttachmentContent(
            summary=MailAttachmentSummary(id="att_4", filename="194-1-26.pdf", content_type="application/pdf", size=7, is_visible=True),
            content=b"invoice",
        ),
    )


def _conversation_fetch(
    uid,
    subject,
    sender,
    to,
    date="",
    message_id="",
    in_reply_to="",
    references="",
    flags="",
    attach=False,
):
    bodystructure = (
        b'(("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1 NIL NIL NIL)'
        b'("APPLICATION" "PDF" ("NAME" "report.pdf") NIL NIL "BASE64" 100 NIL '
        b'("ATTACHMENT" ("FILENAME" "report.pdf")) NIL) "MIXED")'
        if attach
        else b'("TEXT" "PLAIN" ("CHARSET" "UTF-8") NIL NIL "7BIT" 12 1 NIL NIL NIL)'
    )
    headers = [
        f"Subject: {subject}",
        f"From: {sender}",
        f"To: {to}",
    ]
    if date:
        headers.append(f"Date: {date}")
    if message_id:
        headers.append(f"Message-ID: {message_id}")
    if in_reply_to:
        headers.append(f"In-Reply-To: {in_reply_to}")
    if references:
        headers.append(f"References: {references}")
    payload = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8")
    metadata = f"{uid} (UID {uid} FLAGS ({flags}) RFC822.SIZE 123 BODYSTRUCTURE ".encode("ascii") + bodystructure + b" BODY[HEADER.FIELDS ...] {123}"
    return ("OK", [(metadata, payload)])


def _remove_content_disposition(part, filename=None):
    if "Content-Disposition" in part:
        del part["Content-Disposition"]
    if filename:
        part.set_param("name", filename, header="Content-Type")


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
