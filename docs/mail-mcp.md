# Mail MCP sidecar

Read/write MCP endpoint for Grok (and other remote MCP clients) against one mailbox.

## Endpoint

- URL: `https://mail-mcp.finestar.hr/mcp`
- Auth: `Authorization: Bearer <MAIL_MCP_TOKEN>`
- Transport: Streamable HTTP (stateless JSON)
- Health: `https://mail-mcp.finestar.hr/healthz` (no auth)

Mailbox password stays in server `.env` (`MAIL_MCP_MAILBOX_PASSWORD`). The connector only needs the Bearer token.

After rebuilding/redeploying `mail-mcp`, **restart the Grok connector** so it rediscovers tools (including updated `send_mail` schema).

## Tools

### Read

| Tool | Source | Notes |
|------|--------|-------|
| `list_folders` | IMAP | Live folder list |
| `list_messages` | IMAP | Always fresh summaries |
| `read_message` | IMAP peek | Does **not** mark `\Seen`; attachments are metadata only |
| `search_mail` | Postgres index first | Header/subject/sender/recipients; `INBOX` + Sent only |

`search_mail` falls back to IMAP `SEARCH TEXT` when `include_body=true`, the folder is outside the index, or the index is unusable. Responses include `source`, `index_last_indexed_at`, `indexed_folders`, and `stale`.

### Write

| Tool | Backend | Notes |
|------|---------|-------|
| `move_message` | IMAP MOVE | Archive / Junk / Trash / other selectable folders |
| `delete_message` | IMAP | Soft delete to Trash by default; `permanent=true` = STORE `\Deleted` + EXPUNGE |
| `set_flags` | IMAP STORE | `Seen`, `Flagged`, `Answered` only |
| `send_mail` | SMTP via `MailboxService` | Optional reply threading, alias `from_`, base64 attachments |

Soft delete is the default. `permanent=true` must be used **only** when the user explicitly says trajno / zauvijek / permanently / forever.

### `send_mail` arguments

| Arg | Required | Notes |
|-----|----------|-------|
| `to` | yes | CSV or single address |
| `subject` | yes | |
| `body` | yes | Plain text body |
| `cc` | no | |
| `from_` | no | Login mailbox or alias that targets it in `postfix-virtual.cf` (e.g. `accounting@finestar.hr`) |
| `from_name` | no | Display name for From header |
| `attachments` | no | List of `{filename, content_base64, mime_type?}` (max 10 files; **10 MB each after base64 decode**; HTTP body up to 16 MB) |
| `reply_to_uid` / `reply_folder` | no | Sets In-Reply-To / References |

`from_` is rejected with `from_not_allowed` unless it is the configured mailbox or an alias pointing at that mailbox. SMTP AUTH still uses the mailbox login; envelope + header From use the allowed alias.

## Connect from Grok

1. Add a custom MCP connector with URL `https://mail-mcp.finestar.hr/mcp`
2. Set authorization / Bearer header to the value of `MAIL_MCP_TOKEN` from `.env`
3. Confirm tools appear (8): read + write set above
4. After any `mail-mcp` rebuild, restart the connector

## Ops

Service: `mail-mcp` in [`compose.yaml`](/opt/stacks/mailserver/compose.yaml)

```bash
cd /opt/stacks/mailserver
docker compose up -d --build --no-deps mail-mcp
```

Use `--no-deps` so `mailserver` / `mailadmin` are not recreated.

Env vars (see [`.env.example`](/opt/stacks/mailserver/.env.example)):

- `MAIL_MCP_HOST`
- `MAIL_MCP_MAILBOX_EMAIL`
- `MAIL_MCP_MAILBOX_PASSWORD`
- `MAIL_MCP_TOKEN`
- `MAIL_MCP_INDEX_USERNAME` (optional; set when multiple index owners exist)
- `MAIL_MCP_ALLOWED_HOSTS` / `MAIL_MCP_ALLOWED_ORIGINS` (DNS-rebinding allowlists)

DNS: `mail-mcp.finestar.hr` is an A record to the server IP, **DNS-only** (not Cloudflare-proxied), so streaming is not buffered.

## Limits

- One mailbox per instance
- No draft tool, no BCC in v1
- Flag whitelist: Seen / Flagged / Answered
- Attachments via base64 only (no remote URL fetch)
- Index has no message bodies and only covers `INBOX` + Sent
- Index freshness depends on `mailindex-sync`, which skips accounts without `MailboxTokenCredential`
