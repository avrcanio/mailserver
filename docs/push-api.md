# Mailadmin Push API

These endpoints support Android FCM registration and mailserver-triggered push delivery. Device registration is tied to the authenticated mailbox token from `POST /api/auth/login`; the mailserver hook remains protected by a service secret.

One Android app install can register the same FCM token for multiple mailbox accounts. The backend stores one row per normalized `(account_email, fcm_token)` pair; the FCM token is not a permanent account identity model.

## Device Registration

`POST /api/devices/`

Headers:

```http
Authorization: Token drf-token-key
X-Device-Registration-Secret: shared-registration-secret
Content-Type: application/json
```

Request:

```json
{
  "account_email": "user@finestar.hr",
  "fcmToken": "android-fcm-token",
  "platform": "android",
  "appVersion": "1.0.0"
}
```

Compatibility aliases are accepted for the account email: `account_email`, `accountEmail`, `accountId`, or `email`. The stored mailbox email always comes from the authenticated token after `strip().lower()` normalization; if a supplied account email does not match the normalized token identity, the request is rejected.

Registration semantics:

- `account_email` is normalized with `strip().lower()` before lookup and save
- `fcm_token` / `fcmToken` is normalized with `strip()` and must be non-empty
- registering the same FCM token for mailbox A and mailbox B creates two enabled associations
- registering the same mailbox/token pair updates that association instead of replacing other mailboxes on the same token

Response:

```json
{
  "status": "ok",
  "created": true,
  "id": 123,
  "account_email": "user@finestar.hr"
}
```

Errors:

- missing or invalid DRF token: `401 {"error": "not_authenticated"}`
- valid token without stored mailbox credentials: `401 {"error": "mailbox_credentials_missing"}`
- invalid registration secret: `403 {"error": "unauthorized"}`
- mismatched supplied account email: `403 {"error": "account_email_mismatch"}`
- invalid request body: `400`

## New Mail Hook

`POST /api/mail/new/`

Headers:

```http
X-Mail-Hook-Secret: shared-mail-hook-secret
Content-Type: application/json
```

Request:

```json
{
  "accountEmail": "user@finestar.hr",
  "sender": "Sender Name <sender@example.com>",
  "subject": "Hello",
  "receivedAt": "2026-04-16T07:00:00Z",
  "folder": "INBOX",
  "uid": "42",
  "messageId": "<m1@example.com>"
}
```

Response:

```json
{
  "status": "success",
  "deviceCount": 2,
  "successCount": 2,
  "failureCount": 0
}
```

If no enabled devices are registered for the mailbox, the endpoint returns a successful no-op response:

```json
{
  "status": "skipped",
  "deviceCount": 0,
  "successCount": 0,
  "failureCount": 0
}
```

Push payload rules:

- notification title is the sender/display name
- notification body is the subject
- data contains only `accountEmail`, `folder`, `uid`, `messageId`, and `receivedAt`
- email body content is never sent to FCM or stored in push logs

Delivery handling:

- invalid hook secret: `403 {"error": "unauthorized"}`
- invalid request body: `400`
- invalid/unregistered FCM tokens are disabled without failing delivery to other devices; because the FCM token represents the app install, cleanup disables all rows for that token and does not delete mailbox credentials
- each delivery attempt is recorded in `PushNotificationLog`
