"""Employer workflows: registration, profile, verification, job postings, applicants.

Ownership rules: every employer query is scoped to the caller's own
``EmployerProfile``; other employers' jobs/applicants behave as non-existent (404).
"""

from __future__ import annotations

import contextlib
import uuid
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationAppError,
)
from app.models.application import (
    Application,
    ApplicationStatus,
    EmployerReviewStatus,
)
from app.models.base import utcnow
from app.models.employer import (
    EmployerAccountStatus,
    EmployerProfile,
    EmployerVerificationRequest,
    VerificationStatus,
)
from app.models.job import Job, JobSource, PostingStatus, WorkplaceType
from app.models.profile import UserCV, UserProfile
from app.models.user import User, UserRole
from app.schemas.employer import EmployerJobIn, EmployerProfileIn, VerificationSubmit
from app.services import audit
from app.services.cv.validation import ValidatedFile
from app.services.storage import StorageBackend, StorageError, build_object_path

# Employer-initiated posting transitions. Admin-only states (published-from-review,
# rejected, suspended) can never be entered by an employer.
EMPLOYER_TRANSITIONS: dict[PostingStatus, set[PostingStatus]] = {
    PostingStatus.DRAFT: {PostingStatus.PENDING_REVIEW, PostingStatus.PUBLISHED, PostingStatus.ARCHIVED},
    PostingStatus.PENDING_REVIEW: {PostingStatus.DRAFT},
    PostingStatus.REJECTED: {PostingStatus.DRAFT, PostingStatus.PENDING_REVIEW, PostingStatus.ARCHIVED},
    PostingStatus.PUBLISHED: {PostingStatus.PAUSED, PostingStatus.CLOSED},
    PostingStatus.PAUSED: {PostingStatus.PUBLISHED, PostingStatus.CLOSED},
    PostingStatus.CLOSED: {PostingStatus.ARCHIVED},
    PostingStatus.EXPIRED: {PostingStatus.CLOSED, PostingStatus.ARCHIVED, PostingStatus.PENDING_REVIEW},
    PostingStatus.ARCHIVED: set(),
    PostingStatus.SUSPENDED: set(),
}
EDITABLE = {PostingStatus.DRAFT, PostingStatus.REJECTED, PostingStatus.PUBLISHED, PostingStatus.PAUSED, PostingStatus.EXPIRED}


# ---------------- Profile ----------------
async def get_my_employer(db: AsyncSession, user: User) -> EmployerProfile:
    employer = await db.scalar(select(EmployerProfile).where(EmployerProfile.owner_user_id == user.id))
    if employer is None:
        raise PermissionDeniedError("Register your company first.", code="employer_profile_required")
    return employer


def ensure_active(employer: EmployerProfile) -> None:
    if employer.account_status != EmployerAccountStatus.ACTIVE:
        raise PermissionDeniedError("This employer account is suspended.", code="employer_suspended")


async def register(db: AsyncSession, user: User, body: EmployerProfileIn) -> EmployerProfile:
    """Create the company profile and grant the employer role.

    Only job seekers (or employer-role users without a profile) may register;
    admins cannot convert themselves. Publishing still requires verification.
    """
    if user.role == UserRole.ADMIN:
        raise PermissionDeniedError("Administrators cannot register as employers.", code="insufficient_role")
    existing = await db.scalar(select(EmployerProfile.id).where(EmployerProfile.owner_user_id == user.id))
    if existing:
        raise ConflictError("You have already registered a company.", code="employer_exists")
    active_applications = await db.scalar(
        select(func.count()).select_from(Application).where(
            Application.user_id == user.id, Application.employer_status.is_not(None),
            Application.status != ApplicationStatus.WITHDRAWN)
    )
    if active_applications:
        raise ConflictError("Withdraw your active SharkHire job applications before registering as an employer.",
                            code="active_applications")
    employer = EmployerProfile(owner_user_id=user.id, **body.model_dump(mode="json"))
    db.add(employer)
    previous_role = user.role
    user.role = UserRole.EMPLOYER
    await db.flush()
    audit.record(db, actor_id=user.id, action="employer.register", target_type="employer", target_id=employer.id,
                 previous={"role": previous_role.value}, new={"role": UserRole.EMPLOYER.value, "company_name": employer.company_name})
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("You have already registered a company.", code="employer_exists") from None
    await db.refresh(employer)
    return employer


async def update_profile(db: AsyncSession, employer: EmployerProfile, body: EmployerProfileIn) -> EmployerProfile:
    for key, value in body.model_dump(mode="json").items():
        setattr(employer, key, value)
    await db.commit()
    await db.refresh(employer)
    return employer


async def logo_url(storage: StorageBackend, employer: EmployerProfile) -> str | None:
    if not employer.logo_path:
        return None
    settings = get_settings()
    try:
        return await storage.signed_url(settings.SUPABASE_STORAGE_MEDIA_BUCKET, employer.logo_path, settings.SIGNED_URL_TTL_SECONDS)
    except StorageError:
        return None


async def set_logo(db: AsyncSession, employer: EmployerProfile, storage: StorageBackend, image: ValidatedFile) -> None:
    bucket = get_settings().SUPABASE_STORAGE_MEDIA_BUCKET
    path = build_object_path("logos", employer.id, image.extension)
    await storage.put(bucket, path, image.data, image.content_type)
    old, employer.logo_path = employer.logo_path, path
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        await storage.delete(bucket, path)
        raise
    if old:
        with contextlib.suppress(StorageError):
            await storage.delete(bucket, old)


async def delete_logo(db: AsyncSession, employer: EmployerProfile, storage: StorageBackend) -> None:
    if not employer.logo_path:
        raise NotFoundError("No logo to delete.", code="logo_not_found")
    await storage.delete(get_settings().SUPABASE_STORAGE_MEDIA_BUCKET, employer.logo_path)
    employer.logo_path = None
    await db.commit()


async def public_profile(db: AsyncSession, employer_id: uuid.UUID) -> tuple[EmployerProfile, int]:
    from app.services.visibility import internal_visible_filter

    employer = await db.get(EmployerProfile, employer_id)
    if employer is None or employer.account_status != EmployerAccountStatus.ACTIVE:
        raise NotFoundError("Employer not found.", code="employer_not_found")
    open_jobs = await db.scalar(
        select(func.count()).select_from(Job).where(Job.employer_id == employer.id, internal_visible_filter())
    ) or 0
    return employer, int(open_jobs)


# ---------------- Verification ----------------
async def latest_verification(db: AsyncSession, employer: EmployerProfile) -> EmployerVerificationRequest | None:
    return await db.scalar(
        select(EmployerVerificationRequest).where(EmployerVerificationRequest.employer_id == employer.id)
        .order_by(EmployerVerificationRequest.submitted_at.desc()).limit(1)
    )


def can_submit_verification(employer: EmployerProfile) -> bool:
    return employer.verification_status in {VerificationStatus.UNVERIFIED, VerificationStatus.REJECTED}


async def submit_verification(db: AsyncSession, user: User, employer: EmployerProfile, body: VerificationSubmit) -> EmployerVerificationRequest:
    ensure_active(employer)
    if not can_submit_verification(employer):
        raise ConflictError("A verification request is already pending or approved.", code="verification_not_allowed")
    request = EmployerVerificationRequest(employer_id=employer.id, submitted_by=user.id, **body.model_dump(mode="json"))
    db.add(request)
    employer.verification_status = VerificationStatus.PENDING
    await db.commit()
    await db.refresh(request)
    return request


# ---------------- Job postings ----------------
def _job_values(body: EmployerJobIn) -> dict[str, Any]:
    # Python mode keeps dates as date objects; enums are reduced to stored values below.
    data = body.model_dump(exclude={"submit_for_review"})
    data["employment_type"] = body.employment_type.value
    data["experience_level"] = body.experience_level.value if body.experience_level else None
    url = data.pop("external_application_url")
    data["apply_url"] = url
    data["employment_types"] = [data["employment_type"]]
    data["is_remote"] = body.workplace_type == WorkplaceType.REMOTE
    data["workplace_type"] = body.workplace_type
    data["country"] = data["country"].lower() if data.get("country") else None
    if not data.get("location"):
        data["location"] = ", ".join(p for p in (data.get("city"), body.country) if p) or (
            "Remote" if body.workplace_type == WorkplaceType.REMOTE else None)
    data["derived_requirements"] = None
    return data


async def get_own_job(db: AsyncSession, employer: EmployerProfile, job_id: uuid.UUID) -> Job:
    job = await db.scalar(select(Job).where(Job.id == job_id, Job.employer_id == employer.id, Job.source == JobSource.SHARKHIRE))
    if job is None:
        raise NotFoundError("Job not found.", code="job_not_found")
    return job


async def _publish_target(db: AsyncSession, employer: EmployerProfile, job: Job) -> PostingStatus:
    """Validate that the job may go live; return pending_review or published."""
    ensure_active(employer)
    if employer.verification_status != VerificationStatus.VERIFIED:
        raise PermissionDeniedError("Your company must be verified before publishing jobs.", code="employer_not_verified")
    if job.application_deadline is not None and job.application_deadline < utcnow().date():
        raise ValidationAppError("The application deadline has passed. Update it first.", code="deadline_passed")
    return PostingStatus.PENDING_REVIEW if get_settings().JOB_MODERATION_ENABLED else PostingStatus.PUBLISHED


async def create_job(db: AsyncSession, user: User, employer: EmployerProfile, body: Any) -> Job:
    ensure_active(employer)
    job = Job(
        source=JobSource.SHARKHIRE, external_id=None, employer_id=employer.id, created_by_user_id=user.id,
        company_name=employer.company_name, company_website=employer.website, posting_status=PostingStatus.DRAFT,
        highlights={}, apply_options=[], provider_metadata={}, **_job_values(body),
    )
    db.add(job)
    await db.flush()
    if body.submit_for_review:
        target = await _publish_target(db, employer, job)
        _set_status(job, target, user)
    await db.commit()
    await db.refresh(job)
    return job


def _set_status(job: Job, status: PostingStatus, actor: User) -> None:
    job.posting_status = status
    if status == PostingStatus.PUBLISHED and job.published_at is None:
        job.published_at = utcnow()
        job.posted_at = job.published_at
        job.last_seen_at = job.published_at


async def update_job(db: AsyncSession, user: User, employer: EmployerProfile, job_id: uuid.UUID, body: EmployerJobIn) -> Job:
    ensure_active(employer)
    job = await get_own_job(db, employer, job_id)
    if job.posting_status not in EDITABLE:
        raise ConflictError(f"A {job.posting_status.value} job cannot be edited.", code="job_not_editable")
    for key, value in _job_values(body).items():
        setattr(job, key, value)
    job.company_name = employer.company_name
    if job.posting_status == PostingStatus.PUBLISHED and get_settings().JOB_MODERATION_ENABLED:
        job.posting_status = PostingStatus.PENDING_REVIEW  # edited content is re-reviewed
        audit.record(db, actor_id=user.id, action="job.resubmitted_after_edit", target_type="job", target_id=job.id,
                     previous={"posting_status": "published"}, new={"posting_status": "pending_review"})
    await db.commit()
    await db.refresh(job)
    return job


async def change_job_status(db: AsyncSession, user: User, employer: EmployerProfile, job_id: uuid.UUID,
                            target: PostingStatus, reason: str | None) -> Job:
    ensure_active(employer)
    job = await get_own_job(db, employer, job_id)
    current = job.posting_status
    assert current is not None
    if target not in EMPLOYER_TRANSITIONS.get(current, set()):
        raise ConflictError(f"Cannot change a {current.value} job to {target.value}.", code="invalid_transition")
    if target in {PostingStatus.PENDING_REVIEW, PostingStatus.PUBLISHED} and current != PostingStatus.PAUSED:
        allowed = await _publish_target(db, employer, job)
        if target == PostingStatus.PUBLISHED and allowed == PostingStatus.PENDING_REVIEW:
            raise ConflictError("Jobs must be reviewed by an administrator. Submit for review instead.",
                                code="moderation_required")
        target = allowed
    if current == PostingStatus.PAUSED and target == PostingStatus.PUBLISHED:
        ensure_active(employer)
        if job.application_deadline is not None and job.application_deadline < utcnow().date():
            raise ValidationAppError("The application deadline has passed. Update it first.", code="deadline_passed")
    _set_status(job, target, user)
    audit.record(db, actor_id=user.id, action="job.status_change", target_type="job", target_id=job.id,
                 previous={"posting_status": current.value}, new={"posting_status": job.posting_status.value}, reason=reason)
    await db.commit()
    await db.refresh(job)
    return job


async def delete_job(db: AsyncSession, user: User, employer: EmployerProfile, job_id: uuid.UUID) -> str:
    """Retention policy: a draft with no applications is deleted; anything else is
    archived so applicants' records and audit history stay intact."""
    ensure_active(employer)
    job = await get_own_job(db, employer, job_id)
    has_apps = await db.scalar(select(func.count()).select_from(Application).where(Application.job_id == job.id)) or 0
    if job.posting_status == PostingStatus.DRAFT and not has_apps:
        await db.delete(job)
        await db.commit()
        return "deleted"
    if job.posting_status == PostingStatus.SUSPENDED:
        raise ConflictError("Suspended jobs can only be changed by an administrator.", code="job_suspended")
    previous = job.posting_status
    job.posting_status = PostingStatus.ARCHIVED
    audit.record(db, actor_id=user.id, action="job.archived", target_type="job", target_id=job.id,
                 previous={"posting_status": previous.value if previous else None}, new={"posting_status": "archived"})
    await db.commit()
    return "archived"


async def expire_due_jobs(db: AsyncSession, employer_id: uuid.UUID | None = None) -> int:
    """Mark published/paused SharkHire jobs whose deadline has passed as expired."""
    stmt = select(Job).where(
        Job.source == JobSource.SHARKHIRE,
        Job.posting_status.in_([PostingStatus.PUBLISHED, PostingStatus.PAUSED]),
        Job.application_deadline < utcnow().date(),
    )
    if employer_id:
        stmt = stmt.where(Job.employer_id == employer_id)
    jobs = list(await db.scalars(stmt))
    for job in jobs:
        job.posting_status = PostingStatus.EXPIRED
    if jobs:
        await db.commit()
    return len(jobs)


async def applicant_counts(db: AsyncSession, job_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not job_ids:
        return {}
    rows = await db.execute(
        select(Application.job_id, func.count()).where(Application.job_id.in_(job_ids), Application.employer_status.is_not(None))
        .group_by(Application.job_id)
    )
    return {job_id: int(n) for job_id, n in rows.all()}


async def list_jobs(db: AsyncSession, employer: EmployerProfile, status: PostingStatus | None, page: int, page_size: int) -> tuple[list[Job], int]:
    await expire_due_jobs(db, employer.id)
    stmt: Select[Any] = select(Job).where(Job.employer_id == employer.id, Job.source == JobSource.SHARKHIRE)
    if status:
        stmt = stmt.where(Job.posting_status == status)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await db.scalars(stmt.order_by(Job.updated_at.desc(), Job.id).offset((page - 1) * page_size).limit(page_size))
    return list(rows), int(total)


def job_out(job: Job, applicants: int = 0) -> dict[str, Any]:
    return {
        "id": job.id, "title": job.title, "description": job.description, "required_skills": job.required_skills or [],
        "preferred_skills": job.preferred_skills or [], "experience_level": job.experience_level,
        "min_experience_years": job.min_experience_years, "max_experience_years": job.max_experience_years,
        "employment_type": job.employment_type, "workplace_type": job.workplace_type, "city": job.city,
        "location": job.location, "country": job.country.upper() if job.country else None, "is_remote": job.is_remote,
        "min_salary": job.min_salary, "max_salary": job.max_salary, "salary_currency": job.salary_currency,
        "salary_period": job.salary_period, "education_requirements": job.education_requirements,
        "application_deadline": job.application_deadline, "application_instructions": job.application_instructions,
        "external_application_url": job.apply_url, "openings": job.openings, "benefits": job.benefits or [],
        "posting_status": job.posting_status, "moderation_note": job.moderation_note, "published_at": job.published_at,
        "applicant_count": applicants, "created_at": job.created_at, "updated_at": job.updated_at,
    }


# ---------------- Applicants ----------------
def _employer_apps(employer: EmployerProfile) -> Select[Any]:
    return (
        select(Application, Job)
        .join(Job, Job.id == Application.job_id)
        .where(Job.employer_id == employer.id, Job.source == JobSource.SHARKHIRE, Application.employer_status.is_not(None))
    )


async def list_applicants(db: AsyncSession, employer: EmployerProfile, *, job_id: uuid.UUID | None,
                          status: EmployerReviewStatus | None, sort: str, page: int, page_size: int) -> tuple[list[dict[str, Any]], int]:
    if job_id is not None:
        await get_own_job(db, employer, job_id)
    stmt = _employer_apps(employer)
    if job_id is not None:
        stmt = stmt.where(Application.job_id == job_id)
    if status is not None:
        stmt = stmt.where(Application.employer_status == status)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    order = Application.submitted_at.asc() if sort == "oldest" else Application.submitted_at.desc()
    rows = (await db.execute(stmt.order_by(order, Application.id).offset((page - 1) * page_size).limit(page_size))).all()
    return await applicants_out(db, [a for a, _ in rows]), int(total)


async def get_applicant(db: AsyncSession, employer: EmployerProfile, application_id: uuid.UUID) -> Application:
    row = (await db.execute(_employer_apps(employer).where(Application.id == application_id))).first()
    if row is None:
        raise NotFoundError("Application not found.", code="application_not_found")
    return row[0]


async def applicants_out(db: AsyncSession, applications: list[Application]) -> list[dict[str, Any]]:
    """Batch version: 3 queries for the whole page instead of 3 per applicant (no N+1)."""
    if not applications:
        return []
    user_ids = {a.user_id for a in applications}
    cv_ids = {a.selected_cv_id for a in applications if a.selected_cv_id}
    users = {u.id: u for u in await db.scalars(select(User).where(User.id.in_(user_ids)))}
    profiles = {p.user_id: p for p in await db.scalars(select(UserProfile).where(UserProfile.user_id.in_(user_ids)))}
    cvs = {c.id: c for c in await db.scalars(select(UserCV).where(UserCV.id.in_(cv_ids)))} if cv_ids else {}
    return [
        _applicant_dict(a, users[a.user_id], profiles.get(a.user_id), cvs.get(a.selected_cv_id) if a.selected_cv_id else None)
        for a in applications
    ]


async def applicant_out(db: AsyncSession, application: Application) -> dict[str, Any]:
    return (await applicants_out(db, [application]))[0]


def _applicant_dict(application: Application, user: User, profile: UserProfile | None, cv: UserCV | None) -> dict[str, Any]:
    """Employer view: excludes the candidate's private notes, reminders and tracking data."""
    return {
        "id": application.id, "job_id": application.job_id, "job_title": application.job_title,
        "submitted_at": application.submitted_at, "employer_status": application.employer_status,
        "employer_notes": application.employer_notes,
        "candidate_withdrawn": application.status == ApplicationStatus.WITHDRAWN,
        "cover_letter": application.cover_letter, "match_percentage": application.match_percentage,
        "cv_available": cv is not None, "cv_filename": cv.original_filename if cv else None,
        "applicant": {
            "user_id": user.id, "full_name": (profile.full_name if profile else None) or user.full_name,
            "email": user.email, "phone": profile.phone if profile else None,
            "headline": profile.headline if profile else None, "city": profile.city if profile else None,
            "country": profile.country if profile else None, "skills": (profile.skills if profile else None) or [],
            "years_of_experience": profile.years_of_experience if profile else None,
        },
    }


async def update_applicant_status(db: AsyncSession, user: User, employer: EmployerProfile, application_id: uuid.UUID,
                                  status: EmployerReviewStatus, notes: str | None) -> Application:
    ensure_active(employer)
    application = await get_applicant(db, employer, application_id)
    if application.status == ApplicationStatus.WITHDRAWN:
        raise ConflictError("The candidate has withdrawn this application.", code="candidate_withdrawn")
    previous = application.employer_status
    if previous == status and notes is None:
        raise ConflictError("The application already has this status.", code="status_unchanged")
    application.employer_status = status
    if notes is not None:
        application.employer_notes = notes
    if previous != status:
        from app.services.job_management import add_history

        await add_history(db, application, previous.value if previous else None, status.value, user, None, pipeline="employer")
    await db.commit()
    await db.refresh(application)
    return application


async def applicant_cv_url(db: AsyncSession, user: User, employer: EmployerProfile, storage: StorageBackend,
                           application_id: uuid.UUID) -> dict[str, Any]:
    """Short-lived signed URL for the CV the candidate attached to THIS application."""
    ensure_active(employer)
    application = await get_applicant(db, employer, application_id)
    if application.selected_cv_id is None:
        raise NotFoundError("No CV is attached to this application.", code="cv_not_found")
    cv = await db.scalar(select(UserCV).where(UserCV.id == application.selected_cv_id, UserCV.user_id == application.user_id))
    if cv is None:
        raise NotFoundError("The candidate's CV is no longer available.", code="cv_not_found")
    settings = get_settings()
    url = await storage.signed_url(settings.SUPABASE_STORAGE_CV_BUCKET, cv.storage_path, settings.SIGNED_URL_TTL_SECONDS, cv.original_filename)
    audit.record(db, actor_id=user.id, action="employer.cv_access", target_type="application", target_id=application.id,
                 new={"cv_id": str(cv.id), "employer_id": str(employer.id)})
    await db.commit()
    return {"url": url, "expires_in": settings.SIGNED_URL_TTL_SECONDS, "filename": cv.original_filename}


async def statistics(db: AsyncSession, employer: EmployerProfile) -> dict[str, Any]:
    await expire_due_jobs(db, employer.id)
    job_rows = await db.execute(
        select(Job.posting_status, func.count()).where(Job.employer_id == employer.id, Job.source == JobSource.SHARKHIRE)
        .group_by(Job.posting_status)
    )
    jobs_by_status = {s.value: 0 for s in PostingStatus}
    for status, n in job_rows.all():
        jobs_by_status[status.value] = int(n)
    apps = (await db.execute(_employer_apps(employer).with_only_columns(Application.employer_status, Application.status, Job.id, Job.title))).all()
    by_status = {s.value: 0 for s in EmployerReviewStatus}
    per_job: dict[uuid.UUID, dict[str, Any]] = {}
    withdrawn = 0
    for emp_status, cand_status, job_id, title in apps:
        by_status[emp_status.value] += 1
        withdrawn += cand_status == ApplicationStatus.WITHDRAWN
        per_job.setdefault(job_id, {"job_id": job_id, "title": title, "applicants": 0})["applicants"] += 1
    return {
        "total_jobs": sum(jobs_by_status.values()),
        "jobs_by_status": jobs_by_status,
        "total_applicants": len(apps),
        "applicants_by_status": by_status,
        "applicants_per_job": sorted(per_job.values(), key=lambda r: (-r["applicants"], r["title"])),
        "pending_applications": by_status["submitted"] + by_status["under_review"],
        "shortlisted": by_status["shortlisted"],
        "interviews": by_status["interview"],
        "offers": by_status["offered"],
        "hires": by_status["hired"],
        "withdrawn": int(withdrawn),
    }
