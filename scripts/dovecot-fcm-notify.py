#!/usr/bin/env python3
import json
import os
import sys
import urllib.error
import urllib.request
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime


HOOK_URL = os.environ.get("FCM_HOOK_URL", "http://mailadmin:8000/api/mail/new/")
SECRET_PATH = os.environ.get("FCM_HOOK_SECRET_FILE", "/run/secrets/fcm-hook-secret")
TIMEOUT_SECONDS = float(os.environ.get("FCM_HOOK_TIMEOUT", "3"))


def header_value(message, name):
    value = message.get(name)
    return str(value).strip() if value else ""


def sender_title(message):
    from_header = header_value(message, "From")
    addresses = getaddresses([from_header])
    if addresses:
        display_name, email_address = addresses[0]
        return display_name or email_address or from_header
    return from_header


def received_at(message):
    date_header = header_value(message, "Date")
    if not date_header:
        return ""
    try:
        return parsedate_to_datetime(date_header).isoformat()
    except (TypeError, ValueError):
        return date_header


def read_secret():
    with open(SECRET_PATH, "r", encoding="utf-8") as secret_file:
        return secret_file.read().strip()


def main():
    account_email = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
    raw_message = sys.stdin.buffer.read()
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    if not account_email:
        account_email = header_value(message, "Delivered-To").lower()
    if not account_email:
        account_email = header_value(message, "X-Original-To").lower()
    if not account_email:
        return 0

    payload = {
        "accountEmail": account_email,
        "sender": sender_title(message),
        "subject": header_value(message, "Subject"),
        "receivedAt": received_at(message),
        "folder": "INBOX",
        "messageId": header_value(message, "Message-ID"),
    }
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        HOOK_URL,
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Mail-Hook-Secret": read_secret(),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):
            pass
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        print(f"fcm-notify hook failed: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
