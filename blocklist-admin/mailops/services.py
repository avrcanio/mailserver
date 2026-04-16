import docker
from django.conf import settings
from firebase_admin import credentials, get_app, initialize_app, messaging

from .models import DeviceRegistration, PushNotificationLog, SenderBlocklistRule


def render_postfix_map():
    lines = [
        "# Managed by mailadmin.",
        "# Rendered from Django mailadmin and reloaded into Postfix.",
    ]
    for rule in SenderBlocklistRule.objects.filter(enabled=True).order_by("kind", "value"):
        lines.append(f"{rule.value} REJECT {settings.BLOCKLIST_REJECT_MESSAGE}")
    settings.BLOCKLIST_CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reload_mailserver():
    client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    container = client.containers.get(settings.MAILSERVER_CONTAINER_NAME)
    result = container.exec_run(["postfix", "reload"])
    if result.exit_code != 0:
        raise RuntimeError(result.output.decode("utf-8", errors="replace"))


def apply_blocklist():
    settings.BLOCKLIST_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    render_postfix_map()
    reload_mailserver()


def get_firebase_app():
    try:
        return get_app()
    except ValueError:
        return initialize_app(credentials.ApplicationDefault())


def _clean_data(value):
    if value is None:
        return ""
    return str(value)


def send_mail_notification(event):
    account_email = event["accountEmail"].strip().lower()
    devices = list(DeviceRegistration.objects.filter(account_email=account_email, enabled=True))
    if not devices:
        PushNotificationLog.objects.create(
            account_email=account_email,
            sender=event.get("sender", ""),
            subject=event.get("subject", ""),
            message_id=event.get("messageId", ""),
            status=PushNotificationLog.STATUS_SKIPPED,
            device_count=0,
        )
        return {"status": "skipped", "deviceCount": 0, "successCount": 0, "failureCount": 0}

    data = {
        "accountEmail": account_email,
        "folder": _clean_data(event.get("folder")),
        "uid": _clean_data(event.get("uid")),
        "messageId": _clean_data(event.get("messageId")),
        "receivedAt": _clean_data(event.get("receivedAt")),
    }
    multicast = messaging.MulticastMessage(
        tokens=[device.fcm_token for device in devices],
        notification=messaging.Notification(
            title=event.get("sender") or "New mail",
            body=event.get("subject") or "(No subject)",
        ),
        data=data,
    )

    try:
        response = messaging.send_each_for_multicast(multicast, app=get_firebase_app())
    except Exception as exc:
        PushNotificationLog.objects.create(
            account_email=account_email,
            sender=event.get("sender", ""),
            subject=event.get("subject", ""),
            message_id=event.get("messageId", ""),
            status=PushNotificationLog.STATUS_ERROR,
            device_count=len(devices),
            error=str(exc),
        )
        raise

    invalid_tokens = []
    for index, result in enumerate(response.responses):
        if result.success:
            continue
        code = getattr(result.exception, "code", "")
        if code in {"UNREGISTERED", "INVALID_ARGUMENT", "registration-token-not-registered"}:
            invalid_tokens.append(devices[index].fcm_token)
    if invalid_tokens:
        DeviceRegistration.objects.filter(fcm_token__in=invalid_tokens).update(enabled=False)

    status = PushNotificationLog.STATUS_SUCCESS if response.failure_count == 0 else PushNotificationLog.STATUS_PARTIAL
    PushNotificationLog.objects.create(
        account_email=account_email,
        sender=event.get("sender", ""),
        subject=event.get("subject", ""),
        message_id=event.get("messageId", ""),
        status=status,
        device_count=len(devices),
        success_count=response.success_count,
        failure_count=response.failure_count,
    )
    return {
        "status": status,
        "deviceCount": len(devices),
        "successCount": response.success_count,
        "failureCount": response.failure_count,
    }
