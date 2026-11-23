"""Centralized app-access control (paywall) on protected routes."""

from __future__ import annotations

import uuid
from datetime import timedelta

import httpx
import pytest
from fastapi.routing import APIRoute
from sqlalchemy.exc import OperationalError

from app.api.deps import require_app_access
from app.core.config import get_settings
from app.models.user import UserRole
from tests.billing_helpers import event, send
from tests.documents import make_pdf
from tests.jsearch_data import raw_job, search_payload
from tests.test_auth import STRONG, session_payload

pytestmark = pytest.mark.usefixtures("billing_settings")

# Routes that must stay reachable without a trial/subscription.
OPEN_PREFIXES = (
    "/api/v1/health",
    "/api/v1/auth",
    "/api/v1/subscriptions",
    "/api/v1/reports",
    "/api/v1/files",
    "/api/v1/admin",
    "/api/v1/_test",
)
OPEN_EXACT = {"/api/v1/jobs/filters"}

PROTECTED_SAMPLE = [
    ("GET", "/api/v1/profile/me"),
    ("GET", "/api/v1/cv"),
    ("GET", "/api/v1/jobs/search?query=python"),
    ("GET", "/api/v1/recommendations/for-me"),
    ("GET", "/api/v1/saved-jobs"),
    ("GET", "/api/v1/applications"),
    ("GET", "/api/v1/interviews/upcoming"),
    ("GET", "/api/v1/employer/jobs"),
    ("GET", "/api/v1/employers/me"),
]


def _depends_on(dependant, target) -> bool:
    return any(d.call is target or _depends_on(d, target) for d in dependant.dependencies)


@pytest.mark.unit
def test_every_application_route_is_behind_the_paywall():
    """New endpoints are protected by default: this fails if one is added without the paywall."""
    from app.main import app

    unprotected = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/v1"):
            continue
        if route.path in OPEN_EXACT or route.path.startswith(OPEN_PREFIXES):
            continue
        if not _depends_on(route.dependant, require_app_access):
            unprotected.append(f"{sorted(route.methods)} {route.path}")
    assert not unprotected, f"Routes without app-access control: {unprotected}"


@pytest.mark.unit
def test_open_routes_do_not_require_app_access():
    from app.main import app

    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith(("/api/v1/auth", "/api/v1/subscriptions")):
            assert not _depends_on(route.dependant, require_app_access), route.path


async def expire_trial(client, headers, clock) -> None:
    await client.get("/api/v1/subscriptions/status", headers=headers)
    clock.advance(days=3, minutes=1)


# 29, 31 -------------------------------------------------------------------------
@pytest.mark.parametrize(("method", "path"), PROTECTED_SAMPLE)
async def test_expired_trial_blocked_everywhere(client, user_factory, clock, method, path):
    _, headers = await user_factory()
    await expire_trial(client, headers, clock)
    r = await client.request(method, path, headers=headers)
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["success"] is False and body["error"]["code"] == "subscription_required"
    assert body["data"]["access_status"] == "payment_required"
    assert body["error"]["details"]["trial_ends_at"]


async def test_active_trial_reaches_protected_routes(client, user_factory, jsearch):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job()])))
    assert (await client.get("/api/v1/cv", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/jobs/search", params={"query": "python"}, headers=headers)).status_code == 200
    assert (await client.get("/api/v1/applications", headers=headers)).status_code == 200


# 30 --------------------------------------------------------------------------------
async def test_active_subscription_reaches_protected_routes(client, user_factory, clock, jsearch):
    user, headers = await user_factory()
    await expire_trial(client, headers, clock)
    now = clock.now()
    await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30)))
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job()])))
    assert (await client.get("/api/v1/jobs/search", params={"query": "python"}, headers=headers)).status_code == 200
    assert (await client.get("/api/v1/saved-jobs", headers=headers)).status_code == 200


# 35, 36 ----------------------------------------------------------------------------
async def test_job_search_and_cv_upload_protected(client, user_factory, clock, jsearch):
    _, headers = await user_factory()
    await expire_trial(client, headers, clock)
    r = await client.get("/api/v1/jobs/search", params={"query": "python"}, headers=headers)
    assert r.status_code == 403
    assert not jsearch.requests  # blocked before any paid upstream call
    r = await client.post("/api/v1/cv/upload", headers=headers, files={"file": ("cv.pdf", make_pdf(), "application/pdf")})
    assert r.status_code == 403 and r.json()["error"]["code"] == "subscription_required"


# 32 --------------------------------------------------------------------------------
async def test_unauthenticated_gets_401_not_403(client):
    for path in ("/api/v1/cv", "/api/v1/jobs/search?query=x", "/api/v1/subscriptions/status"):
        r = await client.get(path)
        assert r.status_code == 401 and r.json()["error"]["code"] == "not_authenticated"


# 33, 34 ----------------------------------------------------------------------------
async def test_open_endpoints_remain_accessible_after_trial(client, user_factory, clock, supabase):
    user, headers = await user_factory(email="open@example.com")
    await expire_trial(client, headers, clock)
    for path in ("/api/v1/subscriptions/status", "/api/v1/subscriptions/history", "/api/v1/subscriptions/plans",
                 "/api/v1/auth/me", "/api/v1/jobs/filters", "/api/v1/reports/mine"):
        r = await client.get(path, headers=headers)
        assert r.status_code == 200, (path, r.text)
    supabase.add("POST", "grant_type=password", httpx.Response(200, json=session_payload(str(user.id), user.email)))
    login = await client.post("/api/v1/auth/login", json={"email": user.email, "password": STRONG})
    assert login.status_code == 200
    supabase.add("POST", "/logout", httpx.Response(204))
    assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 200
    supabase.add("POST", "/recover", httpx.Response(200, json={}))
    assert (await client.post("/api/v1/auth/forgot-password", json={"email": user.email})).status_code == 200


async def test_suspended_account_still_gets_account_suspended(client, user_factory):
    _, headers = await user_factory(is_active=False)
    r = await client.get("/api/v1/cv", headers=headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "account_suspended"


async def test_admin_exempt(client, user_factory, clock):
    _, headers = await user_factory(UserRole.ADMIN)
    await expire_trial(client, headers, clock)
    assert (await client.get("/api/v1/cv", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/subscriptions/status", headers=headers)).json()["data"]["access_status"] == "exempt"


async def test_employer_needs_access_too(client, user_factory, clock):
    _, headers = await user_factory(UserRole.EMPLOYER)
    await expire_trial(client, headers, clock)
    assert (await client.get("/api/v1/employer/jobs", headers=headers)).status_code == 403


async def test_enforcement_switch(client, user_factory, clock):
    _, headers = await user_factory()
    await expire_trial(client, headers, clock)
    get_settings().SUBSCRIPTION_ENFORCEMENT_ENABLED = False
    assert (await client.get("/api/v1/cv", headers=headers)).status_code == 200
    data = (await client.get("/api/v1/subscriptions/status", headers=headers)).json()["data"]
    assert data["access_allowed"] is True and data["access_status"] == "payment_required"


# 40 --------------------------------------------------------------------------------
async def test_database_failure_during_access_check(client, user_factory, monkeypatch):
    from app.api import deps

    _, headers = await user_factory()

    async def broken(*_a, **_k):
        raise OperationalError("SELECT user_trials", {}, Exception("db down"))

    monkeypatch.setattr(deps, "evaluate_access", broken)
    r = await client.get("/api/v1/cv", headers=headers)
    assert r.status_code == 503 and r.json()["error"]["code"] == "database_unavailable"


# G: manipulation ---------------------------------------------------------------------
async def test_client_cannot_grant_itself_access(client, user_factory, clock):
    user, headers = await user_factory()
    await expire_trial(client, headers, clock)
    spoof = {"access_allowed": True, "subscription_status": "active", "user_id": str(uuid.uuid4()),
             "expiration_at": "2099-01-01T00:00:00Z", "entitlement_active": True}
    # No endpoint accepts client subscription data...
    r = await client.post("/api/v1/subscriptions/sync", headers=headers, json=spoof)
    assert r.status_code == 503  # ...sync only trusts RevenueCat (not configured here)
    # ...and a forged webhook without the shared secret is rejected.
    now = clock.now()
    forged = await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=365)), auth="Bearer guess")
    assert forged.status_code == 401
    assert (await client.get("/api/v1/cv", headers=headers)).status_code == 403
