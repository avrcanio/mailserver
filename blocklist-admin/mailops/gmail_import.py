from dataclasses import dataclass

from django.db.models import Q
from django.utils import timezone

from mail_integration.gmail_client import GmailClient, GmailMessageRef
from mail_integration.imap_client import ImapClient
from mail_integration.schemas import MailboxCredentials
from mail_integration.exceptions import MailConnectionError, MailProtocolError

from .models import GmailImportAccount, GmailImportMessage, GmailImportRun, MailAccountIndex, MailboxTokenCredential


GMAIL_HISTORICAL_QUERY = "in:anywhere -in:drafts -in:spam -in:trash"
GMAIL_SENT_LABEL = "SENT"
DEFAULT_TARGET_FOLDER = "INBOX"


@dataclass(frozen=True)
class GmailImportResult:
    run: GmailImportRun | None
    scanned: int
    appended: int
    committed: int
    cleaned: int
    skipped: int
    failed: int
    history_id: str = ""


@dataclass(frozen=True)
class GmailImportCycleResult:
    scanned: int
    selected: int
    synced: int
    failed: int
    skipped: int


class GmailImportError(Exception):
    pass


class GmailHistoryUnavailableError(GmailImportError):
    pass


class GmailImportService:
    def __init__(self, gmail_client_factory=None, imap_client_factory=ImapClient):
        self.gmail_client_factory = gmail_client_factory or (lambda refresh_token: GmailClient(refresh_token=refresh_token))
        self.imap_client_factory = imap_client_factory

    def run_historical_import(self, gmail_email, target_mailbox_email, limit=100, since="", dry_run=False, no_delete=False):
        gmail_email = _normalize_email(gmail_email)
        target_mailbox_email = _normalize_email(target_mailbox_email)
        limit = int(limit)
        if limit < 1:
            raise GmailImportError("--limit must be greater than zero")

        import_account = self._get_import_account(gmail_email, target_mailbox_email)
        return self._run_historical_import_for_account(
            import_account=import_account,
            target_mailbox_email=target_mailbox_email,
            limit=limit,
            since=since,
            dry_run=dry_run,
            no_delete=no_delete,
        )

    def run_historical_import_for_user(self, user, limit=100, since="", dry_run=False, no_delete=False):
        limit = int(limit)
        if limit < 1:
            raise GmailImportError("--limit must be greater than zero")
        import_account = self._get_user_import_account(user)
        return self._run_historical_import_for_account(
            import_account=import_account,
            target_mailbox_email=import_account.target_mailbox_email,
            limit=limit,
            since=since,
            dry_run=dry_run,
            no_delete=no_delete,
        )

    def _run_historical_import_for_account(self, import_account, target_mailbox_email, limit, since, dry_run, no_delete):
        run = None if dry_run else GmailImportRun.objects.create(import_account=import_account, mode=GmailImportRun.MODE_HISTORICAL)

        try:
            result = self._run_historical_batch(
                import_account=import_account,
                target_mailbox_email=target_mailbox_email,
                run=run,
                limit=limit,
                since=since,
                dry_run=dry_run,
                no_delete=no_delete,
            )
        except Exception as exc:
            if run is not None:
                run.status = GmailImportRun.STATUS_FAILED
                run.error = str(exc)[:2000]
                run.finished_at = timezone.now()
                run.save(update_fields=["status", "error", "finished_at"])
            import_account.consecutive_failures += 1
            import_account.last_error = str(exc)[:2000]
            import_account.save(update_fields=["consecutive_failures", "last_error", "updated_at"])
            raise

        if run is not None:
            run.status = GmailImportRun.STATUS_SUCCESS if result.failed == 0 else GmailImportRun.STATUS_PARTIAL
            run.scanned_count = result.scanned
            run.appended_count = result.appended
            run.committed_count = result.committed
            run.cleaned_count = result.cleaned
            run.skipped_count = result.skipped
            run.failed_count = result.failed
            run.finished_at = timezone.now()
            run.save(
                update_fields=[
                    "status",
                    "scanned_count",
                    "appended_count",
                    "committed_count",
                    "cleaned_count",
                    "skipped_count",
                    "failed_count",
                    "finished_at",
                ]
            )

        if dry_run:
            return result

        if result.failed == 0:
            import_account.consecutive_failures = 0
            import_account.last_error = ""
            import_account.last_success_at = run.finished_at if run is not None else timezone.now()
            update_fields = ["consecutive_failures", "last_error", "last_success_at", "updated_at"]
            if not import_account.historical_import_completed_at:
                import_account.historical_import_completed_at = import_account.last_success_at
                update_fields.append("historical_import_completed_at")
            if result.history_id:
                import_account.last_history_id = result.history_id
                update_fields.append("last_history_id")
            import_account.save(update_fields=update_fields)
        elif result.failed:
            import_account.last_error = f"{result.failed} Gmail import message(s) failed"
            import_account.save(update_fields=["last_error", "updated_at"])

        return result

    def run_incremental_import(self, gmail_email, target_mailbox_email, limit=100, no_delete=False):
        gmail_email = _normalize_email(gmail_email)
        target_mailbox_email = _normalize_email(target_mailbox_email)
        limit = int(limit)
        if limit < 1:
            raise GmailImportError("--limit must be greater than zero")

        import_account = self._get_import_account(gmail_email, target_mailbox_email)
        return self._run_incremental_import_for_account(
            import_account=import_account,
            target_mailbox_email=target_mailbox_email,
            limit=limit,
            no_delete=no_delete,
        )

    def run_incremental_import_for_user(self, user, limit=100, no_delete=False):
        limit = int(limit)
        if limit < 1:
            raise GmailImportError("--limit must be greater than zero")
        import_account = self._get_user_import_account(user)
        return self._run_incremental_import_for_account(
            import_account=import_account,
            target_mailbox_email=import_account.target_mailbox_email,
            limit=limit,
            no_delete=no_delete,
        )

    def _run_incremental_import_for_account(self, import_account, target_mailbox_email, limit, no_delete):
        run = GmailImportRun.objects.create(import_account=import_account, mode=GmailImportRun.MODE_INCREMENTAL)
        try:
            result = self._run_incremental_batch(
                import_account=import_account,
                target_mailbox_email=target_mailbox_email,
                run=run,
                limit=limit,
                no_delete=no_delete,
            )
        except Exception as exc:
            run.status = GmailImportRun.STATUS_FAILED
            run.error = str(exc)[:2000]
            run.finished_at = timezone.now()
            run.save(update_fields=["status", "error", "finished_at"])
            import_account.consecutive_failures += 1
            import_account.last_error = str(exc)[:2000]
            import_account.save(update_fields=["consecutive_failures", "last_error", "updated_at"])
            raise

        run.status = GmailImportRun.STATUS_SUCCESS if result.failed == 0 else GmailImportRun.STATUS_PARTIAL
        run.scanned_count = result.scanned
        run.appended_count = result.appended
        run.committed_count = result.committed
        run.cleaned_count = result.cleaned
        run.skipped_count = result.skipped
        run.failed_count = result.failed
        run.finished_at = timezone.now()
        run.save(
            update_fields=[
                "status",
                "scanned_count",
                "appended_count",
                "committed_count",
                "cleaned_count",
                "skipped_count",
                "failed_count",
                "finished_at",
            ]
        )

        if result.failed == 0:
            import_account.consecutive_failures = 0
            import_account.last_error = ""
            import_account.last_success_at = run.finished_at
            update_fields = ["consecutive_failures", "last_error", "last_success_at", "updated_at"]
            if result.history_id:
                import_account.last_history_id = result.history_id
                update_fields.append("last_history_id")
            import_account.save(update_fields=update_fields)
        elif result.failed:
            import_account.last_error = f"{result.failed} Gmail import message(s) failed"
            import_account.save(update_fields=["last_error", "updated_at"])

        return result

    def run_incremental_cycle(self, limit=100, max_accounts=20, no_delete=False):
        limit = int(limit)
        max_accounts = int(max_accounts)
        if limit < 1:
            raise GmailImportError("--limit must be greater than zero")
        if max_accounts < 1:
            raise GmailImportError("--max-accounts must be greater than zero")

        accounts = list(
            GmailImportAccount.objects.filter(historical_import_completed_at__isnull=False)
            .filter(Q(consecutive_failures=0) | Q(last_success_at__isnull=False))
            .order_by("last_success_at", "gmail_email")[:max_accounts]
        )
        synced = failed = skipped = 0
        for account in accounts:
            try:
                self._run_incremental_import_for_account(
                    import_account=account,
                    target_mailbox_email=account.target_mailbox_email,
                    limit=limit,
                    no_delete=no_delete,
                )
            except Exception:
                failed += 1
            else:
                synced += 1
        return GmailImportCycleResult(scanned=GmailImportAccount.objects.count(), selected=len(accounts), synced=synced, failed=failed, skipped=skipped)

    def _run_historical_batch(self, import_account, target_mailbox_email, run, limit, since, dry_run, no_delete):
        gmail_client = self.gmail_client_factory(import_account.get_refresh_token())
        refs = _bounded_refs(gmail_client, query=_historical_query(since), limit=limit)
        scanned = len(refs)
        if dry_run:
            return self._dry_run_result(run, scanned=scanned)

        return self._import_refs(
            import_account=import_account,
            target_mailbox_email=target_mailbox_email,
            run=run,
            refs=refs,
            no_delete=no_delete,
            gmail_client=gmail_client,
        )

    def _run_incremental_batch(self, import_account, target_mailbox_email, run, limit, no_delete):
        gmail_client = self.gmail_client_factory(import_account.get_refresh_token())
        history_id = ""
        try:
            refs, history_id = _incremental_refs(gmail_client, import_account.last_history_id, limit=limit)
        except GmailHistoryUnavailableError:
            refs = _bounded_refs(gmail_client, query=GMAIL_HISTORICAL_QUERY, limit=limit)
        result = self._import_refs(
            import_account=import_account,
            target_mailbox_email=target_mailbox_email,
            run=run,
            refs=refs,
            no_delete=no_delete,
            gmail_client=gmail_client,
        )
        return GmailImportResult(
            run=run,
            scanned=result.scanned,
            appended=result.appended,
            committed=result.committed,
            cleaned=result.cleaned,
            skipped=result.skipped,
            failed=result.failed,
            history_id=history_id or result.history_id,
        )

    def _import_refs(self, import_account, target_mailbox_email, run, refs, no_delete, gmail_client=None):
        gmail_client = gmail_client or self.gmail_client_factory(import_account.get_refresh_token())
        scanned = len(refs)
        target_credentials = self._target_credentials(target_mailbox_email, owner=import_account.user)
        appended = committed = cleaned = skipped = failed = 0
        any_committed = False
        max_history_id = ""
        cleanup_enabled = bool(import_account.delete_after_import and not no_delete)

        with self.imap_client_factory() as imap_client:
            imap_client.login(target_credentials)
            sent_folder = imap_client._resolve_sent_folder()
            for ref in refs:
                message_record = self._get_or_create_message_record(import_account, ref)
                if message_record.state == GmailImportMessage.STATE_CLEANED:
                    skipped += 1
                    continue
                if message_record.state == GmailImportMessage.STATE_COMMITTED:
                    skipped += 1
                    if cleanup_enabled and message_record.cleanup_status != GmailImportMessage.STATUS_SUCCESS:
                        if self._try_clean_gmail_source(gmail_client, message_record):
                            cleaned += 1
                        else:
                            failed += 1
                    continue
                if message_record.state == GmailImportMessage.STATE_APPENDED and message_record.append_status == GmailImportMessage.STATUS_SUCCESS:
                    self._mark_committed(message_record)
                    committed += 1
                    any_committed = True
                    if cleanup_enabled:
                        if self._try_clean_gmail_source(gmail_client, message_record):
                            cleaned += 1
                        else:
                            failed += 1
                    continue

                try:
                    raw_message = gmail_client.fetch_raw_message(ref.gmail_message_id)
                    max_history_id = _max_history_id(max_history_id, raw_message.history_id)
                    target_folder = _target_folder(raw_message.label_ids, sent_folder)
                    self._mark_fetched(message_record, raw_message)
                    imap_client.append_message(target_folder, raw_message.raw_bytes)
                    self._mark_appended(message_record, target_folder)
                    appended += 1
                    self._mark_committed(message_record)
                    committed += 1
                    any_committed = True
                    if cleanup_enabled:
                        if self._try_clean_gmail_source(gmail_client, message_record):
                            cleaned += 1
                        else:
                            failed += 1
                except Exception as exc:
                    failed += 1
                    self._mark_failed(message_record, exc)

        if any_committed:
            self._mark_index_stale(target_mailbox_email)

        return GmailImportResult(
            run=run,
            scanned=scanned,
            appended=appended,
            committed=committed,
            cleaned=cleaned,
            skipped=skipped,
            failed=failed,
            history_id=max_history_id,
        )

    def _dry_run_result(self, run, scanned):
        return GmailImportResult(run=run, scanned=scanned, appended=0, committed=0, cleaned=0, skipped=0, failed=0)

    def _get_import_account(self, gmail_email, target_mailbox_email):
        try:
            account = GmailImportAccount.objects.get(gmail_email=gmail_email)
        except GmailImportAccount.DoesNotExist as exc:
            raise GmailImportError(f"No Gmail import account configured for {gmail_email}. Run bootstrap_gmail_import_oauth first.") from exc
        if account.target_mailbox_email != target_mailbox_email:
            raise GmailImportError(f"Gmail import account {gmail_email} is mapped to {account.target_mailbox_email}, not {target_mailbox_email}.")
        return account

    def _get_user_import_account(self, user):
        user_email = _normalize_email(getattr(user, "email", ""))
        if not getattr(user, "is_authenticated", False):
            raise GmailImportError("Authenticated Django user is required for user-scoped Gmail import.")
        if not user_email:
            raise GmailImportError("Django user must have an email before Gmail import.")
        try:
            account = GmailImportAccount.objects.get(user=user)
        except GmailImportAccount.DoesNotExist as exc:
            raise GmailImportError(f"No Gmail import account connected for {user_email}.") from exc
        if account.gmail_email != user_email or account.target_mailbox_email != user_email:
            raise GmailImportError(f"Gmail import account for {user_email} is not mapped to the owning user mailbox.")
        return account

    def _target_credentials(self, target_mailbox_email, owner=None):
        try:
            credential = MailboxTokenCredential.objects.select_related("token__user").get(mailbox_email=target_mailbox_email)
        except MailboxTokenCredential.DoesNotExist as exc:
            raise GmailImportError(f"No mailbox token credential found for target mailbox {target_mailbox_email}") from exc
        if owner is not None and credential.token.user_id != owner.pk:
            raise GmailImportError(f"Target mailbox {target_mailbox_email} is not owned by the Gmail import account user.")
        return MailboxCredentials(email=credential.mailbox_email, password=credential.get_mailbox_password())

    def _get_or_create_message_record(self, import_account, ref):
        record, _ = GmailImportMessage.objects.get_or_create(
            import_account=import_account,
            gmail_message_id=ref.gmail_message_id,
            defaults={
                "gmail_thread_id": ref.gmail_thread_id,
                "state": GmailImportMessage.STATE_FETCHED,
                "fetched_at": timezone.now(),
            },
        )
        return record

    def _mark_fetched(self, record, raw_message):
        record.gmail_thread_id = raw_message.gmail_thread_id or record.gmail_thread_id
        record.rfc_message_id = raw_message.rfc_message_id
        record.state = GmailImportMessage.STATE_FETCHED
        record.fetched_at = timezone.now()
        record.error = ""
        record.save(update_fields=["gmail_thread_id", "rfc_message_id", "state", "fetched_at", "error", "updated_at"])

    def _mark_appended(self, record, target_folder):
        record.target_folder = target_folder
        record.state = GmailImportMessage.STATE_APPENDED
        record.append_status = GmailImportMessage.STATUS_SUCCESS
        record.appended_at = timezone.now()
        record.error = ""
        record.save(update_fields=["target_folder", "state", "append_status", "appended_at", "error", "updated_at"])

    def _mark_committed(self, record):
        record.state = GmailImportMessage.STATE_COMMITTED
        record.committed_at = record.committed_at or timezone.now()
        record.error = ""
        record.save(update_fields=["state", "committed_at", "error", "updated_at"])

    def _clean_gmail_source(self, gmail_client, record):
        gmail_client.delete_message(record.gmail_message_id)
        record.state = GmailImportMessage.STATE_CLEANED
        record.cleanup_status = GmailImportMessage.STATUS_SUCCESS
        record.cleaned_at = timezone.now()
        record.error = ""
        record.save(update_fields=["state", "cleanup_status", "cleaned_at", "error", "updated_at"])
        return True

    def _try_clean_gmail_source(self, gmail_client, record):
        try:
            return self._clean_gmail_source(gmail_client, record)
        except Exception as exc:
            self._mark_cleanup_failed(record, exc)
            return False

    def _mark_failed(self, record, exc):
        record.state = GmailImportMessage.STATE_FAILED
        record.error = str(exc)[:2000]
        if record.append_status != GmailImportMessage.STATUS_SUCCESS:
            record.append_status = GmailImportMessage.STATUS_FAILED
        record.save(update_fields=["state", "append_status", "error", "updated_at"])

    def _mark_cleanup_failed(self, record, exc):
        record.state = GmailImportMessage.STATE_COMMITTED
        record.cleanup_status = GmailImportMessage.STATUS_FAILED
        record.error = str(exc)[:2000]
        record.save(update_fields=["state", "cleanup_status", "error", "updated_at"])

    def _mark_index_stale(self, target_mailbox_email):
        MailAccountIndex.objects.filter(account_email=target_mailbox_email).update(last_indexed_at=None)


def _bounded_refs(gmail_client, query, limit):
    refs = []
    page_token = ""
    while len(refs) < limit:
        page_limit = min(100, limit - len(refs))
        page_refs, page_token = gmail_client.list_message_refs(query=query, max_results=page_limit, page_token=page_token)
        refs.extend(page_refs)
        if not page_token or not page_refs:
            break
    return tuple(refs[:limit])


def _incremental_refs(gmail_client, start_history_id, limit):
    start_history_id = str(start_history_id or "").strip()
    if not start_history_id:
        raise GmailHistoryUnavailableError("Gmail history cursor is missing")
    refs = []
    page_token = ""
    latest_history_id = ""
    try:
        while len(refs) < limit:
            page = gmail_client.list_history_page(start_history_id=start_history_id, page_token=page_token)
            latest_history_id = _max_history_id(latest_history_id, page.history_id)
            for message in page.messages_added:
                refs.append(
                    GmailMessageRef(
                        gmail_message_id=message.gmail_message_id,
                        gmail_thread_id=message.gmail_thread_id,
                        history_id=message.history_id,
                        label_ids=message.label_ids,
                    )
                )
                latest_history_id = _max_history_id(latest_history_id, message.history_id)
                if len(refs) >= limit:
                    break
            page_token = page.next_page_token
            if not page_token:
                break
    except (MailConnectionError, MailProtocolError) as exc:
        raise GmailHistoryUnavailableError(str(exc)) from exc
    return tuple(refs[:limit]), latest_history_id


def _historical_query(since):
    query = GMAIL_HISTORICAL_QUERY
    since = str(since or "").strip()
    if since:
        query = f"{query} after:{since}"
    return query


def _target_folder(label_ids, sent_folder):
    normalized_labels = {str(label).upper() for label in label_ids or ()}
    if GMAIL_SENT_LABEL in normalized_labels and sent_folder:
        return sent_folder
    return DEFAULT_TARGET_FOLDER


def _normalize_email(value):
    return str(value or "").strip().lower()


def _max_history_id(current, candidate):
    current = str(current or "").strip()
    candidate = str(candidate or "").strip()
    if not candidate:
        return current
    if not current:
        return candidate
    try:
        return str(max(int(current), int(candidate)))
    except ValueError:
        return candidate
