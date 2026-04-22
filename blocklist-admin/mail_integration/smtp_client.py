import smtplib
import socket
import ssl
from email.message import EmailMessage
from email.headerregistry import Address
from email.utils import format_datetime, localtime, make_msgid

from django.conf import settings

from .exceptions import MailAuthError, MailConnectionError, MailProtocolError, MailSendError, MailTimeoutError
from .schemas import MailboxCredentials, SendMailRequest


class SmtpClient:
    def __init__(self, host=None, port=None, use_starttls=None, timeout=None):
        self.host = host or settings.MAIL_SMTP_HOST
        self.port = int(port or settings.MAIL_SMTP_PORT)
        self.use_starttls = settings.MAIL_SMTP_USE_STARTTLS if use_starttls is None else use_starttls
        self.timeout = int(timeout or settings.MAIL_CLIENT_TIMEOUT_SECONDS)
        self.connection = None
        self.last_sent_message = None

    def connect(self):
        try:
            self.connection = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
            if self.use_starttls:
                self.connection.starttls(context=ssl.create_default_context())
            return self
        except socket.timeout as exc:
            raise MailTimeoutError(f"Timed out connecting to SMTP server {self.host}:{self.port}") from exc
        except (OSError, ssl.SSLError, smtplib.SMTPException) as exc:
            raise MailConnectionError(f"Could not connect to SMTP server {self.host}:{self.port}: {exc}") from exc

    def login(self, credentials: MailboxCredentials):
        connection = self._require_connection()
        try:
            connection.login(credentials.email, credentials.password)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailAuthError("SMTP authentication failed") from exc
        except socket.timeout as exc:
            raise MailTimeoutError("Timed out during SMTP authentication") from exc
        except (OSError, ssl.SSLError, smtplib.SMTPException) as exc:
            raise MailConnectionError(f"SMTP authentication connection failure: {exc}") from exc
        return self

    def send_mail(self, credentials: MailboxCredentials, request: SendMailRequest):
        connection = self._require_connection()
        try:
            message = build_email_message(credentials.email, request)
            recipients = list(request.to) + list(request.cc) + list(request.bcc)
        except (TypeError, ValueError, UnicodeError) as exc:
            raise MailProtocolError(f"Could not build SMTP message: {exc}") from exc
        try:
            connection.send_message(message, from_addr=credentials.email, to_addrs=recipients)
            self.last_sent_message = message
        except socket.timeout as exc:
            raise MailTimeoutError("Timed out sending SMTP message") from exc
        except smtplib.SMTPAuthenticationError as exc:
            raise MailAuthError("SMTP authentication failed while sending") from exc
        except smtplib.SMTPException as exc:
            raise MailSendError(f"SMTP send failed: {exc}") from exc
        except OSError as exc:
            raise MailConnectionError(f"SMTP send connection failure: {exc}") from exc
        return message["Message-ID"]

    def quit(self):
        if self.connection is None:
            return
        try:
            self.connection.quit()
        except smtplib.SMTPException:
            pass
        finally:
            self.connection = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, traceback):
        self.quit()

    def _require_connection(self):
        if self.connection is None:
            raise MailConnectionError("SMTP client is not connected")
        return self.connection


def build_email_message(from_email, request: SendMailRequest, include_bcc=False):
    if not request.to:
        raise ValueError("At least one recipient is required")
    if not request.text_body and not request.html_body:
        raise ValueError("Either text_body or html_body is required")

    message = EmailMessage()
    message["From"] = _from_header(from_email, request.from_display_name)
    message["To"] = ", ".join(request.to)
    if request.cc:
        message["Cc"] = ", ".join(request.cc)
    if include_bcc and request.bcc:
        message["Bcc"] = ", ".join(request.bcc)
    if request.reply_to:
        message["Reply-To"] = request.reply_to
    if request.in_reply_to:
        message["In-Reply-To"] = request.in_reply_to
    if request.references:
        message["References"] = " ".join(request.references)
    message["Subject"] = request.subject
    message["Date"] = format_datetime(localtime())
    message["Message-ID"] = make_msgid()

    if request.text_body and request.html_body:
        message.set_content(request.text_body)
        message.add_alternative(request.html_body, subtype="html")
        _remove_nested_mime_version_headers(message)
    elif request.html_body:
        message.set_content(request.html_body, subtype="html")
    else:
        message.set_content(request.text_body)
    for attachment in request.attachments:
        maintype, _, subtype = attachment.content_type.partition("/")
        if not maintype or not subtype:
            maintype, subtype = "application", "octet-stream"
        message.add_attachment(
            attachment.content,
            maintype=maintype,
            subtype=subtype,
            filename=attachment.filename,
        )
    return message


def _from_header(from_email, display_name):
    if not display_name:
        return from_email
    local_part, separator, domain = from_email.partition("@")
    if not separator or not local_part or not domain:
        raise ValueError("Sender email must be a valid addr-spec")
    return Address(display_name=display_name, username=local_part, domain=domain)


def _remove_nested_mime_version_headers(message):
    for part in message.iter_parts():
        if "MIME-Version" in part:
            del part["MIME-Version"]
