"""City-based job filtering and ordering (GET /api/v1/jobs/search?city=...)."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.services.locations import canonical_city, city_match_rank, city_variants, fold
from tests.jsearch_data import raw_job, search_payload
from tests.techmap_data import techmap_job, techmap_page

TM_PATH = "/api/v2/jobs/search"


def params_of(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


def js_calls(upstream) -> list[httpx.Request]:
    return [r for r in upstream.requests if r.url.path.startswith("/search")]


def tm_calls(upstream) -> list[httpx.Request]:
    return [r for r in upstream.requests if r.url.path == TM_PATH]


def place(job_id: str, city: str | None, country: str | None = "PK", location: str | None = None, **kw):
    return raw_job(job_id, job_city=city, job_country=country, job_state=None,
                   job_location=location if location is not None else city, **kw)


async def search(client, headers, **params):
    return await client.get("/api/v1/jobs/search", headers=headers, params={"query": "Developer", **params})


# ====================================================================== matching rules
@dataclass
class J:
    city: str | None = None
    state: str | None = None
    location: str | None = None
    country: str | None = None
    is_remote: bool = False


@pytest.mark.unit
def test_fold_and_aliases():
    assert fold("Karāchi,  Sindh") == "karachi sindh"
    assert canonical_city("Bangalore") == "bengaluru" and canonical_city("Islāmābād") == "islamabad"
    assert city_variants("Karachi") == ["Karachi", "Karāchi"]
    assert city_variants("karachi")[0] == "karachi"
    assert city_variants("Toronto") == ["Toronto"]  # any city works, aliases are optional


@pytest.mark.unit
@pytest.mark.parametrize(("job", "city", "kwargs", "rank"), [
    (J(city="Karāchi", country="pk"), "Karachi", {}, 0),
    (J(city="Karachi Division", country="pk"), "Karachi", {}, 1),
    (J(location="Karachi, Sindh, PK", country="pk"), "Karachi", {}, 1),
    (J(city="Lahore", country="pk"), "Karachi", {}, None),
    (J(city="Islamabad", state="Islāmābād", country="pk"), "Islamabad", {}, 0),
    (J(city="Hyderabad", country="in"), "Hyderabad", {"country": "pk"}, None),
    (J(city="Hyderabad", country="pk"), "Hyderabad", {"country": "pk"}, 0),
    (J(city="Hyderabad", country="in"), "Hyderabad", {}, 0),
    (J(city="Bangalore", country="in"), "Bengaluru", {}, 0),
    (J(location="Greater London, England, UK", country="gb"), "London", {}, 1),
    (J(), "Karachi", {}, None),  # no location data at all
    (J(is_remote=True, country="pk"), "Karachi", {}, None),
    (J(is_remote=True, country="pk"), "Karachi", {"include_remote_anywhere": True}, 2),
    (J(is_remote=True, city="Lahore", country="pk"), "Karachi", {"include_remote_anywhere": True}, None),
    (J(city="Newark", country="us"), "New York", {}, None),  # whole-word match only
])
def test_city_match_rank(job, city, kwargs, rank):
    assert city_match_rank(job, city, **kwargs) == rank


# ====================================================================== Pakistani cities
@pytest.mark.parametrize(("city", "spellings"), [
    ("Karachi", "Karachi,Karāchi"), ("Lahore", "Lahore,Lāhore"),
    ("Islamabad", "Islamabad,Islāmābād"), ("Hyderabad", "Hyderabad,Hyderābād"),
])
async def test_pakistani_city_filters_both_providers(client, jsearch, techmap, user_factory, city, spellings):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        place("in-city", city), place("other-city", "Multan" if city != "Multan" else "Quetta"),
        place("district", None, location=f"{city} District, Pakistan"),
    ])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([
        techmap_job(1, title="Backend Developer", city=spellings.split(",")[-1]),
        techmap_job(2, title="Frontend Developer", city="Sukkur"),
    ])))
    extra = {"country": "pk"} if city == "Hyderabad" else {}
    r = await search(client, headers, city=city, **extra)
    assert r.status_code == 200, r.text
    items = r.json()["data"]["items"]
    assert {(i["source"], i["external_job_id"]) for i in items} == {
        ("jsearch", "in-city"), ("jsearch", "district"), ("techmap", "tm" + "0" * 21 + "1")}
    assert items[-1]["external_job_id"] == "district"  # exact city before district matches
    assert params_of(js_calls(jsearch)[0])["query"] == f"Developer in {city}"
    assert params_of(js_calls(jsearch)[0])["country"] == "pk"
    sent = params_of(tm_calls(techmap)[0])
    assert sent["city"] == spellings and sent["countryCode"] == "pk"
    page = r.json()["data"]["pagination"]
    assert page["page_size"] == 3
    assert page["providers"]["jsearch"]["count"] == 2 and page["providers"]["techmap"]["count"] == 1


async def test_hyderabad_country_disambiguates(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        place("hyd-pk", "Hyderabad", "PK"), place("hyd-in", "Hyderabad", "IN"),
    ])))
    pk_only = await search(client, headers, city="Hyderabad", country="pk")
    assert [i["external_job_id"] for i in pk_only.json()["data"]["items"]] == ["hyd-pk"]
    both = await search(client, headers, city="Hyderabad")  # ambiguous without a country
    assert {i["external_job_id"] for i in both.json()["data"]["items"]} == {"hyd-pk", "hyd-in"}


# ====================================================================== international cities
@pytest.mark.parametrize(("city", "country", "job_city", "other"), [
    ("London", "gb", "London", "Manchester"),
    ("Berlin", "de", "Berlin", "Munich"),
    ("Dubai", "ae", "Dubai", "Sharjah"),
    ("Toronto", "ca", "Toronto", "Vancouver"),
])
async def test_international_cities(client, jsearch, techmap, user_factory, city, country, job_city, other):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        place("match", job_city, country.upper()), place("other", other, country.upper()),
    ])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([
        techmap_job(1, city=job_city, countryCode=country), techmap_job(2, title="X Developer", city=other, countryCode=country),
    ])))
    r = await search(client, headers, city=city)
    items = r.json()["data"]["items"]
    assert [i["job_city"] for i in items] == [job_city, job_city]
    assert r.json()["data"]["pagination"]["country"] == country
    assert params_of(tm_calls(techmap)[0])["city"] == city


async def test_city_without_known_country_is_not_restricted(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        place("lisbon", "Lisbon", "PT"), place("porto", "Porto", "PT")])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([])))
    r = await search(client, headers, city="Lisbon")
    assert [i["external_job_id"] for i in r.json()["data"]["items"]] == ["lisbon"]
    assert "country" not in params_of(js_calls(jsearch)[0])
    assert "countryCode" not in params_of(tm_calls(techmap)[0])


# ====================================================================== with country / remote filters
async def test_city_with_explicit_country_and_location(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        place("lhr", "Lahore", "PK"), place("lhr-elsewhere", "Lahore", "US")])))
    r = await search(client, headers, city="Lahore", country="pk", location="Punjab")
    assert [i["external_job_id"] for i in r.json()["data"]["items"]] == ["lhr"]
    sent = params_of(js_calls(jsearch)[0])
    assert sent["query"] == "Developer in Lahore, Punjab" and sent["country"] == "pk"


async def test_city_with_remote_jobs(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        place("remote-anywhere", None, "PK", location="Pakistan", job_is_remote=True),
        place("remote-karachi", "Karachi", job_is_remote=True),
        place("remote-lahore", "Lahore", job_is_remote=True),
    ])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([])))
    r = await search(client, headers, city="Karachi", remote_jobs_only="true")
    assert [i["external_job_id"] for i in r.json()["data"]["items"]] == ["remote-karachi", "remote-anywhere"]
    assert params_of(js_calls(jsearch)[0])["work_from_home"] == "true"
    sent = params_of(tm_calls(techmap)[0])
    assert sent["workPlace"] == "remote" and sent["city"] == "Karachi,Karāchi"
    # Without remote_jobs_only, a remote job with no city is not "in Karachi".
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        place("remote-anywhere", None, "PK", location="Pakistan", job_is_remote=True)])))
    r = await search(client, headers, city="Karachi", query="Analyst")
    assert r.json()["data"]["items"] == []


# ====================================================================== missing / incomplete data
async def test_jobs_with_missing_location_are_skipped_safely(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        place("nothing", None, None, location=None),
        place("country-only", None, "PK", location="PK"),
        place("ok", "Karachi"),
    ])))
    r = await search(client, headers, city="Karachi")
    assert r.status_code == 200
    data = r.json()["data"]
    assert [i["external_job_id"] for i in data["items"]] == ["ok"]
    assert data["pagination"]["page_size"] == 1


async def test_no_matches_returns_empty_page_with_cursor(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([place("x", "Lahore")])))
    r = await search(client, headers, city="Quetta")
    data = r.json()["data"]
    assert data["items"] == [] and r.json()["message"] == "No jobs found for this search."
    assert data["pagination"]["has_more"] is True  # the provider has more pages to check


@pytest.mark.parametrize("value", ["Pakistan", "UK", "United States"])
async def test_country_name_rejected_as_city(client, jsearch, user_factory, value):
    _, headers = await user_factory()
    r = await search(client, headers, city=value)
    assert r.status_code == 422 and not js_calls(jsearch)
    assert any(d["field"] == "city" for d in r.json()["error"]["details"])


# ====================================================================== pagination, duplicates, caching
async def test_pagination_with_city(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([place("p1", "Lahore"), place("x1", "Multan")])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(1, city="Lahore")], total=25)))
    first = await search(client, headers, city="Lahore")
    cursor = first.json()["data"]["pagination"]["next_cursor"]
    assert cursor.startswith("mx1.") and len(first.json()["data"]["items"]) == 2
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([place("p2", "Lāhore"), place("x2", "Sukkur")], cursor=None)))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(2, city="Lāhore")], page=2, total=25)))
    second = await search(client, headers, city="Lahore", cursor=cursor)
    items = second.json()["data"]["items"]
    assert sorted(i["external_job_id"] for i in items) == ["p2", "tm" + "0" * 21 + "2"]
    assert not {i["id"] for i in items} & {i["id"] for i in first.json()["data"]["items"]}
    assert params_of(tm_calls(techmap)[-1])["page"] == "2"


async def test_duplicates_removed_with_city(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        place("j1", "Karachi", job_title="Python Developer", employer_name="Acme Software")])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(1), techmap_job(2, title="Go Developer")])))
    r = await search(client, headers, city="Karachi")
    items = r.json()["data"]["items"]
    assert [(i["source"], i["job_title"]) for i in items] == [("jsearch", "Python Developer"), ("techmap", "Go Developer")]


async def test_cached_results_are_filtered_by_city(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([place("k", "Karachi"), place("l", "Lahore")])))
    first = await search(client, headers, city="Karachi")
    again = await search(client, headers, city="Karachi")
    assert len(js_calls(jsearch)) == 1 and again.json()["data"]["pagination"]["cached"] is True
    assert [i["external_job_id"] for i in again.json()["data"]["items"]] == ["k"] == [
        i["external_job_id"] for i in first.json()["data"]["items"]]


async def test_without_city_behaviour_is_unchanged(client, jsearch, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([place("k", "Karachi"), place("l", "Lahore")])))
    r = await search(client, headers, location="Karachi")
    assert [i["external_job_id"] for i in r.json()["data"]["items"]] == ["k", "l"]  # location is not a filter


async def test_filters_catalog_documents_city(client):
    data = (await client.get("/api/v1/jobs/filters")).json()["data"]
    assert "city" in data["applied_by_provider"] and "city" in data["applied_locally"]
