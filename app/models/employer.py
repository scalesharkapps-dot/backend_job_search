"""Employer profiles, verification requests, abuse reports and the admin audit log."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import ForeignKey, Index, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, utcnow
from app.models.user import str_enum


class CompanySize(str, enum.Enum):
    S1_10 = "1-10"
    S11_50 = "11-50"
    S51_200 = "51-200"
    S201_500 = "201-500"
    S501_1000 = "501-1000"
    S1000_PLUS = "1000+"


class VerificationStatus(str, enum.Enum):
    UNVERIFIED = "unverified"  # registered, nothing submitted yet
    PENDING = "pending"  # submitted, awaiting an admin
    UNDER_REVIEW = "under_review"
    VERIFIED = "verified"
    REJECTED = "rejected"


class EmployerAccountStatus(str, enum.Enum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class EmployerProfile(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "employer_profiles"

    owner_user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    company_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(String(100))
    website: Mapped[str | None] = mapped_column(String(500))
    company_size: Mapped[CompanySize | None] = mapped_column(str_enum(CompanySize, "company_size"))
    headquarters: Mapped[str | None] = mapped_column(String(200))
    country: Mapped[str | None] = mapped_column(String(2))
    contact_email: Mapped[str] = mapped_column(String(320), nullable=False)
    contact_phone: Mapped[str | None] = mapped_column(String(32))
    social_links: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    logo_path: Mapped[str | None] = mapped_column(String(500))
    verification_status: Mapped[VerificationStatus] = mapped_column(
        str_enum(VerificationStatus, "verification_status"), nullable=False, default=VerificationStatus.UNVERIFIED, index=True
    )
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    account_status: Mapped[EmployerAccountStatus] = mapped_column(
        str_enum(EmployerAccountStatus, "employer_account_status"), nullable=False, default=EmployerAccountStatus.ACTIVE, index=True
    )
    status_reason: Mapped[str | None] = mapped_column(String(500))


class VerificationRequestStatus(str, enum.Enum):
    PENDING = "pending"
    UNDER_REVIEW = "under_review"
    APPROVED = "approved"
    REJECTED = "rejected"


class EmployerVerificationRequest(UUIDPrimaryKeyMixin, Base):
    """Verification evidence. Deliberately no identity documents are collected."""

    __tablename__ = "employer_verification_requests"

    employer_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("employer_profiles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    submitted_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    business_registration_number: Mapped[str | None] = mapped_column(String(100))
    company_website: Mapped[str | None] = mapped_column(String(500))
    official_email: Mapped[str | None] = mapped_column(String(320))
    additional_information: Mapped[str | None] = mapped_column(Text)
    status: Mapped[VerificationRequestStatus] = mapped_column(
        str_enum(VerificationRequestStatus, "verification_request_status"), nullable=False,
        default=VerificationRequestStatus.PENDING,
    )
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    decision_reason: Mapped[str | None] = mapped_column(String(1000))
    submitted_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class ReportTargetType(str, enum.Enum):
    JOB = "job"
    EMPLOYER = "employer"


class ReportReason(str, enum.Enum):
    FRAUD = "fraud"
    SCAM = "scam"
    MISLEADING = "misleading"
    INAPPROPRIATE = "inappropriate"
    DISCRIMINATION = "discrimination"
    SPAM = "spam"
    OTHER = "other"


class ReportStatus(str, enum.Enum):
    PENDING = "pending"
    UNDER_REVIEW = "under_review"
    RESOLVED = "resolved"
    REJECTED = "rejected"


class Report(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "reports"
    __table_args__ = (Index("ix_reports_target", "target_type", "target_id"),)

    reporter_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    target_type: Mapped[ReportTargetType] = mapped_column(str_enum(ReportTargetType, "report_target_type"), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    reason: Mapped[ReportReason] = mapped_column(str_enum(ReportReason, "report_reason"), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[ReportStatus] = mapped_column(
        str_enum(ReportStatus, "report_status"), nullable=False, default=ReportStatus.PENDING, index=True
    )
    resolution_note: Mapped[str | None] = mapped_column(String(2000))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"))
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class AdminAuditLog(UUIDPrimaryKeyMixin, Base):
    """Append-only record of privileged and sensitive actions (no secrets stored)."""

    __tablename__ = "admin_audit_logs"
    __table_args__ = (Index("ix_admin_audit_logs_target", "target_type", "target_id"),)

    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(64), nullable=False)
    previous_value: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    new_value: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    reason: Mapped[str | None] = mapped_column(String(1000))
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow, index=True)
