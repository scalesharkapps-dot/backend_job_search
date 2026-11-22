"""Server-side RevenueCat REST client (API v1).

Only ``GET /v1/subscribers/{app_user_id}`` is used: it returns the trusted
CustomerInfo (``subscriber.entitlements`` / ``subscriber.subscriptions``) for an
App User ID. SharkHire always uses the authenticated Supabase user id as the App
User ID, so a caller can only ever look up their own customer record.

Note: RevenueCat creates an empty customer when the id is unknown (HTTP 201);
that is treated as "no purchases".

The secret key (``sk_...``) is sent only to RevenueCat and is never logged.
Idempotent GETs are retried on timeouts, network errors, 429 and 5xx.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import ExternalServiceError, NotConfiguredError, ServiceUnavailableError
from app.services.http import get_http_client

logger = logging.getLogger(__name__)

HTTP_CLIENT_NAME = "revenuecat"


def parse_rc_datetime(value: Any) -> datetime | None:
    """RevenueCat v1 dates are ISO-8601 strings in UTC (e.g. ``2026-11-19T10:00:00Z``)."""
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def ms_to_datetime(value: Any) -> datetime | None:
    """Webhook timestamps are integer milliseconds since the epoch."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class CustomerInfo:
    app_user_id: str
    original_app_user_id: str | None
    request_date: datetime
    entitlements: dict[str, dict[str, Any]]
    subscriptions: dict[str, dict[str, Any]]
    created: bool  # RevenueCat had no customer for this id before this request


class RevenueCatClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.REVENUECAT_SECRET_API_KEY)

    def _headers(self) -> dict[str, str]:
        if not self.settings.REVENUECAT_SECRET_API_KEY:
            raise NotConfiguredError(
                "Subscription verification is not configured on the server.",
                code="subscriptions_not_configured",
            )
        key = self.settings.REVENUECAT_SECRET_API_KEY.get_secret_value()
        return {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    async def _get(self, path: str) -> httpx.Response:
        headers = self._headers()
        url = f"{self.settings.REVENUECAT_API_BASE_URL.rstrip('/')}{path}"
        client = get_http_client(HTTP_CLIENT_NAME, self.settings.REVENUECAT_TIMEOUT_SECONDS)
        attempts = self.settings.REVENUECAT_MAX_RETRIES + 1
        backoff = self.settings.REVENUECAT_RETRY_BACKOFF_SECONDS
        last_reason = "unknown"
        for attempt in range(attempts):
            try:
                response = await client.get(url, headers=headers)
            except httpx.TimeoutException:
                last_reason = "timeout"
            except httpx.HTTPError as exc:
                last_reason = type(exc).__name__
            else:
                if response.status_code != 429 and response.status_code < 500:
                    return response
                last_reason = f"status={response.status_code}"
            logger.warning("RevenueCat request failed (%s), attempt %s/%s", last_reason, attempt + 1, attempts)
            if attempt + 1 < attempts and backoff:
                await asyncio.sleep(min(backoff * (2**attempt), 5.0))
        raise ServiceUnavailableError(
            "Subscription verification is temporarily unavailable. Please try again later.",
            code="subscription_service_unavailable",
        )

    async def get_customer(self, app_user_id: str) -> CustomerInfo:
        response = await self._get(f"/subscribers/{quote(app_user_id, safe='')}")
        if response.status_code in (401, 403):
            # Wrong/revoked secret key or a public (appl_/goog_) key used by mistake.
            logger.error("RevenueCat rejected the server API key (status=%s)", response.status_code)
            raise ExternalServiceError(
                "Subscription verification is temporarily unavailable.",
                code="subscription_service_auth_failed",
            )
        # v1 creates unknown customers (201), so a 404 means a wrong base URL/path -- never
        # "no purchases" (that would revoke verified access).
        if response.status_code not in (200, 201):
            logger.warning("RevenueCat returned unexpected status %s", response.status_code)
            raise ExternalServiceError(
                "Subscription verification failed.", code="subscription_service_error"
            )
        try:
            body = response.json()
        except ValueError:
            body = None
        subscriber = body.get("subscriber") if isinstance(body, dict) else None
        if not isinstance(subscriber, dict):
            logger.warning("RevenueCat returned an invalid customer payload")
            raise ExternalServiceError(
                "Subscription verification returned an invalid response.",
                code="invalid_subscription_response",
            )
        entitlements = subscriber.get("entitlements", {})
        subscriptions = subscriber.get("subscriptions", {})
        if not isinstance(entitlements, dict) or not isinstance(subscriptions, dict):
            raise ExternalServiceError(
                "Subscription verification returned an invalid response.",
                code="invalid_subscription_response",
            )
        request_date = parse_rc_datetime(body.get("request_date")) or datetime.now(UTC)
        return CustomerInfo(
            app_user_id=app_user_id,
            original_app_user_id=subscriber.get("original_app_user_id"),
            request_date=request_date,
            entitlements={k: v for k, v in entitlements.items() if isinstance(v, dict)},
            subscriptions={k: v for k, v in subscriptions.items() if isinstance(v, dict)},
            created=response.status_code == 201,
        )


def get_revenuecat_client() -> RevenueCatClient:
    """FastAPI dependency (overridable in tests)."""
    return RevenueCatClient()
