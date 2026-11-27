"""Job search endpoints (JSearch + Techmap via RapidAPI).

Access policy: searching, details and suggestions require an authenticated user
(protects the RapidAPI subscription from anonymous abuse). The filter catalogue
is public.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import ValidationError

from app.api.deps import AppUser, DBSession
from app.core.config import get_settings
from app.core.exceptions import ValidationAppError
from app.core.rate_limit import enforce_user_limit, rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok, paginate
from app.schemas.job import JobDetailOut, JobSearchOut, Suggestion, job_detail, job_summary
from app.services import job_search
from app.services.jsearch import service as job_service
from app.services.jsearch.client import JSearchClient
from app.services.jsearch.filters import (
    FILTER_CATALOG,
    DatePosted,
    ExperienceRequirement,
    JobSearchParams,
    SearchEmploymentType,
)
from app.services.techmap.client import TechmapClient, get_techmap_client

router = APIRouter(prefix="/jobs", tags=["Jobs"], responses=ERROR_RESPONSES)


def get_jsearch_client() -> JSearchClient:
    return JSearchClient()


Client = Annotated[JSearchClient, Depends(get_jsearch_client)]
Techmap = Annotated[TechmapClient, Depends(get_techmap_client)]


@router.get(
    "/search",
    response_model=ApiResponse[JobSearchOut],
    dependencies=[Depends(rate_limit("job_search_ip", "RATE_LIMIT_SEARCH_PER_MINUTE"))],
    responses={502: {"model": ApiResponse[None]}, 503: {"model": ApiResponse[None]}},
    summary="Search jobs (Pakistan, international, remote)",
)
async def search_jobs(
    user: AppUser,
    db: DBSession,
    client: Client,
    techmap: Techmap,
    query: Annotated[str, Query(min_length=2, max_length=200, description="Job title or keywords, e.g. 'Python Developer'")],
    location: Annotated[str | None, Query(max_length=100, description="City/region, e.g. Karachi, London")] = None,
    country: Annotated[str | None, Query(description="ISO-2 code (pk, us, gb, ...). Inferred from location when omitted")] = None,
    company: Annotated[str | None, Query(max_length=100, description="Employer name (applied locally)")] = None,
    remote_jobs_only: bool = False,
    employment_types: Annotated[list[SearchEmploymentType] | None, Query()] = None,
    experience: Annotated[list[ExperienceRequirement] | None, Query()] = None,
    date_posted: DatePosted = DatePosted.ALL,
    radius: Annotated[int | None, Query(ge=1, le=500, description="km")] = None,
    language: Annotated[str | None, Query(description="ISO 639-1")] = None,
    exclude_job_publishers: Annotated[list[str] | None, Query()] = None,
    min_salary: Annotated[float | None, Query(ge=0, description="Applied locally")] = None,
    num_pages: Annotated[int, Query(ge=1, description="Provider pages (10 results each)")] = 1,
    cursor: Annotated[str | None, Query(max_length=1024, description="next_cursor from the previous response")] = None,
) -> dict[str, Any]:
    """Searches JSearch and Techmap together and returns one normalised, de-duplicated list.

    Each item's `source` is `jsearch` or `techmap`; `pagination.providers` reports
    per-provider status and counts. If one provider fails, the other's jobs are
    still returned. Techmap is skipped for filters it cannot apply (experience,
    radius, publisher exclusion, contractor/intern types).

    Pagination is cursor-based: pass `pagination.next_cursor` to get the next page;
    `has_more` is false when the provider returns no cursor. `total_results` is
    always null because the provider does not report reliable totals.
    Results are cached (`JSEARCH_CACHE_TTL_SECONDS`); `cached` and `stale` flags
    tell the client whether data came from cache and whether it is past its TTL.
    """
    try:
        params = JobSearchParams(
            query=query, location=location, country=country, company=company,
            remote_jobs_only=remote_jobs_only, employment_types=employment_types or [],
            experience=experience or [], date_posted=date_posted, radius=radius, language=language,
            exclude_job_publishers=exclude_job_publishers or [], min_salary=min_salary,
            num_pages=num_pages, cursor=cursor,
        )
    except ValidationError as exc:
        details = [{"field": ".".join(map(str, e["loc"])), "message": e["msg"]} for e in exc.errors()]
        raise ValidationAppError(details=details) from None
    enforce_user_limit("job_search", user.id, get_settings().RATE_LIMIT_SEARCH_PER_MINUTE)
    result = await job_search.search(db, params, client, techmap)
    items = [job_summary(j) for j in result.jobs]
    message = "No jobs found for this search." if not items else "Jobs retrieved successfully"
    if result.stale:
        message = "Live search is temporarily unavailable; showing previously cached results."
    return ok(
        {
            "items": items,
            "pagination": {
                "page_size": len(items),
                "has_more": result.next_cursor is not None,
                "next_cursor": result.next_cursor,
                "total_results": None,
                "source": result.source,
                "cached": result.cached,
                "stale": result.stale,
                "country": result.country,
                "providers": {name: status.as_dict() for name, status in result.providers.items()},
            },
        },
        message,
    )


@router.get("/filters", response_model=ApiResponse[dict[str, Any]], summary="Supported search filters")
async def filters() -> dict[str, Any]:
    """Public catalogue of filter values, and which filters JSearch applies vs. the backend."""
    return ok({**FILTER_CATALOG, "max_num_pages": get_settings().JSEARCH_MAX_NUM_PAGES})


@router.get("/suggestions", response_model=ApiResponse[list[Suggestion]], summary="Job title suggestions")
async def job_suggestions(
    user: AppUser,
    db: DBSession,
    q: Annotated[str, Query(min_length=2, max_length=60, description="Prefix typed by the user")],
    limit: Annotated[int, Query(ge=1, le=20)] = 10,
) -> dict[str, Any]:
    """Suggestions come only from job titles already stored in SharkHire (no invented entries).
    An empty list is returned when nothing matches."""
    return ok(await job_service.suggestions(db, q.strip(), limit))


@router.get("/sharkhire", response_model=ApiResponse[dict[str, Any]], summary="Jobs posted on SharkHire")
async def sharkhire_jobs(
    user: AppUser,
    db: DBSession,
    q: Annotated[str | None, Query(max_length=100, description="Title or company")] = None,
    location: Annotated[str | None, Query(max_length=100)] = None,
    remote: bool | None = None,
    employment_type: SearchEmploymentType | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Published listings from verified SharkHire employers (no RapidAPI usage).
    Apply with POST /applications using the job id and one of your CVs."""
    from sqlalchemy import func, or_, select

    from app.models.job import Job
    from app.services.visibility import internal_visible_filter

    def like(v: str) -> str:
        return "%" + v.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"

    stmt = select(Job).where(internal_visible_filter())
    if q:
        stmt = stmt.where(or_(Job.title.ilike(like(q), escape="\\"), Job.company_name.ilike(like(q), escape="\\")))
    if location:
        stmt = stmt.where(or_(Job.location.ilike(like(location), escape="\\"), Job.city.ilike(like(location), escape="\\")))
    if remote is not None:
        stmt = stmt.where(Job.is_remote.is_(remote))
    if employment_type:
        stmt = stmt.where(Job.employment_type == employment_type.value)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await db.scalars(stmt.order_by(Job.published_at.desc(), Job.id).offset((page - 1) * page_size).limit(page_size))
    return ok(paginate([job_summary(j) for j in rows], page, page_size, int(total)))


@router.get("/{job_id}", response_model=ApiResponse[JobDetailOut], summary="Job details")
async def get_job(
    job_id: uuid.UUID,
    user: AppUser,
    db: DBSession,
    client: Client,
    refresh: Annotated[bool | None, Query(description="Force (true) or skip (false) a provider refresh")] = None,
) -> dict[str, Any]:
    """Returns a stored job. External listings are refreshed from JSearch when older
    than `JSEARCH_DETAILS_REFRESH_SECONDS`; if the refresh fails the stored copy is
    returned with `details_stale: true`. `listing_unavailable` is true when the
    provider no longer returns the listing."""
    job, meta = await job_service.get_job_details(db, job_id, client, refresh=refresh)
    return ok({"job": job_detail(job), "details_refreshed": meta["details_refreshed"], "details_stale": meta["details_stale"]})
