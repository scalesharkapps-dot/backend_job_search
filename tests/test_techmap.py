"""Techmap provider + unified JSearch/Techmap job search.

Both providers are mocked (MockTransport). Payload shapes follow captured real
responses; these tests prove merging, de-duplication, pagination and failure
handling, not live provider availability.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.config import get_settings
from app.models.application import Application, ApplicationSource
from app.models.job import Job, JobSource
from app.services.techmap.normalizer import normalize_techmap_job
from tests.jsearch_data import raw_job, search_payload
from tests.techmap_data import techmap_job, techmap_page

TM_PATH = "/api/v2/jobs/search"


def tm_calls(upstream) -> list[httpx.Request]:
    return [r for r in upstream.requests if r.url.path == TM_PATH]


def js_calls(upstream) -> list[httpx.Request]:
    return [r for r in upstream.requests if r.url.path.startswith("/search")]


def params_of(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


async def search(client, headers, **params):
    return await client.get("/api/v1/jobs/search", headers=headers, params={"query": "Python Developer", **params})


# ====================================================================== normaliser
@pytest.mark.unit
def test_normalizer_maps_real_fields():
    job = normalize_techmap_job(techmap_job(), now=datetime(2026, 11, 28, tzinfo=UTC))
    assert job is not None
    assert job["source"] == JobSource.TECHMAP and job["external_id"] == "tm" + "0" * 21 + "1"
    assert job["title"] == "Python Developer" and job["company_name"] == "Acme Software"
    assert job["city"] == "Karāchi" and job["country"] == "pk" and job["location"] == "Karāchi, Sindh, PK"
    assert job["apply_url"] == "https://pk.linkedin.com/jobs/view/python-developer-1"
    assert job["employment_types"] == ["FULLTIME"] and job["is_remote"] is False
    assert job["required_experience_months"] == 24 and job["publisher"] == "linkedin"
    assert job["company_logo"].startswith("https://media.licdn.com/")
    # Techmap's estimatedSalary is a model estimate, never shown as the posting's salary.
    assert job["min_salary"] is None and job["max_salary"] is None and job["salary_currency"] is None
    assert job["provider_metadata"]["estimated_salary"]["currency"] == "PKR"
    assert job["posted_at"] == datetime(2026, 11, 27, 10, tzinfo=UTC)


@pytest.mark.unit
def test_normalizer_edge_cases():
    remote_contract = normalize_techmap_job(techmap_job(
        workPlace=["Remote"], workType=["N/A"], contractType=["Contract"], city="N/A", state=None,
        hasSalary=True, minSalary=300000, maxSalary=200000,
        jsonLD={"url": "javascript:alert(1)", "image": "ftp://bad", "employmentType": "Contract"},
    ))
    assert remote_contract["is_remote"] is True and remote_contract["employment_types"] == ["CONTRACTOR"]
    assert remote_contract["city"] is None and remote_contract["location"] == "PK"
    assert remote_contract["apply_url"] is None and remote_contract["apply_options"] == []
    assert remote_contract["company_logo"] is None
    assert (remote_contract["min_salary"], remote_contract["max_salary"]) == (200000, 300000)
    assert remote_contract["salary_currency"] == "PKR"
    no_id = normalize_techmap_job(techmap_job(jsonLD={"identifier": None}))
    assert no_id["external_id"].startswith("h:")
    assert normalize_techmap_job(techmap_job(title="N/A", jsonLD={"title": None})) is None
    assert normalize_techmap_job({"jsonLD": "broken", "title": "Dev"})["title"] == "Dev"
    escaped = normalize_techmap_job(techmap_job(company="S&amp;P Global", title="R&amp;D Engineer"))
    assert escaped["company_name"] == "S&P Global" and escaped["title"] == "R&D Engineer"


# ====================================================================== providers independently
async def test_jsearch_only_when_techmap_not_configured(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("a")])))
    r = await search(client, headers, location="Karachi")
    assert r.status_code == 200
    page = r.json()["data"]["pagination"]
    assert page["source"] == "jsearch" and page["next_cursor"] == "next-cursor-1"  # unchanged contract
    assert page["providers"]["techmap"]["status"] == "not_configured"


async def test_techmap_alone_when_jsearch_fails(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(500))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(1), techmap_job(2, title="Django Developer")])))
    r = await search(client, headers, location="Karachi")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [i["source"] for i in data["items"]] == ["techmap", "techmap"]
    assert data["pagination"]["source"] == "techmap"
    assert data["pagination"]["providers"]["jsearch"]["status"] == "error"
    assert data["items"][0]["job_apply_link"] == "https://pk.linkedin.com/jobs/view/python-developer-1"
    assert data["items"][0]["is_external"] is True


async def test_both_providers_fail_keeps_existing_error(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(500))
    techmap.add("GET", TM_PATH, httpx.Response(500))
    r = await search(client, headers)
    assert r.status_code in (502, 503) and r.json()["success"] is False
    assert r.json()["error"]["code"].startswith("job_search")


# ====================================================================== together
async def test_pakistan_search_queries_both_and_merges(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("j1"), raw_job("j2", job_title="Backend Engineer")])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page(
        [techmap_job(1, title="Django Developer"), techmap_job(2, title="Flask Developer")], total=25)))
    r = await search(client, headers, location="Karachi")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert [i["source"] for i in data["items"]] == ["jsearch", "techmap", "jsearch", "techmap"]  # interleaved
    page = data["pagination"]
    assert page["source"] == "jsearch+techmap" and page["country"] == "pk" and page["has_more"] is True
    assert page["providers"] == {"jsearch": {"status": "live", "count": 2}, "techmap": {"status": "live", "count": 2}}
    assert page["next_cursor"].startswith("mx1.")
    [tm] = tm_calls(techmap)
    sent = params_of(tm)
    assert sent["countryCode"] == "pk" and sent["city"] == "Karachi,Karāchi"  # both Techmap spellings (OR)
    assert sent["title"] == "+Python +Developer"
    assert sent["isDuplicate"] == "false" and sent["format"] == "json" and sent["page"] == "1"
    assert sent["dateCreatedMin"] <= sent["dateCreatedMax"]
    assert tm.headers["X-RapidAPI-Key"] == "test-techmap-key"
    assert tm.headers["X-RapidAPI-Host"] == "daily-international-job-postings.p.rapidapi.com"
    assert params_of(js_calls(jsearch)[0])["country"] == "pk"


@pytest.mark.parametrize(("extra", "expected"), [
    ({"country": "gb", "location": "London"}, {"countryCode": "gb", "city": "London"}),
    ({"country": "us"}, {"countryCode": "us"}),
    ({}, {}),  # no country: worldwide
])
async def test_international_searches(client, jsearch, techmap, user_factory, extra, expected):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(1, countryCode=extra.get("country", "pk"))])))
    r = await search(client, headers, **extra)
    assert r.status_code == 200
    sent = params_of(tm_calls(techmap)[0])
    for key in ("countryCode", "city"):
        assert sent.get(key) == expected.get(key)


async def test_duplicates_across_providers_removed(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        raw_job("j1", job_title="Python Developer", employer_name="Acme Software Pvt Ltd", job_city="Karachi"),
        raw_job("j2", job_title="Data Analyst", employer_name="Other Co",
                job_apply_link="https://pk.linkedin.com/jobs/view/python-developer-3"),
    ])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([
        techmap_job(1),  # same title/company (suffix + accents differ): duplicate of j1
        techmap_job(2, title="Python Developer", company="Acme Software", city="Lahore"),  # other city: kept
        techmap_job(3, title="Totally Different"),  # same apply URL as j2: duplicate
        techmap_job(4, title="Go Developer"),
    ])))
    r = await search(client, headers, location="Karachi")
    items = r.json()["data"]["items"]
    tm_titles = [(i["job_title"], i["job_city"]) for i in items if i["source"] == "techmap"]
    assert tm_titles == [("Python Developer", "Lahore"), ("Go Developer", "Karāchi")]
    assert r.json()["data"]["pagination"]["providers"]["techmap"]["count"] == 2


# ====================================================================== partial failures
@pytest.mark.parametrize(("response", "reason"), [
    (httpx.Response(500), "techmap_unavailable"),
    (httpx.Response(403, json={"message": "You are not subscribed"}), "techmap_unauthorized"),
    (httpx.Response(400, json={"message": "bad"}), "techmap_bad_request"),
    (httpx.Response(200, text="not json"), "techmap_bad_response"),
])
async def test_techmap_failure_keeps_jsearch_results(client, jsearch, techmap, user_factory, response, reason, caplog):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("j1")])))
    techmap.add("GET", TM_PATH, response)
    with caplog.at_level(logging.INFO):
        r = await search(client, headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert [i["source"] for i in data["items"]] == ["jsearch"]
    assert data["pagination"]["providers"]["techmap"] == {"status": "error", "count": 0, "reason": reason}
    assert "test-techmap-key" not in caplog.text and "test-techmap-key" not in r.text


async def test_techmap_timeout_does_not_block_jsearch(client, jsearch, techmap, user_factory):
    get_settings().TECHMAP_MAX_RETRIES = 0

    def slow(_request):
        raise httpx.ReadTimeout("timeout")

    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("j1")])))
    techmap.add("GET", TM_PATH, slow)
    r = await search(client, headers)
    assert r.status_code == 200 and len(r.json()["data"]["items"]) == 1


# ====================================================================== pagination, caching, quota
async def test_composite_cursor_pages_both_providers(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("j1")])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(1)], total=25)))
    cursor = (await search(client, headers)).json()["data"]["pagination"]["next_cursor"]
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("j2")], cursor=None)))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(2)], page=2, total=25)))
    second = await search(client, headers, cursor=cursor)
    assert second.status_code == 200
    assert params_of(js_calls(jsearch)[-1])["cursor"] == "next-cursor-1"
    assert params_of(tm_calls(techmap)[-1])["page"] == "2"
    third_cursor = second.json()["data"]["pagination"]["next_cursor"]
    assert third_cursor.startswith("mx1.")  # JSearch exhausted, Techmap has page 3
    n_js = len(js_calls(jsearch))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(3)], page=3, total=25)))
    third = await search(client, headers, cursor=third_cursor)
    assert len(js_calls(jsearch)) == n_js  # exhausted provider not called again
    assert third.json()["data"]["pagination"]["providers"]["jsearch"]["status"] == "exhausted"
    assert params_of(tm_calls(techmap)[-1])["page"] == "3"


async def test_legacy_jsearch_cursor_continues_jsearch_only(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("j9")], cursor=None)))
    r = await search(client, headers, cursor="plain-jsearch-cursor")
    assert r.status_code == 200
    assert not tm_calls(techmap)
    assert r.json()["data"]["pagination"]["next_cursor"] is None


async def test_invalid_composite_cursor(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    r = await search(client, headers, cursor="mx1.not-base64-json!!")
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_cursor"


async def test_results_cached_no_repeated_provider_calls(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("j1")])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(1)])))
    first = await search(client, headers, location="Lahore")
    second = await search(client, headers, location="Lahore")
    assert len(js_calls(jsearch)) == 1 and len(tm_calls(techmap)) == 1
    assert second.json()["data"]["pagination"]["cached"] is True
    assert second.json()["data"]["pagination"]["providers"]["techmap"]["status"] == "cached"
    assert [i["id"] for i in first.json()["data"]["items"]] == [i["id"] for i in second.json()["data"]["items"]]


async def test_quota_reserve_stops_techmap_before_overage(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("j1")])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(1)]), headers={
        "x-ratelimit-requests-limit": "100", "x-ratelimit-requests-remaining": "10",
        "x-ratelimit-requests-reset": "86400"}))
    await search(client, headers, location="Lahore")
    r = await search(client, headers, location="Islamabad")  # different search: would need a new call
    assert len(tm_calls(techmap)) == 1
    assert r.json()["data"]["pagination"]["providers"]["techmap"] == {
        "status": "error", "count": 0, "reason": "techmap_quota_reserved"}


@pytest.mark.parametrize("params", [{"experience": "no_experience"}, {"radius": 25},
                                    {"employment_types": "INTERN"}, {"exclude_job_publishers": "Indeed"}])
async def test_unsupported_filters_skip_techmap(client, jsearch, techmap, user_factory, params):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("j1")])))
    r = await search(client, headers, **params)
    assert r.status_code == 200 and not tm_calls(techmap)
    assert r.json()["data"]["pagination"]["providers"]["techmap"]["status"] == "skipped"


async def test_supported_filters_passed_to_techmap(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([])))
    await search(client, headers, remote_jobs_only="true", employment_types=["FULLTIME", "PARTTIME"],
                 date_posted="week", language="en")
    sent = params_of(tm_calls(techmap)[0])
    assert sent["workPlace"] == "remote" and sent["workType"] == "fulltime,parttime" and sent["language"] == "en"
    from datetime import date
    assert (date.fromisoformat(sent["dateCreatedMax"]) - date.fromisoformat(sent["dateCreatedMin"])).days == 6


# ====================================================================== Techmap jobs elsewhere in the app
async def test_techmap_job_details_and_application_tracking(client, jsearch, techmap, user_factory, db):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(1)])))
    [item] = (await search(client, headers)).json()["data"]["items"]
    details = await client.get(f"/api/v1/jobs/{item['id']}", headers=headers)
    assert details.status_code == 200, details.text
    job = details.json()["data"]["job"]
    assert job["source"] == "techmap" and job["job_description"].startswith("### Python Developer")
    r = await client.post("/api/v1/applications", headers=headers, json={"job_id": item["id"], "status": "applied"})
    assert r.status_code == 201, r.text
    app_row = await db.get(Application, uuid.UUID(r.json()["data"]["id"]))
    assert app_row.source == ApplicationSource.TECHMAP and app_row.external_job_id == "tm" + "0" * 21 + "1"
    stored = await db.get(Job, uuid.UUID(item["id"]))
    assert stored.source == JobSource.TECHMAP


async def test_techmap_store_failure_keeps_jsearch_results(client, jsearch, techmap, user_factory, monkeypatch):
    """E.g. code deployed before migration 0012: Techmap rows violate the old CHECK constraint."""
    from app.services import job_search

    def broken(_raw):
        raise RuntimeError("CHECK constraint failed: ck_jobs_job_source")

    monkeypatch.setattr(job_search, "normalize_techmap_job", broken)
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("j1")])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(1)])))
    r = await search(client, headers)
    assert r.status_code == 200
    data = r.json()["data"]
    assert [i["source"] for i in data["items"]] == ["jsearch"]
    assert data["pagination"]["providers"]["techmap"]["reason"] == "techmap_store_failed"
