"""Text extraction from PDF (PyMuPDF) and DOCX (python-docx).

Scanned PDFs (image-only pages) are detected and reported as ``ocr_required``;
OCR itself is NOT implemented.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field

from app.models.profile import CVProcessingStatus

MIN_TEXT_CHARS = 40


@dataclass
class ExtractionResult:
    status: CVProcessingStatus
    text: str = ""
    page_count: int | None = None
    error: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​﻿]")


def normalize_text(text: str) -> str:
    """Unicode-normalise, drop control characters, collapse whitespace, keep line breaks."""
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL.sub("", text)
    text = text.replace("•", "•").replace("", "•").replace("●", "•")
    lines = [re.sub(r"[ \t ]+", " ", line).strip() for line in text.split("\n")]
    out: list[str] = []
    blank = 0
    for line in lines:
        if not line:
            blank += 1
            if blank <= 1:
                out.append("")
            continue
        blank = 0
        out.append(line)
    return "\n".join(out).strip()


def extract_pdf(data: bytes, max_pages: int) -> ExtractionResult:
    import fitz

    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return ExtractionResult(CVProcessingStatus.FAILED, error="The PDF could not be opened.")
    with doc:
        if doc.needs_pass:
            return ExtractionResult(
                CVProcessingStatus.FAILED,
                page_count=doc.page_count,
                error="The PDF is password-protected. Please upload an unprotected file.",
            )
        if doc.page_count > max_pages:
            return ExtractionResult(
                CVProcessingStatus.FAILED,
                page_count=doc.page_count,
                error=f"The PDF has more than {max_pages} pages.",
            )
        parts: list[str] = []
        image_pages = 0
        for page in doc:
            parts.append(page.get_text("text", sort=True))
            if page.get_images(full=False):
                image_pages += 1
        text = normalize_text("\n\n".join(parts))
        meta = {"pages": doc.page_count, "pages_with_images": image_pages, "extractor": "pymupdf"}
        if len(text) < MIN_TEXT_CHARS:
            if image_pages:
                return ExtractionResult(
                    CVProcessingStatus.OCR_REQUIRED,
                    text=text,
                    page_count=doc.page_count,
                    error="This appears to be a scanned PDF. Text recognition (OCR) is not supported yet.",
                    metadata=meta,
                )
            return ExtractionResult(
                CVProcessingStatus.NO_TEXT,
                text=text,
                page_count=doc.page_count,
                error="No readable text was found in the document.",
                metadata=meta,
            )
        return ExtractionResult(
            CVProcessingStatus.COMPLETED, text=text, page_count=doc.page_count, metadata=meta
        )


def extract_docx(data: bytes) -> ExtractionResult:
    import docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = docx.Document(io.BytesIO(data))
    except Exception:
        return ExtractionResult(CVProcessingStatus.FAILED, error="The document could not be opened.")

    parts: list[str] = []
    tables = 0
    # Walk the body in document order so table content stays in context.
    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            parts.append(Paragraph(child, document).text)
        elif tag == "tbl":
            tables += 1
            for row in Table(child, document).rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = cell.text.strip()
                    if value and value not in cells:  # merged cells repeat
                        cells.append(value)
                if cells:
                    parts.append(" | ".join(cells))
    text = normalize_text("\n".join(parts))
    meta = {"tables": tables, "extractor": "python-docx"}
    if len(text) < MIN_TEXT_CHARS:
        return ExtractionResult(
            CVProcessingStatus.NO_TEXT, text=text, error="No readable text was found in the document.", metadata=meta
        )
    return ExtractionResult(CVProcessingStatus.COMPLETED, text=text, metadata=meta)


def extract_text(data: bytes, file_type: str, max_pdf_pages: int) -> ExtractionResult:
    if file_type == "pdf":
        return extract_pdf(data, max_pdf_pages)
    if file_type == "docx":
        return extract_docx(data)
    return ExtractionResult(CVProcessingStatus.FAILED, error="Unsupported file type.")
