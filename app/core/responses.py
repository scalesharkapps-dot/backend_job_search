"""Standard success-response envelope shared by all versioned endpoints.

Every ``/api/v1`` endpoint (except health checks) returns::

    {"success": true, "message": "...", "data": <payload>, "error": null}

Errors use the same envelope with ``success: false`` (see ``app.core.exceptions``).
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: Any = None
    request_id: str | None = None


class ApiResponse(BaseModel, Generic[T]):
    """Envelope for every API response."""

    success: bool = True
    message: str = "Request completed successfully"
    data: T | None = None
    error: ErrorInfo | None = None


class PageMeta(BaseModel):
    page: int
    page_size: int
    total: int
    has_more: bool


class Paginated(BaseModel, Generic[T]):
    items: list[T] = Field(default_factory=list)
    pagination: PageMeta


def ok(data: Any = None, message: str = "Request completed successfully") -> dict[str, Any]:
    """Build a success envelope (validated by the route's ``response_model``)."""
    return {"success": True, "message": message, "data": data, "error": None}


def paginate(items: list[Any], page: int, page_size: int, total: int) -> dict[str, Any]:
    return {
        "items": items,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "has_more": page * page_size < total,
        },
    }


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    400: {"model": ApiResponse[None], "description": "Bad request"},
    401: {"model": ApiResponse[None], "description": "Missing or invalid access token"},
    403: {"model": ApiResponse[None], "description": "Insufficient permissions"},
    404: {"model": ApiResponse[None], "description": "Resource not found"},
    422: {"model": ApiResponse[None], "description": "Validation error"},
    429: {"model": ApiResponse[None], "description": "Rate limited"},
}
