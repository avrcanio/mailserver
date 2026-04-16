# Mailadmin Mailbox API

These Django REST Framework endpoints expose the MVP backend mail API for mobile clients. The client logs in once with mailbox credentials and then sends the returned token in the `Authorization` header for mailbox operations.

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
  "account_email": "user@finestar.hr",
  "token": "server-generated-token",
  "folder_count": 5
}
```

Use the returned token on later requests:

```http
Authorization: Token server-generated-token
```

`Bearer server-generated-token` is also accepted.

`GET /api/auth/me`

Response:

```json
{
  "authenticated": true,
  "account_email": "user@finestar.hr"
}
```

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

Response:

```json
{
  "account_email": "user@finestar.hr",
  "folder": "INBOX",
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
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "size": 12345,
        "disposition": "attachment"
      }
    ]
  }
}
```

## Send

`POST /api/mail/send`

Request:

```json
{
  "to": ["recipient@example.com"],
  "cc": ["copy@example.com"],
  "bcc": ["hidden@example.com"],
  "reply_to": "reply@finestar.hr",
  "subject": "Status",
  "text_body": "Plain body",
  "html_body": "<p>HTML body</p>",
  "from_display_name": "Finestar Mail"
}
```

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

## Errors

Unauthenticated mailbox API requests return:

```json
{"error": "not_authenticated"}
```

Mail integration failures are normalized:

- `mail_auth_failed`: HTTP 401
- `mail_timeout`: HTTP 504
- `mail_connection_failed`: HTTP 502
- `mail_protocol_failed`: HTTP 502
- `mail_send_failed`: HTTP 502
