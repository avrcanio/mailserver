import json

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

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
