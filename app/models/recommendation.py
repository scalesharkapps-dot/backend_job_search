"""Recommendation preferences and persisted recommendation results."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Float, ForeignKey, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, utcnow
from app.models.user import str_enum


class RemotePreference(str, enum.Enum):
    ANY = "any"
    REMOTE_ONLY = "remote_only"
    HYBRID = "hybrid"
    ONSITE = "onsite"


class ExperienceLevel(str, enum.Enum):
    INTERNSHIP = "internship"
    ENTRY = "entry"
    JUNIOR = "junior"
    MID = "mid"
    SENIOR = "senior"
    LEAD = "lead"
    MANAGER = "manager"


class UserRecommendationPreferences(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_recommendation_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    preferred_job_titles: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    preferred_locations: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    preferred_countries: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    employment_types: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    remote_preference: Mapped[RemotePreference] = mapped_column(
        str_enum(RemotePreference, "remote_preference"), nullable=False, default=RemotePreference.ANY
    )
    experience_level: Mapped[ExperienceLevel | None] = mapped_column(
        str_enum(ExperienceLevel, "experience_level")
    )
    min_match_score: Mapped[int | None] = mapped_column(Integer)


class JobRecommendation(UUIDPrimaryKeyMixin, Base):
    """Latest computed match per (user, job). Overwritten on recomputation."""

    __tablename__ = "job_recommendations"
    __table_args__ = (UniqueConstraint("user_id", "job_id", name="uq_job_recommendations_user_job"),)

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cv_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("user_cvs.id", ondelete="SET NULL"))
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True)
    match_percentage: Mapped[int] = mapped_column(Integer, nullable=False)
    score_confidence: Mapped[float] = mapped_column(Float, nullable=False)
    score_breakdown: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    matched_skills: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    missing_skills: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    explanation: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    algorithm_version: Mapped[str] = mapped_column(String(32), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, index=True)
