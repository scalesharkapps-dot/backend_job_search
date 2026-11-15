"""Generate real PDF/DOCX/image fixtures in memory for tests."""

from __future__ import annotations

import io
import zipfile

import fitz
from docx import Document
from PIL import Image

SAMPLE_CV = """Ayesha Khan
ayesha.khan@example.com | +92 300 1234567 | Karachi, Pakistan

PROFESSIONAL SUMMARY
Backend developer building APIs with Python.

EXPERIENCE
Senior Python Developer at TechCorp Pvt Ltd
Jan 2022 - Present
Built FastAPI services on AWS with Docker.
Software Engineer | Systems Ltd | Jun 2019 - Dec 2021

EDUCATION
BS Computer Science
FAST National University, 2015 - 2019

SKILLS
Python, FastAPI, Django, PostgreSQL, Machine Learning, Git

CERTIFICATIONS
AWS Certified Cloud Practitioner
"""


def make_pdf(text: str = SAMPLE_CV, pages: int = 1) -> bytes:
    doc = fitz.open()
    chunks = [text] if pages == 1 else [f"Page {i + 1}\n" + text for i in range(pages)]
    for chunk in chunks:
        page = doc.new_page()
        page.insert_text((50, 60), chunk, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


def make_scanned_pdf() -> bytes:
    img = Image.new("RGB", (400, 200), "white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(0, 0, 400, 200), stream=buf.getvalue())
    data = doc.tobytes()
    doc.close()
    return data


def make_encrypted_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page().insert_text((50, 60), SAMPLE_CV)
    data = doc.tobytes(encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="user")
    doc.close()
    return data


def make_empty_pdf() -> bytes:
    doc = fitz.open()
    doc.new_page()
    data = doc.tobytes()
    doc.close()
    return data


def make_docx(text: str = SAMPLE_CV, table_rows: list[list[str]] | None = None) -> bytes:
    document = Document()
    for line in text.split("\n"):
        document.add_paragraph(line)
    if table_rows:
        table = document.add_table(rows=len(table_rows), cols=len(table_rows[0]))
        for r, row in enumerate(table_rows):
            for c, value in enumerate(row):
                table.cell(r, c).text = value
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_docx_with_macro() -> bytes:
    src = zipfile.ZipFile(io.BytesIO(make_docx()))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("word/vbaProject.bin", b"macro")
    return out.getvalue()


def make_image(fmt: str = "PNG", size: tuple[int, int] = (64, 64)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, "blue").save(buf, format=fmt)
    return buf.getvalue()


PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
