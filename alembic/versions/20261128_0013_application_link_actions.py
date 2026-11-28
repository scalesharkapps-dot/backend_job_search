"""jobs: track application-link actions (Free plan daily limit)

Adds ``application_link_actions``: one row per request to open an external job
application link through SharkHire (user, job, provider -- jsearch or techmap --,
time, optional idempotency key, whether it counted toward the Free plan limit).
One shared limit across providers. Index (user_id, created_at) serves
the rolling 24-hour count; UNIQUE (user_id, idempotency_key) makes retries no-ops.

RLS with no policies and no API-role privileges, like every SharkHire table (0007).
New table only: no existing data is touched.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-28
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Read by tests/test_foundation.py::test_every_table_is_covered_by_rls_lockdown.
TABLES = ("application_link_actions",)
API_ROLES = ("anon", "authenticated")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    op.create_table(
        "application_link_actions",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "provider",
            sa.Enum("jsearch", "sharkhire", "techmap", name="application_link_provider",
                    native_enum=False, create_constraint=True, length=32),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("counted", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], name=op.f("fk_application_link_actions_job_id_jobs"), ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_application_link_actions_user_id_users"), ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_application_link_actions")),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_application_link_actions_user_idempotency"),
    )
    with op.batch_alter_table("application_link_actions", schema=None) as batch_op:
        batch_op.create_index("ix_application_link_actions_user_created", ["user_id", "created_at"], unique=False)

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
    with op.batch_alter_table("application_link_actions", schema=None) as batch_op:
        batch_op.drop_index("ix_application_link_actions_user_created")
    op.drop_table("application_link_actions")
