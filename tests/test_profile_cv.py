"""Phase 3 — profile & CV management tests.

Storage: LocalStorage on a temp directory (mocked Supabase Storage tests at the
bottom use MockTransport). Database: in-memory SQLite. No real Supabase calls.
"""

from __future__ import annotations

import uuid
from datetime import date
from urllib.parse import urlparse

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.profile import CVExtractedData, UserCV
from app.services.cv.extraction import extract_docx, extract_pdf, normalize_text
from app.services.cv.parser import merged_months, parse_cv_text, parse_date_range
from tests.documents import (
    DOCX_MIME,
    PDF_MIME,
    SAMPLE_CV,
    make_docx,
    make_docx_with_macro,
    make_empty_pdf,
    make_encrypted_pdf,
    make_image,
    make_pdf,
    make_scanned_pdf,
)


async def upload(client, headers, data: bytes, name: str = "cv.pdf", mime: str = PDF_MIME):
    return await client.post("/api/v1/cv/upload", headers=headers, files={"file": (name, data, mime)})


# ---------------- Profile ----------------
async def test_get_profile_empty(client, user_factory):
    user, headers = await user_factory()
    r = await client.get("/api/v1/profile/me", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["user_id"] == str(user.id)
    assert data["profile_completion"] == 0


async def test_put_and_patch_profile(client, user_factory):
    _, headers = await user_factory()
    body = {
        "full_name": "Ayesha Khan",
        "phone": "+92 300-1234567",
        "headline": "Backend Developer",
        "country": "pk",
        "city": "Karachi",
        "professional_summary": "Backend developer with experience in APIs and databases.",
        "skills": ["python", "Postgres", "fastapi", "Underwater Basket Weaving"],
        "education": [{"degree": "BS CS", "institution": "FAST", "start_year": 2015, "end_year": 2019}],
        "experience": [{"title": "Developer", "company": "X", "start_date": "2020-01-01", "is_current": True}],
        "preferred_employment_types": ["FULLTIME"],
    }
    r = await client.put("/api/v1/profile/me", headers=headers, json=body)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["phone"] == "+923001234567"
    assert data["country"] == "PK"
    assert data["skills"] == ["Python", "PostgreSQL", "FastAPI", "Underwater Basket Weaving"]
    assert data["profile_completion"] == 85  # everything except an uploaded CV
    assert data["completion_breakdown"]["cv_uploaded"] is False

    r = await client.patch("/api/v1/profile/me", headers=headers, json={"headline": "Senior Backend Developer"})
    data = r.json()["data"]
    assert data["headline"] == "Senior Backend Developer"
    assert data["city"] == "Karachi"  # untouched by PATCH

    r = await client.put("/api/v1/profile/me", headers=headers, json={"full_name": "Ayesha"})
    assert r.json()["data"]["city"] is None  # PUT replaces


@pytest.mark.parametrize(
    "body",
    [
        {"phone": "abc"},
        {"country": "Pakistan"},
        {"years_of_experience": -1},
        {"role": "admin"},
        {"user_id": str(uuid.uuid4())},
        {"email": "x@y.com"},
        {"education": [{"start_year": 2020, "end_year": 2010, "degree": "BS"}]},
        {"preferred_employment_types": ["SOMETIMES"]},
        {"skills": ["x" * 61]},
    ],
)
async def test_invalid_profile_data(client, user_factory, body):
    _, headers = await user_factory()
    r = await client.patch("/api/v1/profile/me", headers=headers, json=body)
    assert r.status_code == 422


async def test_profile_requires_auth(client):
    assert (await client.get("/api/v1/profile/me")).status_code == 401


async def test_profile_isolation(client, user_factory):
    _, h1 = await user_factory()
    _, h2 = await user_factory()
    await client.put("/api/v1/profile/me", headers=h1, json={"full_name": "User One"})
    r = await client.get("/api/v1/profile/me", headers=h2)
    assert r.json()["data"]["full_name"] != "User One"


async def test_role_cannot_change_via_profile(client, user_factory):
    user, headers = await user_factory()
    await client.patch("/api/v1/profile/me", headers=headers, json={"role": "admin"})
    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.json()["data"]["role"] == "job_seeker"


async def test_profile_photo_lifecycle(client, user_factory):
    _, headers = await user_factory()
    r = await client.post("/api/v1/profile/me/photo", headers=headers, files={"file": ("me.png", make_image(), "image/png")})
    assert r.status_code == 200, r.text
    url = r.json()["data"]["profile_photo_url"]
    path = urlparse(url).path
    got = await client.get(path)
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    # replacing keeps one photo
    r = await client.post("/api/v1/profile/me/photo", headers=headers, files={"file": ("me.jpg", make_image("JPEG"), "image/jpeg")})
    assert r.status_code == 200
    assert (await client.delete("/api/v1/profile/me/photo", headers=headers)).status_code == 200
    assert (await client.delete("/api/v1/profile/me/photo", headers=headers)).status_code == 404


async def test_profile_photo_rejects_non_image(client, user_factory):
    _, headers = await user_factory()
    r = await client.post("/api/v1/profile/me/photo", headers=headers, files={"file": ("evil.png", b"MZ\x90\x00 executable", "image/png")})
    assert r.status_code == 415
    r = await client.post("/api/v1/profile/me/photo", headers=headers, files={"file": ("x.gif", make_image("GIF"), "image/gif")})
    assert r.status_code == 415


async def test_profile_photo_too_large(client, user_factory):
    get_settings().IMAGE_MAX_FILE_SIZE_MB = 0
    _, headers = await user_factory()
    r = await client.post("/api/v1/profile/me/photo", headers=headers, files={"file": ("a.png", make_image(), "image/png")})
    assert r.status_code == 413


# ---------------- CV upload ----------------
async def test_upload_valid_pdf(client, user_factory, db):
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf())
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["processing_status"] == "completed"
    assert data["is_primary"] is True
    assert data["skills_found"] >= 5
    assert "storage_path" not in data
    extracted = (await client.get(f"/api/v1/cv/{data['id']}/extracted-data", headers=headers)).json()["data"]
    assert "Python" in extracted["skills"]
    assert extracted["extracted_email"] == "ayesha.khan@example.com"
    assert extracted["is_user_confirmed"] is False


async def test_upload_valid_docx_with_table(client, user_factory):
    _, headers = await user_factory()
    data = make_docx(table_rows=[["Tools", "Docker, Kubernetes"], ["Cloud", "AWS"]])
    r = await upload(client, headers, data, "resume.docx", DOCX_MIME)
    assert r.status_code == 201, r.text
    cv_id = r.json()["data"]["id"]
    extracted = (await client.get(f"/api/v1/cv/{cv_id}/extracted-data", headers=headers)).json()["data"]
    assert {"Docker", "Kubernetes", "AWS", "Python"} <= set(extracted["skills"])


async def test_upload_generic_octet_stream_mime_allowed_when_signature_valid(client, user_factory):
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf(), "cv.pdf", "application/octet-stream")
    assert r.status_code == 201


@pytest.mark.parametrize(
    "name,data,mime,status,code",
    [
        ("cv.txt", b"plain text cv", "text/plain", 415, "unsupported_file_type"),
        ("cv.exe", b"MZ\x90", "application/octet-stream", 415, "unsupported_file_type"),
        ("cv.pdf", b"MZ\x90 this is an exe", PDF_MIME, 415, "invalid_file_signature"),
        ("cv.pdf", b"%PDF-1.7 broken garbage", PDF_MIME, 422, "corrupted_file"),
        ("cv.docx", b"PK\x03\x04 not really a zip", DOCX_MIME, 422, "corrupted_file"),
        ("cv.docx", b"%PDF-1.4", DOCX_MIME, 415, "invalid_file_signature"),
        ("cv.pdf", b"", PDF_MIME, 422, "empty_file"),
        ("cv.pdf", b"%PDF-1.4", "image/png", 415, "mime_mismatch"),
        ("../../etc/passwd.pdf", b"not a pdf", PDF_MIME, 415, "invalid_file_signature"),
    ],
)
async def test_upload_rejections(client, user_factory, name, data, mime, status, code):
    _, headers = await user_factory()
    r = await upload(client, headers, data, name, mime)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code


async def test_upload_docx_with_macro_rejected(client, user_factory):
    _, headers = await user_factory()
    r = await upload(client, headers, make_docx_with_macro(), "cv.docx", DOCX_MIME)
    assert r.json()["error"]["code"] == "macro_not_allowed"


async def test_upload_spoofed_extension(client, user_factory):
    _, headers = await user_factory()
    r = await upload(client, headers, make_docx(), "cv.pdf", PDF_MIME)
    assert r.status_code == 415


async def test_upload_oversized(client, user_factory):
    get_settings().CV_MAX_FILE_SIZE_MB = 0
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf())
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "file_too_large"


async def test_duplicate_upload(client, user_factory):
    _, headers = await user_factory()
    data = make_pdf()
    first = await upload(client, headers, data)
    r = await upload(client, headers, data)
    assert r.status_code == 409
    assert r.json()["error"]["details"]["cv_id"] == first.json()["data"]["id"]


async def test_cv_limit(client, user_factory):
    get_settings().CV_MAX_FILES_PER_USER = 1
    _, headers = await user_factory()
    await upload(client, headers, make_pdf())
    r = await upload(client, headers, make_pdf("Different CV content for a second upload test here."))
    assert r.json()["error"]["code"] == "cv_limit_reached"


async def test_path_traversal_filename_sanitised(client, user_factory, db):
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf(), "..\\..\\evil<script>.pdf")
    assert r.status_code == 201
    cv = await db.get(UserCV, uuid.UUID(r.json()["data"]["id"]))
    assert ".." not in cv.storage_path and "evil" not in cv.storage_path
    assert "/" not in cv.original_filename and "\\" not in cv.original_filename and "<" not in cv.original_filename


async def test_scanned_pdf_reports_ocr_required(client, user_factory):
    _, headers = await user_factory()
    r = await upload(client, headers, make_scanned_pdf())
    assert r.status_code == 201
    assert r.json()["data"]["processing_status"] == "ocr_required"


async def test_empty_pdf_reports_no_text(client, user_factory):
    _, headers = await user_factory()
    r = await upload(client, headers, make_empty_pdf())
    assert r.json()["data"]["processing_status"] == "no_text"


async def test_encrypted_pdf_fails_safely(client, user_factory):
    _, headers = await user_factory()
    r = await upload(client, headers, make_encrypted_pdf())
    assert r.status_code == 201
    data = r.json()["data"]
    assert data["processing_status"] == "failed"
    assert "password" in data["extraction_error"].lower()


# ---------------- CV management ----------------
async def test_list_get_download_delete(client, user_factory, db):
    _, headers = await user_factory()
    cv1 = (await upload(client, headers, make_pdf())).json()["data"]
    cv2 = (await upload(client, headers, make_docx(), "b.docx", DOCX_MIME)).json()["data"]
    assert cv2["is_primary"] is False
    listed = (await client.get("/api/v1/cv", headers=headers)).json()["data"]
    assert next(c["id"] for c in listed) == cv1["id"]

    dl = (await client.get(f"/api/v1/cv/{cv1['id']}/download", headers=headers)).json()["data"]
    assert dl["expires_in"] == get_settings().SIGNED_URL_TTL_SECONDS
    file_resp = await client.get(urlparse(dl["url"]).path)
    assert file_resp.status_code == 200 and file_resp.content.startswith(b"%PDF")
    assert "attachment" in file_resp.headers["content-disposition"]

    primary = await client.post(f"/api/v1/cv/{cv2['id']}/primary", headers=headers)
    assert primary.json()["data"]["is_primary"] is True
    rows = (await db.scalars(select(UserCV).where(UserCV.is_primary.is_(True)))).all()
    assert len(rows) == 1

    assert (await client.delete(f"/api/v1/cv/{cv2['id']}", headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/cv/{cv2['id']}", headers=headers)).status_code == 404
    remaining = (await client.get("/api/v1/cv", headers=headers)).json()["data"]
    assert len(remaining) == 1 and remaining[0]["is_primary"] is True  # promoted
    assert await db.scalar(select(CVExtractedData).where(CVExtractedData.cv_id == uuid.UUID(cv2["id"]))) is None


async def test_expired_or_tampered_download_link(client, user_factory):
    _, headers = await user_factory()
    cv = (await upload(client, headers, make_pdf())).json()["data"]
    get_settings().SIGNED_URL_TTL_SECONDS = -10
    dl = (await client.get(f"/api/v1/cv/{cv['id']}/download", headers=headers)).json()["data"]
    assert (await client.get(urlparse(dl["url"]).path)).status_code == 410
    get_settings().SIGNED_URL_TTL_SECONDS = 300
    dl = (await client.get(f"/api/v1/cv/{cv['id']}/download", headers=headers)).json()["data"]
    path = urlparse(dl["url"]).path
    assert (await client.get(path[:-3] + "AAA")).status_code == 404


async def test_cross_user_cv_access_denied(client, user_factory):
    _, owner = await user_factory()
    _, other = await user_factory()
    cv_id = (await upload(client, owner, make_pdf())).json()["data"]["id"]
    for method, path in [
        ("GET", f"/api/v1/cv/{cv_id}"),
        ("GET", f"/api/v1/cv/{cv_id}/download"),
        ("GET", f"/api/v1/cv/{cv_id}/extracted-data"),
        ("POST", f"/api/v1/cv/{cv_id}/process"),
        ("POST", f"/api/v1/cv/{cv_id}/primary"),
        ("DELETE", f"/api/v1/cv/{cv_id}"),
    ]:
        r = await client.request(method, path, headers=other)
        assert r.status_code == 404, (method, path)
    assert (await client.get("/api/v1/cv", headers=other)).json()["data"] == []
    assert (await client.get(f"/api/v1/cv/{cv_id}", headers=owner)).status_code == 200


async def test_reprocess_and_confirm(client, user_factory):
    _, headers = await user_factory()
    cv_id = (await upload(client, headers, make_pdf())).json()["data"]["id"]
    r = await client.patch(
        f"/api/v1/cv/{cv_id}/extracted-data", headers=headers, json={"skills": ["Python", "rust"], "years_of_experience": 5}
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["is_user_confirmed"] is True and data["skills"] == ["Python", "Rust"]
    r = await client.post(f"/api/v1/cv/{cv_id}/process", headers=headers)
    assert r.json()["data"]["processing_status"] == "completed"
    data = (await client.get(f"/api/v1/cv/{cv_id}/extracted-data", headers=headers)).json()["data"]
    assert data["is_user_confirmed"] is False


async def test_apply_to_profile(client, user_factory):
    _, headers = await user_factory()
    cv_id = (await upload(client, headers, make_pdf())).json()["data"]["id"]
    r = await client.post(f"/api/v1/cv/{cv_id}/apply-to-profile", headers=headers, json={})
    assert r.status_code == 200, r.text
    profile = r.json()["data"]
    assert "FastAPI" in profile["skills"]
    assert profile["current_job_title"] == "Senior Python Developer"
    assert profile["completion_breakdown"]["cv_uploaded"] is True
    assert profile["completion_breakdown"]["experience"] is True


async def test_storage_failure_leaves_no_record(client, user_factory, db, monkeypatch):
    from app.services.storage.base import StorageError
    from app.services.storage.local import LocalStorage

    async def fail(*_a, **_k):
        raise StorageError()

    monkeypatch.setattr(LocalStorage, "put", fail)
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf())
    assert r.status_code == 503
    assert (await db.scalars(select(UserCV))).all() == []


async def test_delete_storage_failure_keeps_record(client, user_factory, monkeypatch):
    from app.services.storage.base import StorageError
    from app.services.storage.local import LocalStorage

    _, headers = await user_factory()
    cv_id = (await upload(client, headers, make_pdf())).json()["data"]["id"]

    async def fail(*_a, **_k):
        raise StorageError()

    monkeypatch.setattr(LocalStorage, "delete", fail)
    assert (await client.delete(f"/api/v1/cv/{cv_id}", headers=headers)).status_code == 503
    assert (await client.get(f"/api/v1/cv/{cv_id}", headers=headers)).status_code == 200


async def test_db_failure_after_upload_cleans_storage(db, user_factory, tmp_path, monkeypatch):
    from app.services.cv import service as cv_service
    from app.services.cv.validation import ValidatedFile
    from app.services.storage.local import LocalStorage

    user, _ = await user_factory()
    storage = LocalStorage(str(tmp_path / "s"))

    async def broken_commit():
        raise RuntimeError("db down")

    monkeypatch.setattr(db, "commit", broken_commit)
    vf = ValidatedFile(make_pdf(), "pdf", PDF_MIME, "a" * 64, "cv.pdf")
    with pytest.raises(RuntimeError):
        await cv_service.upload_cv(db, user, storage, vf)
    assert not any(p.is_file() for p in (tmp_path / "s").rglob("*"))


# ---------------- Supabase Storage (mocked HTTP) ----------------
async def test_supabase_storage_backend_mocked(storage_upstream):
    from pydantic import SecretStr

    from app.services.storage.supabase import SupabaseStorage

    get_settings().SUPABASE_SERVICE_ROLE_KEY = SecretStr("service-role-test")
    storage_upstream.add("POST", "/storage/v1/object/sign/", httpx.Response(200, json={"signedURL": "/object/sign/cvs/a/b.pdf?token=t"}))
    storage_upstream.add("POST", "/storage/v1/object/cvs/", httpx.Response(200, json={"Key": "cvs/x"}))
    storage_upstream.add("DELETE", "/storage/v1/object/cvs", httpx.Response(200, json=[]))
    s = SupabaseStorage()
    await s.put("cvs", "cvs/abc/def.pdf", b"%PDF", PDF_MIME)
    url = await s.signed_url("cvs", "cvs/abc/def.pdf", 60, "cv.pdf")
    assert url.startswith("https://test-project.supabase.co/storage/v1/object/sign/") and "download=cv.pdf" in url
    await s.delete("cvs", "cvs/abc/def.pdf")
    assert storage_upstream.requests[0].headers["Authorization"] == "Bearer service-role-test"


async def test_supabase_storage_outage(storage_upstream):
    from pydantic import SecretStr

    from app.services.storage.base import StorageError
    from app.services.storage.supabase import SupabaseStorage

    get_settings().SUPABASE_SERVICE_ROLE_KEY = SecretStr("k")
    storage_upstream.add("POST", "/storage/v1/object/", httpx.Response(503))
    with pytest.raises(StorageError):
        await SupabaseStorage().put("cvs", "cvs/a/b.pdf", b"x", PDF_MIME)


@pytest.mark.unit
@pytest.mark.parametrize("path", ["../x.pdf", "/abs.pdf", "a\\b.pdf", "a/../../b.pdf", "noext"])
def test_object_path_validation(path):
    from app.services.storage.base import StorageError, validate_object_path

    with pytest.raises(StorageError):
        validate_object_path(path)


# ---------------- Extraction & parser (unit) ----------------
@pytest.mark.unit
def test_pdf_multipage_extraction():
    result = extract_pdf(make_pdf(pages=3), max_pages=30)
    assert result.page_count == 3
    assert result.text.count("Ayesha Khan") == 3


@pytest.mark.unit
def test_pdf_page_limit():
    result = extract_pdf(make_pdf(pages=3), max_pages=2)
    assert result.status.value == "failed"


@pytest.mark.unit
def test_docx_unicode_extraction():
    text = "Zainab Ali\nزینب علی — Développeuse\n\nSKILLS\nPython, Django, PostgreSQL"
    result = extract_docx(make_docx(text))
    assert "زینب علی" in result.text and "Développeuse" in result.text


@pytest.mark.unit
def test_normalize_text():
    assert normalize_text("a  b\r\n\n\n\nc\x00​") == "a b\n\nc"


@pytest.mark.unit
def test_parser_full_sample():
    parsed = parse_cv_text(normalize_text(SAMPLE_CV), today=date(2026, 11, 15))
    assert parsed.name == "Ayesha Khan"
    assert parsed.phone == "+923001234567"
    assert {"Python", "FastAPI", "Django", "PostgreSQL", "Machine Learning", "Git", "AWS", "Docker"} <= set(parsed.skills)
    titles = [e["title"] for e in parsed.work_experience]
    assert titles == ["Senior Python Developer", "Software Engineer"]
    assert parsed.work_experience[1]["company"] == "Systems Ltd"
    assert parsed.years_of_experience == 7.5  # Jun 2019 - Dec 2021 (31m) + Jan 2022 - Nov 2026 (59m)
    assert parsed.education[0]["institution"] == "FAST National University"
    assert parsed.education[0]["start_year"] == 2015
    assert parsed.certifications == ["AWS Certified Cloud Practitioner"]


@pytest.mark.unit
def test_parser_does_not_fabricate():
    parsed = parse_cv_text("Just some words about hobbies like gardening and travel.")
    assert parsed.name is None and parsed.email is None and parsed.phone is None
    assert parsed.work_experience == [] and parsed.education == [] and parsed.years_of_experience is None


@pytest.mark.unit
def test_parser_internships_excluded_from_years():
    text = "EXPERIENCE\nSoftware Intern at Acme\nJun 2024 - Aug 2024\n"
    parsed = parse_cv_text(text, today=date(2026, 1, 1))
    assert parsed.work_experience[0]["is_internship"] is True
    assert parsed.years_of_experience is None


@pytest.mark.unit
def test_parser_explicit_years_statement():
    parsed = parse_cv_text("SUMMARY\nData analyst with 3+ years of professional experience.\n")
    assert parsed.years_of_experience == 3.0
    assert parsed.metadata["years_of_experience_method"] == "explicit_statement"


@pytest.mark.unit
def test_education_dates_not_counted_as_experience():
    text = "EDUCATION\nBS Computer Science\nUniversity of Karachi 2015 - 2019\nSKILLS\nPython"
    parsed = parse_cv_text(text, today=date(2026, 1, 1))
    assert parsed.years_of_experience is None and parsed.work_experience == []


@pytest.mark.unit
def test_date_range_parsing_and_merge():
    today = date(2026, 11, 1)
    assert parse_date_range("Mar 2020 – Present", today)[0] == date(2020, 3, 1)
    assert parse_date_range("09/2018 to 06/2020", today)[1] == date(2020, 6, 1)
    assert parse_date_range("2017 - 2019", today)[1] == date(2019, 12, 1)
    assert parse_date_range("no dates here", today) is None
    overlapping = [(date(2020, 1, 1), date(2020, 12, 1)), (date(2020, 6, 1), date(2021, 5, 1))]
    assert merged_months(overlapping) == 17
