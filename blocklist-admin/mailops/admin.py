from django.contrib import admin

from .forms import SenderBlocklistRuleForm
from .models import ApplyLog, DeviceRegistration, PushNotificationLog, SenderBlocklistRule


@admin.register(SenderBlocklistRule)
class SenderBlocklistRuleAdmin(admin.ModelAdmin):
    form = SenderBlocklistRuleForm
    list_display = ("kind", "value", "enabled", "updated_at")
    list_filter = ("kind", "enabled")
    search_fields = ("value", "note")
    ordering = ("-enabled", "kind", "value")


@admin.register(ApplyLog)
class ApplyLogAdmin(admin.ModelAdmin):
    list_display = ("status", "applied_by", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("message", "applied_by__username")
    readonly_fields = ("status", "message", "applied_by", "created_at")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(DeviceRegistration)
class DeviceRegistrationAdmin(admin.ModelAdmin):
    list_display = ("account_email", "platform", "enabled", "last_seen_at", "updated_at")
    list_filter = ("platform", "enabled")
    search_fields = ("account_email", "fcm_token", "app_version")
    readonly_fields = ("created_at", "updated_at")


@admin.register(PushNotificationLog)
class PushNotificationLogAdmin(admin.ModelAdmin):
    list_display = ("account_email", "status", "device_count", "success_count", "failure_count", "created_at")
    list_filter = ("status", "created_at")
    search_fields = ("account_email", "sender", "subject", "message_id", "error")
    readonly_fields = (
        "account_email",
        "sender",
        "subject",
        "message_id",
        "status",
        "device_count",
        "success_count",
        "failure_count",
        "error",
        "created_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
