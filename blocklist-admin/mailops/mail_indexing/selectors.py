from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from mailops.models import MailAccountIndex

from .threading import normalize_email, same_folder


def get_account_index(user, account_email):
    return MailAccountIndex.objects.filter(user=user, account_email=normalize_email(account_email)).first()


def is_index_usable(account):
    if account is None:
        return False
    if account.index_status not in {MailAccountIndex.STATUS_READY, MailAccountIndex.STATUS_PARTIAL}:
        return False
    if account.last_indexed_at is None:
        return False
    if not account.conversations.exists():
        return False
    max_age_seconds = int(getattr(settings, "MAIL_INDEX_MAX_AGE_SECONDS", 0) or 0)
    if max_age_seconds > 0 and timezone.now() - account.last_indexed_at > timedelta(seconds=max_age_seconds):
        return False
    folder_names = {state.folder.lower() for state in account.folder_states.all()}
    if "inbox" not in folder_names:
        return False
    if account.sent_folder and not any(same_folder(account.sent_folder, folder) for folder in folder_names):
        return False
    return True
