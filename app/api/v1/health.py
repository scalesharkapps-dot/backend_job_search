"""Health-check endpoints (unwrapped JSON for load balancers and probes)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.core.config import get_settings
from app.core.database import check_database
from app.core.exceptions import AppError

logger = logging.getLogger(__name__)
router = APIRouter(tags=["Health"])


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    environment: str
    timestamp: datetime


class VersionedHealthResponse(HealthResponse):
    api_version: str


class DatabaseHealthResponse(BaseModel):
    status: str
    database: str
    timestamp: datetime


def _base() -> dict[str, object]:
    s = get_settings()
    return {
        "status": "ok",
        "service": s.APP_NAME,
        "version": s.APP_VERSION,
        "environment": s.APP_ENV,
        "timestamp": datetime.now(UTC),
    }


async def liveness() -> dict[str, object]:
    """Liveness: the process is up. Does NOT claim the database is reachable."""
    return _base()


@router.get("/health", response_model=VersionedHealthResponse, summary="API liveness")
async def versioned_health() -> dict[str, object]:
    """Versioned liveness check. Use `/api/v1/health/database` for readiness."""
    return {**_base(), "api_version": "v1"}


@router.get(
    "/health/database",
    response_model=DatabaseHealthResponse,
    summary="Database readiness",
    responses={503: {"model": DatabaseHealthResponse, "description": "Database unreachable"}},
)
async def database_health() -> JSONResponse:
    """Run `SELECT 1` against the configured database.

    Returns 200 when the query succeeds and 503 otherwise. Connection details
    and exception messages are never exposed.
    """
    now = datetime.now(UTC).isoformat()
    try:
        await check_database()
    except AppError as exc:  # not configured
        return JSONResponse(
            status_code=503, content={"status": "unavailable", "database": exc.code, "timestamp": now}
        )
    except Exception as exc:
        logger.warning("Database health check failed: %s", type(exc).__name__)
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "database": "unreachable", "timestamp": now},
        )
    return JSONResponse(
        status_code=200, content={"status": "ok", "database": "connected", "timestamp": now}
    )
