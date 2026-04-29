import hashlib
import json
import logging
import re
from dataclasses import dataclass
from html import unescape

from bs4 import BeautifulSoup
from bs4.element import Comment
from django.conf import settings
from django.utils.html import strip_tags

from mail_integration.mailbox_service import MailboxService

from .models import MailMessageTranslation


logger = logging.getLogger("mailops.translation")
_WHITESPACE_RE = re.compile(r"\s+")
_URL_RE = re.compile(r"(?P<url>https?://[^\s)>\]}\"']+)")
_LEADING_TRAILING_RE = re.compile(r"^(?P<leading>\s*)(?P<core>.*?)(?P<trailing>\s*)$", re.DOTALL)
_TRANSLATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["source_language", "translated_subject", "translated_text", "translated_html_segments"],
    "properties": {
        "source_language": {"type": "string"},
        "translated_subject": {"type": "string"},
        "translated_text": {"type": "string"},
        "translated_html_segments": {"type": "array", "items": {"type": "string"}},
    },
}
LANGUAGE_LABELS = {
    "hr": "Croatian",
    "en": "English",
    "es": "Spanish",
    "de": "German",
    "fr": "French",
    "it": "Italian",
    "pt": "Portuguese",
    "zh": "Mandarin Chinese",
}


class MailTranslationError(Exception):
    pass


class MailTranslationUnavailableError(MailTranslationError):
    pass


class MailTranslationFailedError(MailTranslationError):
    pass


class MailTranslationEmptyError(MailTranslationError):
    pass


@dataclass(frozen=True)
class PreparedTranslationSource:
    subject: str
    body: str
    source_hash: str
    truncated: bool


@dataclass(frozen=True)
class MailTranslationResult:
    account_email: str
    folder: str
    uid: str
    message_id: str
    target_language: str
    source_language: str
    translated_subject: str
    translated_text: str
    translated_html: str
    cached: bool
    truncated: bool
    model: str


def supported_target_languages():
    return tuple(settings.MAIL_TRANSLATION_SUPPORTED_LANGUAGES)


def normalize_target_language(value):
    language = str(value or settings.MAIL_TRANSLATION_DEFAULT_TARGET_LANGUAGE).strip()
    if language not in supported_target_languages():
        raise ValueError("invalid_target_language")
    return language


def normalize_translation_text(value):
    return _WHITESPACE_RE.sub(" ", str(value or "").strip())


def html_to_translation_text(html):
    text = unescape(strip_tags(str(html or "")))
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def protect_urls(text):
    if not text:
        return "", {}
    mapping = {}
    counter = 0

    def _replace(match):
        nonlocal counter
        url = match.group("url")
        token = f"__URL_{counter}__"
        mapping[token] = url
        counter += 1
        return token

    return _URL_RE.sub(_replace, text), mapping


def restore_protected_tokens(text, mapping):
    if not text or not mapping:
        return text or ""
    restored = str(text)
    for token, value in mapping.items():
        restored = restored.replace(token, value)
    return restored


@dataclass(frozen=True)
class HtmlSegmentSpec:
    token: str
    leading: str
    trailing: str
    url_tokens: dict[str, str]


def prepare_html_translation(html, max_chars):
    if not html:
        return "", [], {}, False
    soup = BeautifulSoup(str(html), "html.parser")
    skip_parents = {"script", "style", "noscript", "head", "title"}
    segments: list[str] = []
    specs_by_token: dict[str, HtmlSegmentSpec] = {}
    truncated = False
    total_chars = 0

    for node in list(soup.find_all(string=True)):
        if isinstance(node, Comment):
            continue
        parent = getattr(node, "parent", None)
        parent_name = getattr(parent, "name", None)
        if parent_name and str(parent_name).lower() in skip_parents:
            continue
        raw_text = str(node)
        if not raw_text or not raw_text.strip():
            continue
        match = _LEADING_TRAILING_RE.match(raw_text)
        if not match:
            continue
        leading = match.group("leading") or ""
        core = match.group("core") or ""
        trailing = match.group("trailing") or ""
        if not core.strip():
            continue
        core_protected, url_tokens = protect_urls(core)
        if max_chars > 0 and total_chars + len(core_protected) > max_chars:
            truncated = True
            continue
        token = f"__T{len(segments)}__"
        specs_by_token[token] = HtmlSegmentSpec(token=token, leading=leading, trailing=trailing, url_tokens=url_tokens)
        segments.append(core_protected)
        total_chars += len(core_protected)
        node.replace_with(token)

    return str(soup), segments, specs_by_token, truncated


def restore_translated_html(html_with_tokens, translated_segments, specs_by_token):
    if not html_with_tokens:
        return ""
    soup = BeautifulSoup(str(html_with_tokens), "html.parser")
    token_to_segment = {f"__T{i}__": translated_segments[i] for i in range(min(len(translated_segments), len(specs_by_token)))}

    for node in list(soup.find_all(string=True)):
        token = str(node)
        if token not in specs_by_token:
            continue
        spec = specs_by_token[token]
        translated = token_to_segment.get(token, "")
        restored = restore_protected_tokens(str(translated or "").strip(), spec.url_tokens)
        node.replace_with(f"{spec.leading}{restored}{spec.trailing}")

    return str(soup)


def build_translation_source_hash(subject, body):
    digest = hashlib.sha256()
    digest.update(normalize_translation_text(subject).encode("utf-8"))
    digest.update(b"\x00")
    digest.update(normalize_translation_text(body).encode("utf-8"))
    return digest.hexdigest()


def prepare_translation_source(detail, max_chars):
    subject = str(detail.subject or "").strip()
    body = str(detail.text_body or "").strip()
    if not body:
        body = html_to_translation_text(detail.html_body)
    if not subject and not body:
        raise MailTranslationEmptyError("empty_message_body")

    truncated = False
    if max_chars > 0:
        if len(subject) > max_chars:
            subject = subject[:max_chars].rstrip()
            body = ""
            truncated = True
        elif len(subject) + len(body) > max_chars:
            remaining = max_chars - len(subject)
            body = body[: max(remaining, 0)].rstrip()
            truncated = True
    return PreparedTranslationSource(
        subject=subject,
        body=body,
        source_hash=build_translation_source_hash(subject, body),
        truncated=truncated,
    )


class MailTranslationService:
    def __init__(self, mailbox_service=None, openai_client=None):
        self.mailbox_service = mailbox_service or MailboxService()
        self.openai_client = openai_client

    def translate_message(self, *, user, credentials, folder, uid, target_language=None):
        target_language = normalize_target_language(target_language)
        detail = self.mailbox_service.get_message_detail(credentials, folder=folder, uid=uid)
        source = prepare_translation_source(detail, settings.MAIL_TRANSLATION_MAX_INPUT_CHARS)
        cache_row = MailMessageTranslation.objects.filter(
            user=user,
            account_email=credentials.email,
            folder=folder,
            uid=str(uid),
            target_language=target_language,
            source_hash=source.source_hash,
        ).first()
        if cache_row is not None:
            if cache_row.translated_html or not (getattr(detail, "html_body", "") or ""):
                logger.info(
                    "Mail translation cache hit user=%s account=%s folder=%s uid=%s target=%s model=%s",
                    user.pk,
                    credentials.email,
                    folder,
                    uid,
                    target_language,
                    cache_row.model,
                )
                return self._result_from_row(cache_row, cached=True)
            logger.info(
                "Mail translation cache partial miss user=%s account=%s folder=%s uid=%s target=%s",
                user.pk,
                credentials.email,
                folder,
                uid,
                target_language,
            )
            translated = self._translate_with_openai(detail, source, target_language)
            cache_row.source_language = translated["source_language"]
            cache_row.translated_subject = translated["translated_subject"]
            cache_row.translated_text = translated["translated_text"]
            cache_row.translated_html = translated["translated_html"]
            cache_row.truncated = translated["truncated"]
            cache_row.model = settings.OPENAI_TRANSLATION_MODEL
            cache_row.save(update_fields=["source_language", "translated_subject", "translated_text", "translated_html", "truncated", "model", "updated_at"])
            return self._result_from_row(cache_row, cached=False)

        logger.info(
            "Mail translation cache miss user=%s account=%s folder=%s uid=%s target=%s",
            user.pk,
            credentials.email,
            folder,
            uid,
            target_language,
        )
        translated = self._translate_with_openai(detail, source, target_language)
        row, created = MailMessageTranslation.objects.get_or_create(
            user=user,
            account_email=credentials.email,
            folder=folder,
            uid=str(uid),
            target_language=target_language,
            source_hash=source.source_hash,
            defaults={
                "message_id": str(detail.message_id or "").strip(),
                "source_language": translated["source_language"],
                "translated_subject": translated["translated_subject"],
                "translated_text": translated["translated_text"],
                "translated_html": translated["translated_html"],
                "model": settings.OPENAI_TRANSLATION_MODEL,
                "truncated": translated["truncated"],
            },
        )
        if not created:
            logger.info(
                "Mail translation cache filled concurrently user=%s account=%s folder=%s uid=%s target=%s model=%s",
                user.pk,
                credentials.email,
                folder,
                uid,
                target_language,
                row.model,
            )
            return self._result_from_row(row, cached=True)
        return self._result_from_row(row, cached=False)

    def _result_from_row(self, row, cached):
        return MailTranslationResult(
            account_email=row.account_email,
            folder=row.folder,
            uid=row.uid,
            message_id=row.message_id,
            target_language=row.target_language,
            source_language=row.source_language,
            translated_subject=row.translated_subject,
            translated_text=row.translated_text,
            translated_html=row.translated_html,
            cached=cached,
            truncated=row.truncated,
            model=row.model,
        )

    def _translate_with_openai(self, detail, source, target_language):
        client = self.openai_client or self._build_openai_client()
        language_label = LANGUAGE_LABELS.get(target_language, target_language)
        protected_subject, subject_tokens = protect_urls(source.subject)
        protected_body, body_tokens = protect_urls(source.body)
        protected_tokens = {}
        protected_tokens.update(subject_tokens)
        protected_tokens.update(body_tokens)
        html_with_tokens, html_segments, html_specs_by_token, html_truncated = prepare_html_translation(
            getattr(detail, "html_body", "") or "",
            settings.MAIL_TRANSLATION_MAX_INPUT_CHARS,
        )
        try:
            response = client.responses.create(
                model=settings.OPENAI_TRANSLATION_MODEL,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You translate email content. Translate only the provided subject and body into the "
                            f"requested target language ({language_label}). Preserve names, numbers, formatting, "
                            "and business tone. Do not add commentary. URLs and placeholder tokens like __URL_0__ must be "
                            "copied verbatim and never modified. If HTML segments are provided, translate each segment "
                            "independently and return an array with the same length. Return empty strings when the source field is empty."
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "target_language": target_language,
                                "subject": protected_subject,
                                "body": protected_body,
                                "html_segments": html_segments,
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "mail_translation",
                        "strict": True,
                        "schema": _TRANSLATION_SCHEMA,
                    }
                },
            )
        except Exception as exc:
            logger.warning("Mail translation failed target=%s model=%s error=%s", target_language, settings.OPENAI_TRANSLATION_MODEL, exc)
            raise MailTranslationFailedError("translation_failed") from exc
        parsed = self._parse_response_json(response)
        parsed_segments = parsed.get("translated_html_segments") or []
        if not isinstance(parsed_segments, list):
            parsed_segments = []
        translated_html = restore_translated_html(html_with_tokens, parsed_segments, html_specs_by_token)
        truncated = bool(source.truncated or html_truncated or (html_segments and len(parsed_segments) < len(html_segments)))
        return {
            "source_language": str(parsed.get("source_language") or "").strip().lower(),
            "translated_subject": restore_protected_tokens(str(parsed.get("translated_subject") or "").strip(), protected_tokens),
            "translated_text": restore_protected_tokens(str(parsed.get("translated_text") or "").strip(), protected_tokens),
            "translated_html": translated_html,
            "truncated": truncated,
        }

    def _build_openai_client(self):
        if not settings.OPENAI_API_KEY:
            raise MailTranslationUnavailableError("translation_unavailable")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise MailTranslationUnavailableError("translation_unavailable") from exc
        return OpenAI(api_key=settings.OPENAI_API_KEY, timeout=settings.OPENAI_TRANSLATION_TIMEOUT_SECONDS)

    def _parse_response_json(self, response):
        output_text = getattr(response, "output_text", "") or ""
        if not output_text:
            raise MailTranslationFailedError("translation_failed")
        try:
            return json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise MailTranslationFailedError("translation_failed") from exc
