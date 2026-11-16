"""Live JSearch check through the RUNNING SharkHire API (real RapidAPI, real Supabase).

    python scripts/jsearch_live_check.py http://127.0.0.1:8000

Uses a throwaway confirmed Supabase user (deleted at the end, with its DB rows).
Costs about 3 RapidAPI requests (two searches + one cursor page); repeats are
served from the cache. Never prints keys or tokens. Jobs returned by JSearch are
kept: they are real listings and the cache saves quota.
"""

from __future__ import annotations

import secrets
import sys

import httpx
from sqlalchemy import create_engine, text

sys.path.insert(0, ".")
from app.core.config import load_settings
from app.core.database import normalize_database_url

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
S = load_settings()
SEC = S.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
ADMIN_H = {"apikey": SEC, "Authorization": f"Bearer {SEC}"}
KEY = S.JSEARCH_API_KEY.get_secret_value() if S.JSEARCH_API_KEY else ""
results: list[bool] = []


def check(name: str, ok: bool, info: str = "") -> None:
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {info}" if info else ""))


def main() -> int:
    email = f"sharkhire-audit-jsearch-{secrets.token_hex(3)}@example.com"
    pw = "Js-" + secrets.token_urlsafe(12)
    with httpx.Client(timeout=60) as sb, httpx.Client(base_url=BASE, timeout=90) as api:
        uid = sb.post(f"{S.supabase_url}/auth/v1/admin/users", headers=ADMIN_H,
                      json={"email": email, "password": pw, "email_confirm": True}).json()["id"]
        try:
            tok = api.post("/api/v1/auth/login", json={"email": email, "password": pw}).json()["data"]["access_token"]
            H = {"Authorization": f"Bearer {tok}"}

            r = api.get("/api/v1/jobs/search", headers=H, params={"query": "Python Developer", "location": "Pakistan"})
            d = r.json().get("data") or {}
            items, pg = d.get("items", []), d.get("pagination", {})
            check("search Python Developer / Pakistan", r.status_code == 200 and len(items) > 0,
                  f"{len(items)} jobs, country={pg.get('country')}, cached={pg.get('cached')}")
            check("API key never in response", bool(KEY) and KEY not in r.text)
            check("pagination: cursor returned", bool(pg.get("next_cursor")) and pg.get("has_more") is True and pg.get("total_results") is None)
            fields = {"id", "job_title", "employer_name", "job_location", "job_is_remote", "job_apply_link", "job_employment_type", "source"}
            check("normalized item fields", bool(items) and fields <= set(items[0]), f"missing={sorted(fields - set(items[0]))}" if items else "")
            check("apply links are http(s) only", all((i["job_apply_link"] or "https://").startswith(("http://", "https://")) for i in items))
            for i in items[:5]:
                print(f"    · {i['job_title'][:44]:44} | {str(i['employer_name'])[:22]:22} | {str(i['job_location'])[:24]:24} | {i['job_employment_type']}")

            r2 = api.get("/api/v1/jobs/search", headers=H, params={"query": "Python Developer", "location": "Pakistan"})
            check("repeat search served from cache", r2.json()["data"]["pagination"]["cached"] is True
                  and [i["id"] for i in r2.json()["data"]["items"]] == [i["id"] for i in items])

            r3 = api.get("/api/v1/jobs/search", headers=H, params={"query": "Python Developer", "location": "Pakistan", "cursor": pg.get("next_cursor")})
            p2 = r3.json().get("data") or {}
            overlap = {i["external_job_id"] for i in p2.get("items", [])} & {i["external_job_id"] for i in items}
            check("cursor page 2 returns new jobs", r3.status_code == 200 and len(p2.get("items", [])) > 0 and not overlap,
                  f"{len(p2.get('items', []))} jobs, overlap={len(overlap)}")

            r4 = api.get("/api/v1/jobs/search", headers=H, params={"query": "AI/ML Engineer", "remote_jobs_only": "true"})
            ritems = (r4.json().get("data") or {}).get("items", [])
            check("remote AI/ML Engineer search", r4.status_code == 200 and ritems and all(i["job_is_remote"] for i in ritems),
                  f"{len(ritems)} jobs, all remote={all(i['job_is_remote'] for i in ritems) if ritems else None}")
            for i in ritems[:5]:
                print(f"    · {i['job_title'][:44]:44} | {str(i['employer_name'])[:22]:22} | {str(i['job_location'])[:24]:24} | remote={i['job_is_remote']}")

            jid = items[0]["id"]
            r5 = api.get(f"/api/v1/jobs/{jid}", headers=H)
            check("job details (stored, fresh: no provider call)", r5.status_code == 200 and r5.json()["data"]["details_refreshed"] is False)
            r6 = api.get("/api/v1/jobs/suggestions", headers=H, params={"q": "pyth"})
            check("suggestions from stored real titles", r6.status_code == 200 and len(r6.json()["data"]) > 0, str(r6.json()["data"][:2]))
            check("filters endpoint", api.get("/api/v1/jobs/filters").status_code == 200)
            check("search requires auth", api.get("/api/v1/jobs/search", params={"query": "Python"}).status_code == 401)
            bad = [api.get("/api/v1/jobs/search", headers=H, params=p).status_code for p in
                   ({"query": "a"}, {"query": "Python", "country": "PAK"}, {"query": "Python", "num_pages": "50"}, {"query": "<script>"})]
            check("invalid params rejected without provider calls", bad == [422, 422, 422, 422], str(bad))

            api.put("/api/v1/profile/me", headers=H, json={"skills": ["Python", "Django", "FastAPI", "SQL"], "current_job_title": "Python Developer",
                                                          "years_of_experience": 3, "city": "Islamabad", "country": "PK"})
            r7 = api.get("/api/v1/recommendations/for-me", headers=H)
            recs = (r7.json().get("data") or {}).get("items", [])
            check("recommendations over real JSearch jobs", r7.status_code == 200 and len(recs) > 0,
                  f"{len(recs)} recommended; top={recs[0]['match']['match_percentage'] if recs else None}%")
            r8 = api.post(f"/api/v1/saved-jobs/{jid}", headers=H)
            check("save a real JSearch job", r8.status_code == 201)
            r9 = api.post("/api/v1/applications", headers=H, json={"job_id": jid, "discovered_via": "search"})
            check("track application for real JSearch job", r9.status_code == 201 and r9.json()["data"]["source"] == "jsearch")
        finally:
            sb.delete(f"{S.supabase_url}/auth/v1/admin/users/{uid}", headers=ADMIN_H)
            engine = create_engine(normalize_database_url(S.database_url, async_driver=False))
            with engine.begin() as conn:  # removes the user's rows (profile, saved job, application, recs cascade)
                conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": uid})
            engine.dispose()
            print("cleanup: throwaway user and its rows removed")
    print(f"\nTOTAL {len(results)} checks: {sum(results)} PASS, {len(results) - sum(results)} FAIL")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
