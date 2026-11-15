"""Application tracking, status history, interviews, reminders and statistics."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import CurrentUser, DBSession
from app.core.rate_limit import rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok, paginate
from app.models.application import ApplicationSource, ApplicationStatus
from app.models.job import Job
from app.schemas.application import (
    ApplicationCreate,
    ApplicationDetailOut,
    ApplicationOut,
    ApplicationUpdate,
    InterviewCreate,
    InterviewOut,
    InterviewUpdate,
    ReminderOut,
    StatisticsOut,
    StatusHistoryOut,
    StatusUpdate,
    UpcomingInterviewOut,
)
from app.schemas.job import job_summary
from app.services import job_management as jm

router = APIRouter(prefix="/applications", tags=["Applications"], responses=ERROR_RESPONSES)
interviews_router = APIRouter(prefix="/interviews", tags=["Interviews"], responses=ERROR_RESPONSES)
WriteLimit = Depends(rate_limit("write", "RATE_LIMIT_WRITE_PER_MINUTE"))


async def _detail(db: Any, application: Any) -> dict[str, Any]:
    data = ApplicationOut.model_validate(application).model_dump()
    data["history"] = [StatusHistoryOut.model_validate(h) for h in await jm.application_history(db, application)]
    data["interviews"] = [jm.interview_out(i) for i in await jm.application_interviews(db, application)]
    job = await db.get(Job, application.job_id) if application.job_id else None
    data["job"] = job_summary(job) if job else None
    return data


@router.get("/statistics", response_model=ApiResponse[StatisticsOut], summary="My application statistics")
async def stats(user: CurrentUser, db: DBSession) -> dict[str, Any]:
    """Computed from your records only. See `definitions` for each metric's denominator."""
    return ok(await jm.statistics(db, user))


@router.get("/reminders", response_model=ApiResponse[list[ReminderOut]], summary="Upcoming deadlines, follow-ups and interviews")
async def reminders(
    user: CurrentUser, db: DBSession, days: Annotated[int, Query(ge=1, le=90)] = 14,
) -> dict[str, Any]:
    """Returns reminder data for the app to display or schedule locally. The backend
    does not send push notifications from this endpoint. Overdue items from the last
    7 days are included with `overdue: true`."""
    return ok(await jm.reminders(db, user, days))


@router.post("", response_model=ApiResponse[ApplicationDetailOut], status_code=201, dependencies=[WriteLimit],
             responses={409: {"model": ApiResponse[None]}}, summary="Create an application record")
async def create(body: ApplicationCreate, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    """Records an application you made. For external (JSearch) jobs SharkHire does
    NOT submit anything to the employer — open `job_apply_link` in the app and
    record the application here."""
    application = await jm.create_application(db, user, body)
    return ok(await _detail(db, application), "Application recorded")


@router.get("", response_model=ApiResponse[dict[str, Any]], summary="List my applications")
async def list_applications(
    user: CurrentUser,
    db: DBSession,
    status: Annotated[list[ApplicationStatus] | None, Query()] = None,
    company: Annotated[str | None, Query(max_length=100)] = None,
    title: Annotated[str | None, Query(max_length=100)] = None,
    location: Annotated[str | None, Query(max_length=100)] = None,
    source: ApplicationSource | None = None,
    employment_type: Annotated[str | None, Query(max_length=32)] = None,
    applied_from: date | None = None,
    applied_to: date | None = None,
    deadline_before: date | None = None,
    follow_up_before: date | None = None,
    interview_from: date | None = None,
    interview_to: date | None = None,
    sort: Literal["updated_at", "created_at", "applied_at", "deadline", "company"] = "updated_at",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    filters = {k: v for k, v in locals().items() if k in {
        "status", "company", "title", "location", "source", "employment_type", "applied_from", "applied_to",
        "deadline_before", "follow_up_before", "interview_from", "interview_to"} and v not in (None, [])}
    rows, total = await jm.list_applications(db, user, filters=filters, sort=sort, page=page, page_size=page_size)
    items = [ApplicationOut.model_validate(a).model_dump(mode="json") for a in rows]
    return ok(paginate(items, page, page_size, total))


@router.get("/{application_id}", response_model=ApiResponse[ApplicationDetailOut], summary="Application details")
async def get_one(application_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    """Includes the full status history and interviews."""
    return ok(await _detail(db, await jm.get_application(db, user, application_id)))


@router.put("/{application_id}", response_model=ApiResponse[ApplicationDetailOut], dependencies=[WriteLimit],
            summary="Update application details")
async def update(application_id: uuid.UUID, body: ApplicationUpdate, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    """Updates the provided fields (notes, deadline, follow-up, reminder...). Use
    PATCH /status to change the status so history is preserved."""
    application = await jm.update_application(db, user, application_id, body)
    return ok(await _detail(db, application), "Application updated")


@router.patch("/{application_id}/status", response_model=ApiResponse[ApplicationDetailOut], dependencies=[WriteLimit],
              summary="Change application status")
async def change_status(application_id: uuid.UUID, body: StatusUpdate, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    application = await jm.change_status(db, user, application_id, body.status, body.notes)
    return ok(await _detail(db, application), "Status updated")


@router.get("/{application_id}/history", response_model=ApiResponse[list[StatusHistoryOut]], summary="Status history")
async def history(application_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    application = await jm.get_application(db, user, application_id)
    return ok([StatusHistoryOut.model_validate(h) for h in await jm.application_history(db, application)])


@router.delete("/{application_id}", response_model=ApiResponse[None], summary="Delete an application record")
async def delete(application_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    """Permanently deletes your tracking record, its history and interviews."""
    await jm.delete_application(db, user, application_id)
    return ok(None, "Application deleted")


@router.post("/{application_id}/interviews", response_model=ApiResponse[InterviewOut], status_code=201,
             dependencies=[WriteLimit], summary="Record an interview")
async def add_interview(application_id: uuid.UUID, body: InterviewCreate, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    """Stores interview details. Nothing is scheduled with external calendars."""
    return ok(jm.interview_out(await jm.add_interview(db, user, application_id, body)), "Interview recorded")


@router.get("/{application_id}/interviews", response_model=ApiResponse[list[InterviewOut]], summary="List interviews")
async def list_interviews(application_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    application = await jm.get_application(db, user, application_id)
    return ok([jm.interview_out(i) for i in await jm.application_interviews(db, application)])


@interviews_router.get("/upcoming", response_model=ApiResponse[list[UpcomingInterviewOut]], summary="Upcoming interviews")
async def upcoming(user: CurrentUser, db: DBSession, days: Annotated[int, Query(ge=1, le=180)] = 30) -> dict[str, Any]:
    rows = await jm.upcoming_interviews(db, user, days)
    return ok([{**jm.interview_out(i), "job_title": a.job_title, "company_name": a.company_name} for i, a in rows])


@interviews_router.put("/{interview_id}", response_model=ApiResponse[InterviewOut], dependencies=[WriteLimit],
                       summary="Update an interview")
async def update_interview(interview_id: uuid.UUID, body: InterviewUpdate, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    return ok(jm.interview_out(await jm.update_interview(db, user, interview_id, body)), "Interview updated")


@interviews_router.delete("/{interview_id}", response_model=ApiResponse[None], summary="Delete an interview")
async def delete_interview(interview_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    await jm.delete_interview(db, user, interview_id)
    return ok(None, "Interview deleted")
