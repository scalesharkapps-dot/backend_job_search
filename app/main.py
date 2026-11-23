"""FastAPI application factory for the SharkHire backend."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1 import health
from app.api.v1.router import api_router
from app.core.config import Settings, get_settings
from app.core.database import dispose_engine
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware

logger = logging.getLogger(__name__)

DESCRIPTION = """
SharkHire is a job-search platform for Android and iOS (Flutter). This API provides
authentication (Supabase), profiles and CV processing, JSearch-powered job search,
explainable job recommendations, job/application tracking, and employer/admin tools.

All `/api/v1` responses use the envelope
`{"success": bool, "message": str, "data": ..., "error": {...} | null}`.
Authenticate with `Authorization: Bearer <Supabase access token>`.
"""


class ConfigurationError(RuntimeError):
    """Raised at startup when required production configuration is missing."""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    logger.info("Starting %s (%s)", settings.APP_NAME, settings.APP_ENV)
    yield
    from app.services.http import close_http_clients

    await close_http_clients()
    await dispose_engine()
    logger.info("Shutdown complete")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = settings or get_settings()
    configure_logging(settings.LOG_LEVEL, settings.LOG_JSON)

    problems = settings.validate_production()
    if problems:
        raise ConfigurationError("Invalid production configuration: " + "; ".join(problems))
    if settings.STORAGE_BACKEND == "supabase":
        from app.services.storage.supabase import missing_configuration

        missing = missing_configuration()
        if missing:
            # Non-production environments may boot without storage, but CV uploads will fail with
            # storage_not_configured until these are set. Make that obvious in the startup log.
            logger.error(
                "STORAGE_BACKEND=supabase but %s is not set: CV and image uploads will fail "
                "(check GET %s/health/storage)",
                " and ".join(missing),
                settings.API_V1_PREFIX,
            )

    for warning in settings.auth_redirect_warnings():
        logger.warning("Auth e-mail configuration: %s", warning)
    for warning in settings.subscription_config_warnings():
        # Names only, never values. Missing billing config fails closed (no access is granted).
        logger.warning("Subscription configuration: %s", warning)

    docs_enabled = settings.ENABLE_DOCS
    app = FastAPI(
        title=settings.APP_NAME,
        description=DESCRIPTION,
        version=settings.APP_VERSION,
        lifespan=lifespan,
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        openapi_url="/openapi.json" if docs_enabled else None,
        debug=False,  # never return tracebacks to clients
    )
    app.state.settings = settings

    if settings.cors_origins:
        allow_all = settings.cors_origins == ["*"]
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=not allow_all,  # never wildcard + credentials
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
            expose_headers=["X-Request-ID", "Retry-After"],
        )
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(RequestContextMiddleware)  # outermost: request ID + access log
    register_exception_handlers(app)

    app.add_api_route(
        "/health",
        health.liveness,
        methods=["GET"],
        response_model=health.HealthResponse,
        tags=["Health"],
        summary="Application liveness",
    )
    app.include_router(api_router, prefix=settings.API_V1_PREFIX)
    return app


app = create_app()
