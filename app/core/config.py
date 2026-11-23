"""Application configuration loaded from environment variables via Pydantic Settings.

All secrets come from the environment (or a local, git-ignored ``.env`` file).
Nothing in this module may contain a real credential.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "testing", "staging", "production"]

_INSECURE_DEV_SECRET = "dev-insecure-secret-change-me-0123456789abcdef"


class Settings(BaseSettings):
    """Typed application settings.

    Values that only matter for later features (Supabase, JSearch, storage) are
    optional so the service can boot locally without every credential. Production
    requirements are enforced by :meth:`validate_production`.
    """

    model_config = SettingsConfigDict(
        # SHARKHIRE_ENV_FILE overrides the dotenv path; an empty value disables it (the test suite does
        # this so it never reads a developer's real credentials).
        env_file=os.environ.get("SHARKHIRE_ENV_FILE", ".env") or None,
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Application ----
    APP_NAME: str = "SharkHire API"
    APP_VERSION: str = "1.0.0"
    APP_ENV: Environment = "development"
    DEBUG: bool = False
    API_V1_PREFIX: str = "/api/v1"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False
    # Used to sign short-lived local download tokens. MUST be set in production.
    APP_SECRET_KEY: SecretStr = SecretStr(_INSECURE_DEV_SECRET)
    # Comma-separated list. Native Flutter apps do not need CORS; only list web origins.
    CORS_ORIGINS: str = ""
    # Enable the interactive docs (/docs, /redoc, /openapi.json).
    ENABLE_DOCS: bool = True
    # Reject requests whose Content-Length exceeds this many bytes (uploads included).
    MAX_REQUEST_BODY_BYTES: int = 12 * 1024 * 1024
    # Trust X-Forwarded-For for client IP (enable only behind a trusted proxy).
    TRUST_PROXY_HEADERS: bool = False

    # ---- Database ----
    # Supabase: postgresql+psycopg://USER:PASSWORD@HOST:PORT/postgres?sslmode=require
    DATABASE_URL: SecretStr | None = None
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DB_POOL_TIMEOUT: int = 30
    DB_POOL_RECYCLE: int = 1800
    DB_CONNECT_TIMEOUT: int = 10
    DB_ECHO: bool = False
    # Set true when connecting through Supabase's transaction pooler (port 6543 / PgBouncer).
    DB_USE_PGBOUNCER: bool = False

    # ---- Supabase ----
    SUPABASE_URL: str | None = None
    SUPABASE_ANON_KEY: SecretStr | None = None
    SUPABASE_SERVICE_ROLE_KEY: SecretStr | None = None
    # Legacy HS256 JWT secret. Leave empty when the project uses asymmetric signing keys.
    SUPABASE_JWT_SECRET: SecretStr | None = None
    SUPABASE_JWT_ISSUER: str | None = None  # default: {SUPABASE_URL}/auth/v1
    SUPABASE_JWT_AUDIENCE: str | None = "authenticated"
    SUPABASE_JWKS_URL: str | None = None  # default: {SUPABASE_URL}/auth/v1/.well-known/jwks.json
    # Accepted algorithms. ES256/RS256 are verified via JWKS. HS256 is accepted only when listed here
    # explicitly AND SUPABASE_JWT_SECRET is set (legacy projects only).
    SUPABASE_JWT_ALGORITHMS: str = "ES256,RS256"
    JWKS_CACHE_TTL_SECONDS: int = 600
    JWT_LEEWAY_SECONDS: int = 10
    AUTH_HTTP_TIMEOUT_SECONDS: float = 10.0
    # Deep link used in verification and recovery e-mails (e.g. sharkhire://auth-callback).
    AUTH_EMAIL_REDIRECT_URL: str | None = None
    AUTH_PASSWORD_RESET_REDIRECT_URL: str | None = None
    PASSWORD_MIN_LENGTH: int = 8
    # Signup verification e-mails: 1 initial + at most this many successful resends per cycle
    # (persisted in PostgreSQL, see app/services/email_verification.py).
    EMAIL_VERIFICATION_MAX_RESENDS: int = Field(default=2, ge=0, le=10)
    # Minimum gap between two verification e-mails to the same address (Supabase's own
    # per-user "max frequency" defaults to 60 s; keep this >= that value).
    EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS: int = Field(default=60, ge=0)
    # A new verification cycle (fresh resend allowance) starts this many hours after the
    # current one began. 0 = never (the limit is permanent until the e-mail is verified).
    EMAIL_VERIFICATION_CYCLE_HOURS: int = Field(default=24, ge=0)

    # ---- App access: 3-day app-managed trial + RevenueCat subscriptions ----
    # Master switch for the paywall on protected routes (status/sync/webhook keep working).
    SUBSCRIPTION_ENFORCEMENT_ENABLED: bool = True
    # Comma-separated roles that never need a trial/subscription (staff accounts).
    SUBSCRIPTION_EXEMPT_ROLES: str = "admin"
    # RevenueCat secret API key (sk_...), server-side only. Used for GET /v1/subscribers/{id}.
    REVENUECAT_SECRET_API_KEY: SecretStr | None = None
    # Exact value configured as the webhook "Authorization header" in the RevenueCat dashboard.
    REVENUECAT_WEBHOOK_AUTHORIZATION: SecretStr | None = None
    REVENUECAT_API_BASE_URL: str = "https://api.revenuecat.com/v1"
    REVENUECAT_ENTITLEMENT_ID: str = "app_access"
    REVENUECAT_OFFERING_ID: str = "default"
    # Comma-separated store product identifiers per plan (Play and App Store ids may differ).
    REVENUECAT_PRODUCT_INTERNATIONAL: str = ""
    REVENUECAT_PRODUCT_PAKISTAN: str = ""
    # Accept SANDBOX purchases/events. None = only outside production.
    REVENUECAT_ALLOW_SANDBOX: bool | None = None
    REVENUECAT_TIMEOUT_SECONDS: float = 10.0
    REVENUECAT_MAX_RETRIES: int = Field(default=2, ge=0, le=5)
    REVENUECAT_RETRY_BACKOFF_SECONDS: float = Field(default=0.5, ge=0)

    # ---- Storage ----
    STORAGE_BACKEND: Literal["local", "supabase"] = "local"
    LOCAL_STORAGE_DIR: str = "storage"
    SUPABASE_STORAGE_CV_BUCKET: str = "cvs"
    SUPABASE_STORAGE_MEDIA_BUCKET: str = "media"
    SIGNED_URL_TTL_SECONDS: int = 300
    PUBLIC_BASE_URL: str = "http://localhost:8000"

    # ---- Uploads ----
    CV_MAX_FILE_SIZE_MB: int = 5
    IMAGE_MAX_FILE_SIZE_MB: int = 2
    CV_MAX_FILES_PER_USER: int = 10
    CV_MAX_PDF_PAGES: int = 30

    # ---- JSearch / RapidAPI ----
    JSEARCH_API_KEY: SecretStr | None = None
    JSEARCH_API_HOST: str = "jsearch.p.rapidapi.com"
    JSEARCH_BASE_URL: str = "https://jsearch.p.rapidapi.com"
    JSEARCH_SEARCH_PATH: str = "/search-v2"
    JSEARCH_DETAILS_PATH: str = "/job-details"
    JSEARCH_TIMEOUT_SECONDS: float = 15.0
    JSEARCH_CONNECT_TIMEOUT_SECONDS: float = 5.0
    JSEARCH_MAX_RETRIES: int = 2
    JSEARCH_BACKOFF_BASE_SECONDS: float = 0.5
    JSEARCH_MAX_RETRY_AFTER_SECONDS: float = 10.0
    JSEARCH_CACHE_TTL_SECONDS: int = 3600
    JSEARCH_DETAILS_REFRESH_SECONDS: int = 86400
    JSEARCH_MAX_NUM_PAGES: int = 3
    # Process-wide cap on outbound JSearch calls (protects the RapidAPI subscription).
    JSEARCH_GLOBAL_CALLS_PER_MINUTE: int = 30
    # Plan quota guard: RapidAPI reports the remaining requests of the billing period in
    # x-ratelimit-requests-remaining. Calls stop when that many (or fewer) remain, so the
    # quota can never be fully drained by a burst. 0 disables the reserve.
    JSEARCH_QUOTA_RESERVE: int = 5
    JSEARCH_QUOTA_WARN_BELOW: int = 25
    # Used only when no country is given and none can be inferred from the location.
    # Empty = let the provider decide (searches are NOT restricted to Pakistan by default).
    JSEARCH_DEFAULT_COUNTRY: str = ""

    # ---- Rate limiting (per client; process-local) ----
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10
    RATE_LIMIT_EMAIL_PER_HOUR: int = 5
    # Password-recovery requests per e-mail address (any account or none) per hour.
    RATE_LIMIT_PASSWORD_RESET_PER_EMAIL_PER_HOUR: int = 3
    RATE_LIMIT_UPLOAD_PER_MINUTE: int = 10
    RATE_LIMIT_SEARCH_PER_MINUTE: int = 20
    RATE_LIMIT_RECOMMENDATION_PER_MINUTE: int = 20
    RATE_LIMIT_WRITE_PER_MINUTE: int = 60
    RATE_LIMIT_REPORT_PER_HOUR: int = 20

    # ---- Recommendations ----
    REC_WEIGHT_REQUIRED_SKILLS: float = 40
    REC_WEIGHT_PREFERRED_SKILLS: float = 15
    REC_WEIGHT_EXPERIENCE: float = 20
    REC_WEIGHT_TITLE: float = 10
    REC_WEIGHT_LOCATION: float = 10
    REC_WEIGHT_EMPLOYMENT: float = 5
    REC_MAX_CANDIDATE_JOBS: int = 500
    REC_JOB_MAX_AGE_DAYS: int = 45
    REC_MIN_SCORE: int = 20
    REC_HISTORY_LIMIT: int = 200

    # ---- Employer / moderation ----
    JOB_MODERATION_ENABLED: bool = True

    @field_validator("API_V1_PREFIX")
    @classmethod
    def _prefix(cls, value: str) -> str:
        value = "/" + value.strip("/")
        return value

    @model_validator(mode="after")
    def _database_url_scheme(self) -> Settings:
        # Model-level so that Pydantic's error shows the masked SecretStr, never the password.
        if self.DATABASE_URL is None:
            return self
        url = self.DATABASE_URL.get_secret_value().strip()
        if not url:
            self.DATABASE_URL = None
            return self
        if not url.startswith(("postgres://", "postgresql://", "postgresql+psycopg://", "sqlite://", "sqlite+aiosqlite://")):
            raise ValueError("DATABASE_URL must start with postgresql:// (e.g. postgresql://user:password@host:5432/postgres)")
        self.DATABASE_URL = SecretStr(url)
        return self

    @field_validator("LOG_LEVEL")
    @classmethod
    def _log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL must be CRITICAL, ERROR, WARNING, INFO or DEBUG")
        return value

    @field_validator(
        "REVENUECAT_ALLOW_SANDBOX", "REVENUECAT_SECRET_API_KEY", "REVENUECAT_WEBHOOK_AUTHORIZATION", mode="before"
    )
    @classmethod
    def _empty_is_unset(cls, value: object) -> object:
        # ".env.example" style "NAME=" lines mean "not configured".
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def _weights_positive(self) -> Settings:
        weights = self.recommendation_weights
        if any(w < 0 for w in weights.values()) or sum(weights.values()) <= 0:
            raise ValueError("Recommendation weights must be non-negative and sum to > 0")
        return self

    # ---- Derived values ----
    @property
    def is_production(self) -> bool:
        return self.APP_ENV == "production"

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def database_url(self) -> str | None:
        return self.DATABASE_URL.get_secret_value() if self.DATABASE_URL else None

    @property
    def supabase_url(self) -> str | None:
        return self.SUPABASE_URL.rstrip("/") if self.SUPABASE_URL else None

    @property
    def subscription_exempt_roles(self) -> set[str]:
        return {r.strip().lower() for r in self.SUBSCRIPTION_EXEMPT_ROLES.split(",") if r.strip()}

    @property
    def revenuecat_allow_sandbox(self) -> bool:
        if self.REVENUECAT_ALLOW_SANDBOX is None:
            return not self.is_production
        return self.REVENUECAT_ALLOW_SANDBOX

    @staticmethod
    def _csv(value: str) -> list[str]:
        return [v.strip() for v in value.split(",") if v.strip()]

    @property
    def revenuecat_products_international(self) -> list[str]:
        return self._csv(self.REVENUECAT_PRODUCT_INTERNATIONAL)

    @property
    def revenuecat_products_pakistan(self) -> list[str]:
        return self._csv(self.REVENUECAT_PRODUCT_PAKISTAN)

    @property
    def password_reset_redirect_url(self) -> str | None:
        """The single ``redirect_to`` used for every password-recovery e-mail: the app deep link
        ``AUTH_PASSWORD_RESET_REDIRECT_URL`` (e.g. ``sharkhire://auth/reset-password``), as configured."""
        return (self.AUTH_PASSWORD_RESET_REDIRECT_URL or "").strip() or None

    def auth_redirect_warnings(self) -> list[str]:
        """Redirect settings that make Supabase fall back to its Site URL (often localhost:3000).

        Names only, never values. Logged at startup; never blocks boot.
        """
        warnings: list[str] = []
        for name, value in (
            ("AUTH_EMAIL_REDIRECT_URL", (self.AUTH_EMAIL_REDIRECT_URL or "").strip()),
            ("AUTH_PASSWORD_RESET_REDIRECT_URL", self.password_reset_redirect_url or ""),
        ):
            if not value:
                warnings.append(f"{name} is not set: Supabase e-mail links will open the project's Site URL")
            elif "://" not in value:
                warnings.append(f"{name} is not an absolute URL/deep link: Supabase will ignore it")
            elif self.is_production and any(h in value.lower() for h in ("localhost", "127.0.0.1", "0.0.0.0")):  # noqa: S104
                warnings.append(f"{name} points to a local address in production")
        return warnings

    def subscription_config_warnings(self) -> list[str]:
        """Missing billing configuration (logged at startup; never blocks boot)."""
        warnings: list[str] = []
        if not self.SUBSCRIPTION_ENFORCEMENT_ENABLED:
            return warnings
        if not self.REVENUECAT_WEBHOOK_AUTHORIZATION:
            warnings.append("REVENUECAT_WEBHOOK_AUTHORIZATION is not set: webhooks are rejected")
        if not self.REVENUECAT_SECRET_API_KEY:
            warnings.append("REVENUECAT_SECRET_API_KEY is not set: /subscriptions/sync and /restore are unavailable")
        if not (self.revenuecat_products_international or self.revenuecat_products_pakistan):
            warnings.append("REVENUECAT_PRODUCT_* are not set: /subscriptions/plans lists no product ids")
        return warnings

    @property
    def jwt_issuer(self) -> str | None:
        if self.SUPABASE_JWT_ISSUER:
            return self.SUPABASE_JWT_ISSUER
        return f"{self.supabase_url}/auth/v1" if self.supabase_url else None

    @property
    def jwks_url(self) -> str | None:
        if self.SUPABASE_JWKS_URL:
            return self.SUPABASE_JWKS_URL
        return f"{self.supabase_url}/auth/v1/.well-known/jwks.json" if self.supabase_url else None

    @property
    def jwt_algorithms(self) -> list[str]:
        requested = [a.strip().upper() for a in self.SUPABASE_JWT_ALGORITHMS.split(",") if a.strip()]
        # Never accept "none" or HS384/HS512. HS256 (legacy shared secret) requires BOTH an explicit
        # "HS256" entry AND a configured secret: a leftover legacy secret alone must not let anyone
        # holding it mint tokens for a project that signs with asymmetric keys.
        algs = [a for a in requested if a not in {"NONE", "HS256", "HS384", "HS512"}]
        has_secret = self.SUPABASE_JWT_SECRET is not None and bool(self.SUPABASE_JWT_SECRET.get_secret_value())
        if "HS256" in requested and has_secret:
            algs.append("HS256")
        return algs

    @property
    def cv_max_bytes(self) -> int:
        return self.CV_MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def image_max_bytes(self) -> int:
        return self.IMAGE_MAX_FILE_SIZE_MB * 1024 * 1024

    @property
    def recommendation_weights(self) -> dict[str, float]:
        return {
            "required_skills": self.REC_WEIGHT_REQUIRED_SKILLS,
            "preferred_skills": self.REC_WEIGHT_PREFERRED_SKILLS,
            "experience": self.REC_WEIGHT_EXPERIENCE,
            "title": self.REC_WEIGHT_TITLE,
            "location": self.REC_WEIGHT_LOCATION,
            "employment": self.REC_WEIGHT_EMPLOYMENT,
        }

    def validate_production(self) -> list[str]:
        """Return a list of configuration problems that must block a production start."""
        problems: list[str] = []
        if not self.is_production:
            return problems
        if not self.database_url:
            problems.append("DATABASE_URL is required")
        elif self.database_url.startswith("sqlite"):
            problems.append("SQLite is not supported in production; use Supabase PostgreSQL")
        if self.APP_SECRET_KEY.get_secret_value() == _INSECURE_DEV_SECRET or len(
            self.APP_SECRET_KEY.get_secret_value()
        ) < 32:
            problems.append("APP_SECRET_KEY must be set to a random value of at least 32 chars")
        if not self.supabase_url:
            problems.append("SUPABASE_URL is required")
        if not self.SUPABASE_ANON_KEY:
            problems.append("SUPABASE_ANON_KEY is required")
        if not self.jwt_algorithms:
            problems.append("No JWT algorithms configured")
        if self.DEBUG:
            problems.append("DEBUG must be false in production")
        if "*" in self.cors_origins:
            problems.append("Wildcard CORS origins are not allowed in production")
        if self.STORAGE_BACKEND == "supabase" and not self.SUPABASE_SERVICE_ROLE_KEY:
            problems.append("SUPABASE_SERVICE_ROLE_KEY is required for Supabase Storage")
        if self.STORAGE_BACKEND == "local":
            problems.append("STORAGE_BACKEND=local is not suitable for production; use supabase")
        return problems


class SettingsError(RuntimeError):
    """Invalid configuration. The message never contains configuration values."""


def load_settings(**overrides: object) -> Settings:
    """Build Settings, converting validation errors into a secret-free message.

    Pydantic's own error text embeds the (truncated) raw input, which could expose
    credentials in startup logs.
    """
    try:
        return Settings(**overrides)  # type: ignore[arg-type]
    except ValidationError as exc:
        lines = []
        for err in exc.errors(include_input=False, include_url=False):
            field = ".".join(str(p) for p in err.get("loc", ())) or "settings"
            lines.append(f"{field}: {err.get('msg', 'invalid value')}")
        raise SettingsError("Invalid configuration:\n  - " + "\n  - ".join(lines)) from None


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return load_settings()
