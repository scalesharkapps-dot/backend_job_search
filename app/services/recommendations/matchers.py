"""Independent, deterministic matching components.

Each matcher returns a :class:`ComponentResult` whose ``score`` is 0-100, or
``None`` when the component is NOT APPLICABLE because required data is missing on
either side. Missing data is never silently converted into 0 or 100.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.services.recommendations.requirements import LEVEL_ORDER, JobRequirements, detect_level


@dataclass
class CandidateProfile:
    """Everything the engine knows about the candidate (all sourced from stored data)."""

    skills: dict[str, str] = field(default_factory=dict)  # canonical skill -> confirmed|extracted|implied
    titles: list[str] = field(default_factory=list)
    preferred_titles: list[str] = field(default_factory=list)
    years_of_experience: float | None = None
    years_source: str | None = None
    city: str | None = None
    country: str | None = None
    preferred_locations: list[str] = field(default_factory=list)
    preferred_countries: list[str] = field(default_factory=list)
    remote_preference: str = "any"  # any | remote_only | hybrid | onsite
    employment_types: list[str] = field(default_factory=list)
    experience_level: str | None = None
    cv_id: Any = None
    data_sources: list[str] = field(default_factory=list)


@dataclass
class JobFacts:
    title: str
    city: str | None
    country: str | None
    location: str | None
    is_remote: bool
    employment_types: list[str]


@dataclass
class ComponentResult:
    score: float | None
    label: str
    details: dict[str, Any] = field(default_factory=dict)


# ---------------- Skills ----------------
def match_skills(candidate: CandidateProfile, skills: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Return (matched_stated, matched_via_implication, missing)."""
    matched, implied, missing = [], [], []
    for skill in skills:
        source = candidate.skills.get(skill)
        if source is None:
            missing.append(skill)
        elif source == "implied":
            implied.append(skill)
        else:
            matched.append(skill)
    return matched, implied, missing


def skill_component(candidate: CandidateProfile, skills: list[str], kind: str) -> ComponentResult:
    if not skills:
        return ComponentResult(None, "not_applicable", {"reason": f"The job lists no {kind} skills."})
    if not candidate.skills:
        return ComponentResult(None, "unknown", {"reason": "Your profile and CV have no skills."})
    matched, implied, missing = match_skills(candidate, skills)
    # Implied skills (e.g. Python inferred from FastAPI) earn 75% credit.
    score = (len(matched) + 0.75 * len(implied)) / len(skills) * 100
    label = "strong" if score >= 75 else "partial" if score >= 40 else "weak"
    return ComponentResult(
        round(score, 1), label,
        {"matched": matched, "matched_implied": implied, "missing": missing, "total": len(skills)},
    )


# ---------------- Experience ----------------
def experience_component(candidate: CandidateProfile, req: JobRequirements) -> ComponentResult:
    if req.min_years is None:
        return ComponentResult(None, "not_specified", {"reason": "The job does not state an experience requirement."})
    years = candidate.years_of_experience
    if years is None:
        return ComponentResult(None, "unknown", {"reason": "Your years of experience are unknown.", "required_min_years": req.min_years})
    details: dict[str, Any] = {
        "candidate_years": years, "required_min_years": req.min_years,
        "required_max_years": req.max_years, "requirement_source": req.experience_source, "job_level": req.level,
    }
    if years >= req.min_years:
        over_limit = (req.max_years + 3) if req.max_years is not None else (
            8.0 if req.level in {"internship", "entry", "junior"} else None
        )
        if over_limit is not None and years > over_limit:
            return ComponentResult(75.0, "overqualified", details)
        return ComponentResult(100.0, "meets", details)
    if req.min_years <= 0:
        return ComponentResult(100.0, "meets", details)
    ratio = years / req.min_years
    score = 85.0 if ratio >= 0.8 else 65.0 if ratio >= 0.6 else 40.0 if ratio >= 0.4 else 15.0
    return ComponentResult(score, "slightly_below" if ratio >= 0.6 else "below", details)


# ---------------- Title ----------------
_GENERIC = {"engineer", "developer", "specialist", "officer", "associate", "assistant", "consultant",
            "executive", "manager", "lead", "senior", "junior", "sr", "jr", "intern", "staff", "principal",
            "head", "i", "ii", "iii", "iv", "of", "and", "the", "for", "in", "at", "remote", "mid", "level",
            "entry", "trainee", "team", "-", "&", "full", "time"}
_SYNONYMS = {
    "programmer": "developer", "dev": "developer", "swe": "software", "sde": "software",
    "ml": "machine-learning", "ai": "artificial-intelligence", "back": "backend", "back-end": "backend",
    "front": "frontend", "front-end": "frontend", "fullstack": "full-stack", "js": "javascript",
    "analytics": "analyst", "scientist": "science", "golang": "go", "ios": "mobile", "android": "mobile",
    "postgres": "database", "dba": "database",
}
_PHRASES = {"machine learning": "machine-learning", "artificial intelligence": "artificial-intelligence",
            "full stack": "full-stack", "data science": "data science", "deep learning": "machine-learning",
            "back end": "backend", "front end": "frontend", "quality assurance": "qa"}
# Domain families: tokens in the same family are related (partial credit).
_FAMILIES = [
    {"backend", "python", "django", "api", "server", "java", "go", "node", "php", "software"},
    {"frontend", "react", "javascript", "web", "ui", "angular", "vue"},
    {"full-stack", "software", "web"},
    {"data", "analyst", "science", "bi", "sql", "reporting"},
    {"machine-learning", "artificial-intelligence", "nlp", "vision", "data", "science"},
    {"devops", "cloud", "sre", "infrastructure", "platform", "aws", "kubernetes"},
    {"mobile", "flutter", "kotlin", "swift"},
    {"qa", "test", "testing", "automation", "quality"},
    {"security", "cybersecurity", "soc", "penetration"},
    {"marketing", "seo", "content", "social", "digital"},
    {"sales", "business", "account"},
    {"accountant", "accounting", "finance", "financial", "audit"},
    {"hr", "recruiter", "talent", "human"},
    {"design", "designer", "ux", "graphic"},
]


def title_tokens(title: str) -> set[str]:
    text = (title or "").lower()
    for phrase, token in _PHRASES.items():
        text = text.replace(phrase, token)
    tokens = set()
    for raw in re.findall(r"[a-z0-9+#.\-]+", text):
        raw = raw.strip(".-")
        token = _SYNONYMS.get(raw, raw)
        if token and token not in _GENERIC:
            tokens.add(token)
    return tokens


def title_similarity(a: str, b: str) -> float:
    ta, tb = title_tokens(a), title_tokens(b)
    if not ta or not tb:
        return 0.0
    exact = len(ta & tb) / len(ta | tb)
    related = 0.0
    for fam in _FAMILIES:
        if ta & fam and tb & fam:
            related = max(related, 0.5)
    return round(min(1.0, max(exact * 1.0 + (0.25 if exact else 0), related)), 3)


def title_component(candidate: CandidateProfile, job_title: str) -> ComponentResult:
    pool = [t for t in candidate.preferred_titles + candidate.titles if t]
    if not pool:
        return ComponentResult(None, "unknown", {"reason": "No current, past or preferred job titles are known."})
    best_title, best = max(((t, title_similarity(t, job_title)) for t in pool), key=lambda p: p[1])
    score = round(best * 100, 1)
    label = "highly_relevant" if score >= 75 else "related" if score >= 40 else "unrelated"
    return ComponentResult(score, label, {"closest_title": best_title if best > 0 else None})


# ---------------- Location ----------------
def _norm(value: str | None) -> str:
    return (value or "").strip().lower()


def location_component(candidate: CandidateProfile, job: JobFacts) -> ComponentResult:
    pref = candidate.remote_preference
    prefs_locations = [_norm(p) for p in candidate.preferred_locations if p]
    countries = {c.lower() for c in candidate.preferred_countries if c} | ({candidate.country.lower()} if candidate.country else set())
    job_country = _norm(job.country)
    if job.is_remote:
        details = {"job_is_remote": True, "note": "Remote listings may still restrict eligible countries; check the description."}
        if pref == "onsite":
            return ComponentResult(50.0, "remote_but_onsite_preferred", details)
        if countries and job_country and job_country not in countries:
            return ComponentResult(85.0, "remote_other_country", details)
        return ComponentResult(100.0, "compatible", details)

    if pref == "remote_only":
        return ComponentResult(10.0, "onsite_but_remote_preferred", {"job_is_remote": False})
    if not prefs_locations and not countries and not candidate.city:
        return ComponentResult(None, "unknown", {"reason": "Set your city, country or preferred locations."})
    job_places = {_norm(job.city), _norm(job.location)} - {""}
    cities = set(prefs_locations) | ({_norm(candidate.city)} if candidate.city else set())
    if any(c and any(c in place for place in job_places) for c in cities):
        return ComponentResult(100.0, "compatible", {"job_location": job.location})
    if job_country and job_country in countries:
        return ComponentResult(70.0, "same_country", {"job_location": job.location})
    if not job_country and not job_places:
        return ComponentResult(None, "unknown", {"reason": "The job location is not specified."})
    return ComponentResult(15.0, "different_location", {"job_location": job.location})


# ---------------- Employment type ----------------
def employment_component(candidate: CandidateProfile, job: JobFacts) -> ComponentResult:
    if not candidate.employment_types:
        return ComponentResult(None, "no_preference", {})
    if not job.employment_types:
        return ComponentResult(None, "unknown", {"reason": "The job does not state its employment type."})
    overlap = set(candidate.employment_types) & set(job.employment_types)
    if overlap:
        return ComponentResult(100.0, "compatible", {"matching_types": sorted(overlap)})
    return ComponentResult(0.0, "mismatch", {"job_types": job.employment_types})


def experience_level_allowed(preferred_level: str | None, job_level: str | None) -> bool:
    """Filter: hide jobs more than one level away from the user's chosen level."""
    if not preferred_level or not job_level or preferred_level not in LEVEL_ORDER or job_level not in LEVEL_ORDER:
        return True
    return abs(LEVEL_ORDER.index(preferred_level) - LEVEL_ORDER.index(job_level)) <= 2


__all__ = [
    "CandidateProfile", "ComponentResult", "JobFacts", "detect_level", "employment_component",
    "experience_component", "experience_level_allowed", "location_component", "skill_component",
    "title_component", "title_similarity",
]
