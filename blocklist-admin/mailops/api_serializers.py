import json
from email.utils import getaddresses

from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from drf_spectacular.utils import extend_schema_field
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


def normalize_fcm_token(value):
    token = str(value or "").strip()
    if not token:
        raise serializers.ValidationError("This field is required.")
    return token


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


class GmailConnectStartResponseSerializer(serializers.Serializer):
    authorization_url = serializers.URLField()
    state = serializers.CharField()
    account_email = serializers.EmailField()


class GmailConnectCompleteRequestSerializer(serializers.Serializer):
    code = serializers.CharField(trim_whitespace=True, allow_blank=False)
    state = serializers.CharField(trim_whitespace=True, allow_blank=False)


class GmailConnectedAccountSerializer(serializers.Serializer):
    connected = serializers.BooleanField()
    gmail_email = serializers.EmailField()
    target_mailbox_email = serializers.EmailField()
    delete_after_import = serializers.BooleanField()


class ErrorSerializer(serializers.Serializer):
    error = serializers.CharField()
    detail = serializers.CharField(required=False, allow_blank=True)


class FolderSerializer(serializers.Serializer):
    name = serializers.CharField()
    path = serializers.CharField()
    display_name = serializers.CharField()
    parent_path = serializers.CharField(allow_null=True)
    depth = serializers.IntegerField()
    delimiter = serializers.CharField(allow_null=True, required=False)
    flags = serializers.ListField(child=serializers.CharField())
    selectable = serializers.BooleanField()


class AttachmentSerializer(serializers.Serializer):
    id = serializers.CharField()
    filename = serializers.CharField(allow_null=True, required=False)
    content_type = serializers.CharField()
    size = serializers.IntegerField(allow_null=True, required=False)
    disposition = serializers.CharField(allow_null=True, required=False)
    is_inline = serializers.BooleanField()
    content_id = serializers.CharField(allow_blank=True)
    is_visible = serializers.BooleanField()


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
    has_visible_attachments = serializers.BooleanField()


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


class ConversationParticipantSerializer(serializers.Serializer):
    name = serializers.CharField(allow_blank=True)
    email = serializers.EmailField()


class ConversationSerializer(serializers.Serializer):
    conversation_id = serializers.CharField()
    message_count = serializers.IntegerField()
    reply_count = serializers.IntegerField()
    has_unread = serializers.BooleanField()
    has_attachments = serializers.BooleanField()
    has_visible_attachments = serializers.BooleanField()
    participants = ConversationParticipantSerializer(many=True)
    root_message = MessageSummarySerializer()
    replies = MessageSummarySerializer(many=True)
    latest_date = serializers.DateTimeField(allow_null=True)


class ConversationListResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    folder = serializers.CharField()
    conversations = ConversationSerializer(many=True)


class UnifiedMessageSummarySerializer(MessageSummarySerializer):
    direction = serializers.ChoiceField(choices=("inbound", "outbound"))


class UnifiedConversationSerializer(serializers.Serializer):
    conversation_id = serializers.CharField()
    message_count = serializers.IntegerField()
    reply_count = serializers.IntegerField()
    has_unread = serializers.BooleanField()
    has_attachments = serializers.BooleanField()
    has_visible_attachments = serializers.BooleanField()
    participants = ConversationParticipantSerializer(many=True)
    latest_date = serializers.DateTimeField(allow_null=True)
    messages = UnifiedMessageSummarySerializer(many=True)


class UnifiedConversationListResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    folders = serializers.ListField(child=serializers.CharField())
    conversations = UnifiedConversationSerializer(many=True)


class MailIndexStatusQuerySerializer(serializers.Serializer):
    account_email = serializers.EmailField(required=False)


class MailIndexFolderStatusSerializer(serializers.Serializer):
    folder = serializers.CharField()
    uidvalidity = serializers.CharField(allow_blank=True)
    highest_indexed_uid = serializers.IntegerField()
    last_synced_at = serializers.DateTimeField(allow_null=True)


class MailIndexStatusResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    index_status = serializers.CharField()
    last_indexed_at = serializers.DateTimeField(allow_null=True)
    last_sync_started_at = serializers.DateTimeField(allow_null=True)
    last_sync_finished_at = serializers.DateTimeField(allow_null=True)
    last_sync_error = serializers.CharField(allow_blank=True)
    folders = MailIndexFolderStatusSerializer(many=True)


class MessageDetailResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    folder = serializers.CharField()
    message = MessageDetailSerializer()


class ForwardSourceMessageSerializer(serializers.Serializer):
    folder = serializers.CharField(allow_blank=False)
    uid = MailboxUidField()
    attachment_ids = serializers.ListField(child=serializers.CharField(allow_blank=False), allow_empty=False)


@extend_schema_field(ForwardSourceMessageSerializer)
class ForwardSourceMessageField(serializers.Field):
    def to_internal_value(self, data):
        if data in (None, ""):
            return None
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError as exc:
                raise serializers.ValidationError("Enter a valid JSON object.") from exc
        if not isinstance(data, dict):
            raise serializers.ValidationError("Expected an object.")
        serializer = ForwardSourceMessageSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        return serializer.validated_data

    def to_representation(self, value):
        return value


class SendMailRequestSerializer(serializers.Serializer):
    to = serializers.ListField(child=MailboxAddressField(), allow_empty=False)
    subject = serializers.CharField(allow_blank=False)
    text_body = serializers.CharField(required=False, allow_blank=True, default="")
    html_body = serializers.CharField(required=False, allow_blank=True, default="")
    cc = serializers.ListField(child=MailboxAddressField(), required=False, allow_empty=True, default=list)
    bcc = serializers.ListField(child=MailboxAddressField(), required=False, allow_empty=True, default=list)
    reply_to = MailboxAddressField(required=False, allow_blank=True, allow_null=True, default=None)
    in_reply_to = serializers.CharField(required=False, allow_blank=True, default="")
    references = serializers.ListField(child=serializers.CharField(allow_blank=False), required=False, allow_empty=True, default=list)
    from_display_name = serializers.CharField(required=False, allow_blank=True, default="")
    forward_source_message = ForwardSourceMessageField(required=False, allow_null=True, default=None)

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
        try:
            attrs["normalized_fcm_token"] = normalize_fcm_token(attrs.get("fcm_token") or attrs.get("fcmToken") or "")
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({"fcm_token": exc.detail}) from exc
        platform = (attrs.get("platform") or "unknown").strip().lower()
        attrs["normalized_platform"] = platform if platform in {"android", "ios", "web", "unknown"} else "unknown"
        attrs["normalized_app_version"] = (attrs.get("app_version") or attrs.get("appVersion") or "").strip()
        return attrs


class DeviceRegistrationResponseSerializer(serializers.Serializer):
    status = serializers.CharField()
    created = serializers.BooleanField()
    id = serializers.IntegerField()
    account_email = serializers.EmailField()


class AccountsSummaryQuerySerializer(serializers.Serializer):
    fcm_token = serializers.CharField(required=False, allow_blank=True)
    fcmToken = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        try:
            attrs["normalized_fcm_token"] = normalize_fcm_token(attrs.get("fcm_token") or attrs.get("fcmToken") or "")
        except serializers.ValidationError as exc:
            raise serializers.ValidationError({"fcm_token": exc.detail}) from exc
        return attrs


class AccountSummarySerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    display_name = serializers.CharField(allow_blank=True)
    unread_count = serializers.IntegerField()
    important_count = serializers.IntegerField()


class AccountSummariesResponseSerializer(serializers.Serializer):
    accounts = AccountSummarySerializer(many=True)


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
