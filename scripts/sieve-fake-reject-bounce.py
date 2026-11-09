#!/usr/bin/env python3
"""Sieve pipe: keep delivering, but send a fake 'mailbox not in use' bounce to the sender.

Gmail rejects unauthenticated MAILER-DAEMON/null-envelope bounces (550 5.7.26),
so we send from a real DKIM-signed dalekopro.hr address instead.
"""

from __future__ import annotations

import subprocess
import sys
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr

LOCAL_RECIPIENT = "avrcan@dalekopro.hr"
# Must be a domain we DKIM-sign + SPF-pass (not null <> / MAILER-DAEMON@mail...).
BOUNCE_ENV_FROM = "postmaster@dalekopro.hr"
BOUNCE_FROM = "Mail Delivery System <postmaster@dalekopro.hr>"


def extract_address(header_value: str) -> str:
    if not header_value:
        return ""
    _name, addr = parseaddr(header_value)
    if addr:
        return addr.strip().lower()
    addrs = getaddresses([header_value])
    if addrs and addrs[0][1]:
        return addrs[0][1].strip().lower()
    return ""


def should_skip(bounce_to: str) -> bool:
    if not bounce_to or bounce_to == "<>":
        return True
    lowered = bounce_to.lower()
    if "mailer-daemon" in lowered or lowered.startswith("postmaster@"):
        return True
    if lowered.startswith("srs0=") or lowered.startswith("srs1="):
        return True
    return False


def build_bounce(bounce_to: str, original_subject: str, message_id: str) -> bytes:
    del message_id  # do not echo Message-ID back (looks fake / leaks probe ids)
    subject = "Undelivered Mail Returned to Sender"
    if original_subject and "@local" not in original_subject.lower():
        subject = f"Undelivered Mail Returned to Sender: {original_subject}"

    body = f"""This is the mail system at host mail.dalekopro.hr.

I'm sorry to have to inform you that your message could not
be delivered to one or more recipients. It's attached below.

For further assistance, please send mail to postmaster.

If you do so, please include this problem report. You can
delete your own text from the attached returned message.

                   The mail system

<{LOCAL_RECIPIENT}>: host mail.dalekopro.hr[65.108.196.92] said: 550 5.1.1
    <{LOCAL_RECIPIENT}>: Recipient address rejected: User unknown (in
    reply to RCPT TO command)
"""
    headers = [
        f"From: {BOUNCE_FROM}",
        f"To: {bounce_to}",
        f"Subject: {subject}",
        "Auto-Submitted: auto-replied",
        "X-Auto-Response-Suppress: All",
        "MIME-Version: 1.0",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
    ]
    return "\n".join(headers).encode("utf-8")


def main() -> int:
    raw = sys.stdin.buffer.read()
    if not raw:
        return 0

    message = BytesParser(policy=policy.default).parsebytes(raw)

    # Prefer visible From (envelope may be SRS-rewritten at LMTP time).
    bounce_to = extract_address(message.get("From", ""))
    if not bounce_to:
        bounce_to = extract_address(message.get("Return-Path", ""))

    if should_skip(bounce_to):
        return 0

    payload = build_bounce(
        bounce_to=bounce_to,
        original_subject=str(message.get("Subject") or "").strip(),
        message_id="",
    )

    try:
        result = subprocess.run(
            ["/usr/sbin/sendmail", "-i", "-f", BOUNCE_ENV_FROM, "--", bounce_to],
            input=payload,
            check=False,
            timeout=30,
            capture_output=True,
        )
        if result.returncode != 0:
            err = (result.stderr or b"").decode("utf-8", errors="replace")
            print(f"fake-reject-bounce sendmail rc={result.returncode}: {err}", file=sys.stderr)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"fake-reject-bounce failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
