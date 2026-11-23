"""Periodic maintenance tasks (run from cron / a scheduled job):

    python -m app.scripts.maintenance

* Marks SharkHire jobs whose application deadline has passed as ``expired``.
* Purges search-cache rows that expired more than 7 days ago.
* Billing: retries RevenueCat webhook events left ``failed``/``unmatched`` and confirms
  webhook-applied subscription state with the RevenueCat API (when the API key is set).
"""

from __future__ import annotations

import asyncio
import sys

from app.core.database import dispose_engine, get_sessionmaker
from app.services.billing.revenuecat import RevenueCatClient
from app.services.billing.subscriptions import reconcile_flagged
from app.services.billing.webhook import reprocess_events
from app.services.employer_service import expire_due_jobs
from app.services.jsearch.cache import purge_expired


async def run() -> None:
    async with get_sessionmaker()() as db:
        expired = await expire_due_jobs(db)
        purged = await purge_expired(db)
        client = RevenueCatClient()
        events = await reprocess_events(db, client if client.configured else None)
        reconciled = await reconcile_flagged(db, client) if client.configured else {"synced": 0, "failed": 0}
    await dispose_engine()
    print(
        f"expired_jobs={expired} purged_cache_rows={purged} "
        f"webhook_events_retried={events['retried']} webhook_events_failed={events['failed']} "
        f"subscriptions_reconciled={reconciled['synced']} reconcile_failed={reconciled['failed']}"
    )


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run())
