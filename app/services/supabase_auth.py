"""Thin async client for the Supabase Auth (GoTrue) REST API.

Only the public ``anon`` key is used for end-user operations. The service-role key
is used solely for explicitly privileged server-side admin calls
(:meth:`SupabaseAuthClient.admin_update_user`) and never leaves the server.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AppError,
    AuthenticationError,
    ConflictError,
    ExternalServiceError,
    NotConfiguredError,
    RateLimitedError,
    ServiceUnavailableError,
    ValidationAppError,
)
from app.services.http import get_http_client

logger = logging.getLogger(__name__)


def _error_code(payload: dict[str, Any]) -> str:
    return str(payload.get("error_code") or payload.get("error") or payload.get("code") or "")


def _error_message(payload: dict[str, Any]) -> str:
    return str(
        payload.get("msg") or payload.get("error_description") or payload.get("message") or ""
    )


class SupabaseAuthClient:
    """Async wrapper around the Supabase Auth endpoints the API needs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # ---- helpers ----
    def _base(self) -> str:
        if not self.settings.supabase_url or not self.settings.SUPABASE_ANON_KEY:
            raise NotConfiguredError(
                "Authentication is not configured on the server.", code="auth_not_configured"
            )
        return f"{self.settings.supabase_url}/auth/v1"

    def _headers(self, access_token: str | None = None) -> dict[str, str]:
        assert self.settings.SUPABASE_ANON_KEY is not None
        anon = self.settings.SUPABASE_ANON_KEY.get_secret_value()
        headers = {"apikey": anon, "Content-Type": "application/json"}
        headers["Authorization"] = f"Bearer {access_token or anon}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        access_token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = f"{self._base()}{path}"
        client = get_http_client("supabase_auth", self.settings.AUTH_HTTP_TIMEOUT_SECONDS)
        try:
            return await client.request(
                method,
                url,
                json=json,
                params=params,
                headers=headers or self._headers(access_token),
            )
        except httpx.TimeoutException as exc:
            logger.warning("Supabase Auth timeout on %s", path)
            raise ServiceUnavailableError(
                "The authentication service timed out.", code="auth_unavailable"
            ) from exc
        except httpx.HTTPError as exc:
            logger.warning("Supabase Auth network error on %s: %s", path, type(exc).__name__)
            raise ServiceUnavailableError(
                "The authentication service is unavailable.", code="auth_unavailable"
            ) from exc

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _raise_for(self, response: httpx.Response, context: str) -> None:
        """Map Supabase error responses to safe application errors."""
        if response.status_code < 400:
            return
        payload = self._json(response)
        code = _error_code(payload)
        message = _error_message(payload).lower()
        status = response.status_code
        logger.info("Supabase Auth %s failed: status=%s code=%s", context, status, code or "-")

        if status == 429 or code == "over_request_rate_limit" or code == "over_email_send_rate_limit":
            retry = response.headers.get("retry-after")
            raise RateLimitedError(
                "Too many attempts. Please wait before trying again.",
                retry_after=int(retry) if retry and retry.isdigit() else None,
            )
        if code in {"user_already_exists", "email_exists"} or "already registered" in message:
            raise ConflictError("An account with this email already exists.", code="email_exists")
        if code == "weak_password" or "password should" in message:
            raise ValidationAppError(
                "Password does not meet the security requirements.", code="weak_password"
            )
        if code == "email_not_confirmed" or "email not confirmed" in message:
            raise AuthenticationError(
                "Please verify your email address before signing in.", code="email_not_confirmed"
            )
        if code in {"invalid_credentials", "invalid_grant"} or "invalid login" in message:
            raise AuthenticationError("Invalid email or password.", code="invalid_credentials")
        if code in {"refresh_token_not_found", "refresh_token_already_used", "session_not_found"}:
            raise AuthenticationError("The session is invalid or has expired.", code="invalid_session")
        if code in {"signup_disabled", "email_provider_disabled"}:
            raise AppError("Sign-up is currently disabled.", code="signup_disabled", status_code=403)
        if code in {"otp_expired", "bad_jwt", "flow_state_expired"}:
            raise AuthenticationError("The link or token is invalid or has expired.", code="invalid_token")
        if code == "same_password":
            raise ValidationAppError(
                "The new password must differ from the old password.", code="same_password"
            )
        if code == "validation_failed" or status == 422:
            raise ValidationAppError("The submitted data was rejected.", code="validation_error")
        if status in (401, 403):
            raise AuthenticationError("Authentication failed.", code="invalid_session")
        if status >= 500:
            raise ServiceUnavailableError(
                "The authentication service is unavailable.", code="auth_unavailable"
            )
        raise ExternalServiceError("The authentication request failed.", code="auth_error")

    # ---- operations ----
    async def sign_up(
        self, email: str, password: str, metadata: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        params = (
            {"redirect_to": self.settings.AUTH_EMAIL_REDIRECT_URL}
            if self.settings.AUTH_EMAIL_REDIRECT_URL
            else None
        )
        response = await self._request(
            "POST",
            "/signup",
            json={"email": email, "password": password, "data": metadata or {}},
            params=params,
        )
        self._raise_for(response, "signup")
        return self._json(response)

    async def sign_in_with_password(self, email: str, password: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/token",
            params={"grant_type": "password"},
            json={"email": email, "password": password},
        )
        self._raise_for(response, "login")
        return self._json(response)

    async def refresh_session(self, refresh_token: str) -> dict[str, Any]:
        response = await self._request(
            "POST",
            "/token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
        )
        self._raise_for(response, "refresh")
        return self._json(response)

    async def sign_out(self, access_token: str, scope: str = "local") -> bool:
        """Revoke the session server-side.

        Returns True when revoked now, False when the session was already invalid.
        Raises on upstream failure so callers never claim a revocation that failed.
        """
        response = await self._request(
            "POST", "/logout", params={"scope": scope}, access_token=access_token
        )
        if response.status_code in (200, 204):
            return True
        if response.status_code in (401, 403, 404):
            return False
        self._raise_for(response, "logout")
        return False

    async def resend_signup_confirmation(self, email: str) -> None:
        body: dict[str, Any] = {"type": "signup", "email": email}
        if self.settings.AUTH_EMAIL_REDIRECT_URL:
            body["options"] = {"emailRedirectTo": self.settings.AUTH_EMAIL_REDIRECT_URL}
        response = await self._request("POST", "/resend", json=body)
        self._raise_for(response, "resend")

    async def send_password_recovery(self, email: str) -> None:
        params = (
            {"redirect_to": self.settings.AUTH_PASSWORD_RESET_REDIRECT_URL}
            if self.settings.AUTH_PASSWORD_RESET_REDIRECT_URL
            else None
        )
        response = await self._request("POST", "/recover", json={"email": email}, params=params)
        self._raise_for(response, "recover")

    async def verify_recovery_token_hash(self, token_hash: str) -> dict[str, Any]:
        response = await self._request(
            "POST", "/verify", json={"type": "recovery", "token_hash": token_hash}
        )
        self._raise_for(response, "verify")
        return self._json(response)

    async def update_password(self, access_token: str, new_password: str) -> dict[str, Any]:
        response = await self._request(
            "PUT", "/user", json={"password": new_password}, access_token=access_token
        )
        self._raise_for(response, "update_password")
        return self._json(response)

    async def admin_update_user(self, user_id: str, attributes: dict[str, Any]) -> bool:
        """Privileged update (e.g. ban). Returns False when no service-role key is configured."""
        if not self.settings.SUPABASE_SERVICE_ROLE_KEY:
            return False
        key = self.settings.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
        headers = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        response = await self._request(
            "PUT", f"/admin/users/{user_id}", json=attributes, headers=headers
        )
        self._raise_for(response, "admin_update_user")
        return True


def get_supabase_auth() -> SupabaseAuthClient:
    """FastAPI dependency (overridable in tests)."""
    return SupabaseAuthClient()
