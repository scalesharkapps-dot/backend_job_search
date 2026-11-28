"""Saved jobs, application tracking, interviews, reminders and statistics.

Every query is filtered by the authenticated user's id; foreign ids are
indistinguishable from non-existent ones (404) to prevent IDOR enumeration.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Select, and_, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationAppError,
)
from app.models.application import (
    INTERVIEW_STAGE,
    OFFER_STAGE,
    PRE_APPLICATION,
    TERMINAL,
    Application,
    ApplicationSource,
    ApplicationStatus,
    ApplicationStatusHistory,
    EmployerReviewStatus,
    Interview,
    InterviewOutcome,
    SavedJob,
)
from app.models.base import utcnow
from app.models.job import EXTERNAL_SOURCES, Job, JobSource
from app.models.profile import UserCV
from app.models.recommendation import JobRecommendation
from app.models.user import User, UserRole
from app.schemas.application import ApplicationCreate, ApplicationUpdate, InterviewBase, InterviewCreate
from app.services import application_links, visibility

STATUS_ORDER = list(ApplicationStatus)


async def _apply_url_allowed(db: AsyncSession, user: User, job: Job) -> bool:
    """Copy the job's external apply link only if the user may see it (Pro, or unlocked it)."""
    if job.source not in EXTERNAL_SOURCES or application_links.apply_links_visible():
        return True
    return await application_links.has_unlocked(db, user.id, job.id)
# Which application source a tracked job records (external listings are applied to outside SharkHire).
_APPLICATION_SOURCE = {
    JobSource.JSEARCH: ApplicationSource.JSEARCH,
    JobSource.TECHMAP: ApplicationSource.TECHMAP,
    JobSource.SHARKHIRE: ApplicationSource.SHARKHIRE,
}


def _like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def _visible_job(db: AsyncSession, job_id: uuid.UUID) -> Job:
    return await visibility.get_visible_job(db, job_id)


async def _match_for(db: AsyncSession, user: User, job_id: uuid.UUID) -> int | None:
    return await db.scalar(
        select(JobRecommendation.match_percentage).where(
            JobRecommendation.user_id == user.id, JobRecommendation.job_id == job_id
        )
    )


# ---------------- Saved jobs ----------------
async def save_job(db: AsyncSession, user: User, job_id: uuid.UUID, notes: str | None) -> tuple[SavedJob, Job]:
    job = await _visible_job(db, job_id)
    existing = await db.scalar(select(SavedJob).where(SavedJob.user_id == user.id, SavedJob.job_id == job_id))
    if existing:
        raise ConflictError("This job is already saved.", code="already_saved", details={"saved_job_id": str(existing.id)})
    saved = SavedJob(user_id=user.id, job_id=job_id, notes=notes, match_percentage=await _match_for(db, user, job_id))
    db.add(saved)
    try:
        await db.commit()
    except IntegrityError:  # concurrent save of the same job
        await db.rollback()
        raise ConflictError("This job is already saved.", code="already_saved") from None
    return saved, job


async def get_saved(db: AsyncSession, user: User, job_id: uuid.UUID) -> tuple[SavedJob, Job]:
    row = (
        await db.execute(
            select(SavedJob, Job).join(Job, Job.id == SavedJob.job_id).where(SavedJob.user_id == user.id, SavedJob.job_id == job_id)
        )
    ).first()
    if row is None:
        raise NotFoundError("Saved job not found.", code="saved_job_not_found")
    return row[0], row[1]


async def unsave(db: AsyncSession, user: User, job_id: uuid.UUID) -> None:
    saved = await db.scalar(select(SavedJob).where(SavedJob.user_id == user.id, SavedJob.job_id == job_id))
    if saved is None:
        raise NotFoundError("Saved job not found.", code="saved_job_not_found")
    await db.delete(saved)
    await db.commit()


async def is_saved(db: AsyncSession, user: User, job_id: uuid.UUID) -> SavedJob | None:
    return await db.scalar(select(SavedJob).where(SavedJob.user_id == user.id, SavedJob.job_id == job_id))


SAVED_SORTS = {
    "saved_at": SavedJob.created_at.desc(),
    "posted_at": Job.posted_at.desc().nulls_last(),
    "title": Job.title.asc(),
    "match": SavedJob.match_percentage.desc().nulls_last(),
}


async def list_saved(
    db: AsyncSession, user: User, *, q: str | None, company: str | None, location: str | None, remote: bool | None,
    employment_type: str | None, sort: str, page: int, page_size: int,
) -> tuple[list[tuple[SavedJob, Job]], int]:
    stmt: Select[Any] = select(SavedJob, Job).join(Job, Job.id == SavedJob.job_id).where(SavedJob.user_id == user.id)
    if q:
        stmt = stmt.where(or_(Job.title.ilike(_like(q), escape="\\"), Job.company_name.ilike(_like(q), escape="\\")))
    if company:
        stmt = stmt.where(Job.company_name.ilike(_like(company), escape="\\"))
    if location:
        stmt = stmt.where(or_(Job.location.ilike(_like(location), escape="\\"), Job.city.ilike(_like(location), escape="\\")))
    if remote is not None:
        stmt = stmt.where(Job.is_remote.is_(remote))
    if employment_type:
        stmt = stmt.where(Job.employment_type == employment_type)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await db.execute(
        stmt.order_by(SAVED_SORTS[sort], SavedJob.id).offset((page - 1) * page_size).limit(page_size)
    )
    return [(r[0], r[1]) for r in rows.all()], total


# ---------------- Applications ----------------
async def internal_submission_values(db: AsyncSession, user: User, job: Job, body: ApplicationCreate) -> dict[str, Any]:
    """Rules for applying to a SharkHire employer listing (a real submission)."""
    if user.role != UserRole.JOB_SEEKER:
        raise PermissionDeniedError("Only job seekers can apply to SharkHire jobs.", code="insufficient_role")
    if body.selected_cv_id is None:
        raise ValidationAppError("Select a CV to apply to this job.", code="cv_required")
    if body.status != ApplicationStatus.APPLIED:
        raise ValidationAppError("Applications to SharkHire jobs start with status 'applied'.", code="invalid_status")
    now = utcnow()
    return {"submitted_at": now, "applied_at": now, "employer_status": EmployerReviewStatus.SUBMITTED,
            "cover_letter": body.cover_letter}


async def get_application(db: AsyncSession, user: User, application_id: uuid.UUID) -> Application:
    app_ = await db.scalar(select(Application).where(Application.id == application_id, Application.user_id == user.id))
    if app_ is None:
        raise NotFoundError("Application not found.", code="application_not_found")
    return app_


async def _check_cv(db: AsyncSession, user: User, cv_id: uuid.UUID | None) -> None:
    if cv_id is None:
        return
    owned = await db.scalar(select(UserCV.id).where(UserCV.id == cv_id, UserCV.user_id == user.id))
    if owned is None:
        raise NotFoundError("CV not found.", code="cv_not_found")


async def add_history(db: AsyncSession, application: Application, previous: str | None, new: str, user: User,
                       notes: str | None = None, pipeline: str = "candidate") -> None:
    """Append a history row with a strictly increasing timestamp per application, so
    ordering stays correct even when changes land within the same clock tick."""
    last = await db.scalar(
        select(func.max(ApplicationStatusHistory.changed_at)).where(ApplicationStatusHistory.application_id == application.id)
    )
    now = utcnow()
    if last is not None:
        last = last if last.tzinfo else last.replace(tzinfo=ZoneInfo("UTC"))
        if now <= last:
            now = last + timedelta(microseconds=1)
    db.add(ApplicationStatusHistory(
        application_id=application.id, pipeline=pipeline, previous_status=previous, new_status=new,
        changed_by_user_id=user.id, notes=notes, changed_at=now,
    ))


def _salary_text(job: Job) -> str | None:
    if not (job.min_salary or job.max_salary):
        return job.salary_text
    low = f"{job.min_salary:,.0f}" if job.min_salary else "?"
    high = f"{job.max_salary:,.0f}" if job.max_salary else "?"
    parts = [f"{low} - {high}", job.salary_currency or "", f"per {job.salary_period.lower()}" if job.salary_period else ""]
    return " ".join(p for p in parts if p)


async def create_application(db: AsyncSession, user: User, body: ApplicationCreate) -> Application:
    await _check_cv(db, user, body.selected_cv_id)
    values: dict[str, Any] = body.model_dump(exclude={"job_id"})
    job: Job | None = None
    if body.job_id is not None:
        job = await _visible_job(db, body.job_id)
        duplicate = await db.scalar(select(Application.id).where(Application.user_id == user.id, Application.job_id == job.id))
        if duplicate:
            raise ConflictError("You already track an application for this job.", code="duplicate_application",
                                details={"application_id": str(duplicate)})
        values.update(
            await internal_submission_values(db, user, job, body) if job.source == JobSource.SHARKHIRE else {}
        )
        if job.source != JobSource.SHARKHIRE:
            values.pop("cover_letter", None)
        values.update(
            job_title=body.job_title or job.title,
            company_name=body.company_name or job.company_name,
            job_location=body.job_location or job.location,
            job_apply_url=body.job_apply_url or (job.apply_url if await _apply_url_allowed(db, user, job) else None),
            employment_type=body.employment_type or job.employment_type,
            salary_information=body.salary_information or _salary_text(job),
            external_job_id=job.external_id if job.source in EXTERNAL_SOURCES else None,
            source=_APPLICATION_SOURCE.get(job.source, ApplicationSource.SHARKHIRE),
            match_percentage=await _match_for(db, user, job.id),
        )
    else:
        values["source"] = ApplicationSource.MANUAL
        values.pop("cover_letter", None)
    if values.get("applied_at") is None and body.status not in PRE_APPLICATION:
        values["applied_at"] = utcnow()
    application = Application(user_id=user.id, job_id=job.id if job else None, **values)
    db.add(application)
    await db.flush()
    await add_history(db, application, None, application.status.value, user, "Application record created")
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ConflictError("You already track an application for this job.", code="duplicate_application") from None
    await db.refresh(application)
    return application


async def update_application(db: AsyncSession, user: User, application_id: uuid.UUID, body: ApplicationUpdate) -> Application:
    application = await get_application(db, user, application_id)
    fields = body.model_fields_set
    if "selected_cv_id" in fields:
        await _check_cv(db, user, body.selected_cv_id)
    for name in fields:
        value = getattr(body, name)
        if name == "job_title" and value is None:
            continue  # title is required; omit to keep
        setattr(application, name, value)
    await db.commit()
    await db.refresh(application)
    return application


async def change_status(db: AsyncSession, user: User, application_id: uuid.UUID, status: ApplicationStatus,
                        notes: str | None) -> Application:
    """Any valid status may be chosen (users may correct mistakes); every change is
    appended to the history, never overwritten."""
    application = await get_application(db, user, application_id)
    if application.status == status:
        raise BadRequestError("The application already has this status.", code="status_unchanged")
    if application.employer_status is not None and application.status == ApplicationStatus.WITHDRAWN:
        raise ConflictError("A withdrawn SharkHire application cannot be reopened.", code="withdrawal_final")
    previous = application.status
    application.status = status
    if application.applied_at is None and status not in PRE_APPLICATION:
        application.applied_at = utcnow()
    await add_history(db, application, previous.value, status.value, user, notes)
    await db.commit()
    await db.refresh(application)
    return application


async def delete_application(db: AsyncSession, user: User, application_id: uuid.UUID) -> None:
    """Deletion policy: the candidate's own tracking record is hard-deleted together
    with its status history and interviews (cascade)."""
    application = await get_application(db, user, application_id)
    if application.employer_status is not None and application.status != ApplicationStatus.WITHDRAWN:
        raise ConflictError("Withdraw this SharkHire application before deleting it.", code="withdraw_first")
    await db.delete(application)
    await db.commit()


async def application_history(db: AsyncSession, application: Application, pipeline: str = "candidate") -> list[ApplicationStatusHistory]:
    rows = await db.scalars(
        select(ApplicationStatusHistory)
        .where(ApplicationStatusHistory.application_id == application.id, ApplicationStatusHistory.pipeline == pipeline)
        .order_by(ApplicationStatusHistory.changed_at, ApplicationStatusHistory.id)
    )
    return list(rows)


async def application_interviews(db: AsyncSession, application: Application) -> list[Interview]:
    return list(await db.scalars(select(Interview).where(Interview.application_id == application.id).order_by(Interview.scheduled_at)))


APPLICATION_SORTS = {
    "updated_at": Application.updated_at.desc(),
    "created_at": Application.created_at.desc(),
    "applied_at": Application.applied_at.desc().nulls_last(),
    "deadline": Application.deadline.asc().nulls_last(),
    "company": Application.company_name.asc().nulls_last(),
}


async def list_applications(db: AsyncSession, user: User, *, filters: dict[str, Any], sort: str, page: int,
                            page_size: int) -> tuple[list[Application], int]:
    stmt = select(Application).where(Application.user_id == user.id)
    if filters.get("status"):
        stmt = stmt.where(Application.status.in_(filters["status"]))
    for field, column in (("company", Application.company_name), ("title", Application.job_title), ("location", Application.job_location)):
        if filters.get(field):
            stmt = stmt.where(column.ilike(_like(filters[field]), escape="\\"))
    if filters.get("source"):
        stmt = stmt.where(Application.source == filters["source"])
    if filters.get("employment_type"):
        stmt = stmt.where(Application.employment_type == filters["employment_type"])
    if filters.get("applied_from"):
        stmt = stmt.where(Application.applied_at >= datetime.combine(filters["applied_from"], datetime.min.time(), ZoneInfo("UTC")))
    if filters.get("applied_to"):
        stmt = stmt.where(Application.applied_at < datetime.combine(filters["applied_to"] + timedelta(days=1), datetime.min.time(), ZoneInfo("UTC")))
    if filters.get("deadline_before"):
        stmt = stmt.where(Application.deadline <= filters["deadline_before"])
    if filters.get("follow_up_before"):
        stmt = stmt.where(Application.follow_up_date <= filters["follow_up_before"])
    if filters.get("interview_from") or filters.get("interview_to"):
        conds = [Interview.application_id == Application.id]
        if filters.get("interview_from"):
            conds.append(Interview.scheduled_at >= datetime.combine(filters["interview_from"], datetime.min.time(), ZoneInfo("UTC")))
        if filters.get("interview_to"):
            conds.append(Interview.scheduled_at < datetime.combine(filters["interview_to"] + timedelta(days=1), datetime.min.time(), ZoneInfo("UTC")))
        stmt = stmt.where(exists().where(and_(*conds)))
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await db.scalars(stmt.order_by(APPLICATION_SORTS[sort], Application.id).offset((page - 1) * page_size).limit(page_size))
    return list(rows), total


# ---------------- Interviews ----------------
async def get_interview(db: AsyncSession, user: User, interview_id: uuid.UUID) -> tuple[Interview, Application]:
    row = (
        await db.execute(
            select(Interview, Application)
            .join(Application, Application.id == Interview.application_id)
            .where(Interview.id == interview_id, Application.user_id == user.id)
        )
    ).first()
    if row is None:
        raise NotFoundError("Interview not found.", code="interview_not_found")
    return row[0], row[1]


async def add_interview(db: AsyncSession, user: User, application_id: uuid.UUID, body: InterviewCreate) -> Interview:
    application = await get_application(db, user, application_id)
    if application.status in {ApplicationStatus.WITHDRAWN}:
        raise ValidationAppError("Cannot add interviews to a withdrawn application.", code="application_withdrawn")
    interview = Interview(application_id=application.id, **body.model_dump(exclude={"update_application_status"}))
    db.add(interview)
    if body.update_application_status and STATUS_ORDER.index(application.status) < STATUS_ORDER.index(ApplicationStatus.INTERVIEW_SCHEDULED):
        previous = application.status
        application.status = ApplicationStatus.INTERVIEW_SCHEDULED
        application.applied_at = application.applied_at or utcnow()
        await add_history(db, application, previous.value, application.status.value, user, "Interview recorded")
    await db.commit()
    await db.refresh(interview)
    return interview


async def update_interview(db: AsyncSession, user: User, interview_id: uuid.UUID, body: InterviewBase) -> Interview:
    interview, _ = await get_interview(db, user, interview_id)
    for name, value in body.model_dump().items():
        setattr(interview, name, value)
    await db.commit()
    await db.refresh(interview)
    return interview


async def delete_interview(db: AsyncSession, user: User, interview_id: uuid.UUID) -> None:
    interview, _ = await get_interview(db, user, interview_id)
    await db.delete(interview)
    await db.commit()


async def upcoming_interviews(db: AsyncSession, user: User, days: int) -> list[tuple[Interview, Application]]:
    now = utcnow()
    rows = await db.execute(
        select(Interview, Application)
        .join(Application, Application.id == Interview.application_id)
        .where(Application.user_id == user.id, Interview.scheduled_at >= now,
               Interview.scheduled_at <= now + timedelta(days=days),
               Interview.outcome.in_([InterviewOutcome.PENDING, InterviewOutcome.RESCHEDULED]))
        .order_by(Interview.scheduled_at)
    )
    return [(r[0], r[1]) for r in rows.all()]


# ---------------- Reminders ----------------
async def reminders(db: AsyncSession, user: User, days: int, overdue_days: int = 7) -> list[dict[str, Any]]:
    """Reminder DATA for the client (no push notification is sent by this endpoint)."""
    today = utcnow().date()
    window_end = today + timedelta(days=days)
    window_start = today - timedelta(days=overdue_days)
    active = Application.status.notin_(list(TERMINAL))
    items: list[dict[str, Any]] = []
    rows = await db.scalars(
        select(Application).where(
            Application.user_id == user.id, active,
            or_(Application.deadline.between(window_start, window_end), Application.follow_up_date.between(window_start, window_end)),
        )
    )
    for a in rows:
        for kind, due in (("deadline", a.deadline), ("follow_up", a.follow_up_date)):
            if due and window_start <= due <= window_end:
                items.append({"type": kind, "due": due, "overdue": due < today, "application_id": a.id,
                              "job_title": a.job_title, "company_name": a.company_name, "status": a.status,
                              "text": a.reminder_text if kind == "follow_up" else None})
    for interview, a in await upcoming_interviews(db, user, days):
        items.append({"type": "interview", "due": interview.scheduled_at, "overdue": False, "application_id": a.id,
                      "job_title": a.job_title, "company_name": a.company_name, "status": a.status,
                      "text": interview.interview_type.value, "interview_id": interview.id})

    def key(item: dict[str, Any]) -> datetime:
        due = item["due"]
        return due if isinstance(due, datetime) else datetime.combine(due, datetime.min.time(), ZoneInfo("UTC"))

    return sorted(items, key=key)


# ---------------- Statistics ----------------
async def statistics(db: AsyncSession, user: User) -> dict[str, Any]:
    counts = dict(
        (await db.execute(
            select(Application.status, func.count()).where(Application.user_id == user.id).group_by(Application.status)
        )).all()
    )
    by_status = {s.value: int(counts.get(s, 0)) for s in ApplicationStatus}
    total = sum(by_status.values())
    saved = await db.scalar(select(func.count()).select_from(SavedJob).where(SavedJob.user_id == user.id)) or 0
    now = utcnow()
    upcoming_interviews = await db.scalar(
        select(func.count()).select_from(Interview).join(Application, Application.id == Interview.application_id)
        .where(Application.user_id == user.id, Interview.scheduled_at >= now,
               Interview.outcome.in_([InterviewOutcome.PENDING, InterviewOutcome.RESCHEDULED]))
    ) or 0
    upcoming_deadlines = await db.scalar(
        select(func.count()).select_from(Application).where(
            Application.user_id == user.id, Application.status.notin_(list(TERMINAL)),
            Application.deadline.between(now.date(), now.date() + timedelta(days=7)))
    ) or 0

    # Stage reached = current status or any status in the candidate history.
    reached: dict[uuid.UUID, set[str]] = {}
    hist = await db.execute(
        select(Application.id, Application.status, ApplicationStatusHistory.new_status)
        .join(ApplicationStatusHistory, and_(ApplicationStatusHistory.application_id == Application.id,
                                             ApplicationStatusHistory.pipeline == "candidate"), isouter=True)
        .where(Application.user_id == user.id)
    )
    for app_id, status, new_status in hist.all():
        bucket = reached.setdefault(app_id, {status.value})
        if new_status:
            bucket.add(new_status)
    pre = {s.value for s in PRE_APPLICATION}
    submitted = [s for s in reached.values() if s - pre]
    interviewed = [s for s in submitted if s & {x.value for x in INTERVIEW_STAGE}]
    offered = [s for s in submitted if s & {x.value for x in OFFER_STAGE}]

    months: Counter[str] = Counter()
    dates = await db.scalars(select(Application.applied_at).where(Application.user_id == user.id, Application.applied_at.is_not(None),
                                                                   Application.applied_at >= now - timedelta(days=366)))
    for d in dates:
        months[d.strftime("%Y-%m")] += 1

    return {
        "total_applications": total,
        "saved_jobs": int(saved),
        "by_status": by_status,
        "applied_count": len(submitted),
        "shortlisted_count": by_status["shortlisted"],
        "interviews_scheduled": by_status["interview_scheduled"],
        "offers_received": by_status["offer_received"],
        "rejected_count": by_status["rejected"],
        "hired_count": by_status["hired"],
        "withdrawn_count": by_status["withdrawn"],
        "upcoming_interviews": int(upcoming_interviews),
        "upcoming_deadlines": int(upcoming_deadlines),
        "applications_per_month": [{"month": m, "count": c} for m, c in sorted(months.items())],
        "interview_conversion_rate": round(len(interviewed) / len(submitted) * 100, 1) if submitted else None,
        "offer_conversion_rate": round(len(offered) / len(submitted) * 100, 1) if submitted else None,
        "definitions": {
            "applied_count": "Applications that progressed beyond 'interested'/'saved' at any point.",
            "interview_conversion_rate": "Percentage of applied_count that ever reached an interview stage "
                                         "(interview_scheduled, interview_completed, assessment, offer_received, hired).",
            "offer_conversion_rate": "Percentage of applied_count that ever reached offer_received or hired.",
            "upcoming_deadlines": "Active applications with a deadline in the next 7 days.",
            "by_status": "Current status counts.",
        },
    }


def interview_out(interview: Interview) -> dict[str, Any]:
    data = {c: getattr(interview, c) for c in (
        "id", "application_id", "interview_type", "interview_round", "scheduled_at", "timezone", "duration_minutes",
        "location_or_meeting_link", "interviewer_name", "notes", "outcome", "created_at", "updated_at")}
    data["local_time"] = interview.scheduled_at.astimezone(ZoneInfo(interview.timezone)).isoformat()
    return data
