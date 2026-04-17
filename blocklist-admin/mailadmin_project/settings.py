import os
from pathlib import Path
from urllib.parse import urlparse


BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "change-me-too")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if host.strip()]
CSRF_TRUSTED_ORIGINS = [
    origin.strip() for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()
]

DATABASE_URL = os.environ["DATABASE_URL"]
db_url = urlparse(DATABASE_URL)


def env_bool(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "drf_spectacular",
    "mailops",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "mailadmin_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    }
]

WSGI_APPLICATION = "mailadmin_project.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": db_url.path.lstrip("/"),
        "USER": db_url.username,
        "PASSWORD": db_url.password,
        "HOST": db_url.hostname,
        "PORT": db_url.port or 5432,
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("TZ", "Europe/Berlin")
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/admin/login/"
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

MAILADMIN_HOST = os.environ.get("MAILADMIN_HOST", "mailadmin.example.com")
BLOCKLIST_CONFIG_PATH = Path(os.environ.get("BLOCKLIST_CONFIG_PATH", "/app/shared-config/postfix-sender-blocklist"))
MAILSERVER_CONTAINER_NAME = os.environ.get("MAILSERVER_CONTAINER_NAME", "mailserver")
BLOCKLIST_REJECT_MESSAGE = os.environ.get("BLOCKLIST_REJECT_MESSAGE", "Blocked by local policy")
MAIL_NOTIFY_HOOK_SECRET = os.environ.get("MAIL_NOTIFY_HOOK_SECRET", "")
DEVICE_REGISTRATION_SECRET = os.environ.get("DEVICE_REGISTRATION_SECRET", "")
MAILBOX_CREDENTIAL_ENCRYPTION_KEY = os.environ.get("MAILBOX_CREDENTIAL_ENCRYPTION_KEY", "")
MAIL_IMAP_HOST = os.environ.get("MAIL_IMAP_HOST", os.environ.get("MAIL_HOSTNAME", "mail.example.com"))
MAIL_IMAP_PORT = int(os.environ.get("MAIL_IMAP_PORT", "993"))
MAIL_IMAP_USE_SSL = env_bool("MAIL_IMAP_USE_SSL", True)
MAIL_SMTP_HOST = os.environ.get("MAIL_SMTP_HOST", os.environ.get("MAIL_HOSTNAME", "mail.example.com"))
MAIL_SMTP_PORT = int(os.environ.get("MAIL_SMTP_PORT", "587"))
MAIL_SMTP_USE_STARTTLS = env_bool("MAIL_SMTP_USE_STARTTLS", True)
MAIL_CLIENT_TIMEOUT_SECONDS = int(os.environ.get("MAIL_CLIENT_TIMEOUT_SECONDS", "15"))
MAIL_CONVERSATION_SCAN_LIMIT = int(os.environ.get("MAIL_CONVERSATION_SCAN_LIMIT", "1000"))
MAIL_INDEX_RECONCILE_DELETIONS = env_bool("MAIL_INDEX_RECONCILE_DELETIONS", False)
MAIL_INDEX_SYNC_INTERVAL_SECONDS = int(os.environ.get("MAIL_INDEX_SYNC_INTERVAL_SECONDS", "600"))
MAIL_INDEX_SYNC_STALE_AFTER_SECONDS = int(os.environ.get("MAIL_INDEX_SYNC_STALE_AFTER_SECONDS", "600"))
MAIL_INDEX_SYNC_FAILURE_COOLDOWN_SECONDS = int(os.environ.get("MAIL_INDEX_SYNC_FAILURE_COOLDOWN_SECONDS", "1800"))
MAIL_INDEX_SYNC_MAX_ACCOUNTS = int(os.environ.get("MAIL_INDEX_SYNC_MAX_ACCOUNTS", "50"))
MAIL_INDEX_SYNC_LIMIT = int(os.environ.get("MAIL_INDEX_SYNC_LIMIT", "500"))

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.TokenAuthentication",
    ],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "mailops.api_exceptions.mailbox_api_exception_handler",
    "UNAUTHENTICATED_USER": None,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Finestar Mailadmin API",
    "DESCRIPTION": "Backend mail API for Finestar Android clients.",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}
