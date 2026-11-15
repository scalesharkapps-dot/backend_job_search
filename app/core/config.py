"""Application configuration loaded from environment variables via Pydantic Settings.

All secrets come from the environment (or a local, git-ignored ``.env`` file).
Nothing in this module may contain a real credential.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
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
        env_file=".env",
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
    # Asymmetric algorithms accepted from the JWKS. HS256 is added only when a secret is set.
    SUPABASE_JWT_ALGORITHMS: str = "ES256,RS256"
    JWKS_CACHE_TTL_SECONDS: int = 600
    JWT_LEEWAY_SECONDS: int = 10
    AUTH_HTTP_TIMEOUT_SECONDS: float = 10.0
    # Deep link used in verification and recovery e-mails (e.g. sharkhire://auth-callback).
    AUTH_EMAIL_REDIRECT_URL: str | None = None
    AUTH_PASSWORD_RESET_REDIRECT_URL: str | None = None
    PASSWORD_MIN_LENGTH: int = 8

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
    # Used only when no country is given and none can be inferred from the location.
    # Empty = let the provider decide (searches are NOT restricted to Pakistan by default).
    JSEARCH_DEFAULT_COUNTRY: str = ""

    # ---- Rate limiting (per client; process-local) ----
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_AUTH_PER_MINUTE: int = 10
    RATE_LIMIT_EMAIL_PER_HOUR: int = 5
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

    @field_validator("LOG_LEVEL")
    @classmethod
    def _log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("LOG_LEVEL must be CRITICAL, ERROR, WARNING, INFO or DEBUG")
        return value

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
        algs = [a.strip().upper() for a in self.SUPABASE_JWT_ALGORITHMS.split(",") if a.strip()]
        # Never accept "none"; HMAC only when a secret is configured.
        algs = [a for a in algs if a not in {"NONE", "HS256", "HS384", "HS512"}]
        if self.SUPABASE_JWT_SECRET is not None and self.SUPABASE_JWT_SECRET.get_secret_value():
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


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()
