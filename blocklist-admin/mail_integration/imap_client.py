import imaplib
import re
import socket
import ssl
from email.header import decode_header, make_header
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime

from django.conf import settings

from .exceptions import MailAttachmentNotFoundError, MailAuthError, MailConnectionError, MailInvalidOperationError, MailProtocolError, MailTimeoutError
from .schemas import (
    MailAttachmentSummary,
    MailAttachmentContent,
    MailFolderSummary,
    MailMessageDetail,
    MailMessageMoveFailure,
    MailMessageMoveToTrashResult,
    MailMessageRestoreResult,
    MailMessageSummary,
    MailMessageSummaryPage,
    MailboxCredentials,
)


_LIST_RE = re.compile(rb'\((?P<flags>.*?)\)\s+"?(?P<delimiter>[^"\s]*)"?\s+(?P<name>.+)$')
_UID_RE = re.compile(rb"\bUID\s+(\d+)\b", re.IGNORECASE)
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)\b", re.IGNORECASE)
_FLAGS_RE = re.compile(rb"\bFLAGS\s+\((.*?)\)", re.IGNORECASE)
_BODYSTRUCTURE_RE = re.compile(rb"BODYSTRUCTURE\s+(?P<bodystructure>.+?)(?:\s+BODY\[|\s*\)\s*$)", re.IGNORECASE | re.DOTALL)
_ATTACHMENT_MARKER_RE = re.compile(rb'"(?:ATTACHMENT|INLINE|FILENAME|NAME)"', re.IGNORECASE)


class ImapClient:
    def __init__(self, host=None, port=None, use_ssl=None, timeout=None):
        self.host = host or settings.MAIL_IMAP_HOST
        self.port = int(port or settings.MAIL_IMAP_PORT)
        self.use_ssl = settings.MAIL_IMAP_USE_SSL if use_ssl is None else use_ssl
        self.timeout = int(timeout or settings.MAIL_CLIENT_TIMEOUT_SECONDS)
        self.connection = None

    def connect(self):
        try:
            if self.use_ssl:
                context = ssl.create_default_context()
                self.connection = imaplib.IMAP4_SSL(self.host, self.port, ssl_context=context, timeout=self.timeout)
            else:
                self.connection = imaplib.IMAP4(self.host, self.port, timeout=self.timeout)
            return self
        except socket.timeout as exc:
            raise MailTimeoutError(f"Timed out connecting to IMAP server {self.host}:{self.port}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"Could not connect to IMAP server {self.host}:{self.port}: {exc}") from exc

    def login(self, credentials: MailboxCredentials):
        connection = self._require_connection()
        try:
            status, data = connection.login(credentials.email, credentials.password)
        except imaplib.IMAP4.error as exc:
            raise MailAuthError("IMAP authentication failed") from exc
        except socket.timeout as exc:
            raise MailTimeoutError("Timed out during IMAP authentication") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"IMAP authentication connection failure: {exc}") from exc
        if status != "OK":
            detail = _decode_first(data)
            raise MailAuthError(f"IMAP authentication failed: {detail}".rstrip(": "))
        return self

    def logout(self):
        if self.connection is None:
            return
        try:
            self.connection.logout()
        except imaplib.IMAP4.error:
            pass
        finally:
            self.connection = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, exc_type, exc, traceback):
        self.logout()

    def list_folders(self):
        connection = self._require_connection()
        try:
            status, data = connection.list()
        except socket.timeout as exc:
            raise MailTimeoutError("Timed out listing IMAP folders") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"IMAP folder listing connection failure: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailProtocolError("IMAP folder listing failed") from exc
        self._expect_ok(status, data, "IMAP folder listing failed")
        return [_parse_folder(line) for line in data or [] if line]

    def select_folder(self, folder="INBOX", readonly=True):
        connection = self._require_connection()
        try:
            status, data = connection.select(folder, readonly=readonly)
        except socket.timeout as exc:
            raise MailTimeoutError(f"Timed out selecting IMAP folder {folder}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"IMAP folder selection connection failure for {folder}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailProtocolError(f"IMAP folder selection failed for {folder}") from exc
        self._expect_ok(status, data, f"IMAP folder selection failed for {folder}")

    def fetch_message_summaries(self, folder="INBOX", limit=50):
        return list(self.fetch_message_summary_page(folder=folder, limit=limit).messages)

    def fetch_message_summary_page(self, folder="INBOX", limit=50, before_uid=None):
        connection = self._require_connection()
        self.select_folder(folder, readonly=True)
        if limit < 1:
            return MailMessageSummaryPage()
        try:
            status, data = connection.uid("search", None, "UNDELETED")
            self._expect_ok(status, data, f"IMAP search failed for {folder}")
            uids = _parse_uid_list(data[0] or b"")
            if before_uid is not None:
                before_uid_int = _parse_positive_uid(before_uid)
                uids = [uid for uid in uids if uid < before_uid_int]
            selected_uid_ints = list(reversed(uids[-limit:]))
            has_more = len(uids) > len(selected_uid_ints)
            selected_uids = [str(uid).encode("ascii") for uid in selected_uid_ints]
            summaries = []
            for uid in selected_uids:
                status, fetch_data = connection.uid(
                    "fetch",
                    uid,
                    "(FLAGS RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO CC DATE MESSAGE-ID)])",
                )
                self._expect_ok(status, fetch_data, f"IMAP summary fetch failed for UID {uid.decode()}")
                summaries.append(_parse_summary_response(folder, uid.decode(), fetch_data))
            next_before_uid = summaries[-1].uid if has_more and summaries else None
            return MailMessageSummaryPage(messages=tuple(summaries), has_more=has_more, next_before_uid=next_before_uid)
        except socket.timeout as exc:
            raise MailTimeoutError(f"Timed out fetching IMAP summaries for {folder}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"IMAP summary fetch connection failure for {folder}: {exc}") from exc
        except ValueError as exc:
            raise MailProtocolError(f"IMAP summary pagination failed for {folder}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailProtocolError(f"IMAP summary fetch failed for {folder}") from exc

    def fetch_message_detail(self, folder, uid):
        metadata, message = self._fetch_full_message(folder, uid)
        return _parse_detail_message(folder, str(uid), metadata, message)

    def fetch_attachment(self, folder, uid, attachment_id):
        metadata, message = self._fetch_full_message(folder, uid)
        attachments = _extract_attachments(message)
        for attachment in attachments:
            if attachment.summary.id == attachment_id:
                return attachment
        raise MailAttachmentNotFoundError(f"Attachment {attachment_id} was not found")

    def _fetch_full_message(self, folder, uid):
        connection = self._require_connection()
        self.select_folder(folder, readonly=True)
        try:
            status, data = connection.uid("fetch", str(uid), "(FLAGS RFC822.SIZE RFC822)")
        except socket.timeout as exc:
            raise MailTimeoutError(f"Timed out fetching IMAP message {uid}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"IMAP message fetch connection failure for UID {uid}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailProtocolError(f"IMAP message fetch failed for UID {uid}") from exc
        self._expect_ok(status, data, f"IMAP message fetch failed for UID {uid}")
        metadata, payload = _first_fetch_tuple(data)
        try:
            return metadata, BytesParser(policy=policy.default).parsebytes(payload or b"")
        except Exception as exc:
            raise MailProtocolError(f"Could not parse IMAP message response: {exc}") from exc

    def move_messages_to_trash(self, folder, uids):
        connection = self._require_connection()
        normalized_uids = tuple(str(uid) for uid in uids)
        trash_folder = self._resolve_trash_folder()
        if _same_folder(folder, trash_folder):
            raise MailInvalidOperationError("Delete from Trash is not supported")
        self.select_folder(folder, readonly=False)
        moved = []
        failed = []
        for uid in normalized_uids:
            try:
                self._move_message_to_trash(uid, trash_folder)
            except MailProtocolError as exc:
                failed.append(MailMessageMoveFailure(uid=uid, error="move_failed", detail=str(exc)))
            else:
                moved.append(uid)
        return MailMessageMoveToTrashResult(trash_folder=trash_folder, moved_to_trash=tuple(moved), failed=tuple(failed))

    def restore_messages_from_trash(self, folder, target_folder, uids):
        connection = self._require_connection()
        normalized_uids = tuple(str(uid) for uid in uids)
        trash_folder = self._resolve_trash_folder()
        if not _same_folder(folder, trash_folder):
            raise MailInvalidOperationError("restore_source_not_trash")
        if _same_folder(target_folder, trash_folder) or _same_folder(target_folder, folder):
            raise MailInvalidOperationError("restore_target_is_trash")
        self.select_folder(folder, readonly=False)
        restored = []
        failed = []
        for uid in normalized_uids:
            try:
                self._move_message(uid, target_folder, "restore")
            except MailProtocolError as exc:
                failed.append(MailMessageMoveFailure(uid=uid, error="restore_failed", detail=str(exc)))
            else:
                restored.append(uid)
        return MailMessageRestoreResult(target_folder=target_folder, restored=tuple(restored), failed=tuple(failed))

    def _resolve_trash_folder(self):
        folders = self.list_folders()
        for folder in folders:
            if any(flag.lower() == "trash" for flag in folder.flags):
                return folder.name
        folder_by_lower_name = {folder.name.lower(): folder.name for folder in folders}
        for candidate in ("trash", "inbox.trash", "deleted items", "deleted messages"):
            if candidate in folder_by_lower_name:
                return folder_by_lower_name[candidate]
        raise MailProtocolError("Could not resolve IMAP Trash folder")

    def _move_message_to_trash(self, uid, trash_folder):
        self._move_message(uid, trash_folder, "move")

    def _move_message(self, uid, target_folder, operation_name):
        connection = self._require_connection()
        try:
            status, data = connection.uid("MOVE", uid, target_folder)
            if status == "OK":
                return
            move_error = _decode_first(data)
            self._copy_and_mark_deleted(uid, target_folder, operation_name)
            return
        except socket.timeout as exc:
            raise MailTimeoutError(f"Timed out during IMAP {operation_name} for UID {uid}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"IMAP {operation_name} connection failure for UID {uid}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            try:
                self._copy_and_mark_deleted(uid, target_folder, operation_name)
                return
            except socket.timeout as fallback_exc:
                raise MailTimeoutError(f"Timed out during IMAP {operation_name} for UID {uid}") from fallback_exc
            except (OSError, ssl.SSLError) as fallback_exc:
                raise MailConnectionError(f"IMAP {operation_name} connection failure for UID {uid}: {fallback_exc}") from fallback_exc
            except imaplib.IMAP4.error as fallback_exc:
                raise MailProtocolError(f"IMAP {operation_name} failed for UID {uid}: {fallback_exc}") from fallback_exc
        raise MailProtocolError(f"IMAP {operation_name} failed for UID {uid}: {move_error}")

    def _copy_and_mark_deleted(self, uid, target_folder, operation_name):
        connection = self._require_connection()
        status, data = connection.uid("COPY", uid, target_folder)
        self._expect_ok(status, data, f"IMAP {operation_name} copy failed for UID {uid}")
        status, data = connection.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
        self._expect_ok(status, data, f"IMAP {operation_name} mark deleted failed for UID {uid}")

    def _require_connection(self):
        if self.connection is None:
            raise MailConnectionError("IMAP client is not connected")
        return self.connection

    @staticmethod
    def _expect_ok(status, data, message):
        if status != "OK":
            detail = _decode_first(data)
            raise MailProtocolError(f"{message}: {detail}".rstrip(": "))


def _parse_folder(line):
    raw = line if isinstance(line, bytes) else str(line).encode("utf-8")
    match = _LIST_RE.search(raw)
    if not match:
        raise MailProtocolError(f"Could not parse IMAP folder line: {_safe_decode(raw)}")
    flags = tuple(flag.lstrip("\\") for flag in _safe_decode(match.group("flags")).split() if flag)
    delimiter_value = _safe_decode(match.group("delimiter"))
    delimiter = None if delimiter_value.upper() == "NIL" else delimiter_value or None
    name = _decode_mailbox_name(match.group("name"))
    return MailFolderSummary(name=name, delimiter=delimiter, flags=flags)


def _parse_summary_response(folder, fallback_uid, fetch_data):
    try:
        metadata, payload = _first_fetch_tuple(fetch_data)
        message = BytesParser(policy=policy.default).parsebytes(payload or b"")
        return MailMessageSummary(
            uid=_metadata_value(_UID_RE, metadata) or fallback_uid,
            folder=folder,
            subject=_header_value(message, "subject"),
            sender=_header_value(message, "from"),
            to=_address_header(_header_value(message, "to")),
            cc=_address_header(_header_value(message, "cc")),
            date=_parsed_date(_header_value(message, "date")),
            message_id=_header_value(message, "message-id"),
            flags=_parse_flags(metadata),
            size=_parse_int(_metadata_value(_SIZE_RE, metadata)),
            has_attachments=_has_attachment_bodystructure(metadata),
        )
    except MailProtocolError:
        raise
    except Exception as exc:
        raise MailProtocolError(f"Could not parse IMAP summary response: {exc}") from exc


def _parse_detail_response(folder, fallback_uid, fetch_data):
    try:
        metadata, payload = _first_fetch_tuple(fetch_data)
        message = BytesParser(policy=policy.default).parsebytes(payload or b"")
        return _parse_detail_message(folder, fallback_uid, metadata, message)
    except MailProtocolError:
        raise
    except Exception as exc:
        raise MailProtocolError(f"Could not parse IMAP message response: {exc}") from exc


def _parse_detail_message(folder, fallback_uid, metadata, message):
    text_body, html_body, attachments = _extract_message_parts(message)
    return MailMessageDetail(
        uid=_metadata_value(_UID_RE, metadata) or fallback_uid,
        folder=folder,
        subject=_header_value(message, "subject"),
        sender=_header_value(message, "from"),
        to=_address_header(_header_value(message, "to")),
        cc=_address_header(_header_value(message, "cc")),
        date=_parsed_date(_header_value(message, "date")),
        message_id=_header_value(message, "message-id"),
        flags=_parse_flags(metadata),
        size=_parse_int(_metadata_value(_SIZE_RE, metadata)),
        text_body=text_body,
        html_body=html_body,
        attachments=tuple(attachment.summary for attachment in attachments),
    )


def _first_fetch_tuple(fetch_data):
    for item in fetch_data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[0] or b"", item[1] or b""
    raise MailProtocolError("IMAP fetch response did not include message data")


def _extract_message_parts(message):
    text_parts = []
    html_parts = []
    attachments = _extract_attachments(message)
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if _is_attachment_part(filename, disposition):
            continue
        if content_type == "text/plain":
            text_parts.append(_part_content(part))
        elif content_type == "text/html":
            html_parts.append(_part_content(part))
    return "\n".join(filter(None, text_parts)), "\n".join(filter(None, html_parts)), attachments


def _extract_attachments(message):
    attachments = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if not _is_attachment_part(filename, disposition):
            continue
        content = part.get_payload(decode=True) or b""
        attachments.append(
            MailAttachmentContent(
                summary=MailAttachmentSummary(
                    id=f"att_{len(attachments) + 1}",
                    filename=filename,
                    content_type=part.get_content_type(),
                    size=len(content),
                    disposition=disposition,
                    is_inline=disposition == "inline",
                ),
                content=content,
            )
        )
    return attachments


def _is_attachment_part(filename, disposition):
    return bool(filename) or disposition in {"attachment", "inline"}


def _part_content(part):
    try:
        return part.get_content()
    except LookupError as exc:
        raise MailProtocolError(f"Unsupported message charset: {exc}") from exc


def _payload_size(part):
    payload = part.get_payload(decode=True)
    if payload is not None:
        return len(payload)
    raw_payload = part.get_payload()
    if isinstance(raw_payload, str):
        return len(raw_payload.encode(part.get_content_charset() or "utf-8", errors="replace"))
    return None


def _header_value(message, name):
    value = message.get(name, "")
    if value is None:
        return ""
    try:
        return str(make_header(decode_header(str(value))))
    except (LookupError, UnicodeError, ValueError) as exc:
        raise MailProtocolError(f"Could not decode message header {name}: {exc}") from exc


def _metadata_value(pattern, metadata):
    match = pattern.search(metadata or b"")
    if not match:
        return None
    return _safe_decode(match.group(1))


def _parse_uid_list(raw_uids):
    uids = []
    for raw_uid in (raw_uids or b"").split():
        uids.append(_parse_positive_uid(_safe_decode(raw_uid)))
    return uids


def _parse_positive_uid(value):
    try:
        uid = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid UID {value!r}") from exc
    if uid < 1:
        raise ValueError(f"Invalid UID {value!r}")
    return uid


def _parse_flags(metadata):
    match = _FLAGS_RE.search(metadata or b"")
    if not match:
        return ()
    return tuple(flag.lstrip("\\") for flag in _safe_decode(match.group(1)).split() if flag)


def _has_attachment_bodystructure(metadata):
    match = _BODYSTRUCTURE_RE.search(metadata or b"")
    if not match:
        return False
    return bool(_ATTACHMENT_MARKER_RE.search(match.group("bodystructure")))


def _address_header(value):
    if not value:
        return ()
    return tuple(address for _, address in getaddresses([value]) if address)


def _parsed_date(value):
    if not value:
        return None
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _same_folder(left, right):
    return str(left or "").strip().lower() == str(right or "").strip().lower()


def _decode_first(data):
    if not data:
        return ""
    return _safe_decode(data[0])


def _decode_mailbox_name(value):
    decoded = _safe_decode(value).strip()
    if decoded.startswith('"') and decoded.endswith('"'):
        return decoded[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return decoded


def _safe_decode(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
