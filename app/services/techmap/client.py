"""Async client for Techmap's "Daily International Job Postings" API on RapidAPI.

Verified against the live API (2026-11-28) and the vendor reference
(jobdatafeeds.com/job-api-overview):

* ``GET https://daily-international-job-postings.p.rapidapi.com/api/v2/jobs/search``
  with headers ``X-RapidAPI-Key`` and ``X-RapidAPI-Host``.
* Parameters used: ``title``, ``countryCode`` (ISO-2, lowercase), ``city``,
  ``company``, ``language``, ``workPlace`` (remote/hybrid/onsite), ``workType``,
  ``dateCreatedMin`` + ``dateCreatedMax`` (both required together, YYYY-MM-DD;
  without them only the last 2 days are searched), ``isDuplicate=false``,
  ``format=json``, ``page`` (1-based, fixed 10 results per page).
* Response: ``{"page", "pageSize", "totalCount", "result": [...]}``.
* Billing: every request counts against the plan quota and requests above it are
  billed as overage (not blocked). Calls therefore stop once RapidAPI reports
  ``TECHMAP_QUOTA_RESERVE`` or fewer requests left, and a per-minute cap applies.

Only transient failures (timeouts, network errors, 429, 5xx) are retried. The
API key is never logged.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import AppError
from app.services.http import get_http_client
from app.services.jsearch.client import OutboundBudget, PlanQuota

logger = logging.getLogger(__name__)

HTTP_CLIENT_NAME = "techmap"
PAGE_SIZE = 10
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

techmap_budget = OutboundBudget()
techmap_quota = PlanQuota("Techmap")


class TechmapError(AppError):
    """Techmap failures are handled inside the unified search (never shown on their own)."""

    status_code = 503
    code = "techmap_unavailable"
    message = "The Techmap job provider is unavailable."


@dataclass
class TechmapPage:
    jobs: list[dict[str, Any]]
    page: int
    total_count: int | None

    @property
    def next_page(self) -> int | None:
        if self.total_count is not None:
            return self.page + 1 if self.page * PAGE_SIZE < self.total_count else None
        return self.page + 1 if len(self.jobs) >= PAGE_SIZE else None


class TechmapClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        key = self.settings.TECHMAP_API_KEY
        return bool(self.settings.TECHMAP_ENABLED and key is not None and key.get_secret_value())

    def _headers(self) -> dict[str, str]:
        key = self.settings.TECHMAP_API_KEY
        if not self.configured or key is None:
            raise TechmapError("Techmap is not configured.", code="techmap_not_configured")
        return {
            "X-RapidAPI-Key": key.get_secret_value(),
            "X-RapidAPI-Host": self.settings.TECHMAP_API_HOST,
            "Accept": "application/json",
        }

    async def search(self, params: dict[str, Any]) -> TechmapPage:
        headers = self._headers()
        blocked = techmap_quota.blocked_for(self.settings.TECHMAP_QUOTA_RESERVE)
        if blocked:
            logger.warning("Techmap plan quota reserve reached; not calling the provider (avoids overage)")
            raise TechmapError("Techmap quota reserve reached.", code="techmap_quota_reserved")
        if techmap_budget.acquire(self.settings.TECHMAP_CALLS_PER_MINUTE):
            logger.warning("Techmap per-minute call budget exhausted")
            raise TechmapError("Techmap call budget exhausted.", code="techmap_rate_limited")

        timeout = httpx.Timeout(
            self.settings.TECHMAP_TIMEOUT_SECONDS, connect=self.settings.TECHMAP_CONNECT_TIMEOUT_SECONDS
        )
        client = get_http_client(HTTP_CLIENT_NAME, timeout)
        url = f"https://{self.settings.TECHMAP_API_HOST}{self.settings.TECHMAP_SEARCH_PATH}"
        query = {k: v for k, v in params.items() if v not in (None, "", [])}
        attempts = self.settings.TECHMAP_MAX_RETRIES + 1
        for attempt in range(attempts):
            try:
                response = await client.get(url, params=query, headers=headers, timeout=timeout)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                logger.warning("Techmap attempt %d failed: %s", attempt + 1, type(exc).__name__)
            else:
                techmap_quota.update(response.headers, self.settings.TECHMAP_QUOTA_WARN_BELOW)
                status = response.status_code
                if status == 200:
                    return self._parse(response, int(query.get("page") or 1))
                if status in (401, 403):
                    logger.error("Techmap authentication/subscription failure (status=%s)", status)
                    raise TechmapError("Techmap rejected the API key or plan.", code="techmap_unauthorized")
                if status in (400, 404, 422):
                    logger.info("Techmap rejected the request (status=%s)", status)
                    raise TechmapError("Techmap rejected the request.", code="techmap_bad_request")
                if status not in RETRYABLE_STATUS:
                    logger.warning("Techmap unexpected status=%s", status)
                    raise TechmapError(code="techmap_bad_response")
                logger.warning("Techmap transient status=%s attempt %d", status, attempt + 1)
                if status == 429 and attempt == attempts - 1:
                    raise TechmapError("Techmap rate limit reached.", code="techmap_rate_limited")
            if attempt < attempts - 1:
                await asyncio.sleep(0.5 * (2**attempt))
        raise TechmapError()

    @staticmethod
    def _parse(response: httpx.Response, page: int) -> TechmapPage:
        try:
            payload = response.json()
        except ValueError:
            logger.warning("Techmap returned invalid JSON")
            raise TechmapError(code="techmap_bad_response") from None
        if not isinstance(payload, dict):
            raise TechmapError(code="techmap_bad_response")
        result = payload.get("result")
        if result is None:
            result = []
        if not isinstance(result, list):
            raise TechmapError(code="techmap_bad_response")
        total = payload.get("totalCount")
        total_count = int(total) if isinstance(total, int | float) and not isinstance(total, bool) else None
        return TechmapPage([j for j in result if isinstance(j, dict)], page, total_count)


def get_techmap_client() -> TechmapClient:
    """FastAPI dependency (overridable in tests)."""
    return TechmapClient()
