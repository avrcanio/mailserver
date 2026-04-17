from django.db.models import F

from mail_integration.imap_client import _dedupe_unified_items, _unified_item_sort_key
from mail_integration.schemas import (
    MailConversationParticipant,
    MailUnifiedConversationSummary,
    MailUnifiedConversationSummaryPage,
    MailUnifiedMessageSummary,
)

from .selectors import get_account_index, is_index_usable
from .sync import ordered_strings
from .threading import summary_from_message_row


def get_unified_conversation_page_from_index(user, account_email, limit=50):
    account = get_account_index(user, account_email)
    if not is_index_usable(account):
        return None
    conversations = list(
        account.conversations.order_by(F("latest_message_at").desc(nulls_last=True), "-id")
        .prefetch_related("messages")
        .select_related("account")[:limit]
    )
    if not conversations:
        return None
    folders = ordered_strings(folder for conversation in conversations for folder in (conversation.folders_json or []))
    return MailUnifiedConversationSummaryPage(
        folders=tuple(folders),
        conversations=tuple(conversation_from_index(conversation) for conversation in conversations),
    )


def conversation_from_index(conversation):
    raw_items = tuple(
        MailUnifiedMessageSummary(summary=summary_from_message_row(row), direction=row.direction)
        for row in conversation.messages.all()
    )
    deduped_items = _dedupe_unified_items(raw_items, conversation.account.account_email, conversation.account.sent_folder)
    items = tuple(sorted(deduped_items, key=_unified_item_sort_key))
    participants = tuple(
        MailConversationParticipant(name=str(participant.get("name") or ""), email=str(participant.get("email") or ""))
        for participant in conversation.participants_json or []
    )
    return MailUnifiedConversationSummary(
        conversation_id=conversation.conversation_id,
        message_count=conversation.message_count,
        reply_count=max(0, conversation.message_count - 1),
        has_unread=conversation.has_unread,
        has_attachments=conversation.has_attachments,
        has_visible_attachments=conversation.has_visible_attachments,
        participants=participants,
        messages=items,
        latest_date=conversation.latest_message_at,
    )
