import imaplib
import base64
import hashlib
import mimetypes
import re
import socket
import ssl
from collections import defaultdict
from html.parser import HTMLParser
from email.header import decode_header, make_header
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from dataclasses import replace
from urllib.parse import unquote

from django.conf import settings

from .exceptions import MailAttachmentNotFoundError, MailAuthError, MailConnectionError, MailInvalidOperationError, MailProtocolError, MailTimeoutError
from .schemas import (
    MailAttachmentContent,
    MailAttachmentSummary,
    MailConversationParticipant,
    MailConversationSummary,
    MailConversationSummaryPage,
    MailboxAccountSummary,
    MailboxCredentials,
    MailFolderSummary,
    MailMessageDetail,
    MailMessageMoveFailure,
    MailMessageMoveToTrashResult,
    MailMessageRestoreResult,
    MailMessageSummary,
    MailMessageSummaryPage,
)


_LIST_RE = re.compile(rb'\((?P<flags>.*?)\)\s+"?(?P<delimiter>[^"\s]*)"?\s+(?P<name>.+)$')
_UID_RE = re.compile(rb"\bUID\s+(\d+)\b", re.IGNORECASE)
_SIZE_RE = re.compile(rb"\bRFC822\.SIZE\s+(\d+)\b", re.IGNORECASE)
_FLAGS_RE = re.compile(rb"\bFLAGS\s+\((.*?)\)", re.IGNORECASE)
_BODYSTRUCTURE_RE = re.compile(rb"BODYSTRUCTURE\s+(?P<bodystructure>.+?)(?:\s+BODY\[|\s*\)\s*$)", re.IGNORECASE | re.DOTALL)
_ATTACHMENT_MARKER_RE = re.compile(rb'"(?:ATTACHMENT|INLINE|FILENAME|NAME)"', re.IGNORECASE)
_MESSAGE_ID_RE = re.compile(r"<([^<>]+)>")
_SUBJECT_PREFIX_RE = re.compile(r"^\s*(?:(?:re|fw|fwd)\s*:\s*)+", re.IGNORECASE)


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
            status, data = connection.select(_imap_mailbox_arg(folder), readonly=readonly)
        except socket.timeout as exc:
            raise MailTimeoutError(f"Timed out selecting IMAP folder {folder}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"IMAP folder selection connection failure for {folder}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailProtocolError(f"IMAP folder selection failed for {folder}") from exc
        self._expect_ok(status, data, f"IMAP folder selection failed for {folder}")

    def fetch_message_summaries(self, folder="INBOX", limit=50):
        return list(self.fetch_message_summary_page(folder=folder, limit=limit).messages)

    def fetch_account_summary(self):
        self.select_folder("INBOX", readonly=True)
        return MailboxAccountSummary(
            unread_count=self._search_count("UNSEEN"),
            important_count=self._search_count("FLAGGED"),
        )

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
                summary = _parse_summary_response(folder, uid.decode(), fetch_data)
                if summary.has_visible_attachments and _summary_needs_visible_attachment_refinement(fetch_data):
                    status, full_fetch_data = connection.uid("fetch", uid, "(FLAGS RFC822.SIZE RFC822)")
                    self._expect_ok(status, full_fetch_data, f"IMAP summary visibility fetch failed for UID {uid.decode()}")
                    detail = _parse_detail_response(folder, uid.decode(), full_fetch_data)
                    summary = replace(summary, has_visible_attachments=detail.has_visible_attachments)
                summaries.append(summary)
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

    def fetch_conversation_page(self, folder="INBOX", limit=50):
        connection = self._require_connection()
        self.select_folder(folder, readonly=True)
        if limit < 1:
            return MailConversationSummaryPage()
        try:
            status, data = connection.uid("search", None, "UNDELETED")
            self._expect_ok(status, data, f"IMAP conversation search failed for {folder}")
            uids = _parse_uid_list(data[0] or b"")
            summaries = []
            for uid_int in reversed(uids):
                uid = str(uid_int).encode("ascii")
                status, fetch_data = connection.uid(
                    "fetch",
                    uid,
                    "(FLAGS RFC822.SIZE BODYSTRUCTURE BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO CC DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])",
                )
                self._expect_ok(status, fetch_data, f"IMAP conversation fetch failed for UID {uid.decode()}")
                summary = _parse_summary_response(folder, uid.decode(), fetch_data)
                if summary.has_visible_attachments and _summary_needs_visible_attachment_refinement(fetch_data):
                    status, full_fetch_data = connection.uid("fetch", uid, "(FLAGS RFC822.SIZE RFC822)")
                    self._expect_ok(status, full_fetch_data, f"IMAP conversation visibility fetch failed for UID {uid.decode()}")
                    detail = _parse_detail_response(folder, uid.decode(), full_fetch_data)
                    summary = replace(summary, has_visible_attachments=detail.has_visible_attachments)
                summaries.append(summary)
            return _build_conversation_page(folder, summaries, limit)
        except socket.timeout as exc:
            raise MailTimeoutError(f"Timed out fetching IMAP conversations for {folder}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"IMAP conversation fetch connection failure for {folder}: {exc}") from exc
        except ValueError as exc:
            raise MailProtocolError(f"IMAP conversation fetch failed for {folder}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailProtocolError(f"IMAP conversation fetch failed for {folder}") from exc

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

    def fetch_attachments(self, folder, uid):
        _, message = self._fetch_full_message(folder, uid)
        return tuple(_extract_message_parts(message)[2])

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

    def _search_count(self, criterion):
        connection = self._require_connection()
        try:
            status, data = connection.uid("search", None, criterion)
            self._expect_ok(status, data, f"IMAP search failed for INBOX {criterion}")
            return len(_parse_uid_list((data or [b""])[0] or b""))
        except socket.timeout as exc:
            raise MailTimeoutError(f"Timed out counting IMAP INBOX {criterion}") from exc
        except (OSError, ssl.SSLError) as exc:
            raise MailConnectionError(f"IMAP count connection failure for INBOX {criterion}: {exc}") from exc
        except imaplib.IMAP4.error as exc:
            raise MailProtocolError(f"IMAP count failed for INBOX {criterion}") from exc

    def _move_message_to_trash(self, uid, trash_folder):
        self._move_message(uid, trash_folder, "move")

    def _move_message(self, uid, target_folder, operation_name):
        connection = self._require_connection()
        try:
            status, data = connection.uid("MOVE", uid, _imap_mailbox_arg(target_folder))
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
        status, data = connection.uid("COPY", uid, _imap_mailbox_arg(target_folder))
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
    return MailFolderSummary(name=name, delimiter=delimiter, flags=flags, path=name)


def _parse_summary_response(folder, fallback_uid, fetch_data):
    try:
        metadata, payload = _first_fetch_tuple(fetch_data)
        message = BytesParser(policy=policy.default).parsebytes(payload or b"")
        has_attachments = _has_attachment_bodystructure(metadata)
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
            has_attachments=has_attachments,
            has_visible_attachments=_has_visible_attachment_bodystructure(metadata, has_attachments),
            in_reply_to=_message_id_values(_header_value(message, "in-reply-to")),
            references=_message_id_values(_header_value(message, "references")),
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
    has_visible_attachments = _has_visible_attachments(attachment.summary for attachment in attachments)
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
        has_visible_attachments=has_visible_attachments,
    )


def _build_conversation_page(folder, summaries, limit):
    conversations_by_key = defaultdict(list)
    message_ids = {_normalize_message_id(summary.message_id): summary for summary in summaries if _normalize_message_id(summary.message_id)}
    for summary in summaries:
        conversations_by_key[_conversation_key(summary, message_ids)].append(summary)

    conversations = []
    for key, messages in conversations_by_key.items():
        ordered_messages = sorted(messages, key=_message_age_key)
        root = _conversation_root(ordered_messages, message_ids)
        replies = tuple(message for message in ordered_messages if message is not root)
        latest = max(ordered_messages, key=_message_activity_key)
        conversations.append(
            MailConversationSummary(
                conversation_id=_conversation_id(folder, key),
                message_count=len(ordered_messages),
                reply_count=len(replies),
                has_unread=any(not _message_is_seen(message) for message in ordered_messages),
                has_attachments=any(message.has_attachments for message in ordered_messages),
                has_visible_attachments=any(message.has_visible_attachments for message in ordered_messages),
                participants=_conversation_participants((root,) + replies),
                root_message=root,
                replies=replies,
                latest_date=latest.date,
            )
        )
    conversations.sort(key=_conversation_sort_key)
    return MailConversationSummaryPage(conversations=tuple(conversations[:limit]))


def _conversation_key(summary, message_ids):
    own_id = _normalize_message_id(summary.message_id)
    if _has_usable_thread_metadata(summary):
        parent_ids = _thread_parent_ids(summary)
        for parent_id in parent_ids:
            if parent_id in message_ids:
                return f"id:{_thread_root_id(message_ids[parent_id], message_ids)}"
        if parent_ids:
            return f"id:{parent_ids[0]}"
        if own_id:
            return f"id:{own_id}"
    subject = _normalize_thread_subject(summary.subject)
    if subject:
        return f"subject:{subject}"
    if own_id:
        return f"id:{own_id}"
    return f"uid:{summary.uid}"


def _has_usable_thread_metadata(summary):
    return bool(_normalize_message_id(summary.message_id) or summary.in_reply_to or summary.references)


def _thread_parent_ids(summary):
    parent_ids = []
    for value in summary.in_reply_to:
        normalized = _normalize_message_id(value)
        if normalized:
            parent_ids.append(normalized)
    for value in reversed(summary.references):
        normalized = _normalize_message_id(value)
        if normalized and normalized not in parent_ids:
            parent_ids.append(normalized)
    return parent_ids


def _thread_root_id(summary, message_ids, seen=None):
    seen = seen or set()
    own_id = _normalize_message_id(summary.message_id)
    if own_id:
        seen.add(own_id)
    available_parents = [parent_id for parent_id in reversed(summary.references) if parent_id in message_ids]
    available_parents.extend(parent_id for parent_id in summary.in_reply_to if parent_id in message_ids)
    for parent_id in available_parents:
        normalized_parent = _normalize_message_id(parent_id)
        if normalized_parent and normalized_parent not in seen:
            return _thread_root_id(message_ids[normalized_parent], message_ids, seen)
    return own_id or str(summary.uid)


def _conversation_root(messages, message_ids):
    message_set = set(messages)
    candidate_roots = []
    for message in messages:
        for parent_id in reversed(message.references):
            parent = message_ids.get(_normalize_message_id(parent_id))
            if parent in message_set and parent not in candidate_roots:
                candidate_roots.append(parent)
        for parent_id in message.in_reply_to:
            parent = message_ids.get(_normalize_message_id(parent_id))
            if parent in message_set and parent not in candidate_roots:
                candidate_roots.append(parent)
    if candidate_roots:
        return min(candidate_roots, key=_message_age_key)
    return min(messages, key=_message_age_key)


def _conversation_participants(messages):
    participants = []
    seen = set()
    for message in messages:
        raw_values = [message.sender, *message.to, *message.cc]
        for display_name, email_address in getaddresses(raw_values):
            normalized_email = (email_address or "").strip().lower()
            if not normalized_email or normalized_email in seen:
                continue
            seen.add(normalized_email)
            participants.append(MailConversationParticipant(name=(display_name or "").strip(), email=normalized_email))
    return tuple(participants)


def _conversation_id(folder, key):
    return hashlib.sha256(f"{folder}\0{key}".encode("utf-8", errors="replace")).hexdigest()[:16]


def _conversation_sort_key(conversation):
    latest_message = max((conversation.root_message,) + conversation.replies, key=_message_activity_key)
    latest_timestamp = latest_message.date.timestamp() if latest_message.date else 0
    return (-latest_timestamp, -_uid_int(latest_message.uid))


def _message_age_key(message):
    timestamp = message.date.timestamp() if message.date else 0
    return (timestamp, _uid_int(message.uid))


def _message_activity_key(message):
    timestamp = message.date.timestamp() if message.date else 0
    return (timestamp, _uid_int(message.uid))


def _message_is_seen(message):
    return any(flag.lower() == "seen" for flag in message.flags)


def _uid_int(uid):
    try:
        return int(uid)
    except (TypeError, ValueError):
        return 0


def _message_id_values(value):
    raw_value = str(value or "")
    ids = [_normalize_message_id(match) for match in _MESSAGE_ID_RE.findall(raw_value)]
    if ids:
        return tuple(id_value for id_value in ids if id_value)
    normalized = _normalize_message_id(raw_value)
    return (normalized,) if normalized else ()


def _normalize_message_id(value):
    normalized = str(value or "").strip().strip("<>").strip().lower()
    return normalized


def _normalize_thread_subject(value):
    subject = _SUBJECT_PREFIX_RE.sub("", str(value or "")).strip().lower()
    return re.sub(r"\s+", " ", subject)


def _first_fetch_tuple(fetch_data):
    for item in fetch_data or []:
        if isinstance(item, tuple) and len(item) >= 2:
            return item[0] or b"", item[1] or b""
    raise MailProtocolError("IMAP fetch response did not include message data")


def _extract_message_parts(message):
    text_parts = []
    html_parts = []
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_id = _content_id(part.get("Content-ID"))
        if _is_attachment_part(filename, disposition, content_id):
            continue
        if content_type == "text/plain":
            text_parts.append(_part_content(part))
        elif content_type == "text/html":
            html_parts.append(_part_content(part))
    text_body = "\n".join(filter(None, text_parts))
    html_body = "\n".join(filter(None, html_parts))
    if not text_body and html_body:
        text_body = _html_to_text(html_body)
    attachments = _extract_attachments(message, html_body)
    return text_body, html_body, attachments


def _extract_attachments(message, html_body=""):
    candidates = []
    referenced_content_hashes = set()
    cid_refs = _html_cid_refs(html_body)
    parts = message.walk() if message.is_multipart() else [message]
    for part in parts:
        if part.is_multipart():
            continue
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        content_id = _content_id(part.get("Content-ID"))
        if not _is_attachment_part(filename, disposition, content_id):
            continue
        content = part.get_payload(decode=True) or b""
        content_hash = _content_hash(content)
        candidate = {
            "filename": filename,
            "content_type": _attachment_content_type(part, filename),
            "size": len(content),
            "disposition": disposition,
            "is_inline": disposition == "inline",
            "content_id": content_id,
            "content": content,
            "content_hash": content_hash,
        }
        if _is_referenced_cid_image(candidate, cid_refs):
            referenced_content_hashes.add(content_hash)
        candidates.append(candidate)
    attachments = []
    for candidate in candidates:
        is_visible = _attachment_candidate_is_visible(candidate, referenced_content_hashes, cid_refs)
        attachments.append(
            MailAttachmentContent(
                summary=MailAttachmentSummary(
                    id=f"att_{len(attachments) + 1}",
                    filename=candidate["filename"],
                    content_type=candidate["content_type"],
                    size=candidate["size"],
                    disposition=candidate["disposition"],
                    is_inline=candidate["is_inline"],
                    content_id=candidate["content_id"],
                    is_visible=is_visible,
                ),
                content=candidate["content"],
            )
        )
    return attachments


def _is_attachment_part(filename, disposition, content_id=""):
    return bool(filename) or disposition == "attachment" or (disposition == "inline" and bool(content_id))


def _attachment_content_type(part, filename):
    content_type = part.get_content_type() or "application/octet-stream"
    if content_type != "application/octet-stream" or not filename:
        return content_type
    guessed_type, _ = mimetypes.guess_type(filename)
    return guessed_type or content_type


def _content_id(value):
    content_id = str(value or "").strip()
    if content_id.startswith("<") and content_id.endswith(">"):
        return content_id[1:-1].strip()
    return content_id


def _content_hash(content):
    return hashlib.sha256(content or b"").hexdigest()


def _html_cid_refs(html_body):
    return {unquote(match).strip("<>") for match in re.findall(r"cid:([^\"'>\s)]+)", html_body or "", re.IGNORECASE)}


def _cid_referenced(content_id, cid_refs):
    return content_id in cid_refs


def _attachment_candidate_is_visible(candidate, referenced_content_hashes, cid_refs):
    if candidate["is_inline"] and candidate["content_id"] and _cid_referenced(candidate["content_id"], cid_refs):
        return False
    if _is_referenced_cid_image(candidate, cid_refs):
        return False
    if candidate["content_type"].startswith("image/") and candidate["content_hash"] in referenced_content_hashes:
        return False
    return True


def _is_referenced_cid_image(candidate, cid_refs):
    return (
        candidate["content_type"].startswith("image/")
        and bool(candidate["content_id"])
        and _cid_referenced(candidate["content_id"], cid_refs)
    )


def _has_visible_attachments(attachments):
    for attachment in attachments:
        if attachment.is_visible:
            return True
    return False


def _part_content(part):
    try:
        return part.get_content()
    except LookupError as exc:
        raise MailProtocolError(f"Unsupported message charset: {exc}") from exc


class _HtmlTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "p",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._chunks = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in {"script", "style"} and self._skip_depth:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in self.BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._chunks.append(data)

    def text(self):
        lines = []
        for line in "".join(self._chunks).splitlines():
            normalized = re.sub(r"[ \t\r\f\v]+", " ", line).strip()
            if normalized:
                lines.append(normalized)
        return "\n".join(lines)


def _html_to_text(html_body):
    extractor = _HtmlTextExtractor()
    try:
        extractor.feed(html_body)
        extractor.close()
    except Exception:
        return ""
    return extractor.text()


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


def _has_visible_attachment_bodystructure(metadata, has_attachments):
    match = _BODYSTRUCTURE_RE.search(metadata or b"")
    if not match:
        return has_attachments
    try:
        parts = list(_iter_bodystructure_parts(_parse_bodystructure(match.group("bodystructure"))))
    except (TypeError, ValueError):
        return has_attachments
    found_attachment_like = False
    for part in parts:
        disposition = _bodystructure_disposition(part)
        has_name = _bodystructure_has_name(part)
        content_id = _content_id(_bodystructure_value(part, 3))
        if disposition in {"attachment", "inline"} or has_name:
            found_attachment_like = True
        if disposition == "attachment" or (has_name and not (disposition == "inline" and content_id)):
            return True
    return has_attachments if not found_attachment_like else False


def _summary_needs_visible_attachment_refinement(fetch_data):
    try:
        metadata, _ = _first_fetch_tuple(fetch_data)
        match = _BODYSTRUCTURE_RE.search(metadata or b"")
        if not match:
            return False
        parts = list(_iter_bodystructure_parts(_parse_bodystructure(match.group("bodystructure"))))
    except (MailProtocolError, TypeError, ValueError):
        return False
    attachment_like_parts = []
    for part in parts:
        disposition = _bodystructure_disposition(part)
        if disposition in {"attachment", "inline"} or _bodystructure_has_name(part):
            attachment_like_parts.append(part)
    if not attachment_like_parts:
        return False
    has_inline_or_cid = any(_bodystructure_disposition(part) == "inline" or _content_id(_bodystructure_value(part, 3)) for part in attachment_like_parts)
    return has_inline_or_cid and all(str(_bodystructure_value(part, 0) or "").lower() == "image" for part in attachment_like_parts)


def _parse_bodystructure(raw):
    text = _safe_decode(raw)
    value, index = _parse_bodystructure_value(text, 0)
    while index < len(text) and text[index].isspace():
        index += 1
    return value


def _parse_bodystructure_value(text, index):
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        raise ValueError("Unexpected end of BODYSTRUCTURE")
    if text[index] == "(":
        values = []
        index += 1
        while True:
            while index < len(text) and text[index].isspace():
                index += 1
            if index >= len(text):
                raise ValueError("Unterminated BODYSTRUCTURE list")
            if text[index] == ")":
                return values, index + 1
            value, index = _parse_bodystructure_value(text, index)
            values.append(value)
    if text[index] == '"':
        return _parse_bodystructure_quoted(text, index)
    start = index
    while index < len(text) and not text[index].isspace() and text[index] not in "()":
        index += 1
    atom = text[start:index]
    if atom.upper() == "NIL":
        return None, index
    return atom, index


def _parse_bodystructure_quoted(text, index):
    chars = []
    index += 1
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            chars.append(text[index + 1])
            index += 2
            continue
        if char == '"':
            return "".join(chars), index + 1
        chars.append(char)
        index += 1
    raise ValueError("Unterminated BODYSTRUCTURE quoted string")


def _iter_bodystructure_parts(value):
    if not isinstance(value, list):
        return
    if len(value) >= 2 and isinstance(value[0], str) and isinstance(value[1], str):
        yield value
        return
    for child in value:
        yield from _iter_bodystructure_parts(child)


def _bodystructure_value(part, index):
    return part[index] if len(part) > index else None


def _bodystructure_disposition(part):
    for value in part[7:]:
        if isinstance(value, list) and value and isinstance(value[0], str) and value[0].lower() in {"attachment", "inline"}:
            return value[0].lower()
    return ""


def _bodystructure_has_name(part):
    return _bodystructure_param_has_key(_bodystructure_value(part, 2), "name") or any(
        isinstance(value, list) and len(value) > 1 and _bodystructure_param_has_key(value[1], "filename") for value in part[7:]
    )


def _bodystructure_param_has_key(params, key):
    if not isinstance(params, list):
        return False
    return any(isinstance(value, str) and value.lower() == key for value in params[::2])


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
        decoded = decoded[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return _modified_utf7_decode(decoded)


def _imap_mailbox_arg(value):
    mailbox = _modified_utf7_encode(_modified_utf7_decode(str(value or "")))
    escaped = mailbox.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'.encode("ascii")


def _modified_utf7_decode(value):
    result = []
    index = 0
    while index < len(value):
        if value[index] != "&":
            result.append(value[index])
            index += 1
            continue
        end = value.find("-", index)
        if end == -1:
            result.append(value[index])
            index += 1
            continue
        encoded = value[index + 1 : end]
        if encoded == "":
            result.append("&")
        else:
            padding = "=" * (-len(encoded) % 4)
            try:
                data = base64.b64decode((encoded.replace(",", "/") + padding).encode("ascii"), validate=True)
                result.append(data.decode("utf-16-be"))
            except (LookupError, UnicodeError, ValueError):
                result.append(value[index : end + 1])
        index = end + 1
    return "".join(result)


def _modified_utf7_encode(value):
    chunks = []
    unicode_buffer = []

    def flush_unicode_buffer():
        if not unicode_buffer:
            return
        data = "".join(unicode_buffer).encode("utf-16-be")
        encoded = base64.b64encode(data).decode("ascii").rstrip("=").replace("/", ",")
        chunks.append(f"&{encoded}-")
        unicode_buffer.clear()

    for char in value:
        codepoint = ord(char)
        if char == "&":
            flush_unicode_buffer()
            chunks.append("&-")
        elif 0x20 <= codepoint <= 0x7E:
            flush_unicode_buffer()
            chunks.append(char)
        else:
            unicode_buffer.append(char)
    flush_unicode_buffer()
    return "".join(chunks)


def _safe_decode(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)
