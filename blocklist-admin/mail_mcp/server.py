import json
import logging
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mailadmin_project.settings")
django.setup()

from mcp.server.mcpserver import MCPServer

from mail_integration.exceptions import MailIntegrationError, MailInvalidOperationError
from mail_integration.mailbox_service import MailboxService
from mail_integration.schemas import MailboxCredentials

from .config import MailMcpSettings, load_settings
from .search import (
    IndexSearchError,
    folder_is_indexed,
    index_freshness,
    parse_since,
    resolve_account_index,
    search_index,
)
from .serializers import clamp_limit, detail_to_dict, folder_to_dict, summary_to_dict
from .writes import (
    after_flags_changed,
    after_messages_removed,
    after_send,
    build_send_request,
)


logger = logging.getLogger("mail_mcp")

mcp = MCPServer(
    name="finestar-mail",
    instructions=(
        "Read/write access to one Finestar mailbox. "
        "Read: list_folders, list_messages, read_message, search_mail. "
        "search_mail uses the Postgres header index (INBOX+Sent; no body). "
        "Set include_body=true for IMAP body search or non-indexed folders. "
        "Write: move_message, delete_message, set_flags, send_mail. "
        "Prefer soft delete (delete_message permanent=false, or move_message to Trash). "
        "Use permanent=true ONLY when the user explicitly says trajno/zauvijek/permanently/"
        "forever — never for ordinary 'delete' or 'obriši'. "
        "set_flags supports Seen, Flagged, Answered. "
        "send_mail uses SMTP via the existing mailadmin stack; pass reply_to_uid to reply. "
        "send_mail accepts from_ (alias From, e.g. accounting@finestar.hr when it maps to "
        "this mailbox in postfix-virtual.cf) and attachments as "
        "[{filename, content_base64, mime_type?}]."
    ),
)

_settings: MailMcpSettings | None = None
_mailbox_service = MailboxService()


def get_settings() -> MailMcpSettings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def _credentials() -> MailboxCredentials:
    settings = get_settings()
    return MailboxCredentials(email=settings.mailbox_email, password=settings.mailbox_password)


def _json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _error(code: str, detail: str = "") -> str:
    payload = {"ok": False, "error": code}
    if detail:
        payload["detail"] = detail
    return _json(payload)


@mcp.tool()
def list_folders() -> str:
    """List IMAP folders for the configured mailbox."""
    folders = _mailbox_service.list_folders(_credentials())
    return _json(
        {
            "account_email": get_settings().mailbox_email,
            "folders": [folder_to_dict(folder) for folder in folders],
        }
    )


@mcp.tool()
def list_messages(folder: str = "INBOX", limit: int = 25, before_uid: str = "") -> str:
    """List recent message summaries from IMAP (always fresh)."""
    settings = get_settings()
    safe_limit = clamp_limit(limit, settings.max_limit)
    page = _mailbox_service.list_message_summary_page(
        _credentials(),
        folder=(folder or "INBOX").strip() or "INBOX",
        limit=safe_limit,
        before_uid=(before_uid or "").strip() or None,
    )
    return _json(
        {
            "account_email": settings.mailbox_email,
            "folder": (folder or "INBOX").strip() or "INBOX",
            "has_more": page.has_more,
            "next_before_uid": page.next_before_uid,
            "messages": [summary_to_dict(message, source="imap") for message in page.messages],
        }
    )


@mcp.tool()
def read_message(folder: str, uid: str) -> str:
    """Read one message via IMAP without marking it as seen. Attachments are metadata only."""
    settings = get_settings()
    detail = _mailbox_service.peek_message_detail(
        _credentials(),
        folder=(folder or "INBOX").strip() or "INBOX",
        uid=str(uid).strip(),
    )
    payload = detail_to_dict(detail, max_body_chars=settings.max_body_chars)
    payload["account_email"] = settings.mailbox_email
    payload["source"] = "imap"
    return _json(payload)


@mcp.tool()
def search_mail(
    query: str,
    folder: str = "",
    since: str = "",
    limit: int = 25,
    include_body: bool = False,
) -> str:
    """Search mail. Default path uses the Postgres header index (INBOX+Sent).

    Set include_body=true to search message bodies over IMAP, or when searching
    folders that are not indexed. Response includes source/stale metadata.
    """
    settings = get_settings()
    safe_limit = clamp_limit(limit, settings.max_limit)
    normalized_query = (query or "").strip()
    if not normalized_query:
        return _json({"error": "query_required", "messages": []})

    since_dt = parse_since(since) if since else None
    folder_name = (folder or "").strip()

    try:
        account = resolve_account_index(settings.mailbox_email, settings.index_username)
    except IndexSearchError as exc:
        return _json({"error": "index_ambiguous", "detail": str(exc), "messages": []})

    freshness = index_freshness(account, settings.stale_after_seconds)
    use_imap = bool(include_body) or (bool(folder_name) and not folder_is_indexed(account, folder_name))

    if not use_imap and freshness["usable"]:
        messages = search_index(
            account,
            query=normalized_query,
            folder=folder_name,
            since=since_dt,
            limit=safe_limit,
        )
        return _json(
            {
                "account_email": settings.mailbox_email,
                "query": normalized_query,
                "folder": folder_name or None,
                "since": since or None,
                "include_body": False,
                "source": "index",
                "messages": messages,
                **freshness,
            }
        )

    if not use_imap and not freshness["usable"]:
        use_imap = True

    imap_folder = folder_name or "INBOX"
    page = _mailbox_service.search_message_summaries(
        _credentials(),
        folder=imap_folder,
        query=normalized_query,
        since=since_dt,
        limit=safe_limit,
    )
    return _json(
        {
            "account_email": settings.mailbox_email,
            "query": normalized_query,
            "folder": imap_folder,
            "since": since or None,
            "include_body": bool(include_body),
            "source": "imap",
            "has_more": page.has_more,
            "messages": [summary_to_dict(message, source="imap") for message in page.messages],
            **freshness,
            "fallback_reason": (
                "include_body"
                if include_body
                else ("folder_not_indexed" if folder_name and not folder_is_indexed(account, folder_name) else "index_unusable")
            ),
        }
    )


@mcp.tool()
def move_message(folder: str, uid: str, dest_folder: str) -> str:
    """Move one message to another IMAP folder (Archive, Junk, Trash, etc.)."""
    settings = get_settings()
    source = (folder or "INBOX").strip() or "INBOX"
    dest = (dest_folder or "").strip()
    message_uid = str(uid).strip()
    if not dest:
        return _error("invalid_target_folder")
    if not message_uid:
        return _error("invalid_uid")
    try:
        result = _mailbox_service.move_messages_to_folder(
            _credentials(),
            folder=source,
            target_folder=dest,
            uids=(message_uid,),
        )
    except MailInvalidOperationError as exc:
        return _error(str(exc) or "invalid_move")
    except MailIntegrationError as exc:
        return _error("mail_error", str(exc))
    after_messages_removed(settings.mailbox_email, settings.index_username, source, result.moved)
    return _json(
        {
            "ok": bool(result.moved) and not result.failed,
            "account_email": settings.mailbox_email,
            "folder": source,
            "uid": message_uid,
            "dest_folder": result.target_folder,
            "moved": list(result.moved),
            "failed": [{"uid": item.uid, "error": item.error, "detail": item.detail} for item in result.failed],
        }
    )


@mcp.tool()
def delete_message(folder: str, uid: str, permanent: bool = False) -> str:
    """Delete one message. Default is soft delete into Trash.

    Set permanent=true ONLY when the user explicitly says trajno/zauvijek/permanently/forever.
    Ordinary 'delete' / 'obriši' must keep permanent=false.
    """
    settings = get_settings()
    source = (folder or "INBOX").strip() or "INBOX"
    message_uid = str(uid).strip()
    if not message_uid:
        return _error("invalid_uid")
    try:
        if permanent:
            result = _mailbox_service.permanently_delete_messages(
                _credentials(), folder=source, uids=(message_uid,)
            )
            removed = result.deleted
            failed = result.failed
            mode = "permanent"
            trash_folder = None
        else:
            result = _mailbox_service.move_messages_to_trash(
                _credentials(), folder=source, uids=(message_uid,)
            )
            removed = result.moved_to_trash
            failed = result.failed
            mode = "trash"
            trash_folder = result.trash_folder
    except MailInvalidOperationError as exc:
        return _error(str(exc) or "invalid_delete")
    except MailIntegrationError as exc:
        return _error("mail_error", str(exc))
    after_messages_removed(settings.mailbox_email, settings.index_username, source, removed)
    return _json(
        {
            "ok": bool(removed) and not failed,
            "account_email": settings.mailbox_email,
            "folder": source,
            "uid": message_uid,
            "mode": mode,
            "trash_folder": trash_folder,
            "removed": list(removed),
            "failed": [{"uid": item.uid, "error": item.error, "detail": item.detail} for item in failed],
        }
    )


@mcp.tool()
def set_flags(folder: str, uid: str, add: list[str] | None = None, remove: list[str] | None = None) -> str:
    """Add/remove IMAP flags. Allowed: Seen, Flagged, Answered."""
    settings = get_settings()
    source = (folder or "INBOX").strip() or "INBOX"
    message_uid = str(uid).strip()
    add_flags = list(add or [])
    remove_flags = list(remove or [])
    if not message_uid:
        return _error("invalid_uid")
    try:
        result = _mailbox_service.store_message_flags(
            _credentials(),
            folder=source,
            uid=message_uid,
            add=tuple(add_flags),
            remove=tuple(remove_flags),
        )
    except MailInvalidOperationError as exc:
        return _error(str(exc) or "invalid_flags")
    except MailIntegrationError as exc:
        return _error("mail_error", str(exc))
    after_flags_changed(
        settings.mailbox_email,
        settings.index_username,
        source,
        message_uid,
        result["added"],
        result["removed"],
    )
    return _json(
        {
            "ok": True,
            "account_email": settings.mailbox_email,
            "folder": source,
            "uid": message_uid,
            "added": list(result["added"]),
            "removed": list(result["removed"]),
        }
    )


@mcp.tool()
def send_mail(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    from_: str = "",
    from_name: str = "",
    attachments: list | None = None,
    reply_to_uid: str = "",
    reply_folder: str = "INBOX",
) -> str:
    """Send a new message or reply via the existing mailadmin SMTP path.

    Pass reply_to_uid (and optional reply_folder) to set In-Reply-To / References.
    from_ may be the mailbox login or an alias that forwards to it (e.g. accounting@).
    attachments is a list of {filename, content_base64, mime_type?} objects.
    """
    settings = get_settings()
    source_detail = None
    reply_uid = (reply_to_uid or "").strip()
    try:
        if reply_uid:
            source_detail = _mailbox_service.peek_message_detail(
                _credentials(),
                folder=(reply_folder or "INBOX").strip() or "INBOX",
                uid=reply_uid,
            )
        request = build_send_request(
            to=to,
            subject=subject,
            body=body,
            cc=cc,
            from_email=from_,
            from_name=from_name,
            attachments=attachments,
            source=source_detail,
        )
        message_id = _mailbox_service.send_mail(_credentials(), request)
    except ValueError as exc:
        return _error(str(exc) or "invalid_send")
    except MailInvalidOperationError as exc:
        return _error(str(exc) or "invalid_send")
    except MailIntegrationError as exc:
        return _error("mail_error", str(exc))
    after_send(settings.mailbox_email, settings.index_username)
    return _json(
        {
            "ok": True,
            "account_email": settings.mailbox_email,
            "status": "sent",
            "message_id": message_id,
            "from": request.from_email or settings.mailbox_email,
            "from_name": request.from_display_name or None,
            "to": list(request.to),
            "cc": list(request.cc),
            "subject": request.subject,
            "attachment_count": len(request.attachments),
            "reply_to_uid": reply_uid or None,
        }
    )
