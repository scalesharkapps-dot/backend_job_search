"""SQLAlchemy 2.x async engine, session factory and FastAPI session dependency.

Driver: ``psycopg`` (v3) for PostgreSQL/Supabase. The same URL works for the async
engine used by the API and the sync engine used by Alembic.

Supabase connection modes:
* Direct connection / session pooler (port 5432): normal pooling.
* Transaction pooler (port 6543, PgBouncer): set ``DB_USE_PGBOUNCER=true`` so
  server-side prepared statements are disabled.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.exceptions import NotConfiguredError

logger = logging.getLogger(__name__)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def normalize_database_url(url: str, *, async_driver: bool = True) -> str:
    """Normalise user-supplied URLs to the drivers this project uses."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    if url.startswith("sqlite://") and async_driver:
        url = "sqlite+aiosqlite://" + url[len("sqlite://") :]
    if url.startswith("sqlite+aiosqlite://") and not async_driver:
        url = "sqlite://" + url[len("sqlite+aiosqlite://") :]
    return url


def build_engine(settings: Settings, url: str | None = None) -> AsyncEngine:
    """Create an async engine configured for the target database."""
    raw = url or settings.database_url
    if not raw:
        raise NotConfiguredError("The database is not configured.", code="database_not_configured")
    db_url = normalize_database_url(raw)
    kwargs: dict[str, Any] = {"echo": settings.DB_ECHO, "future": True}
    if db_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
    else:
        connect_args: dict[str, Any] = {"connect_timeout": settings.DB_CONNECT_TIMEOUT}
        if settings.DB_USE_PGBOUNCER:
            # PgBouncer transaction mode cannot use server-side prepared statements.
            connect_args["prepare_threshold"] = None
        kwargs.update(
            pool_size=settings.DB_POOL_SIZE,
            max_overflow=settings.DB_MAX_OVERFLOW,
            pool_timeout=settings.DB_POOL_TIMEOUT,
            pool_recycle=settings.DB_POOL_RECYCLE,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
    return create_async_engine(db_url, **kwargs)


def init_engine(settings: Settings | None = None, url: str | None = None) -> AsyncEngine:
    """Initialise (or replace) the global engine and session factory."""
    global _engine, _sessionmaker
    settings = settings or get_settings()
    _engine = build_engine(settings, url)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        init_engine()
    assert _engine is not None
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


def set_engine(engine: AsyncEngine) -> None:
    """Inject an engine (used by tests)."""
    global _engine, _sessionmaker
    _engine = engine
    _sessionmaker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that is always closed.

    Services commit explicitly; anything left uncommitted is rolled back.
    """
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def check_database() -> None:
    """Run a lightweight query; raises on failure."""
    async with get_engine().connect() as conn:
        await conn.execute(text("SELECT 1"))
