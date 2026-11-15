"""User profile, CV and extracted-CV-data models."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from app.models.user import str_enum


class UserProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Job-seeker profile (1:1 with users). All list fields are user-confirmed data."""

    __tablename__ = "user_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    full_name: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(32))
    headline: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(2))  # ISO-3166 alpha-2
    city: Mapped[str | None] = mapped_column(String(100))
    professional_summary: Mapped[str | None] = mapped_column(Text)
    current_job_title: Mapped[str | None] = mapped_column(String(200))
    years_of_experience: Mapped[float | None] = mapped_column(Float)
    education: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    experience: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    skills: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    certifications: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    preferred_job_titles: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    preferred_employment_types: Mapped[list[Any]] = mapped_column(
        JSONType, default=list, nullable=False
    )
    preferred_locations: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    profile_photo_path: Mapped[str | None] = mapped_column(String(500))


class CVProcessingStatus(str, enum.Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    NO_TEXT = "no_text"
    OCR_REQUIRED = "ocr_required"
    FAILED = "failed"


class CVFileType(str, enum.Enum):
    PDF = "pdf"
    DOCX = "docx"


class UserCV(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_cvs"
    __table_args__ = (
        UniqueConstraint("user_id", "file_hash", name="uq_user_cvs_user_hash"),
        # At most one primary CV per user.
        Index(
            "uq_user_cvs_one_primary",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary = 1"),
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_type: Mapped[CVFileType] = mapped_column(str_enum(CVFileType, "cv_file_type"), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer)
    processing_status: Mapped[CVProcessingStatus] = mapped_column(
        str_enum(CVProcessingStatus, "cv_processing_status"),
        nullable=False,
        default=CVProcessingStatus.PENDING,
    )
    extraction_error: Mapped[str | None] = mapped_column(String(500))
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    extracted: Mapped[CVExtractedData | None] = relationship(
        back_populates="cv", cascade="all, delete-orphan", passive_deletes=True, uselist=False, lazy="raise"
    )


class CVExtractedData(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Information extracted from a CV. ``is_user_confirmed`` marks reviewed data."""

    __tablename__ = "cv_extracted_data"

    cv_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("user_cvs.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extracted_name: Mapped[str | None] = mapped_column(String(200))
    extracted_email: Mapped[str | None] = mapped_column(String(320))
    extracted_phone: Mapped[str | None] = mapped_column(String(32))
    skills: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    unrecognized_skills: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    work_experience: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    education: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    certifications: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    job_titles: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    years_of_experience: Mapped[float | None] = mapped_column(Float)
    extraction_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    is_user_confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    cv: Mapped[UserCV] = relationship(back_populates="extracted", lazy="raise")
