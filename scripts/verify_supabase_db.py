"""Verify a migrated SharkHire database (Supabase or any PostgreSQL) — read-mostly and safe.

    python scripts/verify_supabase_db.py            # uses DATABASE_URL from .env
    python scripts/verify_supabase_db.py <db_url>    # explicit URL

Checks: connectivity, SSL, server version, alembic revision, every expected table/column/PK/FK/
unique/check/index vs. the SQLAlchemy models, RLS enabled on every table, no privileges for the
`anon`/`authenticated` API roles, and CRUD / FK / transaction behaviour using ONLY rows it creates
(all identified by the marker `sharkhire-dbverify-` and removed at the end). Never prints credentials.
"""

from __future__ import annotations

import sys
import uuid

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.script import ScriptDirectory

sys.path.insert(0, ".")
from app.core.config import load_settings
from app.core.database import normalize_database_url
from app.models import Base

MARK = "sharkhire-dbverify-"
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, info: str = "") -> bool:
    results.append((name, ok, info))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {info}" if info else ""))
    return ok


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else load_settings().database_url
    if not url:
        print("DATABASE_URL is not configured"); return 2
    engine = create_engine(normalize_database_url(url, async_driver=False), connect_args={"connect_timeout": 15})
    with engine.connect() as conn:
        version = conn.execute(text("SHOW server_version")).scalar()
        check("connect", True, f"PostgreSQL {version}")
        # Client-side TLS of THIS connection. (pg_stat_ssl would describe the pooler->Postgres hop
        # behind Supabase's Supavisor, not the connection from this machine.)
        ssl = bool(conn.connection.dbapi_connection.pgconn.ssl_in_use)
        is_local = (conn.engine.url.host or "") in {"localhost", "127.0.0.1", "::1"}
        check("TLS in use (client connection)", ssl or is_local, "local test server without TLS" if is_local and not ssl else "")
        who = conn.execute(text("SELECT current_user, current_database()")).one()
        check("connected role", True, f"user={who[0]} db={who[1]}")
        rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar() if inspect(conn).has_table("alembic_version") else None
        head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
        check(f"alembic revision is head ({head})", rev == head, f"current={rev}")

        insp = inspect(conn)
        tables = set(insp.get_table_names(schema="public"))
        expected = set(Base.metadata.tables)
        check(f"all {len(expected)} application tables present", expected <= tables, f"missing={sorted(expected - tables)}" if expected - tables else f"{len(expected)} tables")
        for name, table in sorted(Base.metadata.tables.items()):
            if name not in tables:
                continue
            cols = {c["name"] for c in insp.get_columns(name)}
            pk = set(insp.get_pk_constraint(name)["constrained_columns"])
            fks = {(tuple(f["constrained_columns"]), f["referred_table"]) for f in insp.get_foreign_keys(name)}
            exp_fks ={(tuple(e.parent.name for e in fkc.elements), next(iter(fkc.elements)).column.table.name) for fkc in table.foreign_key_constraints}
            idx = {i["name"] for i in insp.get_indexes(name)} | {u["name"] for u in insp.get_unique_constraints(name)}
            exp_idx = {i.name for i in table.indexes} | {c.name for c in table.constraints if c.__class__.__name__ == "UniqueConstraint"}
            problems = []
            if {c.name for c in table.columns} - cols:
                problems.append(f"columns {sorted({c.name for c in table.columns} - cols)}")
            if pk != {c.name for c in table.primary_key.columns}:
                problems.append("primary key")
            if exp_fks - fks:
                problems.append(f"fks {sorted(exp_fks - fks)}")
            if exp_idx - idx:
                problems.append(f"indexes/uniques {sorted(exp_idx - idx)}")
            check(f"structure {name}", not problems, "; ".join(problems) or f"{len(cols)} cols, {len(fks)} FKs, {len(idx)} idx/uq")

        rls_off = conn.execute(text("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND c.relkind = 'r' AND c.relname = ANY(:t) AND NOT c.relrowsecurity"""),
            {"t": sorted(expected | {"alembic_version"})}).scalars().all()
        check("RLS enabled on every SharkHire table", not rls_off, f"without RLS: {rls_off}" if rls_off else f"{len(expected) + 1} tables")
        policies = conn.execute(text("SELECT count(*) FROM pg_policies WHERE schemaname='public' AND tablename = ANY(:t)"),
                                {"t": sorted(expected)}).scalar()
        check("no permissive RLS policies on SharkHire tables (deny-all)", policies == 0, f"policies={policies}")
        leaky_defaults = conn.execute(text("""SELECT defaclobjtype::text FROM pg_default_acl
            WHERE defaclnamespace = 'public'::regnamespace AND pg_get_userbyid(defaclrole) = current_user
              AND defaclobjtype IN ('r', 'S') AND array_to_string(defaclacl, ',') ~ '(^|,)(anon|authenticated)='""")).scalars().all()
        check("future tables/sequences not auto-granted to API roles (0008)", not leaky_defaults,
              f"default grants remain for object types {leaky_defaults}" if leaky_defaults else "")
        for role in ("anon", "authenticated"):
            exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": role}).scalar()
            if not exists:
                check(f"role {role} privileges", True, "role does not exist (not Supabase)")
                continue
            granted = conn.execute(text("""SELECT table_name, privilege_type FROM information_schema.role_table_grants
                WHERE grantee=:r AND table_schema='public' AND table_name = ANY(:t)"""), {"r": role, "t": sorted(expected | {"alembic_version"})}).all()
            check(f"{role} has no privileges on SharkHire tables", not granted, f"{len(granted)} grants" if granted else "")
            denied = []
            for t in ("users", "user_cvs", "applications", "admin_audit_logs"):
                conn.execute(text("BEGIN"))
                try:
                    conn.execute(text(f"SET LOCAL ROLE {role}"))
                    conn.execute(text(f'SELECT 1 FROM public."{t}" LIMIT 1'))
                    denied.append(f"{t}:READABLE")
                except Exception:
                    denied.append(f"{t}:denied")
                finally:
                    conn.execute(text("ROLLBACK"))
            check(f"{role} SELECT denied on protected tables", all(d.endswith("denied") for d in denied), ", ".join(denied))

    # ---------- CRUD / FK / transactions (own rows only) ----------
    uid, jid = uuid.uuid4(), uuid.uuid4()
    email = f"{MARK}{uid.hex[:8]}@example.com"
    try:
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id,email,role,is_active,email_verified) VALUES (:i,:e,'job_seeker',true,false)"), {"i": uid, "e": email})
            conn.execute(text("""INSERT INTO user_profiles (id,user_id,full_name,education,experience,skills,certifications,
                preferred_job_titles,preferred_employment_types,preferred_locations) VALUES (:p,:u,'DB Verify','[]','[]','["Python"]','[]','[]','[]','[]')"""),
                {"p": uuid.uuid4(), "u": uid})
            conn.execute(text("""INSERT INTO jobs (id,source,external_id,title,highlights,required_skills,is_remote,employment_types,
                apply_options,provider_metadata,preferred_skills,benefits) VALUES (:j,'jsearch',:x,'DB Verify Job','{}','[]',false,'[]','[]','{}','[]','[]')"""),
                {"j": jid, "x": f"{MARK}{jid.hex}"})
            conn.execute(text("INSERT INTO saved_jobs (id,user_id,job_id,created_at) VALUES (:s,:u,:j,now())"), {"s": uuid.uuid4(), "u": uid, "j": jid})
        check("CREATE user/profile/job/saved_job", True)
        with engine.connect() as conn:
            row = conn.execute(text("SELECT p.skills FROM user_profiles p JOIN users u ON u.id=p.user_id WHERE u.email=:e"), {"e": email}).scalar()
            check("READ with join + JSONB", row == ["Python"], f"skills={row}")
        with engine.begin() as conn:
            conn.execute(text("UPDATE user_profiles SET full_name='DB Verify Updated', updated_at=now() WHERE user_id=:u"), {"u": uid})
            name = conn.execute(text("SELECT full_name FROM user_profiles WHERE user_id=:u"), {"u": uid}).scalar()
        check("UPDATE", name == "DB Verify Updated")
        try:
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO saved_jobs (id,user_id,job_id,created_at) VALUES (:s,:u,:j,now())"), {"s": uuid.uuid4(), "u": uid, "j": uuid.uuid4()})
            check("FK violation rejected", False, "orphan saved_job accepted")
        except IntegrityError:
            check("FK violation rejected", True)
        try:
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO saved_jobs (id,user_id,job_id,created_at) VALUES (:s,:u,:j,now())"), {"s": uuid.uuid4(), "u": uid, "j": jid})
            check("unique (user_id, job_id) enforced", False)
        except IntegrityError:
            check("unique (user_id, job_id) enforced", True)
        try:
            with engine.begin() as conn:
                conn.execute(text("UPDATE users SET role='superuser' WHERE id=:u"), {"u": uid})
            check("CHECK constraint on role enum", False)
        except IntegrityError:
            check("CHECK constraint on role enum", True)
        try:
            with engine.begin() as conn:
                conn.execute(text("UPDATE user_profiles SET full_name='SHOULD ROLL BACK' WHERE user_id=:u"), {"u": uid})
                raise RuntimeError("simulated failure")
        except RuntimeError:
            pass
        with engine.connect() as conn:
            name = conn.execute(text("SELECT full_name FROM user_profiles WHERE user_id=:u"), {"u": uid}).scalar()
        check("transaction rollback", name == "DB Verify Updated")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id=:u"), {"u": uid})
            left = conn.execute(text("SELECT (SELECT count(*) FROM user_profiles WHERE user_id=:u) + (SELECT count(*) FROM saved_jobs WHERE user_id=:u)"), {"u": uid}).scalar()
        check("DELETE cascades to profile + saved_jobs", left == 0)
    finally:
        with engine.begin() as conn:  # remove only this script's rows
            conn.execute(text("DELETE FROM users WHERE email LIKE :m"), {"m": f"{MARK}%"})
            conn.execute(text("DELETE FROM jobs WHERE external_id LIKE :m"), {"m": f"{MARK}%"})
    engine.dispose()
    failed = [r for r in results if not r[1]]
    print(f"\nTOTAL {len(results)} checks: {len(results) - len(failed)} PASS, {len(failed)} FAIL")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
