from drf_spectacular.utils import OpenApiParameter, extend_schema
from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from mail_integration.exceptions import MailAuthError, MailConnectionError, MailIntegrationError, MailProtocolError, MailSendError, MailTimeoutError
from mail_integration.mailbox_service import MailboxService
from mail_integration.schemas import MailboxCredentials, SendMailRequest

from .api_serializers import (
    DeviceRegistrationRequestSerializer,
    DeviceRegistrationResponseSerializer,
    ErrorSerializer,
    FoldersResponseSerializer,
    IdentitySerializer,
    LoginRequestSerializer,
    LogoutResponseSerializer,
    MailHookRequestSerializer,
    MailHookResponseSerializer,
    MessageDetailResponseSerializer,
    MessageSummariesResponseSerializer,
    SendMailRequestSerializer,
    SendMailResponseSerializer,
)
from .models import DeviceRegistration, MailboxTokenCredential
from .services import send_mail_notification


MAILBOX_API_AUTHENTICATION_CLASSES = [TokenAuthentication]
MAILBOX_API_PERMISSION_CLASSES = [IsAuthenticated]


def create_mailbox_token(email, password):
    normalized_email = email.strip().lower()
    user = get_or_create_mailbox_user(normalized_email)
    token, _ = Token.objects.get_or_create(user=user)
    try:
        token_credential = token.mailbox_credential
    except MailboxTokenCredential.DoesNotExist:
        token_credential = MailboxTokenCredential(token=token)
    token_credential.mailbox_email = normalized_email
    token_credential.set_mailbox_password(password)
    token_credential.save()
    return token


def get_or_create_mailbox_user(email):
    User = get_user_model()
    normalized_email = email.strip().lower()
    user = User.objects.filter(email__iexact=normalized_email).first()
    created = False
    if user is None:
        user, created = User.objects.get_or_create(
            username=normalized_email,
            defaults={
                "email": normalized_email,
                "is_active": True,
                "is_staff": False,
                "is_superuser": False,
            },
        )

    changed = False
    for field, value in {
        "username": normalized_email,
        "email": normalized_email,
        "is_active": True,
        "is_staff": False,
        "is_superuser": False,
    }.items():
        if getattr(user, field) != value:
            setattr(user, field, value)
            changed = True
    if created or user.has_usable_password():
        user.set_unusable_password()
        changed = True
    if changed:
        user.save(update_fields=["username", "email", "is_active", "is_staff", "is_superuser", "password"])
    return user


def mailbox_credentials_from_request(request):
    token = request.auth
    if not isinstance(token, Token):
        return None
    try:
        token_credential = token.mailbox_credential
    except MailboxTokenCredential.DoesNotExist:
        return None
    return MailboxCredentials(email=token_credential.mailbox_email, password=token_credential.get_mailbox_password())


def require_mailbox_credentials(request):
    if not request.user or not request.user.is_authenticated:
        return None, Response({"error": "not_authenticated"}, status=status.HTTP_401_UNAUTHORIZED)
    credentials = mailbox_credentials_from_request(request)
    if credentials is None:
        return None, Response({"error": "mailbox_credentials_missing"}, status=status.HTTP_401_UNAUTHORIZED)
    return credentials, None


def secret_matches(request, header_name, expected_secret):
    if not expected_secret:
        return False
    return request.headers.get(header_name) == expected_secret


def mail_error_response(exc):
    error_map = {
        MailAuthError: ("mail_auth_failed", status.HTTP_401_UNAUTHORIZED),
        MailTimeoutError: ("mail_timeout", status.HTTP_504_GATEWAY_TIMEOUT),
        MailConnectionError: ("mail_connection_failed", status.HTTP_502_BAD_GATEWAY),
        MailProtocolError: ("mail_protocol_failed", status.HTTP_502_BAD_GATEWAY),
        MailSendError: ("mail_send_failed", status.HTTP_502_BAD_GATEWAY),
    }
    for error_type, (code, response_status) in error_map.items():
        if isinstance(exc, error_type):
            return Response({"error": code, "detail": str(exc)}, status=response_status)
    return Response({"error": "mail_integration_failed", "detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)


def folder_payload(folder):
    return {
        "name": folder.name,
        "delimiter": folder.delimiter,
        "flags": list(folder.flags),
    }


def summary_payload(summary):
    return {
        "uid": summary.uid,
        "folder": summary.folder,
        "subject": summary.subject,
        "sender": summary.sender,
        "to": list(summary.to),
        "cc": list(summary.cc),
        "date": summary.date,
        "message_id": summary.message_id,
        "flags": list(summary.flags),
        "size": summary.size,
    }


def detail_payload(detail):
    payload = summary_payload(detail)
    payload.update(
        {
            "text_body": detail.text_body,
            "html_body": detail.html_body,
            "attachments": [
                {
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "size": attachment.size,
                    "disposition": attachment.disposition,
                }
                for attachment in detail.attachments
            ],
        }
    )
    return payload


class LoginView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(
        request=LoginRequestSerializer,
        responses={
            200: IdentitySerializer,
            400: ErrorSerializer,
            401: ErrorSerializer,
            502: ErrorSerializer,
            504: ErrorSerializer,
        },
    )
    def post(self, request):
        serializer = LoginRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data["email"].strip().lower()
        password = serializer.validated_data["password"]
        credentials = MailboxCredentials(email=email, password=password)
        try:
            folders = MailboxService().list_folders(credentials)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        token = create_mailbox_token(email, password)
        return Response(
            {
                "authenticated": True,
                "user": {
                    "id": token.user_id,
                    "email": token.user.email,
                },
                "account_email": email,
                "token": token.key,
                "folder_count": len(folders),
            }
        )


class MeView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(responses={200: IdentitySerializer, 401: ErrorSerializer})
    def get(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        return Response(
            {
                "authenticated": True,
                "user": {
                    "id": request.user.id,
                    "email": request.user.email,
                },
                "account_email": credentials.email,
            }
        )


class LogoutView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(request=None, responses={200: LogoutResponseSerializer, 401: ErrorSerializer})
    def post(self, request):
        request.auth.delete()
        return Response({"success": True})


class FolderListView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(responses={200: FoldersResponseSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer})
    def get(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        try:
            folders = MailboxService().list_folders(credentials)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response({"account_email": credentials.email, "folders": [folder_payload(folder) for folder in folders]})


class MessageListView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="mail_messages_list",
        parameters=[
            OpenApiParameter("folder", str, required=False, description="Mailbox folder name. Defaults to INBOX."),
            OpenApiParameter("limit", int, required=False, description="Maximum message summaries to return. 1-200, defaults to 50."),
        ],
        responses={200: MessageSummariesResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def get(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        folder = (request.query_params.get("folder") or "INBOX").strip() or "INBOX"
        try:
            limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError):
            return Response({"error": "invalid_limit"}, status=status.HTTP_400_BAD_REQUEST)
        if limit < 1 or limit > 200:
            return Response({"error": "invalid_limit"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            summaries = MailboxService().list_message_summaries(credentials, folder=folder, limit=limit)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response(
            {
                "account_email": credentials.email,
                "folder": folder,
                "messages": [summary_payload(summary) for summary in summaries],
            }
        )


class MessageDetailView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="mail_messages_detail",
        parameters=[OpenApiParameter("folder", str, required=False, description="Mailbox folder name. Defaults to INBOX.")],
        responses={200: MessageDetailResponseSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def get(self, request, uid):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        folder = (request.query_params.get("folder") or "INBOX").strip() or "INBOX"
        try:
            detail = MailboxService().get_message_detail(credentials, folder=folder, uid=uid)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response({"account_email": credentials.email, "folder": folder, "message": detail_payload(detail)})


class SendMailView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        request=SendMailRequestSerializer,
        responses={200: SendMailResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def post(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        serializer = SendMailRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        send_request = SendMailRequest(
            to=tuple(data["to"]),
            cc=tuple(data.get("cc", ())),
            bcc=tuple(data.get("bcc", ())),
            reply_to=data.get("reply_to") or None,
            subject=data["subject"],
            text_body=data.get("text_body", ""),
            html_body=data.get("html_body", ""),
            from_display_name=data.get("from_display_name", ""),
        )
        try:
            message_id = MailboxService().send_mail(credentials, send_request)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response({"account_email": credentials.email, "status": "sent", "message_id": message_id})


class DeviceRegistrationView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        request=DeviceRegistrationRequestSerializer,
        responses={200: DeviceRegistrationResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer},
    )
    def post(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        if not secret_matches(request, "X-Device-Registration-Secret", settings.DEVICE_REGISTRATION_SECRET):
            return Response({"error": "unauthorized"}, status=status.HTTP_403_FORBIDDEN)

        serializer = DeviceRegistrationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        supplied_email = data["normalized_account_email"]
        if supplied_email and supplied_email != credentials.email:
            return Response({"error": "account_email_mismatch"}, status=status.HTTP_403_FORBIDDEN)

        device, created = DeviceRegistration.objects.update_or_create(
            fcm_token=data["normalized_fcm_token"],
            defaults={
                "account_email": credentials.email,
                "platform": data["normalized_platform"],
                "app_version": data["normalized_app_version"],
                "enabled": True,
                "last_seen_at": timezone.now(),
            },
        )
        return Response(
            {
                "status": "ok",
                "created": created,
                "id": device.id,
                "account_email": device.account_email,
            }
        )


class NewMailHookView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(
        request=MailHookRequestSerializer,
        responses={200: MailHookResponseSerializer, 400: ErrorSerializer, 403: ErrorSerializer, 502: ErrorSerializer},
    )
    def post(self, request):
        if not secret_matches(request, "X-Mail-Hook-Secret", settings.MAIL_NOTIFY_HOOK_SECRET):
            return Response({"error": "unauthorized"}, status=status.HTTP_403_FORBIDDEN)

        serializer = MailHookRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        event = {
            "accountEmail": data["accountEmail"].strip().lower(),
            "sender": (data.get("sender") or "").strip()[:255],
            "subject": (data.get("subject") or "").strip()[:255],
            "receivedAt": (data.get("receivedAt") or timezone.now().isoformat()).strip(),
            "folder": (data.get("folder") or "").strip(),
            "uid": (data.get("uid") or "").strip(),
            "messageId": (data.get("messageId") or "").strip(),
        }
        try:
            result = send_mail_notification(event)
        except Exception as exc:
            return Response({"error": "notification_failed", "detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)
