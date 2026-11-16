"""security: stop auto-granting the Supabase API roles on FUTURE tables

Supabase configures default privileges so every new table/sequence created by
``postgres`` in ``public`` is automatically granted ALL to ``anon`` and
``authenticated``. SharkHire never lets clients query the database directly
(the Data API is intentionally disabled; FastAPI is the only client), so new
tables must not inherit those grants. Until now future tables were protected
only by the project's automatic-RLS event trigger; this removes the grants
themselves (defence in depth).

Scope: default privileges of role ``postgres`` in schema ``public`` (tables and
sequences). Defaults owned by ``supabase_admin`` are platform-managed and cannot
be changed by ``postgres``. Existing tables are unaffected (0007 already revoked
them). PostgreSQL only; skipped when the roles do not exist.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-16
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DO = """
DO $$
DECLARE r text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'postgres') THEN
    RETURN;
  END IF;
  FOREACH r IN ARRAY ARRAY['anon', 'authenticated'] LOOP
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public %s ALL ON TABLES %s %I', '{verb}', '{prep}', r);
      EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public %s ALL ON SEQUENCES %s %I', '{verb}', '{prep}', r);
    END IF;
  END LOOP;
END $$;
"""


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    if _is_postgres():
        op.execute(_DO.replace("{verb}", "REVOKE").replace("{prep}", "FROM"))


def downgrade() -> None:
    # Restores Supabase's platform defaults for role postgres (re-enables auto-grants on future tables).
    if _is_postgres():
        op.execute(_DO.replace("{verb}", "GRANT").replace("{prep}", "TO"))
