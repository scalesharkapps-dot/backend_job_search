"""Helpers for the trial / subscription / webhook tests.

RevenueCat is mocked with httpx MockTransport. Payload shapes follow RevenueCat's
documented REST API v1 ``GET /v1/subscribers/{id}`` response and webhook event
fields; they are test fixtures, not captured production traffic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.models import base as model_base
from app.services.billing import subscriptions as subs
from app.services.billing import trial as trial_service
from app.services.http import set_http_client
from tests.conftest import MockUpstream

WEBHOOK_SECRET = "Bearer test-webhook-secret-0123456789"  # secret-scan: ignore
INTL = "sharkhire_monthly_intl"
PK = "sharkhire_monthly_pk"
WEBHOOK = "/api/v1/subscriptions/webhook"


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta(0)

    def now(self) -> datetime:
        return model_base.utcnow() + self.offset

    def advance(self, **kw: float) -> None:
        self.offset += timedelta(**kw)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(trial_service, "utcnow", c.now)
    monkeypatch.setattr(subs, "utcnow", c.now)
    return c


@pytest.fixture
def billing_settings() -> Any:
    s = get_settings()
    s.REVENUECAT_WEBHOOK_AUTHORIZATION = SecretStr(WEBHOOK_SECRET)
    s.REVENUECAT_SECRET_API_KEY = None  # webhook-only unless a test enables the API
    s.REVENUECAT_PRODUCT_INTERNATIONAL = f"{INTL},{INTL}_ios"
    s.REVENUECAT_PRODUCT_PAKISTAN = PK
    s.REVENUECAT_RETRY_BACKOFF_SECONDS = 0
    s.REVENUECAT_ALLOW_SANDBOX = False
    return s


@pytest.fixture
def revenuecat(billing_settings: Any) -> MockUpstream:
    billing_settings.REVENUECAT_SECRET_API_KEY = SecretStr("sk_test_server_key_0123456789")  # secret-scan: ignore
    upstream = MockUpstream()
    set_http_client("revenuecat", upstream.client())
    return upstream


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def event(
    event_type: str,
    user_id: uuid.UUID | str,
    *,
    at: datetime,
    expires: datetime | None = None,
    product: str = INTL,
    store: str = "PLAY_STORE",
    environment: str = "PRODUCTION",
    transaction_id: str | None = None,
    entitled: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": str(uuid.uuid4()).upper(),
        "type": event_type,
        "app_user_id": str(user_id),
        "original_app_user_id": str(user_id),
        "aliases": [str(user_id)],
        "product_id": product,
        "entitlement_ids": ["app_access"] if entitled else [],
        "period_type": "NORMAL",
        "purchased_at_ms": ms(at),
        "expiration_at_ms": ms(expires) if expires else None,
        "event_timestamp_ms": ms(at),
        "environment": environment,
        "store": store,
        "transaction_id": transaction_id,
        "original_transaction_id": "GPA.1111-2222",
        "currency": "PKR" if product == PK else "USD",
        "price": 1.79 if product == PK else 10.0,
        "price_in_purchased_currency": 499.0 if product == PK else 10.0,
        "country_code": "PK" if product == PK else "US",
        "subscriber_attributes": {"$email": {"value": "private@example.com", "updated_at_ms": ms(at)}},
    }
    body.update(extra)
    return body


async def send(client: httpx.AsyncClient, ev: dict[str, Any], auth: str | None = WEBHOOK_SECRET) -> httpx.Response:
    headers = {"Authorization": auth} if auth is not None else {}
    return await client.post(WEBHOOK, json={"api_version": "1.0", "event": ev}, headers=headers)


def subscriber(
    *,
    product: str | None = INTL,
    expires: datetime | None = None,
    store: str = "play_store",
    sandbox: bool = False,
    entitled: bool = True,
    unsubscribed: datetime | None = None,
    billing: datetime | None = None,
    grace: datetime | None = None,
    refunded: datetime | None = None,
    app_user_id: str = "",
) -> dict[str, Any]:
    """A RevenueCat v1 ``GET /subscribers/{id}`` body."""
    now = datetime.now(UTC)
    subscriptions: dict[str, Any] = {}
    entitlements: dict[str, Any] = {}
    if product:
        subscriptions[product] = {
            "auto_resume_date": None,
            "billing_issues_detected_at": iso(billing),
            "expires_date": iso(expires),
            "grace_period_expires_date": iso(grace),
            "is_sandbox": sandbox,
            "original_purchase_date": iso(now - timedelta(days=40)),
            "ownership_type": "PURCHASED",
            "period_type": "normal",
            "purchase_date": iso(now - timedelta(days=10)),
            "refunded_at": iso(refunded),
            "store": store,
            "store_transaction_id": "GPA.3333-4444..5",
            "unsubscribe_detected_at": iso(unsubscribed),
        }
        if entitled:
            entitlements["app_access"] = {
                "expires_date": iso(expires),
                "grace_period_expires_date": iso(grace),
                "product_identifier": product,
                "purchase_date": iso(now - timedelta(days=10)),
            }
    return {
        "request_date": iso(now),
        "request_date_ms": ms(now),
        "subscriber": {
            "entitlements": entitlements,
            "first_seen": iso(now - timedelta(days=40)),
            "management_url": None,
            "non_subscriptions": {},
            "original_app_user_id": app_user_id,
            "other_purchases": {},
            "subscriptions": subscriptions,
        },
    }


def rc_requests(upstream: MockUpstream) -> list[httpx.Request]:
    return [r for r in upstream.requests if "/subscribers/" in r.url.path]
