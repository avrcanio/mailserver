from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from importlib import import_module
from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from mail_integration.exceptions import MailAuthError, MailConnectionError, MailIntegrationError, MailProtocolError, MailSendError, MailTimeoutError
from mail_integration.mailbox_service import MailboxService
from mail_integration.schemas import MailboxCredentials, SendMailRequest

from .api_serializers import (
    ErrorSerializer,
    FoldersResponseSerializer,
    IdentitySerializer,
    LoginRequestSerializer,
    MessageDetailResponseSerializer,
    MessageSummariesResponseSerializer,
    SendMailRequestSerializer,
    SendMailResponseSerializer,
)


MAILBOX_EMAIL_SESSION_KEY = "mailbox_email"
MAILBOX_PASSWORD_SESSION_KEY = "mailbox_password"


def create_mailbox_token(email, password):
    session = _session_store()
    session[MAILBOX_EMAIL_SESSION_KEY] = email.strip().lower()
    session[MAILBOX_PASSWORD_SESSION_KEY] = password
    session.create()
    return session.session_key


def mailbox_credentials_from_token(request):
    token = _authorization_token(request)
    if not token:
        return None
    session = _session_store(session_key=token)
    if not session.exists(token):
        return None
    email = session.get(MAILBOX_EMAIL_SESSION_KEY)
    password = session.get(MAILBOX_PASSWORD_SESSION_KEY)
    if not email or not password:
        return None
    return MailboxCredentials(email=email, password=password)


def require_mailbox_credentials(request):
    credentials = mailbox_credentials_from_token(request)
    if credentials is None:
        return None, Response({"error": "not_authenticated"}, status=status.HTTP_401_UNAUTHORIZED)
    return credentials, None


def _authorization_token(request):
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() not in {"token", "bearer"} or not token.strip():
        return None
    return token.strip()


def _session_store(session_key=None):
    engine = import_module(settings.SESSION_ENGINE)
    return engine.SessionStore(session_key=session_key)


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
        return Response({"authenticated": True, "account_email": email, "token": token, "folder_count": len(folders)})


class MeView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(responses={200: IdentitySerializer, 401: ErrorSerializer})
    def get(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        return Response({"authenticated": True, "account_email": credentials.email})


class FolderListView(APIView):
    authentication_classes = []
    permission_classes = []

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
    authentication_classes = []
    permission_classes = []

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
    authentication_classes = []
    permission_classes = []

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
    authentication_classes = []
    permission_classes = []

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
