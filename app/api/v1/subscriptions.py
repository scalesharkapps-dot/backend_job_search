"""App-access subscription endpoints (3-day app trial + RevenueCat monthly subscription).

Everything here stays reachable after the trial ends: users must be able to see
why access is blocked, subscribe, and restore purchases. Client-supplied
subscription data is never accepted; state comes from RevenueCat only.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DBSession
from app.core.config import get_settings
from app.core.exceptions import NotConfiguredError
from app.core.rate_limit import enforce_user_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok, paginate
from app.models.subscription import PaymentHistory
from app.models.user import User
from app.schemas.subscription import (
    PaymentOut,
    PlansOut,
    SubscriptionOut,
    SubscriptionStatusOut,
    SubscriptionSyncOut,
    WebhookAckOut,
    WebhookIn,
)
from app.services.billing import subscriptions as subs
from app.services.billing import trial as trial_service
from app.services.billing import webhook as webhook_service
from app.services.billing.revenuecat import RevenueCatClient, get_revenuecat_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/subscriptions", tags=["Subscriptions"], responses=ERROR_RESPONSES)

RevenueCat = Annotated[RevenueCatClient, Depends(get_revenuecat_client)]
SYNC_LIMIT_PER_MINUTE = 6


def _status_payload(decision: subs.AccessDecision) -> dict[str, Any]:
    sub = decision.subscription
    trial = decision.trial
    return {
        "user_id": decision.user_id,
        "access_allowed": decision.allowed,
        "access_status": decision.access_status,
        "trial_status": trial.status.value if trial else None,
        "trial_started_at": trial.started_at if trial else None,
        "trial_ends_at": trial.ends_at if trial else None,
        "trial_remaining_seconds": trial.remaining_seconds if trial else 0,
        "subscription_status": sub.subscription_status.value if sub else None,
        "product_id": sub.product_id if sub else None,
        "plan": subs.plan_for_product(sub.product_id) if sub else None,
        "store": sub.store if sub else None,
        "current_period_end": sub.current_period_end if sub else None,
        "will_renew": sub.will_renew if sub else None,
        "entitlement_active": decision.entitlement_active,
        "environment": sub.environment if sub else None,
        "last_verified_at": sub.last_synced_at if sub else None,
    }


@router.get("/status", response_model=ApiResponse[SubscriptionStatusOut], summary="My app access and subscription status")
async def status(user: CurrentUser, db: DBSession, client: RevenueCat) -> dict[str, Any]:
    decision = await subs.evaluate_access(db, user, client)
    return ok(_status_payload(decision))


async def _sync(user: User, db: DBSession, client: RevenueCatClient) -> dict[str, Any]:
    enforce_user_limit("subscription_sync", user.id, SYNC_LIMIT_PER_MINUTE, 60)
    if not client.configured:
        raise NotConfiguredError(
            "Subscription verification is not configured on the server.",
            code="subscriptions_not_configured",
        )
    info = await subs.sync_customer(db, user, client)
    decision = await subs.evaluate_access(db, user, client, allow_sync=False)
    payload = _status_payload(decision)
    payload.update(synced=True, revenuecat_customer_found=not info.created and bool(info.subscriptions or info.entitlements))
    return payload


@router.post("/sync", response_model=ApiResponse[SubscriptionSyncOut], summary="Re-verify my subscription with RevenueCat")
async def sync(user: CurrentUser, db: DBSession, client: RevenueCat) -> dict[str, Any]:
    """Fetches the caller's own RevenueCat customer (App User ID = authenticated user id)
    and stores the verified result. Takes no subscription data from the client."""
    return ok(await _sync(user, db, client), "Subscription status synchronized")


@router.post("/restore", response_model=ApiResponse[SubscriptionSyncOut], summary="Verify restored purchases")
async def restore(user: CurrentUser, db: DBSession, client: RevenueCat) -> dict[str, Any]:
    """Call after the app restored purchases through the store SDK. The backend only
    trusts what RevenueCat now reports for this user."""
    payload = await _sync(user, db, client)
    message = "Subscription restored" if payload["entitlement_active"] else "No active subscription found to restore"
    return ok(payload, message)


@router.get("/history", response_model=ApiResponse[dict[str, Any]], summary="My subscriptions and verified transactions")
async def history(
    user: CurrentUser,
    db: DBSession,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, Any]:
    subscriptions = await subs.list_subscriptions(db, user.id)
    total = await db.scalar(select(func.count()).select_from(PaymentHistory).where(PaymentHistory.user_id == user.id))
    payments = (
        await db.execute(
            select(PaymentHistory)
            .where(PaymentHistory.user_id == user.id)
            .order_by(PaymentHistory.transaction_date.desc().nulls_last(), PaymentHistory.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    data = paginate(
        [PaymentOut.model_validate(p).model_dump(mode="json") for p in payments], page, page_size, int(total or 0)
    )
    data["subscriptions"] = [SubscriptionOut.model_validate(s).model_dump(mode="json") for s in subscriptions]
    return ok(data)


@router.get("/plans", response_model=ApiResponse[PlansOut], summary="Subscription plans (no authentication)")
async def plans() -> dict[str, Any]:
    s = get_settings()
    common = {"entitlement_id": s.REVENUECAT_ENTITLEMENT_ID, "offering_id": s.REVENUECAT_OFFERING_ID}
    return ok(
        {
            "plans": [
                {"plan_id": "international", "name": "SharkHire Monthly", "reference_price": "10.00",
                 "currency": "USD", "product_ids": s.revenuecat_products_international, **common},
                {"plan_id": "pakistan", "name": "SharkHire Monthly (Pakistan)", "reference_price": "499",
                 "currency": "PKR", "product_ids": s.revenuecat_products_pakistan, **common},
            ],
            "trial_days": trial_service.TRIAL_DURATION.days,
            "note": (
                "Reference prices only. The charged, localized price is shown by Google Play / the App Store; "
                "access is granted only after RevenueCat verifies the purchase."
            ),
        }
    )


def _webhook_auth(authorization: Annotated[str | None, Header()] = None) -> None:
    # A dependency so unauthorized calls are rejected before the body is even validated.
    webhook_service.verify_authorization(authorization)


@router.post(
    "/webhook",
    response_model=ApiResponse[WebhookAckOut],
    dependencies=[Depends(_webhook_auth)],
    summary="RevenueCat webhook receiver (RevenueCat only)",
    responses={401: {"description": "Invalid authorization"}, 500: {"description": "Processing failed; retried"}},
)
async def webhook(
    body: WebhookIn,
    db: DBSession,
    client: RevenueCat,
) -> dict[str, Any]:
    """Authenticated with the shared secret configured in RevenueCat (``Authorization`` header).
    Idempotent per event id; non-2xx responses make RevenueCat retry."""
    result = await webhook_service.handle_event(db, body.event_dict(), client)
    message = "Duplicate event acknowledged" if result.duplicate else "Event processed"
    return ok({"event_id": result.event_id, "status": result.status, "duplicate": result.duplicate}, message)
