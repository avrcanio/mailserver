import concurrent.futures
import io
import json
import logging
import tarfile
import uuid

import docker
from django.conf import settings

logger = logging.getLogger("mailops.paddleocr_receipt")


class ReceiptOCRDisabledError(Exception):
    """PaddleOCR integration is not configured or unavailable."""


class ReceiptOCRInputError(Exception):
    """Invalid image upload (size, type, etc.)."""


class ReceiptOCRDockerError(Exception):
    """Docker or PaddleOCR container command failed."""

    def __init__(self, message, *, exec_exit_code=None):
        super().__init__(message)
        self.exec_exit_code = exec_exit_code


class ReceiptOCRInvalidOutputError(Exception):
    """OCR pipeline did not return valid JSON on stdout."""


class ReceiptOCRTimeoutError(Exception):
    """OCR exec exceeded the configured timeout."""


def _extension_for_content_type(content_type: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
    }
    return mapping.get(ct, ".bin")


def _tar_bytes(filename: str, data: bytes) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        info = tarfile.TarInfo(name=filename)
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    stream.seek(0)
    return stream.read()


def _docker_client():
    return docker.DockerClient(base_url="unix:///var/run/docker.sock")


def _exec_run_timed(container, cmd, timeout_seconds):
    def run():
        return container.exec_run(cmd, demux=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            return future.result(timeout=timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            raise ReceiptOCRTimeoutError("Receipt OCR command exceeded the configured timeout.") from exc


def run_receipt_ocr_from_image_bytes(image_bytes: bytes, content_type: str) -> dict:
    """
    Upload image bytes into the PaddleOCR container under /tmp, run image_to_r1_json.py, return parsed JSON.
    """
    container_name = (settings.PADDLEOCR_CONTAINER_NAME or "").strip()
    if not container_name:
        raise ReceiptOCRDisabledError("Receipt OCR is not configured (set PADDLEOCR_CONTAINER_NAME).")

    script = (settings.PADDLEOCR_IMAGE_TO_R1_JSON or "").strip()
    if not script:
        raise ReceiptOCRDisabledError("Receipt OCR script path is not configured.")

    max_bytes = int(getattr(settings, "PADDLEOCR_MAX_IMAGE_BYTES", 12 * 1024 * 1024))
    if len(image_bytes) > max_bytes:
        raise ReceiptOCRInputError(f"Image exceeds maximum size of {max_bytes} bytes.")

    header_ct = (content_type or "").split(";")[0].strip().lower()
    allowed = getattr(settings, "PADDLEOCR_ALLOWED_CONTENT_TYPES", ())
    if header_ct not in allowed:
        raise ReceiptOCRInputError("Unsupported or missing image content type.")

    timeout = int(getattr(settings, "PADDLEOCR_EXEC_TIMEOUT_SECONDS", 120))

    unique = uuid.uuid4().hex
    inner_name = f"mailadmin_receipt_{unique}{_extension_for_content_type(content_type)}"
    container_path = f"/tmp/{inner_name}"

    client = _docker_client()
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound as exc:
        raise ReceiptOCRDockerError(f"PaddleOCR container not found: {container_name!r}.") from exc
    except Exception as exc:
        raise ReceiptOCRDockerError(f"Unable to access Docker: {exc}") from exc

    try:
        tar = _tar_bytes(inner_name, image_bytes)
        uploaded = container.put_archive("/tmp", tar)
        if not uploaded:
            raise ReceiptOCRDockerError("Failed to upload image into the PaddleOCR container.")
    except ReceiptOCRDockerError:
        raise
    except Exception as exc:
        raise ReceiptOCRDockerError(f"Failed to upload image: {exc}") from exc

    cmd = ["python3", script, container_path]
    try:
        exit_code, output = _exec_run_timed(container, cmd, timeout)
    except ReceiptOCRTimeoutError:
        raise
    except Exception as exc:
        raise ReceiptOCRDockerError(f"Docker exec failed: {exc}") from exc
    finally:
        try:
            container.exec_run(["rm", "-f", container_path])
        except Exception:
            logger.debug("Ignoring cleanup failure for %s", container_path, exc_info=True)

    stdout, stderr = output if isinstance(output, tuple) else (output, b"")
    if exit_code != 0:
        err_preview = (stderr or b"").decode("utf-8", errors="replace")[:500]
        logger.warning("image_to_r1_json failed exit_code=%s stderr_preview=%r", exit_code, err_preview)
        raise ReceiptOCRDockerError("Receipt OCR command failed.", exec_exit_code=exit_code)

    raw = (stdout or b"").decode("utf-8", errors="replace").strip()
    if not raw:
        raise ReceiptOCRInvalidOutputError("Empty output from receipt OCR command.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("OCR stdout is not valid JSON (prefix): %r", raw[:200])
        raise ReceiptOCRInvalidOutputError("Receipt OCR output is not valid JSON.") from exc
