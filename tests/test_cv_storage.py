"""CV upload through Supabase Storage (mocked HTTP), storage misconfiguration, and the
current-CV endpoints (/cv/me, /cv/status, DELETE /cv/me, ?replace=true).

The fake Supabase server below reproduces the real Storage REST API responses, including
its habit of answering HTTP 400 with the actual status inside the JSON body
(verified against a real project on 2026-11-19).
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.core.config import get_settings
from app.models.profile import CVExtractedData, UserCV, UserProfile
from tests.conftest import auth_header, make_token
from tests.documents import DOCX_MIME, PDF_MIME, make_docx, make_pdf, make_scanned_pdf

BASE = "/storage/v1"
NO_BUCKET = {"statusCode": "404", "error": "Bucket not found", "message": "Bucket not found", "code": "NoSuchBucket"}
BAD_KEY = {"statusCode": "403", "error": "Unauthorized", "message": "Invalid Compact JWS", "code": "AccessDenied"}


class FakeSupabaseStorage:
    """Minimal in-memory Supabase Storage REST API."""

    def __init__(self, buckets: dict[str, bool] | None = None, key: str = "service-role-test") -> None:
        self.buckets = buckets if buckets is not None else {"cvs": False, "media": False}  # name -> public
        self.objects: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.key = key
        self.requests: list[httpx.Request] = []
        self.fail_delete = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("Authorization") != f"Bearer {self.key}" or request.headers.get("apikey") != self.key:
            return httpx.Response(400, json=BAD_KEY)
        path = request.url.path.split(BASE, 1)[1]
        method = request.method
        if path.startswith("/bucket"):
            name = path.removeprefix("/bucket").strip("/")
            if method == "GET" and name:
                if name not in self.buckets:
                    return httpx.Response(400, json=NO_BUCKET)
                return httpx.Response(200, json={"id": name, "public": self.buckets[name],
                                                 "file_size_limit": 5242880, "allowed_mime_types": [PDF_MIME]})
            if method == "POST":
                body = json.loads(request.content)
                self.buckets[body["id"]] = body["public"]
                return httpx.Response(200, json={"name": body["id"]})
        if path.startswith("/object/sign/"):
            bucket, key = path.removeprefix("/object/sign/").split("/", 1)
            if (bucket, key) not in self.objects:
                return httpx.Response(400, json={"statusCode": "404", "error": "not_found"})
            return httpx.Response(200, json={"signedURL": f"/object/sign/{bucket}/{key}?token=signed"})
        if path.startswith("/object/authenticated/"):
            bucket, key = path.removeprefix("/object/authenticated/").split("/", 1)
            if bucket not in self.buckets:
                return httpx.Response(400, json=NO_BUCKET)
            if (bucket, key) not in self.objects:
                return httpx.Response(400, json={"statusCode": "404", "error": "not_found"})
            return httpx.Response(200, content=self.objects[(bucket, key)][0])
        if path.startswith("/object/"):
            rest = path.removeprefix("/object/")
            if method == "DELETE":
                bucket = rest.strip("/")
                if self.fail_delete:
                    return httpx.Response(500, json={"error": "internal"})
                if bucket not in self.buckets:
                    return httpx.Response(400, json=NO_BUCKET)
                for prefix in json.loads(request.content)["prefixes"]:
                    self.objects.pop((bucket, prefix), None)
                return httpx.Response(200, json=[])
            if method == "POST":
                bucket, key = rest.split("/", 1)
                if bucket not in self.buckets:
                    return httpx.Response(400, json=NO_BUCKET)
                if (bucket, key) in self.objects:
                    return httpx.Response(400, json={"statusCode": "409", "error": "Duplicate"})
                self.objects[(bucket, key)] = (request.content, request.headers["Content-Type"])
                return httpx.Response(200, json={"Key": f"{bucket}/{key}"})
        return httpx.Response(404, json={"error": "unexpected route"})


@pytest.fixture
def fake_storage() -> FakeSupabaseStorage:
    from app.services.http import set_http_client

    fake = FakeSupabaseStorage()
    set_http_client("supabase_storage", httpx.AsyncClient(transport=httpx.MockTransport(fake)))
    settings = get_settings()
    settings.STORAGE_BACKEND = "supabase"
    settings.SUPABASE_SERVICE_ROLE_KEY = SecretStr("service-role-test")
    settings.SUPABASE_STORAGE_CV_BUCKET = "cvs"
    return fake


async def upload(client, headers, data: bytes, name: str = "cv.pdf", mime: str = PDF_MIME, **params: Any):
    return await client.post("/api/v1/cv/upload", headers=headers, files={"file": (name, data, mime)}, params=params)


# ---------------- Successful uploads through Supabase Storage ----------------
async def test_pdf_upload_stored_in_supabase_and_extracted(client, user_factory, db, fake_storage):
    user, headers = await user_factory()
    r = await upload(client, headers, make_pdf())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["success"] is True and body["message"] == "CV uploaded successfully"
    data = body["data"]
    assert data["processing_status"] == "completed" and data["is_primary"] is True
    assert "storage_path" not in data
    # Review structure returned to the frontend.
    extracted = data["extracted_data"]
    assert set(extracted) >= {"skills", "experience", "education", "qualifications", "job_titles"}
    assert "Python" in extracted["skills"]
    assert extracted["experience"] and extracted["education"]
    assert extracted["is_user_confirmed"] is False
    # Object stored privately under the user's own prefix with a server-generated name.
    [(bucket, key)] = list(fake_storage.objects)
    assert bucket == "cvs" and key.startswith(f"cvs/{user.id}/") and key.endswith(".pdf")
    content, ctype = fake_storage.objects[(bucket, key)]
    assert content.startswith(b"%PDF") and ctype == PDF_MIME
    put = next(req for req in fake_storage.requests if req.method == "POST")
    assert put.headers["x-upsert"] == "false"
    # Metadata + extracted data persisted against the right user.
    cv = await db.scalar(select(UserCV).where(UserCV.id == uuid.UUID(data["id"])))
    assert cv.user_id == user.id and cv.storage_path == key and cv.file_size == len(content)
    assert cv.original_filename == "cv.pdf" and cv.file_type.value == "pdf" and cv.created_at
    stored = await db.scalar(select(CVExtractedData).where(CVExtractedData.cv_id == cv.id))
    assert "Python" in stored.skills


async def test_docx_upload_stored_in_supabase(client, user_factory, fake_storage):
    _, headers = await user_factory()
    r = await upload(client, headers, make_docx(), "My CV.docx", DOCX_MIME)
    assert r.status_code == 201, r.text
    assert r.json()["data"]["file_type"] == "docx"
    assert r.json()["data"]["extracted_data"]["skills"]
    [(_, key)] = list(fake_storage.objects)
    assert key.endswith(".docx") and " " not in key


async def test_upload_does_not_touch_profile(client, user_factory, db, fake_storage):
    user, headers = await user_factory()
    await client.put("/api/v1/profile/me", headers=headers, json={"full_name": "Keep Me", "skills": ["Rust"]})
    assert (await upload(client, headers, make_pdf())).status_code == 201
    profile = await db.scalar(select(UserProfile).where(UserProfile.user_id == user.id).execution_options(populate_existing=True))
    assert profile.full_name == "Keep Me" and profile.skills == ["Rust"]


async def test_unextractable_cv_keeps_file_and_reports_status(client, user_factory, fake_storage):
    _, headers = await user_factory()
    r = await upload(client, headers, make_scanned_pdf())
    assert r.status_code == 201
    data = r.json()["data"]
    assert data["processing_status"] == "ocr_required"
    assert data["extracted_data"] is None
    assert "could not be extracted" in r.json()["message"]
    assert len(fake_storage.objects) == 1  # file not lost
    status = (await client.get("/api/v1/cv/status", headers=headers)).json()["data"]
    assert status["has_cv"] is True and status["processing_status"] == "ocr_required"


# ---------------- Storage misconfiguration ----------------
@pytest.mark.parametrize("key", ["", "   "])
async def test_missing_service_key_returns_clear_error(client, user_factory, db, fake_storage, key):
    get_settings().SUPABASE_SERVICE_ROLE_KEY = SecretStr(key)
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf())
    assert r.status_code == 503
    err = r.json()
    assert err["success"] is False and err["error"]["code"] == "storage_not_configured"
    assert err["error"]["message"] == "File uploads aren't available right now because the server's file storage isn't set up."
    assert "SUPABASE" not in r.text  # configuration names/values are logged, never returned
    assert (await db.scalar(select(UserCV))) is None


async def test_missing_supabase_url_returns_clear_error(client, user_factory, fake_storage):
    get_settings().SUPABASE_URL = None
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf())
    assert r.status_code == 503 and r.json()["error"]["code"] == "storage_not_configured"


async def test_missing_bucket_returns_specific_error_and_no_record(client, user_factory, db, fake_storage):
    fake_storage.buckets.clear()
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf())
    assert r.status_code == 503 and r.json()["error"]["code"] == "storage_bucket_missing"
    assert (await db.scalar(select(UserCV))) is None


async def test_wrong_key_returns_auth_error(client, user_factory, fake_storage):
    get_settings().SUPABASE_SERVICE_ROLE_KEY = SecretStr("sb_publishable_wrong")
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf())
    assert r.status_code == 503 and r.json()["error"]["code"] == "storage_auth_failed"


async def test_storage_secret_never_in_responses(client, user_factory, fake_storage):
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf())
    me = await client.get("/api/v1/cv/me", headers=headers)
    for response in (r, me):
        assert "service-role-test" not in response.text and "storage_path" not in response.text


# ---------------- Storage health endpoint ----------------
async def test_storage_health_ok(client, fake_storage):
    r = await client.get("/api/v1/health/storage")
    assert r.status_code == 200
    assert r.json()["storage"] == "connected" and r.json()["backend"] == "supabase"
    assert "service-role-test" not in r.text


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda f: setattr(get_settings(), "SUPABASE_SERVICE_ROLE_KEY", None), "storage_not_configured"),
        (lambda f: f.buckets.clear(), "storage_bucket_missing"),
        (lambda f: f.buckets.update({"cvs": True}), "storage_bucket_public"),
        (lambda f: setattr(f, "key", "rotated"), "storage_auth_failed"),
    ],
)
async def test_storage_health_reports_problem(client, fake_storage, mutate, code):
    mutate(fake_storage)
    r = await client.get("/api/v1/health/storage")
    assert r.status_code == 503 and r.json()["storage"] == code


async def test_storage_health_local_backend(client):
    r = await client.get("/api/v1/health/storage")
    assert r.status_code == 200 and r.json()["backend"] == "local"


async def test_create_bucket_is_private(fake_storage):
    from app.services.storage.supabase import SupabaseStorage

    fake_storage.buckets.clear()
    s = SupabaseStorage()
    await s.create_bucket("user-cvs", file_size_limit=5 * 1024 * 1024, allowed_mime_types=[PDF_MIME])
    assert fake_storage.buckets["user-cvs"] is False
    assert (await s.check("user-cvs"))["public"] is False


# ---------------- Authentication & isolation ----------------
async def test_upload_requires_authentication(client, fake_storage):
    r = await upload(client, {}, make_pdf())
    assert r.status_code == 401
    r = await upload(client, auth_header(make_token(expires_in=-60)), make_pdf())
    assert r.status_code == 401
    r = await upload(client, {"Authorization": "Bearer not-a-jwt"}, make_pdf())
    assert r.status_code == 401
    assert fake_storage.objects == {}


@pytest.mark.parametrize("path", ["/api/v1/cv/me", "/api/v1/cv/status"])
async def test_current_cv_endpoints_require_auth(client, path):
    assert (await client.get(path)).status_code == 401
    assert (await client.delete("/api/v1/cv/me")).status_code == 401


async def test_users_cannot_see_or_delete_each_others_cv(client, user_factory, fake_storage):
    _, h1 = await user_factory()
    _, h2 = await user_factory()
    cv_id = (await upload(client, h1, make_pdf())).json()["data"]["id"]
    assert (await client.get("/api/v1/cv/me", headers=h2)).json()["data"] == {"cv": None, "extracted_data": None}
    assert (await client.get("/api/v1/cv/status", headers=h2)).json()["data"]["has_cv"] is False
    assert (await client.delete("/api/v1/cv/me", headers=h2)).status_code == 404
    assert (await client.delete(f"/api/v1/cv/{cv_id}", headers=h2)).status_code == 404
    assert (await client.get(f"/api/v1/cv/{cv_id}/download", headers=h2)).status_code == 404
    assert len(fake_storage.objects) == 1
    assert (await client.get("/api/v1/cv/me", headers=h1)).json()["data"]["cv"]["id"] == cv_id


# ---------------- /cv/me, /cv/status, DELETE /cv/me ----------------
async def test_me_and_status_without_cv(client, user_factory):
    _, headers = await user_factory()
    me = await client.get("/api/v1/cv/me", headers=headers)
    assert me.status_code == 200 and me.json()["data"] == {"cv": None, "extracted_data": None}
    st = (await client.get("/api/v1/cv/status", headers=headers)).json()["data"]
    assert st["has_cv"] is False and st["total_cvs"] == 0
    r = await client.delete("/api/v1/cv/me", headers=headers)
    assert r.status_code == 404 and r.json()["error"]["code"] == "cv_not_found"


async def test_me_status_and_review_flow(client, user_factory, fake_storage):
    _, headers = await user_factory()
    cv_id = (await upload(client, headers, make_pdf())).json()["data"]["id"]
    me = (await client.get("/api/v1/cv/me", headers=headers)).json()["data"]
    assert me["cv"]["id"] == cv_id and "Python" in me["extracted_data"]["skills"]
    st = (await client.get("/api/v1/cv/status", headers=headers)).json()["data"]
    assert st == {**st, "has_cv": True, "cv_id": cv_id, "processing_status": "completed", "is_reviewed": False, "total_cvs": 1}
    # User reviews/corrects -> status shows reviewed.
    r = await client.patch(f"/api/v1/cv/{cv_id}/extracted-data", headers=headers, json={"skills": ["Python", "SQL"]})
    assert r.status_code == 200
    assert (await client.get("/api/v1/cv/status", headers=headers)).json()["data"]["is_reviewed"] is True
    assert (await client.get("/api/v1/cv/me", headers=headers)).json()["data"]["extracted_data"]["skills"] == ["Python", "SQL"]


async def test_delete_me_removes_file_and_records(client, user_factory, db, fake_storage):
    _, headers = await user_factory()
    await upload(client, headers, make_pdf())
    r = await client.delete("/api/v1/cv/me", headers=headers)
    assert r.status_code == 200 and r.json()["message"] == "CV deleted"
    assert fake_storage.objects == {}
    assert (await db.scalar(select(UserCV))) is None
    assert (await db.scalar(select(CVExtractedData))) is None
    assert (await client.get("/api/v1/cv/status", headers=headers)).json()["data"]["has_cv"] is False


async def test_delete_me_storage_failure_keeps_record(client, user_factory, db, fake_storage):
    _, headers = await user_factory()
    await upload(client, headers, make_pdf())
    fake_storage.fail_delete = True
    r = await client.delete("/api/v1/cv/me", headers=headers)
    assert r.status_code == 503
    assert (await client.get("/api/v1/cv/status", headers=headers)).json()["data"]["has_cv"] is True


# ---------------- Duplicates & replacement ----------------
async def test_duplicate_upload_rejected(client, user_factory, fake_storage):
    _, headers = await user_factory()
    pdf = make_pdf()
    assert (await upload(client, headers, pdf)).status_code == 201
    r = await upload(client, headers, pdf, "renamed.pdf")
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_cv"
    assert len(fake_storage.objects) == 1


async def test_replace_cv(client, user_factory, db, fake_storage):
    _, headers = await user_factory()
    old_id = (await upload(client, headers, make_pdf())).json()["data"]["id"]
    r = await upload(client, headers, make_docx(), "new.docx", DOCX_MIME, replace="true")
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["replaced_cv_id"] == old_id and data["is_primary"] is True
    rows = list(await db.scalars(select(UserCV).execution_options(populate_existing=True)))
    assert [str(c.id) for c in rows] == [data["id"]]
    assert len(fake_storage.objects) == 1 and next(iter(fake_storage.objects))[1].endswith(".docx")
    assert (await client.get("/api/v1/cv/me", headers=headers)).json()["data"]["cv"]["id"] == data["id"]


async def test_replace_when_no_cv_behaves_like_upload(client, user_factory, fake_storage):
    _, headers = await user_factory()
    r = await upload(client, headers, make_pdf(), replace="true")
    assert r.status_code == 201
    assert r.json()["data"]["replaced_cv_id"] is None and r.json()["data"]["is_primary"] is True


async def test_replace_allowed_at_cv_limit(client, user_factory, fake_storage):
    get_settings().CV_MAX_FILES_PER_USER = 1
    _, headers = await user_factory()
    assert (await upload(client, headers, make_pdf())).status_code == 201
    assert (await upload(client, headers, make_docx(), "b.docx", DOCX_MIME)).status_code == 409
    assert (await upload(client, headers, make_docx(), "b.docx", DOCX_MIME, replace="true")).status_code == 201


async def test_replace_keeps_new_cv_when_old_delete_fails(client, user_factory, db, fake_storage):
    _, headers = await user_factory()
    old_id = (await upload(client, headers, make_pdf())).json()["data"]["id"]
    fake_storage.fail_delete = True
    r = await upload(client, headers, make_docx(), "b.docx", DOCX_MIME, replace="true")
    assert r.status_code == 201
    data = r.json()["data"]
    assert data["replaced_cv_id"] is None and data["is_primary"] is True
    listed = (await client.get("/api/v1/cv", headers=headers)).json()["data"]
    assert {c["id"] for c in listed} == {old_id, data["id"]}
    assert [c["id"] for c in listed if c["is_primary"]] == [data["id"]]


# ---------------- Validation through the Supabase path ----------------
@pytest.mark.parametrize(
    ("name", "content", "mime", "status", "code"),
    [
        ("cv.txt", b"plain text", "text/plain", 415, "unsupported_file_type"),
        ("cv.pdf", b"", PDF_MIME, 422, "empty_file"),
        ("cv.pdf", b"%PDF-1.4 garbage not a pdf", PDF_MIME, 422, "corrupted_file"),
        ("cv.pdf", b"PK\x03\x04 not really", PDF_MIME, 415, "invalid_file_signature"),
        ("cv.docx", b"%PDF-1.4", DOCX_MIME, 415, "invalid_file_signature"),
        ("cv.pdf", b"%PDF-1.4", "image/png", 415, "mime_mismatch"),
    ],
)
async def test_invalid_files_never_reach_storage(client, user_factory, fake_storage, name, content, mime, status, code):
    _, headers = await user_factory()
    r = await upload(client, headers, content, name, mime)
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert fake_storage.objects == {}


async def test_file_over_5mb_rejected(client, user_factory, fake_storage):
    assert get_settings().CV_MAX_FILE_SIZE_MB == 5
    _, headers = await user_factory()
    r = await upload(client, headers, b"%PDF-1.4\n" + b"0" * (5 * 1024 * 1024))
    assert r.status_code == 413 and r.json()["error"]["code"] == "file_too_large"
    assert fake_storage.objects == {}
