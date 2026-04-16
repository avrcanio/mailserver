# Mailadmin Mailbox API

These internal JSON endpoints are exposed by Django `mailadmin` for staff users only. They use the active Django admin session and do not store mailbox passwords.

Use the canonical mail account address and password per request:

```json
{
  "accountEmail": "user@finestar.hr",
  "password": "mailbox-password"
}
```

## List Message Summaries

`POST /api/mail/messages/`

Request:

```json
{
  "accountEmail": "user@finestar.hr",
  "password": "mailbox-password",
  "folder": "INBOX",
  "limit": 25
}
```

Response:

```json
{
  "accountEmail": "user@finestar.hr",
  "folder": "INBOX",
  "messages": [
    {
      "uid": "42",
      "folder": "INBOX",
      "subject": "Hello",
      "sender": "Sender <sender@example.com>",
      "to": ["user@finestar.hr"],
      "cc": [],
      "date": "2026-04-16T07:00:00+00:00",
      "messageId": "<m1@example.com>",
      "flags": ["Seen"],
      "size": 1234
    }
  ]
}
```

## Get Message Detail

`POST /api/mail/message/`

Request:

```json
{
  "accountEmail": "user@finestar.hr",
  "password": "mailbox-password",
  "folder": "INBOX",
  "uid": "42"
}
```

Response:

```json
{
  "accountEmail": "user@finestar.hr",
  "folder": "INBOX",
  "message": {
    "uid": "42",
    "folder": "INBOX",
    "subject": "Hello",
    "sender": "Sender <sender@example.com>",
    "to": ["user@finestar.hr"],
    "cc": [],
    "date": "2026-04-16T07:00:00+00:00",
    "messageId": "<m1@example.com>",
    "flags": ["Seen"],
    "size": 2048,
    "textBody": "Plain body",
    "htmlBody": "<p>HTML body</p>",
    "attachments": [
      {
        "filename": "report.pdf",
        "contentType": "application/pdf",
        "size": 12345,
        "disposition": "attachment"
      }
    ]
  }
}
```

## Send Mail

`POST /api/mail/send/`

Request:

```json
{
  "accountEmail": "sender@finestar.hr",
  "password": "mailbox-password",
  "to": ["recipient@example.com"],
  "cc": ["copy@example.com"],
  "bcc": ["hidden@example.com"],
  "replyTo": "reply@finestar.hr",
  "subject": "Status",
  "textBody": "Plain body",
  "htmlBody": "<p>HTML body</p>"
}
```

Response:

```json
{
  "accountEmail": "sender@finestar.hr",
  "status": "sent",
  "messageId": "<generated-message-id@example.com>"
}
```

## Errors

Common validation errors return HTTP 400:

```json
{"error": "account_email_and_password_required"}
```

Mail integration failures are normalized:

- `mail_auth_failed`: HTTP 401
- `mail_timeout`: HTTP 504
- `mail_connection_failed`: HTTP 502
- `mail_protocol_failed`: HTTP 502
- `mail_send_failed`: HTTP 502

