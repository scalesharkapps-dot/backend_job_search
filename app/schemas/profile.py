"""Profile and CV schemas."""

from __future__ import annotations

import re
import uuid
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.models.profile import CVFileType, CVProcessingStatus
from app.services.skills import normalize_skill_list

PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()\-]{6,19}$")


class EmploymentType(str, Enum):
    FULLTIME = "FULLTIME"
    PARTTIME = "PARTTIME"
    CONTRACTOR = "CONTRACTOR"
    INTERN = "INTERN"


class EducationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    degree: str | None = Field(default=None, max_length=200)
    institution: str | None = Field(default=None, max_length=200)
    field_of_study: str | None = Field(default=None, max_length=200)
    start_year: int | None = Field(default=None, ge=1950, le=2100)
    end_year: int | None = Field(default=None, ge=1950, le=2100)

    @model_validator(mode="after")
    def _years(self) -> EducationItem:
        if self.start_year and self.end_year and self.end_year < self.start_year:
            raise ValueError("end_year must not be before start_year")
        if not (self.degree or self.institution):
            raise ValueError("degree or institution is required")
        return self


class ExperienceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    company: str | None = Field(default=None, max_length=200)
    start_date: date | None = None
    end_date: date | None = None
    is_current: bool = False
    description: str | None = Field(default=None, max_length=3000)

    @model_validator(mode="after")
    def _dates(self) -> ExperienceItem:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        if self.is_current:
            self.end_date = None
        return self


def _clean_str_list(values: list[str], max_items: int, max_len: int) -> list[str]:
    out: dict[str, None] = {}
    for v in values:
        v = re.sub(r"\s+", " ", str(v)).strip()
        if v:
            if len(v) > max_len:
                raise ValueError(f"items must be at most {max_len} characters")
            out.setdefault(v, None)
    if len(out) > max_items:
        raise ValueError(f"at most {max_items} items are allowed")
    return list(out)


def normalize_skills_value(v: list[str] | None) -> list[str] | None:
    """Canonicalise known skills, keep unknown ones verbatim (deduplicated)."""
    if v is None:
        return None
    cleaned = _clean_str_list(v, 100, 60)
    known, unknown = normalize_skill_list(cleaned)
    return known + [u for u in unknown if u.lower() not in {k.lower() for k in known}]


class ProfileBase(BaseModel):
    """Editable profile fields. Role, email and user id are not editable here."""

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=32)
    headline: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    country: str | None = Field(default=None, description="ISO 3166-1 alpha-2 code, e.g. PK")
    city: str | None = Field(default=None, max_length=100)
    professional_summary: str | None = Field(default=None, max_length=5000)
    current_job_title: str | None = Field(default=None, max_length=200)
    years_of_experience: float | None = Field(default=None, ge=0, le=60)
    education: list[EducationItem] | None = None
    experience: list[ExperienceItem] | None = None
    skills: list[str] | None = None
    certifications: list[str] | None = None
    preferred_job_titles: list[str] | None = None
    preferred_employment_types: list[EmploymentType] | None = None
    preferred_locations: list[str] | None = None

    @field_validator("full_name", "headline", "location", "city", "current_job_title", "professional_summary")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        return (v.strip() or None) if isinstance(v, str) else v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not PHONE_RE.match(v.strip()):
            raise ValueError("Invalid phone number format")
        digits = re.sub(r"\D", "", v)
        if not 7 <= len(digits) <= 15:
            raise ValueError("Phone number must contain 7-15 digits")
        return ("+" if v.strip().startswith("+") else "") + digits

    @field_validator("country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().upper()
        if not re.fullmatch(r"[A-Z]{2}", v):
            raise ValueError("country must be a 2-letter ISO code")
        return v

    @field_validator("skills")
    @classmethod
    def _skills(cls, v: list[str] | None) -> list[str] | None:
        return normalize_skills_value(v)

    @field_validator("certifications")
    @classmethod
    def _certs(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_str_list(v, 50, 200)

    @field_validator("preferred_job_titles", "preferred_locations")
    @classmethod
    def _prefs(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_str_list(v, 20, 100)

    @field_validator("education")
    @classmethod
    def _edu_len(cls, v: list[EducationItem] | None) -> list[EducationItem] | None:
        if v is not None and len(v) > 20:
            raise ValueError("at most 20 education entries")
        return v

    @field_validator("experience")
    @classmethod
    def _exp_len(cls, v: list[ExperienceItem] | None) -> list[ExperienceItem] | None:
        if v is not None and len(v) > 50:
            raise ValueError("at most 50 experience entries")
        return v


class ProfileUpdate(ProfileBase):
    """PUT body: replaces all editable fields (omitted fields are cleared)."""


class ProfilePatch(ProfileBase):
    """PATCH body: only provided fields are changed."""


class ProfileOut(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str
    full_name: str | None = None
    phone: str | None = None
    headline: str | None = None
    location: str | None = None
    country: str | None = None
    city: str | None = None
    professional_summary: str | None = None
    current_job_title: str | None = None
    years_of_experience: float | None = None
    education: list[dict[str, Any]] = []
    experience: list[dict[str, Any]] = []
    skills: list[str] = []
    certifications: list[str] = []
    preferred_job_titles: list[str] = []
    preferred_employment_types: list[str] = []
    preferred_locations: list[str] = []
    profile_photo_url: str | None = None
    profile_completion: int
    completion_breakdown: dict[str, bool]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PhotoOut(BaseModel):
    profile_photo_url: str
    expires_in: int


class CVOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    original_filename: str
    file_type: CVFileType
    file_size: int
    page_count: int | None = None
    processing_status: CVProcessingStatus
    extraction_error: str | None = None
    is_primary: bool
    processed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CVUploadOut(CVOut):
    skills_found: int = 0


class CVDownloadOut(BaseModel):
    url: str
    expires_in: int
    filename: str


class ExtractedDataOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cv_id: uuid.UUID
    processing_status: CVProcessingStatus
    extracted_name: str | None = None
    extracted_email: str | None = None
    extracted_phone: str | None = None
    skills: list[str] = []
    unrecognized_skills: list[str] = []
    work_experience: list[dict[str, Any]] = []
    education: list[dict[str, Any]] = []
    certifications: list[str] = []
    job_titles: list[str] = []
    years_of_experience: float | None = None
    extraction_metadata: dict[str, Any] = {}
    is_user_confirmed: bool = False
    confirmed_at: datetime | None = None
    text_preview: str | None = None


class ExtractedDataUpdate(BaseModel):
    """User review/correction of extracted data; marks it as confirmed."""

    model_config = ConfigDict(extra="forbid")
    extracted_name: str | None = Field(default=None, max_length=200)
    skills: list[str] | None = None
    job_titles: list[str] | None = None
    years_of_experience: float | None = Field(default=None, ge=0, le=60)
    certifications: list[str] | None = None
    education: list[EducationItem] | None = None
    work_experience: list[ExperienceItem] | None = None

    @field_validator("skills")
    @classmethod
    def _skills(cls, v: list[str] | None) -> list[str] | None:
        return normalize_skills_value(v)

    @field_validator("job_titles", "certifications")
    @classmethod
    def _lists(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _clean_str_list(v, 50, 200)


class ApplyToProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[str] = Field(
        default_factory=lambda: ["skills", "education", "experience", "certifications", "years_of_experience", "current_job_title"],
        description="Profile fields to fill from the CV's extracted data.",
    )
    overwrite: bool = Field(default=False, description="Replace existing profile values instead of only filling empty ones")

    @field_validator("fields")
    @classmethod
    def _valid(cls, v: list[str]) -> list[str]:
        allowed = {"skills", "education", "experience", "certifications", "years_of_experience",
                   "current_job_title", "full_name", "phone"}
        bad = set(v) - allowed
        if bad:
            raise ValueError(f"Unsupported fields: {sorted(bad)}")
        return v
