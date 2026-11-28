"""Remote jobs are a Pro feature (verified app_access entitlement)."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.core.config import get_settings
from app.models.job import JobSource
from app.models.subscription import SubscriptionStatus
from app.models.user import UserRole
from tests.jsearch_data import raw_job, search_payload
from tests.techmap_data import techmap_job, techmap_page
from tests.test_application_limit import make_jobs, make_pro
from tests.test_recommendations import add_job, setup_profile

TM_PATH = "/api/v2/jobs/search"
MESSAGE = ("Remote job access is available exclusively to Pro members. "
           "Upgrade to Pro to explore remote opportunities.")
PRO_REQUIRED = {"code": "PRO_FEATURE_REQUIRED", "feature": "remote_jobs", "upgrade_available": True}


@pytest.fixture(autouse=True)
def remote_pro_only():
    get_settings().REMOTE_JOBS_PRO_ONLY = True


def params_of(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


def calls(upstream, path_start: str) -> list[httpx.Request]:
    return [r for r in upstream.requests if r.url.path.startswith(path_start)]


async def search(client, headers, **params):
    return await client.get("/api/v1/jobs/search", headers=headers, params={"query": "Python Developer", **params})


def assert_pro_required(r: httpx.Response) -> None:
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["success"] is False and body["message"] == MESSAGE
    assert body["data"] == PRO_REQUIRED
    assert body["error"]["code"] == "PRO_FEATURE_REQUIRED" and body["error"]["details"] == PRO_REQUIRED


def mixed_jsearch() -> dict:
    return search_payload([raw_job("onsite-1", job_is_remote=False), raw_job("remote-1", job_is_remote=True),
                           raw_job("onsite-2", job_is_remote=False)])


def mixed_techmap() -> dict:
    return techmap_page([techmap_job(1, title="Onsite Dev", workPlace=["Onsite"]),
                         techmap_job(2, title="Remote Dev", workPlace=["Remote"])], total=30)


# ====================================================================== Free users
@pytest.mark.parametrize("value", ["true", "1", "True", "yes", "on"])
async def test_free_user_remote_filter_refused_before_providers(client, jsearch, techmap, user_factory, value):
    _, headers = await user_factory()
    r = await search(client, headers, remote_jobs_only=value)
    assert_pro_required(r)
    assert not calls(jsearch, "/search") and not calls(techmap, TM_PATH)  # no provider call, no quota used


async def test_free_user_normal_search_excludes_remote_from_both_providers(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=mixed_jsearch()))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=mixed_techmap()))
    r = await search(client, headers)
    assert r.status_code == 200
    items = r.json()["data"]["items"]
    assert items and not any(i["job_is_remote"] for i in items)
    assert sorted(i["job_title"] for i in items if i["source"] == "techmap") == ["Onsite Dev"]
    assert {i["external_job_id"] for i in items if i["source"] == "jsearch"} == {"onsite-1", "onsite-2"}
    providers = r.json()["data"]["pagination"]["providers"]
    assert providers["jsearch"]["count"] == 2 and providers["techmap"]["count"] == 1
    assert "work_from_home" not in params_of(calls(jsearch, "/search")[0])


@pytest.mark.parametrize(("kwargs", "label"), [
    ({"expires_in": timedelta(hours=-1)}, "expired"),
    ({"status": SubscriptionStatus.REFUNDED, "active": False}, "refunded"),
    ({"status": SubscriptionStatus.TRANSFERRED}, "transferred"),
    ({"active": False}, "inactive"),
])
async def test_expired_or_inactive_subscription_is_free(client, db, jsearch, user_factory, kwargs, label):
    user, headers = await user_factory()
    await make_pro(db, user, **kwargs)
    assert_pro_required(await search(client, headers, remote_jobs_only="true"))


# ====================================================================== Pro users
async def test_pro_user_jsearch_remote_jobs(client, db, jsearch, user_factory):
    user, headers = await user_factory()
    await make_pro(db, user)
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("remote-1", job_is_remote=True)])))
    r = await search(client, headers, remote_jobs_only="true")
    assert r.status_code == 200
    assert [i["job_is_remote"] for i in r.json()["data"]["items"]] == [True]
    assert params_of(calls(jsearch, "/search")[0])["work_from_home"] == "true"


async def test_pro_user_techmap_remote_jobs(client, db, jsearch, techmap, user_factory):
    user, headers = await user_factory()
    await make_pro(db, user)
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([techmap_job(2, title="Remote Dev", workPlace=["Remote"])])))
    r = await search(client, headers, remote_jobs_only="true")
    items = r.json()["data"]["items"]
    assert [(i["source"], i["job_is_remote"]) for i in items] == [("techmap", True)]
    assert params_of(calls(techmap, TM_PATH)[0])["workPlace"] == "remote"


async def test_pro_user_can_toggle_and_sees_remote_without_filter(client, db, jsearch, techmap, user_factory):
    user, headers = await user_factory()
    await make_pro(db, user)
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=mixed_jsearch()))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=mixed_techmap()))
    items = (await search(client, headers, remote_jobs_only="false")).json()["data"]["items"]
    assert sum(i["job_is_remote"] for i in items) == 2  # remote listings included when filter is off


async def test_admin_is_exempt(client, db, jsearch, user_factory):
    _, headers = await user_factory(UserRole.ADMIN)
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("r", job_is_remote=True)])))
    assert (await search(client, headers, remote_jobs_only="true")).status_code == 200


# ====================================================================== cache, pagination, other filters
async def test_cached_results_filtered_per_plan(client, db, jsearch, techmap, user_factory):
    pro, pro_headers = await user_factory()
    await make_pro(db, pro)
    _, free_headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=mixed_jsearch()))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=mixed_techmap()))
    pro_items = (await search(client, pro_headers)).json()["data"]["items"]
    free = (await search(client, free_headers)).json()["data"]
    assert len(calls(jsearch, "/search")) == 1 and len(calls(techmap, TM_PATH)) == 1  # second search = cache
    assert free["pagination"]["cached"] is True
    assert any(i["job_is_remote"] for i in pro_items) and not any(i["job_is_remote"] for i in free["items"])


async def test_pagination_keeps_excluding_remote(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=mixed_jsearch()))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=mixed_techmap()))
    cursor = (await search(client, headers)).json()["data"]["pagination"]["next_cursor"]
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload(
        [raw_job("remote-2", job_is_remote=True), raw_job("onsite-3")], cursor=None)))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page(
        [techmap_job(3, title="Remote 2", workPlace=["Remote"]), techmap_job(4, title="Onsite 2")], page=2, total=30)))
    page2 = (await search(client, headers, cursor=cursor)).json()["data"]["items"]
    assert page2 and not any(i["job_is_remote"] for i in page2)
    assert {i["job_title"] for i in page2 if i["source"] == "techmap"} == {"Onsite 2"}


async def test_city_and_country_filters_unaffected(client, jsearch, techmap, user_factory):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([
        raw_job("khi", job_city="Karachi"), raw_job("khi-remote", job_city="Karachi", job_is_remote=True),
        raw_job("lhr", job_city="Lahore", job_location="Lahore")])))
    techmap.add("GET", TM_PATH, httpx.Response(200, json=techmap_page([])))
    r = await search(client, headers, city="Karachi", country="pk")
    assert [i["external_job_id"] for i in r.json()["data"]["items"]] == ["khi"]
    assert params_of(calls(jsearch, "/search")[0])["country"] == "pk"
    assert params_of(calls(techmap, TM_PATH)[0])["city"] == "Karachi,Karāchi"


# ====================================================================== other discovery flows
async def test_remote_job_cannot_be_opened_by_id_on_free_plan(client, db, user_factory):
    _, headers = await user_factory()
    [remote] = await make_jobs(db, 1, is_remote=True)
    [onsite] = await make_jobs(db, 1)
    assert_pro_required(await client.get(f"/api/v1/jobs/{remote.id}", headers=headers, params={"refresh": "false"}))
    assert_pro_required(await client.post(f"/api/v1/jobs/{remote.id}/apply-link", headers=headers))
    assert_pro_required(await client.post(f"/api/v1/saved-jobs/{remote.id}", headers=headers, json={}))
    assert_pro_required(await client.post("/api/v1/applications", headers=headers,
                                          json={"job_id": str(remote.id), "status": "saved"}))
    usage = (await client.get("/api/v1/jobs/application-usage", headers=headers)).json()["data"]
    assert usage["used"] == 0  # refused remote job did not consume the daily limit
    assert (await client.post(f"/api/v1/jobs/{onsite.id}/apply-link", headers=headers)).status_code == 200


async def test_pro_user_can_open_remote_jobs(client, db, user_factory):
    user, headers = await user_factory()
    await make_pro(db, user)
    [remote] = await make_jobs(db, 1, source=JobSource.TECHMAP, is_remote=True)
    assert (await client.get(f"/api/v1/jobs/{remote.id}", headers=headers)).status_code == 200
    assert (await client.post(f"/api/v1/jobs/{remote.id}/apply-link", headers=headers)).status_code == 200
    assert (await client.post(f"/api/v1/saved-jobs/{remote.id}", headers=headers, json={})).status_code == 201


async def test_saved_jobs_and_sharkhire_listing(client, db, user_factory):
    user, headers = await user_factory()
    await make_pro(db, user)
    [remote] = await make_jobs(db, 1, is_remote=True)
    [onsite] = await make_jobs(db, 1)
    for job in (remote, onsite):
        await client.post(f"/api/v1/saved-jobs/{job.id}", headers=headers, json={})
    assert len((await client.get("/api/v1/saved-jobs", headers=headers)).json()["data"]["items"]) == 2
    # Subscription ends: the saved remote job is withheld, the remote filters are refused.
    from sqlalchemy import update

    from app.models.subscription import Subscription
    await db.execute(update(Subscription).where(Subscription.user_id == user.id).values(entitlement_active=False))
    await db.commit()
    items = (await client.get("/api/v1/saved-jobs", headers=headers)).json()["data"]["items"]
    assert [i["job"]["id"] for i in items] == [str(onsite.id)]
    assert_pro_required(await client.get("/api/v1/saved-jobs?remote=true", headers=headers))
    assert_pro_required(await client.get(f"/api/v1/saved-jobs/{remote.id}", headers=headers))
    assert_pro_required(await client.get("/api/v1/jobs/sharkhire?remote=true", headers=headers))
    assert (await client.get("/api/v1/jobs/sharkhire", headers=headers)).status_code == 200


async def test_recommendations(client, db, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    await add_job(db, title="Python Developer", is_remote=False)
    await add_job(db, title="Remote Python Developer", is_remote=True)
    items = (await client.get("/api/v1/recommendations/for-me", headers=headers)).json()["data"]["items"]
    assert items and not any(i["job"]["job_is_remote"] for i in items)
    assert_pro_required(await client.get("/api/v1/recommendations/for-me?remote_only=true", headers=headers))
    assert_pro_required(await client.put("/api/v1/recommendations/preferences", headers=headers,
                                         json={"remote_preference": "remote_only"}))
    ok = await client.put("/api/v1/recommendations/preferences", headers=headers, json={"remote_preference": "onsite"})
    assert ok.status_code == 200
    history = (await client.get("/api/v1/recommendations/history", headers=headers)).json()["data"]["items"]
    assert not any(h["job"] and h["job"]["job_is_remote"] for h in history)


async def test_recommendations_pro_remote(client, db, user_factory):
    user, headers = await user_factory()
    await make_pro(db, user)
    await setup_profile(client, headers)
    await add_job(db, title="Python Developer", is_remote=False)
    await add_job(db, title="Remote Python Developer", is_remote=True)
    r = await client.get("/api/v1/recommendations/for-me?remote_only=true", headers=headers)
    assert r.status_code == 200 and [i["job"]["job_is_remote"] for i in r.json()["data"]["items"]] == [True]


async def test_client_cannot_claim_pro(client, jsearch, user_factory):
    _, headers = await user_factory()
    r = await client.get("/api/v1/jobs/search", headers={**headers, "X-Plan": "pro"},
                         params={"query": "Python Developer", "remote_jobs_only": "true", "plan": "pro", "pro": "true"})
    assert_pro_required(r)
