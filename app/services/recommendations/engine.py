"""Recommendation engine: builds the candidate profile, selects candidate jobs,
scores them deterministically and persists the latest results.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import ValidationAppError
from app.models.base import utcnow
from app.models.job import Job, JobSource
from app.models.profile import CVProcessingStatus, UserCV, UserProfile
from app.models.recommendation import (
    JobRecommendation,
    RemotePreference,
    UserRecommendationPreferences,
)
from app.models.user import User
from app.services import visibility
from app.services.cv import service as cv_service
from app.services.recommendations.matchers import CandidateProfile, JobFacts, experience_level_allowed
from app.services.recommendations.requirements import DERIVATION_VERSION, JobRequirements, derive_requirements
from app.services.recommendations.scoring import MatchResult, score_components
from app.services.skills import expand_implied, normalize_skill_list

logger = logging.getLogger(__name__)

ALGORITHM_VERSION = f"weighted-1.{DERIVATION_VERSION}"


@dataclass
class ScoredJob:
    job: Job
    result: MatchResult
    rank_score: float


# ---------------- Candidate ----------------
async def get_preferences(db: AsyncSession, user: User) -> UserRecommendationPreferences | None:
    return await db.scalar(
        select(UserRecommendationPreferences).where(UserRecommendationPreferences.user_id == user.id)
    )


async def resolve_cv(db: AsyncSession, user: User, cv_id: uuid.UUID | None) -> UserCV | None:
    """Explicit CV (ownership-checked, must be processed) or the primary CV."""
    if cv_id is not None:
        cv = await cv_service.get_user_cv(db, user.id, cv_id, with_data=True)
        if cv.processing_status != CVProcessingStatus.COMPLETED or cv.extracted is None:
            raise ValidationAppError(
                "The selected CV has not been processed successfully.",
                code="cv_not_processed",
                details={"processing_status": cv.processing_status.value},
            )
        return cv
    cv = await cv_service.get_primary_cv(db, user.id)
    if cv is not None and cv.processing_status == CVProcessingStatus.COMPLETED and cv.extracted is not None:
        return cv
    return None


async def build_candidate(db: AsyncSession, user: User, cv_id: uuid.UUID | None = None) -> CandidateProfile:
    """Combine profile (user-confirmed) data with CV data, confirmed data first."""
    profile = await db.scalar(select(UserProfile).where(UserProfile.user_id == user.id))
    prefs = await get_preferences(db, user)
    cv = await resolve_cv(db, user, cv_id)
    data = cv.extracted if cv else None
    candidate = CandidateProfile(cv_id=cv.id if cv else None)

    skills: dict[str, str] = {}
    if profile and profile.skills:
        known, _ = normalize_skill_list(profile.skills)
        skills.update({s: "confirmed" for s in known})
        candidate.data_sources.append("profile")
    if data is not None:
        cv_known, _ = normalize_skill_list(data.skills)
        tag = "confirmed" if data.is_user_confirmed else "extracted"
        for skill in cv_known:
            skills.setdefault(skill, tag)
        candidate.data_sources.append("cv_confirmed" if data.is_user_confirmed else "cv_extracted")
    for skill, origin in expand_implied(list(skills)).items():
        if origin == "implied":
            skills.setdefault(skill, "implied")
    candidate.skills = skills

    titles: list[str] = []
    if profile:
        if profile.current_job_title:
            titles.append(profile.current_job_title)
        titles += [e.get("title") for e in profile.experience or [] if isinstance(e, dict) and e.get("title")]
        if profile.headline:
            titles.append(profile.headline)
    if data is not None:
        titles += [t for t in data.job_titles if t]
    candidate.titles = list(dict.fromkeys(titles))[:20]

    if profile and profile.years_of_experience is not None:
        candidate.years_of_experience, candidate.years_source = profile.years_of_experience, "profile"
    elif data is not None and data.years_of_experience is not None:
        candidate.years_of_experience = data.years_of_experience
        candidate.years_source = "cv_confirmed" if data.is_user_confirmed else "cv_extracted"

    if profile:
        candidate.city, candidate.country = profile.city, profile.country
    candidate.preferred_titles = list((prefs.preferred_job_titles if prefs else None) or (profile.preferred_job_titles if profile else []) or [])
    candidate.preferred_locations = list((prefs.preferred_locations if prefs else None) or (profile.preferred_locations if profile else []) or [])
    candidate.preferred_countries = [c.lower() for c in ((prefs.preferred_countries if prefs else None) or [])]
    candidate.employment_types = list((prefs.employment_types if prefs else None) or (profile.preferred_employment_types if profile else []) or [])
    candidate.remote_preference = prefs.remote_preference.value if prefs else RemotePreference.ANY.value
    candidate.experience_level = prefs.experience_level.value if prefs and prefs.experience_level else None
    return candidate


def missing_information(candidate: CandidateProfile) -> list[str]:
    missing = []
    if not candidate.skills:
        missing.append("skills (upload a CV or add skills to your profile)")
    if not candidate.titles and not candidate.preferred_titles:
        missing.append("job titles (current title, experience or preferred job titles)")
    return missing


def require_sufficient(candidate: CandidateProfile) -> None:
    missing = missing_information(candidate)
    if len(missing) == 2:  # neither skills nor titles: nothing meaningful to compare
        raise ValidationAppError(
            "Not enough profile information to generate recommendations.",
            code="insufficient_profile",
            details={"missing": missing},
        )


# ---------------- Jobs ----------------
def job_requirements(job: Job) -> tuple[JobRequirements, bool]:
    """Return cached requirements or derive them. Second value: whether it changed."""
    cached = job.derived_requirements
    if cached and cached.get("version") == DERIVATION_VERSION:
        return JobRequirements.from_dict(cached), False
    req = derive_requirements(
        title=job.title,
        description=job.description,
        highlights=job.highlights,
        explicit_required=list(job.required_skills or []),
        explicit_preferred=list(job.preferred_skills or []),
        required_experience_months=job.required_experience_months,
        min_years=job.min_experience_years,
        max_years=job.max_experience_years,
        no_experience_required=job.no_experience_required,
        level=job.experience_level,
    )
    job.derived_requirements = req.to_dict()
    return req, True


def job_facts(job: Job) -> JobFacts:
    return JobFacts(
        title=job.title,
        city=job.city,
        country=job.country,
        location=job.location,
        is_remote=job.is_remote,
        employment_types=list(job.employment_types or ([job.employment_type] if job.employment_type else [])),
    )


def recommendable_filter() -> Any:
    """Jobs eligible for recommendation: recently seen external listings that are
    not known to be gone, plus publicly visible SharkHire listings."""
    settings = get_settings()
    cutoff = utcnow() - timedelta(days=settings.REC_JOB_MAX_AGE_DAYS)
    external = and_(Job.source == JobSource.JSEARCH, Job.unavailable_since.is_(None), Job.last_seen_at >= cutoff)
    return or_(external, visibility.internal_visible_filter())


async def candidate_jobs(db: AsyncSession, *, remote_only: bool = False) -> list[Job]:
    settings = get_settings()
    stmt = select(Job).where(recommendable_filter())
    if remote_only:
        stmt = stmt.where(Job.is_remote.is_(True))
    stmt = stmt.order_by(Job.posted_at.desc().nulls_last(), Job.id).limit(settings.REC_MAX_CANDIDATE_JOBS)
    return list(await db.scalars(stmt))


def freshness_bonus(job: Job) -> float:
    """Ranking-only tie-breaker (0-3 points); never added to the displayed match score."""
    if not job.posted_at:
        return 0.0
    age_days = (utcnow() - job.posted_at).days
    return 3.0 if age_days <= 3 else 2.0 if age_days <= 7 else 1.0 if age_days <= 30 else 0.0


def score_job(candidate: CandidateProfile, job: Job) -> tuple[MatchResult, bool]:
    req, changed = job_requirements(job)
    return score_components(candidate, req, job_facts(job), get_settings().recommendation_weights), changed


def match_payload(job: Job, result: MatchResult) -> dict[str, Any]:
    return {
        "job_id": job.id,
        "match_percentage": result.match_percentage,
        "score_confidence": result.score_confidence,
        "confidence_label": result.confidence_label,
        "matched_skills": result.matched_skills,
        "matched_inferred_skills": result.matched_implied_skills,
        "missing_required_skills": result.missing_required_skills,
        "missing_preferred_skills": result.missing_preferred_skills,
        "job_skills_source": result.skills_source,
        "experience_match": result.components["experience"].label,
        "title_match": result.components["title"].label,
        "location_match": result.components["location"].label,
        "employment_match": result.components["employment"].label,
        "score_breakdown": result.breakdown(),
        "relevance_gate": result.relevance_gate,
        "reasons": result.reasons,
        "concerns": result.concerns,
        "disclaimer": "Compatibility score based on your profile data; not a prediction of hiring outcome.",
    }


async def persist(db: AsyncSession, user: User, candidate: CandidateProfile, scored: list[ScoredJob]) -> None:
    """Atomic bulk upsert keyed on (user_id, job_id): safe under concurrent requests."""
    if not scored:
        return
    now = utcnow()
    rows = []
    for item in scored:
        r = item.result
        rows.append({
            "id": uuid.uuid4(),
            "user_id": user.id,
            "job_id": item.job.id,
            "cv_id": candidate.cv_id,
            "match_percentage": r.match_percentage,
            "score_confidence": r.score_confidence,
            "score_breakdown": r.breakdown(),
            "matched_skills": r.matched_skills,
            "missing_skills": r.missing_required_skills,
            "explanation": {"reasons": r.reasons, "concerns": r.concerns, "missing_preferred_skills": r.missing_preferred_skills},
            "algorithm_version": ALGORITHM_VERSION,
            "generated_at": now,
        })
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as dialect_insert
    else:
        from sqlalchemy.dialects.sqlite import insert as dialect_insert  # type: ignore[assignment]
    stmt = dialect_insert(JobRecommendation).values(rows)
    updatable = [c for c in rows[0] if c not in {"id", "user_id", "job_id"}]
    stmt = stmt.on_conflict_do_update(
        index_elements=["user_id", "job_id"], set_={c: getattr(stmt.excluded, c) for c in updatable}
    )
    await db.execute(stmt)


async def recommend(
    db: AsyncSession,
    user: User,
    *,
    cv_id: uuid.UUID | None = None,
    min_score: int | None = None,
    remote_only: bool = False,
    persist_range: tuple[int, int] | None = None,
) -> tuple[CandidateProfile, list[ScoredJob]]:
    """Score all eligible jobs. Only ``scored[start:end]`` (the page shown to the
    user) is persisted to the recommendation history."""
    settings = get_settings()
    candidate = await build_candidate(db, user, cv_id)
    require_sufficient(candidate)
    prefs = await get_preferences(db, user)
    threshold = min_score if min_score is not None else (prefs.min_match_score if prefs and prefs.min_match_score is not None else settings.REC_MIN_SCORE)
    remote_only = remote_only or candidate.remote_preference == RemotePreference.REMOTE_ONLY.value

    scored: list[ScoredJob] = []
    derived_changed = False
    for job in await candidate_jobs(db, remote_only=remote_only):
        result, changed = score_job(candidate, job)
        derived_changed |= changed
        level = (job.derived_requirements or {}).get("level")
        if not experience_level_allowed(candidate.experience_level, level):
            continue
        if result.match_percentage < threshold:
            continue
        scored.append(ScoredJob(job, result, result.match_percentage + freshness_bonus(job)))

    # Deterministic ordering: rank score, confidence, recency, id.
    scored.sort(key=lambda s: (-s.rank_score, -s.result.score_confidence,
                               -(s.job.posted_at.timestamp() if s.job.posted_at else 0), str(s.job.id)))
    if persist_range is not None:
        start, end = persist_range
        await persist(db, user, candidate, scored[start:end][: settings.REC_HISTORY_LIMIT])
    if persist_range is not None or derived_changed:
        await db.commit()
    return candidate, scored


async def match_single(db: AsyncSession, user: User, job_id: uuid.UUID, cv_id: uuid.UUID | None) -> tuple[CandidateProfile, Job, MatchResult]:
    job = await visibility.get_visible_job(db, job_id)
    candidate = await build_candidate(db, user, cv_id)
    require_sufficient(candidate)
    result, _ = score_job(candidate, job)
    await persist(db, user, candidate, [ScoredJob(job, result, result.match_percentage)])
    await db.commit()
    return candidate, job, result


async def history(
    db: AsyncSession, user: User, page: int, page_size: int
) -> tuple[list[tuple[JobRecommendation, Job | None]], int]:
    total = await db.scalar(
        select(func.count()).select_from(JobRecommendation).where(JobRecommendation.user_id == user.id)
    ) or 0
    recs = list(
        await db.scalars(
            select(JobRecommendation)
            .where(JobRecommendation.user_id == user.id)
            .order_by(JobRecommendation.generated_at.desc(), JobRecommendation.match_percentage.desc(), JobRecommendation.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    jobs: dict[uuid.UUID, Job] = {}
    if recs:
        jobs = {j.id: j for j in await db.scalars(select(Job).where(Job.id.in_([r.job_id for r in recs])))}
    return [(r, jobs.get(r.job_id)) for r in recs], total


__all__ = [
    "ALGORITHM_VERSION",
    "build_candidate",
    "get_preferences",
    "history",
    "match_payload",
    "match_single",
    "missing_information",
    "recommend",
]
