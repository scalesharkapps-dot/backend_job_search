"""Employer, job-posting, applicant, report and admin schemas."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.models.application import EmployerReviewStatus
from app.models.employer import (
    CompanySize,
    EmployerAccountStatus,
    ReportReason,
    ReportStatus,
    ReportTargetType,
    VerificationStatus,
)
from app.models.job import PostingStatus, WorkplaceType
from app.models.recommendation import ExperienceLevel
from app.models.user import UserRole
from app.schemas.profile import PHONE_RE, EmploymentType, _clean_str_list, normalize_skills_value
from app.services.jsearch.normalizer import safe_url

SOCIAL_KEYS = {"linkedin", "facebook", "twitter", "x", "instagram", "youtube", "github"}


def _url(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    if safe_url(v.strip()) is None:
        raise ValueError("must be a valid http(s) URL")
    return v.strip()


def _phone(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    if not PHONE_RE.match(v.strip()):
        raise ValueError("Invalid phone number format")
    digits = re.sub(r"\D", "", v)
    if not 7 <= len(digits) <= 15:
        raise ValueError("Phone number must contain 7-15 digits")
    return ("+" if v.strip().startswith("+") else "") + digits


def _country(v: str | None) -> str | None:
    if v is None or not v.strip():
        return None
    v = v.strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", v):
        raise ValueError("country must be a 2-letter ISO code")
    return v


# ---------------- Employer profile ----------------
class EmployerProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_name: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    industry: str | None = Field(default=None, max_length=100)
    website: str | None = Field(default=None, max_length=500)
    company_size: CompanySize | None = None
    headquarters: str | None = Field(default=None, max_length=200)
    country: str | None = None
    contact_email: EmailStr
    contact_phone: str | None = Field(default=None, max_length=32)
    social_links: dict[str, str] = Field(default_factory=dict)

    @field_validator("company_name", "description", "industry", "headquarters")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return (v.strip() or None) if isinstance(v, str) else v

    _w = field_validator("website")(classmethod(lambda cls, v: _url(v)))
    _p = field_validator("contact_phone")(classmethod(lambda cls, v: _phone(v)))
    _c = field_validator("country")(classmethod(lambda cls, v: _country(v)))

    @field_validator("social_links")
    @classmethod
    def _social(cls, v: dict[str, str]) -> dict[str, str]:
        out = {}
        for key, url in v.items():
            key = key.lower().strip()
            if key not in SOCIAL_KEYS:
                raise ValueError(f"unsupported social link '{key}'")
            out[key] = _url(url)
        return {k: u for k, u in out.items() if u}


class EmployerProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    company_name: str
    description: str | None
    industry: str | None
    website: str | None
    company_size: CompanySize | None
    headquarters: str | None
    country: str | None
    contact_email: str
    contact_phone: str | None
    social_links: dict[str, str]
    logo_url: str | None = None
    verification_status: VerificationStatus
    verified_at: datetime | None
    account_status: EmployerAccountStatus
    status_reason: str | None = None
    created_at: datetime
    updated_at: datetime


class EmployerPublicOut(BaseModel):
    """Public company page: never includes private contact details."""

    id: uuid.UUID
    company_name: str
    description: str | None
    industry: str | None
    website: str | None
    company_size: CompanySize | None
    headquarters: str | None
    country: str | None
    social_links: dict[str, str]
    logo_url: str | None
    is_verified: bool
    open_jobs: int


class VerificationSubmit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    business_registration_number: str | None = Field(default=None, max_length=100)
    company_website: str | None = Field(default=None, max_length=500)
    official_email: EmailStr | None = None
    additional_information: str | None = Field(default=None, max_length=3000)

    _w = field_validator("company_website")(classmethod(lambda cls, v: _url(v)))

    @model_validator(mode="after")
    def _some_evidence(self) -> VerificationSubmit:
        if not (self.business_registration_number or self.company_website or self.official_email):
            raise ValueError("Provide a registration number, company website or official email")
        return self


class VerificationRequestOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    business_registration_number: str | None
    company_website: str | None
    official_email: str | None
    additional_information: str | None
    status: str
    decision_reason: str | None
    submitted_at: datetime
    reviewed_at: datetime | None


class VerificationStateOut(BaseModel):
    verification_status: VerificationStatus
    can_submit: bool
    latest_request: VerificationRequestOut | None


# ---------------- Employer job postings ----------------
class EmployerJobIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=3, max_length=200)
    description: str = Field(min_length=30, max_length=20000)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    experience_level: ExperienceLevel | None = None
    min_experience_years: float | None = Field(default=None, ge=0, le=50)
    max_experience_years: float | None = Field(default=None, ge=0, le=50)
    employment_type: EmploymentType
    workplace_type: WorkplaceType = WorkplaceType.ONSITE
    city: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=200)
    country: str | None = None
    min_salary: float | None = Field(default=None, gt=0)
    max_salary: float | None = Field(default=None, gt=0)
    salary_currency: str | None = Field(default=None, description="ISO 4217, e.g. PKR")
    salary_period: str | None = Field(default=None, description="HOUR, DAY, WEEK, MONTH or YEAR")
    education_requirements: str | None = Field(default=None, max_length=500)
    application_deadline: date | None = None
    application_instructions: str | None = Field(default=None, max_length=3000)
    external_application_url: str | None = Field(default=None, max_length=2048)
    openings: int | None = Field(default=None, ge=1, le=1000)
    benefits: list[str] = Field(default_factory=list)

    @field_validator("title", "description", "city", "location", "education_requirements", "application_instructions")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return (v.strip() or None) if isinstance(v, str) else v

    @field_validator("required_skills", "preferred_skills")
    @classmethod
    def _skills(cls, v: list[str]) -> list[str]:
        return normalize_skills_value(v) or []

    @field_validator("benefits")
    @classmethod
    def _benefits(cls, v: list[str]) -> list[str]:
        return _clean_str_list(v, 30, 200)

    _c = field_validator("country")(classmethod(lambda cls, v: _country(v)))
    _u = field_validator("external_application_url")(classmethod(lambda cls, v: _url(v)))

    @field_validator("salary_currency")
    @classmethod
    def _currency(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not re.fullmatch(r"[A-Za-z]{3}", v.strip()):
            raise ValueError("currency must be a 3-letter ISO 4217 code")
        return v.strip().upper()

    @field_validator("salary_period")
    @classmethod
    def _period(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if v not in {"HOUR", "DAY", "WEEK", "MONTH", "YEAR"}:
            raise ValueError("salary_period must be HOUR, DAY, WEEK, MONTH or YEAR")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> EmployerJobIn:
        if self.min_salary and self.max_salary and self.min_salary > self.max_salary:
            raise ValueError("min_salary must not exceed max_salary")
        if (self.min_salary or self.max_salary) and not (self.salary_currency and self.salary_period):
            raise ValueError("salary_currency and salary_period are required when a salary is given")
        if self.min_experience_years is not None and self.max_experience_years is not None and \
                self.min_experience_years > self.max_experience_years:
            raise ValueError("min_experience_years must not exceed max_experience_years")
        if self.workplace_type != WorkplaceType.REMOTE and not (self.city or self.location or self.country):
            raise ValueError("A location is required for on-site and hybrid jobs")
        overlap = set(self.required_skills) & set(self.preferred_skills)
        if overlap:
            self.preferred_skills = [s for s in self.preferred_skills if s not in overlap]
        return self


class EmployerJobCreate(EmployerJobIn):
    submit_for_review: bool = Field(default=False, description="Immediately submit (requires a verified employer)")


class JobStatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: PostingStatus
    reason: str | None = Field(default=None, max_length=1000)


class EmployerJobOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    required_skills: list[str]
    preferred_skills: list[str]
    experience_level: str | None
    min_experience_years: float | None
    max_experience_years: float | None
    employment_type: str | None
    workplace_type: WorkplaceType | None
    city: str | None
    location: str | None
    country: str | None
    is_remote: bool
    min_salary: float | None
    max_salary: float | None
    salary_currency: str | None
    salary_period: str | None
    education_requirements: str | None
    application_deadline: date | None
    application_instructions: str | None
    external_application_url: str | None
    openings: int | None
    benefits: list[str]
    posting_status: PostingStatus
    moderation_note: str | None
    published_at: datetime | None
    applicant_count: int = 0
    created_at: datetime
    updated_at: datetime


# ---------------- Applicants ----------------
class ApplicantProfileOut(BaseModel):
    user_id: uuid.UUID
    full_name: str | None
    email: str
    phone: str | None
    headline: str | None
    city: str | None
    country: str | None
    skills: list[str]
    years_of_experience: float | None


class EmployerApplicationOut(BaseModel):
    id: uuid.UUID
    job_id: uuid.UUID | None
    job_title: str
    submitted_at: datetime | None
    employer_status: EmployerReviewStatus | None
    employer_notes: str | None
    candidate_withdrawn: bool
    cover_letter: str | None
    match_percentage: int | None
    cv_available: bool
    cv_filename: str | None
    applicant: ApplicantProfileOut


class EmployerStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: EmployerReviewStatus
    notes: str | None = Field(default=None, max_length=5000, description="Private employer notes")


class EmployerStatisticsOut(BaseModel):
    total_jobs: int
    jobs_by_status: dict[str, int]
    total_applicants: int
    applicants_by_status: dict[str, int]
    applicants_per_job: list[dict[str, Any]]
    pending_applications: int
    shortlisted: int
    interviews: int
    offers: int
    hires: int
    withdrawn: int


# ---------------- Reports ----------------
class ReportCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_type: ReportTargetType
    target_id: uuid.UUID
    reason: ReportReason
    description: str | None = Field(default=None, max_length=3000)


class ReportOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    target_type: ReportTargetType
    target_id: uuid.UUID
    reason: ReportReason
    description: str | None
    status: ReportStatus
    resolution_note: str | None
    created_at: datetime
    reviewed_at: datetime | None


class AdminReportOut(ReportOut):
    reporter_id: uuid.UUID | None
    reviewed_by: uuid.UUID | None


class ReportStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: ReportStatus
    resolution_note: str | None = Field(default=None, max_length=2000)


# ---------------- Admin ----------------
class ReasonRequired(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=1000)


class AdminUserStatusUpdate(ReasonRequired):
    is_active: bool


class AdminRoleUpdate(ReasonRequired):
    role: UserRole


class AdminVerificationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(under_review|approve|reject)$")
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reason(self) -> AdminVerificationDecision:
        if self.decision == "reject" and not self.reason:
            raise ValueError("A reason is required when rejecting")
        return self


class AdminEmployerStatusUpdate(ReasonRequired):
    account_status: EmployerAccountStatus


class AdminModerationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str = Field(pattern="^(approve|reject)$")
    reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _reason(self) -> AdminModerationDecision:
        if self.decision == "reject" and not self.reason:
            raise ValueError("A reason is required when rejecting")
        return self


class AdminJobStatusUpdate(ReasonRequired):
    status: PostingStatus


class AdminUserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str | None
    role: UserRole
    is_active: bool
    email_verified: bool
    suspended_reason: str | None
    last_login_at: datetime | None
    created_at: datetime


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    action: str
    target_type: str
    target_id: str
    previous_value: dict[str, Any] | None
    new_value: dict[str, Any] | None
    reason: str | None
    request_id: str | None
    created_at: datetime
