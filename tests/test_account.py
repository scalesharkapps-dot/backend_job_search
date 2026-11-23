"""E-mail verification redirects, password recovery/change and account deletion.

Supabase Auth and Storage are mocked (MockTransport / local storage). No real
account is touched; nothing here proves e-mail delivery or Dashboard settings.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from app.core.config import get_settings, load_settings
from app.models.account_deletion import DeletedAccount
from app.models.application import Application, ApplicationSource, SavedJob
from app.models.base import utcnow
from app.models.email_verification import EmailVerificationTracker
from app.models.employer import EmployerProfile, Report, ReportTargetType
from app.models.job import Job, JobSource, PostingStatus
from app.models.profile import UserCV, UserProfile
from app.models.subscription import Subscription, SubscriptionStatus, UserTrial
from app.models.user import User, UserRole
from app.services.storage.base import StorageError
from tests.conftest import auth_header, make_token
from tests.documents import make_image, make_pdf
from tests.test_auth import STRONG, session_payload

NEW = "N3wPassw0rdX"
DELETE = "/api/v1/auth/delete-account"


def requests_to(supabase, method: str, fragment: str) -> list[httpx.Request]:
    return [r for r in supabase.requests if r.method == method and fragment in str(r.url)]


@pytest.fixture
def service_role() -> None:
    get_settings().SUPABASE_SERVICE_ROLE_KEY = SecretStr("test-service-role-key")


def sign_in_ok(supabase, user: User) -> None:
    supabase.add("POST", "grant_type=password", httpx.Response(200, json=session_payload(str(user.id), user.email)))
    supabase.add("POST", "/logout", httpx.Response(204))


# ============================================================ Phase 1: e-mail verification
async def test_signup_and_resend_send_configured_redirect(client, supabase):
    get_settings().AUTH_EMAIL_REDIRECT_URL = "sharkhire://auth/callback"
    supabase.add("POST", "/signup", httpx.Response(200, json={"id": str(uuid.uuid4()), "identities": [{}]}))
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    get_settings().EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS = 0
    await client.post("/api/v1/auth/signup", json={"email": "r@example.com", "password": STRONG})
    await client.post("/api/v1/auth/resend-verification", json={"email": "r@example.com"})
    for path in ("/signup", "/resend"):
        [req] = requests_to(supabase, "POST", path)
        assert req.url.params["redirect_to"] == "sharkhire://auth/callback", path


async def test_verify_email_token_hash_marks_user_verified(client, supabase, db):
    payload = session_payload(email="verify@example.com")
    supabase.add("POST", "/auth/v1/verify", httpx.Response(200, json=payload))
    r = await client.post("/api/v1/auth/verify-email", json={"token_hash": "pkce_abcdef123456", "type": "signup"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["access_token"] and r.json()["data"]["user"]["email_verified"] is True
    sent = json.loads(requests_to(supabase, "POST", "/verify")[-1].content)
    assert sent == {"type": "signup", "token_hash": "pkce_abcdef123456"}
    assert (await db.get(User, uuid.UUID(payload["user"]["id"]))).email_verified is True


@pytest.mark.parametrize("body", [{"error_code": "otp_expired", "msg": "Email link is invalid or has expired"},
                                  {"error_code": "flow_state_expired"}])
async def test_verify_email_expired_or_reused_link(client, supabase, body):
    supabase.add("POST", "/auth/v1/verify", httpx.Response(403, json=body))
    r = await client.post("/api/v1/auth/verify-email", json={"token_hash": "used-token-1234"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_token"


async def test_verify_email_validation(client, supabase):
    assert (await client.post("/api/v1/auth/verify-email", json={"token_hash": "x"})).status_code == 422
    assert (await client.post("/api/v1/auth/verify-email", json={"token_hash": "abcdefgh", "type": "recovery"})).status_code == 422
    assert not supabase.requests


@pytest.mark.unit
def test_redirect_configuration_warnings():
    s = load_settings(_env_file=None, AUTH_EMAIL_REDIRECT_URL="", AUTH_PASSWORD_RESET_REDIRECT_URL="sharkhire")
    warnings = s.auth_redirect_warnings()
    assert any("AUTH_EMAIL_REDIRECT_URL is not set" in w for w in warnings)
    assert any("AUTH_PASSWORD_RESET_REDIRECT_URL is not an absolute" in w for w in warnings)
    prod = load_settings(_env_file=None, APP_ENV="production",
                         AUTH_EMAIL_REDIRECT_URL="http://localhost:3000/confirm",
                         AUTH_PASSWORD_RESET_REDIRECT_URL="sharkhire://auth/reset-password")
    assert prod.auth_redirect_warnings() == ["AUTH_EMAIL_REDIRECT_URL points to a local address in production"]
    ok = load_settings(_env_file=None, AUTH_EMAIL_REDIRECT_URL="sharkhire://auth/callback",
                       AUTH_PASSWORD_RESET_REDIRECT_URL="sharkhire://auth/reset-password")
    assert ok.auth_redirect_warnings() == []
    assert "localhost:3000" not in json.dumps(load_settings(_env_file=None).model_dump(mode="json"))


# ============================================================ Phase 2: forgot / reset password
async def test_forgot_password_uses_reset_redirect_and_hides_existence(client, supabase):
    get_settings().AUTH_PASSWORD_RESET_REDIRECT_URL = "sharkhire://auth/reset-password"
    supabase.add("POST", "/recover", httpx.Response(200, json={}))
    known = await client.post("/api/v1/auth/forgot-password", json={"email": "known@example.com"})
    unknown = await client.post("/api/v1/auth/forgot-password", json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    req = requests_to(supabase, "POST", "/recover")[-1]
    assert req.url.params["redirect_to"] == "sharkhire://auth/reset-password"


async def test_reset_with_reused_recovery_token(client, supabase):
    supabase.add("POST", "/verify", httpx.Response(403, json={"error_code": "otp_expired"}))
    r = await client.post("/api/v1/auth/reset-password", json={"new_password": STRONG, "token_hash": "already-used-1"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "RECOVERY_LINK_INVALID_OR_EXPIRED"
    assert not requests_to(supabase, "PUT", "/user")


# ============================================================ Phase 2: change password
async def test_change_password_success(client, supabase, user_factory):
    user, headers = await user_factory()
    sign_in_ok(supabase, user)
    supabase.add("PUT", "/auth/v1/user", httpx.Response(200, json={"id": str(user.id)}))
    r = await client.post("/api/v1/auth/change-password", headers=headers,
                          json={"current_password": STRONG, "new_password": NEW})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"password_changed": True, "session": None}
    signin = requests_to(supabase, "POST", "grant_type=password")[-1]
    assert json.loads(signin.content) == {"email": user.email, "password": STRONG}  # token owner's e-mail
    [put] = requests_to(supabase, "PUT", "/user")
    assert put.headers["Authorization"] == headers["Authorization"]  # the caller's own session
    assert json.loads(put.content) == {"password": NEW}
    assert requests_to(supabase, "POST", "/logout")  # password-check session revoked


async def test_change_password_wrong_current_password(client, supabase, user_factory):
    _, headers = await user_factory()
    supabase.add("POST", "grant_type=password", httpx.Response(400, json={"error_code": "invalid_credentials"}))
    r = await client.post("/api/v1/auth/change-password", headers=headers,
                          json={"current_password": "Wr0ngPassword", "new_password": NEW})
    assert r.status_code == 403 and r.json()["error"]["code"] == "invalid_password"
    assert not requests_to(supabase, "PUT", "/user")


@pytest.mark.parametrize(("new", "code"), [("weak", "validation_error"), (STRONG, "same_password")])
async def test_change_password_validation(client, supabase, user_factory, new, code):
    _, headers = await user_factory()
    r = await client.post("/api/v1/auth/change-password", headers=headers,
                          json={"current_password": STRONG, "new_password": new})
    assert r.status_code == 422 and r.json()["error"]["code"] == code
    assert not supabase.requests


async def test_change_password_requires_valid_session(client, supabase):
    body = {"current_password": STRONG, "new_password": NEW}
    assert (await client.post("/api/v1/auth/change-password", json=body)).status_code == 401
    expired = auth_header(make_token(expires_in=-300))
    r = await client.post("/api/v1/auth/change-password", json=body, headers=expired)
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_expired"
    assert not supabase.requests


async def test_change_password_suspended_account(client, supabase, user_factory):
    _, headers = await user_factory(is_active=False)
    r = await client.post("/api/v1/auth/change-password", headers=headers,
                          json={"current_password": STRONG, "new_password": NEW})
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_suspended"


async def test_change_password_reauthentication_fallback(client, supabase, user_factory):
    user, headers = await user_factory()
    fresh = session_payload(str(user.id), user.email)
    supabase.add("POST", "grant_type=password", httpx.Response(200, json=fresh))
    calls = {"n": 0}

    def put(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if request.headers["Authorization"] == headers["Authorization"]:
            return httpx.Response(400, json={"error_code": "reauthentication_needed"})
        return httpx.Response(200, json={"id": str(user.id)})

    supabase.add("PUT", "/auth/v1/user", put)
    r = await client.post("/api/v1/auth/change-password", headers=headers,
                          json={"current_password": STRONG, "new_password": NEW})
    assert r.status_code == 200, r.text
    assert calls["n"] == 2
    assert r.json()["data"]["session"]["access_token"] == fresh["access_token"]


async def test_change_password_supabase_session_expired(client, supabase, user_factory):
    user, headers = await user_factory()
    sign_in_ok(supabase, user)
    supabase.add("PUT", "/auth/v1/user", httpx.Response(403, json={"error_code": "session_not_found"}))
    r = await client.post("/api/v1/auth/change-password", headers=headers,
                          json={"current_password": STRONG, "new_password": NEW})
    assert r.status_code == 401 and r.json()["error"]["code"] == "invalid_session"


# ============================================================ Phase 3: account deletion
async def seed_account(client, db, user_factory, supabase, role=UserRole.JOB_SEEKER) -> dict[str, Any]:
    """A user with profile, photo, CV, saved job, own application, trial, subscription,
    a report and (for employers) a company with jobs another user applied to."""
    user, headers = await user_factory(role)
    other, other_headers = await user_factory()
    r = await client.post("/api/v1/cv/upload", headers=headers, files={"file": ("cv.pdf", make_pdf(), "application/pdf")})
    assert r.status_code == 201, r.text
    r = await client.post("/api/v1/profile/me/photo", headers=headers, files={"file": ("me.png", make_image(), "image/png")})
    assert r.status_code == 200, r.text
    ext_job = Job(source=JobSource.JSEARCH, external_id=f"ext-{uuid.uuid4()}", title="External")
    db.add(ext_job)
    await db.flush()
    db.add(SavedJob(user_id=user.id, job_id=ext_job.id))
    db.add(Application(user_id=user.id, source=ApplicationSource.MANUAL, job_title="Mine"))
    db.add(Report(reporter_id=user.id, target_type=ReportTargetType.JOB, target_id=ext_job.id, reason="spam"))
    db.add(Subscription(user_id=user.id, revenuecat_app_user_id=str(user.id), store="play_store", product_id="p",
                        subscription_status=SubscriptionStatus.ACTIVE, entitlement_active=True, will_renew=True))
    db.add(EmailVerificationTracker(email=user.email, cycle=1, cycle_started_at=utcnow()))
    jobs: dict[str, Job] = {}
    if role == UserRole.EMPLOYER:
        employer = EmployerProfile(owner_user_id=user.id, company_name="Acme", contact_email=user.email,
                                   logo_path=f"logos/{user.id}/logo.png")
        db.add(employer)
        await db.flush()
        for name, status in (("draft", PostingStatus.DRAFT), ("live", PostingStatus.PUBLISHED)):
            jobs[name] = Job(source=JobSource.SHARKHIRE, title=name, employer_id=employer.id,
                             created_by_user_id=user.id, posting_status=status)
            db.add(jobs[name])
        await db.flush()
        db.add(Application(user_id=other.id, source=ApplicationSource.SHARKHIRE, job_title="live", job_id=jobs["live"].id))
    await db.commit()
    await client.get("/api/v1/subscriptions/status", headers=headers)  # creates the trial
    return {"user": user, "headers": headers, "other": other, "other_headers": other_headers, "jobs": jobs}


def storage_files(user_id: uuid.UUID) -> list[Path]:
    root = Path(get_settings().LOCAL_STORAGE_DIR)
    return [p for p in root.rglob("*") if p.is_file() and str(user_id) in str(p)]


async def count(db, model, *where) -> int:
    return int(await db.scalar(select(func.count()).select_from(model).where(*where)))


async def test_delete_account_requires_authentication(client, supabase):
    body = {"password": STRONG, "confirmation": "DELETE"}
    assert (await client.post(DELETE, json=body)).status_code == 401
    r = await client.post(DELETE, json=body, headers=auth_header(make_token(expires_in=-300)))
    assert r.status_code == 401 and r.json()["error"]["code"] == "token_expired"
    r = await client.post(DELETE, json=body, headers=auth_header("not-a-jwt"))
    assert r.status_code == 401
    assert not supabase.requests


async def test_delete_account_requires_confirmation_and_password(client, supabase, db, user_factory, service_role):
    user, headers = await user_factory()
    r = await client.post(DELETE, headers=headers, json={"password": STRONG, "confirmation": "delete"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "confirmation_required"
    supabase.add("POST", "grant_type=password", httpx.Response(400, json={"error_code": "invalid_credentials"}))
    r = await client.post(DELETE, headers=headers, json={"password": "Wr0ngPassword", "confirmation": "DELETE"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "invalid_password"
    assert not requests_to(supabase, "DELETE", "/admin/users")
    fresh = await db.get(User, user.id, populate_existing=True)
    assert fresh is not None and fresh.is_active


async def test_admin_cannot_self_delete(client, supabase, user_factory, service_role):
    _, headers = await user_factory(UserRole.ADMIN)
    r = await client.post(DELETE, headers=headers, json={"password": STRONG, "confirmation": "DELETE"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "admin_account_deletion_not_allowed"


async def test_successful_deletion_cleans_everything(client, supabase, db, user_factory, service_role):
    seed = await seed_account(client, db, user_factory, supabase, UserRole.EMPLOYER)
    user, headers, other = seed["user"], seed["headers"], seed["other"]
    assert len(storage_files(user.id)) == 2  # CV + photo
    sign_in_ok(supabase, user)
    supabase.add("DELETE", f"/admin/users/{user.id}", httpx.Response(200, json={}))

    r = await client.post(DELETE, headers=headers, json={"password": STRONG, "confirmation": "DELETE"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True and body["message"] == "Your account has been permanently deleted."
    assert body["data"] == {"deleted": True, "user_id": str(user.id), "storage_objects_deleted": 3,
                            "active_store_subscription": True}

    # Supabase Auth: deleted with the service-role key, never the user's token.
    [admin] = requests_to(supabase, "DELETE", "/admin/users/")
    assert admin.url.path == f"/auth/v1/admin/users/{user.id}"
    assert admin.headers["Authorization"] == "Bearer test-service-role-key"
    # Storage cleanup.
    assert storage_files(user.id) == []
    # Database cleanup.
    assert await db.get(User, user.id, populate_existing=True) is None
    for model, col in ((UserProfile, UserProfile.user_id), (UserCV, UserCV.user_id), (SavedJob, SavedJob.user_id),
                       (Application, Application.user_id), (UserTrial, UserTrial.user_id),
                       (Subscription, Subscription.user_id), (EmployerProfile, EmployerProfile.owner_user_id)):
        assert await count(db, model, col == user.id) == 0, model.__tablename__
    assert await count(db, EmailVerificationTracker, EmailVerificationTracker.email == user.email) == 0
    assert await db.get(DeletedAccount, user.id, populate_existing=True) is not None
    # Other users' records are untouched; the employer's live job is archived, the unapplied draft removed.
    assert await db.get(User, other.id, populate_existing=True) is not None
    assert await count(db, Application, Application.user_id == other.id) == 1
    live = await db.get(Job, seed["jobs"]["live"].id, populate_existing=True)
    assert live is not None and live.posting_status == PostingStatus.ARCHIVED and live.employer_id is None
    assert await db.get(Job, seed["jobs"]["draft"].id, populate_existing=True) is None
    [report] = (await db.execute(select(Report).execution_options(populate_existing=True))).scalars().all()
    assert report.reporter_id is None  # shared record kept, link removed

    # The still-valid access token can no longer use or resurrect the account.
    r = await client.get("/api/v1/auth/me", headers=headers)
    assert r.status_code == 401 and r.json()["error"]["code"] == "account_deleted"
    assert await db.get(User, user.id, populate_existing=True) is None


async def test_duplicate_deletion_requests(client, supabase, db, user_factory, service_role):
    user, headers = await user_factory()
    sign_in_ok(supabase, user)
    supabase.add("DELETE", "/admin/users/", httpx.Response(200, json={}))
    body = {"password": STRONG, "confirmation": "DELETE"}
    first = await client.post(DELETE, headers=headers, json=body)
    assert first.status_code == 200
    second = await client.post(DELETE, headers=headers, json=body)
    assert second.status_code == 401 and second.json()["error"]["code"] == "account_deleted"
    assert len(requests_to(supabase, "DELETE", "/admin/users/")) == 1


async def test_concurrent_deletion_requests(client, supabase, db, user_factory, service_role):
    user, headers = await user_factory()
    sign_in_ok(supabase, user)

    async def slow_delete(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={})

    supabase.add("DELETE", "/admin/users/", slow_delete)
    body = {"password": STRONG, "confirmation": "DELETE"}
    results = await asyncio.gather(*(client.post(DELETE, headers=headers, json=body) for _ in range(3)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 1 and set(codes) <= {200, 401, 409}, codes
    assert len(requests_to(supabase, "DELETE", "/admin/users/")) == 1


async def test_storage_failure_aborts_without_deleting(client, supabase, db, user_factory, service_role, monkeypatch):
    from app.services.storage.local import LocalStorage

    seed = await seed_account(client, db, user_factory, supabase)
    user, headers = seed["user"], seed["headers"]
    sign_in_ok(supabase, user)

    async def broken(*_a: Any, **_k: Any) -> None:
        raise StorageError()

    monkeypatch.setattr(LocalStorage, "delete", broken)
    r = await client.post(DELETE, headers=headers, json={"password": STRONG, "confirmation": "DELETE"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "storage_unavailable"
    assert not requests_to(supabase, "DELETE", "/admin/users/")
    fresh = await db.get(User, user.id, populate_existing=True)
    assert fresh is not None and fresh.is_active and fresh.suspended_reason is None  # lock released
    assert (await client.get("/api/v1/cv", headers=headers)).status_code == 200  # still usable


@pytest.mark.parametrize(("response", "status", "code"), [
    (httpx.Response(500, json={"msg": "db error"}), 503, "auth_unavailable"),
    (httpx.Response(401, json={"msg": "invalid JWT"}), 503, "auth_unavailable"),
])
async def test_supabase_admin_failure_aborts(client, supabase, db, user_factory, service_role, response, status, code):
    user, headers = await user_factory()
    sign_in_ok(supabase, user)
    supabase.add("DELETE", "/admin/users/", response)
    r = await client.post(DELETE, headers=headers, json={"password": STRONG, "confirmation": "DELETE"})
    assert r.status_code == status and r.json()["error"]["code"] == code
    assert "test-service-role-key" not in r.text
    fresh = await db.get(User, user.id, populate_existing=True)
    assert fresh is not None and fresh.is_active and fresh.suspended_reason is None


async def test_supabase_user_already_gone_is_success(client, supabase, db, user_factory, service_role):
    user, headers = await user_factory()
    sign_in_ok(supabase, user)
    supabase.add("DELETE", "/admin/users/", httpx.Response(404, json={"error_code": "user_not_found"}))
    r = await client.post(DELETE, headers=headers, json={"password": STRONG, "confirmation": "DELETE"})
    assert r.status_code == 200
    assert await db.get(User, user.id, populate_existing=True) is None


async def test_deletion_not_configured(client, supabase, db, user_factory):
    user, headers = await user_factory()
    sign_in_ok(supabase, user)
    r = await client.post(DELETE, headers=headers, json={"password": STRONG, "confirmation": "DELETE"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "account_deletion_not_configured"
    assert (await db.get(User, user.id, populate_existing=True)).is_active


async def test_database_failure_after_auth_deletion_can_be_resumed(client, supabase, db, user_factory, service_role, monkeypatch):
    from app.services import account_deletion

    user, headers = await user_factory()
    sign_in_ok(supabase, user)
    supabase.add("DELETE", "/admin/users/", httpx.Response(200, json={}))
    real = account_deletion._delete_database_records

    async def broken(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(account_deletion, "_delete_database_records", broken)
    r = await client.post(DELETE, headers=headers, json={"password": STRONG, "confirmation": "DELETE"})
    assert r.status_code == 500 and r.json()["error"]["code"] == "account_deletion_incomplete"
    locked = await db.get(User, user.id, populate_existing=True)
    assert locked.is_active is False and locked.suspended_reason == account_deletion.PENDING_MARKER
    assert (await client.get("/api/v1/cv", headers=headers)).status_code == 403  # unusable meanwhile

    # Retry: the Auth user is gone, so the password can no longer be checked -- not needed to resume.
    monkeypatch.setattr(account_deletion, "_delete_database_records", real)
    supabase.add("POST", "grant_type=password", httpx.Response(400, json={"error_code": "invalid_credentials"}))
    supabase.add("DELETE", "/admin/users/", httpx.Response(404, json={"error_code": "user_not_found"}))
    r = await client.post(DELETE, headers=headers, json={"password": "anything", "confirmation": "DELETE"})
    assert r.status_code == 200, r.text
    assert await db.get(User, user.id, populate_existing=True) is None


async def test_reauthentication_must_match_token_owner(client, supabase, db, user_factory, service_role):
    user, headers = await user_factory()
    supabase.add("POST", "grant_type=password",
                 httpx.Response(200, json=session_payload(str(uuid.uuid4()), user.email)))
    r = await client.post(DELETE, headers=headers, json={"password": STRONG, "confirmation": "DELETE"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "invalid_password"
    assert not requests_to(supabase, "DELETE", "/admin/users/")
