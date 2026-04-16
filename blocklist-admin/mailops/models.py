import re

from django.core.exceptions import ValidationError
from django.db import models


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
