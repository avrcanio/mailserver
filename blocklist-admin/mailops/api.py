import logging
import secrets
from email.utils import getaddresses

from drf_spectacular.utils import OpenApiParameter, OpenApiTypes, extend_schema
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils.http import content_disposition_header
from django.utils import timezone
from rest_framework import status
from rest_framework.authentication import TokenAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from mail_integration.exceptions import (
    MailAttachmentNotFoundError,
    MailAttachmentLimitError,
    MailAuthError,
    MailConnectionError,
    MailForwardAttachmentNotFoundError,
    MailForwardAttachmentNotVisibleError,
    MailIntegrationError,
    MailInvalidOperationError,
    MailProtocolError,
    MailSendError,
    MailTimeoutError,
)
from mail_integration.mailbox_service import MAX_SEND_ATTACHMENT_SIZE_BYTES, MAX_SEND_ATTACHMENTS_TOTAL_BYTES, MailboxService
from mail_integration.schemas import ForwardSourceMessage, MailboxCredentials, SendMailAttachment, SendMailRequest

from .api_serializers import (
    AccountSummariesResponseSerializer,
    AccountsSummaryQuerySerializer,
    ConversationListResponseSerializer,
    DeviceRegistrationRequestSerializer,
    DeviceRegistrationResponseSerializer,
    DeleteMessagesRequestSerializer,
    DeleteMessagesResponseSerializer,
    ErrorSerializer,
    ExternalAccountsResponseSerializer,
    FoldersResponseSerializer,
    GmailConnectedAccountSerializer,
    GmailConnectCompleteRequestSerializer,
    GmailConnectStartResponseSerializer,
    GmailDisconnectResponseSerializer,
    GmailSyncTriggerRequestSerializer,
    GmailSyncTriggerResponseSerializer,
    IdentitySerializer,
    LoginRequestSerializer,
    LogoutResponseSerializer,
    MailHookRequestSerializer,
    MailHookResponseSerializer,
    MailIndexStatusQuerySerializer,
    MailIndexStatusResponseSerializer,
    MessageDetailResponseSerializer,
    MessageSummariesResponseSerializer,
    RestoreMessagesRequestSerializer,
    RestoreMessagesResponseSerializer,
    SendMailMultipartRequestSerializer,
    SendMailRequestSerializer,
    SendMailResponseSerializer,
    UnifiedConversationListResponseSerializer,
)
from mail_integration.gmail_client import (
    build_authorization_url,
    exchange_code_for_refresh_token,
    fetch_gmail_profile_email,
    oauth_config_from_settings,
)

from .gmail_import import GmailImportError, GmailImportService
from .models import DeviceRegistration, GmailImportAccount, MailAccountIndex, MailboxTokenCredential
from .services import send_mail_notification


MAILBOX_API_AUTHENTICATION_CLASSES = [TokenAuthentication]
MAILBOX_API_PERMISSION_CLASSES = [IsAuthenticated]
GMAIL_OAUTH_STATE_SALT = "mailops.gmail-oauth-state"
logger = logging.getLogger("mailops.api")


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


def signed_gmail_oauth_state(user):
    return signing.dumps(
        {
            "user_id": user.pk,
            "email": (user.email or "").strip().lower(),
            "nonce": secrets.token_urlsafe(16),
        },
        salt=GMAIL_OAUTH_STATE_SALT,
    )


def validate_gmail_oauth_state(raw_state, user):
    try:
        payload = signing.loads(str(raw_state or ""), salt=GMAIL_OAUTH_STATE_SALT, max_age=600)
    except signing.BadSignature:
        return False
    return payload.get("user_id") == user.pk and payload.get("email") == (user.email or "").strip().lower()


def gmail_account_payload(account):
    if account is None:
        return {
            "connected": False,
            "provider": "gmail",
            "gmail_email": None,
            "target_mailbox_email": None,
        }
    return {
        "connected": True,
        "provider": "gmail",
        "gmail_email": account.gmail_email,
        "target_mailbox_email": account.target_mailbox_email,
        "delete_after_import": account.delete_after_import,
        "last_success_at": account.last_success_at,
        "last_error": account.last_error,
        "historical_import_completed": bool(account.historical_import_completed_at),
        "historical_import_completed_at": account.historical_import_completed_at,
        "consecutive_failures": account.consecutive_failures,
    }


def require_user_mailbox_identity(request):
    credentials, error = require_mailbox_credentials(request)
    if error:
        return None, error
    account_email = (request.user.email or "").strip().lower()
    if credentials.email != account_email:
        return None, Response(
            {"error": "mailbox_identity_mismatch", "detail": "Authenticated mailbox must match the Django user email."},
            status=status.HTTP_401_UNAUTHORIZED,
        )
    return account_email, None


def user_gmail_account(user):
    return GmailImportAccount.objects.filter(user=user).first()


def folder_payload(folder):
    return {
        "name": folder.name,
        "path": folder.path or folder.name,
        "display_name": folder.display_name or folder.name,
        "parent_path": folder.parent_path,
        "depth": folder.depth,
        "delimiter": folder.delimiter,
        "flags": list(folder.flags),
        "selectable": folder.selectable,
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
        "has_attachments": getattr(summary, "has_attachments", False),
        "has_visible_attachments": getattr(summary, "has_visible_attachments", getattr(summary, "has_attachments", False)),
    }


def detail_payload(detail):
    payload = summary_payload(detail)
    payload["has_attachments"] = bool(detail.attachments)
    payload.update(
        {
            "text_body": detail.text_body,
            "html_body": detail.html_body,
            "attachments": [
                {
                    "id": attachment.id,
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "size": attachment.size,
                    "disposition": attachment.disposition,
                    "is_inline": attachment.is_inline,
                    "content_id": attachment.content_id,
                    "is_visible": attachment.is_visible,
                }
                for attachment in detail.attachments
            ],
        }
    )
    return payload


def conversation_payload(conversation):
    return {
        "conversation_id": conversation.conversation_id,
        "message_count": conversation.message_count,
        "reply_count": conversation.reply_count,
        "has_unread": conversation.has_unread,
        "has_attachments": conversation.has_attachments,
        "has_visible_attachments": conversation.has_visible_attachments,
        "participants": [
            {
                "name": participant.name,
                "email": participant.email,
            }
            for participant in conversation.participants
        ],
        "root_message": summary_payload(conversation.root_message),
        "replies": [summary_payload(reply) for reply in conversation.replies],
        "latest_date": conversation.latest_date,
    }


def unified_message_payload(message):
    payload = summary_payload(message.summary)
    payload["direction"] = message.direction
    return payload


def unified_conversation_payload(conversation):
    return {
        "conversation_id": conversation.conversation_id,
        "message_count": conversation.message_count,
        "reply_count": conversation.reply_count,
        "has_unread": conversation.has_unread,
        "has_attachments": conversation.has_attachments,
        "has_visible_attachments": conversation.has_visible_attachments,
        "participants": [
            {
                "name": participant.name,
                "email": participant.email,
            }
            for participant in conversation.participants
        ],
        "latest_date": conversation.latest_date,
        "messages": [unified_message_payload(message) for message in conversation.messages],
    }


def account_summary_payload(account_email, summary):
    return {
        "account_email": account_email,
        "display_name": "",
        "unread_count": summary.unread_count,
        "important_count": summary.important_count,
    }


def mail_index_status_payload(index):
    return {
        "account_email": index.account_email,
        "index_status": index.index_status,
        "last_indexed_at": index.last_indexed_at,
        "last_sync_started_at": index.last_sync_started_at,
        "last_sync_finished_at": index.last_sync_finished_at,
        "last_sync_error": index.last_sync_error,
        "folders": [
            {
                "folder": folder_state.folder,
                "uidvalidity": folder_state.uidvalidity,
                "highest_indexed_uid": folder_state.highest_indexed_uid,
                "last_synced_at": folder_state.last_synced_at,
            }
            for folder_state in index.folder_states.order_by("folder")
        ],
    }


def delete_result_payload(credentials, folder, result):
    failed = [
        {
            "uid": failure.uid,
            "error": failure.error,
            "detail": failure.detail,
        }
        for failure in result.failed
    ]
    return {
        "account_email": credentials.email,
        "folder": folder,
        "trash_folder": result.trash_folder,
        "success": bool(result.moved_to_trash) and not failed,
        "partial": bool(result.moved_to_trash) and bool(failed),
        "moved_to_trash": list(result.moved_to_trash),
        "failed": failed,
    }


def restore_result_payload(credentials, folder, result):
    failed = [
        {
            "uid": failure.uid,
            "error": failure.error,
            "detail": failure.detail,
        }
        for failure in result.failed
    ]
    return {
        "account_email": credentials.email,
        "folder": folder,
        "target_folder": result.target_folder,
        "success": bool(result.restored) and not failed,
        "partial": bool(result.restored) and bool(failed),
        "restored": list(result.restored),
        "failed": failed,
    }


def mark_mail_index_stale_after_send(user, account_email):
    try:
        MailAccountIndex.objects.filter(user=user, account_email=account_email.strip().lower()).update(last_indexed_at=None)
    except Exception as exc:
        logger.warning("Could not mark mail index stale for %s: %s", account_email, exc)


def mark_mail_index_stale_after_incoming(account_email):
    try:
        MailAccountIndex.objects.filter(account_email=account_email.strip().lower()).update(last_indexed_at=None)
    except Exception as exc:
        logger.warning("Could not mark incoming mail index stale for %s: %s", account_email, exc)


def mark_index_message_read(user, account_email, folder, uid):
    try:
        from mailops.mail_indexing.sync import rebuild_conversation
        from mailops.mail_indexing.threading import uid_int

        account = MailAccountIndex.objects.filter(user=user, account_email=account_email.strip().lower()).first()
        if account is None:
            return
        message = account.messages.filter(folder=folder, uid=uid_int(uid)).first()
        if message is None or message.is_read:
            return
        flags = list(message.flags_json or [])
        if not any(str(flag).lower() == "seen" for flag in flags):
            flags.append("Seen")
        message.flags_json = flags
        message.is_read = True
        message.save(update_fields=["flags_json", "is_read", "updated_at"])
        rebuild_conversation(account, message.thread_key)
    except Exception as exc:
        logger.warning("Could not mark indexed message read for %s %s/%s: %s", account_email, folder, uid, exc)


def remove_indexed_messages_after_delete(user, account_email, folder, moved_uids):
    if not moved_uids:
        return
    try:
        from mailops.mail_indexing.sync import rebuild_conversation
        from mailops.mail_indexing.threading import uid_int

        account = MailAccountIndex.objects.filter(user=user, account_email=account_email.strip().lower()).first()
        if account is None:
            return
        normalized_uids = [uid_int(uid) for uid in moved_uids if uid_int(uid)]
        if not normalized_uids:
            return
        rows = list(account.messages.filter(folder=folder, uid__in=normalized_uids))
        touched_thread_keys = {row.thread_key for row in rows}
        account.messages.filter(id__in=[row.id for row in rows]).delete()
        for thread_key in touched_thread_keys:
            rebuild_conversation(account, thread_key)
    except Exception as exc:
        logger.warning("Could not remove deleted indexed messages for %s %s %s: %s", account_email, folder, moved_uids, exc)


def delete_messages_response(request, credentials, folder, uids):
    try:
        result = MailboxService().move_messages_to_trash(credentials, folder=folder, uids=tuple(uids))
    except MailInvalidOperationError:
        return Response({"error": "delete_from_trash_not_supported"}, status=status.HTTP_400_BAD_REQUEST)
    except MailIntegrationError as exc:
        return mail_error_response(exc)
    remove_indexed_messages_after_delete(request.user, credentials.email, folder, result.moved_to_trash)
    return Response(delete_result_payload(credentials, folder, result))


def validate_delete_payload(data):
    if "folder" not in data or not str(data.get("folder") or "").strip():
        return None, Response({"error": "invalid_folder"}, status=status.HTTP_400_BAD_REQUEST)
    if "uids" not in data:
        return None, Response({"error": "empty_uid_list"}, status=status.HTTP_400_BAD_REQUEST)
    serializer = DeleteMessagesRequestSerializer(data=data)
    if not serializer.is_valid():
        if "uids" in serializer.errors:
            errors = serializer.errors["uids"]
            if any(getattr(error, "code", None) == "empty" for error in errors):
                return None, Response({"error": "empty_uid_list"}, status=status.HTTP_400_BAD_REQUEST)
            return None, Response({"error": "invalid_uid"}, status=status.HTTP_400_BAD_REQUEST)
        return None, Response({"error": "invalid_folder"}, status=status.HTTP_400_BAD_REQUEST)
    return serializer.validated_data, None


def validate_restore_payload(data):
    if "folder" not in data or not str(data.get("folder") or "").strip():
        return None, Response({"error": "invalid_folder"}, status=status.HTTP_400_BAD_REQUEST)
    if "target_folder" not in data or not str(data.get("target_folder") or "").strip():
        return None, Response({"error": "invalid_target_folder"}, status=status.HTTP_400_BAD_REQUEST)
    if "uids" not in data:
        return None, Response({"error": "empty_uid_list"}, status=status.HTTP_400_BAD_REQUEST)
    serializer = RestoreMessagesRequestSerializer(data=data)
    if not serializer.is_valid():
        if "uids" in serializer.errors:
            errors = serializer.errors["uids"]
            if any(getattr(error, "code", None) == "empty" for error in errors):
                return None, Response({"error": "empty_uid_list"}, status=status.HTTP_400_BAD_REQUEST)
            return None, Response({"error": "invalid_uid"}, status=status.HTTP_400_BAD_REQUEST)
        if "target_folder" in serializer.errors:
            return None, Response({"error": "invalid_target_folder"}, status=status.HTTP_400_BAD_REQUEST)
        return None, Response({"error": "invalid_folder"}, status=status.HTTP_400_BAD_REQUEST)
    return serializer.validated_data, None


def restore_invalid_operation_response(exc):
    error = str(exc)
    if error == "restore_source_not_trash":
        return Response({"error": "restore_source_not_trash"}, status=status.HTTP_400_BAD_REQUEST)
    if error == "restore_target_is_trash":
        return Response({"error": "restore_target_is_trash"}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"error": "invalid_restore_operation"}, status=status.HTTP_400_BAD_REQUEST)


def send_form_data(data):
    if not hasattr(data, "getlist"):
        return data
    normalized = {}
    for field in ("to", "cc", "bcc"):
        values = []
        for value in data.getlist(field):
            if not isinstance(value, str):
                values.append(value)
                continue
            parsed = getaddresses([value])
            if len(parsed) > 1:
                values.extend(address for _, address in parsed)
            else:
                values.append(value)
        if values:
            normalized[field] = values
    for field in ("reply_to", "in_reply_to", "subject", "text_body", "html_body", "from_display_name", "forward_source_message"):
        values = data.getlist(field)
        if values:
            normalized[field] = values[-1]
    references = data.getlist("references")
    if references:
        normalized["references"] = references
    return normalized


def uploaded_send_attachments(files):
    attachments = []
    total_size = 0
    for uploaded_file in files.getlist("attachments"):
        size = uploaded_file.size or 0
        if size > MAX_SEND_ATTACHMENT_SIZE_BYTES:
            return None, Response({"error": "attachment_too_large"}, status=status.HTTP_400_BAD_REQUEST)
        total_size += size
        if total_size > MAX_SEND_ATTACHMENTS_TOTAL_BYTES:
            return None, Response({"error": "attachments_too_large"}, status=status.HTTP_400_BAD_REQUEST)
        filename = (uploaded_file.name or "").strip()
        if not filename:
            return None, Response({"error": "invalid_attachment_payload"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            content = uploaded_file.read()
        except OSError:
            return None, Response({"error": "invalid_attachment_payload"}, status=status.HTTP_400_BAD_REQUEST)
        attachments.append(
            SendMailAttachment(
                filename=filename,
                content_type=uploaded_file.content_type or "application/octet-stream",
                content=content,
            )
        )
    return tuple(attachments), None


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


class GmailConnectStartView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(request=None, responses={200: GmailConnectStartResponseSerializer, 401: ErrorSerializer, 502: ErrorSerializer})
    def post(self, request):
        account_email, error = require_user_mailbox_identity(request)
        if error:
            return error
        try:
            oauth_config = oauth_config_from_settings()
            oauth_state = signed_gmail_oauth_state(request.user)
            authorization_url = build_authorization_url(oauth_config, state=oauth_state)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response({"authorization_url": authorization_url, "state": oauth_state, "account_email": account_email})


class GmailConnectCompleteView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        request=GmailConnectCompleteRequestSerializer,
        responses={200: GmailConnectedAccountSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer},
    )
    def post(self, request):
        account_email, error = require_user_mailbox_identity(request)
        if error:
            return error
        serializer = GmailConnectCompleteRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        if not validate_gmail_oauth_state(serializer.validated_data["state"], request.user):
            return Response({"error": "invalid_oauth_state"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            oauth_config = oauth_config_from_settings()
            refresh_token = exchange_code_for_refresh_token(serializer.validated_data["code"], oauth_config)
            gmail_email = fetch_gmail_profile_email(refresh_token, oauth_config)
        except MailIntegrationError as exc:
            return mail_error_response(exc)

        if gmail_email != account_email:
            return Response(
                {
                    "error": "gmail_identity_mismatch",
                    "detail": "Connected Gmail account must match the Django user email.",
                    "gmail_email": gmail_email,
                    "expected_email": account_email,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        account = GmailImportAccount.objects.filter(user=request.user).first()
        if account is None:
            account = GmailImportAccount.objects.filter(user__isnull=True, gmail_email=account_email).first()
        if account is None:
            account = GmailImportAccount(user=request.user)
        account.user = request.user
        account.gmail_email = account_email
        account.target_mailbox_email = account_email
        account.last_error = ""
        account.consecutive_failures = 0
        account.set_refresh_token(refresh_token)
        try:
            account.save()
        except ValidationError as exc:
            return Response({"error": "gmail_account_invalid", "detail": exc.message_dict}, status=status.HTTP_400_BAD_REQUEST)
        return Response(gmail_account_payload(account))


class ExternalAccountsView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(responses={200: ExternalAccountsResponseSerializer, 401: ErrorSerializer})
    def get(self, request):
        _, error = require_user_mailbox_identity(request)
        if error:
            return error
        account = user_gmail_account(request.user)
        return Response({"accounts": [gmail_account_payload(account)] if account else []})


class GmailAccountStatusView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(responses={200: GmailConnectedAccountSerializer, 401: ErrorSerializer})
    def get(self, request):
        _, error = require_user_mailbox_identity(request)
        if error:
            return error
        return Response(gmail_account_payload(user_gmail_account(request.user)))


class GmailDisconnectView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(request=None, responses={200: GmailDisconnectResponseSerializer, 401: ErrorSerializer})
    def post(self, request):
        _, error = require_user_mailbox_identity(request)
        if error:
            return error
        account = user_gmail_account(request.user)
        if account is not None:
            account.delete()
        return Response({"disconnected": True, "provider": "gmail"})


class GmailSyncTriggerView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        request=GmailSyncTriggerRequestSerializer,
        responses={200: GmailSyncTriggerResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer},
    )
    def post(self, request):
        _, error = require_user_mailbox_identity(request)
        if error:
            return error
        account = user_gmail_account(request.user)
        if account is None:
            return Response({"error": "gmail_account_not_connected"}, status=status.HTTP_400_BAD_REQUEST)
        serializer = GmailSyncTriggerRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        mode = serializer.validated_data["mode"]
        if mode == "auto":
            mode = "incremental" if account.historical_import_completed_at else "historical"
        service = GmailImportService()
        try:
            if mode == "incremental":
                result = service.run_incremental_import_for_user(
                    request.user,
                    limit=serializer.validated_data["limit"],
                    no_delete=serializer.validated_data["no_delete"],
                )
            else:
                result = service.run_historical_import_for_user(
                    request.user,
                    limit=serializer.validated_data["limit"],
                    since=serializer.validated_data["since"],
                    dry_run=False,
                    no_delete=serializer.validated_data["no_delete"],
                )
        except GmailImportError as exc:
            return Response({"error": "gmail_sync_failed", "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response(
            {
                "provider": "gmail",
                "mode": mode,
                "scanned": result.scanned,
                "appended": result.appended,
                "committed": result.committed,
                "cleaned": result.cleaned,
                "skipped": result.skipped,
                "failed": result.failed,
            }
        )


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
            OpenApiParameter("before_uid", str, required=False, description="Return the next older page before this message UID."),
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
        before_uid = (request.query_params.get("before_uid") or "").strip() or None
        if before_uid is not None:
            try:
                if int(before_uid) < 1:
                    raise ValueError
            except (TypeError, ValueError):
                return Response({"error": "invalid_before_uid"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            page = MailboxService().list_message_summary_page(credentials, folder=folder, limit=limit, before_uid=before_uid)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response(
            {
                "account_email": credentials.email,
                "folder": folder,
                "messages": [summary_payload(summary) for summary in page.messages],
                "has_more": page.has_more,
                "next_before_uid": page.next_before_uid,
            }
        )


class ConversationListView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="mail_conversations_list",
        parameters=[
            OpenApiParameter("folder", str, required=False, description="Mailbox folder name. Defaults to INBOX."),
            OpenApiParameter("limit", int, required=False, description="Maximum conversations to return. 1-200, defaults to 50."),
        ],
        responses={200: ConversationListResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
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
            page = MailboxService().list_conversations(credentials, folder=folder, limit=limit)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response(
            {
                "account_email": credentials.email,
                "folder": folder,
                "conversations": [conversation_payload(conversation) for conversation in page.conversations],
            }
        )


class UnifiedConversationListView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="mail_unified_conversations_list",
        parameters=[
            OpenApiParameter("limit", int, required=False, description="Maximum unified conversations to return. 1-200, defaults to 50."),
        ],
        responses={200: UnifiedConversationListResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def get(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        try:
            limit = int(request.query_params.get("limit", 50))
        except (TypeError, ValueError):
            return Response({"error": "invalid_limit"}, status=status.HTTP_400_BAD_REQUEST)
        if limit < 1 or limit > 200:
            return Response({"error": "invalid_limit"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            page = MailboxService().list_unified_conversations(credentials, limit=limit, user=request.user)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response(
            {
                "account_email": credentials.email,
                "folders": list(page.folders),
                "conversations": [unified_conversation_payload(conversation) for conversation in page.conversations],
            }
        )


class MailIndexStatusView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="mail_index_status",
        parameters=[OpenApiParameter("account_email", str, required=False, description="Mailbox account email. Defaults to the current token mailbox.")],
        responses={200: MailIndexStatusResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 404: ErrorSerializer},
    )
    def get(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        serializer = MailIndexStatusQuerySerializer(data=request.query_params)
        if not serializer.is_valid():
            return Response({"error": "invalid_account_email"}, status=status.HTTP_400_BAD_REQUEST)
        account_email = (serializer.validated_data.get("account_email") or credentials.email).strip().lower()
        index = (
            MailAccountIndex.objects.filter(user=request.user, account_email=account_email)
            .prefetch_related("folder_states")
            .first()
        )
        if index is None:
            return Response({"error": "mail_index_not_found"}, status=status.HTTP_404_NOT_FOUND)
        return Response(mail_index_status_payload(index))


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
        mark_index_message_read(request.user, credentials.email, folder, uid)
        return Response({"account_email": credentials.email, "folder": folder, "message": detail_payload(detail)})

    @extend_schema(
        operation_id="mail_messages_detail_delete",
        parameters=[OpenApiParameter("folder", str, required=False, description="Source mailbox folder name. Defaults to INBOX.")],
        responses={200: DeleteMessagesResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def delete(self, request, uid):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        folder = (request.query_params.get("folder") or "INBOX").strip() or "INBOX"
        data, error = validate_delete_payload({"folder": folder, "uids": [uid]})
        if error:
            return error
        return delete_messages_response(request, credentials, data["folder"], data["uids"])


class AttachmentDownloadView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="mail_messages_attachment_download",
        parameters=[OpenApiParameter("folder", str, required=True, description="Mailbox folder name.")],
        responses={200: OpenApiTypes.BINARY, 400: ErrorSerializer, 401: ErrorSerializer, 404: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def get(self, request, uid, attachment_id):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        folder = (request.query_params.get("folder") or "").strip()
        if not folder:
            return Response({"error": "invalid_folder"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            attachment = MailboxService().get_attachment(credentials, folder=folder, uid=uid, attachment_id=attachment_id)
        except MailAttachmentNotFoundError:
            return Response({"error": "attachment_not_found"}, status=status.HTTP_404_NOT_FOUND)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        response = HttpResponse(attachment.content, content_type=attachment.summary.content_type or "application/octet-stream")
        if attachment.summary.filename:
            response["Content-Disposition"] = content_disposition_header(False, attachment.summary.filename)
        return response


class DeleteMessagesView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="mail_messages_delete",
        request=DeleteMessagesRequestSerializer,
        responses={200: DeleteMessagesResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def post(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        data, error = validate_delete_payload(request.data)
        if error:
            return error
        return delete_messages_response(request, credentials, data["folder"], data["uids"])


class DeleteMessageView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="mail_messages_delete_single",
        request=None,
        parameters=[OpenApiParameter("folder", str, required=True, description="Source mailbox folder name.")],
        responses={200: DeleteMessagesResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def post(self, request, uid):
        return self._delete(request, uid, default_folder="")

    @extend_schema(
        operation_id="mail_messages_delete_single_delete",
        parameters=[OpenApiParameter("folder", str, required=False, description="Source mailbox folder name. Defaults to INBOX.")],
        responses={200: DeleteMessagesResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def delete(self, request, uid):
        return self._delete(request, uid, default_folder="INBOX")

    def _delete(self, request, uid, default_folder):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        folder = (request.query_params.get("folder") or default_folder).strip()
        data, error = validate_delete_payload({"folder": folder, "uids": [uid]})
        if error:
            return error
        return delete_messages_response(request, credentials, data["folder"], data["uids"])


class RestoreMessagesView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="mail_messages_restore",
        request=RestoreMessagesRequestSerializer,
        responses={200: RestoreMessagesResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def post(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        data, error = validate_restore_payload(request.data)
        if error:
            return error
        try:
            result = MailboxService().restore_messages_from_trash(
                credentials,
                folder=data["folder"],
                target_folder=data["target_folder"],
                uids=tuple(data["uids"]),
            )
        except MailInvalidOperationError as exc:
            return restore_invalid_operation_response(exc)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response(restore_result_payload(credentials, data["folder"], result))


class RestoreMessageView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="mail_messages_restore_single",
        request=None,
        parameters=[
            OpenApiParameter("folder", str, required=True, description="Source Trash folder name."),
            OpenApiParameter("target_folder", str, required=True, description="Restore target mailbox folder name."),
        ],
        responses={200: RestoreMessagesResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def post(self, request, uid):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        folder = (request.query_params.get("folder") or "").strip()
        target_folder = (request.query_params.get("target_folder") or "").strip()
        data, error = validate_restore_payload({"folder": folder, "target_folder": target_folder, "uids": [uid]})
        if error:
            return error
        try:
            result = MailboxService().restore_messages_from_trash(
                credentials,
                folder=data["folder"],
                target_folder=data["target_folder"],
                uids=tuple(data["uids"]),
            )
        except MailInvalidOperationError as exc:
            return restore_invalid_operation_response(exc)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        return Response(restore_result_payload(credentials, data["folder"], result))


class SendMailView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES
    parser_classes = [JSONParser, FormParser, MultiPartParser]

    @extend_schema(
        request={
            "application/json": SendMailRequestSerializer,
            "multipart/form-data": SendMailMultipartRequestSerializer,
        },
        responses={200: SendMailResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def post(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error
        is_multipart = request.content_type and request.content_type.startswith("multipart/form-data")
        data = send_form_data(request.data) if is_multipart else request.data
        serializer = SendMailRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        attachments = ()
        if is_multipart:
            attachments, error = uploaded_send_attachments(request.FILES)
            if error:
                return error
        forward_source = data.get("forward_source_message")
        if forward_source:
            forward_source = ForwardSourceMessage(
                folder=forward_source["folder"],
                uid=forward_source["uid"],
                attachment_ids=tuple(forward_source["attachment_ids"]),
            )
        send_request = SendMailRequest(
            to=tuple(data["to"]),
            cc=tuple(data.get("cc", ())),
            bcc=tuple(data.get("bcc", ())),
            reply_to=data.get("reply_to") or None,
            in_reply_to=data.get("in_reply_to", ""),
            references=tuple(data.get("references", ())),
            subject=data["subject"],
            text_body=data.get("text_body", ""),
            html_body=data.get("html_body", ""),
            from_display_name=data.get("from_display_name", ""),
            attachments=attachments,
            forward_source_message=forward_source,
        )
        try:
            message_id = MailboxService().send_mail(credentials, send_request)
        except MailForwardAttachmentNotVisibleError as exc:
            return Response({"error": "forward_attachment_not_visible", "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except MailForwardAttachmentNotFoundError as exc:
            return Response({"error": "forward_attachment_not_found", "detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except MailAttachmentLimitError as exc:
            return Response({"error": exc.code}, status=status.HTTP_400_BAD_REQUEST)
        except MailIntegrationError as exc:
            return mail_error_response(exc)
        mark_mail_index_stale_after_send(request.user, credentials.email)
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
        normalized_account_email = credentials.email.strip().lower()
        if supplied_email and supplied_email != normalized_account_email:
            return Response({"error": "account_email_mismatch"}, status=status.HTTP_403_FORBIDDEN)

        device, created = DeviceRegistration.objects.update_or_create(
            account_email=normalized_account_email,
            fcm_token=data["normalized_fcm_token"],
            defaults={
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


class AccountSummariesView(APIView):
    authentication_classes = MAILBOX_API_AUTHENTICATION_CLASSES
    permission_classes = MAILBOX_API_PERMISSION_CLASSES

    @extend_schema(
        operation_id="accounts_summaries",
        parameters=[
            OpenApiParameter(
                "fcm_token",
                str,
                required=False,
                description="Temporary MVP device-link lookup token. Alias: fcmToken.",
            ),
            OpenApiParameter(
                "fcmToken",
                str,
                required=False,
                description="Alias for fcm_token.",
            ),
        ],
        responses={200: AccountSummariesResponseSerializer, 400: ErrorSerializer, 401: ErrorSerializer, 403: ErrorSerializer, 502: ErrorSerializer, 504: ErrorSerializer},
    )
    def get(self, request):
        credentials, error = require_mailbox_credentials(request)
        if error:
            return error

        serializer = AccountsSummaryQuerySerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        fcm_token = serializer.validated_data["normalized_fcm_token"]
        account_email = credentials.email.strip().lower()

        if not DeviceRegistration.objects.filter(account_email=account_email, fcm_token=fcm_token, enabled=True).exists():
            return Response({"error": "fcm_token_not_linked"}, status=status.HTTP_403_FORBIDDEN)

        account_emails = list(
            DeviceRegistration.objects.filter(fcm_token=fcm_token, enabled=True).order_by("account_email").values_list("account_email", flat=True)
        )
        credentials_by_email = {
            credential.mailbox_email: credential
            for credential in MailboxTokenCredential.objects.filter(mailbox_email__in=account_emails).order_by("mailbox_email")
        }

        accounts = []
        service = MailboxService()
        for linked_email in account_emails:
            token_credential = credentials_by_email.get(linked_email)
            if token_credential is None:
                continue
            linked_credentials = MailboxCredentials(email=token_credential.mailbox_email, password=token_credential.get_mailbox_password())
            try:
                summary = service.get_account_summary(linked_credentials)
            except MailIntegrationError as exc:
                return mail_error_response(exc)
            accounts.append(account_summary_payload(token_credential.mailbox_email, summary))

        return Response({"accounts": accounts})


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
        mark_mail_index_stale_after_incoming(event["accountEmail"])
        try:
            result = send_mail_notification(event)
        except Exception as exc:
            return Response({"error": "notification_failed", "detail": str(exc)}, status=status.HTTP_502_BAD_GATEWAY)
        return Response(result)
