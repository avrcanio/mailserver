from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import migrations
from cryptography.fernet import Fernet


ENCRYPTED_VALUE_PREFIX = "fernet:v1:"


def _fernet():
    key = getattr(settings, "MAILBOX_CREDENTIAL_ENCRYPTION_KEY", "")
    if not key:
        raise ImproperlyConfigured("MAILBOX_CREDENTIAL_ENCRYPTION_KEY is required for credential encryption migration.")
    try:
        return Fernet(key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured("MAILBOX_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def encrypt_legacy_mailbox_passwords(apps, schema_editor):
    fernet = _fernet()
    MailboxTokenCredential = apps.get_model("mailops", "MailboxTokenCredential")
    for credential in MailboxTokenCredential.objects.all().iterator():
        if credential.mailbox_password.startswith(ENCRYPTED_VALUE_PREFIX):
            continue
        encrypted = fernet.encrypt(credential.mailbox_password.encode("utf-8")).decode("ascii")
        credential.mailbox_password = f"{ENCRYPTED_VALUE_PREFIX}{encrypted}"
        credential.save(update_fields=["mailbox_password"])


class Migration(migrations.Migration):
    dependencies = [
        ("mailops", "0003_mailbox_token_credential"),
    ]

    operations = [
        migrations.RunPython(encrypt_legacy_mailbox_passwords, migrations.RunPython.noop),
    ]
