"""Saved jobs (bookmarks). Saving a job never creates an application."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Body, Depends, Query

from app.api.deps import CurrentUser, DBSession
from app.core.rate_limit import rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok, paginate
from app.schemas.application import SavedCheckOut, SavedJobOut, SaveJobRequest
from app.schemas.job import job_summary
from app.services import job_management as jm

router = APIRouter(prefix="/saved-jobs", tags=["Saved Jobs"], responses=ERROR_RESPONSES)


def _out(saved: Any, job: Any) -> dict[str, Any]:
    return {"id": saved.id, "job": job_summary(job), "match_percentage": saved.match_percentage,
            "notes": saved.notes, "saved_at": saved.created_at}


@router.get("", response_model=ApiResponse[dict[str, Any]], summary="List my saved jobs")
async def list_saved(
    user: CurrentUser,
    db: DBSession,
    q: Annotated[str | None, Query(max_length=100, description="Search title or company")] = None,
    company: Annotated[str | None, Query(max_length=100)] = None,
    location: Annotated[str | None, Query(max_length=100)] = None,
    remote: bool | None = None,
    employment_type: Annotated[Literal["FULLTIME", "PARTTIME", "CONTRACTOR", "INTERN"] | None, Query()] = None,
    sort: Literal["saved_at", "posted_at", "title", "match"] = "saved_at",
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    rows, total = await jm.list_saved(db, user, q=q, company=company, location=location, remote=remote,
                                      employment_type=employment_type, sort=sort, page=page, page_size=page_size)
    items = [SavedJobOut(**_out(s, j)).model_dump(mode="json") for s, j in rows]
    return ok(paginate(items, page, page_size, total))


@router.get("/check/{job_id}", response_model=ApiResponse[SavedCheckOut], summary="Is this job saved?")
async def check_saved(job_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    saved = await jm.is_saved(db, user, job_id)
    return ok({"job_id": job_id, "is_saved": saved is not None, "saved_job_id": saved.id if saved else None})


@router.post(
    "/{job_id}",
    response_model=ApiResponse[SavedJobOut],
    status_code=201,
    responses={409: {"model": ApiResponse[None], "description": "Already saved"}},
    dependencies=[Depends(rate_limit("write", "RATE_LIMIT_WRITE_PER_MINUTE"))],
    summary="Save a job",
)
async def save(
    job_id: uuid.UUID, user: CurrentUser, db: DBSession,
    body: Annotated[SaveJobRequest | None, Body()] = None,
) -> dict[str, Any]:
    """Stores a bookmark. If a recommendation exists for this job, its match
    percentage is kept as a snapshot."""
    saved, job = await jm.save_job(db, user, job_id, body.notes if body else None)
    return ok(_out(saved, job), "Job saved")


@router.get("/{job_id}", response_model=ApiResponse[SavedJobOut], summary="Get one saved job")
async def get_saved(job_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    saved, job = await jm.get_saved(db, user, job_id)
    return ok(_out(saved, job))


@router.delete("/{job_id}", response_model=ApiResponse[None], summary="Unsave a job")
async def unsave(job_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    await jm.unsave(db, user, job_id)
    return ok(None, "Job removed from saved jobs")
