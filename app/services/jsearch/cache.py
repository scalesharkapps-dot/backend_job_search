"""PostgreSQL-backed search cache plus in-process request coalescing.

Each cache row stores the ordered job ids for one (parameters, cursor) page and
the provider's next cursor. Expired rows are never presented as fresh: they are
only served as an explicit ``stale`` fallback when the provider is unavailable.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utcnow
from app.models.job import JobSearchCache

_locks: dict[str, asyncio.Lock] = {}
_waiters: dict[str, int] = {}


@asynccontextmanager
async def coalesce(key: str) -> AsyncIterator[None]:
    """Serialise identical concurrent searches so only one hits the provider."""
    lock = _locks.setdefault(key, asyncio.Lock())
    _waiters[key] = _waiters.get(key, 0) + 1
    try:
        async with lock:
            yield
    finally:
        _waiters[key] -= 1
        if _waiters[key] == 0:
            _waiters.pop(key, None)
            _locks.pop(key, None)


async def get_entry(db: AsyncSession, search_hash: str) -> JobSearchCache | None:
    return await db.scalar(select(JobSearchCache).where(JobSearchCache.search_hash == search_hash))


def is_fresh(entry: JobSearchCache, now: datetime | None = None) -> bool:
    return entry.expires_at > (now or utcnow())


async def store(
    db: AsyncSession,
    search_hash: str,
    params: dict[str, Any],
    job_ids: list[uuid.UUID],
    next_cursor: str | None,
    ttl_seconds: int,
) -> None:
    now = utcnow()
    values = {
        "search_parameters": params,
        "job_ids": [str(j) for j in job_ids],
        "next_cursor": next_cursor,
        "result_count": len(job_ids),
        "expires_at": now + timedelta(seconds=ttl_seconds),
        "created_at": now,
        "hit_count": 0,
    }
    entry = await get_entry(db, search_hash)
    if entry is None:
        db.add(JobSearchCache(search_hash=search_hash, **values))
    else:
        for key, value in values.items():
            setattr(entry, key, value)
    try:
        await db.commit()
    except IntegrityError:  # another process stored it first; theirs is equally fresh
        await db.rollback()


async def record_hit(db: AsyncSession, entry: JobSearchCache) -> None:
    entry.hit_count += 1
    await db.commit()


async def invalidate(db: AsyncSession, search_hash: str | None = None) -> int:
    stmt = delete(JobSearchCache)
    if search_hash:
        stmt = stmt.where(JobSearchCache.search_hash == search_hash)
    result = await db.execute(stmt)
    await db.commit()
    return result.rowcount or 0


async def purge_expired(db: AsyncSession, older_than: timedelta = timedelta(days=7)) -> int:
    result = await db.execute(delete(JobSearchCache).where(JobSearchCache.expires_at < utcnow() - older_than))
    await db.commit()
    return result.rowcount or 0
