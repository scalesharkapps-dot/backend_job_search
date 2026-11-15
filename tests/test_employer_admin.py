"""Phase 7 — employer & admin backend: workflows and authorization/security tests."""

from __future__ import annotations

import uuid
from datetime import timedelta
from urllib.parse import urlparse

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.models.base import utcnow
from app.models.employer import AdminAuditLog
from app.models.user import User, UserRole
from tests.documents import PDF_MIME, make_image, make_pdf

COMPANY = {"company_name": "Acme Pvt Ltd", "contact_email": "hr@acme.example", "website": "https://acme.example",
           "industry": "Software", "company_size": "11-50", "country": "PK", "contact_phone": "+92 21 1234567"}
JOB = {"title": "Backend Python Developer", "description": "Build FastAPI services for our hiring platform. " * 2,
       "required_skills": ["Python", "FastAPI"], "preferred_skills": ["Docker"], "employment_type": "FULLTIME",
       "workplace_type": "onsite", "city": "Karachi", "country": "PK", "min_experience_years": 2,
       "min_salary": 150000, "max_salary": 250000, "salary_currency": "PKR", "salary_period": "MONTH", "openings": 2}


async def register_employer(client, user_factory, company=None):
    user, headers = await user_factory()
    r = await client.post("/api/v1/employers/register", headers=headers, json=company or COMPANY)
    assert r.status_code == 201, r.text
    return user, headers, r.json()["data"]


async def verified_employer(client, user_factory, admin_headers):
    user, headers, employer = await register_employer(client, user_factory)
    r = await client.post("/api/v1/employers/me/verification", headers=headers, json={"business_registration_number": "SECP-123"})
    assert r.status_code == 201, r.text
    r = await client.patch(f"/api/v1/admin/employers/{employer['id']}/verification", headers=admin_headers, json={"decision": "approve"})
    assert r.status_code == 200, r.text
    return user, headers, employer


async def published_job(client, emp_headers, admin_headers, **overrides):
    r = await client.post("/api/v1/employer/jobs", headers=emp_headers, json={**JOB, **overrides, "submit_for_review": True})
    assert r.status_code == 201, r.text
    job = r.json()["data"]
    assert job["posting_status"] == "pending_review"
    r = await client.patch(f"/api/v1/admin/jobs/{job['id']}/moderation", headers=admin_headers, json={"decision": "approve"})
    assert r.status_code == 200, r.text
    return r.json()["data"]


async def seeker_with_cv(client, user_factory):
    user, headers = await user_factory()
    cv = (await client.post("/api/v1/cv/upload", headers=headers, files={"file": ("cv.pdf", make_pdf(), PDF_MIME)})).json()["data"]
    return user, headers, cv


@pytest.fixture
async def admin_headers(user_factory):
    _, headers = await user_factory(UserRole.ADMIN)
    return headers


# ---------------- Registration & profile ----------------
async def test_employer_registration(client, db, user_factory):
    user, headers, employer = await register_employer(client, user_factory)
    assert employer["verification_status"] == "unverified" and employer["account_status"] == "active"
    assert employer["contact_phone"] == "+92211234567"
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()["data"]
    assert me["role"] == "employer"
    log = await db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "employer.register"))
    assert log.actor_user_id == user.id
    again = await client.post("/api/v1/employers/register", headers=headers, json=COMPANY)
    assert again.status_code == 409


@pytest.mark.parametrize("bad", [{"company_name": "A"}, {"contact_email": "nope"}, {"website": "javascript:alert(1)"},
                                 {"company_size": "huge"}, {"country": "Pakistan"}, {"verification_status": "verified"}])
async def test_employer_registration_validation(client, user_factory, bad):
    _, headers = await user_factory()
    assert (await client.post("/api/v1/employers/register", headers=headers, json={**COMPANY, **bad})).status_code == 422


async def test_registration_requires_auth_and_blocks_admin(client, user_factory, admin_headers):
    assert (await client.post("/api/v1/employers/register", json=COMPANY)).status_code == 401
    r = await client.post("/api/v1/employers/register", headers=admin_headers, json=COMPANY)
    assert r.status_code == 403


async def test_employer_profile_update_and_logo(client, user_factory):
    _, headers, _ = await register_employer(client, user_factory)
    r = await client.put("/api/v1/employers/me", headers=headers, json={**COMPANY, "description": "We build things."})
    assert r.json()["data"]["description"] == "We build things."
    r = await client.post("/api/v1/employers/me/logo", headers=headers, files={"file": ("logo.png", make_image(), "image/png")})
    assert r.status_code == 200 and r.json()["data"]["logo_url"]
    assert (await client.delete("/api/v1/employers/me/logo", headers=headers)).status_code == 200


async def test_public_profile_hides_contact(client, user_factory):
    _, _, employer = await register_employer(client, user_factory)
    _, viewer = await user_factory()
    r = await client.get(f"/api/v1/employers/{employer['id']}", headers=viewer)
    assert r.status_code == 200
    assert "contact_email" not in r.json()["data"] and "hr@acme.example" not in r.text
    assert (await client.get(f"/api/v1/employers/{uuid.uuid4()}", headers=viewer)).status_code == 404


async def test_job_seeker_cannot_use_employer_endpoints(client, user_factory):
    _, headers = await user_factory()
    for method, path in [("GET", "/api/v1/employers/me"), ("GET", "/api/v1/employer/jobs"),
                         ("POST", "/api/v1/employer/jobs"), ("GET", "/api/v1/employer/applications"),
                         ("GET", "/api/v1/employer/statistics")]:
        r = await client.request(method, path, headers=headers, json=JOB if method == "POST" else None)
        assert r.status_code == 403, (method, path)


async def test_employer_role_without_profile(client, user_factory):
    _, headers = await user_factory(UserRole.EMPLOYER)
    r = await client.get("/api/v1/employer/jobs", headers=headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "employer_profile_required"


# ---------------- Verification ----------------
async def test_verification_workflow(client, user_factory, admin_headers):
    _, headers, employer = await register_employer(client, user_factory)
    state = (await client.get("/api/v1/employers/me/verification", headers=headers)).json()["data"]
    assert state == {"verification_status": "unverified", "can_submit": True, "latest_request": None}
    assert (await client.post("/api/v1/employers/me/verification", headers=headers, json={})).status_code == 422
    r = await client.post("/api/v1/employers/me/verification", headers=headers, json={"company_website": "https://acme.example"})
    assert r.status_code == 201
    assert (await client.post("/api/v1/employers/me/verification", headers=headers, json={"company_website": "https://acme.example"})).status_code == 409

    url = f"/api/v1/admin/employers/{employer['id']}/verification"
    assert (await client.patch(url, headers=admin_headers, json={"decision": "reject"})).status_code == 422  # reason required
    assert (await client.patch(url, headers=admin_headers, json={"decision": "under_review"})).json()["data"]["verification_status"] == "under_review"
    r = await client.patch(url, headers=admin_headers, json={"decision": "reject", "reason": "Registration number not found"})
    assert r.json()["data"]["verification_status"] == "rejected"
    state = (await client.get("/api/v1/employers/me/verification", headers=headers)).json()["data"]
    assert state["can_submit"] is True and state["latest_request"]["decision_reason"] == "Registration number not found"
    # resubmission and approval
    await client.post("/api/v1/employers/me/verification", headers=headers, json={"business_registration_number": "SECP-999"})
    r = await client.patch(url, headers=admin_headers, json={"decision": "approve"})
    assert r.json()["data"]["verification_status"] == "verified" and r.json()["data"]["verified_at"]
    assert (await client.patch(url, headers=admin_headers, json={"decision": "approve"})).status_code == 409


async def test_unverified_employer_cannot_publish(client, user_factory):
    _, headers, _ = await register_employer(client, user_factory)
    r = await client.post("/api/v1/employer/jobs", headers=headers, json={**JOB, "submit_for_review": True})
    assert r.status_code == 403 and r.json()["error"]["code"] == "employer_not_verified"
    draft = (await client.post("/api/v1/employer/jobs", headers=headers, json=JOB)).json()["data"]
    assert draft["posting_status"] == "draft"
    r = await client.patch(f"/api/v1/employer/jobs/{draft['id']}/status", headers=headers, json={"status": "pending_review"})
    assert r.status_code == 403


# ---------------- Job postings ----------------
async def test_job_lifecycle(client, user_factory, admin_headers):
    _, headers, _ = await verified_employer(client, user_factory, admin_headers)
    _, seeker = await user_factory()
    job = await published_job(client, headers, admin_headers)
    assert job["posting_status"] == "published" and job["published_at"]
    listed = (await client.get("/api/v1/jobs/sharkhire", headers=seeker)).json()["data"]["items"]
    assert [j["id"] for j in listed] == [job["id"]] and listed[0]["source"] == "sharkhire"
    assert (await client.get(f"/api/v1/jobs/{job['id']}", headers=seeker)).status_code == 200

    status_url = f"/api/v1/employer/jobs/{job['id']}/status"
    assert (await client.patch(status_url, headers=headers, json={"status": "paused"})).json()["data"]["posting_status"] == "paused"
    assert (await client.get(f"/api/v1/jobs/{job['id']}", headers=seeker)).status_code == 404  # hidden while paused
    assert (await client.patch(status_url, headers=headers, json={"status": "published"})).json()["data"]["posting_status"] == "published"
    assert (await client.patch(status_url, headers=headers, json={"status": "closed"})).json()["data"]["posting_status"] == "closed"
    assert (await client.get("/api/v1/jobs/sharkhire", headers=seeker)).json()["data"]["items"] == []
    r = await client.patch(status_url, headers=headers, json={"status": "published"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "invalid_transition"
    assert (await client.delete(f"/api/v1/employer/jobs/{job['id']}", headers=headers)).json()["data"]["result"] == "archived"


async def test_employer_cannot_self_approve_or_unsuspend(client, user_factory, admin_headers):
    _, headers, _ = await verified_employer(client, user_factory, admin_headers)
    job = (await client.post("/api/v1/employer/jobs", headers=headers, json={**JOB, "submit_for_review": True})).json()["data"]
    r = await client.patch(f"/api/v1/employer/jobs/{job['id']}/status", headers=headers, json={"status": "published"})
    assert r.status_code == 409
    await client.patch(f"/api/v1/admin/jobs/{job['id']}/moderation", headers=admin_headers, json={"decision": "approve"})
    await client.patch(f"/api/v1/admin/jobs/{job['id']}/status", headers=admin_headers, json={"status": "suspended", "reason": "Suspicious"})
    for target in ("published", "paused", "closed", "archived"):
        r = await client.patch(f"/api/v1/employer/jobs/{job['id']}/status", headers=headers, json={"status": target})
        assert r.status_code == 409, target
    assert (await client.put(f"/api/v1/employer/jobs/{job['id']}", headers=headers, json=JOB)).status_code == 409


async def test_moderation_reject_and_resubmit(client, user_factory, admin_headers):
    _, headers, _ = await verified_employer(client, user_factory, admin_headers)
    job = (await client.post("/api/v1/employer/jobs", headers=headers, json={**JOB, "submit_for_review": True})).json()["data"]
    url = f"/api/v1/admin/jobs/{job['id']}/moderation"
    assert (await client.patch(url, headers=admin_headers, json={"decision": "reject"})).status_code == 422
    r = await client.patch(url, headers=admin_headers, json={"decision": "reject", "reason": "Salary unrealistic"})
    assert r.json()["data"]["posting_status"] == "rejected"
    mine = (await client.get(f"/api/v1/employer/jobs/{job['id']}", headers=headers)).json()["data"]
    assert mine["moderation_note"] == "Salary unrealistic"
    await client.put(f"/api/v1/employer/jobs/{job['id']}", headers=headers, json={**JOB, "max_salary": 200000})
    r = await client.patch(f"/api/v1/employer/jobs/{job['id']}/status", headers=headers, json={"status": "pending_review"})
    assert r.json()["data"]["posting_status"] == "pending_review"
    assert (await client.patch(url, headers=admin_headers, json={"decision": "approve"})).json()["data"]["posting_status"] == "published"
    assert (await client.patch(url, headers=admin_headers, json={"decision": "approve"})).status_code == 409


async def test_editing_published_job_requires_rereview(client, user_factory, admin_headers):
    _, headers, _ = await verified_employer(client, user_factory, admin_headers)
    job = await published_job(client, headers, admin_headers)
    r = await client.put(f"/api/v1/employer/jobs/{job['id']}", headers=headers, json={**JOB, "title": "Senior Backend Developer"})
    assert r.json()["data"]["posting_status"] == "pending_review"


async def test_moderation_disabled_publishes_directly(client, user_factory, admin_headers):
    get_settings().JOB_MODERATION_ENABLED = False
    _, headers, _ = await verified_employer(client, user_factory, admin_headers)
    r = await client.post("/api/v1/employer/jobs", headers=headers, json={**JOB, "submit_for_review": True})
    assert r.json()["data"]["posting_status"] == "published"


@pytest.mark.parametrize("bad", [
    {"title": "X"}, {"description": "short"}, {"employment_type": "SOMETIMES"},
    {"min_salary": 300000, "max_salary": 100000}, {"min_salary": 1000, "salary_currency": None},
    {"external_application_url": "javascript:alert(1)"}, {"workplace_type": "onsite", "city": None, "country": None},
    {"min_experience_years": 5, "max_experience_years": 2}, {"salary_currency": "RUPEES"},
    {"posting_status": "published"}, {"employer_id": str(uuid.uuid4())},
])
async def test_job_validation(client, user_factory, bad):
    _, headers, _ = await register_employer(client, user_factory)
    assert (await client.post("/api/v1/employer/jobs", headers=headers, json={**JOB, **bad})).status_code == 422


async def test_draft_deletion_and_deadline_expiry(client, db, user_factory, admin_headers):
    _, headers, _ = await verified_employer(client, user_factory, admin_headers)
    draft = (await client.post("/api/v1/employer/jobs", headers=headers, json=JOB)).json()["data"]
    assert (await client.delete(f"/api/v1/employer/jobs/{draft['id']}", headers=headers)).json()["data"]["result"] == "deleted"
    assert (await client.get(f"/api/v1/employer/jobs/{draft['id']}", headers=headers)).status_code == 404
    tomorrow = (utcnow().date() + timedelta(days=1)).isoformat()
    job = await published_job(client, headers, admin_headers, application_deadline=tomorrow)
    from app.models.job import Job

    row = await db.get(Job, uuid.UUID(job["id"]))
    row.application_deadline = utcnow().date() - timedelta(days=1)
    await db.commit()
    _, seeker = await user_factory()
    assert (await client.get(f"/api/v1/jobs/{job['id']}", headers=seeker)).status_code == 404
    mine = (await client.get("/api/v1/employer/jobs", headers=headers)).json()["data"]["items"]
    assert mine[0]["posting_status"] == "expired"
    past = (utcnow().date() - timedelta(days=1)).isoformat()
    r = await client.post("/api/v1/employer/jobs", headers=headers, json={**JOB, "application_deadline": past, "submit_for_review": True})
    assert r.status_code == 422 and r.json()["error"]["code"] == "deadline_passed"


async def test_cross_employer_isolation(client, user_factory, admin_headers):
    _, h1, _ = await verified_employer(client, user_factory, admin_headers)
    _, h2, _ = await register_employer(client, user_factory, {**COMPANY, "company_name": "Other Corp"})
    job = await published_job(client, h1, admin_headers)
    for method, path, body in [
        ("GET", f"/api/v1/employer/jobs/{job['id']}", None),
        ("PUT", f"/api/v1/employer/jobs/{job['id']}", JOB),
        ("PATCH", f"/api/v1/employer/jobs/{job['id']}/status", {"status": "closed"}),
        ("DELETE", f"/api/v1/employer/jobs/{job['id']}", None),
        ("GET", f"/api/v1/employer/jobs/{job['id']}/applicants", None),
    ]:
        r = await client.request(method, path, headers=h2, json=body)
        assert r.status_code == 404, (method, path)
    assert (await client.get("/api/v1/employer/jobs", headers=h2)).json()["data"]["items"] == []


# ---------------- Internal applications & applicants ----------------
async def test_internal_application_flow(client, db, user_factory, admin_headers):
    _, emp, _ = await verified_employer(client, user_factory, admin_headers)
    job = await published_job(client, emp, admin_headers)
    seeker_user, seeker, cv = await seeker_with_cv(client, user_factory)
    await client.put("/api/v1/profile/me", headers=seeker, json={"full_name": "Ayesha Khan", "skills": ["Python"], "city": "Karachi"})

    r = await client.post("/api/v1/applications", headers=seeker, json={"job_id": job["id"]})
    assert r.status_code == 422 and r.json()["error"]["code"] == "cv_required"
    r = await client.post("/api/v1/applications", headers=seeker,
                          json={"job_id": job["id"], "selected_cv_id": cv["id"], "cover_letter": "I love FastAPI.",
                                "notes": "My private note"})
    assert r.status_code == 201, r.text
    app_ = r.json()["data"]
    assert app_["source"] == "sharkhire" and app_["employer_status"] == "submitted" and app_["submitted_at"]

    applicants = (await client.get(f"/api/v1/employer/jobs/{job['id']}/applicants", headers=emp)).json()["data"]
    assert applicants["pagination"]["total"] == 1
    a = applicants["items"][0]
    assert a["applicant"]["full_name"] == "Ayesha Khan" and a["cover_letter"] == "I love FastAPI."
    assert "My private note" not in str(applicants)

    r = await client.patch(f"/api/v1/employer/applications/{app_['id']}/status", headers=emp,
                           json={"status": "shortlisted", "notes": "Strong candidate"})
    assert r.json()["data"]["employer_status"] == "shortlisted"
    mine = (await client.get(f"/api/v1/applications/{app_['id']}", headers=seeker)).json()["data"]
    assert mine["status"] == "applied" and mine["notes"] == "My private note"  # candidate tracker untouched
    assert "Strong candidate" not in str(mine)
    assert [h["new_status"] for h in mine["history"]] == ["applied"]  # employer history is separate

    filtered = (await client.get("/api/v1/employer/applications?status=shortlisted", headers=emp)).json()["data"]
    assert filtered["pagination"]["total"] == 1
    stats = (await client.get("/api/v1/employer/statistics", headers=emp)).json()["data"]
    assert stats["total_applicants"] == 1 and stats["shortlisted"] == 1 and stats["jobs_by_status"]["published"] == 1

    # withdrawal is visible to the employer and final
    await client.patch(f"/api/v1/applications/{app_['id']}/status", headers=seeker, json={"status": "withdrawn"})
    got = (await client.get(f"/api/v1/employer/applications/{app_['id']}", headers=emp)).json()["data"]
    assert got["candidate_withdrawn"] is True
    r = await client.patch(f"/api/v1/employer/applications/{app_['id']}/status", headers=emp, json={"status": "offered"})
    assert r.status_code == 409
    r = await client.patch(f"/api/v1/applications/{app_['id']}/status", headers=seeker, json={"status": "applied"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "withdrawal_final"


async def test_internal_application_rules(client, user_factory, admin_headers):
    _, emp, _ = await verified_employer(client, user_factory, admin_headers)
    job = await published_job(client, emp, admin_headers)
    _, seeker, cv = await seeker_with_cv(client, user_factory)
    app_ = (await client.post("/api/v1/applications", headers=seeker, json={"job_id": job["id"], "selected_cv_id": cv["id"]})).json()["data"]
    r = await client.delete(f"/api/v1/applications/{app_['id']}", headers=seeker)
    assert r.status_code == 409 and r.json()["error"]["code"] == "withdraw_first"
    # employers cannot apply to jobs
    r = await client.post("/api/v1/applications", headers=emp, json={"job_id": job["id"], "selected_cv_id": cv["id"]})
    assert r.status_code in (403, 404)
    # draft jobs cannot be applied to
    draft = (await client.post("/api/v1/employer/jobs", headers=emp, json=JOB)).json()["data"]
    assert (await client.post("/api/v1/applications", headers=seeker, json={"job_id": draft["id"], "selected_cv_id": cv["id"]})).status_code == 404


async def test_applicant_cv_access(client, db, user_factory, admin_headers):
    _, emp, _ = await verified_employer(client, user_factory, admin_headers)
    _, other_emp, _ = await verified_employer(client, user_factory, admin_headers)
    job = await published_job(client, emp, admin_headers)
    _, seeker, cv = await seeker_with_cv(client, user_factory)
    app_ = (await client.post("/api/v1/applications", headers=seeker, json={"job_id": job["id"], "selected_cv_id": cv["id"]})).json()["data"]

    r = await client.get(f"/api/v1/employer/applications/{app_['id']}/cv", headers=emp)
    assert r.status_code == 200
    file_resp = await client.get(urlparse(r.json()["data"]["url"]).path)
    assert file_resp.content.startswith(b"%PDF")
    assert await db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "employer.cv_access"))

    assert (await client.get(f"/api/v1/employer/applications/{app_['id']}/cv", headers=other_emp)).status_code == 404
    assert (await client.get(f"/api/v1/employer/applications/{app_['id']}", headers=other_emp)).status_code == 404
    assert (await client.patch(f"/api/v1/employer/applications/{app_['id']}/status", headers=other_emp,
                               json={"status": "rejected"})).status_code == 404
    _, nosy_seeker = await user_factory()
    assert (await client.get(f"/api/v1/employer/applications/{app_['id']}/cv", headers=nosy_seeker)).status_code == 403
    # employers can never browse CVs through the candidate CV API
    assert (await client.get(f"/api/v1/cv/{cv['id']}/download", headers=emp)).status_code == 404


async def test_external_application_not_visible_to_employer(client, db, user_factory, admin_headers):
    _, emp, _ = await verified_employer(client, user_factory, admin_headers)
    _, seeker = await user_factory()
    await client.post("/api/v1/applications", headers=seeker, json={"job_title": "Python Developer", "company_name": "Acme Pvt Ltd"})
    assert (await client.get("/api/v1/employer/applications", headers=emp)).json()["data"]["items"] == []


# ---------------- Admin: users ----------------
async def test_admin_user_management(client, db, user_factory, admin_headers):
    target, target_headers = await user_factory(email="target@example.com")
    r = await client.get("/api/v1/admin/users?q=target", headers=admin_headers)
    assert [u["email"] for u in r.json()["data"]["items"]] == ["target@example.com"]
    assert (await client.get(f"/api/v1/admin/users/{target.id}", headers=admin_headers)).json()["data"]["applications"] == 0
    r = await client.patch(f"/api/v1/admin/users/{target.id}/status", headers=admin_headers, json={"is_active": False, "reason": "Spam"})
    assert r.json()["data"]["is_active"] is False and r.json()["data"]["auth_provider_updated"] is False
    assert (await client.get("/api/v1/profile/me", headers=target_headers)).status_code == 403
    await client.patch(f"/api/v1/admin/users/{target.id}/status", headers=admin_headers, json={"is_active": True, "reason": "Appeal"})
    assert (await client.get("/api/v1/profile/me", headers=target_headers)).status_code == 200
    r = await client.patch(f"/api/v1/admin/users/{target.id}/role", headers=admin_headers, json={"role": "employer", "reason": "Requested"})
    assert r.json()["data"]["role"] == "employer"
    assert (await client.get("/api/v1/admin/users?role=employer", headers=admin_headers)).json()["data"]["pagination"]["total"] == 1
    actions = [a.action for a in await db.scalars(select(AdminAuditLog))]
    assert {"user.suspend", "user.reactivate", "user.role_change"} <= set(actions)
    assert (await client.patch(f"/api/v1/admin/users/{target.id}/status", headers=admin_headers, json={"is_active": True})).status_code == 422


async def test_supabase_ban_when_service_key_configured(client, supabase, user_factory, admin_headers):
    import httpx
    from pydantic import SecretStr

    get_settings().SUPABASE_SERVICE_ROLE_KEY = SecretStr("service-role-test")
    target, _ = await user_factory()
    supabase.add("PUT", f"/admin/users/{target.id}", httpx.Response(200, json={}))
    r = await client.patch(f"/api/v1/admin/users/{target.id}/status", headers=admin_headers, json={"is_active": False, "reason": "Fraud"})
    assert r.json()["data"]["auth_provider_updated"] is True
    sent = supabase.requests[-1]
    assert sent.headers["Authorization"] == "Bearer service-role-test" and b"876000h" in sent.content


async def test_last_admin_protection(client, db, user_factory):
    admin_user, admin_h = await user_factory(UserRole.ADMIN)
    r = await client.patch(f"/api/v1/admin/users/{admin_user.id}/role", headers=admin_h, json={"role": "job_seeker", "reason": "x" * 5})
    assert r.status_code == 400 and r.json()["error"]["code"] == "self_action_forbidden"
    second, second_h = await user_factory(UserRole.ADMIN)
    assert (await client.patch(f"/api/v1/admin/users/{admin_user.id}/role", headers=second_h,
                               json={"role": "job_seeker", "reason": "Rotation"})).status_code == 200
    r = await client.patch(f"/api/v1/admin/users/{second.id}/status", headers=second_h, json={"is_active": False, "reason": "Leaving"})
    assert r.status_code == 400  # self
    # A demoted ex-admin cannot act as admin anymore
    assert (await client.get("/api/v1/admin/users", headers=admin_h)).status_code == 403


async def test_last_active_admin_cannot_be_demoted_by_other(db, user_factory):
    from app.services import admin_service

    only_admin, _ = await user_factory(UserRole.ADMIN)
    other_admin, _ = await user_factory(UserRole.ADMIN, is_active=False)  # inactive admins don't count
    from app.core.exceptions import ConflictError

    with pytest.raises(ConflictError):
        await admin_service.set_user_role(db, other_admin, only_admin.id, UserRole.JOB_SEEKER, "test")


async def test_privilege_escalation_blocked(client, user_factory):
    user, headers = await user_factory()
    assert (await client.patch(f"/api/v1/admin/users/{user.id}/role", headers=headers, json={"role": "admin", "reason": "please"})).status_code == 403
    _, emp = await user_factory(UserRole.EMPLOYER)
    for path in ["/api/v1/admin/dashboard", "/api/v1/admin/users", "/api/v1/admin/jobs", "/api/v1/admin/reports",
                 "/api/v1/admin/employers", "/api/v1/admin/audit-logs"]:
        assert (await client.get(path, headers=headers)).status_code == 403
        assert (await client.get(path, headers=emp)).status_code == 403
        assert (await client.get(path)).status_code == 401
    await client.patch("/api/v1/profile/me", headers=headers, json={"role": "admin"})
    await client.put("/api/v1/recommendations/preferences", headers=headers, json={"role": "admin"})
    me = (await client.get("/api/v1/auth/me", headers=headers)).json()["data"]
    assert me["role"] == "job_seeker"


# ---------------- Admin: employers & jobs ----------------
async def test_employer_suspension_hides_jobs(client, user_factory, admin_headers):
    _, emp, employer = await verified_employer(client, user_factory, admin_headers)
    job = await published_job(client, emp, admin_headers)
    _, seeker = await user_factory()
    r = await client.patch(f"/api/v1/admin/employers/{employer['id']}/status", headers=admin_headers,
                           json={"account_status": "suspended", "reason": "Fraud reports"})
    assert r.json()["data"]["account_status"] == "suspended"
    assert (await client.get(f"/api/v1/jobs/{job['id']}", headers=seeker)).status_code == 404
    assert (await client.get("/api/v1/jobs/sharkhire", headers=seeker)).json()["data"]["items"] == []
    r = await client.post("/api/v1/employer/jobs", headers=emp, json=JOB)
    assert r.status_code == 403 and r.json()["error"]["code"] == "employer_suspended"
    assert (await client.get("/api/v1/employer/jobs", headers=emp)).status_code == 200  # read-only access remains
    assert (await client.get(f"/api/v1/employers/{employer['id']}", headers=seeker)).status_code == 404
    listed = (await client.get("/api/v1/admin/employers?account_status=suspended", headers=admin_headers)).json()["data"]
    assert listed["pagination"]["total"] == 1
    detail = (await client.get(f"/api/v1/admin/employers/{employer['id']}", headers=admin_headers)).json()["data"]
    assert len(detail["verification_requests"]) == 1


async def test_admin_job_listing_and_status(client, user_factory, admin_headers):
    _, emp, _ = await verified_employer(client, user_factory, admin_headers)
    job = await published_job(client, emp, admin_headers)
    pending = (await client.get("/api/v1/admin/jobs?status=pending_review", headers=admin_headers)).json()["data"]
    assert pending["pagination"]["total"] == 0
    assert (await client.get(f"/api/v1/admin/jobs/{job['id']}", headers=admin_headers)).status_code == 200
    r = await client.patch(f"/api/v1/admin/jobs/{job['id']}/status", headers=admin_headers, json={"status": "draft", "reason": "x" * 5})
    assert r.status_code == 422
    r = await client.patch(f"/api/v1/admin/jobs/{job['id']}/status", headers=admin_headers, json={"status": "suspended", "reason": "Scam"})
    assert r.json()["data"]["posting_status"] == "suspended"
    r = await client.patch(f"/api/v1/admin/jobs/{job['id']}/status", headers=admin_headers, json={"status": "published", "reason": "Cleared"})
    assert r.json()["data"]["posting_status"] == "published"


# ---------------- Reports ----------------
async def test_reports_flow(client, db, jsearch, user_factory, admin_headers):
    _, emp, employer = await verified_employer(client, user_factory, admin_headers)
    job = await published_job(client, emp, admin_headers)
    reporter, r_headers = await user_factory()
    _, other = await user_factory()
    r = await client.post("/api/v1/reports", headers=r_headers, json={"target_type": "job", "target_id": job["id"],
                                                                     "reason": "scam", "description": "Asks for fees"})
    assert r.status_code == 201
    report = r.json()["data"]
    assert "reporter_id" not in report
    dup = await client.post("/api/v1/reports", headers=r_headers, json={"target_type": "job", "target_id": job["id"], "reason": "spam"})
    assert dup.status_code == 409
    assert (await client.post("/api/v1/reports", headers=r_headers, json={"target_type": "job", "target_id": str(uuid.uuid4()), "reason": "spam"})).status_code == 404
    assert (await client.post("/api/v1/reports", headers=emp, json={"target_type": "employer", "target_id": employer["id"], "reason": "spam"})).status_code == 400
    assert (await client.post("/api/v1/reports", headers=r_headers, json={"target_type": "employer", "target_id": employer["id"], "reason": "fraud"})).status_code == 201

    assert (await client.get("/api/v1/reports/mine", headers=r_headers)).json()["data"]["pagination"]["total"] == 2
    assert (await client.get("/api/v1/reports/mine", headers=other)).json()["data"]["items"] == []
    assert (await client.get(f"/api/v1/admin/reports/{report['id']}", headers=r_headers)).status_code == 403

    admin_view = (await client.get(f"/api/v1/admin/reports/{report['id']}", headers=admin_headers)).json()["data"]
    assert admin_view["reporter_id"] == str(reporter.id)
    url = f"/api/v1/admin/reports/{report['id']}/status"
    assert (await client.patch(url, headers=admin_headers, json={"status": "resolved"})).status_code == 422
    assert (await client.patch(url, headers=admin_headers, json={"status": "under_review"})).status_code == 200
    r = await client.patch(url, headers=admin_headers, json={"status": "resolved", "resolution_note": "Job suspended"})
    assert r.json()["data"]["status"] == "resolved"
    pending = (await client.get("/api/v1/admin/reports?status=pending", headers=admin_headers)).json()["data"]
    assert pending["pagination"]["total"] == 1
    # a resolved report allows a new one
    assert (await client.post("/api/v1/reports", headers=r_headers, json={"target_type": "job", "target_id": job["id"], "reason": "spam"})).status_code == 201


async def test_report_rate_limit(client, db, user_factory, admin_headers):
    get_settings().RATE_LIMIT_REPORT_PER_HOUR = 1
    _, emp, employer = await verified_employer(client, user_factory, admin_headers)
    _, h = await user_factory()
    await client.post("/api/v1/reports", headers=h, json={"target_type": "employer", "target_id": employer["id"], "reason": "spam"})
    r = await client.post("/api/v1/reports", headers=h, json={"target_type": "employer", "target_id": employer["id"], "reason": "fraud"})
    assert r.status_code == 429


# ---------------- Dashboard & audit ----------------
async def test_admin_dashboard_and_audit(client, db, user_factory, admin_headers):
    _, emp, _ = await verified_employer(client, user_factory, admin_headers)
    await published_job(client, emp, admin_headers)
    _, seeker, cv = await seeker_with_cv(client, user_factory)
    await client.post("/api/v1/applications", headers=seeker, json={"job_title": "External job"})
    dash = (await client.get("/api/v1/admin/dashboard", headers=admin_headers)).json()["data"]
    assert dash["total_users"] == 3 and dash["employers"] == 1 and dash["admins"] == 1 and dash["job_seekers"] == 1
    assert dash["verified_employers"] == 1 and dash["published_jobs"] == 1 and dash["pending_moderation"] == 0
    assert dash["total_applications"] == 1 and dash["internal_applications"] == 0 and dash["pending_reports"] == 0
    logs = (await client.get("/api/v1/admin/audit-logs", headers=admin_headers)).json()["data"]
    actions = {i["action"] for i in logs["items"]}
    assert {"employer.register", "employer.verification.approve", "job.moderation.approve"} <= actions
    filtered = (await client.get("/api/v1/admin/audit-logs?action=job.moderation.approve", headers=admin_headers)).json()["data"]
    assert filtered["pagination"]["total"] == 1
    assert all("token" not in str(i).lower() for i in logs["items"])


async def test_promote_admin_script(db, user_factory):
    from app.scripts.promote_admin import promote

    user, _ = await user_factory(email="boss@example.com")
    assert await promote("BOSS@example.com", "bootstrap") == 0
    await db.refresh(user)
    assert user.role == UserRole.ADMIN
    assert await promote("missing@example.com", "x") == 1
    entry = await db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "user.role_change.bootstrap"))
    assert entry.actor_user_id is None


async def test_unused(db):
    assert User and db
