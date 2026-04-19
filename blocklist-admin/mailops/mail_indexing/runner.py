import logging
import time
from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Case, IntegerField, Q, Value, When
from django.utils import timezone

from mail_integration.schemas import MailboxCredentials
from mailops.models import MailAccountIndex, MailboxTokenCredential

from .service import MailIndexService
from .sync import ensure_account
from .threading import normalize_email


logger = logging.getLogger("mailops.mail_indexing.runner")


@dataclass(frozen=True)
class MailIndexSyncCycleResult:
    scanned: int = 0
    selected: int = 0
    synced: int = 0
    failed: int = 0
    skipped: int = 0
    elapsed_seconds: float = 0


def select_accounts_for_sync(
    account_email="",
    max_accounts=50,
    stale_after_seconds=600,
    failure_cooldown_seconds=1800,
    now=None,
):
    now = now or timezone.now()
    stale_sync_before = now - timedelta(seconds=max(1, int(stale_after_seconds)))
    failure_retry_before = now - timedelta(seconds=max(1, int(failure_cooldown_seconds)))

    queryset = MailAccountIndex.objects.select_related("user").filter(
        index_status__in=[
            MailAccountIndex.STATUS_EMPTY,
            MailAccountIndex.STATUS_READY,
            MailAccountIndex.STATUS_PARTIAL,
            MailAccountIndex.STATUS_FAILED,
            MailAccountIndex.STATUS_SYNCING,
        ]
    )
    normalized_account = normalize_email(account_email)
    if normalized_account:
        queryset = queryset.filter(account_email=normalized_account)

    queryset = queryset.filter(
        Q(index_status=MailAccountIndex.STATUS_EMPTY)
        | Q(index_status=MailAccountIndex.STATUS_READY, last_indexed_at__isnull=True)
        | Q(index_status=MailAccountIndex.STATUS_READY, last_indexed_at__lte=stale_sync_before)
        | Q(index_status=MailAccountIndex.STATUS_PARTIAL, last_sync_finished_at__lte=failure_retry_before)
        | Q(index_status=MailAccountIndex.STATUS_FAILED, last_sync_finished_at__lte=failure_retry_before)
        | Q(index_status=MailAccountIndex.STATUS_SYNCING, last_sync_started_at__lte=stale_sync_before)
    )
    queryset = queryset.annotate(
        never_indexed_order=Case(
            When(last_indexed_at__isnull=True, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
    ).order_by("never_indexed_order", "last_indexed_at", "account_email")
    return list(queryset[: max(1, int(max_accounts))])


def run_sync_cycle(
    account_email="",
    limit=500,
    max_accounts=50,
    stale_after_seconds=600,
    failure_cooldown_seconds=1800,
    mail_index_service=None,
):
    started_at = time.monotonic()
    mail_index_service = mail_index_service or MailIndexService()
    seed_account_indexes_for_credentials(account_email=account_email)
    selected_accounts = select_accounts_for_sync(
        account_email=account_email,
        max_accounts=max_accounts,
        stale_after_seconds=stale_after_seconds,
        failure_cooldown_seconds=failure_cooldown_seconds,
    )
    result = {
        "scanned": MailAccountIndex.objects.count(),
        "selected": len(selected_accounts),
        "synced": 0,
        "failed": 0,
        "skipped": 0,
    }

    for account in selected_accounts:
        credential = _credential_for_account(account)
        if credential is None:
            result["skipped"] += 1
            logger.warning("Skipping mail index sync for %s: mailbox credential missing", account.account_email)
            continue
        try:
            credentials = MailboxCredentials(email=credential.mailbox_email, password=credential.get_mailbox_password())
            mail_index_service.sync_account(account.user, credentials, limit=limit, incremental=True)
        except Exception:
            result["failed"] += 1
            logger.exception("Mail index sync cycle failed for %s", account.account_email)
        else:
            result["synced"] += 1

    elapsed_seconds = time.monotonic() - started_at
    cycle_result = MailIndexSyncCycleResult(elapsed_seconds=elapsed_seconds, **result)
    logger.info(
        "Mail index sync cycle complete: scanned=%s selected=%s synced=%s failed=%s skipped=%s elapsed=%.2fs",
        cycle_result.scanned,
        cycle_result.selected,
        cycle_result.synced,
        cycle_result.failed,
        cycle_result.skipped,
        cycle_result.elapsed_seconds,
    )
    return cycle_result


def seed_account_indexes_for_credentials(account_email=""):
    credentials = MailboxTokenCredential.objects.select_related("token__user").order_by("mailbox_email")
    normalized_account = normalize_email(account_email)
    if normalized_account:
        credentials = credentials.filter(mailbox_email=normalized_account)
    for credential in credentials:
        ensure_account(credential.token.user, credential.mailbox_email)


def _credential_for_account(account):
    return (
        MailboxTokenCredential.objects.select_related("token__user")
        .filter(token__user=account.user, mailbox_email=account.account_email)
        .first()
    )
