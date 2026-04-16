from rest_framework import serializers


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
    filename = serializers.CharField(allow_null=True, required=False)
    content_type = serializers.CharField()
    size = serializers.IntegerField(allow_null=True, required=False)
    disposition = serializers.CharField(allow_null=True, required=False)


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


class MessageDetailResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    folder = serializers.CharField()
    message = MessageDetailSerializer()


class SendMailRequestSerializer(serializers.Serializer):
    to = serializers.ListField(child=serializers.EmailField(), allow_empty=False)
    subject = serializers.CharField(allow_blank=False)
    text_body = serializers.CharField(required=False, allow_blank=True, default="")
    html_body = serializers.CharField(required=False, allow_blank=True, default="")
    cc = serializers.ListField(child=serializers.EmailField(), required=False, allow_empty=True, default=list)
    bcc = serializers.ListField(child=serializers.EmailField(), required=False, allow_empty=True, default=list)
    reply_to = serializers.EmailField(required=False, allow_blank=True, allow_null=True, default=None)
    from_display_name = serializers.CharField(required=False, allow_blank=True, default="")

    def validate(self, attrs):
        if not attrs.get("text_body") and not attrs.get("html_body"):
            raise serializers.ValidationError({"body": "Either text_body or html_body is required."})
        return attrs


class SendMailResponseSerializer(serializers.Serializer):
    account_email = serializers.EmailField()
    status = serializers.CharField()
    message_id = serializers.CharField(allow_null=True)


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
