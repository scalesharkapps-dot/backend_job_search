"""Administrative operations. Every mutating action writes an audit-log entry in
the same transaction as the change."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AppError, BadRequestError, ConflictError, NotFoundError, ValidationAppError
from app.models.application import Application
from app.models.base import utcnow
from app.models.employer import (
    AdminAuditLog,
    EmployerAccountStatus,
    EmployerProfile,
    EmployerVerificationRequest,
    Report,
    ReportStatus,
    ReportTargetType,
    VerificationRequestStatus,
    VerificationStatus,
)
from app.models.job import EXTERNAL_SOURCES, Job, JobSource, PostingStatus
from app.models.user import User, UserRole
from app.services import audit
from app.services.jsearch.client import plan_quota
from app.services.supabase_auth import SupabaseAuthClient

logger = logging.getLogger(__name__)


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _paged(db: AsyncSession, stmt: Select[Any], order: Any, page: int, page_size: int) -> tuple[list[Any], int]:
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await db.scalars(stmt.order_by(*order).offset((page - 1) * page_size).limit(page_size))
    return list(rows), int(total)


# ---------------- Users ----------------
async def list_users(db: AsyncSession, *, q: str | None, role: UserRole | None, is_active: bool | None,
                     page: int, page_size: int) -> tuple[list[User], int]:
    stmt = select(User)
    if q:
        stmt = stmt.where(or_(User.email.ilike(_like(q), escape="\\"), User.full_name.ilike(_like(q), escape="\\")))
    if role:
        stmt = stmt.where(User.role == role)
    if is_active is not None:
        stmt = stmt.where(User.is_active.is_(is_active))
    return await _paged(db, stmt, [User.created_at.desc(), User.id], page, page_size)


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise NotFoundError("User not found.", code="user_not_found")
    return user


async def user_detail(db: AsyncSession, user: User) -> dict[str, Any]:
    employer = await db.scalar(select(EmployerProfile).where(EmployerProfile.owner_user_id == user.id))
    applications = await db.scalar(select(func.count()).select_from(Application).where(Application.user_id == user.id)) or 0
    return {
        "employer_profile_id": employer.id if employer else None,
        "employer_company": employer.company_name if employer else None,
        "applications": int(applications),
    }


async def _active_admins(db: AsyncSession) -> int:
    return int(await db.scalar(
        select(func.count()).select_from(User).where(User.role == UserRole.ADMIN, User.is_active.is_(True))
    ) or 0)


async def set_user_status(db: AsyncSession, actor: User, user_id: uuid.UUID, is_active: bool, reason: str,
                          auth: SupabaseAuthClient | None = None) -> tuple[User, bool]:
    """Suspend/reactivate. Suspension blocks every API call immediately (checked on
    each request). When a service-role key is configured the Supabase account is
    also banned/unbanned; the second return value reports whether that happened."""
    user = await get_user(db, user_id)
    if user.id == actor.id:
        raise BadRequestError("You cannot change your own account status.", code="self_action_forbidden")
    if user.is_active == is_active:
        raise ConflictError("The account already has this status.", code="status_unchanged")
    if not is_active and user.role == UserRole.ADMIN and await _active_admins(db) <= 1:
        raise ConflictError("The last active administrator cannot be suspended.", code="last_admin")
    previous = {"is_active": user.is_active}
    user.is_active = is_active
    user.suspended_reason = None if is_active else reason
    audit.record(db, actor_id=actor.id, action="user.reactivate" if is_active else "user.suspend", target_type="user",
                 target_id=user.id, previous=previous, new={"is_active": is_active}, reason=reason)
    await db.commit()
    provider_updated = False
    if auth is not None:
        try:
            provider_updated = await auth.admin_update_user(
                str(user.id), {"ban_duration": "none" if is_active else "876000h"}
            )
        except AppError as exc:
            logger.warning("Supabase ban/unban failed (%s); local suspension still applies", exc.code)
    return user, provider_updated


async def set_user_role(db: AsyncSession, actor: User, user_id: uuid.UUID, role: UserRole, reason: str) -> User:
    user = await get_user(db, user_id)
    if user.id == actor.id:
        raise BadRequestError("You cannot change your own role.", code="self_action_forbidden")
    if user.role == role:
        raise ConflictError("The user already has this role.", code="role_unchanged")
    if user.role == UserRole.ADMIN and user.is_active and await _active_admins(db) <= 1:
        raise ConflictError("The last active administrator cannot be demoted.", code="last_admin")
    previous = user.role
    user.role = role
    audit.record(db, actor_id=actor.id, action="user.role_change", target_type="user", target_id=user.id,
                 previous={"role": previous.value}, new={"role": role.value}, reason=reason)
    await db.commit()
    return user


# ---------------- Employers ----------------
async def list_employers(db: AsyncSession, *, verification: VerificationStatus | None, account: EmployerAccountStatus | None,
                         q: str | None, page: int, page_size: int) -> tuple[list[EmployerProfile], int]:
    stmt = select(EmployerProfile)
    if verification:
        stmt = stmt.where(EmployerProfile.verification_status == verification)
    if account:
        stmt = stmt.where(EmployerProfile.account_status == account)
    if q:
        stmt = stmt.where(EmployerProfile.company_name.ilike(_like(q), escape="\\"))
    return await _paged(db, stmt, [EmployerProfile.created_at.desc(), EmployerProfile.id], page, page_size)


async def get_employer(db: AsyncSession, employer_id: uuid.UUID) -> EmployerProfile:
    employer = await db.get(EmployerProfile, employer_id)
    if employer is None:
        raise NotFoundError("Employer not found.", code="employer_not_found")
    return employer


async def verification_requests(db: AsyncSession, employer: EmployerProfile) -> list[EmployerVerificationRequest]:
    return list(await db.scalars(
        select(EmployerVerificationRequest).where(EmployerVerificationRequest.employer_id == employer.id)
        .order_by(EmployerVerificationRequest.submitted_at.desc())
    ))


async def decide_verification(db: AsyncSession, actor: User, employer_id: uuid.UUID, decision: str, reason: str | None) -> EmployerProfile:
    employer = await get_employer(db, employer_id)
    requests = await verification_requests(db, employer)
    latest = requests[0] if requests else None
    open_states = {VerificationRequestStatus.PENDING, VerificationRequestStatus.UNDER_REVIEW}
    if latest is None or latest.status not in open_states:
        raise ConflictError("There is no open verification request for this employer.", code="no_open_request")
    previous = employer.verification_status
    now = utcnow()
    if decision == "under_review":
        latest.status = VerificationRequestStatus.UNDER_REVIEW
        employer.verification_status = VerificationStatus.UNDER_REVIEW
    elif decision == "approve":
        latest.status, latest.reviewed_at, latest.reviewer_id = VerificationRequestStatus.APPROVED, now, actor.id
        employer.verification_status, employer.verified_at = VerificationStatus.VERIFIED, now
    else:
        latest.status, latest.reviewed_at, latest.reviewer_id = VerificationRequestStatus.REJECTED, now, actor.id
        employer.verification_status = VerificationStatus.REJECTED
    latest.decision_reason = reason
    audit.record(db, actor_id=actor.id, action=f"employer.verification.{decision}", target_type="employer",
                 target_id=employer.id, previous={"verification_status": previous.value},
                 new={"verification_status": employer.verification_status.value}, reason=reason)
    await db.commit()
    await db.refresh(employer)
    return employer


async def set_employer_status(db: AsyncSession, actor: User, employer_id: uuid.UUID, status: EmployerAccountStatus,
                              reason: str) -> EmployerProfile:
    """Suspended employers' listings disappear from job seekers immediately (visibility
    filter) and the employer cannot publish or manage postings."""
    employer = await get_employer(db, employer_id)
    if employer.account_status == status:
        raise ConflictError("The employer already has this status.", code="status_unchanged")
    previous = employer.account_status
    employer.account_status = status
    employer.status_reason = reason if status == EmployerAccountStatus.SUSPENDED else None
    audit.record(db, actor_id=actor.id, action=f"employer.{'suspend' if status == EmployerAccountStatus.SUSPENDED else 'reactivate'}",
                 target_type="employer", target_id=employer.id, previous={"account_status": previous.value},
                 new={"account_status": status.value}, reason=reason)
    await db.commit()
    await db.refresh(employer)
    return employer


# ---------------- Jobs ----------------
async def list_jobs(db: AsyncSession, *, status: PostingStatus | None, source: JobSource | None, employer_id: uuid.UUID | None,
                    q: str | None, page: int, page_size: int) -> tuple[list[Job], int]:
    stmt = select(Job)
    if status:
        stmt = stmt.where(Job.posting_status == status)
    if source:
        stmt = stmt.where(Job.source == source)
    if employer_id:
        stmt = stmt.where(Job.employer_id == employer_id)
    if q:
        stmt = stmt.where(or_(Job.title.ilike(_like(q), escape="\\"), Job.company_name.ilike(_like(q), escape="\\")))
    return await _paged(db, stmt, [Job.updated_at.desc(), Job.id], page, page_size)


async def get_job(db: AsyncSession, job_id: uuid.UUID) -> Job:
    job = await db.get(Job, job_id)
    if job is None:
        raise NotFoundError("Job not found.", code="job_not_found")
    return job


async def moderate_job(db: AsyncSession, actor: User, job_id: uuid.UUID, decision: str, reason: str | None) -> Job:
    job = await get_job(db, job_id)
    if job.source != JobSource.SHARKHIRE:
        raise BadRequestError("Only SharkHire listings can be moderated.", code="not_moderatable")
    if job.posting_status != PostingStatus.PENDING_REVIEW:
        raise ConflictError("Only jobs pending review can be moderated.", code="not_pending_review")
    now = utcnow()
    if decision == "approve":
        employer = await db.get(EmployerProfile, job.employer_id) if job.employer_id else None
        if employer is None or employer.account_status != EmployerAccountStatus.ACTIVE or \
                employer.verification_status != VerificationStatus.VERIFIED:
            raise ConflictError("The employer is not verified and active.", code="employer_not_eligible")
        if job.application_deadline is not None and job.application_deadline < now.date():
            raise ValidationAppError("The application deadline has passed.", code="deadline_passed")
        job.posting_status = PostingStatus.PUBLISHED
        job.published_at = job.published_at or now
        job.posted_at = job.posted_at or now
        job.last_seen_at = now
    else:
        job.posting_status = PostingStatus.REJECTED
    job.moderation_note, job.moderated_by, job.moderated_at = reason, actor.id, now
    audit.record(db, actor_id=actor.id, action=f"job.moderation.{decision}", target_type="job", target_id=job.id,
                 previous={"posting_status": "pending_review"}, new={"posting_status": job.posting_status.value}, reason=reason)
    await db.commit()
    await db.refresh(job)
    return job


ADMIN_JOB_TARGETS = {PostingStatus.SUSPENDED, PostingStatus.CLOSED, PostingStatus.ARCHIVED, PostingStatus.PAUSED,
                     PostingStatus.PUBLISHED}


async def set_job_status(db: AsyncSession, actor: User, job_id: uuid.UUID, status: PostingStatus, reason: str) -> Job:
    job = await get_job(db, job_id)
    if job.source != JobSource.SHARKHIRE:
        raise BadRequestError("Only SharkHire listings can be changed.", code="not_moderatable")
    if status not in ADMIN_JOB_TARGETS:
        raise ValidationAppError("Use moderation to approve or reject pending jobs.", code="invalid_transition")
    if status == job.posting_status:
        raise ConflictError("The job already has this status.", code="status_unchanged")
    if status == PostingStatus.PUBLISHED and job.posting_status != PostingStatus.SUSPENDED:
        raise ConflictError("Only suspended jobs can be reinstated by an administrator.", code="invalid_transition")
    previous = job.posting_status
    job.posting_status = status
    job.moderation_note, job.moderated_by, job.moderated_at = reason, actor.id, utcnow()
    audit.record(db, actor_id=actor.id, action="job.admin_status_change", target_type="job", target_id=job.id,
                 previous={"posting_status": previous.value if previous else None}, new={"posting_status": status.value},
                 reason=reason)
    await db.commit()
    await db.refresh(job)
    return job


# ---------------- Reports ----------------
async def list_reports(db: AsyncSession, *, status: ReportStatus | None, target_type: ReportTargetType | None,
                       page: int, page_size: int) -> tuple[list[Report], int]:
    stmt = select(Report)
    if status:
        stmt = stmt.where(Report.status == status)
    if target_type:
        stmt = stmt.where(Report.target_type == target_type)
    return await _paged(db, stmt, [Report.created_at.desc(), Report.id], page, page_size)


async def get_report(db: AsyncSession, report_id: uuid.UUID) -> Report:
    report = await db.get(Report, report_id)
    if report is None:
        raise NotFoundError("Report not found.", code="report_not_found")
    return report


async def set_report_status(db: AsyncSession, actor: User, report_id: uuid.UUID, status: ReportStatus, note: str | None) -> Report:
    report = await get_report(db, report_id)
    if report.status == status:
        raise ConflictError("The report already has this status.", code="status_unchanged")
    if status in {ReportStatus.RESOLVED, ReportStatus.REJECTED} and not note:
        raise ValidationAppError("A resolution note is required.", code="note_required")
    previous = report.status
    report.status = status
    report.resolution_note = note or report.resolution_note
    report.reviewed_by, report.reviewed_at = actor.id, utcnow()
    audit.record(db, actor_id=actor.id, action="report.status_change", target_type="report", target_id=report.id,
                 previous={"status": previous.value}, new={"status": status.value}, reason=note)
    await db.commit()
    await db.refresh(report)
    return report


# ---------------- Dashboard & audit ----------------
async def dashboard(db: AsyncSession) -> dict[str, Any]:
    async def count(stmt: Any) -> int:
        return int(await db.scalar(stmt) or 0)

    users_by_role = dict((await db.execute(select(User.role, func.count()).group_by(User.role))).all())
    jobs_by_status = dict((await db.execute(
        select(Job.posting_status, func.count()).where(Job.source == JobSource.SHARKHIRE).group_by(Job.posting_status)
    )).all())
    return {
        "total_users": await count(select(func.count()).select_from(User)),
        "active_users": await count(select(func.count()).select_from(User).where(User.is_active.is_(True))),
        "suspended_users": await count(select(func.count()).select_from(User).where(User.is_active.is_(False))),
        "job_seekers": int(users_by_role.get(UserRole.JOB_SEEKER, 0)),
        "employers": int(users_by_role.get(UserRole.EMPLOYER, 0)),
        "admins": int(users_by_role.get(UserRole.ADMIN, 0)),
        "employer_profiles": await count(select(func.count()).select_from(EmployerProfile)),
        "verified_employers": await count(select(func.count()).select_from(EmployerProfile).where(
            EmployerProfile.verification_status == VerificationStatus.VERIFIED)),
        "pending_verification_requests": await count(select(func.count()).select_from(EmployerVerificationRequest).where(
            EmployerVerificationRequest.status.in_([VerificationRequestStatus.PENDING, VerificationRequestStatus.UNDER_REVIEW]))),
        "suspended_employers": await count(select(func.count()).select_from(EmployerProfile).where(
            EmployerProfile.account_status == EmployerAccountStatus.SUSPENDED)),
        "sharkhire_jobs_total": int(sum(jobs_by_status.values())),
        "published_jobs": int(jobs_by_status.get(PostingStatus.PUBLISHED, 0)),
        "pending_moderation": int(jobs_by_status.get(PostingStatus.PENDING_REVIEW, 0)),
        "closed_jobs": int(jobs_by_status.get(PostingStatus.CLOSED, 0)),
        "external_jobs_stored": await count(select(func.count()).select_from(Job).where(Job.source.in_(EXTERNAL_SOURCES))),
        "total_applications": await count(select(func.count()).select_from(Application)),
        "internal_applications": await count(select(func.count()).select_from(Application).where(Application.employer_status.is_not(None))),
        "pending_reports": await count(select(func.count()).select_from(Report).where(
            Report.status.in_([ReportStatus.PENDING, ReportStatus.UNDER_REVIEW]))),
        "jsearch_quota": plan_quota.snapshot(),
        "definitions": {
            "jsearch_quota": "Last RapidAPI plan quota seen by this server process (null until the first provider call).",
            "active_users": "Users whose account is not suspended in SharkHire.",
            "published_jobs": "SharkHire listings in 'published' state (expired-by-deadline listings may still be counted until marked expired).",
            "total_applications": "All application records, including job seekers' personal tracking of external jobs.",
            "internal_applications": "Applications submitted to SharkHire employer listings.",
        },
    }


async def list_audit_logs(db: AsyncSession, *, action: str | None, actor_id: uuid.UUID | None, target_type: str | None,
                          target_id: str | None, page: int, page_size: int) -> tuple[list[AdminAuditLog], int]:
    stmt = select(AdminAuditLog)
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
    if actor_id:
        stmt = stmt.where(AdminAuditLog.actor_user_id == actor_id)
    if target_type:
        stmt = stmt.where(AdminAuditLog.target_type == target_type)
    if target_id:
        stmt = stmt.where(AdminAuditLog.target_id == target_id)
    return await _paged(db, stmt, [AdminAuditLog.created_at.desc(), AdminAuditLog.id], page, page_size)


# ---------------- Reports (user-facing) ----------------
async def create_report(db: AsyncSession, reporter: User, target_type: ReportTargetType, target_id: uuid.UUID,
                        reason: Any, description: str | None) -> Report:
    from app.services.visibility import is_visible

    if target_type == ReportTargetType.JOB:
        job = await db.get(Job, target_id)
        if job is None or not await is_visible(db, job):
            raise NotFoundError("Job not found.", code="job_not_found")
    else:
        employer = await db.get(EmployerProfile, target_id)
        if employer is None:
            raise NotFoundError("Employer not found.", code="employer_not_found")
        if employer.owner_user_id == reporter.id:
            raise BadRequestError("You cannot report your own company.", code="self_report")
    duplicate = await db.scalar(select(Report.id).where(
        Report.reporter_id == reporter.id, Report.target_type == target_type, Report.target_id == target_id,
        Report.status.in_([ReportStatus.PENDING, ReportStatus.UNDER_REVIEW])))
    if duplicate:
        raise ConflictError("You already have an open report for this item.", code="duplicate_report")
    report = Report(reporter_id=reporter.id, target_type=target_type, target_id=target_id, reason=reason,
                    description=description)
    db.add(report)
    await db.commit()
    await db.refresh(report)
    return report
