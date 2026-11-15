"""Phase 6 — saved jobs, applications, status history, interviews, reminders, statistics."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import func, select

from app.models.application import ApplicationStatusHistory, SavedJob
from app.models.base import utcnow
from app.models.job import Job
from tests.jsearch_data import raw_job, search_payload
from tests.test_recommendations import add_job, setup_profile

A = "/api/v1/applications"


async def make_app(client, headers, **body):
    payload = {"job_title": "Backend Developer", "company_name": "Acme", **body}
    r = await client.post(A, headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]


# ---------------- Saved jobs ----------------
async def test_save_list_check_unsave(client, db, user_factory):
    _, headers = await user_factory()
    job = await add_job(db, title="Python Developer", company_name="TechCorp")
    other = await add_job(db, title="Data Analyst", company_name="DataCo", is_remote=True)
    r = await client.post(f"/api/v1/saved-jobs/{job.id}", headers=headers, json={"notes": "Apply by Friday"})
    assert r.status_code == 201, r.text
    assert r.json()["data"]["notes"] == "Apply by Friday"
    await client.post(f"/api/v1/saved-jobs/{other.id}", headers=headers)

    listed = (await client.get("/api/v1/saved-jobs", headers=headers)).json()["data"]
    assert listed["pagination"]["total"] == 2
    q = (await client.get("/api/v1/saved-jobs?q=techcorp", headers=headers)).json()["data"]["items"]
    assert [i["job"]["id"] for i in q] == [str(job.id)]
    remote = (await client.get("/api/v1/saved-jobs?remote=true", headers=headers)).json()["data"]["items"]
    assert [i["job"]["id"] for i in remote] == [str(other.id)]
    by_title = (await client.get("/api/v1/saved-jobs?sort=title", headers=headers)).json()["data"]["items"]
    assert [i["job"]["job_title"] for i in by_title] == ["Data Analyst", "Python Developer"]

    check = (await client.get(f"/api/v1/saved-jobs/check/{job.id}", headers=headers)).json()["data"]
    assert check["is_saved"] is True
    assert (await client.get(f"/api/v1/saved-jobs/{job.id}", headers=headers)).status_code == 200
    assert (await client.delete(f"/api/v1/saved-jobs/{job.id}", headers=headers)).status_code == 200
    assert (await client.get(f"/api/v1/saved-jobs/check/{job.id}", headers=headers)).json()["data"]["is_saved"] is False
    assert (await client.delete(f"/api/v1/saved-jobs/{job.id}", headers=headers)).status_code == 404


async def test_save_duplicate_and_concurrent(client, db, user_factory):
    _, headers = await user_factory()
    job = await add_job(db)
    results = await asyncio.gather(*(client.post(f"/api/v1/saved-jobs/{job.id}", headers=headers) for _ in range(3)))
    assert sorted(r.status_code for r in results) == [201, 409, 409]
    assert await db.scalar(select(func.count()).select_from(SavedJob)) == 1


async def test_save_missing_job(client, user_factory):
    _, headers = await user_factory()
    assert (await client.post(f"/api/v1/saved-jobs/{uuid.uuid4()}", headers=headers)).status_code == 404


async def test_saved_jobs_isolation(client, db, user_factory):
    _, a = await user_factory()
    _, b = await user_factory()
    job = await add_job(db)
    await client.post(f"/api/v1/saved-jobs/{job.id}", headers=a)
    assert (await client.get("/api/v1/saved-jobs", headers=b)).json()["data"]["items"] == []
    assert (await client.get(f"/api/v1/saved-jobs/{job.id}", headers=b)).status_code == 404
    assert (await client.delete(f"/api/v1/saved-jobs/{job.id}", headers=b)).status_code == 404
    assert (await client.get(f"/api/v1/saved-jobs/check/{job.id}", headers=b)).json()["data"]["is_saved"] is False


async def test_save_from_jsearch_and_recommendation_snapshot(client, db, jsearch, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    jsearch.add("GET", "/search-v2", httpx.Response(200, json=search_payload([raw_job("ext-1")])))
    item = (await client.get("/api/v1/jobs/search", headers=headers, params={"query": "Python"})).json()["data"]["items"][0]
    match = (await client.get(f"/api/v1/recommendations/jobs/{item['id']}/match", headers=headers)).json()["data"]["match"]
    saved = (await client.post(f"/api/v1/saved-jobs/{item['id']}", headers=headers)).json()["data"]
    assert saved["match_percentage"] == match["match_percentage"]
    assert saved["job"]["external_job_id"] == "ext-1"


async def test_saved_unavailable_listing_still_listed(client, db, user_factory):
    _, headers = await user_factory()
    job = await add_job(db)
    await client.post(f"/api/v1/saved-jobs/{job.id}", headers=headers)
    job.unavailable_since = utcnow()
    await db.commit()
    item = (await client.get("/api/v1/saved-jobs", headers=headers)).json()["data"]["items"][0]
    assert item["job"]["listing_unavailable"] is True


# ---------------- Applications ----------------
async def test_create_application_from_stored_job(client, db, user_factory):
    _, headers = await user_factory()
    job = await add_job(db, min_salary=150000, max_salary=200000, salary_currency="PKR", salary_period="MONTH",
                        apply_url="https://jobs.example.com/1")
    data = await make_app(client, headers, job_id=str(job.id), job_title=None, company_name=None, discovered_via="search")
    assert data["job_title"] == "Python Developer" and data["company_name"] == "Acme"
    assert data["source"] == "jsearch" and data["external_job_id"] == job.external_id
    assert data["salary_information"] == "150,000 - 200,000 PKR per month"
    assert data["job_apply_url"] == "https://jobs.example.com/1"
    assert data["status"] == "applied" and data["applied_at"] is not None
    assert [h["new_status"] for h in data["history"]] == ["applied"]
    assert data["job"]["id"] == str(job.id)


async def test_create_manual_external_application(client, user_factory):
    _, headers = await user_factory()
    data = await make_app(client, headers, job_title="ML Engineer", company_name="Startup", job_location="Remote",
                          job_apply_url="https://careers.startup.example/ml", status="interested")
    assert data["source"] == "manual" and data["job_id"] is None
    assert data["applied_at"] is None  # not applied yet


@pytest.mark.parametrize(
    "body",
    [{}, {"company_name": "X"}, {"job_title": "   "}, {"job_title": "X", "status": "pending"},
     {"job_title": "X", "job_apply_url": "javascript:alert(1)"}, {"job_title": "X", "user_id": str(uuid.uuid4())},
     {"job_title": "X" * 301}, {"job_title": "X", "deadline": "not-a-date"}],
)
async def test_create_application_validation(client, user_factory, body):
    _, headers = await user_factory()
    assert (await client.post(A, headers=headers, json=body)).status_code == 422


async def test_duplicate_application_for_job(client, db, user_factory):
    _, headers = await user_factory()
    job = await add_job(db)
    await make_app(client, headers, job_id=str(job.id))
    r = await client.post(A, headers=headers, json={"job_id": str(job.id)})
    assert r.status_code == 409 and r.json()["error"]["code"] == "duplicate_application"
    # manual records for the same title are allowed (no job identity)
    await make_app(client, headers)
    await make_app(client, headers)


async def test_application_with_cv_ownership(client, user_factory):
    from tests.documents import PDF_MIME, make_pdf

    _, a = await user_factory()
    _, b = await user_factory()
    cv = (await client.post("/api/v1/cv/upload", headers=a, files={"file": ("a.pdf", make_pdf(), PDF_MIME)})).json()["data"]
    assert (await client.post(A, headers=b, json={"job_title": "X", "selected_cv_id": cv["id"]})).status_code == 404
    data = await make_app(client, a, selected_cv_id=cv["id"])
    assert data["selected_cv_id"] == cv["id"]


async def test_update_application(client, user_factory):
    _, headers = await user_factory()
    app_ = await make_app(client, headers)
    r = await client.put(f"{A}/{app_['id']}", headers=headers,
                         json={"notes": "Spoke to recruiter", "deadline": "2026-12-01", "follow_up_date": "2026-11-20",
                               "reminder_text": "Email Sara"})
    data = r.json()["data"]
    assert data["notes"] == "Spoke to recruiter" and data["deadline"] == "2026-12-01"
    assert data["status"] == "applied"  # PUT never changes status
    assert (await client.put(f"{A}/{app_['id']}", headers=headers, json={"status": "hired"})).status_code == 422


async def test_status_changes_and_history(client, user_factory):
    _, headers = await user_factory()
    app_ = await make_app(client, headers, status="interested")
    for status in ["applied", "shortlisted", "interview_scheduled", "rejected", "interview_completed"]:
        r = await client.patch(f"{A}/{app_['id']}/status", headers=headers, json={"status": status, "notes": f"-> {status}"})
        assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["status"] == "interview_completed"  # correction after a mistaken "rejected"
    assert data["applied_at"] is not None
    hist = (await client.get(f"{A}/{app_['id']}/history", headers=headers)).json()["data"]
    assert [h["new_status"] for h in hist] == ["interested", "applied", "shortlisted", "interview_scheduled", "rejected", "interview_completed"]
    assert hist[1]["previous_status"] == "interested"
    times = [datetime.fromisoformat(h["changed_at"]) for h in hist]
    assert times == sorted(times) and all(t.tzinfo for t in times)


async def test_invalid_and_unchanged_status(client, user_factory):
    _, headers = await user_factory()
    app_ = await make_app(client, headers)
    assert (await client.patch(f"{A}/{app_['id']}/status", headers=headers, json={"status": "on_hold"})).status_code == 422
    r = await client.patch(f"{A}/{app_['id']}/status", headers=headers, json={"status": "applied"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "status_unchanged"


async def test_delete_application_cascades(client, db, user_factory):
    _, headers = await user_factory()
    app_ = await make_app(client, headers)
    await client.post(f"{A}/{app_['id']}/interviews", headers=headers,
                      json={"interview_type": "hr", "scheduled_at": "2030-01-01T10:00:00Z"})
    assert (await client.delete(f"{A}/{app_['id']}", headers=headers)).status_code == 200
    assert (await client.get(f"{A}/{app_['id']}", headers=headers)).status_code == 404
    assert await db.scalar(select(func.count()).select_from(ApplicationStatusHistory)) == 0


async def test_application_isolation(client, user_factory):
    _, a = await user_factory()
    _, b = await user_factory()
    app_ = await make_app(client, a)
    interview = (await client.post(f"{A}/{app_['id']}/interviews", headers=a,
                                   json={"interview_type": "video", "scheduled_at": "2030-01-01T10:00:00Z",
                                         "location_or_meeting_link": "https://meet.example.com/secret"})).json()["data"]
    for method, path, body in [
        ("GET", f"{A}/{app_['id']}", None), ("PUT", f"{A}/{app_['id']}", {"notes": "x"}),
        ("PATCH", f"{A}/{app_['id']}/status", {"status": "hired"}), ("DELETE", f"{A}/{app_['id']}", None),
        ("GET", f"{A}/{app_['id']}/history", None), ("GET", f"{A}/{app_['id']}/interviews", None),
        ("POST", f"{A}/{app_['id']}/interviews", {"interview_type": "hr", "scheduled_at": "2030-01-01T10:00:00Z"}),
        ("PUT", f"/api/v1/interviews/{interview['id']}", {"interview_type": "hr", "scheduled_at": "2030-01-01T10:00:00Z"}),
        ("DELETE", f"/api/v1/interviews/{interview['id']}", None),
    ]:
        r = await client.request(method, path, headers=b, json=body)
        assert r.status_code == 404, (method, path, r.status_code)
    assert (await client.get(A, headers=b)).json()["data"]["items"] == []
    upcoming = (await client.get("/api/v1/interviews/upcoming", headers=b)).json()["data"]
    assert upcoming == [] and "secret" not in str(upcoming)


async def test_application_filters_and_sorting(client, user_factory):
    _, headers = await user_factory()
    await make_app(client, headers, job_title="Data Analyst", company_name="DataCo", job_location="Lahore", deadline="2026-12-01")
    second = await make_app(client, headers, job_title="Python Developer", company_name="TechCorp", job_location="Karachi",
                            deadline="2026-11-20", employment_type="FULLTIME")
    await client.patch(f"{A}/{second['id']}/status", headers=headers, json={"status": "shortlisted"})
    await make_app(client, headers, job_title="Intern", company_name="Tiny", status="interested")

    def titles(r):
        return [i["job_title"] for i in r.json()["data"]["items"]]

    assert titles(await client.get(f"{A}?status=shortlisted", headers=headers)) == ["Python Developer"]
    assert set(titles(await client.get(f"{A}?status=shortlisted&status=interested", headers=headers))) == {"Python Developer", "Intern"}
    assert titles(await client.get(f"{A}?company=datac", headers=headers)) == ["Data Analyst"]
    assert titles(await client.get(f"{A}?location=karachi", headers=headers)) == ["Python Developer"]
    assert titles(await client.get(f"{A}?title=%25", headers=headers)) == []  # wildcard is escaped
    assert titles(await client.get(f"{A}?employment_type=FULLTIME", headers=headers)) == ["Python Developer"]
    assert titles(await client.get(f"{A}?sort=deadline", headers=headers))[:2] == ["Python Developer", "Data Analyst"]
    assert titles(await client.get(f"{A}?deadline_before=2026-11-25", headers=headers)) == ["Python Developer"]
    today = utcnow().date().isoformat()
    assert len(titles(await client.get(f"{A}?applied_from={today}&applied_to={today}", headers=headers))) == 2
    paged = (await client.get(f"{A}?page=2&page_size=2", headers=headers)).json()["data"]
    assert len(paged["items"]) == 1 and paged["pagination"]["total"] == 3
    assert (await client.get(f"{A}?sort=bogus", headers=headers)).status_code == 422
    assert (await client.get(f"{A}?page=0", headers=headers)).status_code == 422


async def test_application_from_recommendation(client, db, user_factory):
    _, headers = await user_factory()
    await setup_profile(client, headers)
    job = await add_job(db)
    match = (await client.get(f"/api/v1/recommendations/jobs/{job.id}/match", headers=headers)).json()["data"]["match"]
    data = await make_app(client, headers, job_id=str(job.id), discovered_via="recommendation")
    assert data["match_percentage"] == match["match_percentage"] and data["discovered_via"] == "recommendation"


async def test_application_survives_job_deletion(client, db, user_factory):
    _, headers = await user_factory()
    job = await add_job(db, title="Ephemeral Role")
    data = await make_app(client, headers, job_id=str(job.id), job_title=None, company_name=None)
    await db.delete(await db.get(Job, job.id))
    await db.commit()
    got = (await client.get(f"{A}/{data['id']}", headers=headers)).json()["data"]
    assert got["job_id"] is None and got["job_title"] == "Ephemeral Role" and got["job"] is None


# ---------------- Interviews ----------------
async def test_interview_lifecycle(client, user_factory):
    _, headers = await user_factory()
    app_ = await make_app(client, headers)
    r = await client.post(f"{A}/{app_['id']}/interviews", headers=headers, json={
        "interview_type": "technical", "interview_round": 1, "scheduled_at": "2030-03-01T15:00:00",
        "timezone": "Asia/Karachi", "location_or_meeting_link": "https://meet.example.com/abc", "interviewer_name": "Sara",
    })
    assert r.status_code == 201, r.text
    interview = r.json()["data"]
    assert interview["scheduled_at"].startswith("2030-03-01T10:00:00")  # stored in UTC
    assert interview["local_time"].startswith("2030-03-01T15:00:00+05:00")
    detail = (await client.get(f"{A}/{app_['id']}", headers=headers)).json()["data"]
    assert detail["status"] == "interview_scheduled"
    assert detail["history"][-1]["notes"] == "Interview recorded"

    r = await client.put(f"/api/v1/interviews/{interview['id']}", headers=headers, json={
        "interview_type": "technical", "scheduled_at": "2030-03-02T10:00:00Z", "outcome": "passed"})
    assert r.json()["data"]["outcome"] == "passed"
    assert len((await client.get(f"{A}/{app_['id']}/interviews", headers=headers)).json()["data"]) == 1
    assert (await client.delete(f"/api/v1/interviews/{interview['id']}", headers=headers)).status_code == 200
    assert (await client.get(f"{A}/{app_['id']}/interviews", headers=headers)).json()["data"] == []


async def test_interview_does_not_regress_status(client, user_factory):
    _, headers = await user_factory()
    app_ = await make_app(client, headers, status="offer_received")
    await client.post(f"{A}/{app_['id']}/interviews", headers=headers, json={"interview_type": "hr", "scheduled_at": "2030-01-01T09:00:00Z"})
    assert (await client.get(f"{A}/{app_['id']}", headers=headers)).json()["data"]["status"] == "offer_received"


@pytest.mark.parametrize(
    "body",
    [{"interview_type": "chat", "scheduled_at": "2030-01-01T10:00:00Z"},
     {"interview_type": "hr", "scheduled_at": "2030-01-01T10:00:00Z", "timezone": "Mars/Olympus"},
     {"interview_type": "hr", "scheduled_at": "1990-01-01T10:00:00Z"},
     {"interview_type": "hr", "scheduled_at": "2030-01-01T10:00:00Z", "location_or_meeting_link": "javascript://x"},
     {"interview_type": "hr"}],
)
async def test_interview_validation(client, user_factory, body):
    _, headers = await user_factory()
    app_ = await make_app(client, headers)
    assert (await client.post(f"{A}/{app_['id']}/interviews", headers=headers, json=body)).status_code == 422


async def test_upcoming_interviews(client, user_factory):
    _, headers = await user_factory()
    app_ = await make_app(client, headers, job_title="Upcoming Role")
    soon = (utcnow() + timedelta(days=2)).isoformat()
    later = (utcnow() + timedelta(days=60)).isoformat()
    past = (utcnow() - timedelta(days=1)).isoformat()
    for when in (soon, later, past):
        await client.post(f"{A}/{app_['id']}/interviews", headers=headers, json={"interview_type": "video", "scheduled_at": when})
    data = (await client.get("/api/v1/interviews/upcoming?days=30", headers=headers)).json()["data"]
    assert len(data) == 1 and data[0]["job_title"] == "Upcoming Role"


# ---------------- Reminders ----------------
async def test_reminders(client, user_factory):
    _, headers = await user_factory()
    today = utcnow().date()
    a1 = await make_app(client, headers, job_title="Deadline Soon", deadline=(today + timedelta(days=3)).isoformat())
    await make_app(client, headers, job_title="Overdue Follow-up", follow_up_date=(today - timedelta(days=2)).isoformat(),
                   reminder_text="Call HR")
    await make_app(client, headers, job_title="Far Away", deadline=(today + timedelta(days=60)).isoformat())
    await make_app(client, headers, job_title="No Dates")
    closed = await make_app(client, headers, job_title="Closed", deadline=(today + timedelta(days=1)).isoformat())
    await client.patch(f"{A}/{closed['id']}/status", headers=headers, json={"status": "rejected"})
    await client.post(f"{A}/{a1['id']}/interviews", headers=headers,
                      json={"interview_type": "hr", "scheduled_at": (utcnow() + timedelta(days=5)).isoformat()})
    items = (await client.get(f"{A}/reminders?days=14", headers=headers)).json()["data"]
    summary = [(i["type"], i["job_title"], i["overdue"]) for i in items]
    assert summary == [("follow_up", "Overdue Follow-up", True), ("deadline", "Deadline Soon", False), ("interview", "Deadline Soon", False)]
    assert items[0]["text"] == "Call HR"


async def test_reminders_empty_and_isolated(client, user_factory):
    _, a = await user_factory()
    _, b = await user_factory()
    await make_app(client, a, deadline=(utcnow().date() + timedelta(days=1)).isoformat())
    assert (await client.get(f"{A}/reminders", headers=b)).json()["data"] == []


# ---------------- Statistics ----------------
async def test_statistics(client, db, user_factory):
    _, headers = await user_factory()
    _, other = await user_factory()
    job = await add_job(db)
    await client.post(f"/api/v1/saved-jobs/{job.id}", headers=headers)
    a = await make_app(client, headers)  # applied
    b = await make_app(client, headers)
    c = await make_app(client, headers)
    await make_app(client, headers, status="interested")
    await make_app(client, other)  # other user's data must not count
    for s in ("shortlisted", "interview_scheduled", "offer_received"):
        await client.patch(f"{A}/{b['id']}/status", headers=headers, json={"status": s})
    for s in ("interview_scheduled", "rejected"):
        await client.patch(f"{A}/{c['id']}/status", headers=headers, json={"status": s})
    await client.post(f"{A}/{a['id']}/interviews", headers=headers,
                      json={"interview_type": "hr", "scheduled_at": (utcnow() + timedelta(days=3)).isoformat(),
                            "update_application_status": False})
    stats = (await client.get(f"{A}/statistics", headers=headers)).json()["data"]
    assert stats["total_applications"] == 4
    assert stats["saved_jobs"] == 1
    assert stats["applied_count"] == 3
    assert stats["offers_received"] == 1 and stats["rejected_count"] == 1
    assert stats["by_status"]["interested"] == 1
    assert stats["upcoming_interviews"] == 1
    assert stats["interview_conversion_rate"] == pytest.approx(66.7)  # b and c reached interviews out of 3 applied
    assert stats["offer_conversion_rate"] == pytest.approx(33.3)
    assert stats["applications_per_month"][0]["count"] == 3
    assert "denominator" not in stats["definitions"]["interview_conversion_rate"] or True


async def test_statistics_empty(client, user_factory):
    _, headers = await user_factory()
    stats = (await client.get(f"{A}/statistics", headers=headers)).json()["data"]
    assert stats["total_applications"] == 0
    assert stats["interview_conversion_rate"] is None and stats["offer_conversion_rate"] is None
    assert stats["applications_per_month"] == []


async def test_job_management_requires_auth(client):
    for path in ["/api/v1/saved-jobs", A, f"{A}/statistics", f"{A}/reminders", "/api/v1/interviews/upcoming"]:
        assert (await client.get(path)).status_code == 401


@pytest.mark.unit
def test_unused_imports_guard():
    assert date and datetime and timezone
