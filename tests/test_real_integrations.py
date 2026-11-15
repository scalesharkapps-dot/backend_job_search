"""OPT-IN tests against REAL external services. Skipped unless credentials are given.

These are the only tests that verify live Supabase / RapidAPI behaviour. Use
dedicated TEST accounts and never production user credentials.

    REAL_SUPABASE_URL=https://xxx.supabase.co REAL_SUPABASE_ANON_KEY=... \
    REAL_SUPABASE_TEST_EMAIL=qa@example.com REAL_SUPABASE_TEST_PASSWORD=... \
    python -m pytest tests/test_real_integrations.py -m real_external

    REAL_JSEARCH_API_KEY=... python -m pytest tests/test_real_integrations.py -m real_external

    TEST_DATABASE_URL=postgresql://... python -m pytest   # whole suite on real PostgreSQL
"""

from __future__ import annotations

import os

import httpx
import pytest
from pydantic import SecretStr

from app.core.config import get_settings
from app.services.http import close_http_clients, set_http_client

SUPABASE_URL = os.environ.get("REAL_SUPABASE_URL")
SUPABASE_ANON = os.environ.get("REAL_SUPABASE_ANON_KEY")
TEST_EMAIL = os.environ.get("REAL_SUPABASE_TEST_EMAIL")
TEST_PASSWORD = os.environ.get("REAL_SUPABASE_TEST_PASSWORD")
JSEARCH_KEY = os.environ.get("REAL_JSEARCH_API_KEY")

pytestmark = pytest.mark.real_external


@pytest.fixture
async def real_http():
    # Use real network clients (the conftest mocks are replaced).
    set_http_client("supabase_auth", httpx.AsyncClient(timeout=20))
    set_http_client("jsearch", httpx.AsyncClient(timeout=30))
    yield
    await close_http_clients()


@pytest.mark.skipif(not (SUPABASE_URL and SUPABASE_ANON and TEST_EMAIL and TEST_PASSWORD), reason="Real Supabase credentials not provided")
async def test_real_supabase_login_and_jwt_verification(real_http):
    from app.core.security import jwks_cache, verify_access_token
    from app.services.supabase_auth import SupabaseAuthClient

    s = get_settings()
    s.SUPABASE_URL = SUPABASE_URL
    s.SUPABASE_ANON_KEY = SecretStr(SUPABASE_ANON)
    s.SUPABASE_JWT_ALGORITHMS = os.environ.get("REAL_SUPABASE_JWT_ALGORITHMS", "ES256,RS256")
    if os.environ.get("REAL_SUPABASE_JWT_SECRET"):
        s.SUPABASE_JWT_SECRET = SecretStr(os.environ["REAL_SUPABASE_JWT_SECRET"])
    jwks_cache.clear()
    auth = SupabaseAuthClient(s)
    session = await auth.sign_in_with_password(TEST_EMAIL, TEST_PASSWORD)
    claims = await verify_access_token(session["access_token"], s)
    assert str(claims.user_id) == session["user"]["id"]
    refreshed = await auth.refresh_session(session["refresh_token"])
    assert refreshed["access_token"]
    assert await auth.sign_out(refreshed["access_token"]) is True


@pytest.mark.skipif(not JSEARCH_KEY, reason="Real JSearch API key not provided")
async def test_real_jsearch_search_pakistan_and_international(real_http):
    from app.services.jsearch.client import JSearchClient
    from app.services.jsearch.filters import JobSearchParams
    from app.services.jsearch.normalizer import normalize_job

    s = get_settings()
    s.JSEARCH_API_KEY = SecretStr(JSEARCH_KEY)
    client = JSearchClient(s)
    for params in (JobSearchParams(query="Python Developer", location="Karachi"),
                   JobSearchParams(query="Backend Developer", location="London")):
        page = await client.search(params.provider_params(), None)
        normalized = [n for n in (normalize_job(j) for j in page.jobs) if n]
        # The provider may legitimately return zero jobs; when it returns any they must normalise.
        assert len(normalized) == len([j for j in page.jobs if j.get("job_title")])
