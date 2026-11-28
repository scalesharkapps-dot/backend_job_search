"""Unified job search: JSearch + Techmap, merged into one normalised result page.

Flow for one ``GET /api/v1/jobs/search`` page:

1. Decide which providers take part. JSearch always does (unchanged behaviour).
   Techmap joins when configured and every requested filter can be honoured by it
   (experience, radius and publisher exclusions are JSearch-only; with those
   filters Techmap is skipped instead of returning unfiltered jobs).
2. Techmap's cache is read first (no provider call when fresh). Then the existing
   JSearch search (its own cache, coalescing, stale fallback) and the Techmap
   HTTP request run concurrently. The Techmap HTTP call never touches the DB
   session, so the session is never used by two coroutines at once.
3. Techmap postings are normalised, stored (``source = techmap``) and cached.
4. Results are merged: Techmap postings that duplicate a JSearch listing
   (same title + company + country, compatible city, or the same apply URL) are
   dropped; the remaining lists are interleaved so both providers are visible.
5. One provider failing never hides the other's results. Only when no provider
   delivers anything is the JSearch error raised (the pre-existing contract).

Pagination: when Techmap takes part, ``next_cursor`` is an opaque composite
(``mx1.`` + base64 JSON) holding JSearch's cursor and Techmap's next page. A plain
(legacy) JSearch cursor is still accepted and continues JSearch only. Searches
without Techmap return exactly the JSearch cursor, as before.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

from app.core.config import get_settings
from app.core.exceptions import AppError, ValidationAppError
from app.models.base import utcnow
from app.models.job import Job
from app.services.jsearch import cache
from app.services.jsearch import service as jsearch_service
from app.services.jsearch.client import JSearchClient
from app.services.jsearch.filters import DatePosted, JobSearchParams, SearchEmploymentType
from app.services.locations import city_match_rank, city_variants, fold, is_country_name
from app.services.techmap.client import TechmapClient, TechmapError, TechmapPage
from app.services.techmap.normalizer import normalize_techmap_job

logger = logging.getLogger(__name__)

CURSOR_PREFIX = "mx1."
_DATE_WINDOW_DAYS = {DatePosted.TODAY: 1, DatePosted.THREE_DAYS: 3, DatePosted.WEEK: 7, DatePosted.MONTH: 30}
_TECHMAP_WORK_TYPES = {SearchEmploymentType.FULLTIME: "fulltime", SearchEmploymentType.PARTTIME: "parttime"}


@dataclass
class ProviderStatus:
    status: str  # live | cached | stale | error | skipped | not_configured | exhausted
    count: int = 0
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status, "count": self.count}
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass
class UnifiedResult:
    jobs: list[Job]
    next_cursor: str | None
    cached: bool
    stale: bool
    country: str | None
    source: str
    providers: dict[str, ProviderStatus] = field(default_factory=dict)


# --------------------------------------------------------------------------- cursor
@dataclass
class _CursorState:
    jsearch_cursor: str | None
    jsearch_active: bool
    techmap_page: int | None
    composite: bool


def _encode_cursor(jsearch_cursor: str | None, jsearch_active: bool, techmap_page: int | None) -> str | None:
    if not jsearch_active and techmap_page is None:
        return None
    payload = json.dumps({"j": jsearch_cursor, "ja": jsearch_active, "t": techmap_page}, separators=(",", ":"))
    return CURSOR_PREFIX + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None, techmap_on: bool) -> _CursorState:
    if not cursor:
        return _CursorState(None, True, 1 if techmap_on else None, composite=techmap_on)
    if not cursor.startswith(CURSOR_PREFIX):  # legacy JSearch cursor: continue JSearch only
        return _CursorState(cursor, True, None, composite=False)
    raw = cursor[len(CURSOR_PREFIX):]
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        j, ja, t = data.get("j"), data.get("ja"), data.get("t")
        if (j is not None and not isinstance(j, str)) or not isinstance(ja, bool) or (
            t is not None and (not isinstance(t, int) or isinstance(t, bool) or not 1 <= t <= 1000)
        ):
            raise ValueError
    except (ValueError, TypeError, AttributeError, binascii.Error, UnicodeDecodeError):
        raise ValidationAppError(
            "The pagination cursor is invalid. Start a new search.", code="invalid_cursor"
        ) from None
    return _CursorState(j, ja, t if techmap_on else None, composite=True)


# --------------------------------------------------------------------------- Techmap request
def techmap_unsupported_reason(params: JobSearchParams) -> str | None:
    """Filters Techmap cannot apply: skip it rather than return unfiltered jobs."""
    if params.experience:
        return "experience filter is JSearch-only"
    if params.radius:
        return "radius filter is JSearch-only"
    if params.exclude_job_publishers:
        return "publisher exclusion is JSearch-only"
    if params.employment_types and any(t not in _TECHMAP_WORK_TYPES for t in params.employment_types):
        return "employment type filter not supported by Techmap"
    return None


def _techmap_city(params: JobSearchParams) -> str | None:
    """``city`` (or the first part of ``location``), with every spelling Techmap uses; "," = OR."""
    place = params.city
    if not place and params.location:
        place = params.location.split(",")[0].strip()
    if not place or is_country_name(place):
        return None
    return ",".join(city_variants(place))


def techmap_query(params: JobSearchParams, page: int, today: date | None = None) -> dict[str, Any]:
    settings = get_settings()
    today = today or utcnow().date()
    days = _DATE_WINDOW_DAYS.get(params.date_posted, settings.TECHMAP_MAX_AGE_DAYS)
    words = [w for w in re.split(r"\s+", params.query.strip()) if w]
    query: dict[str, Any] = {
        # "+word" = must contain (Techmap boolean syntax): all words, any order.
        "title": " ".join(f"+{w}" for w in words) if len(words) > 1 else params.query.strip(),
        "countryCode": params.effective_country,
        "city": _techmap_city(params),
        "company": params.company,
        "language": params.language,
        "workPlace": "remote" if params.remote_jobs_only else None,
        "workType": ",".join(sorted({_TECHMAP_WORK_TYPES[t] for t in params.employment_types})) or None,
        "dateCreatedMin": (today - timedelta(days=days - 1 if days > 1 else 0)).isoformat(),
        "dateCreatedMax": today.isoformat(),
        "isDuplicate": "false",
        "format": "json",
        "page": page,
    }
    return {k: v for k, v in query.items() if v not in (None, "")}


def _techmap_cache_key(query: dict[str, Any]) -> str:
    material = {k: v for k, v in query.items() if k not in {"dateCreatedMin", "dateCreatedMax"}}
    material["provider"] = "techmap"
    material["days"] = query.get("dateCreatedMax", ""), query.get("dateCreatedMin", "")
    return hashlib.sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()


# --------------------------------------------------------------------------- de-duplication
_COMPANY_SUFFIXES = re.compile(r"\b(pvt|private|ltd|limited|llc|inc|incorporated|co|corp|corporation|gmbh|plc|company)\b")


def _company_key(name: str | None) -> str:
    return re.sub(r"\s+", " ", _COMPANY_SUFFIXES.sub(" ", fold(name))).strip()


def _url_key(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    return f"{host}{path}" if host and path else None


def is_duplicate(a: Job, b: Job) -> bool:
    url_a, url_b = _url_key(a.apply_url), _url_key(b.apply_url)
    if url_a and url_a == url_b:
        return True
    if fold(a.title) != fold(b.title) or not _company_key(a.company_name):
        return False
    if _company_key(a.company_name) != _company_key(b.company_name):
        return False
    if a.country and b.country and a.country != b.country:
        return False
    city_a, city_b = fold(a.city), fold(b.city)
    return not (city_a and city_b and city_a != city_b)


def merge_results(primary: list[Job], secondary: list[Job]) -> tuple[list[Job], int]:
    """Drop ``secondary`` jobs that duplicate ``primary`` ones, then interleave both lists."""
    kept: list[Job] = []
    removed = 0
    for job in secondary:
        if any(job.id == p.id or is_duplicate(p, job) for p in primary) or any(is_duplicate(k, job) for k in kept):
            removed += 1
            continue
        kept.append(job)
    merged: list[Job] = []
    for i in range(max(len(primary), len(kept))):
        if i < len(primary):
            merged.append(primary[i])
        if i < len(kept):
            merged.append(kept[i])
    return merged, removed


# --------------------------------------------------------------------------- search
async def search(
    db: Any,
    params: JobSearchParams,
    jsearch_client: JSearchClient | None = None,
    techmap_client: TechmapClient | None = None,
) -> UnifiedResult:
    settings = get_settings()
    techmap_client = techmap_client or TechmapClient()
    unsupported = techmap_unsupported_reason(params)
    techmap_on = techmap_client.configured and unsupported is None
    state = _decode_cursor(params.cursor, techmap_on)
    providers: dict[str, ProviderStatus] = {}
    if not techmap_client.configured:
        providers["techmap"] = ProviderStatus("not_configured")
    elif unsupported:
        providers["techmap"] = ProviderStatus("skipped", reason=unsupported)

    techmap_jobs: list[Job] = []
    techmap_cached_hit = False
    techmap_next: int | None = None
    techmap_query_params: dict[str, Any] | None = None
    techmap_key: str | None = None
    if state.techmap_page is not None:
        techmap_query_params = techmap_query(params, state.techmap_page)
        techmap_key = _techmap_cache_key(techmap_query_params)
        entry = await cache.get_entry(db, techmap_key)
        if entry and cache.is_fresh(entry):
            await cache.record_hit(db, entry)
            techmap_jobs = await jsearch_service._load_jobs(db, entry.job_ids)
            techmap_next = int(entry.next_cursor) if entry.next_cursor and entry.next_cursor.isdigit() else None
            techmap_cached_hit = True
    elif "techmap" not in providers:
        providers["techmap"] = ProviderStatus("exhausted")

    jsearch_params = params.model_copy(update={"cursor": state.jsearch_cursor})

    async def run_jsearch() -> jsearch_service.SearchResult | None:
        if not state.jsearch_active:
            return None
        return await jsearch_service.search_jobs(db, jsearch_params, jsearch_client)

    async def run_techmap() -> TechmapPage | None:
        if techmap_query_params is None or techmap_cached_hit:
            return None
        return await techmap_client.search(techmap_query_params)

    async with AsyncExitStack() as stack:
        if techmap_key and not techmap_cached_hit:
            await stack.enter_async_context(cache.coalesce(techmap_key))
        jsearch_outcome, techmap_outcome = await asyncio.gather(run_jsearch(), run_techmap(), return_exceptions=True)

        # ---- Techmap: store what it returned (sequential DB work)
        techmap_error: Exception | None = None
        if isinstance(techmap_outcome, TechmapPage):
            try:
                normalized = [n for n in (normalize_techmap_job(raw) for raw in techmap_outcome.jobs) if n]
                techmap_jobs = await jsearch_service.upsert_jobs(db, normalized)
                techmap_next = techmap_outcome.next_page
                assert techmap_key is not None and techmap_query_params is not None
                await cache.store(
                    db, techmap_key, {"provider": "techmap", "query": techmap_query_params},
                    [j.id for j in techmap_jobs], str(techmap_next) if techmap_next else None,
                    settings.TECHMAP_CACHE_TTL_SECONDS,
                )
            except Exception as exc:  # e.g. migration 0012 not applied yet: never break JSearch results
                await db.rollback()
                logger.exception("Storing Techmap results failed; continuing with JSearch")
                techmap_jobs, techmap_next = [], None
                techmap_error = TechmapError("Techmap results could not be stored.", code="techmap_store_failed")
                techmap_error.__cause__ = exc
        elif isinstance(techmap_outcome, BaseException):
            techmap_error = techmap_outcome
            if not isinstance(techmap_outcome, AppError):
                logger.exception("Techmap search failed unexpectedly", exc_info=techmap_outcome)
            else:
                logger.warning("Techmap search failed (%s); continuing with JSearch", techmap_outcome.code)

    if techmap_query_params is not None:
        techmap_jobs = jsearch_service._apply_local_filters(techmap_jobs, params)
        if techmap_error is not None:
            code = techmap_error.code if isinstance(techmap_error, AppError) else "techmap_unavailable"
            providers["techmap"] = ProviderStatus("error", reason=code)
        else:
            providers["techmap"] = ProviderStatus("cached" if techmap_cached_hit else "live", len(techmap_jobs))

    # ---- JSearch outcome
    jsearch_jobs: list[Job] = []
    jsearch_next: str | None = None
    jsearch_stale = False
    jsearch_cached = True
    if jsearch_outcome is None:
        providers["jsearch"] = ProviderStatus("exhausted")
    elif isinstance(jsearch_outcome, BaseException):
        if not techmap_jobs:
            raise jsearch_outcome  # nothing to show: keep the existing JSearch error contract
        code = jsearch_outcome.code if isinstance(jsearch_outcome, AppError) else "job_search_unavailable"
        logger.warning("JSearch failed (%s); returning Techmap results only", code)
        providers["jsearch"] = ProviderStatus("error", reason=code)
        jsearch_next = state.jsearch_cursor
    else:
        jsearch_jobs = jsearch_outcome.jobs
        jsearch_next = jsearch_outcome.next_cursor
        jsearch_stale = jsearch_outcome.stale
        jsearch_cached = jsearch_outcome.cached
        status = "stale" if jsearch_outcome.stale else ("cached" if jsearch_outcome.cached else "live")
        providers["jsearch"] = ProviderStatus(status, len(jsearch_jobs))

    merged, removed = merge_results(jsearch_jobs, techmap_jobs)
    if removed:
        providers["techmap"].count = len(techmap_jobs) - removed
        logger.info("Removed %d Techmap postings duplicating JSearch listings", removed)
    if params.city:
        # Exact city first, then district/division matches, then remote-anywhere (stable: keeps interleaving).
        country = params.effective_country

        def rank(job: Job) -> int:
            value = city_match_rank(job, params.city or "", country=country,
                                    include_remote_anywhere=params.remote_jobs_only)
            return 99 if value is None else value

        merged.sort(key=rank)

    # ---- next cursor
    if not state.composite:
        next_cursor = jsearch_next  # unchanged JSearch-only contract
    else:
        jsearch_failed = isinstance(jsearch_outcome, BaseException)
        jsearch_active = state.jsearch_active and (jsearch_failed or jsearch_next is not None)
        if techmap_error is not None:
            techmap_next = state.techmap_page if jsearch_active else None  # retry on the next page
        elif state.techmap_page is None:
            techmap_next = None
        next_cursor = _encode_cursor(jsearch_next if jsearch_active else None, jsearch_active, techmap_next)

    contributing = [name for name in ("jsearch", "techmap")
                    if providers.get(name) and providers[name].status in {"live", "cached", "stale"}]
    ran_live = providers.get("jsearch", ProviderStatus("")).status == "live" or (
        providers.get("techmap", ProviderStatus("")).status == "live"
    )
    return UnifiedResult(
        jobs=merged,
        next_cursor=next_cursor,
        cached=not ran_live and bool(contributing) and jsearch_cached,
        stale=jsearch_stale,
        country=params.effective_country,
        source="+".join(contributing) or "jsearch",
        providers=providers,
    )
