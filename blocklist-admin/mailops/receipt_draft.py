import json
import logging
import time

from django.conf import settings

logger = logging.getLogger("mailops.receipt_draft")


_RECEIPT_DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
}


class ReceiptDraftUnavailableError(RuntimeError):
    pass


class ReceiptDraftFailedError(RuntimeError):
    pass


def _clamp_text(value: str, max_chars: int) -> str:
    text = (value or "").strip()
    if max_chars and len(text) > max_chars:
        return text[:max_chars]
    return text


class ReceiptDraftService:
    def __init__(self, openai_client=None):
        self.openai_client = openai_client

    def create_draft(self, *, receipt: dict, ocr_text: str) -> dict:
        client = self.openai_client or self._build_openai_client()
        model = settings.OPENAI_RECEIPT_DRAFT_MODEL or settings.OPENAI_TRANSLATION_MODEL
        max_chars = int(getattr(settings, "OPENAI_RECEIPT_DRAFT_MAX_INPUT_CHARS", 12000) or 12000)
        payload = {
            "receipt": receipt or {},
            "ocr_text": _clamp_text(ocr_text or "", max_chars),
        }
        start = time.monotonic()
        try:
            response = client.responses.create(
                model=model,
                input=[
                    {
                        "role": "system",
                        "content": (
                            "You draft an email (Croatian) for forwarding a receipt for bookkeeping. "
                            "Return ONLY JSON with keys subject and body. "
                            "Be concise and professional. Use receipt fields if present; otherwise infer from OCR text. "
                            "If a value is missing, omit the line rather than hallucinating."
                        ),
                    },
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "receipt_email_draft",
                        "strict": True,
                        "schema": _RECEIPT_DRAFT_SCHEMA,
                    }
                },
            )
        except Exception as exc:
            logger.warning("Receipt draft failed model=%s error=%s", model, exc)
            raise ReceiptDraftFailedError("openai_failed") from exc
        duration_ms = int((time.monotonic() - start) * 1000)
        parsed = self._parse_response_json(response)
        subject = str(parsed.get("subject") or "").strip()
        body = str(parsed.get("body") or "").strip()
        return {"subject": subject, "body": body, "model": model, "duration_ms": duration_ms}

    def _build_openai_client(self):
        if not settings.OPENAI_API_KEY:
            raise ReceiptDraftUnavailableError("openai_unavailable")
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise ReceiptDraftUnavailableError("openai_unavailable") from exc
        timeout = int(settings.OPENAI_RECEIPT_DRAFT_TIMEOUT_SECONDS or settings.OPENAI_TRANSLATION_TIMEOUT_SECONDS)
        return OpenAI(api_key=settings.OPENAI_API_KEY, timeout=timeout)

    def _parse_response_json(self, response):
        output_text = getattr(response, "output_text", "") or ""
        if not output_text:
            raise ReceiptDraftFailedError("openai_failed")
        try:
            return json.loads(output_text)
        except json.JSONDecodeError as exc:
            raise ReceiptDraftFailedError("openai_failed") from exc

