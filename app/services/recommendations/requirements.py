"""Derive comparable requirements from a job listing.

Explicit data (provider ``job_required_skills``, provider experience months,
employer-entered required/preferred skills) is always preferred. Otherwise
requirements are *inferred* from the description and highlights and labelled so:
a sentence containing a preference cue ("nice to have", "a plus", "preferred",
"bonus") yields preferred skills; other skill mentions are treated as required.
Nothing is invented when the listing says nothing.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app.services.skills import extract_skills_from_text, normalize_skill_list

# Bump when the derivation logic changes so cached results are recomputed.
DERIVATION_VERSION = 1

PREFERRED_CUES = re.compile(
    r"\b(nice[- ]to[- ]have|a plus|is a plus|are a plus|plus\b|preferred|preferably|bonus|desirable|"
    r"advantageous|would be great|good to have|optional|familiarity with)\b",
    re.I,
)
YEARS_RE = re.compile(
    r"(?:(?:minimum|at least|min\.?)\s+(?:of\s+)?)?(\d{1,2})\s*(?:\+|\s*-\s*(\d{1,2}))?\s*(?:years?|yrs?)"
    r"(?:\s+of)?(?:\s+\w+){0,4}?\s+experience",
    re.I,
)
LEVEL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("internship", re.compile(r"\b(intern|internship|trainee)\b", re.I)),
    ("lead", re.compile(r"\b(lead|principal|staff|head of|architect)\b", re.I)),
    ("manager", re.compile(r"\b(manager|director)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|snr)\b", re.I)),
    ("junior", re.compile(r"\b(junior|jr\.?)\b", re.I)),
    ("entry", re.compile(r"\b(entry[- ]level|graduate|fresh(?:er)?|new grad)\b", re.I)),
    ("mid", re.compile(r"\b(mid[- ]level|intermediate)\b", re.I)),
]
# Typical minimum years per level (used only when no explicit requirement exists).
LEVEL_MIN_YEARS = {"internship": 0, "entry": 0, "junior": 0, "mid": 2, "senior": 5, "lead": 7, "manager": 5}
LEVEL_ORDER = ["internship", "entry", "junior", "mid", "senior", "lead", "manager"]


@dataclass
class JobRequirements:
    required_skills: list[str] = field(default_factory=list)
    preferred_skills: list[str] = field(default_factory=list)
    skills_source: str = "none"  # explicit | inferred | none
    min_years: float | None = None
    max_years: float | None = None
    experience_source: str = "none"  # explicit | stated_in_text | title_level | none
    level: str | None = None
    no_experience_required: bool | None = None
    version: int = DERIVATION_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobRequirements:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def detect_level(title: str) -> str | None:
    for level, pattern in LEVEL_PATTERNS:
        if pattern.search(title or ""):
            return level
    return None


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+|\n+|•", text or "") if s.strip()]


def infer_skills_from_text(text: str) -> tuple[list[str], list[str]]:
    required: dict[str, None] = {}
    preferred: dict[str, None] = {}
    for sentence in _sentences(text):
        skills = extract_skills_from_text(sentence)
        if not skills:
            continue
        target = preferred if PREFERRED_CUES.search(sentence) else required
        for skill in skills:
            target.setdefault(skill, None)
    # A skill explicitly required anywhere is required, not preferred.
    preferred = {s: None for s in preferred if s not in required}
    return list(required), list(preferred)


def years_from_text(text: str) -> tuple[float | None, float | None]:
    """Smallest stated minimum (and range maximum) of years of experience."""
    best: tuple[float, float | None] | None = None
    for match in YEARS_RE.finditer(text or ""):
        low = float(match.group(1))
        high = float(match.group(2)) if match.group(2) else None
        if low > 30 or (high is not None and high < low):
            continue
        if best is None or low < best[0]:
            best = (low, high)
    return (best[0], best[1]) if best else (None, None)


def derive_requirements(
    *,
    title: str,
    description: str | None,
    highlights: dict[str, list[str]] | None,
    explicit_required: list[str] | None = None,
    explicit_preferred: list[str] | None = None,
    required_experience_months: int | None = None,
    min_years: float | None = None,
    max_years: float | None = None,
    no_experience_required: bool | None = None,
    level: str | None = None,
) -> JobRequirements:
    req = JobRequirements(no_experience_required=no_experience_required)
    highlights = highlights or {}
    qualification_text = "\n".join(
        line for key, lines in highlights.items() if key.lower().startswith("qualif") for line in lines
    )
    full_text = "\n".join(filter(None, [description, qualification_text, "\n".join(
        line for key, lines in highlights.items() if key.lower().startswith("respons") for line in lines
    )]))

    explicit_req, _ = normalize_skill_list(explicit_required or [])
    explicit_pref, _ = normalize_skill_list(explicit_preferred or [])
    if explicit_req or explicit_pref:
        req.required_skills = explicit_req
        req.preferred_skills = [s for s in explicit_pref if s not in explicit_req]
        req.skills_source = "explicit"
    else:
        required, preferred = infer_skills_from_text(full_text)
        if required or preferred:
            req.required_skills, req.preferred_skills = required, preferred
            req.skills_source = "inferred"

    req.level = level or detect_level(title)
    if min_years is not None:
        req.min_years, req.max_years, req.experience_source = min_years, max_years, "explicit"
    elif required_experience_months is not None:
        req.min_years, req.experience_source = round(required_experience_months / 12, 1), "explicit"
    elif no_experience_required:
        req.min_years, req.experience_source = 0.0, "explicit"
    else:
        low, high = years_from_text(full_text)
        if low is not None:
            req.min_years, req.max_years, req.experience_source = low, high, "stated_in_text"
        elif req.level:
            req.min_years, req.experience_source = float(LEVEL_MIN_YEARS[req.level]), "title_level"
    return req
