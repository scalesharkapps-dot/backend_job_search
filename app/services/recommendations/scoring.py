"""Weighted, renormalised compatibility scoring and explanation generation.

Formula (see docs/RECOMMENDATION_SCORING.md):

    match = Σ(w_i · s_i) / Σ(w_i)   over components i whose score s_i is known

Weights come from settings (REC_WEIGHT_*). Components that are not applicable
(missing data) are excluded and the remaining weights are renormalised.
Confidence = (Σ applicable weights / Σ all weights) × quality factor, where the
quality factor is lowered when job skills were inferred from free text rather
than stated explicitly. The result is a compatibility score, not a hiring
probability.

Relevance gate: experience, location and employment type describe *context*;
they must not make an unrelated job look like a good match. ``core`` is the
weighted mean of the applicable relevance components (required skills,
preferred skills, title). If no relevance component is applicable the score is
capped at ``NO_RELEVANCE_CAP``; if ``core < CORE_GATE`` the score is capped at
``core + GATE_MARGIN``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.services.recommendations.matchers import (
    CandidateProfile,
    ComponentResult,
    JobFacts,
    employment_component,
    experience_component,
    location_component,
    skill_component,
    title_component,
)
from app.services.recommendations.requirements import JobRequirements

RELEVANCE_COMPONENTS = ("required_skills", "preferred_skills", "title")
CORE_GATE = 40.0
GATE_MARGIN = 15.0
NO_RELEVANCE_CAP = 40.0


@dataclass
class MatchResult:
    match_percentage: int
    score_confidence: float
    confidence_label: str
    components: dict[str, ComponentResult]
    weights_used: dict[str, float]
    matched_skills: list[str]
    matched_implied_skills: list[str]
    missing_required_skills: list[str]
    missing_preferred_skills: list[str]
    skills_source: str
    relevance_gate: str | None = None
    reasons: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)

    def breakdown(self) -> dict[str, Any]:
        return {
            name: {
                "score": comp.score,
                "label": comp.label,
                "weight": self.weights_used.get(name, 0.0),
                "applicable": comp.score is not None,
                "details": comp.details,
            }
            for name, comp in self.components.items()
        }


def score_components(
    candidate: CandidateProfile, req: JobRequirements, job: JobFacts, weights: dict[str, float]
) -> MatchResult:
    components = {
        "required_skills": skill_component(candidate, req.required_skills, "required"),
        "preferred_skills": skill_component(candidate, req.preferred_skills, "preferred"),
        "experience": experience_component(candidate, req),
        "title": title_component(candidate, job.title),
        "location": location_component(candidate, job),
        "employment": employment_component(candidate, job),
    }
    total_weight = sum(weights.values())
    applicable = {k: weights[k] for k, c in components.items() if c.score is not None and weights.get(k, 0) > 0}
    applicable_weight = sum(applicable.values())
    if applicable_weight > 0:
        raw = sum(components[k].score * w for k, w in applicable.items()) / applicable_weight  # type: ignore[operator]
    else:
        raw = 0.0
    core_weights = {k: w for k, w in applicable.items() if k in RELEVANCE_COMPONENTS}
    gate: str | None = None
    if not core_weights:
        if raw > NO_RELEVANCE_CAP:
            raw, gate = NO_RELEVANCE_CAP, "no_relevance_data"
    else:
        core = sum(components[k].score * w for k, w in core_weights.items()) / sum(core_weights.values())  # type: ignore[operator]
        if core < CORE_GATE and raw > core + GATE_MARGIN:
            raw, gate = core + GATE_MARGIN, "low_relevance"
    quality = 1.0 if req.skills_source == "explicit" else 0.85 if req.skills_source == "inferred" else 0.7
    confidence = round((applicable_weight / total_weight) * quality, 2) if total_weight else 0.0
    label = "high" if confidence >= 0.7 else "medium" if confidence >= 0.45 else "low"

    req_details = components["required_skills"].details
    pref_details = components["preferred_skills"].details
    matched = list(dict.fromkeys(req_details.get("matched", []) + pref_details.get("matched", [])))
    implied = list(dict.fromkeys(req_details.get("matched_implied", []) + pref_details.get("matched_implied", [])))
    result = MatchResult(
        match_percentage=int(round(max(0.0, min(100.0, raw)))),
        score_confidence=confidence,
        confidence_label=label,
        components=components,
        weights_used={k: round(w / applicable_weight * 100, 1) if applicable_weight else 0.0 for k, w in applicable.items()},
        matched_skills=matched,
        matched_implied_skills=implied,
        missing_required_skills=req_details.get("missing", []),
        missing_preferred_skills=pref_details.get("missing", []),
        skills_source=req.skills_source,
        relevance_gate=gate,
    )
    result.reasons, result.concerns = explain(result, candidate, req, job)
    return result


def _join(items: list[str], limit: int = 4) -> str:
    items = items[:limit]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def explain(result: MatchResult, candidate: CandidateProfile, req: JobRequirements, job: JobFacts) -> tuple[list[str], list[str]]:
    """Human-readable reasons built strictly from the computed component results."""
    reasons: list[str] = []
    concerns: list[str] = []
    c = result.components
    source_note = " (inferred from the job description)" if req.skills_source == "inferred" else ""
    if result.matched_skills:
        reasons.append(f"Your skills match the job's requirements{source_note}: {_join(result.matched_skills)}.")
    if result.matched_implied_skills:
        reasons.append(f"Related experience suggests you know {_join(result.matched_implied_skills)} (inferred from your other skills).")
    if result.missing_required_skills:
        concerns.append(f"Required skills not found in your profile: {_join(result.missing_required_skills)}.")
    if result.missing_preferred_skills:
        concerns.append(f"Nice-to-have skills you could add: {_join(result.missing_preferred_skills)}.")

    exp = c["experience"]
    if exp.label == "meets":
        reasons.append(f"Your experience ({candidate.years_of_experience:g} years) meets the requirement of {req.min_years:g}+ years.")
    elif exp.label == "overqualified":
        concerns.append("You may be more experienced than this role targets.")
    elif exp.label in {"slightly_below", "below"}:
        concerns.append(f"The job asks for {req.min_years:g}+ years; your profile shows {candidate.years_of_experience:g}.")
    elif exp.label == "unknown":
        concerns.append("Add your years of experience to improve matching accuracy.")

    title = c["title"]
    if title.label == "highly_relevant":
        reasons.append(f"The role closely matches your background as {title.details.get('closest_title')}.")
    elif title.label == "related":
        reasons.append(f"The role is related to your background as {title.details.get('closest_title')}.")

    loc = c["location"]
    if loc.label == "compatible":
        reasons.append("This is a remote role." if job.is_remote else "The job location matches your preferences.")
    elif loc.label == "same_country":
        reasons.append("The job is in your preferred country.")
    elif loc.label in {"different_location", "onsite_but_remote_preferred"}:
        concerns.append("The job location does not match your location preferences.")
    elif loc.label == "remote_other_country":
        reasons.append("This is a remote role based in another country; check eligibility requirements.")

    emp = c["employment"]
    if emp.label == "compatible":
        reasons.append("The employment type matches your preference.")
    elif emp.label == "mismatch":
        concerns.append("The employment type differs from your preference.")
    if result.relevance_gate == "low_relevance":
        concerns.append("There is little overlap between your skills or job titles and this role.")
    elif result.relevance_gate == "no_relevance_data":
        concerns.append("Skills and title could not be compared for this job, so the score was limited.")
    if result.confidence_label == "low":
        concerns.append("Limited information was available, so this score is less certain.")
    return reasons, concerns
