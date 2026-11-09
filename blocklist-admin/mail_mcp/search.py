from datetime import datetime
from typing import Any

from django.db.models import Q
from django.utils import timezone

from mailops.mail_indexing.threading import normalize_email, same_folder
from mailops.models import MailAccountIndex, MailMessageIndex

from .serializers import isoformat


class IndexSearchError(Exception):
    pass


def resolve_account_index(account_email: str, index_username: str = "") -> MailAccountIndex | None:
    normalized = normalize_email(account_email)
    queryset = MailAccountIndex.objects.filter(account_email=normalized).select_related("user")
    username = (index_username or "").strip()
    if username:
        queryset = queryset.filter(user__username=username)
    accounts = list(queryset[:2])
    if not accounts:
        return None
    if len(accounts) > 1:
        raise IndexSearchError(
            "Multiple MailAccountIndex rows for this mailbox; set MAIL_MCP_INDEX_USERNAME "
            f"(candidates: {', '.join(account.user.username for account in accounts)})"
        )
    return accounts[0]


def indexed_folders_for(account: MailAccountIndex | None) -> list[str]:
    if account is None:
        return []
    folders = [state.folder for state in account.folder_states.all()]
    if not folders:
        folders = ["INBOX"]
        if account.sent_folder:
            folders.append(account.sent_folder)
    return folders


def folder_is_indexed(account: MailAccountIndex | None, folder: str) -> bool:
    if account is None:
        return False
    folder = (folder or "").strip()
    if not folder:
        return True
    return any(same_folder(folder, indexed) for indexed in indexed_folders_for(account))


def index_freshness(account: MailAccountIndex | None, stale_after_seconds: int) -> dict[str, Any]:
    if account is None:
        return {
            "index_status": "missing",
            "index_last_indexed_at": None,
            "indexed_folders": [],
            "stale": True,
            "usable": False,
        }
    candidates = [account.last_indexed_at, account.last_sync_finished_at]
    candidates.extend(state.last_synced_at for state in account.folder_states.all())
    last_indexed_at = max((value for value in candidates if value is not None), default=None)
    stale = True
    if last_indexed_at is not None:
        age = timezone.now() - last_indexed_at
        stale = age.total_seconds() > stale_after_seconds
    usable = account.index_status in {
        MailAccountIndex.STATUS_READY,
        MailAccountIndex.STATUS_PARTIAL,
    } and account.messages.exists()
    return {
        "index_status": account.index_status,
        "index_last_indexed_at": isoformat(last_indexed_at),
        "indexed_folders": indexed_folders_for(account),
        "stale": stale,
        "usable": usable,
    }


def search_index(
    account: MailAccountIndex,
    query: str,
    folder: str = "",
    since: datetime | None = None,
    limit: int = 25,
) -> list[dict[str, Any]]:
    query = (query or "").strip()
    if not query:
        return []
    filters = (
        Q(subject__icontains=query)
        | Q(normalized_subject__icontains=query)
        | Q(sender_email__icontains=query)
        | Q(sender_name__icontains=query)
        | Q(sender_raw__icontains=query)
        | Q(to_json__icontains=query)
        | Q(cc_json__icontains=query)
    )
    queryset = MailMessageIndex.objects.filter(account=account).filter(filters)
    folder = (folder or "").strip()
    if folder:
        queryset = queryset.filter(folder__iexact=folder)
    if since is not None:
        queryset = queryset.filter(sent_at__gte=since)
    rows = list(queryset.order_by("-sent_at", "-uid")[:limit])
    return [message_row_to_result(row) for row in rows]


def message_row_to_result(row: MailMessageIndex) -> dict[str, Any]:
    sender = row.sender_raw or ""
    if not sender and (row.sender_name or row.sender_email):
        if row.sender_name and row.sender_email:
            sender = f"{row.sender_name} <{row.sender_email}>"
        else:
            sender = row.sender_name or row.sender_email
    return {
        "uid": str(row.uid),
        "folder": row.folder,
        "subject": row.subject,
        "sender": sender,
        "to": list(row.to_json or []),
        "cc": list(row.cc_json or []),
        "date": isoformat(row.sent_at),
        "message_id": row.message_id,
        "flags": list(row.flags_json or []),
        "size": row.size,
        "has_attachments": row.has_attachments,
        "has_visible_attachments": row.has_visible_attachments,
        "source": "index",
    }


def parse_since(value: str | None) -> datetime | None:
    raw = (value or "").strip()
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed
