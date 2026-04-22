# Gmail Import

This document describes the user-scoped Gmail import flow in `mailadmin`.

The v1 model is intentionally strict:

- one Django user maps to one Gmail account and one local mailserver mailbox
- the connected Gmail address must exactly match the Django user email
- onboarding is admin-managed through Django admin
- Gmail access is OAuth-only; admins must not enter or store Gmail passwords
- mailbox read APIs continue to use the local mailserver mailbox
- send uses Gmail API for connected Gmail-backed mailboxes and local SMTP for
  normal local-domain mailboxes

The importer remains conservative:

- Gmail access uses OAuth and the Gmail API.
- The target side writes raw RFC822 messages through the mailserver IMAP service.
- Import success means target IMAP append plus durable import-record commit.
- Mail index/search visibility is downstream and does not gate Gmail cleanup.
- Gmail cleanup is disabled by default.
- When cleanup is enabled, Gmail deletion happens only after committed import
  state.
- Permanent Gmail cleanup uses the Gmail API permanent delete operation, not
  trash or archive semantics, and requires the `https://mail.google.com/`
  OAuth scope.

## Scope

v1 supports one Gmail source account per Django user. The connected Gmail source,
the Django user email, and the target mailserver mailbox must be the same
normalized email address.

Imported source:

- Gmail All Mail through `in:anywhere`
- excludes Drafts, Spam, and Trash
- ignores Gmail labels as folder mapping

Target mapping:

- Gmail `SENT` messages append to the target Sent folder when Dovecot exposes one
- all other imported messages append to target `INBOX`

Out of scope for v1:

- user self-signup
- generic IMAP, POP, or SMTP external accounts
- connecting a Gmail account with a different email than the Django user
- connecting multiple Gmail accounts to one Django user
- mirroring Gmail labels into IMAP folders
- Gmail archive cleanup mode
- importing Gmail Drafts, Spam, or Trash

## Admin-Managed Onboarding

Create the Django user and local mailbox before Gmail connection. The password
here is the application/mailserver password, not a Gmail password.

With mailbox auto-provisioning enabled:

```bash
MAILBOX_AUTO_CREATE_FROM_USER_ADMIN=true
MAILBOX_AUTO_CREATE_SKIP_STAFF=true
```

Then:

1. Open `https://${MAILADMIN_HOST}/admin/`.
2. Add a non-staff Django user.
3. Set `username`, `email`, `password`, and `password confirmation`.
4. Save the user.
5. Confirm the mailbox exists and login works:

```bash
./scripts/mail.sh debug login user@example.com 'PasswordFromAdmin'
```

If mailbox auto-provisioning is not enabled, create the mailbox manually:

```bash
./scripts/mail.sh email add user@example.com 'StrongPasswordHere'
./scripts/mail.sh debug login user@example.com 'StrongPasswordHere'
```

The user must log in through the mailbox API at least once so `mailadmin` stores
the local mailbox credential used by Gmail import:

```bash
curl -sS -X POST "https://${MAILADMIN_HOST}/api/auth/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"user@example.com","password":"StrongPasswordHere"}'
```

Confirm stored credentials:

```bash
docker compose exec -T mailadmin python manage.py shell -c \
  'from mailops.models import MailboxTokenCredential; print(MailboxTokenCredential.objects.filter(mailbox_email="user@example.com").exists())'
```

## Google OAuth Setup

Create a Google OAuth client for the mailadmin app. The redirect URI must be the
URI used by the browser callback that receives Google's `code` and `state`:

```text
https://mailadmin.example.com/oauth/gmail/callback
```

The importer requests the Gmail full-mail scope so cleanup can permanently
delete Gmail source messages after committed import. It also keeps the
`gmail.modify` scope in the request for compatibility with accounts that were
previously connected before permanent cleanup was enabled:

```text
https://mail.google.com/
https://www.googleapis.com/auth/gmail.modify
```

Set these values in the local `.env`:

```bash
GMAIL_IMPORT_GOOGLE_CLIENT_ID=google-client-id
GMAIL_IMPORT_GOOGLE_CLIENT_SECRET=google-client-secret
GMAIL_IMPORT_OAUTH_REDIRECT_URI=https://app.example.com/oauth/gmail/callback
GMAIL_IMPORT_OAUTH_SCOPES=https://mail.google.com/,https://www.googleapis.com/auth/gmail.modify
GMAIL_IMPORT_SYNC_INTERVAL_SECONDS=600
GMAIL_IMPORT_SYNC_LIMIT=100
GMAIL_IMPORT_SYNC_MAX_ACCOUNTS=20
```

Restart `mailadmin` after changing env:

```bash
docker compose up -d --build mailadmin
```

When enabling the periodic sync service:

```bash
docker compose up -d --build gmail-import-sync
```

## User-Facing Gmail Connection API

All user-facing endpoints require the existing token authentication from
`/api/auth/login`. The token user, mailbox credential email, Django user email,
and Gmail OAuth identity must match.

Start Gmail OAuth:

```bash
TOKEN="mailadmin-token-from-login"

curl -sS -X POST "https://${MAILADMIN_HOST}/api/external-accounts/gmail/connect/start" \
  -H "Authorization: Token ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{}'
```

The response contains:

```json
{
  "authorization_url": "https://accounts.google.com/...",
  "state": "signed-state",
  "account_email": "user@example.com"
}
```

Open `authorization_url`, complete Google consent with the matching Gmail
account, then submit the returned authorization code and state:

```bash
curl -sS -X POST "https://${MAILADMIN_HOST}/api/external-accounts/gmail/connect/complete" \
  -H "Authorization: Token ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"code":"GOOGLE_AUTHORIZATION_CODE","state":"SIGNED_STATE_FROM_START"}'
```

OAuth completion is rejected when Google reports any Gmail address other than
the authenticated Django user email.

Read connected account status:

```bash
curl -sS "https://${MAILADMIN_HOST}/api/external-accounts/gmail" \
  -H "Authorization: Token ${TOKEN}"
```

List external accounts:

```bash
curl -sS "https://${MAILADMIN_HOST}/api/external-accounts" \
  -H "Authorization: Token ${TOKEN}"
```

The Gmail account contract includes:

```json
{
  "connected": true,
  "provider": "gmail",
  "gmail_email": "user@example.com",
  "target_mailbox_email": "user@example.com",
  "delete_after_import": false,
  "last_success_at": null,
  "last_error": "",
  "historical_import_completed": false,
  "historical_import_completed_at": null,
  "consecutive_failures": 0
}
```

Disconnect Gmail:

```bash
curl -sS -X POST "https://${MAILADMIN_HOST}/api/external-accounts/gmail/disconnect" \
  -H "Authorization: Token ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Disconnect deletes only the authenticated user's Gmail connection. It does not
delete the Django user, local mailbox, imported mail, or another user's Gmail
connection.

## Historical Import

Use the bounded sync trigger for user-facing imports. `mode=auto` runs
historical import until `historical_import_completed_at` is set, then switches to
incremental.

Run a first bounded import without Gmail cleanup:

```bash
curl -sS -X POST "https://${MAILADMIN_HOST}/api/external-accounts/gmail/sync" \
  -H "Authorization: Token ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"mode":"historical","limit":100,"no_delete":true}'
```

Optional date-bounded import:

```bash
curl -sS -X POST "https://${MAILADMIN_HOST}/api/external-accounts/gmail/sync" \
  -H "Authorization: Token ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"mode":"historical","limit":100,"since":"2026/04/01","no_delete":true}'
```

Inspect import state:

```bash
docker compose exec -T mailadmin python manage.py shell
```

```python
from mailops.models import GmailImportAccount, GmailImportMessage, GmailImportRun

account = GmailImportAccount.objects.get(user__email="user@example.com")
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

account = GmailImportAccount.objects.get(user__email="user@example.com")
account.delete_after_import = True
account.save(update_fields=["delete_after_import", "updated_at"])
```

Run another bounded sync without `no_delete`. Gmail source messages are eligible
for cleanup only when the import record is in `committed` state and has not
already been marked `cleaned`:

```bash
curl -sS -X POST "https://${MAILADMIN_HOST}/api/external-accounts/gmail/sync" \
  -H "Authorization: Token ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"mode":"auto","limit":100}'
```

Use `"no_delete": true` any time you want to override cleanup for one run.
Do not enable periodic or looped cleanup until a bounded manual cleanup batch
succeeds with zero failures.

## Incremental Sync

After a successful historical import, the importer stores
`historical_import_completed_at` and a Gmail `last_history_id` when available.

Run one bounded user-scoped incremental sync:

```bash
curl -sS -X POST "https://${MAILADMIN_HOST}/api/external-accounts/gmail/sync" \
  -H "Authorization: Token ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"mode":"incremental","limit":100,"no_delete":true}'
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

## Admin Compatibility Commands

The old management-command flow remains available for admin/global compatibility
and smoke testing. New user-facing work should use the user-scoped API flow.

Print a Google consent URL:

```bash
docker compose exec -T mailadmin python manage.py bootstrap_gmail_import_oauth \
  --gmail user@example.com \
  --target user@example.com
```

Store the returned authorization code:

```bash
docker compose exec -T mailadmin python manage.py bootstrap_gmail_import_oauth \
  --gmail user@example.com \
  --target user@example.com \
  --code 'GOOGLE_AUTHORIZATION_CODE'
```

Run a dry run from the compatibility path:

```bash
docker compose exec -T mailadmin python manage.py run_gmail_import \
  --account user@example.com \
  --target user@example.com \
  --limit 50 \
  --dry-run \
  --no-delete
```

## Index Refresh

The importer appends raw messages into IMAP. The mail index is a downstream
metadata cache and may not show imported messages until the existing index
refresh runs.

Force a full bounded index refresh:

```bash
docker compose exec -T mailadmin python manage.py sync_mail_index \
  --account user@example.com \
  --limit 500 \
  --full
```

Check status:

```bash
docker compose exec -T mailadmin python manage.py shell
```

```python
from mailops.models import MailAccountIndex

index = MailAccountIndex.objects.get(account_email="user@example.com")
print(index.index_status, index.last_indexed_at, index.last_sync_error)
```

## Two-User Isolation Smoke Path

Use this path to prove isolation before broader rollout:

1. Create two Django users and local mailboxes:
   - `user-a@example.com`
   - `user-b@example.com`
2. Log in each user through `/api/auth/login` and keep separate tokens.
3. Connect Gmail for user A with `user-a@example.com`.
4. Connect Gmail for user B with `user-b@example.com`.
5. Confirm each token sees only its own account:

```bash
curl -sS "https://${MAILADMIN_HOST}/api/external-accounts" \
  -H "Authorization: Token ${TOKEN_A}"

curl -sS "https://${MAILADMIN_HOST}/api/external-accounts" \
  -H "Authorization: Token ${TOKEN_B}"
```

6. Run bounded sync for user A and confirm imports target only
   `user-a@example.com`.
7. Run bounded sync for user B and confirm imports target only
   `user-b@example.com`.
8. Disconnect user A and confirm user B's account still exists.

Useful shell checks:

```bash
docker compose exec -T mailadmin python manage.py shell -c \
  'from mailops.models import GmailImportAccount; print(list(GmailImportAccount.objects.values("user__email", "gmail_email", "target_mailbox_email")))'
```

```bash
docker compose exec -T mailadmin python manage.py shell -c \
  'from mailops.models import GmailImportMessage; print(list(GmailImportMessage.objects.values("import_account__user__email", "gmail_message_id", "state", "target_folder")[:20]))'
```

## Troubleshooting

OAuth config missing:

- confirm `GMAIL_IMPORT_GOOGLE_CLIENT_ID`
- confirm `GMAIL_IMPORT_GOOGLE_CLIENT_SECRET`
- confirm `GMAIL_IMPORT_OAUTH_REDIRECT_URI`
- rebuild/restart `mailadmin`

OAuth completion rejected:

- confirm the Google consent account exactly matches the Django user email
- confirm the API token belongs to that same Django user
- confirm mailbox credential email matches `request.user.email`

Target mailbox credential missing:

- log in once through `/api/auth/login`
- confirm `MailboxTokenCredential` exists for the target mailbox

History cursor expired:

- incremental sync falls back to bounded recent rescan
- dedupe still uses `(import_account, gmail_message_id)`
- check `GmailImportAccount.last_error` and recent `GmailImportRun` rows

Partial import run:

- inspect failed `GmailImportMessage.error`
- rerun bounded sync; committed messages are skipped and failed messages are
  retried
- do not enable `delete_after_import` until the failure mode is understood

Cleanup failed:

- messages remain in committed state with `cleanup_status=failed`
- rerun with cleanup enabled to retry Gmail deletion
- use `"no_delete": true` to pause cleanup while investigating
- verify `GMAIL_IMPORT_OAUTH_SCOPES=https://mail.google.com/` and reconnect the
  Gmail account if cleanup fails with `permission denied`

Logs:

```bash
docker compose logs --tail=100 mailadmin
docker compose logs --tail=100 gmail-import-sync
```
