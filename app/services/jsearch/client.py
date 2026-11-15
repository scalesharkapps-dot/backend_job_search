"""Low-level async HTTP client for JSearch on RapidAPI.

Verified API facts (RapidAPI listing "JSearch" by letscrape / OpenWeb Ninja):
* Base URL ``https://jsearch.p.rapidapi.com``; headers ``X-RapidAPI-Key`` and
  ``X-RapidAPI-Host: jsearch.p.rapidapi.com``.
* ``GET /search-v2``: ``query`` (required), ``cursor`` (pagination token from the
  previous response's ``data.cursor``), ``num_pages`` (1-20), ``country`` (ISO-2),
  ``language``, ``date_posted`` (all|today|3days|week|month), ``work_from_home``,
  ``employment_types`` (FULLTIME,CONTRACTOR,PARTTIME,INTERN), ``job_requirements``
  (under_3_years_experience,more_than_3_years_experience,no_experience,no_degree),
  ``radius`` (km), ``exclude_job_publishers``, ``fields``.
* ``GET /job-details``: ``job_id`` (comma-separated, up to 20), ``country``.
* Legacy ``GET /search`` uses ``page`` instead of ``cursor``; set
  ``JSEARCH_SEARCH_PATH=/search`` to use it.

Retries: only transient failures (timeouts, network errors, 429, 5xx) with
exponential backoff, honouring ``Retry-After`` up to a cap. Authentication and
validation errors are never retried. The API key is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.services.http import get_http_client
from app.services.jsearch.exceptions import (
    JSearchBadRequest,
    JSearchBadResponse,
    JSearchNotConfigured,
    JSearchRateLimited,
    JSearchUnavailable,
)

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class OutboundBudget:
    """Process-wide cap on outbound provider calls per minute (protects the subscription).

    Process-local: with N workers the effective cap is N x the configured value.
    """

    def __init__(self) -> None:
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self, per_minute: int) -> int:
        """Reserve one call. Returns 0 when allowed, else seconds to wait."""
        now = time.monotonic()
        with self._lock:
            while self._calls and self._calls[0] <= now - 60:
                self._calls.popleft()
            if len(self._calls) >= per_minute:
                return max(1, int(self._calls[0] + 60 - now) + 1)
            self._calls.append(now)
            return 0

    def reset(self) -> None:
        with self._lock:
            self._calls.clear()


outbound_budget = OutboundBudget()


@dataclass
class SearchPage:
    jobs: list[dict[str, Any]]
    next_cursor: str | None
    request_id: str | None = None


class JSearchClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _headers(self) -> dict[str, str]:
        key = self.settings.JSEARCH_API_KEY
        if key is None or not key.get_secret_value():
            raise JSearchNotConfigured()
        return {
            "X-RapidAPI-Key": key.get_secret_value(),
            "X-RapidAPI-Host": self.settings.JSEARCH_API_HOST,
            "Accept": "application/json",
        }

    @property
    def uses_cursor(self) -> bool:
        return self.settings.JSEARCH_SEARCH_PATH.rstrip("/").endswith("-v2")

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        headers = self._headers()
        wait = outbound_budget.acquire(self.settings.JSEARCH_GLOBAL_CALLS_PER_MINUTE)
        if wait:
            logger.warning("JSearch outbound budget exhausted")
            raise JSearchRateLimited(retry_after=wait)

        timeout = httpx.Timeout(
            self.settings.JSEARCH_TIMEOUT_SECONDS, connect=self.settings.JSEARCH_CONNECT_TIMEOUT_SECONDS
        )
        client = get_http_client("jsearch", timeout)
        url = f"{self.settings.JSEARCH_BASE_URL.rstrip('/')}{path}"
        clean = {k: v for k, v in params.items() if v not in (None, "", [])}
        attempts = self.settings.JSEARCH_MAX_RETRIES + 1
        last_error: Exception | None = None

        for attempt in range(attempts):
            retry_after: float | None = None
            try:
                response = await client.get(url, params=clean, headers=headers, timeout=timeout)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                logger.warning("JSearch %s attempt %d failed: %s", path, attempt + 1, type(exc).__name__)
            else:
                status = response.status_code
                if status == 200:
                    try:
                        payload = response.json()
                    except ValueError as exc:
                        logger.warning("JSearch returned invalid JSON")
                        raise JSearchBadResponse() from exc
                    if not isinstance(payload, dict):
                        raise JSearchBadResponse()
                    if str(payload.get("status", "OK")).upper() == "ERROR":
                        logger.warning("JSearch payload status=ERROR")
                        raise JSearchBadRequest()
                    return payload
                if status in (401, 403):
                    # Bad/missing key or subscription problem: operator issue, never retried.
                    logger.error("JSearch authentication/subscription failure (status=%s)", status)
                    raise JSearchUnavailable()
                if status in (400, 404, 422):
                    logger.info("JSearch rejected request (status=%s)", status)
                    raise JSearchBadRequest()
                if status not in RETRYABLE_STATUS:
                    logger.warning("JSearch unexpected status=%s", status)
                    raise JSearchBadResponse()
                last_error = None
                header = response.headers.get("retry-after")
                if header:
                    try:
                        retry_after = float(header)
                    except ValueError:
                        retry_after = None
                logger.warning("JSearch transient status=%s attempt %d", status, attempt + 1)
                if status == 429 and retry_after and retry_after > self.settings.JSEARCH_MAX_RETRY_AFTER_SECONDS:
                    raise JSearchRateLimited(retry_after=int(retry_after))
                if attempt == attempts - 1:
                    if status == 429:
                        raise JSearchRateLimited(retry_after=int(retry_after) if retry_after else None)
                    raise JSearchUnavailable()

            if attempt < attempts - 1:
                delay = retry_after if retry_after is not None else (
                    self.settings.JSEARCH_BACKOFF_BASE_SECONDS * (2**attempt) * (1 + random.random() * 0.25)  # noqa: S311 - jitter, not crypto
                )
                await asyncio.sleep(min(delay, self.settings.JSEARCH_MAX_RETRY_AFTER_SECONDS))

        logger.warning("JSearch %s failed after %d attempts (%s)", path, attempts, type(last_error).__name__)
        raise JSearchUnavailable()

    async def search(self, params: dict[str, Any], cursor: str | None) -> SearchPage:
        query = dict(params)
        num_pages = int(query.get("num_pages") or 1)
        if self.uses_cursor:
            query["cursor"] = cursor
        else:
            page = int(cursor) if cursor and cursor.isdigit() else 1
            query["page"] = page
        payload = await self._get(self.settings.JSEARCH_SEARCH_PATH, query)
        jobs, next_cursor = self._extract(payload)
        if not self.uses_cursor:
            # Legacy /search: an opaque cursor encodes the next page number.
            page = int(query["page"])
            next_cursor = str(page + num_pages) if len(jobs) >= 10 * num_pages else None
        return SearchPage(jobs=jobs, next_cursor=next_cursor, request_id=payload.get("request_id"))

    @staticmethod
    def _extract(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], str | None]:
        """Accept both response shapes: ``data: [...]`` (v1) and
        ``data: {"jobs": [...], "cursor": "..."}`` (v2)."""
        data = payload.get("data")
        cursor = payload.get("cursor") or payload.get("next_cursor")
        if isinstance(data, list):
            jobs = data
        elif isinstance(data, dict):
            jobs = data.get("jobs") or data.get("data") or data.get("results") or []
            cursor = data.get("cursor") or data.get("next_cursor") or cursor
        elif data is None:
            jobs = []
        else:
            raise JSearchBadResponse()
        if not isinstance(jobs, list):
            raise JSearchBadResponse()
        return [j for j in jobs if isinstance(j, dict)], (str(cursor) if cursor else None)

    async def job_details(self, external_id: str, country: str | None = None) -> dict[str, Any] | None:
        payload = await self._get(
            self.settings.JSEARCH_DETAILS_PATH, {"job_id": external_id, "country": country}
        )
        jobs, _ = self._extract(payload)
        return jobs[0] if jobs else None
