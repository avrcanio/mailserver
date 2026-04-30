import json
import logging
import time

from django.conf import settings

logger = logging.getLogger("mailops.receipt_draft")


_DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
}

_R1_RECEIPT_SCHEMA = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "invoice": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "document_type": {"type": "string"},
                "number": {"type": "string"},
                "number_display": {"type": "string"},
                "issue_date": {"type": "string"},
                "currency": {"type": "string"},
            },
            "required": ["document_type", "number", "number_display", "issue_date", "currency"],
        },
        "seller": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "name": {"type": "string"},
                "oib": {"type": "string"},
            },
            "required": ["name", "oib"],
        },
        "buyer": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "name": {"type": "string"},
                "street": {"type": "string"},
                "postal_code": {"type": "string"},
                "city": {"type": "string"},
                "country": {"type": "string"},
                "country_name": {"type": "string"},
                "address_single_line": {"type": "string"},
                "oib": {"type": "string"},
            },
            "required": ["name", "street", "postal_code", "city", "country", "country_name", "address_single_line", "oib"],
        },
        "lines": {"type": "array"},
        "totals": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "net": {"type": ["number", "null"]},
                "tax": {"type": ["number", "null"]},
                "gross": {"type": ["number", "null"]},
            },
            "required": ["net", "tax", "gross"],
        },
        "tax_summary": {"type": "object"},
        "payment": {"type": "object"},
        "validation": {"type": "object"},
    },
    "required": ["invoice", "seller", "buyer", "lines", "totals"],
}

_RECEIPT_AND_DRAFT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "receipt": _R1_RECEIPT_SCHEMA,
        "draft": _DRAFT_SCHEMA,
    },
    "required": ["receipt", "draft"],
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

    def create_receipt_and_draft(self, *, ocr_text: str) -> dict:
        client = self.openai_client or self._build_openai_client()
        model = settings.OPENAI_RECEIPT_DRAFT_MODEL or settings.OPENAI_TRANSLATION_MODEL
        max_chars = int(getattr(settings, "OPENAI_RECEIPT_DRAFT_MAX_INPUT_CHARS", 12000) or 12000)
        payload = {
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
                            "You extract Croatian receipt data and draft an email for bookkeeping. "
                            "Return ONLY JSON matching the provided schema: {receipt, draft}. "
                            "The receipt object must follow the R-1 schema. Use empty strings/nulls when values are missing. "
                            "Prefer OCR text as the primary source and do not hallucinate."
                        ),
                    },
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "receipt_and_email_draft",
                        "strict": True,
                        "schema": _RECEIPT_AND_DRAFT_SCHEMA,
                    }
                },
            )
        except Exception as exc:
            logger.warning("Receipt draft failed model=%s error=%s", model, exc)
            raise ReceiptDraftFailedError("openai_failed") from exc
        duration_ms = int((time.monotonic() - start) * 1000)
        parsed = self._parse_response_json(response)
        receipt = parsed.get("receipt") if isinstance(parsed, dict) else None
        draft = parsed.get("draft") if isinstance(parsed, dict) else None
        if not isinstance(receipt, dict) or not isinstance(draft, dict):
            raise ReceiptDraftFailedError("openai_failed")
        subject = str(draft.get("subject") or "").strip()
        body = str(draft.get("body") or "").strip()
        return {
            "receipt": receipt,
            "draft": {"subject": subject, "body": body},
            "model": model,
            "duration_ms": duration_ms,
        }

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

