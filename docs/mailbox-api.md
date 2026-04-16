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

## Folders

`GET /api/mail/folders`

Response:

```json
{
  "account_email": "user@finestar.hr",
  "folders": [
    {
      "name": "INBOX",
      "delimiter": "/",
      "flags": ["HasNoChildren"]
    }
  ]
}
```

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
      "size": 1234
    }
  ]
}
```

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
    "text_body": "Plain body",
    "html_body": "<p>HTML body</p>",
    "attachments": [
      {
        "id": "att_1",
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size": 12345,
        "disposition": "attachment",
        "is_inline": false
      }
    ]
  }
}
```

## Attachments

Message detail includes attachment metadata with stable per-message IDs such as `att_1`, `att_2`, in MIME traversal order.

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
  "subject": "Status",
  "text_body": "Plain body",
  "html_body": "<p>HTML body</p>",
  "from_display_name": "Finestar Mail"
}
```

Recipient fields accept either plain addresses such as `recipient@example.com` or mailbox-formatted values such as `Recipient Name <recipient@example.com>`. The backend normalizes them to one email address per item before sending. `Bcc` recipients are used only in the SMTP envelope and are not exposed in email headers.

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
