from email.utils import getaddresses

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from rest_framework import serializers


def normalize_mailbox_address(value):
    raw_value = value.strip()
    if not raw_value:
        raise serializers.ValidationError("This field may not be blank.")
    if ("<" in raw_value or ">" in raw_value) and not (
        raw_value.count("<") == 1 and raw_value.count(">") == 1 and raw_value.index("<") < raw_value.index(">")
    ):
        raise serializers.ValidationError("Enter a valid email address.")
    addresses = getaddresses([raw_value])
    if len(addresses) != 1:
        raise serializers.ValidationError("Enter one email address per list item.")
    email = addresses[0][1].strip()
    if not email:
        raise serializers.ValidationError("Enter a valid email address.")
    try:
        validate_email(email)
    except DjangoValidationError as exc:
        raise serializers.ValidationError("Enter a valid email address.") from exc
    return email


class MailboxAddressField(serializers.CharField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        if value == "" and self.allow_blank:
            return ""
        return normalize_mailbox_address(value)


class MailboxUidField(serializers.CharField):
    def to_internal_value(self, data):
        value = super().to_internal_value(data)
        try:
            uid = int(value.strip())
        except (TypeError, ValueError) as exc:
            raise serializers.ValidationError("Enter a valid message UID.") from exc
        if uid < 1:
            raise serializers.ValidationError("Enter a valid message UID.")
        return str(uid)


class LoginRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False, write_only=True)


class UserIdentitySerializer(serializers.Serializer):
    id = serializers.IntegerField()
    email = serializers.EmailField()


class IdentitySerializer(serializers.Serializer):
    authenticated = serializers.BooleanField()
    user = UserIdentitySerializer(required=False)
    account_email = serializers.EmailField()
    token = serializers.CharField(required=False)
    folder_count = serializers.IntegerField(required=False)


class LogoutResponseSerializer(serializers.Serializer):
    success = serializers.BooleanField()


class ErrorSerializer(serializers.Serializer):
    error = serializers.CharField()
    detail = serializers.CharField(required=False, allow_blank=True)


class FolderSerializer(serializers.Serializer):
    name = serializers.CharField()
    delimiter = serializers.CharField(allow_null=True, required=False)
    flags = serializers.ListField(child=serializers.CharField())


class AttachmentSerializer(serializers.Serializer):
    id = serializers.CharField()
    filename = serializers.CharField(allow_null=True, required=False)
    content_type = serializers.CharField()
    size = serializers.IntegerField(allow_null=True, required=False)
    disposition = serializers.CharField(allow_null=True, required=False)
    is_inline = serializers.BooleanField()


class MessageSummarySerializer(serializers.Serializer):
    uid = serializers.CharField()
    folder = serializers.CharField()
    subject = serializers.CharField(allow_blank=True)
    sender = serializers.CharField(allow_blank=True)
    to = serializers.ListField(child=serializers.EmailField())
    cc = serializers.ListField(child=serializers.EmailField())
    date = serializers.DateTimeField(allow_null=True)
    message_id = serializers.CharField(allow_blank=True)
    flags = serializers.ListField(child=serializers.CharField())
    size = serializers.IntegerField(allow_null=True)
    has_attachments = serializers.BooleanField()


class MessageDetailSerializer(MessageSummarySerializer):
    text_body = serializers.CharField(allow_blank=True)
    html_body = serializers.CharField(allow_blank=True)
    attachments = AttachmentSerializer(many=True)


class FoldersResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    folders = FolderSerializer(many=True)


class MessageSummariesResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    folder = serializers.CharField()
    messages = MessageSummarySerializer(many=True)
    has_more = serializers.BooleanField()
    next_before_uid = serializers.CharField(allow_null=True)


class MessageDetailResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    folder = serializers.CharField()
    message = MessageDetailSerializer()


class SendMailRequestSerializer(serializers.Serializer):
    to = serializers.ListField(child=MailboxAddressField(), allow_empty=False)
    subject = serializers.CharField(allow_blank=False)
    text_body = serializers.CharField(required=False, allow_blank=True, default="")
    html_body = serializers.CharField(required=False, allow_blank=True, default="")
    cc = serializers.ListField(child=MailboxAddressField(), required=False, allow_empty=True, default=list)
    bcc = serializers.ListField(child=MailboxAddressField(), required=False, allow_empty=True, default=list)
    reply_to = MailboxAddressField(required=False, allow_blank=True, allow_null=True, default=None)
    from_display_name = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if not attrs.get("text_body") and not attrs.get("html_body"):
            raise serializers.ValidationError({"body": "Either text_body or html_body is required."})
        return attrs


class SendMailResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    status = serializers.CharField()
    message_id = serializers.CharField(allow_null=True)


class SendMailMultipartRequestSerializer(SendMailRequestSerializer):
    attachments = serializers.ListField(child=serializers.FileField(), required=False)


class DeleteMessagesRequestSerializer(serializers.Serializer):
    folder = serializers.CharField(allow_blank=False)
    uids = serializers.ListField(child=MailboxUidField(), allow_empty=False)


class DeleteMessageFailureSerializer(serializers.Serializer):
    uid = serializers.CharField()
    error = serializers.CharField()
    detail = serializers.CharField()


class DeleteMessagesResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    folder = serializers.CharField()
    trash_folder = serializers.CharField()
    success = serializers.BooleanField()
    partial = serializers.BooleanField()
    moved_to_trash = serializers.ListField(child=serializers.CharField())
    failed = DeleteMessageFailureSerializer(many=True)


class RestoreMessagesRequestSerializer(serializers.Serializer):
    folder = serializers.CharField(allow_blank=False)
    target_folder = serializers.CharField(allow_blank=False)
    uids = serializers.ListField(child=MailboxUidField(), allow_empty=False)


class RestoreMessagesResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    folder = serializers.CharField()
    target_folder = serializers.CharField()
    success = serializers.BooleanField()
    partial = serializers.BooleanField()
    restored = serializers.ListField(child=serializers.CharField())
    failed = DeleteMessageFailureSerializer(many=True)


class DeviceRegistrationRequestSerializer(serializers.Serializer):
    account_email = serializers.EmailField(required=False)
    accountEmail = serializers.EmailField(required=False)
    accountId = serializers.EmailField(required=False)
    email = serializers.EmailField(required=False)
    fcm_token = serializers.CharField(required=False, allow_blank=True)
    fcmToken = serializers.CharField(required=False, allow_blank=True)
    platform = serializers.CharField(required=False, allow_blank=True, default="", max_length=32)
    app_version = serializers.CharField(required=False, allow_blank=True, default="", max_length=64)
    appVersion = serializers.CharField(required=False, allow_blank=True, max_length=64)

    def validate(self, attrs):
        attrs["normalized_account_email"] = (
            attrs.get("account_email") or attrs.get("accountEmail") or attrs.get("accountId") or attrs.get("email") or ""
        ).strip().lower()
        attrs["normalized_fcm_token"] = (attrs.get("fcm_token") or attrs.get("fcmToken") or "").strip()
        platform = (attrs.get("platform") or "unknown").strip().lower()
        attrs["normalized_platform"] = platform if platform in {"android", "ios", "web", "unknown"} else "unknown"
        attrs["normalized_app_version"] = (attrs.get("app_version") or attrs.get("appVersion") or "").strip()
        if not attrs["normalized_fcm_token"]:
            raise serializers.ValidationError({"fcm_token": "This field is required."})
        return attrs


class DeviceRegistrationResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    created = serializers.BooleanField()
    id = serializers.IntegerField()
    account_email = serializers.EmailField()


class MailHookRequestSerializer(serializers.Serializer):
    accountEmail = serializers.EmailField()
    sender = serializers.CharField(required=False, allow_blank=True, default="")
    subject = serializers.CharField(required=False, allow_blank=True, default="")
    receivedAt = serializers.CharField(required=False, allow_blank=True, default="")
    folder = serializers.CharField(required=False, allow_blank=True, default="")
    uid = serializers.CharField(required=False, allow_blank=True, default="")
    messageId = serializers.CharField(required=False, allow_blank=True, default="")


class MailHookResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    deviceCount = serializers.IntegerField()
    successCount = serializers.IntegerField()
    failureCount = serializers.IntegerField()
