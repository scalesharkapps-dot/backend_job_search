"""Periodic maintenance tasks (run from cron / a scheduled job):

    python -m app.scripts.maintenance

* Marks SharkHire jobs whose application deadline has passed as ``expired``.
* Purges search-cache rows that expired more than 7 days ago.
"""

from __future__ import annotations

import asyncio
import sys

from app.core.database import dispose_engine, get_sessionmaker
from app.services.employer_service import expire_due_jobs
from app.services.jsearch.cache import purge_expired


async def run() -> None:
    async with get_sessionmaker()() as db:
        expired = await expire_due_jobs(db)
        purged = await purge_expired(db)
    await dispose_engine()
    print(f"expired_jobs={expired} purged_cache_rows={purged}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
