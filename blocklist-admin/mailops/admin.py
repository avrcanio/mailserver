import logging

from django.conf import settings
from django.contrib import admin, messages
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import HttpResponseRedirect

from .forms import SenderBlocklistRuleForm
from .models import (
    ApplyLog,
    DeviceRegistration,
    GmailImportAccount,
    GmailImportMessage,
    GmailImportRun,
    MailAccountIndex,
    MailConversationIndex,
    MailFolderIndexState,
    MailMessageIndex,
    PushNotificationLog,
    SenderBlocklistRule,
)
from .services import MailboxCleanupError, MailboxProvisioningError, create_mailbox_account, delete_mailbox_account


logger = logging.getLogger("mailops.admin")


def mailbox_auto_create_enabled():
    return bool(getattr(settings, "MAILBOX_AUTO_CREATE_FROM_USER_ADMIN", False))


def normalize_mailbox_email(email):
    return (email or "").strip().lower()


class MailboxUserCreationForm(AdminUserCreationForm):
    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("username", "email")

    def clean_email(self):
        email = normalize_mailbox_email(self.cleaned_data.get("email"))
        if mailbox_auto_create_enabled():
            if not email:
                raise ValidationError("Email is required when mailbox auto-create is enabled.")
            UserModel = get_user_model()
            if UserModel.objects.filter(email__iexact=email).exists():
                raise ValidationError("A user with this email already exists.")
        return email

    def clean(self):
        cleaned_data = super().clean()
        if mailbox_auto_create_enabled():
            password = cleaned_data.get("password1")
            if not password:
                raise ValidationError("Password is required when mailbox auto-create is enabled.")
        return cleaned_data


class MailboxUserChangeForm(UserChangeForm):
    def clean_email(self):
        email = normalize_mailbox_email(self.cleaned_data.get("email"))
        if mailbox_auto_create_enabled() and self.instance.pk and not self.instance.is_staff and not self.instance.is_superuser:
            original_email = normalize_mailbox_email(type(self.instance).objects.only("email").get(pk=self.instance.pk).email)
            if email != original_email:
                raise ValidationError("Email changes for mailbox-backed users are blocked in v1.")
        if email:
            UserModel = get_user_model()
            duplicate = UserModel.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists()
            if duplicate:
                raise ValidationError("A user with this email already exists.")
        return email


admin.site.unregister(User)


@admin.register(User)
class MailboxUserAdmin(DjangoUserAdmin):
    add_form = MailboxUserCreationForm
    form = MailboxUserChangeForm
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("username", "email", "usable_password", "password1", "password2"),
            },
        ),
    )

    def should_provision_mailbox(self, obj):
        if not mailbox_auto_create_enabled():
            return False
        if getattr(settings, "MAILBOX_AUTO_CREATE_SKIP_STAFF", True) and (obj.is_staff or obj.is_superuser):
            return False
        return True

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if change or not self.should_provision_mailbox(obj):
            return
        email = normalize_mailbox_email(obj.email)
        password = form.cleaned_data.get("password1")
        create_mailbox_account(email, password)
        request._mailadmin_created_mailbox_email = email

    def changeform_view(self, request, object_id=None, form_url="", extra_context=None):
        try:
            with transaction.atomic():
                return super().changeform_view(request, object_id, form_url, extra_context)
        except MailboxProvisioningError as exc:
            self.message_user(request, f"Mailbox provisioning failed: {exc}", level=messages.ERROR)
            return HttpResponseRedirect(request.path)
        except Exception:
            email = getattr(request, "_mailadmin_created_mailbox_email", "")
            if email:
                try:
                    delete_mailbox_account(email)
                except MailboxCleanupError as cleanup_exc:
                    logger.error("Mailbox cleanup failed after admin user create rollback for %s: %s", email, cleanup_exc)
                    self.message_user(
                        request,
                        f"User creation failed after mailbox provisioning, and cleanup failed for {email}: {cleanup_exc}",
                        level=messages.ERROR,
                    )
                    return HttpResponseRedirect(request.path)
                self.message_user(
                    request,
                    f"User creation failed after mailbox provisioning; mailbox cleanup was attempted for {email}.",
                    level=messages.ERROR,
                )
                return HttpResponseRedirect(request.path)
            raise


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


class GmailImportMessageInline(admin.TabularInline):
    model = GmailImportMessage
    extra = 0
    fields = ("gmail_message_id", "state", "append_status", "cleanup_status", "target_folder", "committed_at", "cleaned_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True
    max_num = 0

    def has_add_permission(self, request, obj=None):
        return False


class GmailImportRunInline(admin.TabularInline):
    model = GmailImportRun
    extra = 0
    fields = ("mode", "status", "scanned_count", "committed_count", "cleaned_count", "failed_count", "started_at", "finished_at")
    readonly_fields = fields
    can_delete = False
    show_change_link = True
    max_num = 0

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(GmailImportAccount)
class GmailImportAccountAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "gmail_email",
        "target_mailbox_email",
        "delete_after_import",
        "last_success_at",
        "historical_import_completed_at",
        "consecutive_failures",
        "updated_at",
    )
    list_filter = ("delete_after_import", "consecutive_failures")
    search_fields = ("user__username", "user__email", "gmail_email", "target_mailbox_email", "last_history_id", "last_error")
    readonly_fields = ("refresh_token_status", "created_at", "updated_at")
    exclude = ("refresh_token",)
    inlines = (GmailImportRunInline, GmailImportMessageInline)

    def refresh_token_status(self, obj):
        if not obj or not obj.refresh_token:
            return "Not configured"
        return "Configured"

    def has_add_permission(self, request):
        return False


@admin.register(GmailImportMessage)
class GmailImportMessageAdmin(admin.ModelAdmin):
    list_display = ("import_account", "gmail_message_id", "state", "append_status", "cleanup_status", "target_folder", "committed_at", "cleaned_at")
    list_filter = ("state", "append_status", "cleanup_status", "target_folder")
    search_fields = ("import_account__gmail_email", "gmail_message_id", "gmail_thread_id", "rfc_message_id", "target_folder", "error")
    readonly_fields = (
        "import_account",
        "gmail_message_id",
        "gmail_thread_id",
        "rfc_message_id",
        "target_folder",
        "state",
        "append_status",
        "cleanup_status",
        "fetched_at",
        "appended_at",
        "committed_at",
        "cleaned_at",
        "error",
        "created_at",
        "updated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(GmailImportRun)
class GmailImportRunAdmin(admin.ModelAdmin):
    list_display = ("import_account", "mode", "status", "scanned_count", "committed_count", "cleaned_count", "failed_count", "started_at", "finished_at")
    list_filter = ("mode", "status", "started_at")
    search_fields = ("import_account__gmail_email", "import_account__target_mailbox_email", "error")
    readonly_fields = (
        "import_account",
        "mode",
        "status",
        "scanned_count",
        "appended_count",
        "committed_count",
        "cleaned_count",
        "skipped_count",
        "failed_count",
        "error",
        "started_at",
        "finished_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


@admin.register(MailAccountIndex)
class MailAccountIndexAdmin(admin.ModelAdmin):
    list_display = ("account_email", "index_status", "imap_host", "sent_folder", "last_indexed_at", "updated_at")
    list_filter = ("index_status",)
    search_fields = ("account_email", "imap_host", "sent_folder")
    readonly_fields = ("created_at", "updated_at")


@admin.register(MailConversationIndex)
class MailConversationIndexAdmin(admin.ModelAdmin):
    list_display = ("account", "conversation_id", "latest_message_at", "message_count", "has_unread")
    list_filter = ("has_unread", "has_attachments", "has_visible_attachments")
    search_fields = ("account__account_email", "conversation_id", "thread_key", "normalized_subject")
    readonly_fields = ("created_at", "updated_at")


@admin.register(MailMessageIndex)
class MailMessageIndexAdmin(admin.ModelAdmin):
    list_display = ("account", "folder", "uid", "direction", "subject", "sent_at", "is_read")
    list_filter = ("direction", "folder", "is_read", "has_attachments", "has_visible_attachments")
    search_fields = ("account__account_email", "folder", "uid", "message_id", "subject", "sender_email")
    readonly_fields = ("indexed_at", "created_at", "updated_at")


@admin.register(MailFolderIndexState)
class MailFolderIndexStateAdmin(admin.ModelAdmin):
    list_display = ("account", "folder", "highest_indexed_uid", "last_synced_at", "updated_at")
    search_fields = ("account__account_email", "folder")
    readonly_fields = ("created_at", "updated_at")
