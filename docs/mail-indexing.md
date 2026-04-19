# Mail Indexing

This document describes how the Django mail index works in `blocklist-admin/mailops`.
The index is a metadata cache for mailbox conversation lists. It is not a message
store and it does not replace IMAP as the source of truth.

## Purpose

The mobile client needs a fast unified conversation timeline across `INBOX` and
the account's Sent folder. Fetching and grouping recent IMAP metadata on every
request is slow and bounded by `MAIL_CONVERSATION_SCAN_LIMIT`, so Django stores a
server-side index of message headers and conversation metadata.

The index is used by:

- `GET /api/mail/unified-conversations`
- `GET /api/mail/index-status`
- the background `mailindex-sync` Docker service
- the operational `sync_mail_index` management command

Message details, full bodies, MIME payloads, and attachment bytes are still read
live from IMAP by folder and UID.

## Data Model

The index tables are defined in `mailops.models`.

`MailAccountIndex`

- one row per Django user and normalized mailbox email
- stores the IMAP host, resolved Sent folder, sync status, sync timestamps, and
  last sync error
- status values are `empty`, `syncing`, `ready`, `partial`, and `failed`
- unique constraint: `(user, account_email)`

`MailFolderIndexState`

- one row per indexed folder for an account
- stores `uidvalidity`, `highest_indexed_uid`, and `last_synced_at`
- used to choose initial vs incremental sync behavior

`MailMessageIndex`

- one row per indexed IMAP message copy, keyed by `(account, folder, uid)`
- stores header-level metadata: subject, sender, recipients, dates, flags,
  `Message-ID`, `In-Reply-To`, `References`, attachment booleans, size, direction,
  dedupe key, normalized subject, and thread key
- does not store message body text, HTML body, raw MIME content, or attachment
  bytes

`MailConversationIndex`

- one row per computed conversation thread
- stores `conversation_id`, `thread_key`, latest activity, message count, unread
  state, attachment flags, participants, and folders
- points indexed messages back to their current conversation

## Account Seeding

Mailbox credentials are stored in `MailboxTokenCredential` after successful
`POST /api/auth/login`. The background sync runner now creates missing
`MailAccountIndex` rows from those stored credentials at the beginning of every
sync cycle.

This matters because older accounts may have valid credentials but no index row.
Without an index row, the runner has nothing to select and the mailbox would keep
falling back to live IMAP. Seeding makes existing credentialed accounts eligible
for periodic indexing automatically.

The seeding path is:

1. `run_sync_cycle()` calls `seed_account_indexes_for_credentials()`.
2. It reads `MailboxTokenCredential` rows, optionally filtered by account email.
3. It calls `ensure_account(user, mailbox_email)`.
4. Normal sync selection then sees the new `empty` account row and indexes it.

## Folder Selection

Indexing currently targets:

- `INBOX`
- the resolved Sent folder, when it is not the same as `INBOX`

Sent folder resolution is done through the IMAP client:

1. Prefer a folder with the special-use `\Sent` flag.
2. Fall back to common names such as `Sent`, `INBOX/Sent`, `INBOX.Sent`, and
   `Sent Messages`.

The resolved Sent folder is stored on `MailAccountIndex.sent_folder`.

## Sync Commands

Run a one-off sync for a mailbox:

```bash
docker compose exec -T mailadmin python manage.py sync_mail_index --account user@finestar.hr --limit 500
```

Run a bounded full-style rescan:

```bash
docker compose exec -T mailadmin python manage.py sync_mail_index --account user@finestar.hr --limit 500 --full
```

Run one background cycle manually:

```bash
docker compose exec -T mailadmin python manage.py run_mail_index_sync_cycle
```

Run the loop directly:

```bash
docker compose exec -T mailadmin python manage.py run_mail_index_sync_cycle --loop --interval-seconds 600
```

In the deployed stack, the `mailindex-sync` service runs the loop:

```bash
docker compose ps mailindex-sync
docker compose logs --tail=100 mailindex-sync
```

## Sync Cycle

`run_sync_cycle()` is the periodic entry point.

The cycle:

1. Seeds missing `MailAccountIndex` rows from stored mailbox credentials.
2. Selects due accounts.
3. Looks up the matching `MailboxTokenCredential`.
4. Logs into IMAP through `MailIndexService.sync_account()`.
5. Resolves Sent.
6. Fetches metadata for `INBOX` and Sent.
7. Upserts `MailMessageIndex` rows.
8. Rebuilds touched `MailConversationIndex` rows.
9. Updates folder state and account sync status.

Selection rules:

- `empty` accounts are due immediately
- `ready` accounts are due when `last_indexed_at` is older than
  `MAIL_INDEX_SYNC_STALE_AFTER_SECONDS`
- `partial` and `failed` accounts are retried after
  `MAIL_INDEX_SYNC_FAILURE_COOLDOWN_SECONDS`
- stale `syncing` accounts are retried when their started timestamp is older
  than the stale threshold

The loop size and cadence are controlled by:

- `MAIL_INDEX_SYNC_INTERVAL_SECONDS`
- `MAIL_INDEX_SYNC_STALE_AFTER_SECONDS`
- `MAIL_INDEX_SYNC_FAILURE_COOLDOWN_SECONDS`
- `MAIL_INDEX_SYNC_MAX_ACCOUNTS`
- `MAIL_INDEX_SYNC_LIMIT`

## Initial vs Incremental Sync

Initial sync happens when no usable folder state exists or when `--full` is used.
It fetches recent conversation summaries up to the configured limit.

Incremental sync happens when folder state exists. It fetches:

- messages newer than the stored `highest_indexed_uid`
- a recent window of existing messages, currently `RECENT_WINDOW_SIZE = 100`

The recent window refreshes flags, attachment booleans, and metadata for recently
changed messages without scanning the whole mailbox every cycle.

If IMAP `UIDVALIDITY` changes, the folder is treated like a bounded initial scan
for that cycle because old UIDs may no longer identify the same messages.

## Deletion Reconciliation

By default the index does not delete rows simply because they are absent from a
recent IMAP window. This avoids accidental data loss if the server returns a
partial view or if the scanned window is not complete enough.

Deletion reconciliation is only enabled when:

```env
MAIL_INDEX_RECONCILE_DELETIONS=true
```

When enabled, only indexed rows inside the checked recent UID range can be
removed, and their conversations are rebuilt.

## Threading

Threading is ID-first.

For each message, the backend normalizes:

- `Message-ID`
- `In-Reply-To`
- `References`
- subject

The normal thread key flow is:

1. If a parent from `In-Reply-To` or `References` exists in the local indexed or
   fetched message map, use the root of that parent chain:

   ```text
   id:<root-message-id>
   ```

2. If the message has parent IDs but none of those parents exist locally, use a
   normalized subject fallback:

   ```text
   subject:<normalized-subject>
   ```

3. If there is no parent but the message has its own `Message-ID`, use:

   ```text
   id:<own-message-id>
   ```

4. If no usable IDs exist, use normalized subject if available.

5. If everything else is missing, use:

   ```text
   uid:<uid>
   ```

This keeps real reply chains stable when headers are complete, while still
grouping orphan replies and forwards when the referenced parent message is not
present in the mailbox.

## Subject Normalization

Basic subject normalization removes repeated `Re:`, `Fw:`, and `Fwd:` prefixes,
lowercases the string, trims whitespace, and collapses repeated spaces.

There is an additional business-subject grouping rule for offer emails:

```text
Fwd: Ponuda br. 121714
Re: Fwd: Ponuda br. 121714 razlika
```

Both normalize to:

```text
ponuda br. 121714
```

This rule is intentionally narrow. It extracts the numeric offer reference from
`Ponuda br.` subjects and ignores trailing notes such as `razlika`. It is used
for grouping only; the original subject is preserved on each message row.

## Conversation IDs

`conversation_id` is deterministic per mailbox account and thread key:

```text
sha256("<account_email>\\0<thread_key>")[:32]
```

This means the same logical thread gets a stable API identifier after reindexing,
as long as the account email and thread key stay the same.

Live IMAP fallback uses a shorter internal hash for compatibility, but indexed
unified conversations are the preferred path when the index is usable.

## Direction

Each unified message has a direction:

- `outbound` for messages in Sent
- `inbound` for messages in other indexed folders

The index also tries to infer direction from headers:

- sender equals the account email -> `outbound`
- account email appears in `To` or `Cc` -> `inbound`

Header inference helps when a copied message is stored in an unexpected folder.

## Dedupe

Unified conversations may see the same message in more than one folder, often
because sent or forwarded copies share a `Message-ID`.

Dedupe key:

- `msg:<normalized-message-id>` when `Message-ID` exists
- `uid:<folder>:<uid>` when no `Message-ID` exists

When duplicate copies exist, the backend prefers the copy whose folder matches
the inferred logical direction:

- inbound prefers `INBOX`
- outbound prefers Sent

If direction cannot be inferred, a stable folder and UID tie-break is used.

## Conversation Rebuild

After messages are upserted, every touched thread key is rebuilt.

Rebuild steps:

1. Load all indexed rows for the thread key.
2. Convert rows back to `MailMessageSummary` objects.
3. Dedupe duplicate message copies.
4. Sort messages chronologically with inbound before outbound at the same time.
5. Compute latest activity.
6. Compute participants from sender, `To`, and `Cc`.
7. Set unread state from unread inbound messages only.
8. Set attachment booleans from all rendered messages.
9. Update or create the `MailConversationIndex`.
10. Point all message rows for the thread key at that conversation.

## Serving Unified Conversations

`MailboxService.list_unified_conversations()` first checks the index when a
Django user is available.

The index is considered usable when:

- an account row exists
- status is `ready` or `partial`
- `last_indexed_at` is set
- at least one conversation exists
- `INBOX` folder state exists
- Sent folder state exists when a Sent folder is configured
- the index is not older than `MAIL_INDEX_MAX_AGE_SECONDS`, when that optional
  setting is greater than zero

If the index is not usable, the service falls back to live IMAP grouping. The API
response shape is the same in both modes.

## Status Endpoint

Use:

```http
GET /api/mail/index-status
```

Optional query:

```http
GET /api/mail/index-status?account_email=user@finestar.hr
```

The endpoint returns stored index status only. It does not log into IMAP and does
not start a sync.

Example:

```json
{
  "account_email": "user@finestar.hr",
  "index_status": "ready",
  "last_indexed_at": "2026-04-19T16:12:42Z",
  "last_sync_started_at": "2026-04-19T16:12:38Z",
  "last_sync_finished_at": "2026-04-19T16:12:42Z",
  "last_sync_error": "",
  "folders": [
    {
      "folder": "INBOX",
      "uidvalidity": "12345",
      "highest_indexed_uid": 223,
      "last_synced_at": "2026-04-19T16:12:42Z"
    },
    {
      "folder": "Sent",
      "uidvalidity": "67890",
      "highest_indexed_uid": 57,
      "last_synced_at": "2026-04-19T16:12:42Z"
    }
  ]
}
```

## Troubleshooting

Check containers:

```bash
docker compose ps mailadmin mailindex-sync
```

Check the background sync log:

```bash
docker compose logs --tail=100 mailindex-sync
```

Force a full bounded rescan:

```bash
docker compose exec -T mailadmin python manage.py sync_mail_index --account user@finestar.hr --limit 500 --full
```

Inspect index status:

```bash
docker compose exec -T mailadmin python manage.py shell
```

```python
from mailops.models import MailAccountIndex

account = MailAccountIndex.objects.get(account_email="user@finestar.hr")
print(account.index_status, account.sent_folder, account.last_indexed_at)
for state in account.folder_states.order_by("folder"):
    print(state.folder, state.uidvalidity, state.highest_indexed_uid, state.last_synced_at)
```

Inspect a specific thread:

```python
from mailops.models import MailAccountIndex, MailMessageIndex

account = MailAccountIndex.objects.get(account_email="user@finestar.hr")
for message in MailMessageIndex.objects.filter(account=account, subject__icontains="Ponuda br. 121714").order_by("sent_at", "folder", "uid"):
    print(message.folder, message.uid, message.thread_key, message.conversation_id, message.subject)
```

Common findings:

- Credential exists but no index row: the next sync cycle should seed it; run
  `run_mail_index_sync_cycle` or `sync_mail_index` to force it.
- Index status is `failed`: inspect `last_sync_error` and `mailindex-sync` logs.
- Sent messages missing from unified conversations: verify Sent folder resolution
  and check whether the Sent copy exists in IMAP.
- Messages are split into separate conversations: inspect `Message-ID`,
  `In-Reply-To`, `References`, and normalized subject.
- Messages are duplicated: check whether duplicate copies have different or
  missing `Message-ID` headers.

## Safety Notes

- Keep mail indexing metadata-only.
- Do not write passwords, raw message bodies, raw MIME payloads, or attachment
  bytes into index tables or logs.
- Prefer Docker commands on this host.
- Be careful with `MAIL_INDEX_RECONCILE_DELETIONS`; enable it only when the IMAP
  UID window behavior is understood and trusted.
