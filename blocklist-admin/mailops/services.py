import docker
from django.conf import settings
from firebase_admin import credentials, get_app, initialize_app, messaging

from .models import DeviceRegistration, PushNotificationLog, SenderBlocklistRule


class MailboxProvisioningError(RuntimeError):
    pass


class MailboxCleanupError(RuntimeError):
    pass


def sanitize_mailbox_command_output(value, password=""):
    output = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value or "")
    if password:
        output = output.replace(password, "[redacted-password]")
    return output.strip()


def _mailserver_container():
    client = docker.DockerClient(base_url="unix:///var/run/docker.sock")
    return client.containers.get(settings.MAILSERVER_CONTAINER_NAME)


def _exec_mailserver_setup(args, password=""):
    try:
        container = _mailserver_container()
        result = container.exec_run(["setup", *args])
    except Exception as exc:
        message = sanitize_mailbox_command_output(str(exc), password=password)
        raise MailboxProvisioningError(message or "Unable to execute mailserver setup command.") from exc
    output = sanitize_mailbox_command_output(result.output, password=password)
    return result.exit_code, output


def create_mailbox_account(email, password):
    normalized_email = email.strip().lower()
    exit_code, output = _exec_mailserver_setup(["email", "add", normalized_email, password], password=password)
    if exit_code != 0:
        raise MailboxProvisioningError(output or f"Mailbox provisioning failed for {normalized_email}.")
    return output


def delete_mailbox_account(email, password=""):
    normalized_email = email.strip().lower()
    try:
        exit_code, output = _exec_mailserver_setup(["email", "del", "-y", normalized_email], password=password)
    except MailboxProvisioningError as exc:
        raise MailboxCleanupError(str(exc)) from exc
    if exit_code != 0:
        raise MailboxCleanupError(output or f"Mailbox cleanup failed for {normalized_email}.")
    return output


def render_postfix_map():
    lines = [
        "# Managed by mailadmin.",
        "# Rendered from Django mailadmin and reloaded into Postfix.",
    ]
    for rule in SenderBlocklistRule.objects.filter(enabled=True).order_by("kind", "value"):
        lines.append(f"{rule.value} REJECT {settings.BLOCKLIST_REJECT_MESSAGE}")
    settings.BLOCKLIST_CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reload_mailserver():
    container = _mailserver_container()
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


def _is_unregistered_fcm_error(exc):
    code = str(getattr(exc, "code", "") or "").lower()
    message = str(exc).lower()
    return (
        code in {"unregistered", "invalid-argument", "invalid_argument", "registration-token-not-registered"}
        or "registration-token-not-registered" in message
        or "requested entity was not found" in message
    )


def send_mail_notification(event):
    account_email = event["accountEmail"].strip().lower()
    devices = list(DeviceRegistration.objects.filter(account_email=account_email, enabled=True).order_by("id"))
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
        if _is_unregistered_fcm_error(result.exception):
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
