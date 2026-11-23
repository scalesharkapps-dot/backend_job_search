"""Signup verification e-mail tracking and resend-limit enforcement.

Policy (per e-mail address, per verification cycle):

* 1 initial verification e-mail (sent by Supabase on signup),
* at most ``EMAIL_VERIFICATION_MAX_RESENDS`` (default 2) successful resends,
* at least ``EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS`` between two e-mails.

Enforcement is persistent (PostgreSQL table ``email_verification_trackers``) and
safe under concurrency: a resend slot is *reserved* with one atomic conditional
``UPDATE ... WHERE resend_count < max AND <cooldown elapsed>``. Only one request
can win a given slot, on any number of workers. The Supabase call happens after
the reservation; if Supabase rejects the request (or is unreachable) the slot is
released again, so only resends Supabase accepted are counted.

Nothing a client can do resets the counter: login/logout, app restarts, new
devices and repeated registrations all reuse the same row (a repeated signup for
an unverified address is itself counted as a resend, because Supabase sends a
new e-mail for it). A new cycle starts only after ``EMAIL_VERIFICATION_CYCLE_HOURS``.
"""

from __future__ import annotations

import logging
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

from sqlalchemy import case, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, ConflictError
from app.models import base as model_base
from app.models.email_verification import EmailVerificationTracker as Tracker
from app.models.user import User

logger = logging.getLogger(__name__)

T = TypeVar("T")

MAX_RESEND_MESSAGE = "You've reached the maximum resend limit. Please try again later."
ALREADY_VERIFIED_MESSAGE = "This email address is already verified. Please sign in."


def utcnow() -> datetime:
    # Indirection so tests can move the clock (cooldowns, cycle windows).
    return model_base.utcnow()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _seconds_until(moment: datetime, now: datetime) -> int:
    return max(1, math.ceil((moment - now).total_seconds()))


class ResendLimitReachedError(AppError):
    status_code = 429
    code = "resend_limit_reached"
    message = MAX_RESEND_MESSAGE

    def __init__(self, retry_after: int | None = None) -> None:
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        super().__init__(
            MAX_RESEND_MESSAGE,
            headers=headers,
            details={"remaining_attempts": 0, "retry_after": retry_after},
            data={"remaining_attempts": 0, "retry_after": retry_after},
        )


class ResendCooldownError(AppError):
    status_code = 429
    code = "resend_cooldown"

    def __init__(self, retry_after: int, remaining_attempts: int) -> None:
        super().__init__(
            f"Please wait {retry_after} seconds before requesting another verification email.",
            headers={"Retry-After": str(retry_after)},
            details={"remaining_attempts": remaining_attempts, "retry_after": retry_after},
            data={"remaining_attempts": remaining_attempts, "retry_after": retry_after},
        )


class EmailAlreadyVerifiedError(ConflictError):
    code = "email_already_verified"
    message = ALREADY_VERIFIED_MESSAGE


@dataclass(frozen=True)
class Reservation:
    tracker_id: uuid.UUID
    cycle: int
    stamp: datetime
    previous_last_sent_at: datetime | None
    previous_last_resend_at: datetime | None
    resend_count: int  # value after the reservation


@dataclass(frozen=True)
class ResendResult:
    remaining_attempts: int
    resend_count: int
    max_resends: int
    cooldown_seconds: int


# --------------------------------------------------------------------------- helpers
def _limits() -> tuple[int, int, int]:
    s = get_settings()
    return (
        s.EMAIL_VERIFICATION_MAX_RESENDS,
        s.EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS,
        s.EMAIL_VERIFICATION_CYCLE_HOURS,
    )


async def _get_tracker(db: AsyncSession, email: str) -> Tracker | None:
    return (
        await db.execute(
            select(Tracker).where(Tracker.email == email).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _get_or_create_tracker(
    db: AsyncSession, email: str, user_id: uuid.UUID | None = None
) -> Tracker:
    tracker = await _get_tracker(db, email)
    if tracker is not None:
        return tracker
    db.add(Tracker(email=email, user_id=user_id, cycle=1, cycle_started_at=utcnow(), resend_count=0))
    try:
        await db.commit()
    except IntegrityError:  # a concurrent request created it first
        await db.rollback()
    tracker = await _get_tracker(db, email)
    assert tracker is not None
    return tracker


async def _local_user_verified(db: AsyncSession, email: str) -> bool:
    verified = await db.scalar(
        select(User.email_verified).where(User.email == email).limit(1)
    )
    return bool(verified)


async def _mark_verified(db: AsyncSession, email: str) -> None:
    now = utcnow()
    await db.execute(
        update(Tracker)
        .where(Tracker.email == email, Tracker.verified_at.is_(None))
        .values(verified_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def _roll_cycle_if_due(db: AsyncSession, tracker: Tracker) -> Tracker:
    """Start a new cycle once the window has elapsed (atomic, at most once per window)."""
    _, _, cycle_hours = _limits()
    if cycle_hours <= 0 or tracker.verified_at is not None:
        return tracker
    now = utcnow()
    if tracker.cycle_started_at > now - timedelta(hours=cycle_hours):
        return tracker
    result = await db.execute(
        update(Tracker)
        .where(Tracker.id == tracker.id, Tracker.cycle == tracker.cycle)
        .values(
            cycle=Tracker.cycle + 1,
            cycle_started_at=now,
            initial_sent_at=None,
            resend_count=0,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount:
        logger.info("Email verification tracker started cycle %s", tracker.cycle + 1)
    refreshed = await _get_tracker(db, tracker.email)
    assert refreshed is not None
    return refreshed


def _blocked_error(tracker: Tracker, now: datetime) -> AppError | None:
    max_resends, cooldown, cycle_hours = _limits()
    if tracker.resend_count >= max_resends:
        retry = (
            _seconds_until(tracker.cycle_started_at + timedelta(hours=cycle_hours), now)
            if cycle_hours > 0
            else None
        )
        return ResendLimitReachedError(retry_after=retry)
    if cooldown > 0 and tracker.last_sent_at is not None:
        available_at = tracker.last_sent_at + timedelta(seconds=cooldown)
        if available_at > now:
            return ResendCooldownError(
                _seconds_until(available_at, now), max_resends - tracker.resend_count
            )
    return None


async def _reserve(db: AsyncSession, tracker: Tracker) -> Reservation:
    """Atomically claim one resend slot or raise the reason it is unavailable."""
    max_resends, cooldown, _ = _limits()
    for _attempt in range(3):  # retried only if a concurrent cycle roll-over changed the row
        tracker = await _roll_cycle_if_due(db, tracker)
        if tracker.verified_at is not None:
            raise EmailAlreadyVerifiedError()
        now = utcnow()
        blocked = _blocked_error(tracker, now)
        if blocked is not None:
            raise blocked
        conditions: list[Any] = [
            Tracker.id == tracker.id,
            Tracker.cycle == tracker.cycle,
            Tracker.verified_at.is_(None),
            Tracker.resend_count < max_resends,
        ]
        if cooldown > 0:
            conditions.append(
                or_(
                    Tracker.last_sent_at.is_(None),
                    Tracker.last_sent_at <= now - timedelta(seconds=cooldown),
                )
            )
        # Snapshot first: the values the slot must be restored to if Supabase rejects it.
        candidate = Reservation(
            tracker_id=tracker.id,
            cycle=tracker.cycle,
            stamp=now,
            previous_last_sent_at=tracker.last_sent_at,
            previous_last_resend_at=tracker.last_resend_at,
            resend_count=tracker.resend_count + 1,
        )
        result = await db.execute(
            update(Tracker)
            .where(*conditions)
            .values(
                resend_count=Tracker.resend_count + 1,
                last_sent_at=now,
                last_resend_at=now,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if result.rowcount == 1:
            return candidate
        # Lost a race: re-read and report the real reason (limit / cooldown / verified).
        refreshed = await _get_tracker(db, tracker.email)
        assert refreshed is not None
        tracker = refreshed
    raise ResendLimitReachedError()


async def _release(db: AsyncSession, reservation: Reservation) -> None:
    """Give back a slot whose e-mail Supabase did not accept."""
    try:
        await db.rollback()
        await db.execute(
            update(Tracker)
            .where(
                Tracker.id == reservation.tracker_id,
                Tracker.cycle == reservation.cycle,
                Tracker.resend_count > 0,
            )
            .values(
                resend_count=Tracker.resend_count - 1,
                last_sent_at=case(
                    (Tracker.last_sent_at == reservation.stamp, reservation.previous_last_sent_at),
                    else_=Tracker.last_sent_at,
                ),
                last_resend_at=case(
                    (
                        Tracker.last_resend_at == reservation.stamp,
                        reservation.previous_last_resend_at,
                    ),
                    else_=Tracker.last_resend_at,
                ),
                updated_at=utcnow(),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
    except Exception:  # fail closed: the attempt stays consumed, never over-granted
        logger.exception("Could not release a verification resend reservation")


async def _send_reserved(
    db: AsyncSession, tracker: Tracker, send: Callable[[], Awaitable[T]]
) -> tuple[Reservation, T]:
    reservation = await _reserve(db, tracker)
    try:
        result = await send()
    except AppError as exc:
        await _release(db, reservation)
        if exc.code == EmailAlreadyVerifiedError.code:  # reported by Supabase
            await _mark_verified(db, tracker.email)
        raise
    except BaseException:
        await _release(db, reservation)
        raise
    return reservation, result


# --------------------------------------------------------------------------- public API
async def resend_verification(
    db: AsyncSession, email: str, send: Callable[[], Awaitable[Any]]
) -> ResendResult:
    """Enforce the policy and, if allowed, call ``send`` (the Supabase resend)."""
    email = normalize_email(email)
    max_resends, cooldown, _ = _limits()
    if await _local_user_verified(db, email):
        await _mark_verified(db, email)
        raise EmailAlreadyVerifiedError()
    tracker = await _get_or_create_tracker(db, email)
    reservation, _ = await _send_reserved(db, tracker, send)
    logger.info(
        "Verification email resent (cycle %s, resend %s/%s)",
        reservation.cycle, reservation.resend_count, max_resends,
    )
    return ResendResult(
        remaining_attempts=max(0, max_resends - reservation.resend_count),
        resend_count=reservation.resend_count,
        max_resends=max_resends,
        cooldown_seconds=cooldown,
    )


async def signup_with_tracking(
    db: AsyncSession, email: str, signup: Callable[[], Awaitable[dict[str, Any]]]
) -> dict[str, Any] | None:
    """Run the Supabase signup while keeping the verification limit intact.

    Returns the Supabase payload, or ``None`` when the call was suppressed because
    a repeated registration would exceed the resend policy (the caller then
    returns the normal "check your email" response, so nothing is revealed).
    """
    email = normalize_email(email)
    tracker = await _get_tracker(db, email)
    pending = tracker is not None and tracker.verified_at is None

    if pending and not await _local_user_verified(db, email):
        assert tracker is not None
        tracker = await _roll_cycle_if_due(db, tracker)
        fresh_cycle = (
            tracker.initial_sent_at is None and tracker.resend_count == 0
            and (tracker.last_sent_at is None or tracker.cycle_started_at >= tracker.last_sent_at)
        )
        if not fresh_cycle:
            # Registration retry: Supabase would send another e-mail, so it is a resend.
            try:
                reservation, payload = await _send_reserved(db, tracker, signup)
            except (ResendLimitReachedError, ResendCooldownError, EmailAlreadyVerifiedError):
                logger.info("Repeated signup suppressed by the verification resend policy")
                return None
            logger.info(
                "Repeated signup counted as verification resend (cycle %s, resend %s)",
                reservation.cycle, reservation.resend_count,
            )
            await _after_signup(db, email, payload, initial=False)
            return payload

    payload = await signup()
    await _after_signup(db, email, payload, initial=True)
    return payload


async def _after_signup(
    db: AsyncSession, email: str, payload: dict[str, Any], *, initial: bool
) -> None:
    if payload.get("access_token"):  # auto-confirmed project: no verification e-mail
        await _mark_verified(db, email)
        return
    if await _local_user_verified(db, email):
        return  # existing verified account: Supabase sent nothing
    user = payload.get("user") if isinstance(payload.get("user"), dict) else payload
    user_id: uuid.UUID | None = None
    # Supabase returns an obfuscated user with no identities for existing accounts.
    if user.get("id") and user.get("identities"):
        try:
            user_id = uuid.UUID(str(user["id"]))
        except ValueError:
            user_id = None
    tracker = await _get_or_create_tracker(db, email, user_id)
    now = utcnow()
    values: dict[str, Any] = {"updated_at": now}
    if user_id is not None:
        values["user_id"] = user_id
    if initial:
        values.update(initial_sent_at=now, last_sent_at=now)
    await db.execute(
        update(Tracker)
        .where(Tracker.id == tracker.id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
