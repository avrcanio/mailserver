"""Helpers for MCP write tools (recipients, reply threading, index updates)."""

from __future__ import annotations

import base64
import binascii
import re

from mail_integration.schemas import MailMessageDetail, SendMailAttachment, SendMailRequest
from mailops.api import (
    mark_index_message_read,
    mark_index_message_unread,
    mark_mail_index_stale_after_send,
    remove_indexed_messages_after_delete,
)

from .search import resolve_account_index


_EMAIL_SPLIT_RE = re.compile(r"[,\n;]+")
_SUBJECT_REPLY_RE = re.compile(r"^\s*(?:re\s*:\s*)+", re.IGNORECASE)
MAX_RECIPIENTS = 20
MAX_SUBJECT_CHARS = 998
MAX_MCP_ATTACHMENTS = 10
# Per-file limit after base64 decode (must stay aligned with MailboxService).
MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def parse_address_list(value: str | list | tuple | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        parts = [str(item).strip() for item in value]
    else:
        parts = [part.strip() for part in _EMAIL_SPLIT_RE.split(str(value))]
    addresses = tuple(part for part in parts if part)
    if len(addresses) > MAX_RECIPIENTS:
        raise ValueError("too_many_recipients")
    return addresses


def normalize_subject(subject: str) -> str:
    text = (subject or "").strip()
    if len(text) > MAX_SUBJECT_CHARS:
        return text[:MAX_SUBJECT_CHARS]
    return text


def build_reply_headers(source: MailMessageDetail, subject: str) -> dict:
    message_id = (source.message_id or "").strip()
    references: list[str] = []
    # MailMessageDetail does not expose references; use message_id chain only.
    if message_id:
        references.append(message_id)
    reply_subject = normalize_subject(subject)
    if not reply_subject:
        base = (source.subject or "").strip() or "(no subject)"
        reply_subject = base if _SUBJECT_REPLY_RE.match(base) else f"Re: {base}"
    elif not _SUBJECT_REPLY_RE.match(reply_subject):
        # Keep caller subject as-is when they provided one; only auto-prefix empty subject above.
        pass
    return {
        "subject": reply_subject,
        "in_reply_to": message_id,
        "references": tuple(references),
    }


def decode_mcp_attachments(attachments) -> tuple[SendMailAttachment, ...]:
    if attachments is None:
        return ()
    if not isinstance(attachments, (list, tuple)):
        raise ValueError("invalid_attachments")
    if len(attachments) > MAX_MCP_ATTACHMENTS:
        raise ValueError("too_many_attachments")
    decoded: list[SendMailAttachment] = []
    for item in attachments:
        if not isinstance(item, dict):
            raise ValueError("invalid_attachment")
        filename = str(item.get("filename") or "").strip()
        if not filename:
            raise ValueError("attachment_filename_required")
        raw_b64 = item.get("content_base64")
        if raw_b64 is None or str(raw_b64).strip() == "":
            raise ValueError("attachment_content_required")
        try:
            content = base64.b64decode(str(raw_b64), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("attachment_invalid_base64") from exc
        if len(content) > MAX_ATTACHMENT_BYTES:
            raise ValueError("attachment_too_large")
        mime_type = str(item.get("mime_type") or item.get("content_type") or "").strip()
        if not mime_type:
            mime_type = "application/octet-stream"
        decoded.append(
            SendMailAttachment(
                filename=filename,
                content_type=mime_type,
                content=content,
            )
        )
    return tuple(decoded)


def build_send_request(
    *,
    to: str | list | tuple,
    subject: str,
    body: str,
    cc: str | list | tuple = "",
    from_email: str = "",
    from_name: str = "",
    attachments=None,
    source: MailMessageDetail | None = None,
) -> SendMailRequest:
    to_addrs = parse_address_list(to)
    if not to_addrs:
        raise ValueError("to_required")
    cc_addrs = parse_address_list(cc)
    subject_text = normalize_subject(subject)
    text_body = body or ""
    in_reply_to = ""
    references: tuple[str, ...] = ()
    if source is not None:
        reply = build_reply_headers(source, subject_text)
        subject_text = reply["subject"]
        in_reply_to = reply["in_reply_to"]
        references = reply["references"]
    elif not subject_text:
        raise ValueError("subject_required")
    return SendMailRequest(
        to=to_addrs,
        cc=cc_addrs,
        subject=subject_text,
        text_body=text_body,
        in_reply_to=in_reply_to,
        references=references,
        from_email=(from_email or "").strip().lower(),
        from_display_name=(from_name or "").strip(),
        attachments=decode_mcp_attachments(attachments),
    )


def index_user_for_mailbox(mailbox_email: str, index_username: str = ""):
    account = resolve_account_index(mailbox_email, index_username)
    if account is None:
        return None
    return account.user


def after_messages_removed(mailbox_email: str, index_username: str, folder: str, uids):
    user = index_user_for_mailbox(mailbox_email, index_username)
    if user is None:
        return
    remove_indexed_messages_after_delete(user, mailbox_email, folder, tuple(uids))


def after_send(mailbox_email: str, index_username: str):
    user = index_user_for_mailbox(mailbox_email, index_username)
    if user is None:
        return
    mark_mail_index_stale_after_send(user, mailbox_email)


def after_flags_changed(mailbox_email: str, index_username: str, folder: str, uid: str, added, removed):
    user = index_user_for_mailbox(mailbox_email, index_username)
    if user is None:
        return
    added_lower = {str(flag).lower() for flag in added}
    removed_lower = {str(flag).lower() for flag in removed}
    if "seen" in added_lower:
        mark_index_message_read(user, mailbox_email, folder, uid)
    if "seen" in removed_lower:
        mark_index_message_unread(user, mailbox_email, folder, uid)
