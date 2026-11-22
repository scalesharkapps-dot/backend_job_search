"""RevenueCat webhook intake: authorization, idempotent storage and processing.

Flow for every delivery:

1. Constant-time check of the ``Authorization`` header against
   ``REVENUECAT_WEBHOOK_AUTHORIZATION`` (fails closed when not configured).
2. The event is stored (``INSERT ... ON CONFLICT DO NOTHING`` on the unique
   RevenueCat event id) *before* processing. Subscriber attributes are dropped.
3. The row is *claimed* with an atomic conditional UPDATE, so concurrent
   deliveries of the same event are processed once. Already processed events are
   acknowledged with 200 (replay-safe).
4. State is applied in one transaction, then reconciled with the RevenueCat API.
5. On an unexpected failure the transaction is rolled back, the event is marked
   ``failed`` and HTTP 500 is returned so RevenueCat retries; a retry re-claims it.
"""

from __future__ import annotations

import hmac
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, AuthenticationError, NotConfiguredError
from app.models.subscription import EventProcessingStatus as Status
from app.models.subscription import SubscriptionEvent
from app.models.user import User
from app.services.billing import subscriptions as subs
from app.services.billing.revenuecat import RevenueCatClient, ms_to_datetime

logger = logging.getLogger(__name__)

STALE_CLAIM = timedelta(minutes=10)
FINAL_STATUSES = {Status.PROCESSED, Status.IGNORED, Status.UNMATCHED}


def verify_authorization(header: str | None) -> None:
    secret = get_settings().REVENUECAT_WEBHOOK_AUTHORIZATION
    if not secret or not secret.get_secret_value():
        logger.error("RevenueCat webhook received but REVENUECAT_WEBHOOK_AUTHORIZATION is not set")
        raise NotConfiguredError("Webhook receiver is not configured.", code="webhook_not_configured")
    expected = secret.get_secret_value().encode()
    supplied = (header or "").strip().encode()
    if not supplied or not (
        hmac.compare_digest(supplied, expected) or hmac.compare_digest(supplied, b"Bearer " + expected)
    ):
        logger.warning("Rejected RevenueCat webhook with invalid authorization")
        raise AuthenticationError("Invalid webhook authorization.", code="invalid_webhook_authorization")


@dataclass
class WebhookResult:
    event_id: str
    status: str
    duplicate: bool
    note: str | None = None


def _payload_for_storage(event: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in event.items() if k != "subscriber_attributes"}


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None  # e.g. "$RCAnonymousID:..." -- never a SharkHire user


async def _local_user(db: AsyncSession, app_user_id: Any) -> User | None:
    user_id = _uuid(app_user_id)
    return await db.get(User, user_id) if user_id else None


async def _resolve_user(db: AsyncSession, event: dict[str, Any]) -> User | None:
    """Prefer ``app_user_id``; fall back to the original id / aliases (all must be SharkHire ids)."""
    candidates: list[Any] = [event.get("app_user_id"), event.get("original_app_user_id")]
    aliases = event.get("aliases")
    if isinstance(aliases, list):
        candidates.extend(aliases)
    for candidate in candidates:
        user = await _local_user(db, candidate)
        if user is not None:
            return user
    return None


async def _store(db: AsyncSession, event: dict[str, Any]) -> SubscriptionEvent:
    event_id = str(event["id"])
    now = subs.utcnow()
    await db.execute(
        subs._insert(db)(SubscriptionEvent)
        .values(
            id=uuid.uuid4(),
            revenuecat_event_id=event_id,
            app_user_id=str(event.get("app_user_id"))[:256] if event.get("app_user_id") else None,
            event_type=str(event["type"])[:64],
            environment=subs.normalize_environment(event.get("environment")),
            event_timestamp=ms_to_datetime(event.get("event_timestamp_ms")),
            event_payload=_payload_for_storage(event),
            processing_status=Status.RECEIVED,
            processing_attempts=0,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["revenuecat_event_id"])
    )
    await db.commit()
    row = (
        await db.execute(
            select(SubscriptionEvent)
            .where(SubscriptionEvent.revenuecat_event_id == event_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


async def _claim(db: AsyncSession, row: SubscriptionEvent) -> bool:
    now = subs.utcnow()
    result = await db.execute(
        update(SubscriptionEvent)
        .where(
            SubscriptionEvent.id == row.id,
            or_(
                SubscriptionEvent.processing_status.in_([Status.RECEIVED, Status.FAILED]),
                (SubscriptionEvent.processing_status == Status.PROCESSING)
                & (SubscriptionEvent.updated_at < now - STALE_CLAIM),
            ),
        )
        .values(
            processing_status=Status.PROCESSING,
            processing_attempts=SubscriptionEvent.processing_attempts + 1,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount == 1


async def _finish(
    db: AsyncSession, row_id: uuid.UUID, status: Status, note: str | None, user_id: uuid.UUID | None
) -> None:
    now = subs.utcnow()
    await db.execute(
        update(SubscriptionEvent)
        .where(SubscriptionEvent.id == row_id)
        .values(
            processing_status=status,
            last_error=note[:500] if note else None,
            processed_at=now if status in FINAL_STATUSES else None,
            user_id=user_id,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def _reconcile(db: AsyncSession, user: User, client: RevenueCatClient | None) -> str | None:
    if client is None or not client.configured:
        return "applied from webhook; RevenueCat API not configured"
    try:
        await subs.sync_customer(db, user, client)
    except AppError as exc:  # raised before any local change; nothing to roll back
        logger.warning("Webhook reconciliation with RevenueCat failed: %s", exc.code)
        return f"applied from webhook; reconcile pending ({exc.code})"
    return None


async def _process(
    db: AsyncSession, event: dict[str, Any], client: RevenueCatClient | None
) -> tuple[Status, str | None, uuid.UUID | None]:
    event_type = str(event.get("type"))
    environment = subs.normalize_environment(event.get("environment"))
    if event_type == "TEST":
        return Status.IGNORED, "test event", None
    if not subs.environment_allowed(environment):
        logger.warning("Ignored %s webhook from the %s environment", event_type, environment)
        return Status.IGNORED, f"{environment} events are not accepted", None

    if event_type == "TRANSFER":
        senders = event.get("transferred_from") if isinstance(event.get("transferred_from"), list) else []
        receivers = event.get("transferred_to") if isinstance(event.get("transferred_to"), list) else []
        from_users = [u for u in [await _local_user(db, i) for i in senders] if u is not None]
        to_users = [u for u in [await _local_user(db, i) for i in receivers] if u is not None]
        if not from_users and not to_users:
            return Status.UNMATCHED, "no SharkHire user in transfer", None
        for user in from_users:
            await subs.apply_transfer_away(db, user.id)
        await db.commit()
        notes = [n for u in from_users + to_users if (n := await _reconcile(db, u, client))]
        primary = (to_users or from_users)[0].id
        return Status.PROCESSED, "; ".join(dict.fromkeys(notes)) or None, primary

    if event_type not in subs.STATE_EVENTS:
        return Status.IGNORED, "event type not used for app access", None

    user = await _resolve_user(db, event)
    if user is None:
        logger.warning("RevenueCat %s webhook for an App User ID with no SharkHire user", event_type)
        return Status.UNMATCHED, "no SharkHire user for app_user_id", None
    outcome = await subs.apply_event(db, user.id, event)
    await db.commit()
    reconcile_note = await _reconcile(db, user, client)
    note = "; ".join(n for n in (outcome.note, reconcile_note) if n) or None
    return Status.PROCESSED, note, user.id


async def handle_event(
    db: AsyncSession, event: dict[str, Any], client: RevenueCatClient | None
) -> WebhookResult:
    row = await _store(db, event)
    # Plain values: a rollback expires ORM objects, and async sessions cannot lazy-load them.
    row_id, event_id = row.id, row.revenuecat_event_id
    if row.processing_status in FINAL_STATUSES:
        return WebhookResult(event_id, row.processing_status.value, duplicate=True)
    if not await _claim(db, row):
        raise AppError(
            "This event is already being processed.", code="event_in_progress", status_code=409
        )
    try:
        status, note, user_id = await _process(db, event, client)
    except Exception as exc:
        await db.rollback()
        logger.exception("RevenueCat webhook processing failed (event type %s)", event.get("type"))
        await _finish(db, row_id, Status.FAILED, type(exc).__name__, None)
        raise AppError(
            "The event could not be processed and will be retried.",
            code="webhook_processing_failed",
            status_code=500,
        ) from None
    await _finish(db, row_id, status, note, user_id)
    logger.info("RevenueCat %s webhook %s", event.get("type"), status.value)
    return WebhookResult(event_id, status.value, duplicate=False, note=note)


async def reprocess_events(
    db: AsyncSession, client: RevenueCatClient | None, *, limit: int = 100
) -> dict[str, int]:
    """Retry events left ``failed`` (RevenueCat stops retrying after a few attempts) and
    ``unmatched`` ones whose user may exist now. Uses the stored payload."""
    rows = (
        await db.execute(
            select(SubscriptionEvent.revenuecat_event_id, SubscriptionEvent.event_payload)
            .where(SubscriptionEvent.processing_status.in_([Status.FAILED, Status.UNMATCHED]))
            .order_by(SubscriptionEvent.event_timestamp.asc().nulls_first())
            .limit(limit)
        )
    ).all()
    counts = {"retried": 0, "failed": 0}
    for event_id, payload in rows:
        # UNMATCHED is final for webhooks; reset it so the claim succeeds on this retry.
        await db.execute(
            update(SubscriptionEvent)
            .where(SubscriptionEvent.revenuecat_event_id == event_id,
                   SubscriptionEvent.processing_status == Status.UNMATCHED)
            .values(processing_status=Status.FAILED)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        try:
            await handle_event(db, dict(payload), client)
            counts["retried"] += 1
        except AppError:
            counts["failed"] += 1
    return counts
