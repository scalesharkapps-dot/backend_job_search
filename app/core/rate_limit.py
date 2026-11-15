"""Process-local sliding-window rate limiting.

IMPORTANT: counters live in the memory of one Python process. With several
workers or replicas each process keeps its own counters, so the effective limit
is multiplied by the number of processes. This is NOT distributed rate limiting;
see docs/SECURITY_AND_OPERATIONS.md for the production recommendation.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Request

from app.core.config import get_settings
from app.core.exceptions import RateLimitedError


class SlidingWindowLimiter:
    """Thread-safe in-memory sliding-window counter keyed by arbitrary strings."""

    def __init__(self, max_keys: int = 50_000) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._max_keys = max_keys

    def hit(self, key: str, limit: int, window_seconds: float) -> tuple[bool, int]:
        """Record a hit. Returns (allowed, retry_after_seconds)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._hits.get(key)
            if bucket is None:
                if len(self._hits) >= self._max_keys:
                    self._evict(now, window_seconds)
                bucket = self._hits.setdefault(key, deque())
            while bucket and bucket[0] <= now - window_seconds:
                bucket.popleft()
            if len(bucket) >= limit:
                retry_after = max(1, math.ceil(bucket[0] + window_seconds - now))
                return False, retry_after
            bucket.append(now)
            return True, 0

    def _evict(self, now: float, window_seconds: float) -> None:
        stale = [k for k, b in self._hits.items() if not b or b[-1] <= now - window_seconds]
        for key in stale:
            self._hits.pop(key, None)
        if len(self._hits) >= self._max_keys:  # still full: drop oldest half
            for key in list(self._hits)[: self._max_keys // 2]:
                self._hits.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = SlidingWindowLimiter()


def client_ip(request: Request) -> str:
    settings = get_settings()
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(
    scope: str, limit_setting: str, window_seconds: int = 60
) -> Callable[[Request], None]:
    """Create a dependency limiting requests per client IP (and user when known).

    ``limit_setting`` names a ``Settings`` attribute so limits stay configurable.
    """

    def dependency(request: Request) -> None:
        settings = get_settings()
        if not settings.RATE_LIMIT_ENABLED:
            return
        limit = int(getattr(settings, limit_setting))
        key = f"{scope}:{client_ip(request)}"
        allowed, retry_after = limiter.hit(key, limit, window_seconds)
        if not allowed:
            raise RateLimitedError(retry_after=retry_after)

    return dependency


def enforce_user_limit(scope: str, user_id: object, limit: int, window_seconds: int = 60) -> None:
    """Apply a per-user limit inside a route or service."""
    if not get_settings().RATE_LIMIT_ENABLED:
        return
    allowed, retry_after = limiter.hit(f"{scope}:user:{user_id}", limit, window_seconds)
    if not allowed:
        raise RateLimitedError(retry_after=retry_after)
