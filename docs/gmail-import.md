# Gmail Import

This document describes the operational path for importing one Gmail account into
one existing mailserver mailbox through `mailadmin`.

The importer is intentionally conservative:

- Gmail access uses OAuth and the Gmail API.
- The target side writes raw RFC822 messages through the mailserver IMAP service.
- Import success means target IMAP append plus durable import-record commit.
- Mail index/search visibility is downstream and does not gate Gmail cleanup.
- Gmail cleanup is disabled by default.
- When cleanup is enabled, Gmail deletion happens only after committed import
  state.

## Scope

v1 supports one Gmail source account mapped to one target mailserver mailbox per
`GmailImportAccount` row.

Imported source:

- Gmail All Mail through `in:anywhere`
- excludes Drafts, Spam, and Trash
- ignores Gmail labels as folder mapping

Target mapping:

- Gmail `SENT` messages append to the target Sent folder when Dovecot exposes one
- all other imported messages append to target `INBOX`

Out of scope for v1:

- mirroring Gmail labels into IMAP folders
- Gmail archive cleanup mode
- importing Gmail Drafts, Spam, or Trash
- mobile API changes

## Prerequisites

The target mailbox must already exist on this mailserver and must have stored
mailbox credentials in `mailadmin`.

Create or confirm the mailbox:

```bash
./scripts/mail.sh email add user@finestar.hr 'StrongPasswordHere'
./scripts/mail.sh debug login user@finestar.hr 'StrongPasswordHere'
```

Store target mailbox credentials by logging in once through the existing mailbox
API, or confirm they already exist:

```bash
docker compose exec -T mailadmin python manage.py shell
```

```python
from mailops.models import MailboxTokenCredential

MailboxTokenCredential.objects.filter(mailbox_email="user@finestar.hr").exists()
```

## Google OAuth Setup

Create an OAuth client in Google Cloud for the Gmail source account. The importer
uses the Gmail modify scope:

```text
https://www.googleapis.com/auth/gmail.modify
```

Set these values in the local `.env`:

```bash
GMAIL_IMPORT_GOOGLE_CLIENT_ID=google-client-id
GMAIL_IMPORT_GOOGLE_CLIENT_SECRET=google-client-secret
GMAIL_IMPORT_OAUTH_REDIRECT_URI=urn:ietf:wg:oauth:2.0:oob
GMAIL_IMPORT_OAUTH_SCOPES=https://www.googleapis.com/auth/gmail.modify
GMAIL_IMPORT_SYNC_INTERVAL_SECONDS=600
GMAIL_IMPORT_SYNC_LIMIT=100
GMAIL_IMPORT_SYNC_MAX_ACCOUNTS=20
```

Rebuild and restart `mailadmin` after adding dependencies or changing env:

```bash
docker compose up -d --build mailadmin
```

When enabling the periodic sync service later:

```bash
docker compose up -d --build gmail-import-sync
```

## Bootstrap OAuth

Print the Google consent URL:

```bash
docker compose exec -T mailadmin python manage.py bootstrap_gmail_import_oauth \
  --gmail source@gmail.com \
  --target user@finestar.hr
```

Open the printed URL, authorize access, then store the returned authorization
code:

```bash
docker compose exec -T mailadmin python manage.py bootstrap_gmail_import_oauth \
  --gmail source@gmail.com \
  --target user@finestar.hr \
  --code 'GOOGLE_AUTHORIZATION_CODE'
```

Confirm the import account exists without exposing the encrypted token:

```bash
docker compose exec -T mailadmin python manage.py shell
```

```python
from mailops.models import GmailImportAccount

account = GmailImportAccount.objects.get(gmail_email="source@gmail.com")
print(account.gmail_email, account.target_mailbox_email, account.delete_after_import)
```

`delete_after_import` defaults to `False`.

## Historical Import

Start with a dry run. Dry run fetches a bounded Gmail message list only; it does
not append to IMAP, write import records, or clean Gmail.

```bash
docker compose exec -T mailadmin python manage.py run_gmail_import \
  --account source@gmail.com \
  --target user@finestar.hr \
  --limit 50 \
  --dry-run \
  --no-delete
```

Run the first bounded import without Gmail cleanup:

```bash
docker compose exec -T mailadmin python manage.py run_gmail_import \
  --account source@gmail.com \
  --target user@finestar.hr \
  --limit 100 \
  --no-delete
```

Optional date-bounded import:

```bash
docker compose exec -T mailadmin python manage.py run_gmail_import \
  --account source@gmail.com \
  --target user@finestar.hr \
  --limit 100 \
  --since 2026/04/01 \
  --no-delete
```

Inspect import state:

```bash
docker compose exec -T mailadmin python manage.py shell
```

```python
from mailops.models import GmailImportAccount, GmailImportMessage, GmailImportRun

account = GmailImportAccount.objects.get(gmail_email="source@gmail.com")
print(account.last_success_at, account.last_history_id, account.last_error)
print(GmailImportRun.objects.filter(import_account=account).latest("started_at").status)
print(GmailImportMessage.objects.filter(import_account=account).values("state").order_by("state"))
```

When the import looks correct, enable Gmail deletion explicitly:

```bash
docker compose exec -T mailadmin python manage.py shell
```

```python
from mailops.models import GmailImportAccount

account = GmailImportAccount.objects.get(gmail_email="source@gmail.com")
account.delete_after_import = True
account.save(update_fields=["delete_after_import", "updated_at"])
```

Run another bounded import. Gmail source messages are deleted only after the
message reaches committed import state:

```bash
docker compose exec -T mailadmin python manage.py run_gmail_import \
  --account source@gmail.com \
  --target user@finestar.hr \
  --limit 100
```

Use `--no-delete` any time you want to override cleanup for one run.

## Incremental Sync

After a successful historical import, the importer stores
`historical_import_completed_at` and a Gmail `last_history_id` when available.

Run one incremental batch for a single account:

```bash
docker compose exec -T mailadmin python manage.py run_gmail_import \
  --account source@gmail.com \
  --target user@finestar.hr \
  --incremental \
  --limit 100 \
  --no-delete
```

Run one incremental cycle for all configured accounts that completed historical
import:

```bash
docker compose exec -T mailadmin python manage.py run_gmail_import \
  --incremental \
  --all \
  --limit 100 \
  --max-accounts 20 \
  --no-delete
```

Start the periodic Docker service:

```bash
docker compose up -d gmail-import-sync
docker compose logs --tail=100 gmail-import-sync
```

The incremental path uses Gmail History API when `last_history_id` exists. If the
history cursor is missing, expired, or unavailable, the importer falls back to a
bounded recent rescan and relies on the Gmail message ID unique constraint to
avoid duplicate appends.

The cursor advances only after a failure-free batch. Partial failures keep the
previous cursor so the next run can retry safely.

## Index Refresh

The importer appends raw messages into IMAP. The mail index is a downstream
metadata cache and may not show imported messages until the existing index
refresh runs.

Force a full bounded index refresh:

```bash
docker compose exec -T mailadmin python manage.py sync_mail_index \
  --account user@finestar.hr \
  --limit 500 \
  --full
```

Check status:

```bash
docker compose exec -T mailadmin python manage.py shell
```

```python
from mailops.models import MailAccountIndex

index = MailAccountIndex.objects.get(account_email="user@finestar.hr")
print(index.index_status, index.last_indexed_at, index.last_sync_error)
```

## Smoke Path

Use this minimal path for first production validation:

1. Confirm target mailbox login:

```bash
./scripts/mail.sh debug login user@finestar.hr 'StrongPasswordHere'
```

2. Confirm stored mailbox credentials:

```bash
docker compose exec -T mailadmin python manage.py shell -c \
  'from mailops.models import MailboxTokenCredential; print(MailboxTokenCredential.objects.filter(mailbox_email="user@finestar.hr").exists())'
```

3. Bootstrap Gmail OAuth.

4. Dry run:

```bash
docker compose exec -T mailadmin python manage.py run_gmail_import \
  --account source@gmail.com \
  --target user@finestar.hr \
  --limit 10 \
  --dry-run \
  --no-delete
```

5. Import without Gmail cleanup:

```bash
docker compose exec -T mailadmin python manage.py run_gmail_import \
  --account source@gmail.com \
  --target user@finestar.hr \
  --limit 10 \
  --no-delete
```

6. Refresh the mail index:

```bash
docker compose exec -T mailadmin python manage.py sync_mail_index \
  --account user@finestar.hr \
  --limit 100 \
  --full
```

7. Inspect import records:

```bash
docker compose exec -T mailadmin python manage.py shell -c \
  'from mailops.models import GmailImportMessage; print(list(GmailImportMessage.objects.values("gmail_message_id", "state", "target_folder")[:10]))'
```

8. Enable `delete_after_import=True` only after the imported messages are visible
   in the target mailbox and import records look correct.

## Troubleshooting

OAuth config missing:

- confirm `GMAIL_IMPORT_GOOGLE_CLIENT_ID`
- confirm `GMAIL_IMPORT_GOOGLE_CLIENT_SECRET`
- rebuild/restart `mailadmin`

Target mailbox credential missing:

- login once through the mailbox API
- confirm `MailboxTokenCredential` exists for the target mailbox

History cursor expired:

- incremental sync falls back to bounded recent rescan
- dedupe still uses `(import_account, gmail_message_id)`
- check `GmailImportAccount.last_error` and recent `GmailImportRun` rows

Partial import run:

- inspect failed `GmailImportMessage.error`
- rerun the same command; committed messages are skipped and failed messages are
  retried
- do not enable `delete_after_import` until the failure mode is understood

Cleanup failed:

- messages remain in committed state with `cleanup_status=failed`
- rerun with cleanup enabled to retry Gmail deletion
- use `--no-delete` to pause cleanup while investigating

Logs:

```bash
docker compose logs --tail=100 mailadmin
docker compose logs --tail=100 gmail-import-sync
```
