import json

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from mail_integration.exceptions import MailAuthError, MailConnectionError, MailIntegrationError, MailProtocolError, MailSendError, MailTimeoutError
from mail_integration.mailbox_service import MailboxService
from mail_integration.schemas import MailboxCredentials, SendMailRequest

from .models import ApplyLog, DeviceRegistration, SenderBlocklistRule
from .services import apply_blocklist, send_mail_notification


@staff_member_required
def dashboard(request):
    context = {
        "rules": SenderBlocklistRule.objects.all()[:10],
        "last_apply": ApplyLog.objects.first(),
        "mailadmin_host": request.get_host(),
    }
    return render(request, "mailops/dashboard.html", context)


@staff_member_required
@require_POST
def apply_blocklist_view(request):
    try:
        apply_blocklist()
    except Exception as exc:
        ApplyLog.objects.create(status=ApplyLog.STATUS_ERROR, message=str(exc), applied_by=request.user)
        messages.error(request, f"Apply failed: {exc}")
        return redirect("mailops:dashboard")

    ApplyLog.objects.create(
        status=ApplyLog.STATUS_SUCCESS,
        message="Sender blocklist rendered and Postfix reloaded.",
        applied_by=request.user,
    )
    messages.success(request, "Rules applied to Postfix.")
    return redirect("mailops:dashboard")


def _json_body(request):
    try:
        return json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _secret_matches(request, header_name, expected_secret):
    if not expected_secret:
        return False
    return request.headers.get(header_name) == expected_secret


def _summary_payload(summary):
    return {
        "uid": summary.uid,
        "folder": summary.folder,
        "subject": summary.subject,
        "sender": summary.sender,
        "to": list(summary.to),
        "cc": list(summary.cc),
        "date": summary.date.isoformat() if summary.date else None,
        "messageId": summary.message_id,
        "flags": list(summary.flags),
        "size": summary.size,
    }


def _detail_payload(detail):
    payload = _summary_payload(detail)
    payload.update(
        {
            "textBody": detail.text_body,
            "htmlBody": detail.html_body,
            "attachments": [
                {
                    "filename": attachment.filename,
                    "contentType": attachment.content_type,
                    "size": attachment.size,
                    "disposition": attachment.disposition,
                }
                for attachment in detail.attachments
            ],
        }
    )
    return payload


def _credentials_from_data(data):
    account_email = (data.get("accountEmail") or data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    if not account_email or not password:
        return None, None, JsonResponse({"error": "account_email_and_password_required"}, status=400)
    return account_email, MailboxCredentials(email=account_email, password=password), None


def _string_tuple(data, key):
    value = data.get(key)
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list):
        cleaned = tuple(str(item).strip() for item in value if str(item).strip())
        return cleaned
    return None


def _mail_error_response(exc):
    error_map = {
        MailAuthError: ("mail_auth_failed", 401),
        MailTimeoutError: ("mail_timeout", 504),
        MailConnectionError: ("mail_connection_failed", 502),
        MailProtocolError: ("mail_protocol_failed", 502),
        MailSendError: ("mail_send_failed", 502),
    }
    for error_type, (code, status) in error_map.items():
        if isinstance(exc, error_type):
            return JsonResponse({"error": code, "detail": str(exc)}, status=status)
    return JsonResponse({"error": "mail_integration_failed", "detail": str(exc)}, status=502)


@staff_member_required
@require_POST
def mailbox_summaries_view(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "invalid_json"}, status=400)

    account_email, credentials, error = _credentials_from_data(data)
    if error:
        return error

    folder = (data.get("folder") or "INBOX").strip() or "INBOX"
    try:
        limit = int(data.get("limit", 50))
    except (TypeError, ValueError):
        return JsonResponse({"error": "invalid_limit"}, status=400)
    if limit < 1 or limit > 200:
        return JsonResponse({"error": "invalid_limit"}, status=400)

    try:
        summaries = MailboxService().list_message_summaries(
            credentials,
            folder=folder,
            limit=limit,
        )
    except MailIntegrationError as exc:
        return _mail_error_response(exc)

    return JsonResponse(
        {
            "accountEmail": account_email,
            "folder": folder,
            "messages": [_summary_payload(summary) for summary in summaries],
        }
    )


@staff_member_required
@require_POST
def mailbox_detail_view(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "invalid_json"}, status=400)

    account_email, credentials, error = _credentials_from_data(data)
    if error:
        return error

    folder = (data.get("folder") or "INBOX").strip() or "INBOX"
    uid = str(data.get("uid") or "").strip()
    if not uid:
        return JsonResponse({"error": "uid_required"}, status=400)

    try:
        detail = MailboxService().get_message_detail(credentials, folder=folder, uid=uid)
    except MailIntegrationError as exc:
        return _mail_error_response(exc)

    return JsonResponse({"accountEmail": account_email, "folder": folder, "message": _detail_payload(detail)})


@staff_member_required
@require_POST
def mailbox_send_view(request):
    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "invalid_json"}, status=400)

    account_email, credentials, error = _credentials_from_data(data)
    if error:
        return error

    to = _string_tuple(data, "to")
    cc = _string_tuple(data, "cc")
    bcc = _string_tuple(data, "bcc")
    if to is None or cc is None or bcc is None:
        return JsonResponse({"error": "invalid_recipients"}, status=400)
    if not to:
        return JsonResponse({"error": "to_required"}, status=400)

    subject = str(data.get("subject") or "").strip()
    text_body = str(data.get("textBody") or data.get("text_body") or "")
    html_body = str(data.get("htmlBody") or data.get("html_body") or "")
    reply_to = str(data.get("replyTo") or data.get("reply_to") or "").strip() or None
    if not subject:
        return JsonResponse({"error": "subject_required"}, status=400)
    if not text_body and not html_body:
        return JsonResponse({"error": "body_required"}, status=400)

    try:
        message_id = MailboxService().send_mail(
            credentials,
            SendMailRequest(
                to=to,
                cc=cc,
                bcc=bcc,
                reply_to=reply_to,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            ),
        )
    except MailIntegrationError as exc:
        return _mail_error_response(exc)

    return JsonResponse({"accountEmail": account_email, "status": "sent", "messageId": message_id})


@csrf_exempt
@require_POST
def register_device_view(request):
    if not _secret_matches(request, "X-Device-Registration-Secret", settings.DEVICE_REGISTRATION_SECRET):
        return JsonResponse({"error": "unauthorized"}, status=401)

    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "invalid_json"}, status=400)

    account_email = (data.get("accountId") or data.get("email") or "").strip().lower()
    fcm_token = (data.get("fcmToken") or "").strip()
    if not account_email or not fcm_token:
        return JsonResponse({"error": "account_email_and_fcm_token_required"}, status=400)

    device, created = DeviceRegistration.objects.update_or_create(
        fcm_token=fcm_token,
        defaults={
            "account_email": account_email,
            "platform": (data.get("platform") or DeviceRegistration.PLATFORM_UNKNOWN).strip().lower(),
            "app_version": (data.get("appVersion") or "").strip(),
            "enabled": True,
            "last_seen_at": timezone.now(),
        },
    )
    return JsonResponse({"status": "ok", "created": created, "id": device.id})


@csrf_exempt
@require_POST
def new_mail_view(request):
    if not _secret_matches(request, "X-Mail-Hook-Secret", settings.MAIL_NOTIFY_HOOK_SECRET):
        return JsonResponse({"error": "unauthorized"}, status=401)

    data = _json_body(request)
    if data is None:
        return JsonResponse({"error": "invalid_json"}, status=400)

    account_email = (data.get("accountEmail") or "").strip().lower()
    if not account_email:
        return JsonResponse({"error": "account_email_required"}, status=400)

    event = {
        "accountEmail": account_email,
        "sender": (data.get("sender") or "").strip()[:255],
        "subject": (data.get("subject") or "").strip()[:255],
        "receivedAt": (data.get("receivedAt") or timezone.now().isoformat()).strip(),
        "folder": (data.get("folder") or "").strip(),
        "uid": (data.get("uid") or "").strip(),
        "messageId": (data.get("messageId") or "").strip(),
    }
    try:
        result = send_mail_notification(event)
    except Exception as exc:
        return JsonResponse({"error": "notification_failed", "detail": str(exc)}, status=502)
    return JsonResponse(result)
