import os
from dataclasses import dataclass


def _required(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"{name} must be set")
    return value


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


@dataclass(frozen=True)
class MailMcpSettings:
    mailbox_email: str
    mailbox_password: str
    bearer_token: str
    index_username: str
    public_host: str
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]
    max_limit: int = 50
    max_body_chars: int = 20000
    stale_after_seconds: int = 1200


def load_settings() -> MailMcpSettings:
    stale_raw = (os.environ.get("MAIL_MCP_STALE_AFTER_SECONDS") or "").strip()
    if stale_raw:
        stale_after_seconds = int(stale_raw)
    else:
        sync_interval = int(os.environ.get("MAIL_INDEX_SYNC_STALE_AFTER_SECONDS", "600"))
        stale_after_seconds = max(600, sync_interval * 2)

    public_host = (os.environ.get("MAIL_MCP_HOST") or "mail-mcp.example.com").strip()
    default_hosts = (
        public_host,
        f"{public_host}:*",
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "mail-mcp",
        "mail-mcp:*",
    )
    default_origins = (
        f"https://{public_host}",
        "http://127.0.0.1:*",
        "http://localhost:*",
        "https://grok.com",
        "https://www.grok.com",
        "https://x.com",
        "https://www.x.com",
    )
    allowed_hosts = _csv("MAIL_MCP_ALLOWED_HOSTS", ",".join(default_hosts))
    allowed_origins = _csv("MAIL_MCP_ALLOWED_ORIGINS", ",".join(default_origins))
    return MailMcpSettings(
        mailbox_email=_required("MAIL_MCP_MAILBOX_EMAIL").lower(),
        mailbox_password=_required("MAIL_MCP_MAILBOX_PASSWORD"),
        bearer_token=_required("MAIL_MCP_TOKEN"),
        index_username=(os.environ.get("MAIL_MCP_INDEX_USERNAME") or "").strip(),
        public_host=public_host,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        max_limit=max(1, int(os.environ.get("MAIL_MCP_MAX_LIMIT", "50"))),
        max_body_chars=max(1000, int(os.environ.get("MAIL_MCP_MAX_BODY_CHARS", "20000"))),
        stale_after_seconds=max(60, stale_after_seconds),
    )
