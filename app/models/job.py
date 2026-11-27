"""Job listings (external JSearch jobs and, from Phase 7, employer-posted jobs)."""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from app.models.user import str_enum


class JobSource(str, enum.Enum):
    JSEARCH = "jsearch"
    SHARKHIRE = "sharkhire"  # posted by a SharkHire employer (Phase 7)
    TECHMAP = "techmap"  # Techmap "Daily International Job Postings" (RapidAPI)


# Listings fetched from job-search providers (not posted on SharkHire).
EXTERNAL_SOURCES: tuple[JobSource, ...] = (JobSource.JSEARCH, JobSource.TECHMAP)


class PostingStatus(str, enum.Enum):
    """Lifecycle of SharkHire employer listings (NULL for external jobs)."""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    PUBLISHED = "published"
    PAUSED = "paused"
    CLOSED = "closed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    SUSPENDED = "suspended"  # set by an admin only


class WorkplaceType(str, enum.Enum):
    ONSITE = "onsite"
    REMOTE = "remote"
    HYBRID = "hybrid"


class Job(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("source", "external_id", name="uq_jobs_source_external_id"),
        Index("ix_jobs_posted_at", "posted_at"),
        Index("ix_jobs_last_seen_at", "last_seen_at"),
        Index("ix_jobs_country_remote", "country", "is_remote"),
    )

    source: Mapped[JobSource] = mapped_column(str_enum(JobSource, "job_source"), nullable=False)
    # Provider job id (JSearch ``job_id``) or a stable composite hash ("h:<sha256>").
    external_id: Mapped[str | None] = mapped_column(String(512))
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(300), index=True)
    company_logo: Mapped[str | None] = mapped_column(String(2048))
    company_website: Mapped[str | None] = mapped_column(String(2048))
    description: Mapped[str | None] = mapped_column(Text)
    highlights: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    required_skills: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    city: Mapped[str | None] = mapped_column(String(200))
    state: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(2))
    location: Mapped[str | None] = mapped_column(String(300))
    is_remote: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    employment_type: Mapped[str | None] = mapped_column(String(32))
    employment_types: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    min_salary: Mapped[float | None] = mapped_column(Float)
    max_salary: Mapped[float | None] = mapped_column(Float)
    salary_currency: Mapped[str | None] = mapped_column(String(8))
    salary_period: Mapped[str | None] = mapped_column(String(16))
    salary_text: Mapped[str | None] = mapped_column(String(200))
    publisher: Mapped[str | None] = mapped_column(String(200))
    apply_url: Mapped[str | None] = mapped_column(String(2048))
    apply_is_direct: Mapped[bool | None] = mapped_column(Boolean)
    apply_options: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    google_link: Mapped[str | None] = mapped_column(String(2048))
    posted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    required_experience_months: Mapped[int | None] = mapped_column(Integer)
    no_experience_required: Mapped[bool | None] = mapped_column(Boolean)
    provider_metadata: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    details_refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Set when the provider reports the listing no longer exists.
    unavailable_since: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Cached requirement derivation for recommendations (see recommendations.requirements).
    derived_requirements: Mapped[dict[str, Any] | None] = mapped_column(JSONType)

    # ---- SharkHire employer listings (Phase 7); NULL for external jobs ----
    employer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("employer_profiles.id", ondelete="SET NULL"), index=True
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    posting_status: Mapped[PostingStatus | None] = mapped_column(str_enum(PostingStatus, "posting_status"), index=True)
    preferred_skills: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    experience_level: Mapped[str | None] = mapped_column(String(16))
    min_experience_years: Mapped[float | None] = mapped_column(Float)
    max_experience_years: Mapped[float | None] = mapped_column(Float)
    workplace_type: Mapped[WorkplaceType | None] = mapped_column(str_enum(WorkplaceType, "workplace_type"))
    education_requirements: Mapped[str | None] = mapped_column(String(500))
    application_deadline: Mapped[date | None] = mapped_column(Date)
    application_instructions: Mapped[str | None] = mapped_column(Text)
    openings: Mapped[int | None] = mapped_column(Integer)
    benefits: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    moderation_note: Mapped[str | None] = mapped_column(String(1000))
    moderated_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    moderated_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class JobSearchCache(UUIDPrimaryKeyMixin, Base):
    """Cached normalised search result pages (references to ``jobs`` rows)."""

    __tablename__ = "job_search_cache"

    search_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    search_parameters: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    job_ids: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    next_cursor: Mapped[str | None] = mapped_column(String(1024))
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
