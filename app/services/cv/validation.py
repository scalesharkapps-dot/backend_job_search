"""Upload validation: size limits, extension/MIME/signature checks, image sanitising.

The client-supplied filename and Content-Type are never trusted on their own:
the actual bytes must match the declared type.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass

from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError

from app.core.exceptions import PayloadTooLargeError, UnsupportedMediaError, ValidationAppError

CHUNK = 64 * 1024

PDF_MIME = {"application/pdf", "application/x-pdf"}
DOCX_MIME = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
GENERIC_MIME = {"application/octet-stream", "binary/octet-stream", ""}
IMAGE_FORMATS = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"), "WEBP": ("webp", "image/webp")}

# Zip-bomb protection for DOCX.
MAX_DOCX_UNCOMPRESSED = 50 * 1024 * 1024
MAX_DOCX_ENTRIES = 2000
MAX_IMAGE_PIXELS = 25_000_000


@dataclass
class ValidatedFile:
    data: bytes
    extension: str
    content_type: str
    sha256: str
    original_filename: str


async def read_limited(upload: UploadFile, max_bytes: int) -> bytes:
    """Read an upload in chunks, aborting as soon as ``max_bytes`` is exceeded."""
    buffer = io.BytesIO()
    total = 0
    while True:
        chunk = await upload.read(CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise PayloadTooLargeError(
                f"File exceeds the maximum size of {max_bytes // (1024 * 1024)} MB.",
                code="file_too_large",
            )
        buffer.write(chunk)
    data = buffer.getvalue()
    if not data:
        raise ValidationAppError("The uploaded file is empty.", code="empty_file")
    return data


def safe_display_name(filename: str | None, fallback: str) -> str:
    """Reduce a client filename to a harmless display name (never used as a path)."""
    name = (filename or "").replace("\\", "/").split("/")[-1]
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '<>:"|?*').strip(" .")
    return (name or fallback)[:200]


def _extension(filename: str | None) -> str:
    name = (filename or "").lower()
    return name.rsplit(".", 1)[-1] if "." in name else ""


def _check_docx(data: bytes) -> None:
    if not data.startswith(b"PK\x03\x04"):
        raise UnsupportedMediaError("The file is not a valid DOCX document.", code="invalid_file_signature")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_DOCX_ENTRIES:
                raise ValidationAppError("The document structure is invalid.", code="corrupted_file")
            if sum(i.file_size for i in infos) > MAX_DOCX_UNCOMPRESSED:
                raise ValidationAppError("The document is too large when expanded.", code="corrupted_file")
            names = {i.filename for i in infos}
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                raise UnsupportedMediaError(
                    "The file is not a valid DOCX document.", code="invalid_file_signature"
                )
            if any(n.lower().endswith("vbaproject.bin") for n in names):
                raise UnsupportedMediaError(
                    "Documents containing macros are not accepted.", code="macro_not_allowed"
                )
            if archive.testzip() is not None:
                raise ValidationAppError("The document is corrupted.", code="corrupted_file")
    except zipfile.BadZipFile:
        raise ValidationAppError("The document is corrupted.", code="corrupted_file") from None
    try:
        import docx

        docx.Document(io.BytesIO(data))
    except Exception:
        raise ValidationAppError("The document is corrupted.", code="corrupted_file") from None


def _check_pdf(data: bytes) -> None:
    if not data[:1024].lstrip().startswith(b"%PDF-"):
        raise UnsupportedMediaError("The file is not a valid PDF document.", code="invalid_file_signature")
    import fitz

    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            if doc.page_count < 1:
                raise ValidationAppError("The PDF has no pages.", code="corrupted_file")
    except ValidationAppError:
        raise
    except Exception:
        raise ValidationAppError("The PDF is corrupted.", code="corrupted_file") from None


async def validate_cv_upload(upload: UploadFile, max_bytes: int) -> ValidatedFile:
    """Validate a CV: PDF or DOCX, matching extension, MIME and actual signature."""
    ext = _extension(upload.filename)
    if ext not in {"pdf", "docx"}:
        raise UnsupportedMediaError("Only PDF and DOCX files are supported.", code="unsupported_file_type")
    declared = (upload.content_type or "").split(";")[0].strip().lower()
    allowed = (PDF_MIME if ext == "pdf" else DOCX_MIME) | GENERIC_MIME
    if declared not in allowed:
        raise UnsupportedMediaError(
            "The file content type does not match its extension.", code="mime_mismatch"
        )
    data = await read_limited(upload, max_bytes)
    if ext == "pdf":
        _check_pdf(data)
        content_type = "application/pdf"
    else:
        _check_docx(data)
        content_type = next(iter(DOCX_MIME))
    return ValidatedFile(
        data=data,
        extension=ext,
        content_type=content_type,
        sha256=hashlib.sha256(data).hexdigest(),
        original_filename=safe_display_name(upload.filename, f"cv.{ext}"),
    )


async def validate_image_upload(upload: UploadFile, max_bytes: int) -> ValidatedFile:
    """Validate an image and re-encode it (strips EXIF/GPS metadata and payloads)."""
    declared = (upload.content_type or "").split(";")[0].strip().lower()
    if declared and declared not in {"image/jpeg", "image/png", "image/webp"} | GENERIC_MIME:
        raise UnsupportedMediaError("Only JPEG, PNG or WebP images are supported.", code="unsupported_file_type")
    data = await read_limited(upload, max_bytes)
    Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as probe:
            fmt = probe.format
            probe.verify()
        if fmt not in IMAGE_FORMATS:
            raise UnsupportedMediaError("Only JPEG, PNG or WebP images are supported.", code="unsupported_file_type")
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            if img.width * img.height > MAX_IMAGE_PIXELS:
                raise ValidationAppError("The image dimensions are too large.", code="image_too_large")
            if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            out = io.BytesIO()
            img.save(out, format=fmt)  # re-encode without metadata
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError):
        raise UnsupportedMediaError("The file is not a valid image.", code="invalid_image") from None
    clean = out.getvalue()
    ext, mime = IMAGE_FORMATS[fmt]
    return ValidatedFile(
        data=clean,
        extension=ext,
        content_type=mime,
        sha256=hashlib.sha256(clean).hexdigest(),
        original_filename=safe_display_name(upload.filename, f"image.{ext}"),
    )
