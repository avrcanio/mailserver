import docker
from django.conf import settings

from .models import SenderBlocklistRule


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
