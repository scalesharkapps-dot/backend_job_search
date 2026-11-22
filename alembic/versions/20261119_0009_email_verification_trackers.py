"""auth: persistent tracking for signup verification e-mail resends

Adds ``email_verification_trackers`` (one row per normalised e-mail) so the
resend limit (1 initial + 2 resends per verification cycle) survives restarts,
multiple workers/replicas and client changes. Increments are done with atomic
conditional UPDATEs, so concurrent requests cannot exceed the limit.

Like every SharkHire table it gets RLS with no policies and no API-role
privileges (see 0007): only the FastAPI backend (table owner) may access it.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Read by tests/test_foundation.py::test_every_table_is_covered_by_rls_lockdown.
TABLES = ("email_verification_trackers",)
API_ROLES = ("anon", "authenticated")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "email_verification_trackers",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("cycle", sa.Integer(), server_default="1", nullable=False),
        sa.Column("cycle_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("initial_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resend_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_resend_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_email_verification_trackers")),
    )
    with op.batch_alter_table("email_verification_trackers", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_email_verification_trackers_email"), ["email"], unique=True)
        batch_op.create_index(batch_op.f("ix_email_verification_trackers_user_id"), ["user_id"], unique=False)

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
    with op.batch_alter_table("email_verification_trackers", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_email_verification_trackers_user_id"))
        batch_op.drop_index(batch_op.f("ix_email_verification_trackers_email"))
    op.drop_table("email_verification_trackers")
