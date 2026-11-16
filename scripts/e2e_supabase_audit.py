"""End-to-end API audit against a RUNNING server that uses real Supabase Auth + Storage.

Run (server started with the real .env; DATABASE_URL may point at any migrated PostgreSQL):
    python scripts/e2e_supabase_audit.py http://127.0.0.1:8000 postgresql://user:pw@host:5432/db
NOTE: it seeds rows into that database (jobs, audit users). Use a non-production database,
or the Supabase DB only before launch. Supabase users/files it creates are always removed.

Creates throwaway confirmed Supabase users (no emails), exercises every phase over
HTTP, prints PASS/FAIL per check (never tokens/keys), then deletes the users and
their stored files. Usage: python e2e_audit.py <base_url> <local_pg_url>
"""
import asyncio
import io
import secrets
import sys
import uuid

import httpx
import psycopg

sys.path.insert(0, ".")
from app.core.config import Settings
from tests.documents import make_docx, make_image, make_pdf, make_scanned_pdf

BASE, PG = sys.argv[1], sys.argv[2]
S = Settings(DATABASE_URL=PG)
SEC = S.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
ADMIN_H = {"apikey": SEC, "Authorization": f"Bearer {SEC}"}
results: list[tuple[str, str, str]] = []
created_users: list[str] = []


def check(phase: str, name: str, ok: bool, info: str = "") -> bool:
    results.append((phase, name, "PASS" if ok else "FAIL"))
    print(f"[{'PASS' if ok else 'FAIL'}] {phase:<8} {name}{' — ' + info if info and not ok else ''}")
    return ok


async def make_user(c: httpx.AsyncClient, label: str) -> tuple[str, str, str]:
    email = f"sharkhire-e2e-{label}-{secrets.token_hex(3)}@example.com"
    pw = "E2e-" + secrets.token_urlsafe(12)
    r = await c.post(f"{S.supabase_url}/auth/v1/admin/users", headers=ADMIN_H,
                     json={"email": email, "password": pw, "email_confirm": True, "user_metadata": {"full_name": f"E2E {label}"}})
    r.raise_for_status()
    created_users.append(r.json()["id"])
    return r.json()["id"], email, pw


async def login(api: httpx.AsyncClient, email: str, pw: str) -> dict:
    r = await api.post("/api/v1/auth/login", json={"email": email, "password": pw})
    return r.json()


def H(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


async def main() -> None:
    async with httpx.AsyncClient(timeout=30) as sb, httpx.AsyncClient(base_url=BASE, timeout=60) as api:
        # ---------- Phase 1 ----------
        r = await api.get("/health"); check("P1", "GET /health 200", r.status_code == 200)
        r = await api.get("/api/v1/health/database"); check("P1", "DB health connected", r.json().get("database") == "connected")
        r = await api.get("/docs"); check("P1", "Swagger /docs 200", r.status_code == 200 and "swagger" in r.text.lower())
        spec = (await api.get("/openapi.json")).json(); check("P1", "OpenAPI lists >= 95 operations", sum(len(v) for v in spec["paths"].values()) >= 95)
        r = await api.get("/api/v1/nope"); check("P1", "unknown route 404 envelope", r.status_code == 404 and r.json()["error"]["code"] == "not_found")
        check("P1", "X-Request-ID + security headers", bool(r.headers.get("x-request-id")) and r.headers.get("x-content-type-options") == "nosniff")

        # ---------- Phase 2 (real Supabase Auth) ----------
        seeker_id, s_email, s_pw = await make_user(sb, "seeker")
        other_id, o_email, o_pw = await make_user(sb, "other")
        emp_id, e_email, e_pw = await make_user(sb, "employer")
        adm_id, a_email, a_pw = await make_user(sb, "admin")
        r = await api.post("/api/v1/auth/login", json={"email": s_email, "password": "wrong-" + s_pw})
        check("P2", "login wrong password -> 401 invalid_credentials", r.status_code == 401 and r.json()["error"]["code"] == "invalid_credentials")
        r = await api.post("/api/v1/auth/login", json={"email": "not-an-email", "password": "x"}); check("P2", "login invalid email -> 422", r.status_code == 422)
        sess = await login(api, s_email, s_pw)
        check("P2", "real login returns session + user", sess.get("success") and sess["data"]["user"]["id"] == seeker_id and sess["data"]["user"]["role"] == "job_seeker")
        tok = sess["data"]["access_token"]
        r = await api.get("/api/v1/auth/me", headers=H(tok)); check("P2", "GET /auth/me with real ES256 token", r.status_code == 200 and r.json()["data"]["id"] == seeker_id)
        r = await api.get("/api/v1/auth/me"); check("P2", "no token -> 401", r.status_code == 401)
        r = await api.get("/api/v1/auth/me", headers=H(tok[:-4] + "AAAA")); check("P2", "tampered token -> 401", r.status_code == 401)
        r = await api.post("/api/v1/auth/refresh", json={"refresh_token": sess["data"]["refresh_token"]})
        check("P2", "refresh -> new token", r.status_code == 200 and r.json()["data"]["access_token"] != tok)
        tok = r.json()["data"]["access_token"]
        other = (await login(api, o_email, o_pw))["data"]["access_token"]
        r = await api.post("/api/v1/auth/forgot-password", json={"email": f"nobody-{secrets.token_hex(3)}@example.com"})
        check("P2", "forgot-password generic response (unknown email)", r.status_code in (200, 429), f"status {r.status_code}")

        # ---------- Phase 3 (real Supabase Storage) ----------
        r = await api.put("/api/v1/profile/me", headers=H(tok), json={"full_name": "E2E Seeker", "city": "Karachi", "country": "PK",
            "headline": "Python Developer", "skills": ["python", "fastapi", "postgres"], "current_job_title": "Python Developer",
            "years_of_experience": 4, "preferred_employment_types": ["FULLTIME"], "professional_summary": "Backend developer building APIs with FastAPI."})
        check("P3", "PUT profile", r.status_code == 200 and r.json()["data"]["skills"] == ["Python", "FastAPI", "PostgreSQL"])
        r = await api.patch("/api/v1/profile/me", headers=H(tok), json={"role": "admin"}); check("P3", "role change via profile -> 422", r.status_code == 422)
        r = await api.post("/api/v1/profile/me/photo", headers=H(tok), files={"file": ("me.png", make_image(), "image/png")})
        photo_url = r.json().get("data", {}).get("profile_photo_url") if r.status_code == 200 else None
        check("P3", "photo upload to Supabase Storage", r.status_code == 200 and "supabase.co/storage/v1/object/sign/" in (photo_url or ""), r.text[:120])
        if photo_url:
            check("P3", "photo signed URL works", (await sb.get(photo_url)).status_code == 200)
        r = await api.post("/api/v1/cv/upload", headers=H(tok), files={"file": ("cv.pdf", make_pdf(), "application/pdf")})
        cv = r.json().get("data") or {}
        check("P3", "PDF CV upload + extraction (Supabase Storage)", r.status_code == 201 and cv.get("processing_status") == "completed", r.text[:150])
        r = await api.post("/api/v1/cv/upload", headers=H(tok), files={"file": ("cv.docx", make_docx(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
        check("P3", "DOCX CV upload + extraction", r.status_code == 201 and r.json()["data"]["processing_status"] == "completed")
        r = await api.post("/api/v1/cv/upload", headers=H(tok), files={"file": ("scan.pdf", make_scanned_pdf(), "application/pdf")})
        check("P3", "scanned PDF -> ocr_required", r.status_code == 201 and r.json()["data"]["processing_status"] == "ocr_required")
        r = await api.post("/api/v1/cv/upload", headers=H(tok), files={"file": ("evil.pdf", b"MZ\x90 not pdf", "application/pdf")})
        check("P3", "spoofed file -> 415", r.status_code == 415)
        r = await api.get(f"/api/v1/cv/{cv.get('id')}/extracted-data", headers=H(tok)); check("P3", "extracted skills", "Python" in r.json()["data"]["skills"])
        r = await api.get(f"/api/v1/cv/{cv.get('id')}/download", headers=H(tok))
        dl = r.json()["data"]["url"] if r.status_code == 200 else ""
        check("P3", "CV signed download from Supabase", dl and (await sb.get(dl)).content.startswith(b"%PDF"))
        r = await api.get(f"/api/v1/cv/{cv.get('id')}/download", headers=H(other)); check("P3", "other user CV download -> 404", r.status_code == 404)
        r = await api.get("/api/v1/profile/me", headers=H(other)); check("P3", "profile isolation", r.json()["data"]["full_name"] != "E2E Seeker")

        # ---------- Phase 4 (JSearch not configured) ----------
        r = await api.get("/api/v1/jobs/search", headers=H(tok), params={"query": "Python Developer", "location": "Karachi"})
        check("P4", "search without RapidAPI key -> 503 job_search_not_configured", r.status_code == 503 and r.json()["error"]["code"] == "job_search_not_configured")
        r = await api.get("/api/v1/jobs/filters"); check("P4", "filters public 200", r.status_code == 200)
        r = await api.get("/api/v1/jobs/search", params={"query": "Python"}); check("P4", "search requires auth", r.status_code == 401)

        # Seed external jobs directly (the provider is not configured) so phases 5-6 run on real HTTP + DB.
        job_ids = []
        with psycopg.connect(PG) as conn:
            for title, desc in [("Python Developer", "Python, FastAPI and PostgreSQL required. Docker is a plus. 3+ years of experience."),
                                ("Data Analyst", "SQL, Excel and Power BI required."), ("Registered Nurse", "Patient care. 2 years experience.")]:
                jid = uuid.uuid4(); job_ids.append(str(jid))
                conn.execute("""INSERT INTO jobs (id, source, external_id, title, company_name, description, highlights, required_skills,
                    city, country, location, is_remote, employment_type, employment_types, apply_options, provider_metadata,
                    posted_at, last_seen_at, preferred_skills, benefits)
                    VALUES (%s,'jsearch',%s,%s,'E2E Co',%s,'{}','[]','Karachi','pk','Karachi, PK',false,'FULLTIME','["FULLTIME"]','[]','{}',now(),now(),'[]','[]')""",
                             (jid, f"e2e-{jid.hex}", title, desc))

        # ---------- Phase 5 ----------
        r = await api.get("/api/v1/recommendations/for-me", headers=H(tok)); data = r.json().get("data") or {}
        top = data.get("items", [{}])[0]
        check("P5", "recommendations ranked, explained", r.status_code == 200 and top.get("job", {}).get("job_title") == "Python Developer"
              and top["match"]["match_percentage"] >= 70 and top["match"]["reasons"], r.text[:150])
        check("P5", "unrelated job filtered (relevance gate)", all(i["job"]["job_title"] != "Registered Nurse" for i in data.get("items", [])))
        r = await api.get(f"/api/v1/recommendations/jobs/{job_ids[0]}/match", headers=H(tok)); check("P5", "single job match", r.status_code == 200)
        r = await api.get("/api/v1/recommendations/for-me", headers=H(other)); check("P5", "insufficient profile -> 422", r.status_code == 422)

        # ---------- Phase 6 ----------
        r = await api.post(f"/api/v1/saved-jobs/{job_ids[0]}", headers=H(tok)); check("P6", "save job", r.status_code == 201)
        r = await api.post(f"/api/v1/saved-jobs/{job_ids[0]}", headers=H(tok)); check("P6", "duplicate save -> 409", r.status_code == 409)
        r = await api.post("/api/v1/applications", headers=H(tok), json={"job_id": job_ids[0], "discovered_via": "recommendation"})
        app_id = r.json()["data"]["id"] if r.status_code == 201 else None
        check("P6", "create application", r.status_code == 201 and r.json()["data"]["match_percentage"] is not None)
        for st in ("shortlisted", "interview_scheduled"):
            await api.patch(f"/api/v1/applications/{app_id}/status", headers=H(tok), json={"status": st})
        r = await api.get(f"/api/v1/applications/{app_id}/history", headers=H(tok))
        check("P6", "status history ordered", [h["new_status"] for h in r.json()["data"]] == ["applied", "shortlisted", "interview_scheduled"])
        r = await api.post(f"/api/v1/applications/{app_id}/interviews", headers=H(tok), json={"interview_type": "video", "scheduled_at": "2030-01-10T15:00:00", "timezone": "Asia/Karachi"})
        check("P6", "interview with timezone", r.status_code == 201 and r.json()["data"]["scheduled_at"].startswith("2030-01-10T10:00"))
        r = await api.get(f"/api/v1/applications/{app_id}", headers=H(other)); check("P6", "other user application -> 404", r.status_code == 404)
        r = await api.get("/api/v1/applications/statistics", headers=H(tok)); check("P6", "statistics", r.json()["data"]["total_applications"] == 1)

        # ---------- Phase 7 ----------
        etok = (await login(api, e_email, e_pw))["data"]["access_token"]
        atok = (await login(api, a_email, a_pw))["data"]["access_token"]  # first sign-in creates the users row
        with psycopg.connect(PG) as conn:  # server-side bootstrap, equivalent to app.scripts.promote_admin
            n = conn.execute("UPDATE users SET role='admin' WHERE id=%s", (adm_id,)).rowcount
        check("P7", "bootstrap admin (server-side)", n == 1)
        r = await api.get("/api/v1/admin/dashboard", headers=H(tok)); check("P7", "job seeker -> admin API 403", r.status_code == 403)
        r = await api.post("/api/v1/employers/register", headers=H(etok), json={"company_name": "E2E Hiring Ltd", "contact_email": "hr@example.com", "country": "PK"})
        emp = r.json().get("data") or {}; check("P7", "employer registration", r.status_code == 201)
        r = await api.post("/api/v1/employers/me/logo", headers=H(etok), files={"file": ("l.png", make_image(), "image/png")}); check("P7", "logo to Supabase Storage", r.status_code == 200)
        job_body = {"title": "Backend Python Developer", "description": "Build FastAPI services for SharkHire's hiring platform.",
                    "required_skills": ["Python", "FastAPI"], "employment_type": "FULLTIME", "city": "Karachi", "country": "PK"}
        r = await api.post("/api/v1/employer/jobs", headers=H(etok), json={**job_body, "submit_for_review": True})
        check("P7", "unverified employer cannot publish -> 403", r.status_code == 403)
        await api.post("/api/v1/employers/me/verification", headers=H(etok), json={"business_registration_number": "E2E-123"})
        r = await api.patch(f"/api/v1/admin/employers/{emp.get('id')}/verification", headers=H(atok), json={"decision": "approve"}); check("P7", "admin approves verification", r.status_code == 200)
        r = await api.post("/api/v1/employer/jobs", headers=H(etok), json={**job_body, "submit_for_review": True}); ejob = r.json()["data"]
        r = await api.patch(f"/api/v1/admin/jobs/{ejob['id']}/moderation", headers=H(atok), json={"decision": "approve"})
        check("P7", "moderation publishes job", r.json()["data"]["posting_status"] == "published")
        r = await api.post("/api/v1/applications", headers=H(tok), json={"job_id": ejob["id"], "selected_cv_id": cv.get("id"), "cover_letter": "Hello"})
        iapp = r.json().get("data") or {}; check("P7", "internal application submitted", r.status_code == 201 and iapp.get("employer_status") == "submitted")
        r = await api.get(f"/api/v1/employer/applications/{iapp.get('id')}/cv", headers=H(etok))
        check("P7", "employer gets applicant CV via signed URL", r.status_code == 200 and (await sb.get(r.json()["data"]["url"])).content.startswith(b"%PDF"))
        r = await api.get(f"/api/v1/employer/applications/{iapp.get('id')}/cv", headers=H(other)); check("P7", "non-employer CV access -> 403", r.status_code == 403)
        r = await api.patch(f"/api/v1/admin/users/{adm_id}/role", headers=H(atok), json={"role": "job_seeker", "reason": "self"}); check("P7", "admin self-demotion blocked", r.status_code == 400)
        r = await api.patch(f"/api/v1/admin/users/{other_id}/status", headers=H(atok), json={"is_active": False, "reason": "E2E suspension"})
        check("P7", "suspend user (+ Supabase ban)", r.status_code == 200 and r.json()["data"]["auth_provider_updated"] is True, r.text[:150])
        r = await api.get("/api/v1/profile/me", headers=H(other)); check("P7", "suspended user blocked 403", r.status_code == 403)
        r = await api.post("/api/v1/auth/login", json={"email": o_email, "password": o_pw}); check("P7", "banned user login -> 401 account_suspended", r.status_code == 401 and r.json()["error"]["code"] == "account_suspended", r.text[:120])
        r = await api.get("/api/v1/admin/audit-logs", headers=H(atok)); check("P7", "audit log entries", r.json()["data"]["pagination"]["total"] >= 5)

        # ---------- Phase 8 ----------
        r = await api.post("/api/v1/auth/logout", headers=H(tok)); check("P8", "logout revokes Supabase session", r.status_code == 200 and r.json()["data"]["revoked"] is True)
        big = io.BytesIO(b"%PDF-1.4\n" + b"0" * (6 * 1024 * 1024))
        r = await api.post("/api/v1/cv/upload", headers=H(etok), files={"file": ("big.pdf", big.getvalue(), "application/pdf")}); check("P8", "oversized upload -> 413", r.status_code == 413)

    # Cleanup (users, their DB rows and files only) runs in __main__ via scripts/e2e_cleanup.py.
    fails = [r for r in results if r[2] == "FAIL"]
    print(f"\nTOTAL {len(results)} checks: {len(results) - len(fails)} PASS, {len(fails)} FAIL")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    import pathlib
    import subprocess

    try:
        asyncio.run(main())
    finally:  # always remove audit users + their files, even after a crash
        subprocess.run([sys.executable, str(pathlib.Path(__file__).with_name("e2e_cleanup.py")), PG], check=False)
