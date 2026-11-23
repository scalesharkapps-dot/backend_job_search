"""accounts: tombstones for deleted accounts (no personal data)

Supabase access tokens remain valid until they expire even after the Auth user is
deleted. ``deleted_accounts`` (user id + time only) stops such a token from
re-creating the local user. RLS with no policies and no API-role privileges, like
every SharkHire table (see 0007).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-23
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Read by tests/test_foundation.py::test_every_table_is_covered_by_rls_lockdown.
TABLES = ("deleted_accounts",)
API_ROLES = ("anon", "authenticated")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "deleted_accounts",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_deleted_accounts")),
    )
    if _is_postgres():
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
    op.drop_table("deleted_accounts")
