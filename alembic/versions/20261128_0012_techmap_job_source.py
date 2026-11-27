"""jobs: allow the Techmap provider as a job / application source

Adds ``techmap`` to the CHECK constraints of ``jobs.source`` (``job_source``) and
``applications.source`` (``application_source``). Both columns are VARCHAR + CHECK
(non-native enums), so this only replaces the constraints; no data changes.

Downgrade removes Techmap listings (saved jobs / recommendations cascade; tracked
applications keep their copied details with ``job_id`` set to NULL and source
``manual``) so the old constraints can be restored.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_SOURCES_OLD = ("jsearch", "sharkhire")
JOB_SOURCES_NEW = ("jsearch", "sharkhire", "techmap")
APP_SOURCES_OLD = ("jsearch", "sharkhire", "manual")
APP_SOURCES_NEW = ("jsearch", "sharkhire", "manual", "techmap")


def _check(values: tuple[str, ...]) -> str:
    return "source IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def _replace(table: str, name: str, values: tuple[str, ...]) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.drop_constraint(op.f(name), type_="check")
        batch_op.create_check_constraint(op.f(name), _check(values))


def upgrade() -> None:
    _replace("jobs", "ck_jobs_job_source", JOB_SOURCES_NEW)
    _replace("applications", "ck_applications_application_source", APP_SOURCES_NEW)


def downgrade() -> None:
    op.execute("UPDATE applications SET source = 'manual' WHERE source = 'techmap'")
    op.execute("DELETE FROM jobs WHERE source = 'techmap'")
    _replace("applications", "ck_applications_application_source", APP_SOURCES_OLD)
    _replace("jobs", "ck_jobs_job_source", JOB_SOURCES_OLD)
