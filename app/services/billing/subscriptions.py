"""Subscription state, payment history and the central app-access decision.

Sources of truth, in order of trust:

1. ``GET /v1/subscribers/{app_user_id}`` (RevenueCat API) -- :func:`sync_customer`
   overwrites local state for the user and clears ``needs_reconcile``.
2. Authenticated RevenueCat webhooks -- :func:`apply_event` applies event data
   with an ordering guard (``last_event_at`` / ``last_synced_at``), so a delayed
   or out-of-order event never overwrites newer state, and flags the row for
   reconciliation with the API.

Freshness policy (see docs/subscription_backend.md):

* Access is granted only up to the *verified* ``expiration_at`` (or grace-period
  end). Nothing extends access beyond a time RevenueCat reported.
* A RevenueCat outage never shortens verified access: local rows are not touched
  when the API cannot be reached.
* When a renewing subscription passes its expiration (renewal webhook not seen
  yet) or a row awaits reconciliation, the access check performs at most one
  RevenueCat lookup per user every ``LAZY_SYNC_INTERVAL``; if that fails, access
  stays blocked until verified data arrives.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.models import base as model_base
from app.models.subscription import (
    PaymentHistory,
    Subscription,
    SubscriptionStatus,
    TransactionStatus,
)
from app.models.user import User
from app.services.billing import trial as trial_service
from app.services.billing.revenuecat import (
    CustomerInfo,
    RevenueCatClient,
    ms_to_datetime,
    parse_rc_datetime,
)

logger = logging.getLogger(__name__)

LAZY_SYNC_INTERVAL = timedelta(minutes=5)
LAZY_SYNC_LOOKBACK = timedelta(days=3)

# Webhook event types (https://www.revenuecat.com/docs/integrations/webhooks/event-types-and-fields).
GRANTING_EVENTS = {
    "INITIAL_PURCHASE",
    "RENEWAL",
    "UNCANCELLATION",
    "SUBSCRIPTION_EXTENDED",
    "TEMPORARY_ENTITLEMENT_GRANT",
    "NON_RENEWING_PURCHASE",
    "REFUND_REVERSED",
}
STATE_EVENTS = GRANTING_EVENTS | {
    "CANCELLATION",
    "EXPIRATION",
    "BILLING_ISSUE",
    "SUBSCRIPTION_PAUSED",
    "PRODUCT_CHANGE",
}
PAYMENT_EVENTS = {"INITIAL_PURCHASE", "RENEWAL", "NON_RENEWING_PURCHASE"}
# RevenueCat reports refunds as CANCELLATION with cancel_reason CUSTOMER_SUPPORT.
REFUND_CANCEL_REASON = "CUSTOMER_SUPPORT"

INACTIVE_STATUSES = {SubscriptionStatus.TRANSFERRED, SubscriptionStatus.REVOKED}


def utcnow() -> datetime:
    return model_base.utcnow()


def _insert(db: AsyncSession):  # type: ignore[no-untyped-def]
    return pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert


def normalize_store(store: Any) -> str:
    return str(store or "unknown").strip().lower()[:32] or "unknown"


def normalize_environment(value: Any, *, sandbox: bool | None = None) -> str:
    if sandbox is not None:
        return "SANDBOX" if sandbox else "PRODUCTION"
    env = str(value or "PRODUCTION").strip().upper()
    return env if env in {"SANDBOX", "PRODUCTION"} else "PRODUCTION"


def environment_allowed(environment: str) -> bool:
    return environment != "SANDBOX" or get_settings().revenuecat_allow_sandbox


def _product_base(product_id: str) -> str:
    # Google Play products may be reported as "subscription_id:base_plan_id".
    return product_id.split(":", 1)[0]


def plan_for_product(product_id: str | None) -> str | None:
    if not product_id:
        return None
    s = get_settings()
    for plan, ids in (
        ("international", s.revenuecat_products_international),
        ("pakistan", s.revenuecat_products_pakistan),
    ):
        if any(product_id == p or _product_base(product_id) == _product_base(p) for p in ids):
            return plan
    return None


def _same_product(a: str | None, b: str | None) -> bool:
    return bool(a and b) and (a == b or _product_base(a) == _product_base(b))  # type: ignore[arg-type]


def entitlement_window_open(sub: Subscription, now: datetime) -> bool:
    if sub.expiration_at is None or sub.expiration_at > now:
        return True
    return sub.grace_period_expires_at is not None and sub.grace_period_expires_at > now


def grants_access(sub: Subscription, now: datetime) -> bool:
    """Verified, unexpired ``app_access`` entitlement held by this row."""
    return (
        sub.entitlement_active
        and sub.subscription_status not in INACTIVE_STATUSES
        and environment_allowed(sub.environment)
        and entitlement_window_open(sub, now)
    )


async def list_subscriptions(db: AsyncSession, user_id: uuid.UUID) -> list[Subscription]:
    rows = await db.execute(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.updated_at.desc())
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def _get_or_create(
    db: AsyncSession, user_id: uuid.UUID, product_id: str, store: str, environment: str
) -> Subscription:
    now = utcnow()
    await db.execute(
        _insert(db)(Subscription)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            revenuecat_app_user_id=str(user_id),
            store=store,
            product_id=product_id[:200],
            entitlement_id=get_settings().REVENUECAT_ENTITLEMENT_ID,
            subscription_status=SubscriptionStatus.EXPIRED,
            entitlement_active=False,
            is_trial=False,
            will_renew=False,
            environment=environment,
            needs_reconcile=False,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["user_id", "product_id"])
    )
    row = (
        await db.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.product_id == product_id[:200])
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


# --------------------------------------------------------------------------- API sync
async def sync_customer(db: AsyncSession, user: User, client: RevenueCatClient) -> CustomerInfo:
    """Replace the user's local subscription state with RevenueCat's CustomerInfo.

    The App User ID is always the authenticated user's own id. Raises (and leaves
    local data untouched) when RevenueCat cannot be reached or answers invalidly.
    """
    app_user_id = str(user.id)
    await _note_sync_attempt(db, user.id)
    info = await client.get_customer(app_user_id)
    if info.original_app_user_id and info.original_app_user_id != app_user_id:
        # Customer aliasing (e.g. an anonymous purchase later identified as this user).
        logger.info("RevenueCat customer is aliased to another original App User ID")

    settings = get_settings()
    now = utcnow()
    checked_at = info.request_date
    entitlement = info.entitlements.get(settings.REVENUECAT_ENTITLEMENT_ID)
    ent_product = entitlement.get("product_identifier") if entitlement else None
    ent_expires = parse_rc_datetime(entitlement.get("expires_date")) if entitlement else None
    ent_grace = parse_rc_datetime(entitlement.get("grace_period_expires_date")) if entitlement else None
    ent_open = entitlement is not None and (
        ent_expires is None or ent_expires > now or (ent_grace is not None and ent_grace > now)
    )

    seen: set[str] = set()
    any_active = False
    for product_id, data in info.subscriptions.items():
        product_id = str(product_id)[:200]
        seen.add(product_id)
        environment = normalize_environment(None, sandbox=bool(data.get("is_sandbox")))
        row = await _get_or_create(db, user.id, product_id, normalize_store(data.get("store")), environment)
        expires = parse_rc_datetime(data.get("expires_date"))
        grace = parse_rc_datetime(data.get("grace_period_expires_date"))
        refunded = parse_rc_datetime(data.get("refunded_at"))
        unsubscribed = parse_rc_datetime(data.get("unsubscribe_detected_at"))
        billing = parse_rc_datetime(data.get("billing_issues_detected_at"))
        window = expires is None or expires > now or (grace is not None and grace > now)

        if refunded is not None:
            status = SubscriptionStatus.REFUNDED
        elif not window:
            status = SubscriptionStatus.PAUSED if data.get("auto_resume_date") else SubscriptionStatus.EXPIRED
        elif billing is not None:
            status = SubscriptionStatus.BILLING_ISSUE
        elif unsubscribed is not None:
            status = SubscriptionStatus.CANCELLED
        else:
            status = SubscriptionStatus.ACTIVE

        holds_entitlement = ent_open and _same_product(ent_product, product_id)
        row.store = normalize_store(data.get("store"))
        row.environment = environment
        row.entitlement_id = settings.REVENUECAT_ENTITLEMENT_ID
        row.entitlement_active = bool(holds_entitlement and environment_allowed(environment))
        row.subscription_status = status
        row.is_trial = str(data.get("period_type") or "").lower() == "trial"
        row.current_period_start = parse_rc_datetime(data.get("purchase_date"))
        row.current_period_end = expires
        row.expiration_at = expires
        row.grace_period_expires_at = grace
        row.billing_issue_detected_at = billing
        row.cancellation_at = unsubscribed
        row.refunded_at = refunded
        row.will_renew = window and unsubscribed is None and refunded is None
        if data.get("store_transaction_id"):
            row.latest_transaction_id = str(data["store_transaction_id"])[:200]
        row.last_synced_at = checked_at
        row.needs_reconcile = False
        any_active = any_active or row.entitlement_active
        if plan_for_product(product_id) is None and row.entitlement_active:
            logger.warning("Entitlement granted by a product that is not in REVENUECAT_PRODUCT_*")

    # Entitlement granted without a subscription entry (e.g. promotional grant).
    if ent_open and ent_product and str(ent_product)[:200] not in seen:
        product_id = str(ent_product)[:200]
        seen.add(product_id)
        row = await _get_or_create(db, user.id, product_id, "promotional", "PRODUCTION")
        row.entitlement_active = True
        row.subscription_status = SubscriptionStatus.ACTIVE
        row.expiration_at = row.current_period_end = ent_expires
        row.grace_period_expires_at = ent_grace
        row.current_period_start = parse_rc_datetime(entitlement.get("purchase_date")) if entitlement else None
        row.will_renew = False
        row.last_synced_at = checked_at
        row.needs_reconcile = False
        any_active = True

    # Anything RevenueCat no longer reports for this customer is not theirs any more.
    await db.flush()  # the session does not autoflush; the re-select must not discard edits
    for row in await list_subscriptions(db, user.id):
        if row.product_id in seen:
            continue
        if row.subscription_status not in INACTIVE_STATUSES:
            row.subscription_status = SubscriptionStatus.REVOKED
        row.entitlement_active = False
        row.will_renew = False
        row.last_synced_at = checked_at
        row.needs_reconcile = False

    if any_active:
        await trial_service.mark_converted(db, user.id)
    await db.commit()
    logger.info("Synchronized subscription state from RevenueCat (entitlement_active=%s)", any_active)
    return info


async def _note_sync_attempt(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(
        update(Subscription)
        .where(Subscription.user_id == user_id)
        .values(last_sync_attempt_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    await db.commit()


# --------------------------------------------------------------------------- webhook events
def event_entitlement_granted(event: dict[str, Any]) -> bool:
    wanted = get_settings().REVENUECAT_ENTITLEMENT_ID
    ids = event.get("entitlement_ids")
    if isinstance(ids, list) and wanted in ids:
        return True
    return event.get("entitlement_id") == wanted  # deprecated single-id field


@dataclass
class EventOutcome:
    applied: bool
    note: str | None = None


async def apply_event(db: AsyncSession, user_id: uuid.UUID, event: dict[str, Any]) -> EventOutcome:
    """Apply one subscription event for one user (caller commits)."""
    event_type = str(event.get("type") or "")
    product_id = event.get("product_id")
    if not product_id or not isinstance(product_id, str):
        return EventOutcome(False, "missing product_id")
    environment = normalize_environment(event.get("environment"))
    store = normalize_store(event.get("store"))
    row = await _get_or_create(db, user_id, product_id, store, environment)
    now = utcnow()
    event_at = ms_to_datetime(event.get("event_timestamp_ms")) or now

    await _record_payment(db, user_id, row, event, environment)

    guard = max((t for t in (row.last_event_at, row.last_synced_at) if t is not None), default=None)
    if guard is not None and event_at < guard:
        return EventOutcome(False, "stale event (newer state already applied)")

    expiration = ms_to_datetime(event.get("expiration_at_ms"))
    granted = event_entitlement_granted(event)
    env_ok = environment_allowed(environment)
    row.store = store
    row.environment = environment

    if event_type in GRANTING_EVENTS:
        window = expiration is None or expiration > now
        row.subscription_status = SubscriptionStatus.ACTIVE if window else SubscriptionStatus.EXPIRED
        row.entitlement_active = bool(granted and window and env_ok)
        row.will_renew = window and event_type not in {"NON_RENEWING_PURCHASE", "TEMPORARY_ENTITLEMENT_GRANT"}
        row.current_period_start = ms_to_datetime(event.get("purchased_at_ms")) or row.current_period_start
        row.current_period_end = expiration
        row.expiration_at = expiration
        row.is_trial = str(event.get("period_type") or "").upper() == "TRIAL"
        row.cancellation_at = None
        if event_type in {"INITIAL_PURCHASE", "RENEWAL", "REFUND_REVERSED", "UNCANCELLATION"}:
            row.billing_issue_detected_at = None
            row.grace_period_expires_at = None
            row.refunded_at = None
        if event.get("transaction_id"):
            row.latest_transaction_id = str(event["transaction_id"])[:200]
        if event.get("original_transaction_id"):
            row.original_transaction_id = str(event["original_transaction_id"])[:200]
        if granted and window and plan_for_product(product_id) is None:
            logger.warning("Entitlement granted by a product that is not in REVENUECAT_PRODUCT_*")
    elif event_type == "CANCELLATION":
        row.will_renew = False
        if expiration is not None:
            row.expiration_at = expiration
        window = entitlement_window_open(row, now)
        if str(event.get("cancel_reason") or "").upper() == REFUND_CANCEL_REASON:
            row.subscription_status = SubscriptionStatus.REFUNDED
            row.refunded_at = event_at
        else:
            row.cancellation_at = event_at
            row.subscription_status = SubscriptionStatus.CANCELLED if window else SubscriptionStatus.EXPIRED
        row.entitlement_active = bool(row.entitlement_active and window)
    elif event_type == "EXPIRATION":
        if row.expiration_at and expiration and expiration < row.expiration_at:
            return EventOutcome(False, "stale expiration for an earlier period")
        paused = str(event.get("expiration_reason") or "").upper() == "SUBSCRIPTION_PAUSED"
        row.subscription_status = SubscriptionStatus.PAUSED if paused else SubscriptionStatus.EXPIRED
        row.expiration_at = expiration or event_at
        row.current_period_end = row.expiration_at
        row.grace_period_expires_at = None
        row.entitlement_active = False
        row.will_renew = False
    elif event_type == "BILLING_ISSUE":
        row.subscription_status = SubscriptionStatus.BILLING_ISSUE
        row.billing_issue_detected_at = event_at
        row.grace_period_expires_at = ms_to_datetime(event.get("grace_period_expiration_at_ms"))
        if expiration is not None:
            row.expiration_at = expiration
        row.entitlement_active = bool(
            (row.entitlement_active or granted) and env_ok and entitlement_window_open(row, now)
        )
    elif event_type == "SUBSCRIPTION_PAUSED":
        # Play pauses take effect at the end of the paid period: access continues until then.
        row.subscription_status = SubscriptionStatus.PAUSED
        row.will_renew = False
        row.entitlement_active = bool(row.entitlement_active and entitlement_window_open(row, now))
    elif event_type == "PRODUCT_CHANGE":
        pass  # informational; the new product arrives via RENEWAL/INITIAL_PURCHASE + API sync
    else:
        return EventOutcome(False, "event type does not change subscription state")

    row.last_event_at = event_at
    row.needs_reconcile = True  # cleared once the RevenueCat API confirms the state
    if row.entitlement_active:
        await trial_service.mark_converted(db, user_id)
    return EventOutcome(True)


async def apply_transfer_away(db: AsyncSession, user_id: uuid.UUID) -> int:
    """TRANSFER: the purchases now belong to another App User ID."""
    rows = await list_subscriptions(db, user_id)
    for row in rows:
        row.subscription_status = SubscriptionStatus.TRANSFERRED
        row.entitlement_active = False
        row.will_renew = False
        row.needs_reconcile = True
    return len(rows)


def _amount(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.0001"))
    except (InvalidOperation, ValueError):
        return None


async def _record_payment(
    db: AsyncSession, user_id: uuid.UUID, row: Subscription, event: dict[str, Any], environment: str
) -> None:
    """Idempotent: one row per (store, transaction_id); refunds update the status."""
    event_type = str(event.get("type") or "")
    transaction_id = event.get("transaction_id")
    if not transaction_id:
        return
    transaction_id = str(transaction_id)[:200]
    refund = event_type == "CANCELLATION" and str(event.get("cancel_reason") or "").upper() == REFUND_CANCEL_REASON
    if event_type not in PAYMENT_EVENTS and not refund and event_type != "REFUND_REVERSED":
        return
    status = TransactionStatus.REFUNDED if refund else TransactionStatus.COMPLETED
    now = utcnow()
    currency = event.get("currency")
    values = {
        "id": uuid.uuid4(),
        "user_id": user_id,
        "subscription_id": row.id,
        "transaction_id": transaction_id,
        "original_transaction_id": (str(event["original_transaction_id"])[:200] if event.get("original_transaction_id") else None),
        "product_id": row.product_id,
        "store": row.store,
        "transaction_amount": _amount(event.get("price_in_purchased_currency")) if not refund else None,
        "currency": str(currency).upper()[:3] if currency else None,
        "transaction_status": status,
        "transaction_date": ms_to_datetime(event.get("purchased_at_ms")) or ms_to_datetime(event.get("event_timestamp_ms")),
        "environment": environment,
        "revenuecat_event_id": str(event.get("id"))[:128] if event.get("id") else None,
        "created_at": now,
        "updated_at": now,
    }
    stmt = _insert(db)(PaymentHistory).values(**values)
    if event_type in PAYMENT_EVENTS:
        await db.execute(stmt.on_conflict_do_nothing(index_elements=["store", "transaction_id"]))
    else:  # refund / refund reversal: change the status of the existing transaction
        await db.execute(
            stmt.on_conflict_do_update(
                index_elements=["store", "transaction_id"],
                set_={"transaction_status": status, "updated_at": now},
            )
        )


# --------------------------------------------------------------------------- access decision
@dataclass(frozen=True)
class AccessDecision:
    user_id: uuid.UUID
    allowed: bool
    access_status: str
    trial: trial_service.TrialState | None
    subscription: Subscription | None
    entitlement_active: bool

    def summary(self) -> dict[str, Any]:
        sub = self.subscription
        return {
            "access_status": self.access_status,
            "trial_status": self.trial.status.value if self.trial else None,
            "trial_ends_at": self.trial.ends_at.isoformat() if self.trial else None,
            "subscription_status": sub.subscription_status.value if sub else None,
            "current_period_end": sub.current_period_end.isoformat() if sub and sub.current_period_end else None,
        }


def _primary(rows: list[Subscription], now: datetime) -> Subscription | None:
    if not rows:
        return None
    active = [r for r in rows if grants_access(r, now)]
    pool = active or rows
    return max(pool, key=lambda r: (r.expiration_at or datetime.max.replace(tzinfo=now.tzinfo), r.updated_at))


def _needs_lazy_sync(rows: list[Subscription], now: datetime) -> bool:
    for row in rows:
        if row.last_sync_attempt_at is not None and row.last_sync_attempt_at > now - LAZY_SYNC_INTERVAL:
            return False
    for row in rows:
        if row.needs_reconcile:
            return True
        if row.will_renew and row.expiration_at and now - LAZY_SYNC_LOOKBACK < row.expiration_at <= now:
            return True
    return False


async def evaluate_access(
    db: AsyncSession, user: User, client: RevenueCatClient | None = None, *, allow_sync: bool = True
) -> AccessDecision:
    """Decide whether ``user`` may use the app right now (trusted server data only)."""
    settings = get_settings()
    now = utcnow()
    trial_row = await trial_service.ensure_trial(db, user.id)
    trial = trial_service.trial_state(trial_row, now)
    await trial_service.refresh_stored_status(db, trial_row, trial)
    rows = await list_subscriptions(db, user.id)

    if (
        allow_sync
        and client is not None
        and client.configured
        and not any(grants_access(r, now) for r in rows)
        and not trial.active
        and _needs_lazy_sync(rows, now)
    ):
        try:
            await sync_customer(db, user, client)
        except AppError as exc:  # raised before any local change; verified rows stay as they are
            logger.warning("Lazy RevenueCat reconciliation failed: %s", exc.code)
        rows = await list_subscriptions(db, user.id)
        now = utcnow()

    primary = _primary(rows, now)
    entitled = primary is not None and grants_access(primary, now)

    if user.role.value in settings.subscription_exempt_roles:
        status, allowed = "exempt", True
    elif entitled:
        assert primary is not None
        allowed = True
        if primary.subscription_status == SubscriptionStatus.BILLING_ISSUE:
            status = "billing_issue"
        elif not primary.will_renew:
            status = "subscription_cancelled_but_active"
        else:
            status = "subscription_active"
    elif trial.active:
        status, allowed = "trial_active", True
    elif primary is not None:
        allowed = False
        billing = primary.subscription_status == SubscriptionStatus.BILLING_ISSUE
        status = "billing_issue" if billing else "subscription_expired"
    else:
        status, allowed = "payment_required", False

    if not settings.SUBSCRIPTION_ENFORCEMENT_ENABLED:
        allowed = True
    return AccessDecision(user.id, allowed, status, trial, primary, entitled)


async def reconcile_flagged(db: AsyncSession, client: RevenueCatClient, *, limit: int = 200) -> dict[str, int]:
    """Confirm webhook-applied state with the RevenueCat API (rows with ``needs_reconcile``)."""
    user_ids = (
        await db.execute(
            select(Subscription.user_id).where(Subscription.needs_reconcile.is_(True)).distinct().limit(limit)
        )
    ).scalars().all()
    counts = {"synced": 0, "failed": 0}
    for user_id in user_ids:
        user = await db.get(User, user_id)
        if user is None:
            continue
        try:
            await sync_customer(db, user, client)
            counts["synced"] += 1
        except AppError as exc:
            logger.warning("Scheduled RevenueCat reconciliation failed: %s", exc.code)
            counts["failed"] += 1
    return counts
