"""Personalised, explainable job recommendations."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DBSession
from app.core.config import get_settings
from app.core.rate_limit import enforce_user_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok, paginate
from app.models.recommendation import RemotePreference, UserRecommendationPreferences
from app.schemas.job import job_summary
from app.schemas.recommendation import (
    AnalyzeRequest,
    HistoryItem,
    PreferencesIn,
    PreferencesOut,
    RecommendationPage,
    SingleMatchOut,
)
from app.services.plan_access import require_remote_access
from app.services.recommendations import engine
from app.services.recommendations.matchers import CandidateProfile

router = APIRouter(prefix="/recommendations", tags=["Recommendations"], responses=ERROR_RESPONSES)


def _candidate_summary(c: CandidateProfile) -> dict[str, Any]:
    return {
        "cv_id": c.cv_id,
        "skills_considered": len([s for s, src in c.skills.items() if src != "implied"]),
        "data_sources": c.data_sources,
        "years_of_experience": c.years_of_experience,
        "missing_information": engine.missing_information(c),
    }


async def _page(db, user, *, cv_id, page, page_size, min_score, remote_only) -> dict[str, Any]:
    enforce_user_limit("recommendations", user.id, get_settings().RATE_LIMIT_RECOMMENDATION_PER_MINUTE)
    start = (page - 1) * page_size
    candidate, scored = await engine.recommend(
        db, user, cv_id=cv_id, min_score=min_score, remote_only=remote_only, persist_range=(start, start + page_size)
    )
    items = [
        {"job": job_summary(s.job), "match": engine.match_payload(s.job, s.result)}
        for s in scored[start : start + page_size]
    ]
    data = paginate(items, page, page_size, len(scored))
    data.update(candidate=_candidate_summary(candidate), algorithm_version=engine.ALGORITHM_VERSION)
    return data


@router.get("/for-me", response_model=ApiResponse[RecommendationPage], summary="Personalised recommendations")
async def for_me(
    user: CurrentUser,
    db: DBSession,
    cv_id: Annotated[uuid.UUID | None, Query(description="Defaults to your primary CV")] = None,
    page: Annotated[int, Query(ge=1, le=100)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
    min_score: Annotated[int | None, Query(ge=0, le=100)] = None,
    remote_only: bool = False,
) -> dict[str, Any]:
    """Scores stored jobs (recent JSearch listings and published SharkHire jobs)
    against your profile and CV. Run job searches to widen the pool.

    `match_percentage` is a compatibility score, not a probability of being hired.
    Returns 422 `insufficient_profile` when neither skills nor job titles are known."""
    data = await _page(db, user, cv_id=cv_id, page=page, page_size=page_size, min_score=min_score, remote_only=remote_only)
    msg = "Recommendations generated" if data["items"] else "No matching jobs yet. Try searching for jobs or adjusting preferences."
    return ok(data, msg)


@router.post("/analyze", response_model=ApiResponse[RecommendationPage], summary="Analyse a specific CV against jobs")
async def analyze(body: AnalyzeRequest, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    data = await _page(db, user, cv_id=body.cv_id, page=body.page, page_size=body.page_size,
                       min_score=body.min_score, remote_only=body.remote_only)
    return ok(data, "Analysis complete")


@router.get("/jobs/{job_id}/match", response_model=ApiResponse[SingleMatchOut], summary="Match one job against my CV")
async def match_job(
    job_id: uuid.UUID,
    user: CurrentUser,
    db: DBSession,
    cv_id: Annotated[uuid.UUID | None, Query()] = None,
) -> dict[str, Any]:
    enforce_user_limit("recommendations", user.id, get_settings().RATE_LIMIT_RECOMMENDATION_PER_MINUTE)
    candidate, job, result = await engine.match_single(db, user, job_id, cv_id)
    return ok({"job": job_summary(job), "match": engine.match_payload(job, result), "candidate": _candidate_summary(candidate)})


@router.get("/preferences", response_model=ApiResponse[PreferencesOut], summary="Get recommendation preferences")
async def get_preferences(user: CurrentUser, db: DBSession) -> dict[str, Any]:
    prefs = await engine.get_preferences(db, user)
    return ok(PreferencesOut.model_validate(prefs) if prefs else PreferencesOut())


@router.put("/preferences", response_model=ApiResponse[PreferencesOut], summary="Replace recommendation preferences")
async def put_preferences(body: PreferencesIn, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    require_remote_access(body.remote_preference == RemotePreference.REMOTE_ONLY)
    prefs = await engine.get_preferences(db, user)
    values = body.model_dump(mode="json")
    if prefs is None:
        prefs = UserRecommendationPreferences(user_id=user.id, **{**values, "remote_preference": body.remote_preference,
                                                                  "experience_level": body.experience_level})
        db.add(prefs)
    else:
        for key in values:
            setattr(prefs, key, getattr(body, key) if key in {"remote_preference", "experience_level"} else values[key])
    await db.commit()
    await db.refresh(prefs)
    return ok(PreferencesOut.model_validate(prefs), "Preferences saved")


@router.get("/history", response_model=ApiResponse[dict[str, Any]], summary="Previously generated recommendations")
async def get_history(
    user: CurrentUser,
    db: DBSession,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> dict[str, Any]:
    """Latest stored result per job (recomputed results replace older ones)."""
    rows, total = await engine.history(db, user, page, page_size)
    items = [
        HistoryItem(
            id=r.id, job=job_summary(job) if job else None, cv_id=r.cv_id, match_percentage=r.match_percentage,
            score_confidence=r.score_confidence, matched_skills=r.matched_skills, missing_skills=r.missing_skills,
            explanation=r.explanation, algorithm_version=r.algorithm_version, generated_at=r.generated_at,
        ).model_dump(mode="json")
        for r, job in rows
    ]
    return ok(paginate(items, page, page_size, total))
