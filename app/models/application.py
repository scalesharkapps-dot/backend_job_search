"""Saved jobs, application tracking, status history and interviews."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, utcnow
from app.models.user import str_enum


class ApplicationStatus(str, enum.Enum):
    INTERESTED = "interested"
    SAVED = "saved"
    APPLIED = "applied"
    APPLICATION_VIEWED = "application_viewed"
    SHORTLISTED = "shortlisted"
    INTERVIEW_SCHEDULED = "interview_scheduled"
    INTERVIEW_COMPLETED = "interview_completed"
    ASSESSMENT = "assessment"
    OFFER_RECEIVED = "offer_received"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    HIRED = "hired"


PRE_APPLICATION = {ApplicationStatus.INTERESTED, ApplicationStatus.SAVED}
INTERVIEW_STAGE = {
    ApplicationStatus.INTERVIEW_SCHEDULED, ApplicationStatus.INTERVIEW_COMPLETED, ApplicationStatus.ASSESSMENT,
    ApplicationStatus.OFFER_RECEIVED, ApplicationStatus.HIRED,
}
OFFER_STAGE = {ApplicationStatus.OFFER_RECEIVED, ApplicationStatus.HIRED}
TERMINAL = {ApplicationStatus.REJECTED, ApplicationStatus.WITHDRAWN, ApplicationStatus.HIRED}


class EmployerReviewStatus(str, enum.Enum):
    """Employer-side pipeline for SharkHire (internal) applications only."""

    SUBMITTED = "submitted"
    UNDER_REVIEW = "under_review"
    SHORTLISTED = "shortlisted"
    INTERVIEW = "interview"
    OFFERED = "offered"
    HIRED = "hired"
    REJECTED = "rejected"


class ApplicationSource(str, enum.Enum):
    JSEARCH = "jsearch"  # external listing found via JSearch (applied outside SharkHire)
    SHARKHIRE = "sharkhire"  # SharkHire employer listing (internal submission, Phase 7)
    MANUAL = "manual"  # job not stored in SharkHire; details entered by the user


class DiscoveredVia(str, enum.Enum):
    SEARCH = "search"
    RECOMMENDATION = "recommendation"
    SAVED_JOBS = "saved_jobs"
    OTHER = "other"


class SavedJob(UUIDPrimaryKeyMixin, Base):
    __tablename__ = "saved_jobs"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_saved_jobs_user_job"),)

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    match_percentage: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, index=True)


class Application(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A job seeker's application record. Job details are snapshotted so the
    record stays meaningful if the listing disappears (job_id → NULL)."""

    __tablename__ = "applications"
    __table_args__ = (
        Index(
            "uq_applications_user_job",
            "user_id",
            "job_id",
            unique=True,
            postgresql_where=text("job_id IS NOT NULL"),
            sqlite_where=text("job_id IS NOT NULL"),
        ),
        Index("ix_applications_user_status", "user_id", "status"),
        Index("ix_applications_user_updated", "user_id", "updated_at"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), index=True)
    external_job_id: Mapped[str | None] = mapped_column(String(512))
    source: Mapped[ApplicationSource] = mapped_column(str_enum(ApplicationSource, "application_source"), nullable=False)
    discovered_via: Mapped[DiscoveredVia | None] = mapped_column(str_enum(DiscoveredVia, "discovered_via"))
    job_title: Mapped[str] = mapped_column(String(300), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(300))
    job_location: Mapped[str | None] = mapped_column(String(300))
    job_apply_url: Mapped[str | None] = mapped_column(String(2048))
    employment_type: Mapped[str | None] = mapped_column(String(32))
    salary_information: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[ApplicationStatus] = mapped_column(
        str_enum(ApplicationStatus, "application_status"), nullable=False, default=ApplicationStatus.APPLIED
    )
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    deadline: Mapped[date | None] = mapped_column(Date, index=True)
    follow_up_date: Mapped[date | None] = mapped_column(Date, index=True)
    reminder_text: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    selected_cv_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("user_cvs.id", ondelete="SET NULL"))
    match_percentage: Mapped[int | None] = mapped_column(Integer)
    # ---- Internal submissions to SharkHire employers (Phase 7) ----
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    cover_letter: Mapped[str | None] = mapped_column(Text)
    employer_status: Mapped[EmployerReviewStatus | None] = mapped_column(
        str_enum(EmployerReviewStatus, "employer_review_status"), index=True
    )
    employer_notes: Mapped[str | None] = mapped_column(Text)  # never shown to the candidate


class ApplicationStatusHistory(UUIDPrimaryKeyMixin, Base):
    """Append-only status changes. ``pipeline`` distinguishes the candidate's own
    tracker from an employer's review pipeline (Phase 7)."""

    __tablename__ = "application_status_history"

    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    pipeline: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate")
    previous_status: Mapped[str | None] = mapped_column(String(32))
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    changed_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(String(1000))
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)


class InterviewType(str, enum.Enum):
    TECHNICAL = "technical"
    HR = "hr"
    PHONE = "phone"
    VIDEO = "video"
    IN_PERSON = "in_person"
    OTHER = "other"


class InterviewOutcome(str, enum.Enum):
    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"


class Interview(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "interviews"
    __table_args__ = (Index("ix_interviews_scheduled_at", "scheduled_at"),)

    application_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    interview_type: Mapped[InterviewType] = mapped_column(str_enum(InterviewType, "interview_type"), nullable=False)
    interview_round: Mapped[int | None] = mapped_column(Integer)
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    duration_minutes: Mapped[int | None] = mapped_column(Integer)
    location_or_meeting_link: Mapped[str | None] = mapped_column(String(1000))
    interviewer_name: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[InterviewOutcome] = mapped_column(
        str_enum(InterviewOutcome, "interview_outcome"), nullable=False, default=InterviewOutcome.PENDING
    )


__all__ = [
    "INTERVIEW_STAGE",
    "OFFER_STAGE",
    "PRE_APPLICATION",
    "TERMINAL",
    "Application",
    "ApplicationSource",
    "ApplicationStatus",
    "ApplicationStatusHistory",
    "DiscoveredVia",
    "EmployerReviewStatus",
    "Interview",
    "InterviewOutcome",
    "InterviewType",
    "SavedJob",
]
