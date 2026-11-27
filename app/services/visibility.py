"""Which jobs job seekers may see.

* External (JSearch) jobs are always visible (availability flags are shown separately).
* SharkHire jobs are visible only when published, not past their application
  deadline, and owned by an employer whose account is active.
Draft, pending, rejected, paused, closed, expired, archived and suspended
listings are never shown to job seekers.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import NotFoundError
from app.models.base import utcnow
from app.models.employer import EmployerAccountStatus, EmployerProfile
from app.models.job import EXTERNAL_SOURCES, Job, JobSource, PostingStatus


def _today() -> date:
    return utcnow().date()


def internal_visible_filter() -> Any:
    active_employers = select(EmployerProfile.id).where(EmployerProfile.account_status == EmployerAccountStatus.ACTIVE)
    return and_(
        Job.source == JobSource.SHARKHIRE,
        Job.posting_status == PostingStatus.PUBLISHED,
        or_(Job.application_deadline.is_(None), Job.application_deadline >= _today()),
        Job.employer_id.in_(active_employers),
    )


def public_job_filter() -> Any:
    return or_(Job.source.in_(EXTERNAL_SOURCES), internal_visible_filter())


async def is_visible(db: AsyncSession, job: Job) -> bool:
    if job.source in EXTERNAL_SOURCES:
        return True
    if job.posting_status != PostingStatus.PUBLISHED or job.employer_id is None:
        return False
    if job.application_deadline is not None and job.application_deadline < _today():
        return False
    status = await db.scalar(select(EmployerProfile.account_status).where(EmployerProfile.id == job.employer_id))
    return status == EmployerAccountStatus.ACTIVE


async def get_visible_job(db: AsyncSession, job_id: uuid.UUID) -> Job:
    job = await db.get(Job, job_id)
    if job is None or not await is_visible(db, job):
        raise NotFoundError("Job not found.", code="job_not_found")
    return job
