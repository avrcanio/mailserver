import concurrent.futures
import io
import json
import logging
import re
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


class ReceiptOCRPdfUnavailableError(Exception):
    """PDF generation is disabled or unavailable."""


_ARTIFACTS_DIR_RE = re.compile(r"(?m)^artifacts_dir=(.+)$")


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


def _read_tar_stream(stream) -> bytes:
    # docker-py returns a generator of raw tar bytes
    if stream is None:
        return b""
    chunks = []
    for chunk in stream:
        if chunk:
            chunks.append(chunk)
    return b"".join(chunks)


def _extract_single_file_from_tar(tar_bytes: bytes, expected_name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as archive:
        member = archive.getmember(expected_name)
        fileobj = archive.extractfile(member)
        if fileobj is None:
            raise ReceiptOCRPdfUnavailableError("Unable to read generated PDF from container archive.")
        return fileobj.read()

def _extract_first_regular_file_from_tar(tar_bytes: bytes) -> tuple[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            fileobj = archive.extractfile(member)
            if fileobj is None:
                continue
            return member.name, fileobj.read()
    raise ReceiptOCRPdfUnavailableError("Unable to read file from container archive.")


def _extract_artifacts_dir(stderr_bytes: bytes) -> str | None:
    if not stderr_bytes:
        return None
    value = stderr_bytes.decode("utf-8", errors="replace")
    match = _ARTIFACTS_DIR_RE.search(value)
    if not match:
        return None
    return (match.group(1) or "").strip() or None

_IMAGE_DPI_RE = re.compile(r"(?:^|\\s)--image-dpi\\s+(\\d+)(?:\\s|$)")


def _extract_image_dpi(extra_args: str) -> int | None:
    if not extra_args:
        return None
    match = _IMAGE_DPI_RE.search(str(extra_args))
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


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


def run_receipt_ocr_json_and_pdf_from_image_bytes(image_bytes: bytes, content_type: str) -> tuple[dict, bytes, str | None]:
    """
    Upload image bytes into the PaddleOCR container, run JSON OCR CLI and generate searchable PDF via ocrmypdf.

    Returns (parsed_json, pdf_bytes).
    """
    container_name = (settings.PADDLEOCR_CONTAINER_NAME or "").strip()
    if not container_name:
        raise ReceiptOCRDisabledError("Receipt OCR is not configured (set PADDLEOCR_CONTAINER_NAME).")

    script = (settings.PADDLEOCR_IMAGE_TO_R1_JSON or "").strip()
    if not script:
        raise ReceiptOCRDisabledError("Receipt OCR script path is not configured.")

    if not bool(getattr(settings, "PADDLEOCR_PDF_ENABLED", True)):
        raise ReceiptOCRPdfUnavailableError("Receipt PDF generation is disabled.")

    max_bytes = int(getattr(settings, "PADDLEOCR_MAX_IMAGE_BYTES", 12 * 1024 * 1024))
    if len(image_bytes) > max_bytes:
        raise ReceiptOCRInputError(f"Image exceeds maximum size of {max_bytes} bytes.")

    header_ct = (content_type or "").split(";")[0].strip().lower()
    allowed = getattr(settings, "PADDLEOCR_ALLOWED_CONTENT_TYPES", ())
    if header_ct not in allowed:
        raise ReceiptOCRInputError("Unsupported or missing image content type.")

    timeout = int(getattr(settings, "PADDLEOCR_EXEC_TIMEOUT_SECONDS", 120))

    unique = uuid.uuid4().hex
    image_name = f"mailadmin_receipt_{unique}{_extension_for_content_type(content_type)}"
    image_path = f"/tmp/{image_name}"
    pdf_name = f"mailadmin_receipt_{unique}.pdf"
    pdf_path = f"/tmp/{pdf_name}"

    client = _docker_client()
    try:
        container = client.containers.get(container_name)
    except docker.errors.NotFound as exc:
        raise ReceiptOCRDockerError(f"PaddleOCR container not found: {container_name!r}.") from exc
    except Exception as exc:
        raise ReceiptOCRDockerError(f"Unable to access Docker: {exc}") from exc

    try:
        tar = _tar_bytes(image_name, image_bytes)
        uploaded = container.put_archive("/tmp", tar)
        if not uploaded:
            raise ReceiptOCRDockerError("Failed to upload image into the PaddleOCR container.")

        artifact_root = (getattr(settings, "PADDLEOCR_ARTIFACT_ROOT", "") or "").strip()
        script_cmd = ["python3", script, image_path]
        if artifact_root:
            script_cmd.extend(["--artifact-root", artifact_root])
        # Let the script generate out.pdf in artifacts dir by default.
        lang = (getattr(settings, "PADDLEOCR_OCRMYPDF_LANG", "") or "").strip()
        if lang:
            script_cmd.extend(["--pdf-lang", lang])
        extra_args = (getattr(settings, "PADDLEOCR_OCRMYPDF_ARGS", "") or "").strip()
        dpi = _extract_image_dpi(extra_args)
        if dpi:
            script_cmd.extend(["--pdf-image-dpi", str(dpi)])

        exit_code, output = _exec_run_timed(container, script_cmd, timeout)
        stdout, stderr = output if isinstance(output, tuple) else (output, b"")
        artifacts_dir = _extract_artifacts_dir(stderr)
        if exit_code != 0:
            err_preview = (stderr or b"").decode("utf-8", errors="replace")[:500]
            logger.warning("image_to_r1_json failed exit_code=%s stderr_preview=%r", exit_code, err_preview)
            raise ReceiptOCRDockerError("Receipt OCR command failed.", exec_exit_code=exit_code)

        if not artifacts_dir:
            raw = (stdout or b"").decode("utf-8", errors="replace").strip()
            if not raw:
                raise ReceiptOCRInvalidOutputError("Empty output from receipt OCR command.")
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError as exc:
                logger.warning("OCR stdout is not valid JSON (prefix): %r", raw[:200])
                raise ReceiptOCRInvalidOutputError("Receipt OCR output is not valid JSON.") from exc
            raise ReceiptOCRPdfUnavailableError("Receipt PDF generation requires artifacts_dir output.")

        # When artifacts are enabled, the CLI writes parsed.json and out.pdf into artifacts_dir.
        try:
            json_stream, _ = container.get_archive(f"{artifacts_dir}/parsed.json")
            json_tar = _read_tar_stream(json_stream)
            _, json_bytes = _extract_first_regular_file_from_tar(json_tar)
            payload = json.loads(json_bytes.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ReceiptOCRInvalidOutputError("Receipt OCR output is not valid JSON.") from exc
        except ReceiptOCRInvalidOutputError:
            raise
        except Exception as exc:
            raise ReceiptOCRInvalidOutputError(f"Unable to fetch parsed.json: {exc}") from exc

        try:
            pdf_stream, _ = container.get_archive(f"{artifacts_dir}/out.pdf")
            pdf_tar = _read_tar_stream(pdf_stream)
            _, pdf_bytes = _extract_first_regular_file_from_tar(pdf_tar)
        except Exception as exc:
            raise ReceiptOCRPdfUnavailableError(f"Unable to fetch generated PDF: {exc}") from exc

        if not pdf_bytes:
            raise ReceiptOCRPdfUnavailableError("Generated PDF is empty.")
        return payload, pdf_bytes, artifacts_dir
    finally:
        try:
            container.exec_run(["rm", "-f", image_path, pdf_path])
        except Exception:
            logger.debug("Ignoring cleanup failure for %s and %s", image_path, pdf_path, exc_info=True)
