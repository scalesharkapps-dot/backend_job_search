"""Phase 1 — foundation tests (app factory, health, docs, errors, config, logging)."""

from __future__ import annotations

import logging

import pytest
from fastapi import APIRouter
from pydantic import BaseModel

from app.core.config import Settings
from app.core.logging import redact
from app.main import ConfigurationError, create_app


async def test_root_health(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["service"] == "SharkHire API"
    assert "database" not in body  # liveness never claims DB connectivity
    assert r.headers["X-Request-ID"]


async def test_versioned_health(client):
    r = await client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["api_version"] == "v1"


async def test_database_health_connected(client):
    r = await client.get("/api/v1/health/database")
    assert r.status_code == 200
    assert r.json()["database"] == "connected"


async def test_database_health_unreachable(client, monkeypatch):
    from app.api.v1 import health

    async def boom() -> None:
        raise OSError("connection refused to postgres://user:secret@host")  # secret-scan: ignore

    monkeypatch.setattr(health, "check_database", boom)
    r = await client.get("/api/v1/health/database")
    assert r.status_code == 503
    assert r.json()["status"] == "unavailable"
    assert "secret" not in r.text and "refused" not in r.text


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_docs_available(client, path):
    r = await client.get(path)
    assert r.status_code == 200


async def test_openapi_metadata(client):
    spec = (await client.get("/openapi.json")).json()
    assert spec["info"]["title"] == "SharkHire API"
    assert "/api/v1/health" in spec["paths"]


async def test_unknown_route_404_envelope(client):
    r = await client.get("/api/v1/does-not-exist")
    assert r.status_code == 404
    body = r.json()
    assert body["success"] is False
    assert body["error"]["code"] == "not_found"
    assert body["error"]["request_id"] == r.headers["X-Request-ID"]


async def test_method_not_allowed(client):
    r = await client.delete("/api/v1/health")
    assert r.status_code == 405
    assert r.json()["error"]["code"] == "method_not_allowed"


async def test_validation_error_envelope(client):
    r = await client.post("/api/v1/auth/login", json={"email": "not-an-email"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    fields = {d["field"] for d in body["error"]["details"]}
    assert {"email", "password"} <= fields


async def test_unhandled_exception_is_safe():
    app = create_app()
    router = APIRouter()

    @router.get("/boom")
    async def boom() -> None:
        raise RuntimeError("database password=hunter2 leaked")

    app.include_router(router)
    import httpx

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/boom")
    assert r.status_code == 500
    assert "hunter2" not in r.text
    assert r.json()["error"]["code"] == "internal_error"


async def test_request_id_propagation(client):
    r = await client.get("/health", headers={"X-Request-ID": "abc12345-req"})
    assert r.headers["X-Request-ID"] == "abc12345-req"
    r2 = await client.get("/health", headers={"X-Request-ID": "bad id with spaces"})
    assert r2.headers["X-Request-ID"] != "bad id with spaces"


async def test_oversized_body_rejected(client):
    from app.core.config import get_settings

    get_settings().MAX_REQUEST_BODY_BYTES = 100
    r = await client.post("/api/v1/auth/login", content=b"x" * 500, headers={"content-type": "application/json"})
    assert r.status_code == 413


async def test_chunked_oversized_body_rejected(client, user_factory):
    from app.core.config import get_settings

    _, headers = await user_factory()
    get_settings().MAX_REQUEST_BODY_BYTES = 1000

    async def stream():
        for _ in range(10):
            yield b"x" * 500

    async def multipart():
        crlf = b"\r\n"
        yield b"--abc" + crlf + b'Content-Disposition: form-data; name="file"; filename="a.pdf"' + crlf
        yield b"Content-Type: application/pdf" + crlf + crlf
        for _ in range(10):
            yield b"%PDF" + b"x" * 496

    r = await client.post("/api/v1/cv/upload", headers={**headers, "content-type": "multipart/form-data; boundary=abc"}, content=multipart())
    assert r.status_code == 413
    assert r.json()["error"]["code"] == "payload_too_large"
    r = await client.post("/api/v1/auth/login", headers={"content-type": "application/json"}, content=stream())
    assert r.status_code == 413


async def test_security_headers(client):
    r = await client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.unit
def test_production_config_validation():
    s = Settings(APP_ENV="production", _env_file=None, DATABASE_URL=None, DEBUG=True, APP_SECRET_KEY="short")
    problems = s.validate_production()
    assert any("DATABASE_URL" in p for p in problems)
    assert any("APP_SECRET_KEY" in p for p in problems)
    assert any("DEBUG" in p for p in problems)
    with pytest.raises(ConfigurationError):
        create_app(s)


@pytest.mark.unit
def test_production_rejects_sqlite_and_wildcard_cors():
    s = Settings(
        APP_ENV="production",
        _env_file=None,
        DATABASE_URL="sqlite:///x.db",
        CORS_ORIGINS="*",
    )
    problems = s.validate_production()
    assert any("SQLite" in p for p in problems)
    assert any("CORS" in p for p in problems)


@pytest.mark.unit
def test_development_boots_without_external_credentials():
    s = Settings(_env_file=None, APP_ENV="development", DATABASE_URL=None, SUPABASE_URL=None)
    assert s.validate_production() == []
    create_app(s)


@pytest.mark.unit
def test_settings_derived_values():
    s = Settings(_env_file=None, SUPABASE_URL="https://abc.supabase.co/", CORS_ORIGINS="https://a.com, https://b.com")
    assert s.jwt_issuer == "https://abc.supabase.co/auth/v1"
    assert s.jwks_url.endswith("/auth/v1/.well-known/jwks.json")
    assert s.cors_origins == ["https://a.com", "https://b.com"]
    assert "HS256" not in s.jwt_algorithms
    s2 = Settings(_env_file=None, SUPABASE_JWT_SECRET="x" * 40, SUPABASE_JWT_ALGORITHMS="ES256,none,HS256")
    assert s2.jwt_algorithms == ["ES256", "HS256"]


@pytest.mark.unit
def test_invalid_log_level_rejected():
    with pytest.raises(ValueError):
        Settings(_env_file=None, LOG_LEVEL="LOUD")


@pytest.mark.unit
def test_redaction_of_secrets():
    text = (
        "Authorization: Bearer eyJabc.eyJdef.sig password=hunter2 "
        "postgresql+psycopg://user:pw123@db.host:5432/postgres x-rapidapi-key: k123"
    )
    out = redact(text)
    for secret in ("hunter2", "pw123", "eyJabc.eyJdef.sig", "k123"):
        assert secret not in out


@pytest.mark.unit
def test_normalize_database_url():
    from app.core.database import normalize_database_url

    assert normalize_database_url("postgres://u:p@h/db").startswith("postgresql+psycopg://")
    assert normalize_database_url("postgresql://u:p@h/db").startswith("postgresql+psycopg://")
    assert normalize_database_url("sqlite:///a.db") == "sqlite+aiosqlite:///a.db"
    assert normalize_database_url("sqlite+aiosqlite:///a.db", async_driver=False) == "sqlite:///a.db"


async def test_access_log_has_no_query_string(client, caplog):
    caplog.set_level(logging.INFO, logger="sharkhire.access")
    await client.get("/health?token=supersecret")
    assert "supersecret" not in caplog.text


class _Unused(BaseModel):
    pass


@pytest.mark.unit
def test_database_url_without_scheme_rejected_without_leaking():
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, DATABASE_URL="//postgres:topsecretpw@db.example.supabase.co:5432/postgres")  # secret-scan: ignore
    assert "postgresql://" in str(exc.value)
    assert "topsecretpw" not in str(exc.value)


@pytest.mark.unit
def test_every_table_is_covered_by_rls_lockdown():
    """New tables must be added to an RLS/privilege lockdown migration (Supabase Data API exposure)."""
    import importlib.util
    import pathlib

    from app.models import Base

    # 0007 locked down the original tables; later migrations lock down the tables they add
    # and declare them in a module-level TABLES tuple.
    covered: set[str] = set()
    for path in sorted(pathlib.Path("alembic/versions").glob("*.py")):
        spec = importlib.util.spec_from_file_location(f"mig_{path.stem}", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tables = getattr(module, "TABLES", ())
        if tables and "ENABLE ROW LEVEL SECURITY" in path.read_text(encoding="utf-8"):
            covered |= set(tables)
    missing = set(Base.metadata.tables) - covered
    assert not missing, f"Tables without RLS lockdown: {sorted(missing)}"


@pytest.mark.unit
def test_settings_errors_never_include_values():
    from app.core.config import SettingsError, load_settings

    for kwargs, expected in [
        ({"DATABASE_URL": "//postgres:topsecretpw@db.example.supabase.co/postgres"}, "DATABASE_URL must start with postgresql://"),  # secret-scan: ignore
        ({"LOG_LEVEL": "LOUD"}, "LOG_LEVEL"),
    ]:
        with pytest.raises(SettingsError) as exc:
            load_settings(_env_file=None, SUPABASE_SERVICE_ROLE_KEY="sb_secret_shouldneverappear", **kwargs)
        text = str(exc.value)
        assert expected in text
        assert "topsecretpw" not in text and "sb_secret_shouldneverappear" not in text and "input_value" not in text


@pytest.mark.unit
def test_no_source_file_is_git_ignored():
    """Regression: an unanchored "storage/" rule once kept app/services/storage/ out of the repository,
    so CI and Docker builds could not import the app. Every source file must be committable."""
    import pathlib
    import shutil
    import subprocess

    git = shutil.which("git")
    if git is None or not pathlib.Path(".git").exists():
        pytest.skip("not a git checkout")
    sources = [str(p) for d in ("app", "alembic", "scripts", "tests") for p in pathlib.Path(d).rglob("*.py")
               if "__pycache__" not in p.parts]
    # Fixed command and repository-relative paths only (no external input).
    result = subprocess.run([git, "check-ignore", "--no-index", *sources], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.stdout.strip() == "", f"git ignores source files:\n{result.stdout}"
