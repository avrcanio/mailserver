from rest_framework import serializers


class LoginRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(trim_whitespace=False, write_only=True)


class IdentitySerializer(serializers.Serializer):
    authenticated = serializers.BooleanField()
    account_email = serializers.EmailField()
    token = serializers.CharField(required=False)
    folder_count = serializers.IntegerField(required=False)


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
