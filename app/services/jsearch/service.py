"""Job search business logic: caching, provider calls, persistence, details refresh."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AppError
from app.models.base import utcnow
from app.models.job import Job, JobSource
from app.services.jsearch import cache
from app.services.jsearch.client import JSearchClient
from app.services.jsearch.filters import JobSearchParams
from app.services.jsearch.normalizer import normalize_job

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    jobs: list[Job]
    next_cursor: str | None
    cached: bool
    stale: bool = False
    country: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


async def upsert_jobs(db: AsyncSession, normalized: list[dict[str, Any]]) -> list[Job]:
    """Insert new listings and refresh existing ones, preserving internal ids.

    Identity is (source, external_id), enforced by a unique constraint. Concurrent
    inserts of the same listing are resolved by retrying once after a conflict.
    """
    if not normalized:
        return []
    # Deduplicate within the page (providers sometimes repeat a listing).
    unique: dict[tuple[JobSource, str], dict[str, Any]] = {}
    for item in normalized:
        unique.setdefault((item["source"], item["external_id"]), item)

    for attempt in range(2):
        keys = list(unique)
        existing = {
            (j.source, j.external_id): j
            for j in await db.scalars(
                select(Job).where(tuple_(Job.source, Job.external_id).in_(keys))
            )
        }
        now = utcnow()
        jobs: dict[tuple[JobSource, str], Job] = {}
        for key, values in unique.items():
            job = existing.get(key)
            if job is None:
                job = Job(**values, last_seen_at=now)
                db.add(job)
            else:
                for name, value in values.items():
                    setattr(job, name, value)
                job.last_seen_at = now
                job.unavailable_since = None
                job.derived_requirements = None  # recompute for recommendations
            jobs[key] = job
        try:
            await db.commit()
            break
        except IntegrityError:
            await db.rollback()
            if attempt == 1:
                raise
            logger.info("Concurrent job insert detected; retrying upsert")
    order = [(item["source"], item["external_id"]) for item in normalized]
    seen: set[tuple[JobSource, str]] = set()
    result = []
    for key in order:
        if key not in seen:
            seen.add(key)
            result.append(jobs[key])
    return result


async def _load_jobs(db: AsyncSession, ids: list[str]) -> list[Job]:
    uuids = [uuid.UUID(i) for i in ids]
    if not uuids:
        return []
    rows = {j.id: j for j in await db.scalars(select(Job).where(Job.id.in_(uuids)))}
    return [rows[i] for i in uuids if i in rows]


def _apply_local_filters(jobs: list[Job], params: JobSearchParams) -> list[Job]:
    result = jobs
    if params.company:
        needle = params.company.lower()
        result = [j for j in result if j.company_name and needle in j.company_name.lower()]
    if params.min_salary is not None:
        result = [j for j in result if (j.max_salary or j.min_salary) and (j.max_salary or j.min_salary) >= params.min_salary]
    return result


async def search_jobs(
    db: AsyncSession, params: JobSearchParams, client: JSearchClient | None = None
) -> SearchResult:
    settings = get_settings()
    client = client or JSearchClient()
    key = params.cache_key()
    provider_params = params.provider_params()

    entry = await cache.get_entry(db, key)
    if entry and cache.is_fresh(entry):
        await cache.record_hit(db, entry)
        jobs = await _load_jobs(db, entry.job_ids)
        return SearchResult(_apply_local_filters(jobs, params), entry.next_cursor, cached=True, country=params.effective_country)

    async with cache.coalesce(key):
        # Another request may have filled the cache while we waited.
        db.expire_all()
        entry = await cache.get_entry(db, key)
        if entry and cache.is_fresh(entry):
            jobs = await _load_jobs(db, entry.job_ids)
            return SearchResult(_apply_local_filters(jobs, params), entry.next_cursor, cached=True, country=params.effective_country)
        try:
            page = await client.search(provider_params, params.cursor)
        except AppError as exc:
            if entry is not None and exc.code not in {"job_search_not_configured", "job_search_invalid_request"}:
                logger.warning("Serving stale cached results (%s)", exc.code)
                jobs = await _load_jobs(db, entry.job_ids)
                return SearchResult(
                    _apply_local_filters(jobs, params), entry.next_cursor, cached=True, stale=True,
                    country=params.effective_country, meta={"stale_reason": exc.code},
                )
            raise
        normalized = [n for n in (normalize_job(raw) for raw in page.jobs) if n]
        skipped = len(page.jobs) - len(normalized)
        jobs = await upsert_jobs(db, normalized)
        await cache.store(
            db, key, {"provider": provider_params, "cursor": params.cursor},
            [j.id for j in jobs], page.next_cursor, settings.JSEARCH_CACHE_TTL_SECONDS,
        )
        return SearchResult(
            _apply_local_filters(jobs, params), page.next_cursor, cached=False,
            country=params.effective_country, meta={"skipped_invalid": skipped},
        )


async def get_job(db: AsyncSession, job_id: uuid.UUID) -> Job:
    """A job visible to job seekers (hidden SharkHire listings behave as missing)."""
    from app.services.visibility import get_visible_job

    return await get_visible_job(db, job_id)


async def get_job_details(
    db: AsyncSession, job_id: uuid.UUID, client: JSearchClient | None = None, *, refresh: bool | None = None
) -> tuple[Job, dict[str, Any]]:
    """Return a stored job, refreshing external listings when their details are stale.

    A provider failure never breaks the endpoint: the stored copy is returned with
    ``details_stale: true``.
    """
    job = await get_job(db, job_id)
    meta: dict[str, Any] = {"details_refreshed": False, "details_stale": False}
    if job.source != JobSource.JSEARCH or not job.external_id or job.external_id.startswith("h:"):
        return job, meta
    settings = get_settings()
    last = job.details_refreshed_at or job.last_seen_at
    due = last is None or last < utcnow() - timedelta(seconds=settings.JSEARCH_DETAILS_REFRESH_SECONDS)
    if refresh is False or not (due or refresh):
        return job, meta
    client = client or JSearchClient()
    try:
        raw = await client.job_details(job.external_id, job.country)
    except AppError as exc:
        logger.info("Job details refresh failed (%s); serving stored copy", exc.code)
        meta["details_stale"] = True
        return job, meta
    if raw is None:
        if job.unavailable_since is None:
            job.unavailable_since = utcnow()
        job.details_refreshed_at = utcnow()
        await db.commit()
        meta["provider_listing_found"] = False
        return job, meta
    normalized = normalize_job(raw)
    if normalized:
        for name, value in normalized.items():
            if name in {"source", "external_id"}:
                continue
            if value in (None, [], {}) and getattr(job, name) not in (None, [], {}):
                continue  # don't erase richer stored data with an emptier response
            setattr(job, name, value)
    job.details_refreshed_at = utcnow()
    job.unavailable_since = None
    job.derived_requirements = None
    await db.commit()
    meta["details_refreshed"] = True
    return job, meta


async def suggestions(db: AsyncSession, prefix: str, limit: int = 10) -> list[dict[str, Any]]:
    """Job-title suggestions from titles already stored in SharkHire (real data only)."""
    pattern = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    word_pattern = "% " + pattern
    rows = await db.execute(
        select(Job.title, func.count().label("n"))
        .where(or_(Job.title.ilike(pattern, escape="\\"), Job.title.ilike(word_pattern, escape="\\")))
        .group_by(Job.title)
        .order_by(func.count().desc(), Job.title)
        .limit(limit)
    )
    return [{"title": title, "listings": n} for title, n in rows.all()]
