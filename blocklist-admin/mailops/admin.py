from django.contrib import admin

from .forms import SenderBlocklistRuleForm
from .models import ApplyLog, SenderBlocklistRule


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
