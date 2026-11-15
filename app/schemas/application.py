"""Saved-job, application and interview schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.application import (
    ApplicationSource,
    ApplicationStatus,
    DiscoveredVia,
    InterviewOutcome,
    InterviewType,
)
from app.schemas.job import JobSummaryOut
from app.services.jsearch.normalizer import safe_url


def _clean(v: str | None) -> str | None:
    return (v.strip() or None) if isinstance(v, str) else v


def _validate_apply_url(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    if safe_url(v) is None:
        raise ValueError("must be a valid http(s) URL")
    return v.strip()


# ---------------- Saved jobs ----------------
class SaveJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str | None = Field(default=None, max_length=1000)


class SavedJobOut(BaseModel):
    id: uuid.UUID
    job: JobSummaryOut
    match_percentage: int | None
    notes: str | None
    saved_at: datetime


class SavedCheckOut(BaseModel):
    job_id: uuid.UUID
    is_saved: bool
    saved_job_id: uuid.UUID | None = None


# ---------------- Applications ----------------
class ApplicationCreate(BaseModel):
    """Create an application record.

    Provide ``job_id`` for a job stored in SharkHire (details are copied), or
    ``job_title`` (+ optional company/location/apply URL) for a job found
    elsewhere. Creating a record does NOT submit anything to an external employer.
    """

    model_config = ConfigDict(extra="forbid")
    job_id: uuid.UUID | None = None
    job_title: str | None = Field(default=None, max_length=300)
    company_name: str | None = Field(default=None, max_length=300)
    job_location: str | None = Field(default=None, max_length=300)
    job_apply_url: str | None = Field(default=None, max_length=2048)
    employment_type: str | None = Field(default=None, max_length=32)
    salary_information: str | None = Field(default=None, max_length=200)
    status: ApplicationStatus = ApplicationStatus.APPLIED
    applied_at: datetime | None = None
    deadline: date | None = None
    follow_up_date: date | None = None
    reminder_text: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=10000)
    selected_cv_id: uuid.UUID | None = None
    discovered_via: DiscoveredVia | None = None
    cover_letter: str | None = Field(default=None, max_length=10000, description="SharkHire jobs only")

    @field_validator("job_title", "company_name", "job_location", "salary_information", "reminder_text", "notes")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return _clean(v)

    @field_validator("job_apply_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _validate_apply_url(v)

    @model_validator(mode="after")
    def _job_or_title(self) -> ApplicationCreate:
        if self.job_id is None and not self.job_title:
            raise ValueError("Provide job_id or job_title")
        return self


class ApplicationUpdate(BaseModel):
    """PUT body: replaces the editable fields provided (status changes use PATCH /status)."""

    model_config = ConfigDict(extra="forbid")
    job_title: str | None = Field(default=None, max_length=300)
    company_name: str | None = Field(default=None, max_length=300)
    job_location: str | None = Field(default=None, max_length=300)
    job_apply_url: str | None = Field(default=None, max_length=2048)
    employment_type: str | None = Field(default=None, max_length=32)
    salary_information: str | None = Field(default=None, max_length=200)
    applied_at: datetime | None = None
    deadline: date | None = None
    follow_up_date: date | None = None
    reminder_text: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=10000)
    selected_cv_id: uuid.UUID | None = None

    @field_validator("company_name", "job_location", "salary_information", "reminder_text", "notes")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return _clean(v)

    @field_validator("job_apply_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return _validate_apply_url(v)

    @field_validator("job_title")
    @classmethod
    def _title_not_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        cleaned = _clean(v)
        if not cleaned:
            raise ValueError("job_title cannot be empty")
        return cleaned


class StatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ApplicationStatus
    notes: str | None = Field(default=None, max_length=1000)


class StatusHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    previous_status: str | None
    new_status: str
    changed_at: datetime
    notes: str | None


class ApplicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    job_id: uuid.UUID | None
    external_job_id: str | None
    source: ApplicationSource
    discovered_via: DiscoveredVia | None
    job_title: str
    company_name: str | None
    job_location: str | None
    job_apply_url: str | None
    employment_type: str | None
    salary_information: str | None
    status: ApplicationStatus
    applied_at: datetime | None
    deadline: date | None
    follow_up_date: date | None
    reminder_text: str | None
    notes: str | None
    selected_cv_id: uuid.UUID | None
    match_percentage: int | None
    submitted_at: datetime | None = None
    employer_status: str | None = Field(default=None, description="Employer pipeline status (SharkHire jobs)")
    created_at: datetime
    updated_at: datetime


class ApplicationDetailOut(ApplicationOut):
    history: list[StatusHistoryOut] = []
    interviews: list[InterviewOut] = []
    job: JobSummaryOut | None = None


# ---------------- Interviews ----------------
def _validate_tz(v: str) -> str:
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("timezone must be a valid IANA name, e.g. Asia/Karachi") from None
    return v


class InterviewBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interview_type: InterviewType
    interview_round: int | None = Field(default=None, ge=1, le=20)
    scheduled_at: datetime = Field(description="ISO 8601. A naive time is interpreted in `timezone`.")
    timezone: str = Field(default="UTC", max_length=64)
    duration_minutes: int | None = Field(default=None, ge=5, le=600)
    location_or_meeting_link: str | None = Field(default=None, max_length=1000)
    interviewer_name: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=10000)
    outcome: InterviewOutcome = InterviewOutcome.PENDING

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str) -> str:
        return _validate_tz(v)

    @field_validator("location_or_meeting_link")
    @classmethod
    def _link(cls, v: str | None) -> str | None:
        v = _clean(v)
        if v and "://" in v and safe_url(v) is None:
            raise ValueError("meeting link must be a valid http(s) URL")
        return v

    @model_validator(mode="after")
    def _aware(self) -> InterviewBase:
        if self.scheduled_at.tzinfo is None:
            self.scheduled_at = self.scheduled_at.replace(tzinfo=ZoneInfo(self.timezone))
        if not 2000 <= self.scheduled_at.year <= 2100:
            raise ValueError("scheduled_at is out of range")
        return self


class InterviewCreate(InterviewBase):
    update_application_status: bool = Field(
        default=True, description="Move the application to interview_scheduled when it is at an earlier stage."
    )


class InterviewUpdate(InterviewBase):
    pass


class InterviewOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    application_id: uuid.UUID
    interview_type: InterviewType
    interview_round: int | None
    scheduled_at: datetime
    timezone: str
    local_time: str | None = None
    duration_minutes: int | None
    location_or_meeting_link: str | None
    interviewer_name: str | None
    notes: str | None
    outcome: InterviewOutcome
    created_at: datetime
    updated_at: datetime


class UpcomingInterviewOut(InterviewOut):
    job_title: str
    company_name: str | None


class ReminderOut(BaseModel):
    type: str  # deadline | follow_up | interview
    due: datetime | date
    overdue: bool
    application_id: uuid.UUID
    job_title: str
    company_name: str | None
    status: ApplicationStatus
    text: str | None = None
    interview_id: uuid.UUID | None = None


class StatisticsOut(BaseModel):
    total_applications: int
    saved_jobs: int
    by_status: dict[str, int]
    applied_count: int
    shortlisted_count: int
    interviews_scheduled: int
    offers_received: int
    rejected_count: int
    hired_count: int
    withdrawn_count: int
    upcoming_interviews: int
    upcoming_deadlines: int
    applications_per_month: list[dict[str, Any]]
    interview_conversion_rate: float | None
    offer_conversion_rate: float | None
    definitions: dict[str, str]


ApplicationDetailOut.model_rebuild()
