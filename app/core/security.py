"""Supabase JWT verification.

Supports both Supabase signing configurations:

* Asymmetric signing keys (ES256 / RS256) published at the project's JWKS
  endpoint (``/auth/v1/.well-known/jwks.json``). Keys are cached and refreshed
  when an unknown ``kid`` appears (with a refresh throttle).
* Legacy symmetric HS256 secret, accepted only when ``SUPABASE_JWT_SECRET`` is set.

Every token must have a valid signature, an allowed algorithm, an unexpired
``exp``, the expected ``iss`` and ``aud`` (when configured), and a UUID ``sub``.
Tokens are never logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
from jwt import PyJWK

from app.core.config import Settings, get_settings
from app.core.exceptions import AuthenticationError, ServiceUnavailableError
from app.services.http import get_http_client

logger = logging.getLogger(__name__)

_MIN_REFRESH_INTERVAL = 30.0


@dataclass
class TokenClaims:
    """Verified identity extracted from an access token."""

    user_id: uuid.UUID
    email: str | None
    role_claim: str | None
    session_id: str | None
    expires_at: int
    aal: str | None = None
    is_anonymous: bool = False
    user_metadata: dict[str, Any] = field(default_factory=dict)
    app_metadata: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


class JWKSCache:
    """Fetches and caches the Supabase JWKS."""

    def __init__(self) -> None:
        self._keys: dict[str, PyJWK] = {}
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    def clear(self) -> None:
        self._keys = {}
        self._fetched_at = 0.0

    def set_keys(self, jwks: dict[str, Any]) -> None:
        keys: dict[str, PyJWK] = {}
        for jwk in jwks.get("keys", []):
            try:
                key = PyJWK.from_dict(jwk)
            except jwt.PyJWTError:
                logger.warning("Skipping unsupported JWK (kid=%s)", jwk.get("kid"))
                continue
            keys[jwk.get("kid") or ""] = key
        self._keys = keys
        self._fetched_at = time.monotonic()

    async def _fetch(self, settings: Settings) -> None:
        url = settings.jwks_url
        if not url:
            raise ServiceUnavailableError(
                "Token verification is not configured.", code="auth_not_configured"
            )
        client = get_http_client("supabase_auth", settings.AUTH_HTTP_TIMEOUT_SECONDS)
        try:
            response = await client.get(url)
            response.raise_for_status()
            self.set_keys(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            logger.error("JWKS fetch failed: %s", type(exc).__name__)
            if not self._keys:
                raise ServiceUnavailableError(
                    "Unable to verify tokens right now.", code="auth_unavailable"
                ) from exc

    async def get_key(self, kid: str | None, settings: Settings) -> PyJWK:
        expired = time.monotonic() - self._fetched_at > settings.JWKS_CACHE_TTL_SECONDS
        if expired or (kid or "") not in self._keys:
            async with self._lock:
                stale = time.monotonic() - self._fetched_at > settings.JWKS_CACHE_TTL_SECONDS
                can_refresh = time.monotonic() - self._fetched_at > _MIN_REFRESH_INTERVAL
                if stale or ((kid or "") not in self._keys and can_refresh):
                    await self._fetch(settings)
        key = self._keys.get(kid or "")
        if key is None and kid is None and len(self._keys) == 1:
            key = next(iter(self._keys.values()))
        if key is None:
            raise AuthenticationError("Invalid access token.", code="invalid_token")
        return key


jwks_cache = JWKSCache()


def _invalid(message: str = "Invalid access token.", code: str = "invalid_token") -> AuthenticationError:
    return AuthenticationError(message, code=code)


async def verify_access_token(token: str, settings: Settings | None = None) -> TokenClaims:
    """Verify a Supabase access token and return its claims.

    Raises ``AuthenticationError`` for any invalid token.
    """
    settings = settings or get_settings()
    if not token or token.count(".") != 2 or len(token) > 8192:
        raise _invalid()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise _invalid() from exc

    alg = str(header.get("alg", "")).upper()
    allowed = settings.jwt_algorithms
    if not alg or alg == "NONE" or alg not in allowed:
        raise _invalid("Unsupported token algorithm.", "unsupported_algorithm")

    if alg.startswith("HS"):
        assert settings.SUPABASE_JWT_SECRET is not None
        key: Any = settings.SUPABASE_JWT_SECRET.get_secret_value()
    else:
        key = (await jwks_cache.get_key(header.get("kid"), settings)).key

    issuer = settings.jwt_issuer
    audience = settings.SUPABASE_JWT_AUDIENCE or None
    required = ["exp", "sub"] + (["iss"] if issuer else []) + (["aud"] if audience else [])
    try:
        claims = jwt.decode(
            token,
            key=key,
            algorithms=[alg],
            audience=audience,
            issuer=issuer,
            leeway=settings.JWT_LEEWAY_SECONDS,
            options={
                "require": required,
                "verify_signature": True,
                "verify_exp": True,
                "verify_aud": audience is not None,
                "verify_iss": issuer is not None,
            },
        )
    except jwt.ExpiredSignatureError as exc:
        raise _invalid("Access token has expired.", "token_expired") from exc
    except jwt.InvalidAudienceError as exc:
        raise _invalid("Invalid token audience.") from exc
    except jwt.InvalidIssuerError as exc:
        raise _invalid("Invalid token issuer.") from exc
    except jwt.PyJWTError as exc:
        raise _invalid() from exc

    try:
        user_id = uuid.UUID(str(claims["sub"]))
    except (ValueError, KeyError) as exc:
        raise _invalid("Token subject is invalid.") from exc

    role_claim = claims.get("role")
    if role_claim not in (None, "authenticated"):
        # anon / service_role tokens are not end-user sessions.
        raise _invalid("Token is not a user session.")

    return TokenClaims(
        user_id=user_id,
        email=claims.get("email"),
        role_claim=role_claim,
        session_id=claims.get("session_id"),
        expires_at=int(claims["exp"]),
        aal=claims.get("aal"),
        is_anonymous=bool(claims.get("is_anonymous", False)),
        user_metadata=claims.get("user_metadata") or {},
        app_metadata=claims.get("app_metadata") or {},
        raw=claims,
    )
