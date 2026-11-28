"""Free plan daily application-link limit (5 per rolling 24 h); Pro unlimited."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.models.application import ApplicationLinkAction
from app.models.base import utcnow
from app.models.job import Job, JobSource
from app.models.subscription import Subscription, SubscriptionStatus
from app.models.user import UserRole
from app.services import application_links
from tests.conftest import auth_header, make_token
from tests.jsearch_data import raw_job, search_payload

LIMIT_MSG = ("You've reached your daily limit of 5 job applications. "
             "Try again after 24 hours or upgrade to Pro for unlimited access.")


class Clock:
    def __init__(self) -> None:
        self.offset = timedelta(0)

    def __call__(self):
        return utcnow() + self.offset


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(application_links, "utcnow", c)
    return c


@pytest.fixture
def hide_links():
    get_settings().HIDE_APPLY_LINKS_FOR_FREE_USERS = True


async def make_jobs(db, n: int, source: JobSource = JobSource.JSEARCH, **kw) -> list[Job]:
    jobs = []
    for i in range(n):
        values = {"apply_url": f"https://jobs.example.com/apply/{uuid.uuid4().hex[:8]}",
                  "apply_options": [{"publisher": "LinkedIn", "apply_link": "https://linkedin.example.com/j/1", "is_direct": False}]}
        values.update(kw)
        job = Job(source=source, external_id=f"ext-{uuid.uuid4().hex}", title=f"Job {i}", **values)
        db.add(job)
        jobs.append(job)
    await db.commit()
    return jobs


async def make_pro(db, user, *, expires_in=timedelta(days=20), status=SubscriptionStatus.ACTIVE, active=True):
    db.add(Subscription(user_id=user.id, revenuecat_app_user_id=str(user.id), store="play_store",
                        product_id=f"p-{uuid.uuid4().hex[:6]}", subscription_status=status, entitlement_active=active,
                        will_renew=True, expiration_at=utcnow() + expires_in, environment="PRODUCTION"))
    await db.commit()


async def open_link(client, headers, job, key=None):
    extra = {"Idempotency-Key": key} if key else {}
    return await client.post(f"/api/v1/jobs/{job.id}/apply-link", headers={**headers, **extra})


async def counted_rows(db, user_id) -> int:
    return int(await db.scalar(select(func.count()).select_from(ApplicationLinkAction)
                               .where(ApplicationLinkAction.user_id == user_id, ApplicationLinkAction.counted.is_(True))))


# ====================================================================== Free plan
async def test_first_action_returns_link_and_usage(client, db, user_factory, clock):
    user, headers = await user_factory()
    [job] = await make_jobs(db, 1)
    r = await open_link(client, headers, job)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["apply_url"] == job.apply_url and data["apply_options"][0]["publisher"] == "LinkedIn"
    assert data["already_opened"] is False
    usage = data["usage"]
    assert (usage["plan"], usage["limit"], usage["used"], usage["remaining"], usage["unlimited"]) == ("free", 5, 1, 4, False)
    assert usage["reset_at"]
    row = (await db.execute(select(ApplicationLinkAction))).scalar_one()
    assert row.job_id == job.id and row.user_id == user.id and row.counted is True and row.created_at is not None


async def test_five_allowed_sixth_blocked(client, db, user_factory, clock):
    _, headers = await user_factory()
    jobs = await make_jobs(db, 6)
    for i, job in enumerate(jobs[:5], start=1):
        r = await open_link(client, headers, job)
        assert r.status_code == 200 and r.json()["data"]["usage"]["remaining"] == 5 - i
        clock.offset += timedelta(minutes=10)
    r = await open_link(client, headers, jobs[5])
    assert r.status_code == 429
    body = r.json()
    assert body["success"] is False and body["message"] == LIMIT_MSG
    assert body["error"]["code"] == "DAILY_APPLICATION_LIMIT_REACHED"
    data = body["data"]
    assert (data["code"], data["limit"], data["used"], data["remaining"], data["upgrade_available"]) == (
        "DAILY_APPLICATION_LIMIT_REACHED", 5, 5, 0, True)
    assert data["reset_at"] and body["error"]["details"] == data
    assert int(r.headers["Retry-After"]) > 0
    assert "apply_url" not in str(body)  # no link handed out


async def test_reopening_same_job_is_free_even_at_limit(client, db, user_factory, clock):
    user, headers = await user_factory()
    jobs = await make_jobs(db, 5)
    for job in jobs:
        await open_link(client, headers, job)
    again = await open_link(client, headers, jobs[2])
    assert again.status_code == 200 and again.json()["data"]["already_opened"] is True
    assert again.json()["data"]["usage"]["used"] == 5
    assert await counted_rows(db, user.id) == 5


async def test_access_restored_on_rolling_window(client, db, user_factory, clock):
    _, headers = await user_factory()
    jobs = await make_jobs(db, 7)
    for job in jobs[:5]:
        await open_link(client, headers, job)
        clock.offset += timedelta(hours=1)  # actions at +0h .. +4h
    blocked = await open_link(client, headers, jobs[5])
    assert blocked.status_code == 429
    reset_at = blocked.json()["data"]["reset_at"]
    clock.offset = timedelta(hours=24, seconds=1)  # only the first action has aged out
    r = await open_link(client, headers, jobs[5])
    assert r.status_code == 200 and r.json()["data"]["usage"]["remaining"] == 0
    assert (await open_link(client, headers, jobs[6])).status_code == 429
    assert reset_at < r.json()["data"]["usage"]["reset_at"]  # next reset follows the next-oldest action


async def test_limits_are_per_user_and_survive_new_logins(client, db, user_factory, clock):
    user_a, headers_a = await user_factory()
    _, headers_b = await user_factory()
    jobs = await make_jobs(db, 6)
    for job in jobs[:5]:
        await open_link(client, headers_a, job)
    assert (await open_link(client, headers_b, jobs[0])).status_code == 200  # B unaffected
    new_device = auth_header(make_token(user_a.id, email=user_a.email))  # new session / device
    assert (await open_link(client, new_device, jobs[5])).status_code == 429


# ====================================================================== Pro plan
async def test_pro_users_unlimited(client, db, user_factory, clock):
    user, headers = await user_factory()
    await make_pro(db, user)
    jobs = await make_jobs(db, 8)
    for job in jobs:
        r = await open_link(client, headers, job)
        assert r.status_code == 200
        assert r.json()["data"]["usage"] == {"plan": "pro", "unlimited": True, "limit": None, "used": 0,
                                             "remaining": None, "reset_at": None}
    assert await counted_rows(db, user.id) == 0  # recorded, never counted


async def test_admin_unlimited(client, db, user_factory, clock):
    _, headers = await user_factory(UserRole.ADMIN)
    jobs = await make_jobs(db, 6)
    assert all([(await open_link(client, headers, j)).status_code == 200 for j in jobs])


@pytest.mark.parametrize(("kwargs", "label"), [
    ({"expires_in": timedelta(hours=-1)}, "expired"),
    ({"status": SubscriptionStatus.REFUNDED, "active": False}, "refunded"),
    ({"status": SubscriptionStatus.TRANSFERRED}, "transferred"),
    ({"active": False}, "no entitlement"),
])
async def test_inactive_pro_subscriptions_get_free_limit(client, db, user_factory, clock, kwargs, label):
    user, headers = await user_factory()
    await make_pro(db, user, **kwargs)
    jobs = await make_jobs(db, 6)
    codes = [(await open_link(client, headers, j)).status_code for j in jobs]
    assert codes == [200] * 5 + [429], label


async def test_pro_actions_do_not_count_after_downgrade(client, db, user_factory, clock):
    user, headers = await user_factory()
    await make_pro(db, user)
    jobs = await make_jobs(db, 11)
    for job in jobs[:6]:
        await open_link(client, headers, job)
    sub = (await db.execute(select(Subscription).where(Subscription.user_id == user.id))).scalar_one()
    sub.expiration_at = utcnow() - timedelta(minutes=1)
    await db.commit()
    codes = [(await open_link(client, headers, j)).status_code for j in jobs[6:]]
    assert codes == [200] * 5  # full Free allowance after Pro ends


async def test_client_cannot_claim_pro(client, db, user_factory, clock):
    _, headers = await user_factory()
    jobs = await make_jobs(db, 6)
    for job in jobs[:5]:
        await client.post(f"/api/v1/jobs/{job.id}/apply-link?plan=pro&unlimited=true",
                          headers={**headers, "X-Plan": "pro"}, json={"plan": "pro"})
    r = await client.post(f"/api/v1/jobs/{jobs[5].id}/apply-link?plan=pro", headers={**headers, "X-Plan": "pro"})
    assert r.status_code == 429


# ====================================================================== concurrency & idempotency
async def test_concurrent_requests_cannot_exceed_limit(client, db, user_factory, clock):
    user, headers = await user_factory()
    jobs = await make_jobs(db, 9)
    results = await asyncio.gather(*(open_link(client, headers, j) for j in jobs))
    codes = sorted(r.status_code for r in results)
    assert codes.count(200) == 5 and codes.count(429) == 4, codes
    assert await counted_rows(db, user.id) == 5


async def test_idempotency_key_counts_once(client, db, user_factory, clock):
    user, headers = await user_factory()
    jobs = await make_jobs(db, 2)
    first = await open_link(client, headers, jobs[0], key="tap-0001-abcdef")
    retry = await open_link(client, headers, jobs[0], key="tap-0001-abcdef")
    assert first.status_code == retry.status_code == 200
    assert retry.json()["data"]["already_opened"] is True and retry.json()["data"]["usage"]["used"] == 1
    clash = await open_link(client, headers, jobs[1], key="tap-0001-abcdef")
    assert clash.status_code == 409 and clash.json()["error"]["code"] == "idempotency_key_reused"
    assert await counted_rows(db, user.id) == 1


async def test_concurrent_retries_with_same_key_count_once(client, db, user_factory, clock):
    user, headers = await user_factory()
    [job] = await make_jobs(db, 1)
    results = await asyncio.gather(*(open_link(client, headers, job, key="tap-same-key-123") for _ in range(5)))
    assert all(r.status_code == 200 for r in results)
    assert await counted_rows(db, user.id) == 1


# ====================================================================== invalid / unauthenticated
async def test_invalid_and_unauthenticated_requests(client, db, user_factory, clock):
    _, headers = await user_factory()
    [job] = await make_jobs(db, 1)
    assert (await client.post(f"/api/v1/jobs/{job.id}/apply-link")).status_code == 401
    r = await client.post(f"/api/v1/jobs/{job.id}/apply-link", headers=auth_header(make_token(expires_in=-300)))
    assert r.status_code == 401
    assert (await client.post(f"/api/v1/jobs/{uuid.uuid4()}/apply-link", headers=headers)).status_code == 404
    assert (await client.post("/api/v1/jobs/not-a-uuid/apply-link", headers=headers)).status_code == 422
    assert (await open_link(client, headers, job, key="bad key with spaces")).status_code == 422
    [no_link] = await make_jobs(db, 1, apply_url=None, apply_options=[])
    r = await open_link(client, headers, no_link)
    assert r.status_code == 404 and r.json()["error"]["code"] == "apply_link_unavailable"
    assert (await client.get("/api/v1/jobs/application-usage")).status_code == 401
    assert (await client.get("/api/v1/jobs/application-usage", headers=headers)).json()["data"]["used"] == 0


async def test_internal_sharkhire_jobs_are_not_external_links(client, db, user_factory, clock):
    _, headers = await user_factory()
    [job] = await make_jobs(db, 1, source=JobSource.SHARKHIRE)
    r = await open_link(client, headers, job)
    assert r.status_code in (400, 404)  # unpublished internal job is not visible; published ones are 400


async def test_paywalled_user_blocked_before_limit(client, db, user_factory, monkeypatch):
    from app.services.billing import subscriptions as subs
    from app.services.billing import trial as trial_service

    _, headers = await user_factory()
    [job] = await make_jobs(db, 1)
    await client.get("/api/v1/subscriptions/status", headers=headers)
    later = lambda: utcnow() + timedelta(days=4)  # noqa: E731
    monkeypatch.setattr(trial_service, "utcnow", later)
    monkeypatch.setattr(subs, "utcnow", later)
    r = await open_link(client, headers, job)
    assert r.status_code == 403 and r.json()["error"]["code"] == "subscription_required"


# ====================================================================== usage endpoint & what is not counted
async def test_usage_endpoint_is_private_and_matches(client, db, user_factory, clock, jsearch):
    user_a, headers_a = await user_factory()
    _, headers_b = await user_factory()
    jobs = await make_jobs(db, 2)
    for job in jobs:
        await open_link(client, headers_a, job)
    a = (await client.get("/api/v1/jobs/application-usage", headers=headers_a)).json()["data"]
    b = (await client.get("/api/v1/jobs/application-usage", headers=headers_b)).json()["data"]
    assert (a["used"], a["remaining"]) == (2, 3) and (b["used"], b["remaining"]) == (0, 5)
    # Search, details and saving are never counted.
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("x")])))
    await client.get("/api/v1/jobs/search", headers=headers_a, params={"query": "Python Developer"})
    await client.get(f"/api/v1/jobs/{jobs[0].id}", headers=headers_a, params={"refresh": "false"})
    await client.post(f"/api/v1/saved-jobs/{jobs[1].id}", headers=headers_a, json={})
    again = (await client.get("/api/v1/jobs/application-usage", headers=headers_a)).json()["data"]
    assert again["used"] == 2
    assert await counted_rows(db, user_a.id) == 2


# ====================================================================== links withheld from Free users
async def test_free_users_do_not_get_links_in_payloads(client, db, user_factory, clock, jsearch, hide_links):
    _, headers = await user_factory()
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("x1")])))
    item = (await client.get("/api/v1/jobs/search", headers=headers, params={"query": "Python Developer"})).json()["data"]["items"][0]
    assert item["job_apply_link"] is None and item["apply_link_locked"] is True
    detail = (await client.get(f"/api/v1/jobs/{item['id']}", headers=headers, params={"refresh": "false"})).json()["data"]["job"]
    assert detail["job_apply_link"] is None and detail["apply_options"] == [] and detail["apply_link_locked"] is True
    saved = (await client.post(f"/api/v1/saved-jobs/{item['id']}", headers=headers, json={})).json()["data"]
    assert saved["job"]["job_apply_link"] is None
    # Tracking an application does not leak the link either...
    app = (await client.post("/api/v1/applications", headers=headers, json={"job_id": item["id"], "status": "saved"})).json()["data"]
    assert app["job_apply_url"] is None
    # ...but once the user opened it through SharkHire, it is copied.
    opened = await client.post(f"/api/v1/jobs/{item['id']}/apply-link", headers=headers)
    assert opened.status_code == 200 and opened.json()["data"]["apply_url"] == "https://jobs.example.com/apply/123"


async def test_pro_users_see_links_inline(client, db, user_factory, clock, jsearch, hide_links):
    user, headers = await user_factory()
    await make_pro(db, user)
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("x2")])))
    item = (await client.get("/api/v1/jobs/search", headers=headers, params={"query": "Python Developer"})).json()["data"]["items"][0]
    assert item["job_apply_link"] == "https://jobs.example.com/apply/123" and item["apply_link_locked"] is False


async def test_unlocked_job_link_copied_into_tracked_application(client, db, user_factory, clock, hide_links):
    _, headers = await user_factory()
    [job] = await make_jobs(db, 1)
    assert (await open_link(client, headers, job)).status_code == 200
    r = await client.post("/api/v1/applications", headers=headers, json={"job_id": str(job.id), "status": "applied"})
    assert r.status_code == 201 and r.json()["data"]["job_apply_url"] == job.apply_url


# ====================================================================== JSearch + Techmap share one limit
async def providers_of(db, user_id) -> list[str]:
    rows = await db.execute(select(ApplicationLinkAction.provider).where(ApplicationLinkAction.user_id == user_id)
                            .order_by(ApplicationLinkAction.created_at))
    return [p.value for p in rows.scalars()]


@pytest.mark.parametrize("source", [JobSource.JSEARCH, JobSource.TECHMAP])
async def test_each_provider_individually(client, db, user_factory, clock, source):
    user, headers = await user_factory()
    jobs = await make_jobs(db, 6, source=source)
    results = []
    for job in jobs:
        results.append(await open_link(client, headers, job))
        clock.offset += timedelta(seconds=1)
    assert [r.status_code for r in results] == [200] * 5 + [429]
    assert {r.json()["data"]["provider"] for r in results[:5]} == {source.value}
    assert await providers_of(db, user.id) == [source.value] * 5


async def test_mixed_providers_share_one_allowance(client, db, user_factory, clock):
    user, headers = await user_factory()
    jsearch_jobs = await make_jobs(db, 4, source=JobSource.JSEARCH)
    techmap_jobs = await make_jobs(db, 3, source=JobSource.TECHMAP)
    for job in [jsearch_jobs[0], techmap_jobs[0], jsearch_jobs[1], techmap_jobs[1], jsearch_jobs[2]]:  # 3 + 2
        r = await open_link(client, headers, job)
        assert r.status_code == 200
        clock.offset += timedelta(seconds=1)
    assert r.json()["data"]["usage"]["used"] == 5 and r.json()["data"]["usage"]["remaining"] == 0
    assert await providers_of(db, user.id) == ["jsearch", "techmap", "jsearch", "techmap", "jsearch"]
    # The 6th is blocked whichever provider supplies the job.
    for job in (jsearch_jobs[3], techmap_jobs[2]):
        r = await open_link(client, headers, job)
        assert r.status_code == 429 and r.json()["data"]["used"] == 5
    usage = (await client.get("/api/v1/jobs/application-usage", headers=headers)).json()["data"]
    assert (usage["used"], usage["remaining"]) == (5, 0)  # one total, no per-provider allowance
    assert await counted_rows(db, user.id) == 5


async def test_reopening_either_provider_job_is_free(client, db, user_factory, clock):
    user, headers = await user_factory()
    [js] = await make_jobs(db, 1, source=JobSource.JSEARCH)
    [tm] = await make_jobs(db, 1, source=JobSource.TECHMAP)
    for job in (js, tm, js, tm):
        assert (await open_link(client, headers, job)).status_code == 200
    assert await counted_rows(db, user.id) == 2


async def test_pro_unlimited_across_both_providers(client, db, user_factory, clock):
    user, headers = await user_factory()
    await make_pro(db, user)
    jobs = await make_jobs(db, 4, source=JobSource.JSEARCH) + await make_jobs(db, 4, source=JobSource.TECHMAP)
    assert all([(await open_link(client, headers, j)).status_code == 200 for j in jobs])
    assert sorted(await providers_of(db, user.id)) == ["jsearch"] * 4 + ["techmap"] * 4
    assert await counted_rows(db, user.id) == 0


async def test_concurrent_mixed_providers_cannot_exceed_limit(client, db, user_factory, clock):
    user, headers = await user_factory()
    jobs = await make_jobs(db, 5, source=JobSource.JSEARCH) + await make_jobs(db, 4, source=JobSource.TECHMAP)
    results = await asyncio.gather(*(open_link(client, headers, j) for j in jobs))
    assert sorted(r.status_code for r in results).count(200) == 5
    assert await counted_rows(db, user.id) == 5


async def test_techmap_jobs_hidden_and_unlocked_like_jsearch(client, db, user_factory, clock, hide_links):
    _, headers = await user_factory()
    [tm] = await make_jobs(db, 1, source=JobSource.TECHMAP)
    detail = (await client.get(f"/api/v1/jobs/{tm.id}", headers=headers)).json()["data"]["job"]
    assert detail["job_apply_link"] is None and detail["apply_link_locked"] is True
    r = await open_link(client, headers, tm)
    assert r.status_code == 200 and r.json()["data"]["apply_url"] == tm.apply_url and r.json()["data"]["provider"] == "techmap"
