"""Remove ONLY artefacts created by the audit scripts — never anything else.

Audit identities are Supabase users whose email starts with ``sharkhire-e2e-`` or
``sharkhire-audit-`` and ends with ``@example.com``; seeded jobs have external ids
starting with ``e2e-``. With a database URL it also removes their database rows
(users cascade to profiles, CVs, applications, ...; their employer profiles, jobs,
reports and audit-log entries), and their storage objects.

    python scripts/e2e_cleanup.py [database_url]
"""

import sys

import httpx
from sqlalchemy import create_engine, text

sys.path.insert(0, ".")
from app.core.config import load_settings
from app.core.database import normalize_database_url

S = load_settings()
SEC = S.SUPABASE_SERVICE_ROLE_KEY.get_secret_value()
H = {"apikey": SEC, "Authorization": f"Bearer {SEC}"}
PREFIXES = ("sharkhire-e2e-", "sharkhire-audit-")
DB_URL = sys.argv[1] if len(sys.argv) > 1 else None


def is_audit_email(email: str | None) -> bool:
    return bool(email) and email.startswith(PREFIXES) and email.endswith("@example.com")


with httpx.Client(timeout=30) as c:
    auth_ids: list[str] = []
    page = 1
    while True:
        users = c.get(f"{S.supabase_url}/auth/v1/admin/users", params={"page": page, "per_page": 200}, headers=H).json().get("users", [])
        auth_ids += [u["id"] for u in users if is_audit_email(u.get("email"))]
        if len(users) < 200:
            break
        page += 1

    user_ids: set[str] = set(auth_ids)
    employer_ids: set[str] = set()
    db_summary = "database not cleaned (no URL given)"
    if DB_URL:
        engine = create_engine(normalize_database_url(DB_URL, async_driver=False))
        with engine.begin() as conn:
            user_ids |= {str(r[0]) for r in conn.execute(text(
                "SELECT id FROM users WHERE (email LIKE 'sharkhire-e2e-%' OR email LIKE 'sharkhire-audit-%') AND email LIKE '%@example.com'"))}
            uid_list = sorted(user_ids)
            employer_ids = {str(r[0]) for r in conn.execute(text("SELECT id FROM employer_profiles WHERE owner_user_id::text = ANY(:u)"), {"u": uid_list})}
            job_ids = {str(r[0]) for r in conn.execute(text(
                "SELECT id FROM jobs WHERE external_id LIKE 'e2e-%' OR employer_id::text = ANY(:e) OR created_by_user_id::text = ANY(:u)"),
                {"e": sorted(employer_ids), "u": uid_list})}
            app_ids = {str(r[0]) for r in conn.execute(text("SELECT id FROM applications WHERE user_id::text = ANY(:u) OR job_id::text = ANY(:j)"),
                                                       {"u": uid_list, "j": sorted(job_ids)})}
            targets = sorted(user_ids | employer_ids | job_ids | app_ids)
            n_audit = conn.execute(text("DELETE FROM admin_audit_logs WHERE actor_user_id::text = ANY(:u) OR target_id = ANY(:t)"),
                                   {"u": uid_list, "t": targets}).rowcount
            n_reports = conn.execute(text("DELETE FROM reports WHERE reporter_id::text = ANY(:u) OR target_id::text = ANY(:t)"),
                                     {"u": uid_list, "t": targets}).rowcount
            n_apps = conn.execute(text("DELETE FROM applications WHERE id::text = ANY(:a)"), {"a": sorted(app_ids)}).rowcount
            n_jobs = conn.execute(text("DELETE FROM jobs WHERE id::text = ANY(:j)"), {"j": sorted(job_ids)}).rowcount
            n_users = conn.execute(text("DELETE FROM users WHERE id::text = ANY(:u)"), {"u": uid_list}).rowcount  # cascades
        engine.dispose()
        db_summary = f"db rows: users={n_users} jobs={n_jobs} applications={n_apps} reports={n_reports} audit_logs={n_audit}"

    removed = 0
    owners = {("cvs", "cvs"): user_ids, ("media", "avatars"): user_ids, ("media", "logos"): employer_ids}
    buckets = {"cvs": S.SUPABASE_STORAGE_CV_BUCKET, "media": S.SUPABASE_STORAGE_MEDIA_BUCKET}
    for (bucket_key, kind), allowed in owners.items():
        bucket = buckets[bucket_key]
        for owner in sorted(allowed):  # only folders that belong to audit identities
            files = c.post(f"{S.supabase_url}/storage/v1/object/list/{bucket}", headers=H, json={"prefix": f"{kind}/{owner}/", "limit": 1000}).json()
            paths = [f"{kind}/{owner}/{f['name']}" for f in files if isinstance(f, dict) and f.get("name")]
            if paths:
                c.request("DELETE", f"{S.supabase_url}/storage/v1/object/{bucket}", headers=H, json={"prefixes": paths})
                removed += len(paths)
    for uid in auth_ids:
        c.delete(f"{S.supabase_url}/auth/v1/admin/users/{uid}", headers=H)
    print(f"deleted audit auth users: {len(auth_ids)} | storage objects: {removed} | {db_summary}")
