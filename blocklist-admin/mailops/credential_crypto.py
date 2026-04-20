from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from cryptography.fernet import Fernet, InvalidToken


ENCRYPTED_VALUE_PREFIX = "fernet:v1:"


class CredentialEncryptionError(ValueError):
    pass


def is_encrypted_credential_value(value):
    return isinstance(value, str) and value.startswith(ENCRYPTED_VALUE_PREFIX)


def is_encrypted_mailbox_password(value):
    return is_encrypted_credential_value(value)


def _fernet():
    key = getattr(settings, "MAILBOX_CREDENTIAL_ENCRYPTION_KEY", "")
    if not key:
        raise ImproperlyConfigured("MAILBOX_CREDENTIAL_ENCRYPTION_KEY is required.")
    try:
        return Fernet(key.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise ImproperlyConfigured("MAILBOX_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def encrypt_credential_value(plaintext):
    encrypted = _fernet().encrypt(str(plaintext).encode("utf-8")).decode("ascii")
    return f"{ENCRYPTED_VALUE_PREFIX}{encrypted}"


def decrypt_credential_value(value, label="Credential value"):
    if not is_encrypted_credential_value(value):
        raise CredentialEncryptionError(f"{label} is not encrypted.")
    token = value.removeprefix(ENCRYPTED_VALUE_PREFIX)
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError) as exc:
        raise CredentialEncryptionError(f"{label} could not be decrypted.") from exc


def encrypt_mailbox_password(plaintext):
    return encrypt_credential_value(plaintext)


def decrypt_mailbox_password(value):
    return decrypt_credential_value(value, label="Mailbox password")
