"""Synchronisation of Supabase Auth identities into the application ``users`` table."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthenticationError, ConflictError
from app.models.account_deletion import DeletedAccount
from app.models.base import utcnow
from app.models.user import User, UserRole

logger = logging.getLogger(__name__)


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    return await db.get(User, user_id)


async def sync_user(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    email: str | None,
    full_name: str | None = None,
    email_verified: bool | None = None,
    mark_login: bool = False,
) -> User:
    """Create the local user on first sight, or refresh mirrored identity fields.

    The role is never taken from the token or request: new users are always
    ``job_seeker``; privileged roles are assigned only by server-side workflows.
    """
    user = await db.get(User, user_id)
    changed = False
    if user is None:
        if await db.get(DeletedAccount, user_id) is not None:
            # A token issued before the account was deleted: never resurrect the account.
            raise AuthenticationError("This account has been deleted.", code="account_deleted")
        if not email:
            # Supabase user tokens always carry email for email/password accounts.
            email = f"{user_id}@users.noreply.sharkhire"
        user = User(
            id=user_id,
            email=email.lower(),
            full_name=full_name,
            role=UserRole.JOB_SEEKER,
            is_active=True,
            email_verified=bool(email_verified),
            last_login_at=utcnow() if mark_login else None,
        )
        db.add(user)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await db.get(User, user_id)
            if existing is not None:  # concurrent first request won the race
                return existing
            logger.warning("User sync conflict: email already linked to another account")
            raise ConflictError(
                "This email is linked to another account.", code="account_conflict"
            ) from None
        await db.refresh(user)
        logger.info("Created local user record for new Supabase identity")
        return user

    if email and user.email != email.lower():
        user.email = email.lower()
        changed = True
    if full_name and not user.full_name:
        user.full_name = full_name
        changed = True
    if email_verified is not None and user.email_verified != email_verified:
        user.email_verified = email_verified
        changed = True
    if mark_login:
        user.last_login_at = utcnow()
        changed = True
    if changed:
        await db.commit()
    return user


def user_from_supabase_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract identity fields from a Supabase user object."""
    meta = payload.get("user_metadata") or {}
    return {
        "user_id": uuid.UUID(str(payload["id"])),
        "email": payload.get("email"),
        "full_name": meta.get("full_name") or meta.get("name"),
        "email_verified": bool(payload.get("email_confirmed_at") or payload.get("confirmed_at")),
    }
