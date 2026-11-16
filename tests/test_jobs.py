"""Phase 4 — JSearch integration tests.

ALL JSearch HTTP traffic is mocked (httpx MockTransport). These tests verify
request construction, normalisation, caching, retries and error handling — they
do NOT prove that the real RapidAPI subscription works (see tests/test_real_integrations.py).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.base import utcnow
from app.models.job import Job, JobSearchCache
from app.services.jsearch.client import JSearchClient
from app.services.jsearch.filters import JobSearchParams, infer_country
from app.services.jsearch.normalizer import normalize_job, safe_url, stable_external_id
from tests.jsearch_data import raw_job, search_payload


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)

    monkeypatch.setattr("app.services.jsearch.client.asyncio.sleep", fake_sleep)
    return calls


def params_of(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


async def search(client, headers, **params):
    return await client.get("/api/v1/jobs/search", headers=headers, params={"query": "Python Developer", **params})


# ---------------- Search endpoint ----------------
async def test_search_pakistan(client, jsearch, user_factory, db):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("a"), raw_job("b", job_title="Backend Engineer")])))
    r = await search(client, headers, location="Karachi")
    assert r.status_code == 200, r.text
    body = r.json()["data"]
    assert [i["job_title"] for i in body["items"]] == ["Python Developer", "Backend Engineer"]
    assert body["pagination"] == {
        "page_size": 2, "has_more": True, "next_cursor": "next-cursor-1", "total_results": None,
        "source": "jsearch", "cached": False, "stale": False, "country": "pk",
    }
    sent = params_of(jsearch.requests[-1])
    assert sent["query"] == "Python Developer in Karachi"
    assert sent["country"] == "pk"
    req = jsearch.requests[-1]
    assert req.headers["X-RapidAPI-Key"] == "test-rapidapi-key"
    assert req.headers["X-RapidAPI-Host"] == "jsearch.p.rapidapi.com"
    assert "test-rapidapi-key" not in r.text
    item = body["items"][0]
    assert item["job_country"] == "PK" and item["job_is_remote"] is False and item["is_external"] is True
    assert item["job_min_salary"] is None  # missing salary is not zero
    assert await db.scalar(select(func.count()).select_from(Job)) == 2


@pytest.mark.parametrize(
    "location,country,expected",
    [("London", None, "gb"), ("Toronto", None, "ca"), ("Berlin", None, "de"), ("Dubai", None, "ae"),
     ("Somewhere", "us", "us"), ("Lahore", None, "pk"), (None, "au", "au")],
)
async def test_search_international(client, jsearch, user_factory, location, country, expected):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job()])))
    params = {k: v for k, v in {"location": location, "country": country}.items() if v}
    r = await search(client, headers, **params)
    assert r.status_code == 200
    assert params_of(jsearch.requests[-1])["country"] == expected


async def test_search_without_location_is_not_restricted_to_pakistan(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([])))
    await search(client, headers)
    assert "country" not in params_of(jsearch.requests[-1])


async def test_search_remote_and_filters(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job(job_is_remote=True)])))
    r = await client.get(
        "/api/v1/jobs/search",
        headers=headers,
        params=[("query", "AI Engineer"), ("remote_jobs_only", "true"), ("employment_types", "FULLTIME"),
                ("employment_types", "CONTRACTOR"), ("date_posted", "week"), ("experience", "under_3_years_experience"),
                ("num_pages", "2"), ("exclude_job_publishers", "BeeBe")],
    )
    assert r.status_code == 200, r.text
    sent = params_of(jsearch.requests[-1])
    assert sent["work_from_home"] == "true"
    assert sent["employment_types"] == "CONTRACTOR,FULLTIME"
    assert sent["date_posted"] == "week"
    assert sent["job_requirements"] == "under_3_years_experience"
    assert sent["num_pages"] == "2"
    assert sent["exclude_job_publishers"] == "BeeBe"
    assert r.json()["data"]["items"][0]["job_is_remote"] is True


async def test_search_cursor_pagination(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", lambda req: httpx.Response(
        200, json=search_payload([raw_job("p2")], cursor=None) if params_of(req).get("cursor") == "next-cursor-1"
        else search_payload([raw_job("p1")]),
    ))
    first = (await search(client, headers)).json()["data"]
    second = (await search(client, headers, cursor=first["pagination"]["next_cursor"])).json()["data"]
    assert second["items"][0]["external_job_id"] == "p2"
    assert second["pagination"]["has_more"] is False and second["pagination"]["next_cursor"] is None


async def test_legacy_search_path_uses_page_numbers(client, jsearch, user_factory):
    get_settings().JSEARCH_SEARCH_PATH = "/search"
    _, headers = await user_factory()
    jsearch.add("GET", "/search?", httpx.Response(200, json={"status": "OK", "data": [raw_job(str(i)) for i in range(10)]}))
    r = (await search(client, headers)).json()["data"]
    assert params_of(jsearch.requests[-1])["page"] == "1"
    assert r["pagination"]["next_cursor"] == "2"
    await search(client, headers, cursor="2")
    assert params_of(jsearch.requests[-1])["page"] == "2"


async def test_search_empty_results(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([], cursor=None)))
    r = await search(client, headers)
    assert r.status_code == 200
    assert r.json()["data"]["items"] == [] and r.json()["message"] == "No jobs found for this search."


@pytest.mark.parametrize(
    "params",
    [{"query": "a"}, {"query": "x" * 201}, {"country": "PAK"}, {"date_posted": "yesterday"},
     {"num_pages": "50"}, {"employment_types": "FOREVER"}, {"query": "<script>alert(1)</script>"},
     {"radius": "0"}, {"language": "english"}],
)
async def test_search_invalid_params(client, jsearch, user_factory, params):
    _, headers = await user_factory()
    r = await client.get("/api/v1/jobs/search", headers=headers, params={"query": "Python", **params})
    assert r.status_code == 422, (params, r.text)
    assert not jsearch.requests


async def test_search_requires_auth(client, jsearch):
    r = await client.get("/api/v1/jobs/search", params={"query": "Python"})
    assert r.status_code == 401
    assert not jsearch.requests


async def test_search_cache_hit_avoids_provider(client, jsearch, user_factory, db):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job()])))
    await search(client, headers, location="Lahore")
    second = (await search(client, headers, location="Lahore")).json()["data"]
    assert len(jsearch.requests) == 1
    assert second["pagination"]["cached"] is True
    entry = await db.scalar(select(JobSearchCache))
    await db.refresh(entry)
    assert entry.hit_count == 1


async def test_search_cache_expiry_refetches(client, jsearch, user_factory, db):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job()])))
    await search(client, headers)
    entry = await db.scalar(select(JobSearchCache))
    entry.expires_at = utcnow() - timedelta(seconds=1)
    await db.commit()
    r = (await search(client, headers)).json()["data"]
    assert len(jsearch.requests) == 2 and r["pagination"]["cached"] is False


async def test_stale_cache_served_when_provider_down(client, jsearch, user_factory, db):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job()])))
    await search(client, headers)
    entry = await db.scalar(select(JobSearchCache))
    entry.expires_at = utcnow() - timedelta(seconds=1)
    await db.commit()
    jsearch.add("GET", "/search-v2", httpx.Response(503))
    r = await search(client, headers)
    assert r.status_code == 200
    assert r.json()["data"]["pagination"]["stale"] is True
    assert "temporarily unavailable" in r.json()["message"]


async def test_duplicate_detection_and_update(client, jsearch, user_factory, db):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("same"), raw_job("same")])))
    first = (await search(client, headers)).json()["data"]["items"]
    assert len(first) == 1
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("same", job_title="Senior Python Developer")])))
    second = (await search(client, headers, location="Islamabad")).json()["data"]["items"]
    assert second[0]["id"] == first[0]["id"]  # stable internal id
    assert second[0]["job_title"] == "Senior Python Developer"
    assert await db.scalar(select(func.count()).select_from(Job)) == 1


async def test_same_title_different_companies_not_merged(client, jsearch, user_factory, db):
    _, headers = await user_factory()
    jobs = [raw_job("1", employer_name="A"), raw_job("2", employer_name="B")]
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload(jobs)))
    await search(client, headers)
    assert await db.scalar(select(func.count()).select_from(Job)) == 2


async def test_concurrent_identical_searches_coalesce(client, jsearch, user_factory):
    _, headers = await user_factory()

    async def slow(_req):
        await asyncio.sleep(0)
        return httpx.Response(200, json=search_payload([raw_job()]))

    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job()])))
    results = await asyncio.gather(*(search(client, headers, location="Quetta") for _ in range(3)))
    assert all(r.status_code == 200 for r in results)
    assert len(jsearch.requests) == 1


async def test_local_company_and_salary_filters(client, jsearch, user_factory):
    _, headers = await user_factory()
    jobs = [
        raw_job("1", employer_name="Systems Ltd", job_min_salary=100000, job_max_salary=150000, job_salary_period="MONTH"),
        raw_job("2", employer_name="Other Co", job_min_salary=50000, job_max_salary=60000),
    ]
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload(jobs)))
    r = (await search(client, headers, company="systems")).json()["data"]["items"]
    assert [i["external_job_id"] for i in r] == ["1"]
    assert params_of(jsearch.requests[-1])["query"] == "Python Developer at systems"
    r = (await search(client, headers, company="systems", min_salary=120000)).json()["data"]["items"]
    assert [i["external_job_id"] for i in r] == ["1"]


# ---------------- Provider errors ----------------
@pytest.mark.parametrize(
    "response,status,code",
    [
        (httpx.Response(401, json={"message": "Invalid API key"}), 503, "job_search_unavailable"),
        (httpx.Response(403, json={"message": "You are not subscribed"}), 503, "job_search_unavailable"),
        (httpx.Response(400, json={"message": "bad"}), 400, "job_search_invalid_request"),
        (httpx.Response(404), 400, "job_search_invalid_request"),
        (httpx.Response(500), 503, "job_search_unavailable"),
        (httpx.Response(502), 503, "job_search_unavailable"),
        (httpx.Response(503), 503, "job_search_unavailable"),
        (httpx.Response(429), 429, "job_search_rate_limited"),
        (httpx.Response(200, content=b"<html>not json"), 502, "job_search_bad_response"),
        (httpx.Response(200, json={"status": "OK", "data": "unexpected"}), 502, "job_search_bad_response"),
        (httpx.Response(200, json={"status": "ERROR", "error": {"message": "x"}}), 400, "job_search_invalid_request"),
        (httpx.Response(418), 502, "job_search_bad_response"),
    ],
)
async def test_provider_errors(client, jsearch, user_factory, response, status, code):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", response)
    r = await search(client, headers)
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code
    assert "Invalid API key" not in r.text and "subscribed" not in r.text


async def test_auth_errors_not_retried(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(401))
    await search(client, headers)
    assert len(jsearch.requests) == 1


async def test_transient_errors_retried_with_backoff(client, jsearch, user_factory, no_sleep):
    _, headers = await user_factory()
    responses = iter([httpx.Response(503), httpx.Response(502), httpx.Response(200, json=search_payload([raw_job()]))])
    jsearch.add("GET", "/search-v2", lambda _r: next(responses))
    r = await search(client, headers)
    assert r.status_code == 200
    assert len(jsearch.requests) == 3
    assert len(no_sleep) == 2 and no_sleep[1] > no_sleep[0]  # exponential


async def test_retry_after_respected(client, jsearch, user_factory, no_sleep):
    _, headers = await user_factory()
    responses = iter([httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json=search_payload([]))])
    jsearch.add("GET", "/search-v2", lambda _r: next(responses))
    assert (await search(client, headers)).status_code == 200
    assert no_sleep == [3.0]


async def test_long_retry_after_not_waited(client, jsearch, user_factory, no_sleep):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(429, headers={"Retry-After": "3600"}))
    r = await search(client, headers)
    assert r.status_code == 429 and r.headers["Retry-After"] == "3600"
    assert no_sleep == [] and len(jsearch.requests) == 1


@pytest.mark.parametrize("exc", [httpx.ConnectTimeout("t"), httpx.ReadTimeout("t"), httpx.ConnectError("down")])
async def test_network_failures(client, jsearch, user_factory, exc):
    _, headers = await user_factory()

    def fail(_r):
        raise exc

    jsearch.add("GET", "/search-v2", fail)
    r = await search(client, headers)
    assert r.status_code == 503
    assert len(jsearch.requests) == get_settings().JSEARCH_MAX_RETRIES + 1


async def test_missing_api_key(client, jsearch, user_factory):
    from pydantic import SecretStr

    get_settings().JSEARCH_API_KEY = SecretStr("")
    _, headers = await user_factory()
    r = await search(client, headers)
    assert r.status_code == 503 and r.json()["error"]["code"] == "job_search_not_configured"
    assert not jsearch.requests


async def test_outbound_budget_protects_subscription(client, jsearch, user_factory):
    get_settings().JSEARCH_GLOBAL_CALLS_PER_MINUTE = 2
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([])))
    codes = [(await search(client, headers, location=f"City{i}")).status_code for i in range(3)]
    assert codes == [200, 200, 429]
    assert len(jsearch.requests) == 2


async def test_per_user_search_rate_limit(client, jsearch, user_factory):
    get_settings().RATE_LIMIT_SEARCH_PER_MINUTE = 2
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([])))
    codes = [(await search(client, headers)).status_code for _ in range(3)]
    assert codes[-1] == 429


# ---------------- Details / filters / suggestions ----------------
async def _seed(client, jsearch, headers, job=None):
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([job or raw_job()])))
    return (await search(client, headers)).json()["data"]["items"][0]["id"]


async def test_job_details_fresh_no_refresh(client, jsearch, user_factory):
    _, headers = await user_factory()
    job_id = await _seed(client, jsearch, headers)
    r = await client.get(f"/api/v1/jobs/{job_id}", headers=headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["details_refreshed"] is False
    job = data["job"]
    assert job["job_highlights"]["Qualifications"][0] == "3+ years of Python"
    assert [o["apply_link"] for o in job["apply_options"]] == ["https://linkedin.example.com/jobs/1"]
    assert len(jsearch.requests) == 1


async def test_job_details_refresh_when_stale(client, jsearch, user_factory, db):
    _, headers = await user_factory()
    job_id = await _seed(client, jsearch, headers)
    job = await db.get(Job, uuid.UUID(job_id))
    job.last_seen_at = utcnow() - timedelta(days=3)
    await db.commit()
    jsearch.add("GET", "/job-details", httpx.Response(200, json={"status": "OK", "data": [raw_job(job_description="Updated description")]}))
    data = (await client.get(f"/api/v1/jobs/{job_id}", headers=headers)).json()["data"]
    assert data["details_refreshed"] is True and data["job"]["job_description"] == "Updated description"
    assert params_of(jsearch.requests[-1])["job_id"] == "abc123=="


async def test_job_details_provider_failure_returns_stored(client, jsearch, user_factory):
    _, headers = await user_factory()
    job_id = await _seed(client, jsearch, headers)
    jsearch.add("GET", "/job-details", httpx.Response(500))
    data = (await client.get(f"/api/v1/jobs/{job_id}?refresh=true", headers=headers)).json()["data"]
    assert data["details_stale"] is True and data["job"]["job_title"] == "Python Developer"


async def test_job_details_listing_gone(client, jsearch, user_factory):
    _, headers = await user_factory()
    job_id = await _seed(client, jsearch, headers)
    jsearch.add("GET", "/job-details", httpx.Response(200, json={"status": "OK", "data": []}))
    data = (await client.get(f"/api/v1/jobs/{job_id}?refresh=true", headers=headers)).json()["data"]
    assert data["job"]["listing_unavailable"] is True


async def test_job_details_not_found(client, user_factory):
    _, headers = await user_factory()
    assert (await client.get(f"/api/v1/jobs/{uuid.uuid4()}", headers=headers)).status_code == 404
    assert (await client.get("/api/v1/jobs/not-a-uuid", headers=headers)).status_code == 422


async def test_filters_public(client):
    r = await client.get("/api/v1/jobs/filters")
    assert r.status_code == 200
    data = r.json()["data"]
    assert "FULLTIME" in data["employment_types"] and data["pagination"] == "cursor"
    assert "company" in data["applied_locally"]


async def test_suggestions_from_stored_titles(client, jsearch, user_factory):
    _, headers = await user_factory()
    jobs = [raw_job("1", job_title="Python Developer"), raw_job("2", job_title="Python Developer", employer_name="X"),
            raw_job("3", job_title="Senior Python Engineer"), raw_job("4", job_title="Data Analyst")]
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload(jobs)))
    await search(client, headers)
    data = (await client.get("/api/v1/jobs/suggestions?q=pyth", headers=headers)).json()["data"]
    assert data[0] == {"title": "Python Developer", "listings": 2}
    assert {d["title"] for d in data} == {"Python Developer", "Senior Python Engineer"}
    assert (await client.get("/api/v1/jobs/suggestions?q=zzzz", headers=headers)).json()["data"] == []
    assert (await client.get("/api/v1/jobs/suggestions?q=%25%25", headers=headers)).json()["data"] == []


# ---------------- Normaliser (unit) ----------------
@pytest.mark.unit
def test_normalize_complete_job():
    n = normalize_job(raw_job(job_min_salary=1000, job_max_salary=500, job_salary_period="month", job_salary_currency="pkr"))
    assert n["employment_type"] == "FULLTIME"
    assert (n["min_salary"], n["max_salary"]) == (500, 1000)
    assert n["salary_currency"] == "PKR" and n["salary_period"] == "MONTH"
    assert n["country"] == "pk" and n["location"] == "Karachi, Sindh, PK"
    assert n["posted_at"].tzinfo is not None


@pytest.mark.unit
def test_normalize_missing_fields():
    n = normalize_job({"job_title": "Analyst"})
    assert n["external_id"].startswith("h:")
    assert n["company_name"] is None and n["min_salary"] is None and n["salary_currency"] is None
    assert n["salary_period"] is None and n["apply_url"] is None and n["posted_at"] is None
    assert n["is_remote"] is False and n["employment_types"] == []
    assert normalize_job({"job_id": "x"}) is None  # no title → unusable


@pytest.mark.unit
def test_normalize_salary_zero_is_missing_and_currency_not_assumed():
    n = normalize_job(raw_job(job_min_salary=0, job_max_salary=None, job_salary_period="YEAR"))
    assert n["min_salary"] is None and n["salary_period"] is None and n["salary_currency"] is None


@pytest.mark.unit
@pytest.mark.parametrize(
    "ts,dt,expected_none",
    [(None, "2026-11-01T10:00:00.000Z", False), ("bad", None, True), (32503680000, None, True), (100, None, True), (None, "garbage", True)],
)
def test_normalize_timestamps(ts, dt, expected_none):
    n = normalize_job(raw_job(job_posted_at_timestamp=ts, job_posted_at_datetime_utc=dt))
    assert (n["posted_at"] is None) is expected_none


@pytest.mark.unit
@pytest.mark.parametrize(
    "url,ok",
    [("https://a.com/x", True), ("http://a.com", True), ("javascript:alert(1)", False), ("data:text/html,x", False),
     ("ftp://a.com/x", False), ("https://", False), ("//a.com", False), ("https://user:pw@a.com", False),
     ("https://a.com/\nx", False), (123, False)],
)
def test_safe_url(url, ok):
    assert (safe_url(url) is not None) is ok


@pytest.mark.unit
def test_dangerous_apply_link_dropped():
    n = normalize_job(raw_job(job_apply_link="javascript:steal()"))
    assert n["apply_url"] == "https://linkedin.example.com/jobs/1"  # falls back to a safe option
    n = normalize_job(raw_job(job_apply_link="javascript:steal()", apply_options=[]))
    assert n["apply_url"] is None


@pytest.mark.unit
def test_remote_and_structured_fields():
    n = normalize_job(raw_job(job_is_remote=True, job_required_skills=["python", "Postgres", "Teamwork"],
                              job_required_experience={"required_experience_in_months": 36, "no_experience_required": False}))
    assert n["is_remote"] is True
    assert n["required_skills"] == ["Python", "PostgreSQL", "Teamwork"]
    assert n["required_experience_months"] == 36 and n["no_experience_required"] is False


@pytest.mark.unit
def test_stable_external_id_differs_by_company():
    assert stable_external_id({"job_title": "Dev", "employer_name": "A"}) != stable_external_id({"job_title": "Dev", "employer_name": "B"})


@pytest.mark.unit
def test_infer_country_and_params():
    assert infer_country("Hyderabad, Pakistan") == "pk"
    assert infer_country("Hyderabad") is None  # ambiguous (India / Pakistan)
    assert infer_country("Remote") is None
    p = JobSearchParams(query="Data Analyst", location="Lahore")
    assert p.provider_params()["country"] == "pk"
    assert p.cache_key() == JobSearchParams(query="Data  Analyst", location="Lahore").cache_key()
    assert p.cache_key() != JobSearchParams(query="Data Analyst", location="Lahore", cursor="c").cache_key()


@pytest.mark.unit
def test_client_extract_shapes():
    jobs, cursor = JSearchClient._extract({"data": [{"job_id": "1"}]})
    assert jobs == [{"job_id": "1"}] and cursor is None
    jobs, cursor = JSearchClient._extract({"data": {"jobs": [{"job_id": "2"}], "cursor": "c2"}})
    assert cursor == "c2"
    jobs, _ = JSearchClient._extract({"data": None})
    assert jobs == []


# ---------------- Plan quota guard (RapidAPI x-ratelimit-requests-*) ----------------
def quota_headers(remaining: int, reset: int = 3600, limit: int = 200) -> dict[str, str]:
    return {"x-ratelimit-requests-limit": str(limit), "x-ratelimit-requests-remaining": str(remaining),
            "x-ratelimit-requests-reset": str(reset)}


async def test_quota_reserve_stops_provider_calls(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job()]), headers=quota_headers(5)))
    assert (await search(client, headers, location="Lahore")).status_code == 200  # reports 5 left (= reserve)
    r = await search(client, headers, location="Karachi")  # different query: would need a provider call
    assert r.status_code == 429 and r.json()["error"]["code"] == "job_search_rate_limited"
    assert 3500 <= int(r.headers["Retry-After"]) <= 3600
    assert "quota" in r.json()["error"]["message"].lower()
    assert len(jsearch.requests) == 1  # the reserve was never spent


async def test_quota_reserve_serves_stale_cache(client, jsearch, user_factory, db):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job()]), headers=quota_headers(3)))
    await search(client, headers)
    entry = await db.scalar(select(JobSearchCache))
    entry.expires_at = utcnow() - timedelta(seconds=1)
    await db.commit()
    r = await search(client, headers)
    assert r.status_code == 200 and r.json()["data"]["pagination"]["stale"] is True
    assert len(jsearch.requests) == 1


async def test_quota_above_reserve_and_period_reset(client, jsearch, user_factory, monkeypatch):
    from app.services.jsearch import client as jclient

    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([]), headers=quota_headers(6, reset=1)))
    assert (await search(client, headers, location="A")).status_code == 200
    assert (await search(client, headers, location="B")).status_code == 200  # 6 > reserve 5
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([]), headers=quota_headers(2, reset=1)))
    await search(client, headers, location="C")
    assert (await search(client, headers, location="D")).status_code == 429
    real = jclient.time.monotonic
    monkeypatch.setattr(jclient.time, "monotonic", lambda: real() + 5)  # billing period has reset
    assert (await search(client, headers, location="E")).status_code == 200


async def test_quota_guard_disabled_and_missing_headers(client, jsearch, user_factory):
    get_settings().JSEARCH_QUOTA_RESERVE = 0
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([]), headers=quota_headers(0)))
    assert (await search(client, headers, location="A")).status_code == 200
    assert (await search(client, headers, location="B")).status_code == 200
    get_settings().JSEARCH_QUOTA_RESERVE = 5
    from app.services.jsearch.client import plan_quota

    plan_quota.reset()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([])))  # no quota headers at all
    assert (await search(client, headers, location="C")).status_code == 200
    assert (await search(client, headers, location="D")).status_code == 200


async def test_quota_visible_on_admin_dashboard(client, jsearch, user_factory):
    from app.models.user import UserRole

    _, seeker = await user_factory()
    _, admin = await user_factory(UserRole.ADMIN)
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([]), headers=quota_headers(150)))
    await search(client, seeker)
    q = (await client.get("/api/v1/admin/dashboard", headers=admin)).json()["data"]["jsearch_quota"]
    assert q["limit"] == 200 and q["remaining"] == 150 and q["resets_in_seconds"] > 3500


# ---------------- Real /search-v2 field set (observed live 2026-11-17) ----------------
def real_v2_job(**overrides):
    job = {
        "job_id": "Zx1Abc==", "job_uid": "uid-123", "job_title": "Python Developer", "employer_name": "Cloudtek",
        "employer_logo": None, "employer_website": None, "employer_reviews": [], "job_publisher": "Jooble",
        "job_employment_type": "Full-time", "job_employment_types": ["FULLTIME"],
        "job_apply_link": "https://pk.jooble.org/desc/123", "job_apply_is_direct": False,
        "apply_options": [{"publisher": "Jooble", "apply_link": "https://pk.jooble.org/desc/123", "is_direct": False}],
        "job_description": "We are hiring a Python developer with Django and REST API experience.",
        "job_is_remote": False, "job_posted_at": "7 days ago", "job_posted_at_timestamp": 1794182400,
        "job_posted_at_datetime_utc": "2026-11-09T00:00:00.000Z", "job_location": "Islamabad", "job_city": "Islamabad",
        "job_state": "Islamabad Capital Territory", "job_country": "PK", "job_latitude": 33.68, "job_longitude": 73.04,
        "job_benefits": None, "job_benefits_strings": [], "job_google_link": "https://www.google.com/search?q=x",
        "job_salary": None, "job_salary_string": None, "job_min_salary": None, "job_max_salary": None,
        "job_salary_period": None, "job_highlights": {}, "job_onet_soc": "15113200", "job_onet_job_zone": "4",
    }
    job.update(overrides)
    return job


@pytest.mark.unit
def test_normalize_real_v2_shape():
    n = normalize_job(real_v2_job())
    assert n["external_id"] == "Zx1Abc==" and n["employment_type"] == "FULLTIME" and n["country"] == "pk"
    assert n["apply_url"] == "https://pk.jooble.org/desc/123" and n["posted_at"] is not None
    assert n["highlights"] == {} and n["min_salary"] is None and n["salary_text"] is None
    assert n["required_skills"] == []  # provider gives none; recommendations infer them from the text


@pytest.mark.unit
def test_normalize_real_v2_without_dates_or_city():
    """5 of 10 live Pakistan results had no posted date in any field and no city."""
    n = normalize_job(real_v2_job(job_posted_at=None, job_posted_at_timestamp=None, job_posted_at_datetime_utc=None,
                                  job_city=None, job_state=None, job_location="Pakistan"))
    assert n["posted_at"] is None and n["location"] == "Pakistan" and n["city"] is None


async def test_search_real_v2_payload(client, jsearch, user_factory):
    _, headers = await user_factory()
    payload = {"status": "OK", "request_id": "r", "parameters": {"query": "python developer in pakistan"},
               "data": {"jobs": [real_v2_job(), real_v2_job(job_id="Other==", employer_name="Devsinc", job_employment_type="Contractor",
                                                            job_employment_types=["CONTRACTOR"])], "cursor": "opaque-cursor"}}
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=payload))
    data = (await search(client, headers, location="Pakistan")).json()["data"]
    assert [i["job_employment_type"] for i in data["items"]] == ["FULLTIME", "CONTRACTOR"]
    assert data["pagination"]["next_cursor"] == "opaque-cursor" and data["pagination"]["country"] == "pk"
