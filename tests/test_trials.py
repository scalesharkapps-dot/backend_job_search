"""3-day app-managed trial: creation, duration, persistence and access."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.subscription import TrialStatus, UserTrial
from app.models.user import User
from app.services.billing import trial as trial_service
from tests.conftest import auth_header, make_token
from tests.test_auth import STRONG, session_payload

pytestmark = pytest.mark.usefixtures("billing_settings")


async def trials_for(db, user_id) -> list[UserTrial]:
    rows = await db.execute(
        select(UserTrial).where(UserTrial.user_id == user_id).execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


# 1, 2, 9 ------------------------------------------------------------------------
async def test_registration_creates_exact_three_day_utc_trial(client, supabase, db):
    uid = uuid.uuid4()
    supabase.add("POST", "/signup", httpx.Response(200, json={
        "id": str(uid), "email": "trial@example.com", "identities": [{"provider": "email"}],
    }))
    r = await client.post("/api/v1/auth/signup", json={"email": "trial@example.com", "password": STRONG})
    assert r.status_code == 201, r.text
    [trial] = await trials_for(db, uid)
    assert trial.trial_ends_at - trial.trial_started_at == timedelta(days=3)
    assert trial.trial_started_at.tzinfo is not None and trial.trial_started_at.utcoffset() == timedelta(0)
    assert abs((trial.trial_started_at - trial_service.utcnow()).total_seconds()) < 60
    assert trial.trial_status == TrialStatus.ACTIVE
    user = await db.get(User, uid)
    assert user is not None and user.email_verified is False


async def test_signup_with_session_creates_trial(client, supabase, db):
    payload = session_payload(email="instant@example.com")
    supabase.add("POST", "/signup", httpx.Response(200, json=payload))
    assert (await client.post("/api/v1/auth/signup", json={"email": "instant@example.com", "password": STRONG})).status_code == 201
    assert len(await trials_for(db, uuid.UUID(payload["user"]["id"]))) == 1


async def test_obfuscated_existing_account_signup_creates_no_trial(client, supabase, db):
    fake = uuid.uuid4()
    supabase.add("POST", "/signup", httpx.Response(200, json={"id": str(fake), "email": "x@example.com", "identities": []}))
    assert (await client.post("/api/v1/auth/signup", json={"email": "x@example.com", "password": STRONG})).status_code == 201
    assert await trials_for(db, fake) == []


async def test_existing_user_gets_trial_on_first_request(client, user_factory, db):
    """Accounts created before billing existed are not locked out without warning."""
    user, headers = await user_factory()
    assert await trials_for(db, user.id) == []
    r = await client.get("/api/v1/subscriptions/status", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["access_status"] == "trial_active" and data["access_allowed"] is True
    assert 3 * 86400 - 60 < data["trial_remaining_seconds"] <= 3 * 86400
    assert len(await trials_for(db, user.id)) == 1


# 3, 4 ----------------------------------------------------------------------------
async def test_active_trial_allows_and_expired_trial_blocks(client, user_factory, clock):
    _, headers = await user_factory()
    assert (await client.get("/api/v1/cv", headers=headers)).status_code == 200
    clock.advance(days=3, seconds=-5)
    assert (await client.get("/api/v1/cv", headers=headers)).status_code == 200
    clock.advance(seconds=10)
    r = await client.get("/api/v1/cv", headers=headers)
    assert r.status_code == 403
    body = r.json()
    assert body["success"] is False and body["error"]["code"] == "subscription_required"
    assert body["data"]["access_status"] == "payment_required"
    assert body["data"]["trial_status"] == "expired"
    status = (await client.get("/api/v1/subscriptions/status", headers=headers)).json()["data"]
    assert status["access_allowed"] is False and status["trial_remaining_seconds"] == 0


# 5, 6 ----------------------------------------------------------------------------
async def test_login_logout_and_new_tokens_do_not_restart_trial(client, supabase, db, clock):
    uid = uuid.uuid4()
    headers = auth_header(make_token(uid, email="again@example.com"))
    await client.get("/api/v1/subscriptions/status", headers=headers)
    [original] = await trials_for(db, uid)
    clock.advance(days=2)

    supabase.add("POST", "grant_type=password", httpx.Response(200, json=session_payload(str(uid), "again@example.com")))
    supabase.add("POST", "/logout", httpx.Response(204))
    login = await client.post("/api/v1/auth/login", json={"email": "again@example.com", "password": STRONG})
    assert login.status_code == 200
    token = login.json()["data"]["access_token"]
    assert (await client.post("/api/v1/auth/logout", headers=auth_header(token))).status_code == 200
    # "Reinstall / new device": a brand-new token for the same account.
    fresh = auth_header(make_token(uid, email="again@example.com"))
    data = (await client.get("/api/v1/subscriptions/status", headers=fresh)).json()["data"]

    [after] = await trials_for(db, uid)
    assert after.id == original.id and after.trial_started_at == original.trial_started_at
    assert data["trial_remaining_seconds"] <= 86400


async def test_repeated_signup_does_not_restart_trial(client, supabase, db, clock):
    uid = uuid.uuid4()
    body = {"id": str(uid), "email": "dup@example.com", "identities": [{"provider": "email"}]}
    supabase.add("POST", "/signup", httpx.Response(200, json=body))
    await client.post("/api/v1/auth/signup", json={"email": "dup@example.com", "password": STRONG})
    [first] = await trials_for(db, uid)
    clock.advance(days=4)
    await client.post("/api/v1/auth/signup", json={"email": "dup@example.com", "password": STRONG})
    [again] = await trials_for(db, uid)
    assert again.trial_started_at == first.trial_started_at


# 7, 8 ----------------------------------------------------------------------------
async def test_duplicate_trial_prevented(db, user_factory):
    user, _ = await user_factory()
    a = await trial_service.ensure_trial(db, user.id)
    b = await trial_service.ensure_trial(db, user.id)
    assert a.id == b.id
    now = trial_service.utcnow()
    db.add(UserTrial(user_id=user.id, trial_started_at=now, trial_ends_at=now + timedelta(days=3)))
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_concurrent_first_requests_create_one_trial(client, db):
    uid = uuid.uuid4()
    headers = auth_header(make_token(uid, email="race@example.com"))
    await client.get("/api/v1/auth/me", headers=headers)  # local user row
    results = await asyncio.gather(*(client.get("/api/v1/subscriptions/status", headers=headers) for _ in range(6)))
    assert all(r.status_code == 200 for r in results), [r.text for r in results if r.status_code != 200]
    assert {r.json()["data"]["trial_ends_at"] for r in results} and len(
        {r.json()["data"]["trial_ends_at"] for r in results}
    ) == 1
    count = await db.scalar(select(func.count()).select_from(UserTrial).where(UserTrial.user_id == uid))
    assert count == 1


@pytest.mark.unit
def test_trial_state_math():
    now = trial_service.utcnow()
    trial = UserTrial(user_id=uuid.uuid4(), trial_started_at=now, trial_ends_at=now + trial_service.TRIAL_DURATION,
                      trial_status=TrialStatus.ACTIVE)
    assert trial_service.trial_state(trial, now).remaining_seconds == 3 * 86400
    late = trial_service.trial_state(trial, now + timedelta(days=3))
    assert late.status == TrialStatus.EXPIRED and not late.active
    assert timedelta(days=3) == trial_service.TRIAL_DURATION
    assert now.tzinfo is UTC or now.utcoffset() == timedelta(0)
