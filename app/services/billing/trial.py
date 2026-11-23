"""The 3-day app-managed free trial (not a store introductory offer).

* Exactly one trial per user id, enforced by a UNIQUE constraint and an atomic
  ``INSERT ... ON CONFLICT DO NOTHING`` -- concurrent requests cannot create two,
  and nothing (login, logout, reinstall, new device, data refresh) can restart it.
* Start and end are server-side UTC; nothing comes from the client.
* Created at registration when Supabase returns the new user id, otherwise on
  the user's first authenticated request (this also covers accounts that existed
  before billing was introduced, so they are not locked out without warning).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import base as model_base
from app.models.subscription import TrialStatus, UserTrial

logger = logging.getLogger(__name__)

TRIAL_DURATION = timedelta(days=3)


def utcnow() -> datetime:
    # Indirection so tests can move the clock.
    return model_base.utcnow()


@dataclass(frozen=True)
class TrialState:
    status: TrialStatus
    started_at: datetime
    ends_at: datetime
    remaining_seconds: int

    @property
    def active(self) -> bool:
        return self.remaining_seconds > 0 and self.status != TrialStatus.EXPIRED


def _insert(db: AsyncSession):  # type: ignore[no-untyped-def]
    return pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert


async def get_trial(db: AsyncSession, user_id: uuid.UUID) -> UserTrial | None:
    return (
        await db.execute(
            select(UserTrial).where(UserTrial.user_id == user_id).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def ensure_trial(db: AsyncSession, user_id: uuid.UUID) -> UserTrial:
    """Return the user's trial, creating it (now -> now + 3 days) if none exists yet."""
    existing = await get_trial(db, user_id)
    if existing is not None:
        return existing
    now = utcnow()
    stmt = (
        _insert(db)(UserTrial)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            trial_started_at=now,
            trial_ends_at=now + TRIAL_DURATION,
            trial_status=TrialStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(index_elements=["user_id"])
    )
    result = await db.execute(stmt)
    await db.commit()
    if result.rowcount:
        logger.info("Started 3-day app trial for a user")
    trial = await get_trial(db, user_id)
    assert trial is not None
    return trial


def trial_state(trial: UserTrial, now: datetime | None = None) -> TrialState:
    now = now or utcnow()
    remaining = max(0, int((trial.trial_ends_at - now).total_seconds()))
    status = trial.trial_status
    if status == TrialStatus.ACTIVE and remaining == 0:
        status = TrialStatus.EXPIRED
    return TrialState(status, trial.trial_started_at, trial.trial_ends_at, remaining)


async def refresh_stored_status(db: AsyncSession, trial: UserTrial, state: TrialState) -> None:
    """Persist the time-derived status (bookkeeping only; access never depends on it)."""
    if trial.trial_status == TrialStatus.ACTIVE and state.status == TrialStatus.EXPIRED:
        await db.execute(
            update(UserTrial)
            .where(UserTrial.id == trial.id, UserTrial.trial_status == TrialStatus.ACTIVE)
            .values(trial_status=TrialStatus.EXPIRED, updated_at=utcnow())
            .execution_options(synchronize_session=False)
        )
        await db.commit()


async def mark_converted(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Record that the user subscribed. The trial window itself is never extended."""
    await db.execute(
        update(UserTrial)
        .where(UserTrial.user_id == user_id, UserTrial.trial_status != TrialStatus.CONVERTED)
        .values(trial_status=TrialStatus.CONVERTED, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
