import hashlib
from email.utils import getaddresses

from mail_integration.imap_client import (
    _conversation_key,
    _folder_direction,
    _message_id_values,
    _message_is_seen,
    _normalize_message_id,
    _normalize_thread_subject,
    _same_folder,
)
from mail_integration.schemas import MailMessageSummary


def normalize_email(value):
    return str(value or "").strip().lower()


def normalize_message_id(value):
    return _normalize_message_id(value)


def normalize_subject(value):
    return _normalize_thread_subject(value)


def same_folder(left, right):
    return _same_folder(left, right)


def message_is_seen(summary):
    return _message_is_seen(summary)


def compute_thread_key(summary, message_ids):
    return _conversation_key(summary, message_ids)


def compute_conversation_id(account_email, thread_key):
    normalized_email = normalize_email(account_email)
    return hashlib.sha256(f"{normalized_email}\0{thread_key}".encode("utf-8", errors="replace")).hexdigest()[:32]


def compute_dedupe_key(summary):
    message_id = normalize_message_id(summary.message_id)
    if message_id:
        return f"msg:{message_id}"
    return f"uid:{str(summary.folder).strip().lower()}:{summary.uid}"


def infer_direction(summary, account_email, sent_folder):
    folder_direction = _folder_direction(summary.folder, sent_folder)
    inferred = infer_direction_from_headers(summary, account_email)
    return inferred or folder_direction


def infer_direction_from_headers(summary, account_email):
    normalized_account = normalize_email(account_email)
    if not normalized_account:
        return None
    if first_email(summary.sender) == normalized_account:
        return "outbound"
    recipients = set()
    for value in (*summary.to, *summary.cc):
        recipients.update(normalize_email(email) for _, email in getaddresses([str(value or "")]))
    if normalized_account in recipients:
        return "inbound"
    return None


def first_email(value):
    addresses = getaddresses([str(value or "")])
    if not addresses:
        return ""
    return normalize_email(addresses[0][1])


def first_address(value):
    addresses = getaddresses([str(value or "")])
    if not addresses:
        return "", ""
    name, email = addresses[0]
    return (name or "").strip(), normalize_email(email)


def ids_header_value(values):
    return " ".join(f"<{value}>" for value in values if value)


def message_id_values(value):
    return _message_id_values(value)


def summary_thread_parent_values(summary):
    in_reply_to = tuple(normalize_message_id(value) for value in summary.in_reply_to if normalize_message_id(value))
    references = tuple(normalize_message_id(value) for value in summary.references if normalize_message_id(value))
    return in_reply_to, references


def summary_from_message_row(row):
    return MailMessageSummary(
        uid=str(row.uid),
        folder=row.folder,
        subject=row.subject,
        sender=row.sender_raw or format_address(row.sender_name, row.sender_email),
        to=tuple(row.to_json or []),
        cc=tuple(row.cc_json or []),
        date=row.sent_at,
        message_id=row.raw_headers_json.get("message_id") or row.message_id,
        flags=tuple(row.flags_json or []),
        size=row.size,
        has_attachments=row.has_attachments,
        has_visible_attachments=row.has_visible_attachments,
        in_reply_to=message_id_values(row.in_reply_to),
        references=message_id_values(row.references_raw),
    )


def format_address(name, email):
    if name and email:
        return f"{name} <{email}>"
    return email or name or ""


def uid_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
