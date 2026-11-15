"""JSearch-specific errors (all client-safe; raw provider errors are never exposed)."""

from __future__ import annotations

from app.core.exceptions import (
    BadRequestError,
    ExternalServiceError,
    NotConfiguredError,
    RateLimitedError,
    ServiceUnavailableError,
)


class JSearchNotConfigured(NotConfiguredError):
    code = "job_search_not_configured"
    message = "Job search is not configured on the server."


class JSearchUnavailable(ServiceUnavailableError):
    code = "job_search_unavailable"
    message = "Job search is temporarily unavailable. Please try again later."


class JSearchRateLimited(RateLimitedError):
    code = "job_search_rate_limited"
    message = "Job search is busy right now. Please try again shortly."


class JSearchBadResponse(ExternalServiceError):
    code = "job_search_bad_response"
    message = "The job search provider returned an unexpected response."


class JSearchBadRequest(BadRequestError):
    code = "job_search_invalid_request"
    message = "The job search request was rejected by the provider."
