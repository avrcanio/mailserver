import io
import json
import logging
import re
import uuid
from pathlib import Path

from django.conf import settings

logger = logging.getLogger("mailops.receipt_artifacts")


class ReceiptImageInputError(Exception):
    """Invalid image upload (size, type, etc.)."""


class ReceiptArtifactStorageError(Exception):
    """Unable to persist receipt artifacts to disk."""


def receipt_allowed_content_types() -> tuple[str, ...]:
    allowed = getattr(settings, "RECEIPT_ALLOWED_CONTENT_TYPES", None)
    if allowed:
        return tuple(allowed)
    return tuple(getattr(settings, "PADDLEOCR_ALLOWED_CONTENT_TYPES", ()) or ())


def receipt_max_image_bytes() -> int:
    value = getattr(settings, "RECEIPT_MAX_IMAGE_BYTES", None)
    if value is not None:
        return int(value)
    return int(getattr(settings, "PADDLEOCR_MAX_IMAGE_BYTES", 12 * 1024 * 1024))


def receipt_artifact_root() -> str:
    root = (getattr(settings, "RECEIPT_ARTIFACT_ROOT", "") or "").strip()
    if root:
        return root
    return (getattr(settings, "PADDLEOCR_ARTIFACT_ROOT", "") or "").strip()


def validate_receipt_image(image_bytes: bytes, content_type: str) -> str:
    max_bytes = receipt_max_image_bytes()
    if len(image_bytes) > max_bytes:
        raise ReceiptImageInputError(f"Image exceeds maximum size of {max_bytes} bytes.")

    header_ct = (content_type or "").split(";")[0].strip().lower()
    allowed = receipt_allowed_content_types()
    if header_ct not in allowed:
        raise ReceiptImageInputError("Unsupported or missing image content type.")
    return header_ct


def extension_for_content_type(content_type: str) -> str:
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }
    return mapping.get((content_type or "").split(";")[0].strip().lower(), ".bin")


def _safe_account_slug(account_email: str) -> str:
    slug = (account_email or "unknown").strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "_", slug.replace("@", "_at_"))
    return slug[:80] or "unknown"


def image_bytes_to_pdf(image_bytes: bytes, content_type: str) -> bytes:
    try:
        from PIL import Image
    except ImportError as exc:
        raise ReceiptArtifactStorageError("PDF generation requires Pillow.") from exc

    with Image.open(io.BytesIO(image_bytes)) as image:
        converted = image.convert("RGB") if image.mode not in {"RGB", "L"} else image
        out = io.BytesIO()
        converted.save(out, format="PDF")
        return out.getvalue()


def persist_receipt_artifacts(
    *,
    account_email: str,
    image_bytes: bytes,
    content_type: str,
    payload: dict,
    pdf_bytes: bytes,
) -> str:
    root = receipt_artifact_root()
    if not root:
        logger.warning("RECEIPT_ARTIFACT_ROOT is not configured; skipping disk persistence.")
        return ""

    case_id = uuid.uuid4().hex[:12]
    case_dir = Path(root) / f"{_safe_account_slug(account_email)}_{case_id}"
    try:
        case_dir.mkdir(parents=True, exist_ok=False)
        ext = extension_for_content_type(content_type)
        (case_dir / f"input{ext}").write_bytes(image_bytes)
        (case_dir / "out.pdf").write_bytes(pdf_bytes)
        (case_dir / "receipt.json").write_text(
            json.dumps(payload.get("receipt") or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (case_dir / "response.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        draft = payload.get("draft") or {}
        if draft:
            (case_dir / "draft.txt").write_text(
                f"Subject: {draft.get('subject', '')}\n\n{draft.get('body', '')}",
                encoding="utf-8",
            )
    except OSError as exc:
        raise ReceiptArtifactStorageError(f"Unable to write receipt artifacts: {exc}") from exc

    return str(case_dir)
