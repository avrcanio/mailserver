import logging

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from mail_integration.imap_client import (
    _conversation_participants,
    _dedupe_unified_items,
    _message_activity_key,
    _unified_item_sort_key,
)
from mail_integration.schemas import MailUnifiedMessageSummary

from mailops.models import MailAccountIndex, MailConversationIndex, MailFolderIndexState, MailMessageIndex

from .threading import (
    compute_conversation_id,
    compute_dedupe_key,
    compute_thread_key,
    first_address,
    ids_header_value,
    infer_direction,
    message_is_seen,
    normalize_email,
    normalize_message_id,
    normalize_subject,
    same_folder,
    sent_reply_subject_thread_keys,
    summary_from_message_row,
    summary_thread_parent_values,
    uid_int,
)


logger = logging.getLogger("mailops.mail_indexing.sync")

DEFAULT_INDEX_FOLDERS = ("INBOX",)
RECENT_WINDOW_SIZE = 100


class FolderSyncResult:
    def __init__(self, folder, uidvalidity="", summaries=(), present_uids=()):
        self.folder = folder
        self.uidvalidity = str(uidvalidity or "")
        self.summaries = tuple(summaries or ())
        self.present_uids = {uid_int(uid) for uid in present_uids or () if uid_int(uid)}


def sync_account(user, credentials, imap_client_factory, limit=500, incremental=True):
    account = ensure_account(user, credentials.email)
    mark_account_syncing(account)
    try:
        with imap_client_factory() as client:
            client.login(credentials)
            sent_folder = client._resolve_sent_folder()
            folders = index_folders(sent_folder)
            imap_host = getattr(client, "host", settings.MAIL_IMAP_HOST)
            folder_results = []
            for folder in folders:
                state = MailFolderIndexState.objects.filter(account=account, folder=folder).first()
                if incremental and state and state.last_synced_at:
                    folder_results.append(fetch_incremental_folder(client, folder, state, limit=limit))
                else:
                    folder_results.append(fetch_initial_folder(client, folder, limit=limit))
        return index_folder_results(
            user=user,
            account_email=credentials.email,
            imap_host=imap_host,
            sent_folder=sent_folder or "",
            folder_results=folder_results,
        )
    except Exception as exc:
        mark_account_failed(account, exc)
        logger.exception("Mail index sync failed for %s", credentials.email)
        raise


def ensure_account(user, account_email, imap_host="", sent_folder=""):
    account, _ = MailAccountIndex.objects.get_or_create(
        user=user,
        account_email=normalize_email(account_email),
        defaults={
            "imap_host": (imap_host or "").strip(),
            "sent_folder": (sent_folder or "").strip(),
        },
    )
    update_fields = []
    normalized_host = (imap_host or "").strip()
    normalized_sent_folder = (sent_folder or "").strip()
    if normalized_host and account.imap_host != normalized_host:
        account.imap_host = normalized_host
        update_fields.append("imap_host")
    if normalized_sent_folder and account.sent_folder != normalized_sent_folder:
        account.sent_folder = normalized_sent_folder
        update_fields.append("sent_folder")
    if update_fields:
        update_fields.append("updated_at")
        account.save(update_fields=update_fields)
    return account


def mark_account_syncing(account):
    account.index_status = MailAccountIndex.STATUS_SYNCING
    account.last_sync_started_at = timezone.now()
    account.last_sync_error = ""
    account.save(update_fields=["index_status", "last_sync_started_at", "last_sync_error", "updated_at"])


def mark_account_failed(account, exc):
    account.index_status = MailAccountIndex.STATUS_FAILED if account.last_indexed_at is None else MailAccountIndex.STATUS_PARTIAL
    account.last_sync_finished_at = timezone.now()
    account.last_sync_error = str(exc)[:2000]
    account.save(update_fields=["index_status", "last_sync_finished_at", "last_sync_error", "updated_at"])


def index_folders(sent_folder):
    folders = list(DEFAULT_INDEX_FOLDERS)
    if sent_folder and not same_folder(sent_folder, "INBOX"):
        folders.append(sent_folder)
    return tuple(folders)


def fetch_initial_folder(client, folder, limit):
    uidvalidity = client.fetch_folder_uidvalidity(folder)
    summaries = tuple(client.fetch_recent_conversation_summaries(folder=folder, limit=limit))
    return FolderSyncResult(folder=folder, uidvalidity=uidvalidity, summaries=summaries)


def fetch_incremental_folder(client, folder, state, limit):
    uidvalidity = client.fetch_folder_uidvalidity(folder)
    if state.uidvalidity and uidvalidity and state.uidvalidity != uidvalidity:
        summaries = tuple(client.fetch_recent_conversation_summaries(folder=folder, limit=min(limit, RECENT_WINDOW_SIZE)))
        return FolderSyncResult(folder=folder, uidvalidity=uidvalidity, summaries=summaries)

    newer = tuple(client.fetch_conversation_summaries_since_uid(folder=folder, min_uid=state.highest_indexed_uid, limit=limit))
    recent = tuple(client.fetch_recent_conversation_summaries(folder=folder, limit=min(limit, RECENT_WINDOW_SIZE)))
    by_uid = {}
    for summary in (*recent, *newer):
        by_uid[uid_int(summary.uid)] = summary
    return FolderSyncResult(folder=folder, uidvalidity=uidvalidity, summaries=by_uid.values(), present_uids=(summary.uid for summary in recent))


@transaction.atomic
def index_folder_results(user, account_email, folder_results, imap_host="", sent_folder=""):
    account = ensure_account(user, account_email, imap_host=imap_host, sent_folder=sent_folder)
    touched_thread_keys = set()
    summaries = tuple(summary for result in folder_results for summary in result.summaries)
    message_ids = message_ids_for_threading(account, summaries)
    subject_thread_keys = sent_reply_subject_thread_keys(summaries, sent_folder or account.sent_folder)

    for summary in summaries:
        old_thread_key = (
            MailMessageIndex.objects.filter(account=account, folder=summary.folder, uid=uid_int(summary.uid))
            .values_list("thread_key", flat=True)
            .first()
        )
        thread_key = compute_thread_key(summary, message_ids, subject_thread_keys=subject_thread_keys)
        upsert_message(account, summary, thread_key, sent_folder=sent_folder or account.sent_folder)
        touched_thread_keys.add(thread_key)
        if old_thread_key and old_thread_key != thread_key:
            touched_thread_keys.add(old_thread_key)

    for result in folder_results:
        reconcile_recent_missing_messages(account, result, touched_thread_keys)
        highest_uid = max((uid_int(summary.uid) for summary in result.summaries), default=0)
        existing_highest_uid = MailFolderIndexState.objects.filter(account=account, folder=result.folder).values_list(
            "highest_indexed_uid", flat=True
        ).first() or 0
        MailFolderIndexState.objects.update_or_create(
            account=account,
            folder=result.folder,
            defaults={
                "uidvalidity": result.uidvalidity,
                "highest_indexed_uid": max(existing_highest_uid, highest_uid),
                "last_synced_at": timezone.now(),
            },
        )

    for thread_key in touched_thread_keys:
        rebuild_conversation(account, thread_key)

    account.last_indexed_at = timezone.now()
    account.last_sync_finished_at = account.last_indexed_at
    account.last_sync_error = ""
    account.index_status = MailAccountIndex.STATUS_READY if account.conversations.exists() else MailAccountIndex.STATUS_EMPTY
    if sent_folder and account.sent_folder != sent_folder:
        account.sent_folder = sent_folder
    account.save(
        update_fields=[
            "sent_folder",
            "last_indexed_at",
            "last_sync_finished_at",
            "last_sync_error",
            "index_status",
            "updated_at",
        ]
    )
    logger.info("Indexed %s: %s messages, %s conversations", account.account_email, account.messages.count(), account.conversations.count())
    return account


def message_ids_for_threading(account, summaries):
    message_ids = {}
    for row in account.messages.exclude(message_id="").order_by("sent_at", "uid"):
        summary = summary_from_message_row(row)
        if row.message_id and row.message_id not in message_ids:
            message_ids[row.message_id] = summary
    for summary in summaries:
        message_id = normalize_message_id(summary.message_id)
        if message_id and message_id not in message_ids:
            message_ids[message_id] = summary
    return message_ids


def upsert_message(account, summary, thread_key, sent_folder):
    sender_name, sender_email = first_address(summary.sender)
    normalized_message_id = normalize_message_id(summary.message_id)
    in_reply_to, references = summary_thread_parent_values(summary)
    defaults = {
        "direction": infer_direction(summary, account.account_email, sent_folder),
        "message_id": normalized_message_id,
        "in_reply_to": ids_header_value(in_reply_to),
        "references_raw": ids_header_value(references),
        "thread_key": thread_key,
        "normalized_subject": normalize_subject(summary.subject),
        "subject": summary.subject or "",
        "sender_name": sender_name,
        "sender_email": sender_email,
        "sender_raw": summary.sender or "",
        "to_json": list(summary.to),
        "cc_json": list(summary.cc),
        "sent_at": summary.date,
        "flags_json": list(summary.flags),
        "is_read": message_is_seen(summary),
        "size": int(summary.size or 0),
        "has_attachments": bool(summary.has_attachments),
        "has_visible_attachments": bool(summary.has_visible_attachments),
        "dedupe_key": compute_dedupe_key(summary),
        "raw_headers_json": {
            "message_id": summary.message_id or "",
            "in_reply_to": list(in_reply_to),
            "references": list(references),
        },
    }
    MailMessageIndex.objects.update_or_create(
        account=account,
        folder=summary.folder,
        uid=uid_int(summary.uid),
        defaults=defaults,
    )


def reconcile_recent_missing_messages(account, folder_result, touched_thread_keys):
    if not getattr(settings, "MAIL_INDEX_RECONCILE_DELETIONS", False):
        return
    if not folder_result.present_uids:
        return
    indexed_recent = account.messages.filter(folder=folder_result.folder, uid__lte=max(folder_result.present_uids), uid__gte=min(folder_result.present_uids))
    missing = indexed_recent.exclude(uid__in=folder_result.present_uids)
    for thread_key in missing.values_list("thread_key", flat=True).distinct():
        touched_thread_keys.add(thread_key)
    missing.delete()


def rebuild_conversation(account, thread_key):
    rows = list(account.messages.filter(thread_key=thread_key).order_by("sent_at", "uid", "folder"))
    if not rows:
        account.conversations.filter(thread_key=thread_key).delete()
        return
    items = tuple(MailUnifiedMessageSummary(summary=summary_from_message_row(row), direction=row.direction) for row in rows)
    deduped_items = _dedupe_unified_items(items, account.account_email, account.sent_folder)
    ordered_items = tuple(sorted(deduped_items, key=_unified_item_sort_key))
    latest_item = max(ordered_items, key=lambda item: _message_activity_key(item.summary))
    conversation_id = compute_conversation_id(account.account_email, thread_key)
    participants = _conversation_participants(tuple(item.summary for item in ordered_items))
    conversation, _ = MailConversationIndex.objects.update_or_create(
        account=account,
        conversation_id=conversation_id,
        defaults={
            "thread_key": thread_key,
            "normalized_subject": first_nonblank(item.summary.subject for item in ordered_items),
            "latest_message_at": latest_item.summary.date,
            "message_count": len(ordered_items),
            "has_unread": any(item.direction == MailMessageIndex.DIRECTION_INBOUND and not message_is_seen(item.summary) for item in ordered_items),
            "has_attachments": any(item.summary.has_attachments for item in ordered_items),
            "has_visible_attachments": any(item.summary.has_visible_attachments for item in ordered_items),
            "participants_json": [{"name": participant.name, "email": participant.email} for participant in participants],
            "folders_json": ordered_strings(item.summary.folder for item in ordered_items),
        },
    )
    account.messages.filter(thread_key=thread_key).update(conversation=conversation)


def ordered_strings(values):
    ordered = []
    seen = set()
    for value in values:
        normalized = str(value or "").strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        ordered.append(normalized)
    return ordered


def first_nonblank(values):
    for value in values:
        normalized = normalize_subject(value)
        if normalized:
            return normalized
    return ""
