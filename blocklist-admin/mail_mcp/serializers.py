from datetime import datetime

from mail_integration.imap_client import _html_to_text
from mail_integration.schemas import MailFolderSummary, MailMessageDetail, MailMessageSummary


def clamp_limit(limit: int | None, max_limit: int) -> int:
    if limit is None:
        return min(25, max_limit)
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return min(25, max_limit)
    return max(1, min(value, max_limit))


def truncate_text(value: str, max_chars: int) -> tuple[str, bool]:
    text = value or ""
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.isoformat() + "Z"
    return value.isoformat()


def folder_to_dict(folder: MailFolderSummary) -> dict:
    return {
        "path": folder.path or folder.name,
        "name": folder.name,
        "display_name": folder.display_name or folder.name,
        "parent_path": folder.parent_path,
        "depth": folder.depth,
        "delimiter": folder.delimiter,
        "flags": list(folder.flags),
        "selectable": folder.selectable,
    }


def summary_to_dict(summary: MailMessageSummary, source: str | None = None) -> dict:
    payload = {
        "uid": summary.uid,
        "folder": summary.folder,
        "subject": summary.subject,
        "sender": summary.sender,
        "to": list(summary.to),
        "cc": list(summary.cc),
        "date": isoformat(summary.date),
        "message_id": summary.message_id,
        "flags": list(summary.flags),
        "size": summary.size,
        "has_attachments": summary.has_attachments,
        "has_visible_attachments": summary.has_visible_attachments,
    }
    if source:
        payload["source"] = source
    return payload


def detail_to_dict(detail: MailMessageDetail, max_body_chars: int) -> dict:
    body = (detail.text_body or "").strip()
    body_format = "text"
    if not body and detail.html_body:
        body = _html_to_text(detail.html_body).strip()
        body_format = "html_converted"
    body, truncated = truncate_text(body, max_body_chars)
    return {
        "uid": detail.uid,
        "folder": detail.folder,
        "subject": detail.subject,
        "sender": detail.sender,
        "to": list(detail.to),
        "cc": list(detail.cc),
        "date": isoformat(detail.date),
        "message_id": detail.message_id,
        "flags": list(detail.flags),
        "size": detail.size,
        "body": body,
        "body_format": body_format,
        "body_truncated": truncated,
        "has_visible_attachments": detail.has_visible_attachments,
        "attachments": [
            {
                "id": attachment.id,
                "filename": attachment.filename,
                "content_type": attachment.content_type,
                "size": attachment.size,
                "is_visible": attachment.is_visible,
            }
            for attachment in detail.attachments
        ],
    }
