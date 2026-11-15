"""Uvicorn loop factory that always uses the selector event loop.

psycopg's async mode cannot run on Windows' ProactorEventLoop. On Windows run:

    uvicorn app.main:app --loop app.core.eventloop:selector_loop_factory

(``python -m app.server`` does this automatically). Linux/Docker is unaffected.
"""

from __future__ import annotations

import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Create a selector event loop (uvicorn calls this to build its loop)."""
    return asyncio.SelectorEventLoop()
