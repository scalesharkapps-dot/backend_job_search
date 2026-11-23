"""Permanent, self-service account deletion.

Order of operations (designed for safe retries and no orphaned personal data):

1. **Re-authenticate** with the current password (skipped only when an earlier
   attempt by the same account already passed it and stopped half-way).
2. **Lock** the local user with an atomic conditional UPDATE (``is_active=false``,
   ``suspended_reason=account_deletion_pending``). Concurrent duplicate requests get
   ``409``; the account cannot be used meanwhile.
3. **Storage**: delete every private file the database references (CVs, profile
   photo, company logo). Failure -> lock released, nothing else changed, retryable.
4. **Supabase Auth**: delete the user with the service-role key (removes sessions and
   refresh tokens). "Already deleted" counts as success. Failure -> lock released.
5. **Database**, one transaction: archive the employer's posted jobs (other users'
   applications stay intact, as in ``delete_job``), delete the e-mail verification
   tracker, write a ``deleted_accounts`` tombstone, delete the ``users`` row (every
   user-owned table cascades; references in shared records are set to NULL).
   If this fails after step 4, the account stays locked and a retry with the same
   (still valid) access token completes it without a password.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError, AuthenticationError, PermissionDeniedError, ValidationAppError
from app.models.account_deletion import DeletedAccount
from app.models.application import Application
from app.models.base import utcnow
from app.models.email_verification import EmailVerificationTracker
from app.models.employer import EmployerProfile
from app.models.job import Job, PostingStatus
from app.models.profile import UserCV, UserProfile
from app.models.subscription import Subscription
from app.models.user import User, UserRole
from app.services.storage.base import StorageBackend, StorageError
from app.services.supabase_auth import SupabaseAuthClient

logger = logging.getLogger(__name__)

CONFIRMATION_PHRASE = "DELETE"
PENDING_MARKER = "account_deletion_pending"
STALE_LOCK = timedelta(minutes=5)


@dataclass(frozen=True)
class DeletionResult:
    user_id: uuid.UUID
    storage_objects_deleted: int
    active_store_subscription: bool


async def _reauthenticate(auth: SupabaseAuthClient, user: User, password: str) -> None:
    try:
        payload = await auth.sign_in_with_password(user.email, password)
    except AuthenticationError as exc:
        if exc.code in {"invalid_credentials", "email_not_confirmed"}:
            raise PermissionDeniedError("The password is incorrect.", code="invalid_password") from None
        raise
    session_user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    if str(session_user.get("id")) != str(user.id):
        # The e-mail now belongs to a different Auth user: never act on it.
        raise PermissionDeniedError("Re-authentication failed.", code="invalid_password")
    token = payload.get("access_token")
    if token:
        try:  # the extra session was only needed to prove the password
            await auth.sign_out(token)
        except AppError:
            logger.info("Could not revoke the re-authentication session")


async def _lock(db: AsyncSession, user_id: uuid.UUID) -> bool:
    now = utcnow()
    result = await db.execute(
        update(User)
        .where(
            User.id == user_id,
            or_(
                User.suspended_reason.is_(None),
                User.suspended_reason != PENDING_MARKER,
                User.updated_at < now - STALE_LOCK,
            ),
        )
        .values(is_active=False, suspended_reason=PENDING_MARKER, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount == 1


async def _unlock(db: AsyncSession, user_id: uuid.UUID, was_active: bool, reason: str | None) -> None:
    await db.rollback()
    await db.execute(
        update(User)
        .where(User.id == user_id, User.suspended_reason == PENDING_MARKER)
        .values(is_active=was_active, suspended_reason=reason, updated_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def _stored_files(db: AsyncSession, user_id: uuid.UUID) -> list[tuple[str, str]]:
    settings = get_settings()
    files: list[tuple[str, str]] = []
    cvs = await db.execute(select(UserCV.storage_path).where(UserCV.user_id == user_id))
    files += [(settings.SUPABASE_STORAGE_CV_BUCKET, p) for p in cvs.scalars() if p]
    photo = await db.scalar(select(UserProfile.profile_photo_path).where(UserProfile.user_id == user_id))
    if photo:
        files.append((settings.SUPABASE_STORAGE_MEDIA_BUCKET, photo))
    logo = await db.scalar(select(EmployerProfile.logo_path).where(EmployerProfile.owner_user_id == user_id))
    if logo:
        files.append((settings.SUPABASE_STORAGE_MEDIA_BUCKET, logo))
    return files


async def _delete_files(storage: StorageBackend, files: list[tuple[str, str]]) -> int:
    deleted = 0
    for bucket, path in files:
        try:
            await storage.delete(bucket, path)  # idempotent: a missing object is not an error
        except StorageError as exc:
            if exc.code == "invalid_storage_path":
                logger.warning("Skipped a malformed storage path during account deletion")
                continue
            raise
        deleted += 1
    return deleted


async def _has_renewing_subscription(db: AsyncSession, user_id: uuid.UUID) -> bool:
    count = await db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.user_id == user_id, Subscription.entitlement_active.is_(True),
               Subscription.will_renew.is_(True))
    )
    return bool(count)


async def _delete_database_records(db: AsyncSession, user: User) -> None:
    employer_id = await db.scalar(select(EmployerProfile.id).where(EmployerProfile.owner_user_id == user.id))
    if employer_id is not None:
        has_apps = select(Application.id).where(Application.job_id == Job.id).exists()
        # Unapplied drafts disappear; every other posting is archived so applicants keep their records.
        await db.execute(
            delete(Job)
            .where(Job.employer_id == employer_id, Job.posting_status == PostingStatus.DRAFT, ~has_apps)
            .execution_options(synchronize_session=False)
        )
        await db.execute(
            update(Job)
            .where(and_(Job.employer_id == employer_id, Job.posting_status != PostingStatus.ARCHIVED))
            .values(posting_status=PostingStatus.ARCHIVED)
            .execution_options(synchronize_session=False)
        )
    await db.execute(delete(EmailVerificationTracker).where(EmailVerificationTracker.email == user.email.lower()))
    if await db.get(DeletedAccount, user.id) is None:
        db.add(DeletedAccount(user_id=user.id, deleted_at=utcnow()))
    # Cascades: profile, CVs + extracted data, saved jobs, applications (+ history, interviews),
    # recommendations + preferences, employer profile (+ verification requests), trial,
    # subscriptions, payment history. Shared records (reports, audit log, jobs) keep a NULL reference.
    await db.execute(delete(User).where(User.id == user.id).execution_options(synchronize_session=False))
    await db.commit()


async def delete_account(
    db: AsyncSession,
    user: User,
    password: str,
    confirmation: str,
    auth: SupabaseAuthClient,
    storage: StorageBackend,
) -> DeletionResult:
    if confirmation != CONFIRMATION_PHRASE:
        raise ValidationAppError(
            f'Type "{CONFIRMATION_PHRASE}" to confirm account deletion.', code="confirmation_required"
        )
    if user.role == UserRole.ADMIN:
        raise PermissionDeniedError(
            "Administrator accounts cannot be deleted from the app.", code="admin_account_deletion_not_allowed"
        )
    user_id, was_active, previous_reason = user.id, user.is_active, user.suspended_reason
    resuming = previous_reason == PENDING_MARKER  # an earlier attempt already re-authenticated
    if resuming:
        was_active, previous_reason = True, None
    else:
        await _reauthenticate(auth, user, password)

    if not await _lock(db, user_id):
        raise AppError(
            "Account deletion is already in progress.", code="account_deletion_in_progress", status_code=409
        )
    logger.info("Account deletion started")

    try:
        renewing = await _has_renewing_subscription(db, user_id)
        files = await _stored_files(db, user_id)
        deleted_files = await _delete_files(storage, files)
        await auth.admin_delete_user(str(user_id))
    except AppError:
        await _unlock(db, user_id, was_active, previous_reason)
        logger.warning("Account deletion aborted before any irreversible step completed")
        raise
    except Exception:
        await _unlock(db, user_id, was_active, previous_reason)
        raise

    try:
        await _delete_database_records(db, user)
    except Exception:
        await db.rollback()
        logger.exception("Account deletion: Auth user removed but database cleanup failed")
        try:  # keep the marker (no password needed to resume) but allow an immediate retry
            await db.execute(
                update(User)
                .where(User.id == user_id, User.suspended_reason == PENDING_MARKER)
                .values(updated_at=utcnow() - STALE_LOCK - timedelta(seconds=1))
                .execution_options(synchronize_session=False)
            )
            await db.commit()
        except Exception:
            await db.rollback()
        raise AppError(
            "Your sign-in has been removed, but some data could not be deleted yet. "
            "Please try again; the deletion will be completed.",
            code="account_deletion_incomplete",
            status_code=500,
        ) from None
    logger.info("Account deletion completed")
    return DeletionResult(user_id, deleted_files, renewing)
