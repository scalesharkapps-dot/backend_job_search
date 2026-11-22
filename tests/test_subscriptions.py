"""Subscriptions: RevenueCat webhooks, API sync/restore, ordering, idempotency and reliability.

RevenueCat is mocked; nothing here proves a real Google Play / App Store purchase.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models.subscription import (
    EventProcessingStatus,
    PaymentHistory,
    Subscription,
    SubscriptionEvent,
    SubscriptionStatus,
    TransactionStatus,
)
from app.services.billing import subscriptions as subs
from tests.billing_helpers import INTL, PK, event, rc_requests, send, subscriber

pytestmark = pytest.mark.usefixtures("billing_settings")


async def expired_trial_user(user_factory, client, clock) -> tuple[Any, dict[str, str]]:
    user, headers = await user_factory()
    await client.get("/api/v1/subscriptions/status", headers=headers)  # creates the trial
    clock.advance(days=3, minutes=1)
    return user, headers


async def status(client, headers) -> dict[str, Any]:
    r = await client.get("/api/v1/subscriptions/status", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def can_use_app(client, headers) -> bool:
    r = await client.get("/api/v1/cv", headers=headers)
    assert r.status_code in (200, 403), r.text
    return r.status_code == 200


async def sub_row(db, user_id, product=INTL) -> Subscription:
    return (await db.execute(
        select(Subscription).where(Subscription.user_id == user_id, Subscription.product_id == product)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def event_row(db, event_id) -> SubscriptionEvent:
    return (await db.execute(
        select(SubscriptionEvent).where(SubscriptionEvent.revenuecat_event_id == event_id)
        .execution_options(populate_existing=True)
    )).scalar_one()


async def payments(db, user_id) -> list[PaymentHistory]:
    return list((await db.execute(
        select(PaymentHistory).where(PaymentHistory.user_id == user_id).execution_options(populate_existing=True)
    )).scalars().all())


# 10, 19, 21 ------------------------------------------------- purchase via webhook (Android)
async def test_initial_purchase_android_grants_access(client, user_factory, clock, db):
    user, headers = await expired_trial_user(user_factory, client, clock)
    assert not await can_use_app(client, headers)
    now = clock.now()
    ev = event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30),
               store="PLAY_STORE", product=f"{INTL}:monthly-base", transaction_id="GPA.0001")
    r = await send(client, ev)
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"event_id": ev["id"], "status": "processed", "duplicate": False}

    data = await status(client, headers)
    assert data["access_allowed"] is True and data["access_status"] == "subscription_active"
    assert data["store"] == "play_store" and data["plan"] == "international" and data["will_renew"] is True
    assert await can_use_app(client, headers)

    stored = await event_row(db, ev["id"])
    assert stored.processing_status == EventProcessingStatus.PROCESSED and stored.user_id == user.id
    assert "subscriber_attributes" not in stored.event_payload  # personal data not persisted
    [pay] = await payments(db, user.id)
    assert pay.transaction_id == "GPA.0001" and str(pay.transaction_amount) == "10.0000" and pay.currency == "USD"
    assert pay.transaction_status == TransactionStatus.COMPLETED


# 20 ------------------------------------------------------------------------ iOS + Pakistan plan
async def test_ios_pakistan_purchase(client, user_factory, clock, db):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    r = await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30),
                                 store="APP_STORE", product=PK, transaction_id="2000000001"))
    assert r.status_code == 200
    data = await status(client, headers)
    assert data["store"] == "app_store" and data["plan"] == "pakistan" and data["access_allowed"]
    [pay] = await payments(db, user.id)
    assert pay.currency == "PKR" and str(pay.transaction_amount) == "499.0000"


# 11 ------------------------------------------------------------------------ renewal
async def test_renewal_maintains_access(client, user_factory, clock, db):
    user, headers = await expired_trial_user(user_factory, client, clock)
    start = clock.now()
    await send(client, event("INITIAL_PURCHASE", user.id, at=start, expires=start + timedelta(days=30), transaction_id="T1"))
    clock.advance(days=30)
    renew_at = clock.now()
    await send(client, event("RENEWAL", user.id, at=renew_at, expires=renew_at + timedelta(days=30), transaction_id="T2"))
    clock.advance(days=1)
    assert (await status(client, headers))["access_status"] == "subscription_active"
    assert {p.transaction_id for p in await payments(db, user.id)} == {"T1", "T2"}


# 12 ------------------------------------------------------------------------ cancellation
async def test_cancellation_keeps_access_until_expiration(client, user_factory, clock):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    expires = now + timedelta(days=10)
    await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=expires))
    clock.advance(days=1)
    await send(client, event("CANCELLATION", user.id, at=clock.now(), expires=expires, cancel_reason="UNSUBSCRIBE"))
    data = await status(client, headers)
    assert data["access_allowed"] and data["access_status"] == "subscription_cancelled_but_active"
    assert data["will_renew"] is False
    clock.advance(days=9, minutes=1)
    data = await status(client, headers)
    assert not data["access_allowed"] and data["access_status"] == "subscription_expired"


# 13 ------------------------------------------------------------------------ expiration
async def test_expiration_removes_access(client, user_factory, clock):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30)))
    assert await can_use_app(client, headers)
    clock.advance(days=30)
    await send(client, event("EXPIRATION", user.id, at=clock.now(), expires=now + timedelta(days=30),
                             expiration_reason="UNSUBSCRIBE"))
    r = await client.get("/api/v1/cv", headers=headers)
    assert r.status_code == 403 and r.json()["data"]["access_status"] == "subscription_expired"


# 14 ------------------------------------------------------------------------ refund
async def test_refund_revokes_access_and_marks_transaction(client, user_factory, clock, db):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30), transaction_id="R1"))
    clock.advance(hours=2)
    refund_at = clock.now()
    r = await send(client, event("CANCELLATION", user.id, at=refund_at, expires=refund_at,
                                 cancel_reason="CUSTOMER_SUPPORT", transaction_id="R1"))
    assert r.status_code == 200
    clock.advance(seconds=1)
    assert not await can_use_app(client, headers)
    row = await sub_row(db, user.id)
    assert row.subscription_status == SubscriptionStatus.REFUNDED and row.refunded_at is not None
    [pay] = await payments(db, user.id)
    assert pay.transaction_status == TransactionStatus.REFUNDED

    # REFUND_REVERSED restores the entitlement RevenueCat reports.
    clock.advance(minutes=5)
    later = clock.now()
    await send(client, event("REFUND_REVERSED", user.id, at=later, expires=now + timedelta(days=30), transaction_id="R1"))
    assert await can_use_app(client, headers)
    [pay] = await payments(db, user.id)
    assert pay.transaction_status == TransactionStatus.COMPLETED


# 16 ------------------------------------------------------------------------ billing grace period
async def test_billing_issue_grace_period(client, user_factory, clock):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    period_end = now + timedelta(days=1)
    await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=period_end))
    clock.advance(days=1, minutes=1)  # renewal failed at period end
    await send(client, event("BILLING_ISSUE", user.id, at=clock.now(), expires=period_end,
                             grace_period_expiration_at_ms=int((clock.now() + timedelta(days=6)).timestamp() * 1000)))
    data = await status(client, headers)
    assert data["access_allowed"] is True and data["access_status"] == "billing_issue"
    clock.advance(days=6, minutes=1)
    data = await status(client, headers)
    assert data["access_allowed"] is False and data["access_status"] == "billing_issue"


# 23, 43 -------------------------------------------------------------------- duplicates
async def test_duplicate_webhook_and_duplicate_transaction(client, user_factory, clock, db):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    ev = event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30), transaction_id="DUP1")
    first, second = await send(client, ev), await send(client, ev)
    assert first.status_code == second.status_code == 200
    assert second.json()["data"]["duplicate"] is True
    # A different event id carrying the same store transaction is not a second payment.
    again = dict(ev, id=str(uuid.uuid4()))
    assert (await send(client, again)).status_code == 200
    assert len(await payments(db, user.id)) == 1
    events = await db.scalar(select(func.count()).select_from(SubscriptionEvent))
    assert events == 2


async def test_concurrent_duplicate_deliveries_processed_once(client, user_factory, clock, db):
    user, _ = await user_factory()
    now = clock.now()
    ev = event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30), transaction_id="C1")
    results = await asyncio.gather(*(send(client, ev) for _ in range(4)))
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) >= 1 and set(codes) <= {200, 409}, codes
    assert (await event_row(db, ev["id"])).processing_attempts == 1
    assert len(await payments(db, user.id)) == 1


# 24, 25 -------------------------------------------------------------------- delayed / out of order
async def test_delayed_older_event_does_not_override_newer_state(client, user_factory, clock, db):
    user, headers = await expired_trial_user(user_factory, client, clock)
    t0 = clock.now()
    renewal = event("RENEWAL", user.id, at=t0 + timedelta(minutes=10), expires=t0 + timedelta(days=30))
    old_cancel = event("CANCELLATION", user.id, at=t0, expires=t0 + timedelta(days=1), cancel_reason="UNSUBSCRIBE")
    assert (await send(client, renewal)).status_code == 200
    r = await send(client, old_cancel)  # delivered late
    assert r.status_code == 200 and r.json()["data"]["status"] == "processed"
    assert "stale" in (await event_row(db, old_cancel["id"])).last_error
    data = await status(client, headers)
    assert data["access_status"] == "subscription_active" and data["will_renew"] is True


async def test_out_of_order_expiration_for_previous_period_is_ignored(client, user_factory, clock):
    user, headers = await expired_trial_user(user_factory, client, clock)
    t0 = clock.now()
    period1_end = t0 + timedelta(days=1)
    await send(client, event("INITIAL_PURCHASE", user.id, at=t0, expires=period1_end))
    await send(client, event("RENEWAL", user.id, at=t0 + timedelta(hours=1), expires=period1_end + timedelta(days=30)))
    # An EXPIRATION for period 1 with a later timestamp (e.g. retried) must not revoke period 2.
    await send(client, event("EXPIRATION", user.id, at=t0 + timedelta(hours=2), expires=period1_end))
    assert (await status(client, headers))["access_allowed"] is True


# 26 ------------------------------------------------------------------------ unknown / test events
@pytest.mark.parametrize("event_type", ["TEST", "SOME_FUTURE_EVENT", "INVOICE_ISSUANCE"])
async def test_unknown_and_test_events_acknowledged(client, user_factory, clock, db, event_type):
    user, _ = await user_factory()
    ev = event(event_type, user.id, at=clock.now())
    r = await send(client, ev)
    assert r.status_code == 200 and r.json()["data"]["status"] == "ignored"
    assert await db.scalar(select(func.count()).select_from(Subscription)) == 0


async def test_event_for_unknown_user_is_recorded_unmatched(client, clock, db):
    ev = event("INITIAL_PURCHASE", "$RCAnonymousID:abc", at=clock.now(), expires=clock.now() + timedelta(days=30))
    r = await send(client, ev)
    assert r.status_code == 200 and r.json()["data"]["status"] == "unmatched"
    assert (await event_row(db, ev["id"])).processing_status == EventProcessingStatus.UNMATCHED


async def test_event_resolved_through_alias(client, user_factory, clock):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    ev = event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30),
               app_user_id="$RCAnonymousID:xyz", original_app_user_id="$RCAnonymousID:xyz",
               aliases=["$RCAnonymousID:xyz", str(user.id)])
    assert (await send(client, ev)).status_code == 200
    assert (await status(client, headers))["access_allowed"]


# 27, 40 -------------------------------------------------------------------- failure + retry
async def test_failed_event_returns_500_and_is_retried(client, user_factory, clock, db, monkeypatch):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    ev = event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30), transaction_id="F1")
    real = subs.apply_event
    calls = {"n": 0}

    async def flaky(*args: Any, **kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OperationalError("UPDATE subscriptions", {}, Exception("connection lost"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(subs, "apply_event", flaky)
    r = await send(client, ev)
    assert r.status_code == 500 and r.json()["error"]["code"] == "webhook_processing_failed"
    stored = await event_row(db, ev["id"])
    assert stored.processing_status == EventProcessingStatus.FAILED and stored.last_error == "OperationalError"
    assert await payments(db, user.id) == []  # rolled back
    assert not await can_use_app(client, headers)

    r = await send(client, ev)  # RevenueCat's retry
    assert r.status_code == 200 and r.json()["data"]["duplicate"] is False
    stored = await event_row(db, ev["id"])
    assert stored.processing_status == EventProcessingStatus.PROCESSED and stored.processing_attempts == 2
    assert await can_use_app(client, headers)


# 22, 28 -------------------------------------------------------------------- authorization / validation
async def test_webhook_authorization(client, user_factory, clock, billing_settings):
    user, _ = await user_factory()
    ev = event("INITIAL_PURCHASE", user.id, at=clock.now(), expires=clock.now() + timedelta(days=30))
    for auth in (None, "", "Bearer wrong", "test-webhook-secret"):
        r = await send(client, ev, auth=auth)
        assert r.status_code == 401, auth
        assert r.json()["error"]["code"] in {"invalid_webhook_authorization", "not_authenticated"}
    # Unauthorized requests are rejected before the body is validated.
    r = await client.post("/api/v1/subscriptions/webhook", json={"nonsense": True})
    assert r.status_code == 401
    billing_settings.REVENUECAT_WEBHOOK_AUTHORIZATION = None
    r = await send(client, ev)
    assert r.status_code == 503 and r.json()["error"]["code"] == "webhook_not_configured"


@pytest.mark.parametrize(
    "body",
    [{}, {"event": {}}, {"event": {"type": "RENEWAL"}}, {"event": {"id": "", "type": "RENEWAL"}}, {"event": "x"}],
)
async def test_invalid_webhook_payload_rejected(client, body):
    from tests.billing_helpers import WEBHOOK, WEBHOOK_SECRET

    r = await client.post(WEBHOOK, json=body, headers={"Authorization": WEBHOOK_SECRET})
    assert r.status_code == 422


# 42 ------------------------------------------------------------------------ sandbox / production
async def test_sandbox_events_rejected_in_production_mode(client, user_factory, clock, db, billing_settings):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    r = await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30), environment="SANDBOX"))
    assert r.status_code == 200 and r.json()["data"]["status"] == "ignored"
    assert not await can_use_app(client, headers)

    billing_settings.REVENUECAT_ALLOW_SANDBOX = True  # staging / QA
    r = await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30), environment="SANDBOX"))
    assert r.json()["data"]["status"] == "processed"
    assert (await status(client, headers))["environment"] == "SANDBOX"
    billing_settings.REVENUECAT_ALLOW_SANDBOX = False  # sandbox rows never grant production access
    assert not await can_use_app(client, headers)


# 41 ------------------------------------------------------------------------ product mapping
async def test_product_mapping(client, user_factory, clock):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30),
                             product="some_other_app_product", entitled=False))
    assert not await can_use_app(client, headers)  # no app_access entitlement -> no access
    await send(client, event("INITIAL_PURCHASE", user.id, at=now + timedelta(seconds=1), expires=now + timedelta(days=30),
                             product="unlisted_but_entitled"))
    data = await status(client, headers)
    assert data["access_allowed"] and data["plan"] is None  # entitlement is authoritative; flagged


# 17, 18 -------------------------------------------------------------------- API sync / restore
async def test_restore_synchronizes_from_revenuecat(client, user_factory, clock, revenuecat, db):
    user, headers = await expired_trial_user(user_factory, client, clock)
    revenuecat.add("GET", "/subscribers/", httpx.Response(200, json=subscriber(expires=clock.now() + timedelta(days=20))))
    r = await client.post("/api/v1/subscriptions/restore", headers=headers, json={"user_id": str(uuid.uuid4()), "status": "active"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["message"] == "Subscription restored"
    assert body["data"]["access_allowed"] and body["data"]["entitlement_active"] and body["data"]["last_verified_at"]
    [req] = rc_requests(revenuecat)
    assert req.url.path == f"/v1/subscribers/{user.id}"  # always the caller's own id, body ignored
    assert req.headers["Authorization"] == "Bearer sk_test_server_key_0123456789"
    assert not (await sub_row(db, user.id)).needs_reconcile
    assert await can_use_app(client, headers)


async def test_sync_without_purchases(client, user_factory, clock, revenuecat):
    _, headers = await expired_trial_user(user_factory, client, clock)
    revenuecat.add("GET", "/subscribers/", httpx.Response(201, json=subscriber(product=None)))
    r = await client.post("/api/v1/subscriptions/sync", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["access_allowed"] is False and data["revenuecat_customer_found"] is False
    assert data["access_status"] == "payment_required"


async def test_sync_requires_configuration_and_auth(client, user_factory):
    _, headers = await user_factory()
    r = await client.post("/api/v1/subscriptions/sync", headers=headers)
    assert r.status_code == 503 and r.json()["error"]["code"] == "subscriptions_not_configured"
    assert (await client.post("/api/v1/subscriptions/sync")).status_code == 401


# 15 ------------------------------------------------------------------------ revocation via sync
async def test_revoked_when_revenuecat_no_longer_reports_it(client, user_factory, clock, revenuecat, db):
    user, headers = await expired_trial_user(user_factory, client, clock)
    revenuecat.add("GET", "/subscribers/", httpx.Response(200, json=subscriber(expires=clock.now() + timedelta(days=20))))
    await client.post("/api/v1/subscriptions/sync", headers=headers)
    assert await can_use_app(client, headers)
    revenuecat.add("GET", "/subscribers/", httpx.Response(200, json=subscriber(product=None)))
    await client.post("/api/v1/subscriptions/sync", headers=headers)
    assert (await sub_row(db, user.id)).subscription_status == SubscriptionStatus.REVOKED
    assert not await can_use_app(client, headers)


async def test_sync_billing_issue_and_cancellation_states(client, user_factory, clock, revenuecat):
    _, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    revenuecat.add("GET", "/subscribers/", httpx.Response(200, json=subscriber(
        expires=now - timedelta(hours=1), billing=now - timedelta(hours=1), grace=now + timedelta(days=3))))
    data = (await client.post("/api/v1/subscriptions/sync", headers=headers)).json()["data"]
    assert data["access_allowed"] and data["access_status"] == "billing_issue"
    revenuecat.add("GET", "/subscribers/", httpx.Response(200, json=subscriber(
        expires=now + timedelta(days=5), unsubscribed=now - timedelta(days=1))))
    data = (await client.post("/api/v1/subscriptions/sync", headers=headers)).json()["data"]
    assert data["access_status"] == "subscription_cancelled_but_active" and data["will_renew"] is False


async def test_sync_sandbox_purchase_not_accepted_in_production_mode(client, user_factory, clock, revenuecat):
    _, headers = await expired_trial_user(user_factory, client, clock)
    revenuecat.add("GET", "/subscribers/", httpx.Response(200, json=subscriber(
        expires=clock.now() + timedelta(days=20), sandbox=True)))
    data = (await client.post("/api/v1/subscriptions/sync", headers=headers)).json()["data"]
    assert data["access_allowed"] is False and data["environment"] == "SANDBOX"


# 38, 39 -------------------------------------------------------------------- RevenueCat failures
async def test_revenuecat_timeout_retries_then_503_and_keeps_state(client, user_factory, clock, revenuecat, db):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30)))

    def slow(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    revenuecat.add("GET", "/subscribers/", slow)
    r = await client.post("/api/v1/subscriptions/sync", headers=headers)
    assert r.status_code == 503 and r.json()["error"]["code"] == "subscription_service_unavailable"
    assert len(rc_requests(revenuecat)) >= 3  # webhook reconcile (3 tries) + sync (3 tries)
    # A RevenueCat outage never removes verified access.
    assert await can_use_app(client, headers)
    assert (await sub_row(db, user.id)).needs_reconcile is True


@pytest.mark.parametrize("code", [401, 403])
async def test_revenuecat_auth_failure(client, user_factory, revenuecat, code):
    _, headers = await user_factory()
    revenuecat.add("GET", "/subscribers/", httpx.Response(code, json={"code": 7225, "message": "Invalid API key"}))
    r = await client.post("/api/v1/subscriptions/sync", headers=headers)
    assert r.status_code == 502 and r.json()["error"]["code"] == "subscription_service_auth_failed"
    assert "sk_test" not in r.text
    assert len(rc_requests(revenuecat)) == 1  # auth failures are not retried


@pytest.mark.parametrize("payload", [{"no": "subscriber"}, {"subscriber": {"entitlements": []}}, "not json"])
async def test_revenuecat_invalid_response(client, user_factory, revenuecat, payload):
    _, headers = await user_factory()
    response = httpx.Response(200, text=payload) if isinstance(payload, str) else httpx.Response(200, json=payload)
    revenuecat.add("GET", "/subscribers/", response)
    r = await client.post("/api/v1/subscriptions/sync", headers=headers)
    assert r.status_code == 502 and r.json()["error"]["code"] == "invalid_subscription_response"


async def test_revenuecat_5xx_recovers_on_retry(client, user_factory, clock, revenuecat):
    _, headers = await expired_trial_user(user_factory, client, clock)
    replies = iter([httpx.Response(503), httpx.Response(200, json=subscriber(expires=clock.now() + timedelta(days=9)))])
    revenuecat.add("GET", "/subscribers/", lambda _r: next(replies))
    r = await client.post("/api/v1/subscriptions/sync", headers=headers)
    assert r.status_code == 200 and r.json()["data"]["access_allowed"]


# freshness policy: lazy reconciliation at the renewal boundary
async def test_lazy_sync_picks_up_renewal_when_webhook_missing(client, user_factory, clock, revenuecat):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30)))
    revenuecat.add("GET", "/subscribers/", httpx.Response(200, json=subscriber(expires=now + timedelta(days=30))))
    await client.post("/api/v1/subscriptions/sync", headers=headers)
    clock.advance(days=30, minutes=10)  # renewed at the store, RENEWAL webhook not received yet
    revenuecat.add("GET", "/subscribers/", httpx.Response(200, json=subscriber(expires=clock.now() + timedelta(days=30))))
    before = len(rc_requests(revenuecat))
    assert await can_use_app(client, headers)
    assert len(rc_requests(revenuecat)) == before + 1


async def test_lazy_sync_failure_keeps_expired_subscription_blocked(client, user_factory, clock, revenuecat):
    user, headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    revenuecat.add("GET", "/subscribers/", httpx.Response(200, json=subscriber(expires=now + timedelta(days=30))))
    await client.post("/api/v1/subscriptions/sync", headers=headers)
    clock.advance(days=30, minutes=10)
    revenuecat.add("GET", "/subscribers/", httpx.Response(500))
    assert not await can_use_app(client, headers)  # never extended without verification
    calls = len(rc_requests(revenuecat))
    assert not await can_use_app(client, headers)
    assert len(rc_requests(revenuecat)) == calls  # throttled: one attempt per 5 minutes


# 44 ------------------------------------------------------------------------ transfer
async def test_transfer_moves_access_between_users(client, user_factory, clock, revenuecat, db):
    a, a_headers = await expired_trial_user(user_factory, client, clock)
    b, b_headers = await user_factory()
    await client.get("/api/v1/subscriptions/status", headers=b_headers)
    clock.advance(days=4)  # b's trial expired too
    now = clock.now()
    active = httpx.Response(200, json=subscriber(expires=now + timedelta(days=20)))
    empty = httpx.Response(200, json=subscriber(product=None))
    revenuecat.add("GET", f"/subscribers/{a.id}", active)
    revenuecat.add("GET", f"/subscribers/{b.id}", empty)
    await send(client, event("INITIAL_PURCHASE", a.id, at=now, expires=now + timedelta(days=20)))
    assert await can_use_app(client, a_headers) and not await can_use_app(client, b_headers)

    # The store account restores on b: RevenueCat transfers the purchase from a to b.
    revenuecat.add("GET", f"/subscribers/{a.id}", empty)
    revenuecat.add("GET", f"/subscribers/{b.id}", active)
    transfer = {"id": str(uuid.uuid4()), "type": "TRANSFER", "environment": "PRODUCTION", "store": "PLAY_STORE",
                "event_timestamp_ms": int(clock.now().timestamp() * 1000) + 1000,
                "transferred_from": [str(a.id)], "transferred_to": [str(b.id)]}
    r = await send(client, transfer)
    assert r.status_code == 200 and r.json()["data"]["status"] == "processed"
    assert not await can_use_app(client, a_headers)
    assert await can_use_app(client, b_headers)


async def test_transfer_without_api_revokes_sender_only(client, user_factory, clock, db):
    a, a_headers = await expired_trial_user(user_factory, client, clock)
    b, b_headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    await send(client, event("INITIAL_PURCHASE", a.id, at=now, expires=now + timedelta(days=20)))
    transfer = {"id": str(uuid.uuid4()), "type": "TRANSFER", "environment": "PRODUCTION",
                "event_timestamp_ms": int(now.timestamp() * 1000) + 1000,
                "transferred_from": [str(a.id)], "transferred_to": [str(b.id)]}
    assert (await send(client, transfer)).status_code == 200
    assert (await sub_row(db, a.id)).subscription_status == SubscriptionStatus.TRANSFERRED
    assert not await can_use_app(client, a_headers)
    assert not await can_use_app(client, b_headers)  # b needs verified data (sync/restore) first


# history + ownership (37) --------------------------------------------------------------
async def test_history_is_private_and_paginated(client, user_factory, clock):
    a, a_headers = await expired_trial_user(user_factory, client, clock)
    b, b_headers = await user_factory()
    now = clock.now()
    for i in range(3):
        await send(client, event("RENEWAL" if i else "INITIAL_PURCHASE", a.id, at=now + timedelta(days=30 * i),
                                 expires=now + timedelta(days=30 * (i + 1)), transaction_id=f"H{i}"))
    r = await client.get("/api/v1/subscriptions/history?page=1&page_size=2", headers=a_headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["pagination"] == {"page": 1, "page_size": 2, "total": 3, "has_more": True}
    assert [p["transaction_id"] for p in data["items"]] == ["H2", "H1"]
    assert len(data["subscriptions"]) == 1 and data["subscriptions"][0]["product_id"] == INTL
    other = (await client.get("/api/v1/subscriptions/history", headers=b_headers)).json()["data"]
    assert other["items"] == [] and other["subscriptions"] == []
    assert (await client.get("/api/v1/subscriptions/history")).status_code == 401


async def test_webhook_for_one_user_never_grants_another(client, user_factory, clock):
    a, _ = await expired_trial_user(user_factory, client, clock)
    _, b_headers = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    await send(client, event("INITIAL_PURCHASE", a.id, at=now, expires=now + timedelta(days=30)))
    assert not await can_use_app(client, b_headers)


async def test_plans_public_and_not_proof_of_purchase(client, billing_settings):
    r = await client.get("/api/v1/subscriptions/plans")
    assert r.status_code == 200
    data = r.json()["data"]
    by_id = {p["plan_id"]: p for p in data["plans"]}
    assert by_id["international"]["reference_price"] == "10.00" and by_id["international"]["currency"] == "USD"
    assert by_id["pakistan"]["reference_price"] == "499" and by_id["pakistan"]["currency"] == "PKR"
    assert by_id["international"]["product_ids"] == [INTL, f"{INTL}_ios"]
    assert all(p["entitlement_id"] == "app_access" and p["billing_interval"] == "month" for p in data["plans"])
    assert data["trial_days"] == 3 and data["trial_type"] == "app_managed"


@pytest.mark.unit
def test_stored_payload_has_no_personal_attributes():
    from app.services.billing.webhook import _payload_for_storage

    stored = _payload_for_storage(event("RENEWAL", uuid.uuid4(), at=subs.utcnow()))
    assert "subscriber_attributes" not in stored
    assert "private@example.com" not in json.dumps(stored)


# scheduled reconciliation (app/scripts/maintenance.py) -------------------------------
async def test_reprocess_unmatched_event_after_user_appears(client, clock, db):
    from app.models.user import User
    from app.services.billing.webhook import reprocess_events

    uid = uuid.uuid4()
    now = clock.now()
    ev = event("INITIAL_PURCHASE", uid, at=now, expires=now + timedelta(days=30), transaction_id="U1")
    assert (await send(client, ev)).json()["data"]["status"] == "unmatched"
    db.add(User(id=uid, email="late-user@example.com"))
    await db.commit()
    assert await reprocess_events(db, None) == {"retried": 1, "failed": 0}
    assert (await event_row(db, ev["id"])).processing_status == EventProcessingStatus.PROCESSED
    assert len(await payments(db, uid)) == 1


async def test_reconcile_flagged_confirms_with_api(client, user_factory, clock, revenuecat, db, billing_settings):
    from app.services.billing.revenuecat import RevenueCatClient

    user, _ = await expired_trial_user(user_factory, client, clock)
    now = clock.now()
    billing_settings.REVENUECAT_SECRET_API_KEY, key = None, billing_settings.REVENUECAT_SECRET_API_KEY
    await send(client, event("INITIAL_PURCHASE", user.id, at=now, expires=now + timedelta(days=30)))
    assert (await sub_row(db, user.id)).needs_reconcile is True
    billing_settings.REVENUECAT_SECRET_API_KEY = key
    revenuecat.add("GET", "/subscribers/", httpx.Response(200, json=subscriber(expires=now + timedelta(days=30))))
    assert await subs.reconcile_flagged(db, RevenueCatClient()) == {"synced": 1, "failed": 0}
    assert (await sub_row(db, user.id)).needs_reconcile is False
