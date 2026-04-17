import re

from django.core.exceptions import ValidationError
from django.conf import settings
from django.db import models

from .credential_crypto import decrypt_mailbox_password, encrypt_mailbox_password


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


class SenderBlocklistRule(models.Model):
    KIND_SENDER_EMAIL = "sender_email"
    KIND_SENDER_DOMAIN = "sender_domain"
    KIND_CHOICES = [
        (KIND_SENDER_EMAIL, "Sender email"),
        (KIND_SENDER_DOMAIN, "Sender domain"),
    ]

    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    value = models.CharField(max_length=255)
    enabled = models.BooleanField(default=True)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "sender_blocklist_rules"
        ordering = ["-enabled", "kind", "value"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "value"], name="sender_blocklist_rules_kind_value_uniq"),
        ]
        verbose_name = "Sender blocklist rule"
        verbose_name_plural = "Sender blocklist rules"

    def __str__(self):
        return f"{self.kind}: {self.value}"

    @staticmethod
    def normalize_value(kind, value):
        normalized = value.strip().lower()
        if kind == SenderBlocklistRule.KIND_SENDER_EMAIL:
            if not EMAIL_RE.match(normalized):
                raise ValidationError("Sender email must be a valid e-mail address.")
            return normalized
        if kind == SenderBlocklistRule.KIND_SENDER_DOMAIN:
            if not DOMAIN_RE.match(normalized):
                raise ValidationError("Sender domain must be a valid domain name.")
            return normalized
        raise ValidationError("Unsupported rule kind.")

    def clean(self):
        self.value = self.normalize_value(self.kind, self.value)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ApplyLog(models.Model):
    STATUS_SUCCESS = "success"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [
        (STATUS_SUCCESS, "Success"),
        (STATUS_ERROR, "Error"),
    ]

    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    message = models.TextField(blank=True, default="")
    applied_by = models.ForeignKey(
        "auth.User",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mailadmin_apply_logs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Apply log"
        verbose_name_plural = "Apply logs"

    def __str__(self):
        return f"{self.status} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


class DeviceRegistration(models.Model):
    PLATFORM_ANDROID = "android"
    PLATFORM_IOS = "ios"
    PLATFORM_WEB = "web"
    PLATFORM_UNKNOWN = "unknown"
    PLATFORM_CHOICES = [
        (PLATFORM_ANDROID, "Android"),
        (PLATFORM_IOS, "iOS"),
        (PLATFORM_WEB, "Web"),
        (PLATFORM_UNKNOWN, "Unknown"),
    ]

    account_email = models.EmailField(db_index=True)
    fcm_token = models.TextField()
    platform = models.CharField(max_length=16, choices=PLATFORM_CHOICES, default=PLATFORM_UNKNOWN)
    app_version = models.CharField(max_length=64, blank=True, default="")
    enabled = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["account_email", "-last_seen_at"]
        constraints = [
            models.UniqueConstraint(fields=["account_email", "fcm_token"], name="uniq_device_registration_account_fcm_token"),
        ]
        verbose_name = "Device registration"
        verbose_name_plural = "Device registrations"

    def clean(self):
        self.account_email = self.account_email.strip().lower()
        self.platform = (self.platform or self.PLATFORM_UNKNOWN).strip().lower()
        self.app_version = (self.app_version or "").strip()

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.account_email} ({self.platform})"


class PushNotificationLog(models.Model):
    STATUS_SUCCESS = "success"
    STATUS_PARTIAL = "partial"
    STATUS_SKIPPED = "skipped"
    STATUS_ERROR = "error"
    STATUS_CHOICES = [
        (STATUS_SUCCESS, "Success"),
        (STATUS_PARTIAL, "Partial"),
        (STATUS_SKIPPED, "Skipped"),
        (STATUS_ERROR, "Error"),
    ]

    account_email = models.EmailField(db_index=True)
    sender = models.CharField(max_length=255, blank=True, default="")
    subject = models.CharField(max_length=255, blank=True, default="")
    message_id = models.CharField(max_length=512, blank=True, default="")
    status = models.CharField(max_length=16, choices=STATUS_CHOICES)
    device_count = models.PositiveIntegerField(default=0)
    success_count = models.PositiveIntegerField(default=0)
    failure_count = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Push notification log"
        verbose_name_plural = "Push notification logs"

    def __str__(self):
        return f"{self.account_email}: {self.status} @ {self.created_at:%Y-%m-%d %H:%M:%S}"


class MailboxTokenCredential(models.Model):
    token = models.OneToOneField(
        "authtoken.Token",
        on_delete=models.CASCADE,
        related_name="mailbox_credential",
    )
    mailbox_email = models.EmailField(unique=True, db_index=True)
    mailbox_password = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["mailbox_email"]
        verbose_name = "Mailbox token credential"
        verbose_name_plural = "Mailbox token credentials"

    def clean(self):
        self.mailbox_email = self.mailbox_email.strip().lower()

    def set_mailbox_password(self, plaintext):
        self.mailbox_password = encrypt_mailbox_password(plaintext)

    def get_mailbox_password(self):
        return decrypt_mailbox_password(self.mailbox_password)

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.mailbox_email


class MailAccountIndex(models.Model):
    STATUS_EMPTY = "empty"
    STATUS_SYNCING = "syncing"
    STATUS_READY = "ready"
    STATUS_PARTIAL = "partial"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = [
        (STATUS_EMPTY, "Empty"),
        (STATUS_SYNCING, "Syncing"),
        (STATUS_READY, "Ready"),
        (STATUS_PARTIAL, "Partial"),
        (STATUS_FAILED, "Failed"),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mail_account_indexes")
    account_email = models.EmailField(db_index=True)
    imap_host = models.CharField(max_length=255, blank=True, default="")
    sent_folder = models.CharField(max_length=255, blank=True, default="")
    index_status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=STATUS_EMPTY, db_index=True)
    last_indexed_at = models.DateTimeField(null=True, blank=True)
    last_sync_started_at = models.DateTimeField(null=True, blank=True)
    last_sync_finished_at = models.DateTimeField(null=True, blank=True)
    last_sync_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["account_email"]
        constraints = [
            models.UniqueConstraint(fields=["user", "account_email"], name="uniq_mail_account_index_user_email"),
        ]
        verbose_name = "Mail account index"
        verbose_name_plural = "Mail account indexes"

    def clean(self):
        self.account_email = self.account_email.strip().lower()
        self.imap_host = (self.imap_host or "").strip()
        self.sent_folder = (self.sent_folder or "").strip()

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.account_email


class MailConversationIndex(models.Model):
    account = models.ForeignKey(MailAccountIndex, on_delete=models.CASCADE, related_name="conversations")
    conversation_id = models.CharField(max_length=128, db_index=True)
    thread_key = models.CharField(max_length=512, db_index=True)
    normalized_subject = models.CharField(max_length=998, blank=True, default="")
    latest_message_at = models.DateTimeField(null=True, blank=True, db_index=True)
    message_count = models.PositiveIntegerField(default=0)
    has_unread = models.BooleanField(default=False)
    has_attachments = models.BooleanField(default=False)
    has_visible_attachments = models.BooleanField(default=False)
    participants_json = models.JSONField(default=list, blank=True)
    folders_json = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-latest_message_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["account", "conversation_id"], name="uniq_mail_conversation_index_account_conversation"),
        ]
        indexes = [
            models.Index(fields=["account", "latest_message_at"], name="mailconv_account_latest_idx"),
            models.Index(fields=["account", "thread_key"], name="mailconv_account_thread_idx"),
        ]
        verbose_name = "Mail conversation index"
        verbose_name_plural = "Mail conversation indexes"

    def __str__(self):
        return f"{self.account_email}: {self.conversation_id}"

    @property
    def account_email(self):
        return self.account.account_email


class MailMessageIndex(models.Model):
    DIRECTION_INBOUND = "inbound"
    DIRECTION_OUTBOUND = "outbound"
    DIRECTION_CHOICES = [
        (DIRECTION_INBOUND, "Inbound"),
        (DIRECTION_OUTBOUND, "Outbound"),
    ]

    account = models.ForeignKey(MailAccountIndex, on_delete=models.CASCADE, related_name="messages")
    conversation = models.ForeignKey(
        MailConversationIndex,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
    )
    folder = models.CharField(max_length=255, db_index=True)
    uid = models.PositiveBigIntegerField()
    direction = models.CharField(max_length=16, choices=DIRECTION_CHOICES)
    message_id = models.CharField(max_length=998, blank=True, default="", db_index=True)
    in_reply_to = models.CharField(max_length=998, blank=True, default="")
    references_raw = models.TextField(blank=True, default="")
    thread_key = models.CharField(max_length=512, db_index=True)
    normalized_subject = models.CharField(max_length=998, blank=True, default="")
    subject = models.CharField(max_length=998, blank=True, default="")
    sender_name = models.CharField(max_length=255, blank=True, default="")
    sender_email = models.EmailField(blank=True, default="")
    sender_raw = models.CharField(max_length=998, blank=True, default="")
    to_json = models.JSONField(default=list, blank=True)
    cc_json = models.JSONField(default=list, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    flags_json = models.JSONField(default=list, blank=True)
    is_read = models.BooleanField(default=False, db_index=True)
    size = models.PositiveBigIntegerField(default=0)
    has_attachments = models.BooleanField(default=False)
    has_visible_attachments = models.BooleanField(default=False)
    dedupe_key = models.CharField(max_length=1024, db_index=True)
    raw_headers_json = models.JSONField(default=dict, blank=True)
    indexed_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-sent_at", "-uid"]
        constraints = [
            models.UniqueConstraint(fields=["account", "folder", "uid"], name="uniq_mail_message_index_account_folder_uid"),
        ]
        indexes = [
            models.Index(fields=["account", "message_id"], name="mailmsg_account_msgid_idx"),
            models.Index(fields=["account", "thread_key"], name="mailmsg_account_thread_idx"),
            models.Index(fields=["account", "folder", "sent_at"], name="mailmsg_acc_folder_date_idx"),
            models.Index(fields=["account", "direction", "sent_at"], name="mailmsg_account_dir_date_idx"),
            models.Index(fields=["account", "dedupe_key"], name="mailmsg_account_dedupe_idx"),
        ]
        verbose_name = "Mail message index"
        verbose_name_plural = "Mail message indexes"

    def __str__(self):
        return f"{self.account.account_email} {self.folder}/{self.uid}"


class MailFolderIndexState(models.Model):
    account = models.ForeignKey(MailAccountIndex, on_delete=models.CASCADE, related_name="folder_states")
    folder = models.CharField(max_length=255)
    uidvalidity = models.CharField(max_length=64, blank=True, default="")
    highest_indexed_uid = models.PositiveBigIntegerField(default=0)
    last_synced_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["account", "folder"]
        constraints = [
            models.UniqueConstraint(fields=["account", "folder"], name="uniq_mail_folder_index_state_account_folder"),
        ]
        indexes = [
            models.Index(fields=["account", "folder"], name="mailfolder_account_folder_idx"),
        ]
        verbose_name = "Mail folder index state"
        verbose_name_plural = "Mail folder index states"

    def __str__(self):
        return f"{self.account.account_email} {self.folder}"
