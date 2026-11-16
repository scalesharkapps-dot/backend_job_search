"""security: enable RLS and revoke Data API access on all SharkHire tables

Supabase exposes the ``public`` schema through its Data API (PostgREST) to the
``anon`` and ``authenticated`` roles, using keys that ship inside client apps.
SharkHire clients never use the Data API — every request goes through the
FastAPI backend, which connects as the table owner (bypasses RLS) and enforces
authorization itself. This migration therefore:

* enables Row Level Security with NO policies (deny-all for API roles), and
* revokes all privileges from ``anon`` / ``authenticated`` (defence in depth).

PostgreSQL only; a no-op on SQLite. Role-specific statements are skipped when
the roles do not exist (plain PostgreSQL outside Supabase).

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "users", "user_profiles", "user_cvs", "cv_extracted_data", "jobs", "job_search_cache",
    "user_recommendation_preferences", "job_recommendations", "saved_jobs", "applications",
    "application_status_history", "interviews", "employer_profiles", "employer_verification_requests",
    "reports", "admin_audit_logs", "alembic_version",
)
API_ROLES = ("anon", "authenticated")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if not _is_postgres():
        return
    for table in TABLES:
        op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')
    roles = ", ".join(f"'{r}'" for r in API_ROLES)
    tables = ", ".join(f"'{t}'" for t in TABLES)
    # Built only from the hard-coded constants above (no external input).
    op.execute(f"""
        DO $$
        DECLARE r text; t text;
        BEGIN
          FOREACH r IN ARRAY ARRAY[{roles}] LOOP
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
              FOREACH t IN ARRAY ARRAY[{tables}] LOOP
                EXECUTE format('REVOKE ALL ON TABLE public.%I FROM %I', t, r);
              END LOOP;
            END IF;
          END LOOP;
        END $$;
    """)  # noqa: S608


def downgrade() -> None:
    if not _is_postgres():
        return
    # Privileges are intentionally NOT re-granted: restoring Data API access to
    # these tables must be a deliberate decision, not a side effect of a downgrade.
    for table in TABLES:
        op.execute(f'ALTER TABLE public."{table}" DISABLE ROW LEVEL SECURITY')
