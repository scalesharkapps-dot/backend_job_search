"""Recommendation API schemas."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.recommendation import ExperienceLevel, RemotePreference
from app.schemas.job import JobSummaryOut
from app.schemas.profile import EmploymentType, _clean_str_list


class MatchOut(BaseModel):
    job_id: uuid.UUID
    match_percentage: int
    score_confidence: float
    confidence_label: str
    matched_skills: list[str]
    matched_inferred_skills: list[str]
    missing_required_skills: list[str]
    missing_preferred_skills: list[str]
    job_skills_source: str
    experience_match: str
    title_match: str
    location_match: str
    employment_match: str
    score_breakdown: dict[str, Any]
    relevance_gate: str | None = None
    reasons: list[str]
    concerns: list[str]
    disclaimer: str


class RecommendationItem(BaseModel):
    job: JobSummaryOut
    match: MatchOut


class CandidateSummary(BaseModel):
    cv_id: uuid.UUID | None
    skills_considered: int
    data_sources: list[str]
    years_of_experience: float | None
    missing_information: list[str]


class RecommendationPage(BaseModel):
    items: list[RecommendationItem]
    pagination: dict[str, Any]
    candidate: CandidateSummary
    algorithm_version: str


class SingleMatchOut(BaseModel):
    job: JobSummaryOut
    match: MatchOut
    candidate: CandidateSummary


class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cv_id: uuid.UUID
    page: int = Field(default=1, ge=1, le=100)
    page_size: int = Field(default=20, ge=1, le=50)
    min_score: int | None = Field(default=None, ge=0, le=100)
    remote_only: bool = False


class PreferencesIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preferred_job_titles: list[str] = Field(default_factory=list)
    preferred_locations: list[str] = Field(default_factory=list)
    preferred_countries: list[str] = Field(default_factory=list, description="ISO-2 codes")
    employment_types: list[EmploymentType] = Field(default_factory=list)
    remote_preference: RemotePreference = RemotePreference.ANY
    experience_level: ExperienceLevel | None = None
    min_match_score: int | None = Field(default=None, ge=0, le=100)

    @field_validator("preferred_job_titles", "preferred_locations")
    @classmethod
    def _lists(cls, v: list[str]) -> list[str]:
        return _clean_str_list(v, 20, 100)

    @field_validator("preferred_countries")
    @classmethod
    def _countries(cls, v: list[str]) -> list[str]:
        out = []
        for c in v:
            c = c.strip().lower()
            if not re.fullmatch(r"[a-z]{2}", c):
                raise ValueError("countries must be 2-letter ISO codes")
            if c not in out:
                out.append(c)
        if len(out) > 20:
            raise ValueError("at most 20 countries")
        return out


class PreferencesOut(PreferencesIn):
    model_config = ConfigDict(from_attributes=True, extra="ignore")
    updated_at: datetime | None = None


class HistoryItem(BaseModel):
    id: uuid.UUID
    job: JobSummaryOut | None
    cv_id: uuid.UUID | None
    match_percentage: int
    score_confidence: float
    matched_skills: list[str]
    missing_skills: list[str]
    explanation: dict[str, Any]
    algorithm_version: str
    generated_at: datetime
