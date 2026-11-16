"""Shared pytest fixtures.

Test categories (see markers in pytest.ini):
* unit: pure functions, no I/O.
* (default) mocked integration: full FastAPI stack against an in-memory SQLite
  database, with Supabase Auth / Storage / JSearch replaced by httpx MockTransport.
* real_db / real_external: opt-in tests against real services (skipped unless
  the corresponding environment variables are provided).

Nothing in this suite contacts Supabase or RapidAPI.
"""

from __future__ import annotations

import os

# Configure the environment BEFORE the application is imported.
os.environ.update(
    {
        # Never load the developer's real .env during tests.
        "SHARKHIRE_ENV_FILE": "",
        # Explicitly pin everything that the real .env might otherwise influence.
        "SUPABASE_JWT_ISSUER": "",
        "SUPABASE_JWKS_URL": "",
        "SUPABASE_JWT_AUDIENCE": "authenticated",
        "AUTH_EMAIL_REDIRECT_URL": "",
        "AUTH_PASSWORD_RESET_REDIRECT_URL": "",
        "APP_ENV": "testing",
        "DEBUG": "false",
        "DATABASE_URL": "sqlite+aiosqlite://",
        "SUPABASE_URL": "https://test-project.supabase.co",
        "SUPABASE_ANON_KEY": "test-anon-key",
        "SUPABASE_SERVICE_ROLE_KEY": "",
        "SUPABASE_JWT_SECRET": "",
        "SUPABASE_JWT_ALGORITHMS": "ES256,RS256",
        "JSEARCH_API_KEY": "test-rapidapi-key",
        "STORAGE_BACKEND": "local",
        "LOG_LEVEL": "WARNING",
        "RATE_LIMIT_ENABLED": "true",
        "APP_SECRET_KEY": "test-secret-key-0123456789abcdefghijklmnop",
    }
)

import asyncio
import sys

if sys.platform == "win32":  # psycopg async requires the selector loop on Windows
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import get_settings
from app.core.database import get_sessionmaker, set_engine
from app.core.rate_limit import limiter
from app.core.security import jwks_cache
from app.models import Base
from app.models.user import User, UserRole
from app.services.http import close_http_clients, set_http_client
from app.services.jsearch.client import outbound_budget, plan_quota

SUPABASE = "https://test-project.supabase.co"
ISSUER = f"{SUPABASE}/auth/v1"
KID = "test-key-1"

_PRIVATE_KEY = ec.generate_private_key(ec.SECP256R1())
_PUBLIC_JWK = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(_PRIVATE_KEY.public_key()))
_PUBLIC_JWK.update({"kid": KID, "alg": "ES256", "use": "sig"})


def make_token(
    user_id: uuid.UUID | str | None = None,
    *,
    email: str = "user@example.com",
    expires_in: int = 3600,
    issuer: str = ISSUER,
    audience: str | None = "authenticated",
    role: str = "authenticated",
    key: Any = None,
    kid: str = KID,
    algorithm: str = "ES256",
    extra: dict[str, Any] | None = None,
    drop: tuple[str, ...] = (),
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": str(user_id or uuid.uuid4()),
        "email": email,
        "role": role,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "exp": now + expires_in,
        "session_id": str(uuid.uuid4()),
        "aal": "aal1",
    }
    if audience is None:
        claims.pop("aud")
    claims.update(extra or {})
    for name in drop:
        claims.pop(name, None)
    return jwt.encode(claims, key or _PRIVATE_KEY, algorithm=algorithm, headers={"kid": kid})


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class MockUpstream:
    """Route-table based httpx MockTransport recording every request."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, Callable[[httpx.Request], httpx.Response]]] = []
        self.requests: list[httpx.Request] = []

    def add(
        self, method: str, path_contains: str, handler: Callable[[httpx.Request], httpx.Response] | httpx.Response
    ) -> None:
        fn = handler if callable(handler) else (lambda _r, h=handler: h)
        self.routes.insert(0, (method.upper(), path_contains, fn))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for method, fragment, fn in self.routes:
            if request.method == method and fragment in str(request.url):
                return fn(request)
        return httpx.Response(404, json={"msg": "no mock route"})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self))


@pytest.fixture(autouse=True)
def _reset_state(tmp_path: Any) -> Any:
    settings = get_settings()
    original = settings.model_dump()
    settings.LOCAL_STORAGE_DIR = str(tmp_path / "storage")
    limiter.reset()
    outbound_budget.reset()
    plan_quota.reset()
    jwks_cache.set_keys({"keys": [_PUBLIC_JWK]})
    yield
    for key, value in original.items():
        setattr(settings, key, value)
    limiter.reset()
    jwks_cache.clear()


# Set TEST_DATABASE_URL (e.g. postgresql://user:pw@localhost:55432/db) to run the
# whole suite against a real PostgreSQL server instead of in-memory SQLite.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def engine(tmp_path: Any) -> AsyncIterator[Any]:
    from sqlalchemy import event
    from sqlalchemy.pool import NullPool

    if TEST_DATABASE_URL:
        from app.core.database import normalize_database_url

        eng = create_async_engine(normalize_database_url(TEST_DATABASE_URL), poolclass=NullPool)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
    else:
        # A file-backed SQLite DB with one connection per session gives each
        # request its own transaction (in-memory StaticPool would share one).
        eng = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
            connect_args={"check_same_thread": False, "timeout": 30},
            poolclass=NullPool,
        )

        @event.listens_for(eng.sync_engine, "connect")
        def _pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    set_engine(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
async def db(engine: Any) -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def supabase() -> MockUpstream:
    upstream = MockUpstream()
    set_http_client("supabase_auth", upstream.client())
    return upstream


@pytest.fixture
def storage_upstream() -> MockUpstream:
    upstream = MockUpstream()
    set_http_client("supabase_storage", upstream.client())
    return upstream


@pytest.fixture
def jsearch() -> MockUpstream:
    upstream = MockUpstream()
    set_http_client("jsearch", upstream.client())
    return upstream


@pytest.fixture
async def client(engine: Any, supabase: MockUpstream) -> AsyncIterator[httpx.AsyncClient]:
    from app.main import app

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    await close_http_clients()


async def create_user(
    db: AsyncSession,
    role: UserRole = UserRole.JOB_SEEKER,
    *,
    email: str | None = None,
    is_active: bool = True,
) -> tuple[User, dict[str, str]]:
    """Insert a user and return it with a valid Authorization header."""
    user_id = uuid.uuid4()
    email = email or f"{role.value}-{user_id.hex[:8]}@example.com"
    user = User(id=user_id, email=email, role=role, is_active=is_active, email_verified=True)
    db.add(user)
    await db.commit()
    return user, auth_header(make_token(user_id, email=email))


@pytest.fixture
def user_factory(db: AsyncSession) -> Callable[..., Any]:
    async def factory(role: UserRole = UserRole.JOB_SEEKER, **kw: Any) -> tuple[User, dict[str, str]]:
        return await create_user(db, role, **kw)

    return factory
