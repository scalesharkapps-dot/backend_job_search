"""Free plan limit on opening external job application links.

Rules (server-side only; nothing from the client decides the plan):

* **Pro** = a RevenueCat-verified active ``app_access`` entitlement (``evaluate_access``),
  plus exempt staff roles. Unlimited; actions are recorded with ``counted = false``.
* **Free** = everyone else allowed into the app (today: users in the 3-day trial).
  At most ``APPLICATION_LINK_DAILY_LIMIT`` (5) counted actions in any rolling
  ``APPLICATION_LINK_WINDOW_HOURS`` (24 h) window. Access returns automatically as the
  oldest action in the window ages out.
* Opening the same job again inside the window is free (no second count).
* Retries carrying the same ``Idempotency-Key`` are recorded once.
* One allowance across providers: JSearch and Techmap jobs count toward the same 5
  (the count never filters by provider); each action stores the job's provider.

Concurrency: the user's ``users`` row is write-locked (``UPDATE ... SET updated_at =
updated_at``) before counting, so concurrent requests from any device or worker are
serialised and can never exceed the limit (row lock on PostgreSQL, write lock on SQLite).
"""

from __future__ import annotations

import math
import uuid
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, ConflictError
from app.models import base as model_base
from app.models.application import ApplicationLinkAction as Action
from app.models.job import JobSource
from app.models.user import User

LIMIT_MESSAGE = (
    "You've reached your daily limit of {limit} job applications. "
    "Try again after 24 hours or upgrade to Pro for unlimited access."
)

# Whether external apply links may be included in job payloads for the current request.
# Set by ``require_app_access``; True outside the paywalled routes (e.g. admin).
_links_visible: ContextVar[bool] = ContextVar("apply_links_visible", default=True)


def set_apply_links_visible(visible: bool) -> None:
    _links_visible.set(visible)


def apply_links_visible() -> bool:
    return _links_visible.get()


def utcnow() -> datetime:
    return model_base.utcnow()


@dataclass(frozen=True)
class Usage:
    plan: str  # "pro" | "free"
    limit: int | None
    used: int
    remaining: int | None
    reset_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan,
            "unlimited": self.plan == "pro",
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
        }


class DailyApplicationLimitReached(AppError):
    status_code = 429
    code = "DAILY_APPLICATION_LIMIT_REACHED"

    def __init__(self, usage: Usage, now: datetime) -> None:
        payload = {
            "code": self.code,
            "limit": usage.limit,
            "used": usage.used,
            "remaining": 0,
            "reset_at": usage.reset_at.isoformat() if usage.reset_at else None,
            "upgrade_available": True,
        }
        retry = max(1, math.ceil((usage.reset_at - now).total_seconds())) if usage.reset_at else None
        super().__init__(
            LIMIT_MESSAGE.format(limit=usage.limit),
            details=payload,
            data=payload,
            headers={"Retry-After": str(retry)} if retry else None,
        )


def _window() -> timedelta:
    return timedelta(hours=get_settings().APPLICATION_LINK_WINDOW_HOURS)


async def get_usage(db: AsyncSession, user_id: uuid.UUID, *, unlimited: bool) -> Usage:
    """Counted actions in the current rolling window (own records only)."""
    limit = get_settings().APPLICATION_LINK_DAILY_LIMIT
    since = utcnow() - _window()
    used, oldest = (
        await db.execute(
            select(func.count(Action.id), func.min(Action.created_at)).where(
                Action.user_id == user_id, Action.counted.is_(True), Action.created_at > since
            )
        )
    ).one()
    used = int(used or 0)
    if oldest is not None and oldest.tzinfo is None:  # SQLite returns naive aggregates
        oldest = oldest.replace(tzinfo=UTC)
    reset_at = oldest + _window() if oldest else None
    if unlimited:
        return Usage("pro", None, used, None, None)
    return Usage("free", limit, used, max(0, limit - used), reset_at)


async def has_unlocked(db: AsyncSession, user_id: uuid.UUID, job_id: uuid.UUID) -> bool:
    """The user has already opened this job's application link through SharkHire."""
    found = await db.scalar(
        select(Action.id).where(Action.user_id == user_id, Action.job_id == job_id).limit(1)
    )
    return found is not None


async def open_application_link(
    db: AsyncSession,
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    *,
    provider: JobSource,
    unlimited: bool,
    idempotency_key: str | None = None,
) -> tuple[Usage, bool]:
    """Record one application-link action (or reuse a previous one).

    Returns ``(usage_after, reused)``; raises ``DailyApplicationLimitReached`` (nothing
    recorded) when a Free user has used the whole allowance.
    """
    await db.commit()  # end any read transaction: the lock below must start a fresh one
    await db.execute(
        update(User).where(User.id == user_id).values(updated_at=User.updated_at)
        .execution_options(synchronize_session=False)
    )
    try:
        now = utcnow()
        if idempotency_key:
            previous = (
                await db.execute(
                    select(Action).where(Action.user_id == user_id, Action.idempotency_key == idempotency_key)
                )
            ).scalar_one_or_none()
            if previous is not None:
                if previous.job_id != job_id:
                    raise ConflictError(
                        "This Idempotency-Key was already used for another job.", code="idempotency_key_reused"
                    )
                await db.rollback()
                return await get_usage(db, user_id, unlimited=unlimited), True

        same_job = await db.scalar(
            select(Action.id)
            .where(Action.user_id == user_id, Action.job_id == job_id, Action.created_at > now - _window())
            .limit(1)
        )
        if same_job is not None:
            await db.rollback()
            return await get_usage(db, user_id, unlimited=unlimited), True

        if not unlimited:
            usage = await get_usage(db, user_id, unlimited=False)
            if usage.remaining == 0:
                await db.rollback()
                raise DailyApplicationLimitReached(usage, now)

        db.add(Action(user_id=user_id, job_id=job_id, provider=provider, idempotency_key=idempotency_key,
                      counted=not unlimited, created_at=now))
        await db.commit()
    except IntegrityError:  # same Idempotency-Key recorded concurrently: treat as the retry it is
        await db.rollback()
        return await get_usage(db, user_id, unlimited=unlimited), True
    except Exception:
        await db.rollback()
        raise
    return await get_usage(db, user_id, unlimited=unlimited), False
