"""Signup verification e-mail: resend limit, cooldown, persistence and concurrency.

Supabase Auth is mocked (MockTransport). These tests prove the backend's policy
enforcement and the exact requests sent to Supabase -- they do NOT prove that a
real e-mail is delivered.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.core.database import set_engine
from app.core.rate_limit import limiter
from app.models.base import utcnow as real_utcnow
from app.models.email_verification import EmailVerificationTracker
from app.models.user import User
from app.services import email_verification as ev
from app.services.http import close_http_clients, set_http_client
from tests.conftest import TEST_DATABASE_URL

STRONG = "Str0ngPassw0rd"
LIMIT_MSG = "You've reached the maximum resend limit. Please try again later."
RESEND = "/api/v1/auth/resend-verification"
SIGNUP = "/api/v1/auth/signup"


@pytest.fixture(autouse=True)
def _no_cooldown() -> None:
    # Most tests exercise the count limit; the cooldown has dedicated tests.
    get_settings().EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS = 0
    get_settings().RATE_LIMIT_EMAIL_PER_HOUR = 50


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state = {"offset": timedelta(0)}
    monkeypatch.setattr(ev, "utcnow", lambda: real_utcnow() + state["offset"])
    return state


def confirmation_payload(email: str, user_id: str | None = None) -> dict[str, Any]:
    """Supabase /signup response when e-mail confirmation is required."""
    return {
        "id": user_id or str(uuid.uuid4()),
        "email": email,
        "confirmation_sent_at": "2026-11-19T00:00:00Z",
        "identities": [{"identity_id": str(uuid.uuid4()), "provider": "email"}],
    }


def resend_calls(supabase: Any) -> list[httpx.Request]:
    return [r for r in supabase.requests if r.url.path.endswith("/resend")]


def signup_calls(supabase: Any) -> list[httpx.Request]:
    return [r for r in supabase.requests if r.url.path.endswith("/signup")]


async def resend(client: httpx.AsyncClient, email: str) -> httpx.Response:
    return await client.post(RESEND, json={"email": email})


async def tracker_for(db: Any, email: str) -> EmailVerificationTracker | None:
    return (
        await db.execute(
            select(EmailVerificationTracker)
            .where(EmailVerificationTracker.email == email.lower())
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


# 1 ---------------------------------------------------------------- initial e-mail
async def test_initial_verification_email_is_tracked(client, supabase, db):
    uid = str(uuid.uuid4())
    supabase.add("POST", "/signup", httpx.Response(200, json=confirmation_payload("new@example.com", uid)))
    r = await client.post(SIGNUP, json={"email": "New@Example.com", "password": STRONG})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["email_confirmation_required"] is True
    assert len(signup_calls(supabase)) == 1

    tracker = await tracker_for(db, "new@example.com")
    assert tracker is not None
    assert tracker.initial_sent_at is not None and tracker.last_sent_at is not None
    assert tracker.resend_count == 0 and tracker.cycle == 1
    assert str(tracker.user_id) == uid


async def test_autoconfirmed_signup_marks_verified(client, supabase, db):
    from tests.test_auth import session_payload

    supabase.add("POST", "/signup", httpx.Response(200, json=session_payload(email="auto@example.com")))
    r = await client.post(SIGNUP, json={"email": "auto@example.com", "password": STRONG})
    assert r.status_code == 201
    r = await resend(client, "auto@example.com")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_already_verified"
    assert not resend_calls(supabase)


# 2-5 -------------------------------------------------------- 2 resends, 3rd rejected
async def test_two_resends_then_exact_limit_message(client, supabase, db):
    supabase.add("POST", "/signup", httpx.Response(200, json=confirmation_payload("a@example.com")))
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    assert (await client.post(SIGNUP, json={"email": "a@example.com", "password": STRONG})).status_code == 201

    first = await resend(client, "a@example.com")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["success"] is True
    assert body["message"]
    assert body["data"] == {"remaining_attempts": 1, "max_resends": 2, "cooldown_seconds": 0}

    second = await resend(client, "a@example.com")
    assert second.status_code == 200
    assert second.json()["data"]["remaining_attempts"] == 0

    third = await resend(client, "a@example.com")
    assert third.status_code == 429
    body = third.json()
    assert body["success"] is False
    assert body["message"] == LIMIT_MSG
    assert body["error"]["message"] == LIMIT_MSG
    assert body["error"]["code"] == "resend_limit_reached"
    assert body["data"]["remaining_attempts"] == 0
    assert body["error"]["details"]["remaining_attempts"] == 0
    assert int(third.headers["Retry-After"]) > 0  # until the next cycle (24 h default)

    # Exactly 1 initial + 2 resends reached Supabase.
    assert len(signup_calls(supabase)) == 1
    assert len(resend_calls(supabase)) == 2
    assert (await tracker_for(db, "a@example.com")).resend_count == 2


async def test_resend_request_shape_and_redirect(client, supabase):
    """Root-cause regression: GoTrue reads the redirect from the redirect_to query parameter."""
    get_settings().AUTH_EMAIL_REDIRECT_URL = "sharkhire://auth/callback"
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    assert (await resend(client, "shape@example.com")).status_code == 200
    req = resend_calls(supabase)[-1]
    assert req.url.params["redirect_to"] == "sharkhire://auth/callback"
    assert json.loads(req.content) == {"type": "signup", "email": "shape@example.com"}
    assert req.headers["apikey"] == "test-anon-key"


async def test_limit_without_prior_signup_tracking(client, supabase):
    """Accounts created before tracking existed are limited too (row created lazily)."""
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    codes = [(await resend(client, "legacy@example.com")).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]
    assert len(resend_calls(supabase)) == 2


# 6 -------------------------------------------------- Supabase failures are not counted
@pytest.mark.parametrize(
    "response,status",
    [
        (httpx.Response(500, json={"msg": "smtp failure"}), 503),
        (httpx.Response(429, json={"error_code": "over_email_send_rate_limit", "msg": "email rate limit exceeded"}), 429),
        (httpx.Response(400, json={"error_code": "validation_failed"}), 422),
    ],
)
async def test_supabase_rejection_does_not_consume_attempt(client, supabase, db, response, status):
    supabase.add("POST", "/resend", response)
    for _ in range(3):
        r = await resend(client, "fail@example.com")
        assert r.status_code == status
        assert r.json()["success"] is False
    tracker = await tracker_for(db, "fail@example.com")
    assert tracker.resend_count == 0 and tracker.last_sent_at is None

    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    r = await resend(client, "fail@example.com")
    assert r.status_code == 200
    assert r.json()["data"]["remaining_attempts"] == 1


async def test_network_error_does_not_consume_attempt(client, supabase, db):
    def down(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    supabase.add("POST", "/resend", down)
    r = await resend(client, "net@example.com")
    assert r.status_code == 503 and r.json()["error"]["code"] == "auth_unavailable"
    assert (await tracker_for(db, "net@example.com")).resend_count == 0


# 7 ---------------------------------------------------------------- already verified
async def test_already_verified_local_user(client, supabase, db):
    db.add(User(id=uuid.uuid4(), email="done@example.com", email_verified=True))
    await db.commit()
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    r = await resend(client, "Done@Example.com")
    assert r.status_code == 409
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "email_already_verified"
    assert not resend_calls(supabase)  # Supabase never called, nothing counted


async def test_already_verified_reported_by_supabase(client, supabase, db):
    supabase.add("POST", "/resend", httpx.Response(400, json={"msg": "Email address already confirmed"}))
    r = await resend(client, "confirmed@example.com")
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_already_verified"
    tracker = await tracker_for(db, "confirmed@example.com")
    assert tracker.resend_count == 0 and tracker.verified_at is not None
    # Subsequent requests short-circuit without calling Supabase.
    calls = len(resend_calls(supabase))
    assert (await resend(client, "confirmed@example.com")).status_code == 409
    assert len(resend_calls(supabase)) == calls


# 8 ------------------------------------------------------------ expired verification link
async def test_expired_link_user_can_resend_within_limit(client, supabase, db, clock):
    """A link that expired (Supabase default: 24 h OTP expiry is configurable, often 1 h)
    leaves the account unverified; resending issues a new link and still counts."""
    get_settings().EMAIL_VERIFICATION_CYCLE_HOURS = 48
    supabase.add("POST", "/signup", httpx.Response(200, json=confirmation_payload("late@example.com")))
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    assert (await client.post(SIGNUP, json={"email": "late@example.com", "password": STRONG})).status_code == 201

    clock["offset"] = timedelta(hours=2)  # the initial link has expired by now
    r = await resend(client, "late@example.com")
    assert r.status_code == 200 and r.json()["data"]["remaining_attempts"] == 1
    clock["offset"] = timedelta(hours=4)  # second link expired too
    assert (await resend(client, "late@example.com")).status_code == 200
    r = await resend(client, "late@example.com")
    assert r.status_code == 429 and r.json()["message"] == LIMIT_MSG  # expiry never resets


async def test_new_cycle_after_window(client, supabase, db, clock):
    get_settings().EMAIL_VERIFICATION_CYCLE_HOURS = 24
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    assert [(await resend(client, "cyc@example.com")).status_code for _ in range(3)] == [200, 200, 429]
    clock["offset"] = timedelta(hours=23)
    assert (await resend(client, "cyc@example.com")).status_code == 429
    clock["offset"] = timedelta(hours=24, minutes=1)
    r = await resend(client, "cyc@example.com")
    assert r.status_code == 200 and r.json()["data"]["remaining_attempts"] == 1
    tracker = await tracker_for(db, "cyc@example.com")
    assert tracker.cycle == 2 and tracker.resend_count == 1


async def test_cycle_window_zero_never_resets(client, supabase, clock):
    get_settings().EMAIL_VERIFICATION_CYCLE_HOURS = 0
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    assert [(await resend(client, "perm@example.com")).status_code for _ in range(3)] == [200, 200, 429]
    clock["offset"] = timedelta(days=365)
    r = await resend(client, "perm@example.com")
    assert r.status_code == 429 and "Retry-After" not in r.headers


# cooldown ----------------------------------------------------------------------
async def test_cooldown_between_emails(client, supabase, clock):
    get_settings().EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS = 60
    supabase.add("POST", "/signup", httpx.Response(200, json=confirmation_payload("cool@example.com")))
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    assert (await client.post(SIGNUP, json={"email": "cool@example.com", "password": STRONG})).status_code == 201

    r = await resend(client, "cool@example.com")  # right after the initial e-mail
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "resend_cooldown"
    assert body["data"] == {"remaining_attempts": 2, "retry_after": int(r.headers["Retry-After"])}
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert not resend_calls(supabase)

    clock["offset"] = timedelta(seconds=61)
    r = await resend(client, "cool@example.com")
    assert r.status_code == 200 and r.json()["data"]["cooldown_seconds"] == 60
    clock["offset"] = timedelta(seconds=90)
    assert (await resend(client, "cool@example.com")).json()["error"]["code"] == "resend_cooldown"
    clock["offset"] = timedelta(seconds=125)
    assert (await resend(client, "cool@example.com")).status_code == 200
    clock["offset"] = timedelta(seconds=500)
    assert (await resend(client, "cool@example.com")).json()["message"] == LIMIT_MSG


async def test_failed_resend_does_not_start_cooldown(client, supabase):
    get_settings().EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS = 60
    supabase.add("POST", "/resend", httpx.Response(500, json={}))
    assert (await resend(client, "cdfail@example.com")).status_code == 503
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    assert (await resend(client, "cdfail@example.com")).status_code == 200


async def test_forgot_password_does_not_use_resend_ip_budget(client, supabase):
    get_settings().RATE_LIMIT_EMAIL_PER_HOUR = 2
    supabase.add("POST", "/recover", httpx.Response(200, json={}))
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    for _ in range(2):
        assert (await client.post("/api/v1/auth/forgot-password", json={"email": "f@example.com"})).status_code == 200
    assert (await resend(client, "f@example.com")).status_code == 200


# 9 ---------------------------------------------------------------- concurrency
async def test_concurrent_resends_cannot_exceed_limit(client, supabase, db):
    async def slow_ok(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)  # keep requests in flight simultaneously
        return httpx.Response(200, json={})

    supabase.add("POST", "/resend", slow_ok)
    responses = await asyncio.gather(*(resend(client, "race@example.com") for _ in range(8)))
    codes = sorted(r.status_code for r in responses)
    assert codes.count(200) == 2, codes
    assert codes.count(429) == 6, codes
    assert all(r.json()["message"] == LIMIT_MSG for r in responses if r.status_code == 429)
    assert len(resend_calls(supabase)) == 2
    assert (await tracker_for(db, "race@example.com")).resend_count == 2


async def test_concurrent_failures_release_slots(client, supabase, db):
    async def slow_fail(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return httpx.Response(500, json={})

    supabase.add("POST", "/resend", slow_fail)
    await asyncio.gather(*(resend(client, "racefail@example.com") for _ in range(4)))
    assert (await tracker_for(db, "racefail@example.com")).resend_count == 0


# 10 ------------------------------------------------------------ restart persistence
async def test_limit_persists_across_restart(client, supabase, engine, tmp_path):
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    assert [(await resend(client, "persist@example.com")).status_code for _ in range(2)] == [200, 200]

    # Simulate a process restart: drop all in-memory state and reconnect to the same database.
    await engine.dispose()
    limiter.reset()
    await close_http_clients()
    if TEST_DATABASE_URL:
        from app.core.database import normalize_database_url

        fresh = create_async_engine(normalize_database_url(TEST_DATABASE_URL), poolclass=NullPool)
    else:
        fresh = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=NullPool,
        )

        @event.listens_for(fresh.sync_engine, "connect")
        def _pragmas(dbapi_conn: Any, _record: Any) -> None:
            dbapi_conn.cursor().execute("PRAGMA foreign_keys=ON")

    set_engine(fresh)
    try:
        from tests.conftest import MockUpstream

        upstream = MockUpstream()
        upstream.add("POST", "/resend", httpx.Response(200, json={}))
        set_http_client("supabase_auth", upstream.client())
        r = await resend(client, "persist@example.com")
        assert r.status_code == 429 and r.json()["message"] == LIMIT_MSG
        assert not resend_calls(upstream)
    finally:
        await fresh.dispose()


# 11 ---------------------------------------------------------- per-user independence
async def test_limits_are_independent_per_user(client, supabase):
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    for _ in range(2):
        assert (await resend(client, "one@example.com")).status_code == 200
    assert (await resend(client, "one@example.com")).status_code == 429
    r = await resend(client, "two@example.com")
    assert r.status_code == 200 and r.json()["data"]["remaining_attempts"] == 1
    # E-mail matching is case-insensitive: same user, same counter.
    assert (await resend(client, "ONE@example.com")).status_code == 429


# 12 ------------------------------------------------------ registration retries
async def test_registration_retry_cannot_reset_limit(client, supabase, db):
    supabase.add("POST", "/signup", httpx.Response(200, json=confirmation_payload("retry@example.com")))
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    assert (await client.post(SIGNUP, json={"email": "retry@example.com", "password": STRONG})).status_code == 201
    assert [(await resend(client, "retry@example.com")).status_code for _ in range(2)] == [200, 200]

    # Registering again (another device / reinstall) returns the usual response...
    r = await client.post(SIGNUP, json={"email": "retry@example.com", "password": STRONG})
    assert r.status_code == 201
    assert r.json()["data"]["email_confirmation_required"] is True
    # ...but Supabase is not asked to send a 4th e-mail and the counter is unchanged.
    assert len(signup_calls(supabase)) == 1
    tracker = await tracker_for(db, "retry@example.com")
    assert tracker.resend_count == 2 and tracker.cycle == 1
    assert (await resend(client, "retry@example.com")).json()["message"] == LIMIT_MSG


async def test_registration_retry_counts_as_resend(client, supabase, db):
    supabase.add("POST", "/signup", httpx.Response(200, json=confirmation_payload("again@example.com")))
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    for _ in range(3):  # initial + 2 retries (Supabase re-sends the confirmation e-mail)
        assert (await client.post(SIGNUP, json={"email": "again@example.com", "password": STRONG})).status_code == 201
    assert len(signup_calls(supabase)) == 3
    assert (await tracker_for(db, "again@example.com")).resend_count == 2
    r = await resend(client, "again@example.com")
    assert r.status_code == 429 and not resend_calls(supabase)
    # A 4th registration is suppressed too.
    assert (await client.post(SIGNUP, json={"email": "again@example.com", "password": STRONG})).status_code == 201
    assert len(signup_calls(supabase)) == 3


async def test_failed_registration_retry_releases_slot(client, supabase, db):
    supabase.add("POST", "/signup", httpx.Response(200, json=confirmation_payload("sf@example.com")))
    assert (await client.post(SIGNUP, json={"email": "sf@example.com", "password": STRONG})).status_code == 201
    supabase.add("POST", "/signup", httpx.Response(429, json={"error_code": "over_email_send_rate_limit"}))
    assert (await client.post(SIGNUP, json={"email": "sf@example.com", "password": STRONG})).status_code == 429
    assert (await tracker_for(db, "sf@example.com")).resend_count == 0


# 13 ----------------------------------------------- login/registration unaffected
async def test_login_and_logout_do_not_reset_limit(client, supabase, db):
    from tests.test_auth import session_payload

    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    assert [(await resend(client, "keep@example.com")).status_code for _ in range(3)] == [200, 200, 429]
    payload = session_payload(email="other@example.com")
    supabase.add("POST", "grant_type=password", httpx.Response(200, json=payload))
    supabase.add("POST", "/logout", httpx.Response(204))
    login = await client.post("/api/v1/auth/login", json={"email": "other@example.com", "password": STRONG})
    assert login.status_code == 200
    token = login.json()["data"]["access_token"]
    assert (await client.post("/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"})).status_code == 200
    assert (await resend(client, "keep@example.com")).status_code == 429


async def test_signup_errors_unchanged(client, supabase, db):
    supabase.add("POST", "/signup", httpx.Response(422, json={"error_code": "user_already_exists", "msg": "User already registered"}))
    r = await client.post(SIGNUP, json={"email": "dup2@example.com", "password": STRONG})
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_exists"
    assert await tracker_for(db, "dup2@example.com") is None


async def test_resend_validates_email(client, supabase):
    assert (await client.post(RESEND, json={"email": "not-an-email"})).status_code == 422
    assert not resend_calls(supabase)
