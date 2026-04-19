# Mailadmin Mailbox API

These Django REST Framework endpoints expose the MVP backend mail API for mobile clients. The client logs in once with mailbox credentials, receives a Django-user-backed DRF API token, and sends that token in the `Authorization` header for later mailbox operations.

Mailbox credentials are validated through the mail integration layer and stored server-side against the DRF token. Android stores only the backend-issued token.

## Auth

`POST /api/auth/login`

Request:

```json
{
  "email": "user@finestar.hr",
  "password": "mailbox-password"
}
```

Response:

```json
{
  "authenticated": true,
  "user": {
    "id": 12,
    "email": "user@finestar.hr"
  },
  "account_email": "user@finestar.hr",
  "token": "drf-token-key",
  "folder_count": 5
}
```

Use the returned token on later requests:

```http
Authorization: Token drf-token-key
```

The token belongs to a non-staff active Django user whose `email` and `username` match the mailbox email.

`GET /api/auth/me`

Response:

```json
{
  "authenticated": true,
  "user": {
    "id": 12,
    "email": "user@finestar.hr"
  },
  "account_email": "user@finestar.hr"
}
```

`POST /api/auth/logout`

Headers:

```http
Authorization: Token drf-token-key
```

Request body is optional and ignored.

Response:

```json
{
  "success": true
}
```

Logout revokes only the current DRF token. The linked server-side mailbox credential is deleted automatically, so the same token can no longer access `/api/auth/me` or protected mail endpoints. The Django user and existing push device registrations remain unchanged.

## Account Summaries

`GET /api/accounts/summaries?fcm_token=android-fcm-token`

Use the current mailbox DRF token in the `Authorization` header. `fcmToken` is accepted as an alias for `fcm_token`.

For this MVP, `fcm_token` is a temporary device-link lookup mechanism for the Accounts screen. It is normalized with `strip()`, must be non-empty, and is used only to find mailbox accounts already linked to the same app install through `POST /api/devices/`. It is not a permanent account identity design.

The authenticated mailbox must already have an enabled registration for the supplied FCM token. The response includes all enabled mailbox associations for that FCM token that still have stored server-side mailbox credentials.

Response:

```json
{
  "accounts": [
    {
      "account_email": "user@finestar.hr",
      "display_name": "",
      "unread_count": 12,
      "important_count": 3
    }
  ]
}
```

Counter semantics:

- `unread_count` is the number of IMAP `UNSEEN` messages in `INBOX`
- `important_count` is the number of IMAP `FLAGGED` / starred messages in `INBOX`
- Trash, Spam/Junk, Archive, Sent, and other folders are not included in these MVP counters

Errors:

- missing or invalid DRF token: `401 {"error": "not_authenticated"}`
- valid token without stored mailbox credentials: `401 {"error": "mailbox_credentials_missing"}`
- missing or blank FCM token: `400`
- FCM token is not linked to the authenticated mailbox: `403 {"error": "fcm_token_not_linked"}`

## Folders

`GET /api/mail/folders`

Response:

```json
{
  "account_email": "user@finestar.hr",
  "folders": [
    {
      "name": "INBOX",
      "path": "INBOX",
      "display_name": "INBOX",
      "parent_path": null,
      "depth": 0,
      "delimiter": "/",
      "flags": ["HasChildren"],
      "selectable": true
    },
    {
      "name": "INBOX/Invoices/2026",
      "path": "INBOX/Invoices/2026",
      "display_name": "2026",
      "parent_path": "INBOX/Invoices",
      "depth": 2,
      "delimiter": "/",
      "flags": ["HasNoChildren"],
      "selectable": true
    }
  ]
}
```

`name` remains a backwards-compatible alias for the full IMAP folder path. New clients should use `path` as the stable identifier when calling message APIs, `display_name` for the visible label, and `depth` / `parent_path` to render nested folders. Folders with `selectable: false` are visible hierarchy nodes but should not be opened.

## Messages

`GET /api/mail/messages?folder=INBOX&limit=25`

Use `before_uid` to fetch the next older page for lazy-loaded infinite scrolling:

```http
GET /api/mail/messages?folder=INBOX&limit=25&before_uid=42
```

Response:

```json
{
  "account_email": "user@finestar.hr",
  "folder": "INBOX",
  "has_more": true,
  "next_before_uid": "42",
  "messages": [
    {
      "uid": "42",
      "folder": "INBOX",
      "subject": "Hello",
      "sender": "Sender <sender@example.com>",
      "to": ["user@finestar.hr"],
      "cc": [],
      "date": "2026-04-16T07:00:00Z",
      "message_id": "<m1@example.com>",
      "flags": ["Seen"],
      "size": 1234,
      "has_attachments": true,
      "has_visible_attachments": true
    }
  ]
}
```

## Conversations

`GET /api/mail/conversations?folder=INBOX&limit=50`

Conversations are computed server-side for one folder. `folder` defaults to `INBOX`. `limit` defaults to `50`, accepts `1` through `200`, and applies to the number of conversations returned. The backend may fetch or scan more than `limit` messages internally so it can assemble up to `limit` conversations.

Response:

```json
{
  "account_email": "user@finestar.hr",
  "folder": "INBOX",
  "conversations": [
    {
      "conversation_id": "thread-hash",
      "message_count": 3,
      "reply_count": 2,
      "has_unread": true,
      "has_attachments": true,
      "has_visible_attachments": true,
      "participants": [
        {
          "name": "Sender Name",
          "email": "sender@example.com"
        }
      ],
      "root_message": {
        "uid": "40",
        "folder": "INBOX",
        "subject": "Hello",
        "sender": "Sender Name <sender@example.com>",
        "to": ["user@finestar.hr"],
        "cc": [],
        "date": "2026-04-16T07:00:00Z",
        "message_id": "<root@example.com>",
        "flags": ["Seen"],
        "size": 1234,
        "has_attachments": false,
        "has_visible_attachments": false
      },
      "replies": [
        {
          "uid": "42",
          "folder": "INBOX",
          "subject": "Re: Hello",
          "sender": "Reply Person <reply@example.com>",
          "to": ["sender@example.com"],
          "cc": [],
          "date": "2026-04-16T08:00:00Z",
          "message_id": "<reply@example.com>",
          "flags": [],
          "size": 2345,
          "has_attachments": true,
          "has_visible_attachments": true
        }
      ],
      "latest_date": "2026-04-16T08:00:00Z"
    }
  ]
}
```

`root_message` and `replies` use the same summary shape as `GET /api/mail/messages`. Fetch full bodies or attachment metadata through the existing message detail endpoint.

Threading rules:

- Message-ID based threading is used first through `Message-ID`, `In-Reply-To`, and `References`.
- `root_message` is determined primarily by the referenced parent chain. If the parent chain is incomplete or inconsistent, the backend falls back to the earliest dated message, then the lower numeric UID.
- Normalized-subject fallback is used only for orphan messages where usable ID-based threading metadata is missing. `Re:`, `Fw:`, and `Fwd:` prefixes are stripped repeatedly.
- Replies are sorted chronologically after the root, then by lower UID. Conversations are sorted by latest activity descending.
- `has_attachments` is true when any message in the conversation has any attachment-like MIME part. `has_visible_attachments` is true when any message has at least one visible attachment.

## Unified Conversations

`GET /api/mail/unified-conversations?limit=50`

Unified conversations are computed across `INBOX` and the account's Sent folder so the client can render a full inbound/outbound timeline. `limit` defaults to `50`, accepts `1` through `200`, and applies to the number of conversations returned. The backend may fetch or scan more than `limit` messages internally.

The endpoint keeps the same response contract whether data comes from the Django mail index or from live IMAP. When a usable index exists for the authenticated mailbox, the backend serves indexed metadata first. If the index is missing, empty, stale, or not ready, the backend falls back to the live IMAP implementation.

Live IMAP conversation fallback scans only the most recent `MAIL_CONVERSATION_SCAN_LIMIT` messages per folder, defaulting to `1000`, so large folders do not trigger unbounded metadata fetches.

Response:

```json
{
  "account_email": "user@finestar.hr",
  "folders": ["INBOX", "Sent"],
  "conversations": [
    {
      "conversation_id": "thread-hash",
      "message_count": 2,
      "reply_count": 1,
      "has_unread": false,
      "has_attachments": true,
      "has_visible_attachments": true,
      "participants": [
        {
          "name": "Sender Name",
          "email": "sender@example.com"
        }
      ],
      "latest_date": "2026-04-16T08:00:00Z",
      "messages": [
        {
          "uid": "42",
          "folder": "INBOX",
          "direction": "inbound",
          "subject": "Hello",
          "sender": "Sender Name <sender@example.com>",
          "to": ["user@finestar.hr"],
          "cc": [],
          "date": "2026-04-16T07:00:00Z",
          "message_id": "<root@example.com>",
          "flags": ["Seen"],
          "size": 1234,
          "has_attachments": false,
          "has_visible_attachments": false
        },
        {
          "uid": "7",
          "folder": "Sent",
          "direction": "outbound",
          "subject": "Re: Hello",
          "sender": "User <user@finestar.hr>",
          "to": ["sender@example.com"],
          "cc": [],
          "date": "2026-04-16T08:00:00Z",
          "message_id": "<reply@example.com>",
          "flags": [],
          "size": 2345,
          "has_attachments": true,
          "has_visible_attachments": true
        }
      ]
    }
  ]
}
```

The backend resolves the Sent folder by special-use `\Sent` flag first, then common names such as `Sent`, `INBOX/Sent`, `INBOX.Sent`, and `Sent Messages`. If Sent cannot be resolved, the endpoint returns INBOX-only unified conversations and `folders: ["INBOX"]`.

Threading uses the same ID-first rules as folder-local conversations, but the deterministic `conversation_id` is independent of source folder so matching INBOX/Sent messages appear in one thread. Each message keeps its original `folder` and `uid`; clients must use those values for detail and message actions.

Duplicate messages with the same normalized `Message-ID` are rendered once. When possible, the backend infers logical direction from sender/recipient data and keeps the copy whose folder matches that direction: inbound prefers `INBOX`, outbound prefers Sent. If direction cannot be inferred confidently, a stable folder/UID tie-break is used. `has_unread` considers inbound messages only; Sent messages do not create unread state.

Indexing can be refreshed operationally with:

```bash
python manage.py sync_mail_index --account user@finestar.hr --limit 500
```

By default the command performs incremental UID-window sync when folder state exists. Use `--full` for a bounded initial-style rescan. The index stores message metadata only; it does not store message bodies, raw MIME payloads, or attachment bytes. For the full operational and implementation overview, see `docs/mail-indexing.md`.

The deployed stack also includes a periodic sync runner:

```bash
python manage.py run_mail_index_sync_cycle
python manage.py run_mail_index_sync_cycle --loop --interval-seconds 600
```

In Docker, the `mailindex-sync` service runs this loop. It selects stale indexed accounts, skips active syncs, applies a cooldown for recent failures, and logs each cycle summary. Defaults are configured with `MAIL_INDEX_SYNC_INTERVAL_SECONDS`, `MAIL_INDEX_SYNC_STALE_AFTER_SECONDS`, `MAIL_INDEX_SYNC_FAILURE_COOLDOWN_SECONDS`, `MAIL_INDEX_SYNC_MAX_ACCOUNTS`, and `MAIL_INDEX_SYNC_LIMIT`.

Incremental sync refreshes newer UIDs plus a recent metadata window. It does not delete indexed rows that are missing from that recent window unless `MAIL_INDEX_RECONCILE_DELETIONS=true` is explicitly enabled, because deletion reconciliation depends on the IMAP server returning a complete and trustworthy UID view for that checked window.

`GET /api/mail/index-status`

Returns the stored Django mail index status for the authenticated mailbox. The endpoint is read-only, requires the same mailbox token context as the other mailbox APIs, and never triggers sync or live IMAP calls. Optional query parameter: `account_email`; when omitted, the current token mailbox is used.

Response:

```json
{
  "account_email": "user@finestar.hr",
  "index_status": "ready",
  "last_indexed_at": "2026-04-17T13:40:00Z",
  "last_sync_started_at": "2026-04-17T13:39:50Z",
  "last_sync_finished_at": "2026-04-17T13:40:00Z",
  "last_sync_error": "",
  "folders": [
    {
      "folder": "INBOX",
      "uidvalidity": "12345",
      "highest_indexed_uid": 500,
      "last_synced_at": "2026-04-17T13:40:00Z"
    }
  ]
}
```

If the authenticated user has no index row for the requested mailbox, the endpoint returns `404 {"error": "mail_index_not_found"}`. Invalid `account_email` returns `400 {"error": "invalid_account_email"}`. `last_sync_error` is returned as stored operational status only; sync code must not write credentials, raw message bodies, or raw MIME content into that field.

`GET /api/mail/messages/42?folder=INBOX`

Response:

```json
{
  "account_email": "user@finestar.hr",
  "folder": "INBOX",
  "message": {
    "uid": "42",
    "folder": "INBOX",
    "subject": "Hello",
    "sender": "Sender <sender@example.com>",
    "to": ["user@finestar.hr"],
    "cc": [],
    "date": "2026-04-16T07:00:00Z",
    "message_id": "<m1@example.com>",
    "flags": ["Seen"],
    "size": 2048,
    "has_attachments": true,
    "has_visible_attachments": true,
    "text_body": "Plain body",
    "html_body": "<p>HTML body</p>",
    "attachments": [
      {
        "id": "att_1",
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size": 12345,
        "disposition": "attachment",
        "is_inline": false,
        "content_id": "",
        "is_visible": true
      },
      {
        "id": "att_2",
        "filename": "logo.png",
        "content_type": "image/png",
        "size": 2345,
        "disposition": "inline",
        "is_inline": true,
        "content_id": "logo123",
        "is_visible": false
      }
    ]
  }
}
```

## Attachments

Message detail includes attachment metadata with stable per-message IDs such as `att_1`, `att_2`, in MIME traversal order. Inline HTML body resources are included when they have MIME attachment-like metadata, so clients can fetch bytes for `cid:` image rendering.

Use `has_visible_attachments` for message-list paperclip UI. `has_attachments` remains backward-compatible and can be true for inline-only CID resources.

Attachment `content_id` is normalized without surrounding angle brackets. Clients should use `is_visible` for attachment chips when present. Inline body resources and duplicate image payloads of referenced inline resources are included for CID rendering, but marked with `is_visible: false`.

`GET /api/mail/messages/42/attachments/att_1?folder=INBOX`

Response is binary attachment content. The backend sets `Content-Type` from the MIME part and `Content-Disposition` with the filename when available.

Attachment download errors:

- missing or blank `folder`: `400 {"error": "invalid_folder"}`
- unknown attachment ID: `404 {"error": "attachment_not_found"}`

## Delete

Delete moves messages from the source folder to the server Trash folder. It does not permanently delete or expunge messages.

`POST /api/mail/messages/delete`

Request:

```json
{
  "folder": "INBOX",
  "uids": ["123", "124", "130"]
}
```

Response:

```json
{
  "account_email": "user@finestar.hr",
  "folder": "INBOX",
  "trash_folder": "Trash",
  "success": true,
  "partial": false,
  "moved_to_trash": ["123", "124", "130"],
  "failed": []
}
```

Partial failures still return HTTP 200 and identify the failed UIDs:

```json
{
  "account_email": "user@finestar.hr",
  "folder": "INBOX",
  "trash_folder": "Trash",
  "success": false,
  "partial": true,
  "moved_to_trash": ["123"],
  "failed": [
    {
      "uid": "124",
      "error": "move_failed",
      "detail": "IMAP move failed for UID 124"
    }
  ]
}
```

For a single message, use the same behavior through:

```http
POST /api/mail/messages/123/delete?folder=INBOX
```

## Restore

Restore moves messages from Trash into an explicit non-Trash target folder. The backend does not infer the original folder.

`POST /api/mail/messages/restore`

Request:

```json
{
  "folder": "Trash",
  "target_folder": "INBOX",
  "uids": ["123", "124"]
}
```

Response:

```json
{
  "account_email": "user@finestar.hr",
  "folder": "Trash",
  "target_folder": "INBOX",
  "success": true,
  "partial": false,
  "restored": ["123", "124"],
  "failed": []
}
```

Partial failures still return HTTP 200 and identify the failed UIDs:

```json
{
  "account_email": "user@finestar.hr",
  "folder": "Trash",
  "target_folder": "INBOX",
  "success": false,
  "partial": true,
  "restored": ["123"],
  "failed": [
    {
      "uid": "124",
      "error": "restore_failed",
      "detail": "IMAP restore failed for UID 124"
    }
  ]
}
```

For a single message, use:

```http
POST /api/mail/messages/123/restore?folder=Trash&target_folder=INBOX
```

Restore requires the source folder to resolve to the server Trash folder, and the target folder must be a non-Trash folder.

## Send

`POST /api/mail/send`

Request:

```json
{
  "to": ["Recipient Name <recipient@example.com>"],
  "cc": ["copy@example.com"],
  "bcc": ["hidden@example.com"],
  "reply_to": "Reply Person <reply@finestar.hr>",
  "in_reply_to": "<source-message-id@example.com>",
  "references": ["<root-message-id@example.com>", "<source-message-id@example.com>"],
  "subject": "Status",
  "text_body": "Plain body",
  "html_body": "<p>HTML body</p>",
  "from_display_name": "Finestar Mail"
}
```

Recipient fields accept either plain addresses such as `recipient@example.com` or mailbox-formatted values such as `Recipient Name <recipient@example.com>`. The backend normalizes them to one email address per item before sending. `Bcc` recipients are used only in the SMTP envelope and are not exposed in email headers.

For reply and reply-all flows, clients should send `in_reply_to` and `references` from the source message headers when available. Use the source message's `message_id` as `in_reply_to`; build `references` from the source message's existing `references` plus the source `message_id`. The backend writes these values as `In-Reply-To` and `References` headers so later indexing can attach the Sent copy to the same thread.

After SMTP delivery succeeds, the backend appends the exact generated MIME message to the resolved IMAP Sent folder with the `\Seen` flag and marks the existing mail index stale. The next unified conversation refresh can then see the Sent copy through live IMAP fallback while background indexing catches up. If SMTP succeeds but the Sent append fails, the API still returns `status: sent` and logs a warning to avoid duplicate sends from client retries.

For attachments, send `multipart/form-data` to the same endpoint. Text fields keep the same names, and files use repeated `attachments` parts:

```http
POST /api/mail/send
Content-Type: multipart/form-data
Authorization: Token drf-token-key
```

```text
to=recipient@example.com
subject=Status
text_body=Plain body
attachments=@report.pdf
attachments=@photo.jpg
```

Multipart recipient fields may be repeated, or `to`, `cc`, and `bcc` may contain comma-separated address values. Attachment limits are 10 MB per file and 25 MB total per send request. Oversized files return `attachment_too_large`; oversized total payloads return `attachments_too_large`.

For forwarding original visible attachments, include `forward_source_message` on the same send request. The client supplies the source message reference and the selected attachment IDs from message detail; the backend resolves the original bytes from IMAP and preserves filename and content type.

```json
{
  "to": ["recipient@example.com"],
  "subject": "Fwd: TELWIN",
  "text_body": "Forwarded body",
  "forward_source_message": {
    "folder": "INBOX",
    "uid": "42",
    "attachment_ids": ["att_3", "att_4"]
  }
}
```

For `multipart/form-data`, send `forward_source_message` as a JSON string field alongside any repeated `attachments` file parts. Forwarded original attachments and newly uploaded attachments are included in one outgoing message. The order of `forward_source_message.attachment_ids` is preserved for forwarded attachments.

Only attachments with `is_visible: true` are eligible for forwarding. If a requested ID exists but is hidden or inline-only, the response is `400 {"error": "forward_attachment_not_visible"}`. If a requested ID is not present on the source message, the response is `400 {"error": "forward_attachment_not_found"}`. Hidden CID resources are never silently forwarded.

Reply and reply-all flows should not include original attachments unless the client explicitly sends `forward_source_message`.

Response:

```json
{
  "account_email": "sender@finestar.hr",
  "status": "sent",
  "message_id": "<generated-message-id@example.com>"
}
```

## Documentation

- OpenAPI schema: `GET /api/schema/`
- Swagger UI: `GET /api/docs/`
- ReDoc: `GET /api/redoc/`

## Credential Encryption

Mailbox passwords stored in `MailboxTokenCredential.mailbox_password` are encrypted at rest with Fernet and use the `fernet:v1:` storage prefix. The backend decrypts them only in memory when creating IMAP/SMTP credentials for protected mail operations.

Required environment:

```bash
MAILBOX_CREDENTIAL_ENCRYPTION_KEY=fernet-key
```

Generate a key with:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Losing this key makes stored mailbox credentials undecryptable. Key rotation is intentionally left for a follow-up; for now, configure the key, rebuild, run migrations, and recreate `mailadmin`.

## Errors

Unauthenticated mailbox API requests return:

```json
{"error": "not_authenticated"}
```

Valid DRF tokens without a stored mailbox credential record return:

```json
{"error": "mailbox_credentials_missing"}
```

Mail integration failures are normalized:

- `mail_auth_failed`: HTTP 401
- `mail_timeout`: HTTP 504
- `mail_connection_failed`: HTTP 502
- `mail_protocol_failed`: HTTP 502
- `mail_send_failed`: HTTP 502
