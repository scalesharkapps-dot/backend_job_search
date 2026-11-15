"""Normalised job response schemas (Flutter-facing contract)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models.job import Job


class ApplyOption(BaseModel):
    publisher: str | None = None
    apply_link: str
    is_direct: bool | None = None


class JobSummaryOut(BaseModel):
    id: uuid.UUID
    external_job_id: str | None
    source: str
    job_title: str
    employer_name: str | None
    employer_logo: str | None
    job_city: str | None
    job_state: str | None
    job_country: str | None
    job_location: str | None
    job_is_remote: bool
    job_employment_type: str | None
    job_employment_types: list[str]
    job_min_salary: float | None
    job_max_salary: float | None
    job_salary_period: str | None
    job_currency: str | None
    job_posted_at: datetime | None
    job_posted_at_timestamp: int | None
    job_apply_link: str | None
    job_publisher: str | None
    job_description_snippet: str | None
    job_required_skills: list[str]
    is_external: bool
    listing_unavailable: bool
    last_seen_at: datetime | None


class JobOut(JobSummaryOut):
    employer_website: str | None
    job_description: str | None
    job_highlights: dict[str, list[str]]
    apply_options: list[ApplyOption]
    job_apply_is_direct: bool | None
    job_salary_text: str | None
    job_google_link: str | None
    required_experience_months: int | None
    no_experience_required: bool | None
    created_at: datetime
    updated_at: datetime
    details_refreshed_at: datetime | None


def _snippet(text: str | None, length: int = 300) -> str | None:
    if not text:
        return None
    text = " ".join(text.split())
    return text if len(text) <= length else text[: length - 1].rsplit(" ", 1)[0] + "…"


def job_summary(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "external_job_id": job.external_id,
        "source": job.source.value,
        "job_title": job.title,
        "employer_name": job.company_name,
        "employer_logo": job.company_logo,
        "job_city": job.city,
        "job_state": job.state,
        "job_country": job.country.upper() if job.country else None,
        "job_location": job.location,
        "job_is_remote": job.is_remote,
        "job_employment_type": job.employment_type,
        "job_employment_types": job.employment_types or [],
        "job_min_salary": job.min_salary,
        "job_max_salary": job.max_salary,
        "job_salary_period": job.salary_period,
        "job_currency": job.salary_currency,
        "job_posted_at": job.posted_at,
        "job_posted_at_timestamp": int(job.posted_at.timestamp()) if job.posted_at else None,
        "job_apply_link": job.apply_url,
        "job_publisher": job.publisher,
        "job_description_snippet": _snippet(job.description),
        "job_required_skills": job.required_skills or [],
        "is_external": job.source.value == "jsearch",
        "listing_unavailable": job.unavailable_since is not None,
        "last_seen_at": job.last_seen_at,
    }


def job_detail(job: Job) -> dict[str, Any]:
    data = job_summary(job)
    data.update(
        employer_website=job.company_website,
        job_description=job.description,
        job_highlights=job.highlights or {},
        apply_options=job.apply_options or [],
        job_apply_is_direct=job.apply_is_direct,
        job_salary_text=job.salary_text,
        job_google_link=job.google_link,
        required_experience_months=job.required_experience_months,
        no_experience_required=job.no_experience_required,
        created_at=job.created_at,
        updated_at=job.updated_at,
        details_refreshed_at=job.details_refreshed_at,
    )
    return data


class SearchMeta(BaseModel):
    page_size: int
    has_more: bool
    next_cursor: str | None
    total_results: int | None = None  # the provider does not report reliable totals
    source: str = "jsearch"
    cached: bool
    stale: bool = False
    country: str | None = None


class JobSearchOut(BaseModel):
    items: list[JobSummaryOut]
    pagination: SearchMeta


class JobDetailOut(BaseModel):
    job: JobOut
    details_refreshed: bool
    details_stale: bool


class Suggestion(BaseModel):
    title: str
    listings: int
