import base64
import random
import time
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser

from django.conf import settings

from .exceptions import MailAuthError, MailConnectionError, MailProtocolError


GMAIL_USER_ID = "me"
GMAIL_PERMANENT_DELETE_SCOPE = "https://mail.google.com/"
DEFAULT_RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class GmailOAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...]


@dataclass(frozen=True)
class GmailMessageRef:
    gmail_message_id: str
    gmail_thread_id: str = ""
    history_id: str = ""
    label_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GmailRawMessage:
    gmail_message_id: str
    gmail_thread_id: str
    history_id: str
    label_ids: tuple[str, ...]
    raw_bytes: bytes
    rfc_message_id: str = ""


@dataclass(frozen=True)
class GmailHistoryMessage:
    gmail_message_id: str
    gmail_thread_id: str = ""
    history_id: str = ""
    label_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class GmailHistoryPage:
    history_id: str
    messages_added: tuple[GmailHistoryMessage, ...] = field(default_factory=tuple)
    next_page_token: str = ""


class GmailClient:
    def __init__(
        self,
        refresh_token,
        oauth_config=None,
        service=None,
        sleep=time.sleep,
        max_retries=3,
        initial_backoff_seconds=1,
    ):
        self.refresh_token = refresh_token
        self.oauth_config = oauth_config
        self._service = service
        self.sleep = sleep
        self.max_retries = int(max_retries)
        self.initial_backoff_seconds = float(initial_backoff_seconds)

    @property
    def service(self):
        if self._service is None:
            self._service = build_gmail_service(self.oauth_config or oauth_config_from_settings(), self.refresh_token)
        return self._service

    def list_message_refs(self, query="", max_results=100, page_token=""):
        request = self.service.users().messages().list(
            userId=GMAIL_USER_ID,
            q=query or None,
            maxResults=max_results,
            pageToken=page_token or None,
        )
        payload = self._execute(request, "Gmail message listing failed")
        refs = tuple(
            GmailMessageRef(
                gmail_message_id=str(item.get("id", "")),
                gmail_thread_id=str(item.get("threadId", "")),
            )
            for item in payload.get("messages", ())
            if item.get("id")
        )
        return refs, str(payload.get("nextPageToken", "") or "")

    def fetch_raw_message(self, gmail_message_id):
        request = self.service.users().messages().get(userId=GMAIL_USER_ID, id=gmail_message_id, format="raw")
        payload = self._execute(request, f"Gmail message fetch failed for {gmail_message_id}")
        raw = payload.get("raw", "")
        if not raw:
            raise MailProtocolError(f"Gmail message {gmail_message_id} did not include a raw payload")
        raw_bytes = _urlsafe_b64decode(raw)
        return GmailRawMessage(
            gmail_message_id=str(payload.get("id", gmail_message_id)),
            gmail_thread_id=str(payload.get("threadId", "")),
            history_id=str(payload.get("historyId", "")),
            label_ids=tuple(str(label) for label in payload.get("labelIds", ()) if label),
            raw_bytes=raw_bytes,
            rfc_message_id=_rfc_message_id(raw_bytes),
        )

    def list_history_page(self, start_history_id, page_token=""):
        request = self.service.users().history().list(
            userId=GMAIL_USER_ID,
            startHistoryId=str(start_history_id),
            historyTypes=["messageAdded"],
            pageToken=page_token or None,
        )
        payload = self._execute(request, f"Gmail history listing failed from {start_history_id}")
        messages = []
        for item in payload.get("history", ()):
            history_id = str(item.get("id", "") or "")
            for added in item.get("messagesAdded", ()):
                message = added.get("message", {})
                gmail_message_id = str(message.get("id", "") or "")
                if not gmail_message_id:
                    continue
                messages.append(
                    GmailHistoryMessage(
                        gmail_message_id=gmail_message_id,
                        gmail_thread_id=str(message.get("threadId", "") or ""),
                        history_id=history_id,
                        label_ids=tuple(str(label) for label in message.get("labelIds", ()) if label),
                    )
                )
        return GmailHistoryPage(
            history_id=str(payload.get("historyId", "") or ""),
            messages_added=tuple(messages),
            next_page_token=str(payload.get("nextPageToken", "") or ""),
        )

    def delete_message(self, gmail_message_id):
        request = self.service.users().messages().delete(userId=GMAIL_USER_ID, id=gmail_message_id)
        self._execute(request, f"Gmail message delete failed for {gmail_message_id}")

    def send_raw_message(self, raw_bytes):
        raw_payload = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
        request = self.service.users().messages().send(userId=GMAIL_USER_ID, body={"raw": raw_payload})
        payload = self._execute(request, "Gmail message send failed")
        gmail_message_id = str(payload.get("id", "") or "")
        if not gmail_message_id:
            raise MailProtocolError("Gmail send response did not include a message id")
        return GmailMessageRef(
            gmail_message_id=gmail_message_id,
            gmail_thread_id=str(payload.get("threadId", "") or ""),
            label_ids=tuple(str(label) for label in payload.get("labelIds", ()) if label),
        )

    def get_profile_email(self):
        request = self.service.users().getProfile(userId=GMAIL_USER_ID)
        payload = self._execute(request, "Gmail profile fetch failed")
        email = str(payload.get("emailAddress", "") or "").strip().lower()
        if not email:
            raise MailProtocolError("Gmail profile did not include an email address")
        return email

    def _execute(self, request, error_message):
        return execute_with_retry(
            request,
            error_message=error_message,
            sleep=self.sleep,
            max_retries=self.max_retries,
            initial_backoff_seconds=self.initial_backoff_seconds,
        )


def oauth_config_from_settings():
    config = GmailOAuthConfig(
        client_id=getattr(settings, "GMAIL_IMPORT_GOOGLE_CLIENT_ID", ""),
        client_secret=getattr(settings, "GMAIL_IMPORT_GOOGLE_CLIENT_SECRET", ""),
        redirect_uri=getattr(settings, "GMAIL_IMPORT_OAUTH_REDIRECT_URI", ""),
        scopes=tuple(getattr(settings, "GMAIL_IMPORT_OAUTH_SCOPES", ())),
    )
    if not config.client_id:
        raise MailProtocolError("GMAIL_IMPORT_GOOGLE_CLIENT_ID is required")
    if not config.client_secret:
        raise MailProtocolError("GMAIL_IMPORT_GOOGLE_CLIENT_SECRET is required")
    if not config.redirect_uri:
        raise MailProtocolError("GMAIL_IMPORT_OAUTH_REDIRECT_URI is required")
    if not config.scopes:
        raise MailProtocolError("GMAIL_IMPORT_OAUTH_SCOPES must include at least one scope")
    return config


def build_authorization_url(oauth_config=None, state=""):
    flow = _oauth_flow(oauth_config or oauth_config_from_settings())
    authorization_url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=state or None,
    )
    return authorization_url


def exchange_code_for_refresh_token(code, oauth_config=None):
    if not str(code or "").strip():
        raise MailProtocolError("OAuth authorization code is required")
    flow = _oauth_flow(oauth_config or oauth_config_from_settings())
    try:
        flow.fetch_token(code=str(code).strip())
    except Exception as exc:
        raise MailAuthError("Gmail OAuth token exchange failed") from exc
    refresh_token = getattr(flow.credentials, "refresh_token", "")
    if not refresh_token:
        raise MailAuthError("Gmail OAuth token exchange did not return a refresh token")
    return refresh_token


def fetch_gmail_profile_email(refresh_token, oauth_config=None):
    return GmailClient(refresh_token, oauth_config=oauth_config).get_profile_email()


def build_gmail_service(oauth_config, refresh_token):
    if not str(refresh_token or "").strip():
        raise MailAuthError("Gmail refresh token is required")
    Credentials, build = _google_service_dependencies()
    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=oauth_config.client_id,
        client_secret=oauth_config.client_secret,
        scopes=oauth_config.scopes,
    )
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def execute_with_retry(request, error_message, sleep=time.sleep, max_retries=3, initial_backoff_seconds=1):
    attempts = int(max_retries) + 1
    for attempt in range(attempts):
        try:
            return request.execute()
        except Exception as exc:
            status = _http_error_status(exc)
            if status == 401:
                raise MailAuthError(f"{error_message}: authentication failed") from exc
            if status == 403:
                raise MailAuthError(f"{error_message}: permission denied") from exc
            retryable = status in DEFAULT_RETRY_STATUSES
            if not retryable or attempt >= attempts - 1:
                if status:
                    raise MailConnectionError(f"{error_message}: Gmail API returned HTTP {status}") from exc
                raise MailConnectionError(f"{error_message}: {exc}") from exc
            sleep(_retry_delay(initial_backoff_seconds, attempt))
    raise MailConnectionError(error_message)


def _oauth_flow(oauth_config):
    Flow = _oauth_flow_dependency()
    config = {
        "web": {
            "client_id": oauth_config.client_id,
            "client_secret": oauth_config.client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [oauth_config.redirect_uri],
        }
    }
    return Flow.from_client_config(config, scopes=oauth_config.scopes, redirect_uri=oauth_config.redirect_uri)


def _oauth_flow_dependency():
    try:
        from google_auth_oauthlib.flow import Flow
    except ImportError as exc:
        raise MailProtocolError("google-auth-oauthlib is required for Gmail OAuth") from exc
    return Flow


def _google_service_dependencies():
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise MailProtocolError("google-api-python-client and google-auth are required for Gmail API access") from exc
    return Credentials, build


def _http_error_status(exc):
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    try:
        return int(status) if status else None
    except (TypeError, ValueError):
        return None


def _is_google_auth_error(exc):
    try:
        from google.auth.exceptions import GoogleAuthError
    except ImportError:
        return False
    return isinstance(exc, GoogleAuthError)


def _retry_delay(initial_backoff_seconds, attempt):
    return float(initial_backoff_seconds) * (2**attempt) + random.uniform(0, 0.1)


def _urlsafe_b64decode(value):
    payload = str(value).encode("ascii")
    payload += b"=" * (-len(payload) % 4)
    try:
        return base64.urlsafe_b64decode(payload)
    except (ValueError, TypeError) as exc:
        raise MailProtocolError("Gmail raw payload is not valid base64url data") from exc


def _rfc_message_id(raw_bytes):
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_bytes)
        return str(message.get("message-id", "") or "")
    except Exception:
        return ""
