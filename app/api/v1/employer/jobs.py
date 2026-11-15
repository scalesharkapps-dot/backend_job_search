"""Employer job postings, applicants, CV access and statistics (owner-scoped)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query

from app.api.deps import DBSession
from app.core.permissions import Employer
from app.core.rate_limit import rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok, paginate
from app.models.application import EmployerReviewStatus
from app.models.job import PostingStatus
from app.schemas.employer import (
    EmployerApplicationOut,
    EmployerJobCreate,
    EmployerJobIn,
    EmployerJobOut,
    EmployerStatisticsOut,
    EmployerStatusUpdate,
    JobStatusChange,
)
from app.schemas.profile import CVDownloadOut
from app.services import employer_service as es
from app.services.storage import StorageBackend, get_storage

router = APIRouter(prefix="/employer", tags=["Employer Jobs & Applicants"], responses=ERROR_RESPONSES)
WriteLimit = Depends(rate_limit("write", "RATE_LIMIT_WRITE_PER_MINUTE"))


async def _job(db: Any, job: Any) -> dict[str, Any]:
    counts = await es.applicant_counts(db, [job.id])
    return es.job_out(job, counts.get(job.id, 0))


@router.post("/jobs", response_model=ApiResponse[EmployerJobOut], status_code=201, dependencies=[WriteLimit],
             summary="Create a job posting")
async def create_job(body: EmployerJobCreate, ctx: Employer, db: DBSession) -> dict[str, Any]:
    """Creates a draft. With `submit_for_review: true` (verified employers only) the
    job goes to `pending_review` (or straight to `published` when moderation is disabled)."""
    job = await es.create_job(db, ctx.user, ctx.employer, body)
    return ok(await _job(db, job), "Job created")


@router.get("/jobs", response_model=ApiResponse[dict[str, Any]], summary="My job postings")
async def list_jobs(
    ctx: Employer, db: DBSession, status: PostingStatus | None = None,
    page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    jobs, total = await es.list_jobs(db, ctx.employer, status, page, page_size)
    counts = await es.applicant_counts(db, [j.id for j in jobs])
    items = [EmployerJobOut(**es.job_out(j, counts.get(j.id, 0))).model_dump(mode="json") for j in jobs]
    return ok(paginate(items, page, page_size, total))


@router.get("/jobs/{job_id}", response_model=ApiResponse[EmployerJobOut], summary="One of my job postings")
async def get_job(job_id: uuid.UUID, ctx: Employer, db: DBSession) -> dict[str, Any]:
    return ok(await _job(db, await es.get_own_job(db, ctx.employer, job_id)))


@router.put("/jobs/{job_id}", response_model=ApiResponse[EmployerJobOut], dependencies=[WriteLimit],
            summary="Update a job posting")
async def update_job(job_id: uuid.UUID, body: EmployerJobIn, ctx: Employer, db: DBSession) -> dict[str, Any]:
    """Editing a published job sends it back to `pending_review` when moderation is enabled."""
    return ok(await _job(db, await es.update_job(db, ctx.user, ctx.employer, job_id, body)), "Job updated")


@router.patch("/jobs/{job_id}/status", response_model=ApiResponse[EmployerJobOut], dependencies=[WriteLimit],
              summary="Change job posting status")
async def change_status(job_id: uuid.UUID, body: JobStatusChange, ctx: Employer, db: DBSession) -> dict[str, Any]:
    """Allowed employer transitions:
    draft → pending_review | published (only if moderation disabled) | archived;
    pending_review → draft; rejected → draft | pending_review | archived;
    published → paused | closed; paused → published | closed; closed → archived;
    expired → pending_review | closed | archived. `suspended` and approval are admin-only."""
    job = await es.change_job_status(db, ctx.user, ctx.employer, job_id, body.status, body.reason)
    return ok(await _job(db, job), "Job status updated")


@router.delete("/jobs/{job_id}", response_model=ApiResponse[dict[str, str]], summary="Delete or archive a job")
async def delete_job(job_id: uuid.UUID, ctx: Employer, db: DBSession) -> dict[str, Any]:
    """Drafts without applications are deleted; all other jobs are archived (retention policy)."""
    result = await es.delete_job(db, ctx.user, ctx.employer, job_id)
    return ok({"result": result}, f"Job {result}")


@router.get("/jobs/{job_id}/applicants", response_model=ApiResponse[dict[str, Any]], summary="Applicants for a job")
async def job_applicants(
    job_id: uuid.UUID, ctx: Employer, db: DBSession, status: EmployerReviewStatus | None = None,
    sort: Literal["newest", "oldest"] = "newest",
    page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    items, total = await es.list_applicants(db, ctx.employer, job_id=job_id, status=status, sort=sort, page=page, page_size=page_size)
    return ok(paginate([EmployerApplicationOut(**i).model_dump(mode="json") for i in items], page, page_size, total))


@router.get("/applications", response_model=ApiResponse[dict[str, Any]], summary="All applicants to my jobs")
async def all_applicants(
    ctx: Employer, db: DBSession, job_id: uuid.UUID | None = None, status: EmployerReviewStatus | None = None,
    sort: Literal["newest", "oldest"] = "newest",
    page: Annotated[int, Query(ge=1)] = 1, page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    items, total = await es.list_applicants(db, ctx.employer, job_id=job_id, status=status, sort=sort, page=page, page_size=page_size)
    return ok(paginate([EmployerApplicationOut(**i).model_dump(mode="json") for i in items], page, page_size, total))


@router.get("/applications/{application_id}", response_model=ApiResponse[EmployerApplicationOut], summary="One applicant")
async def get_applicant(application_id: uuid.UUID, ctx: Employer, db: DBSession) -> dict[str, Any]:
    return ok(await es.applicant_out(db, await es.get_applicant(db, ctx.employer, application_id)))


@router.patch("/applications/{application_id}/status", response_model=ApiResponse[EmployerApplicationOut],
              dependencies=[WriteLimit], summary="Update employer review status")
async def update_applicant(application_id: uuid.UUID, body: EmployerStatusUpdate, ctx: Employer, db: DBSession) -> dict[str, Any]:
    """Updates the employer's pipeline only; the candidate's personal tracker,
    notes and reminders are never modified."""
    application = await es.update_applicant_status(db, ctx.user, ctx.employer, application_id, body.status, body.notes)
    return ok(await es.applicant_out(db, application), "Applicant status updated")


@router.get("/applications/{application_id}/cv", response_model=ApiResponse[CVDownloadOut], summary="Applicant CV (signed URL)")
async def applicant_cv(application_id: uuid.UUID, ctx: Employer, db: DBSession,
                       storage: Annotated[StorageBackend, Depends(get_storage)]) -> dict[str, Any]:
    """Short-lived signed URL for the CV attached to this application. Access is audited."""
    return ok(await es.applicant_cv_url(db, ctx.user, ctx.employer, storage, application_id))


@router.get("/statistics", response_model=ApiResponse[EmployerStatisticsOut], summary="My hiring statistics")
async def statistics(ctx: Employer, db: DBSession) -> dict[str, Any]:
    return ok(await es.statistics(db, ctx.employer))
