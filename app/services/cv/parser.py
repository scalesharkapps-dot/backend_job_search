"""Rule-based CV information extraction.

Extracts contact details, skills, work experience, education, certifications and
total experience from normalised CV text. Nothing is invented: fields that cannot
be extracted with reasonable confidence are ``None`` / empty, and every extracted
group carries a confidence value in ``metadata``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.services.skills import extract_skills_from_text, normalize_skill_list

SECTION_KEYWORDS: dict[str, tuple[str, ...]] = {
    "summary": ("summary", "professional summary", "profile", "about me", "objective",
                "career objective", "professional profile"),
    "experience": ("experience", "work experience", "professional experience",
                   "employment history", "work history", "employment", "career history",
                   "relevant experience", "internships", "internship experience"),
    "education": ("education", "academic background", "academics", "qualifications",
                  "academic qualifications", "educational background", "education and training"),
    "skills": ("skills", "technical skills", "core skills", "key skills", "core competencies",
               "competencies", "technologies", "tech stack", "tools and technologies",
               "skills and tools", "areas of expertise", "expertise"),
    "certifications": ("certifications", "certificates", "licenses and certifications",
                       "certifications and licenses", "courses", "training", "courses and certifications"),
    "projects": ("projects", "personal projects", "academic projects", "key projects"),
    "other": ("languages", "references", "interests", "hobbies", "awards", "achievements",
              "publications", "volunteer experience", "volunteering", "activities", "contact",
              "personal information", "declaration"),
}
_HEADING_LOOKUP = {kw: section for section, kws in SECTION_KEYWORDS.items() for kw in kws}

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w])(\+?\d[\d\s().\-]{8,18}\d)(?![\w])")

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_POINT = rf"(?:{_MONTH}\.?,?\s*\d{{4}}|\d{{1,2}}\s*/\s*\d{{4}}|\d{{4}})"
_END = rf"(?:{_POINT}|present|current|now|till date|to date|ongoing)"
DATE_RANGE_RE = re.compile(rf"(?P<start>{_POINT})\s*(?:-|–|—|to|until|till)\s*(?P<end>{_END})", re.I)
YEARS_STATEMENT_RE = re.compile(
    r"(\d{1,2}(?:\.\d)?)\s*\+?\s*(?:years?|yrs?)(?:\s+of)?\s+(?:\w+\s+){0,3}?experience", re.I
)

TITLE_WORDS = (
    "developer", "engineer", "manager", "analyst", "intern", "designer", "consultant",
    "officer", "lead", "specialist", "scientist", "architect", "administrator", "executive",
    "assistant", "associate", "coordinator", "teacher", "lecturer", "accountant", "director",
    "programmer", "technician", "representative", "supervisor", "head", "researcher",
    "trainee", "freelancer", "founder", "co-founder", "instructor", "tester", "qa",
    "devops", "sre", "writer", "marketer", "recruiter", "agent", "operator", "advisor",
)
DEGREE_RE = re.compile(
    r"\b(ph\.?\s?d|doctorate|master'?s?|m\.?\s?sc|m\.?\s?s\b|mba|m\.?\s?phil|m\.?\s?tech|m\.?\s?eng|"
    r"bachelor'?s?|b\.?\s?sc|b\.?\s?s\b|bba|b\.?\s?e\b|b\.?\s?tech|b\.?\s?eng|b\.?\s?com|b\.?\s?a\b|"
    r"associate degree|diploma|intermediate|f\.?\s?sc|f\.?\s?a\b|ics|i\.?\s?com|matric(?:ulation)?|"
    r"a[\s\-]?levels?|o[\s\-]?levels?|high school|ssc|hssc)",
    re.I,
)
INSTITUTION_RE = re.compile(r"\b(university|college|institute|school|academy|polytechnic|campus)\b", re.I)
CERT_HINT_RE = re.compile(r"\b(certified|certification|certificate|certificat)\b", re.I)


@dataclass
class ParsedCV:
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    skills: list[str] = field(default_factory=list)
    unrecognized_skills: list[str] = field(default_factory=list)
    work_experience: list[dict[str, Any]] = field(default_factory=list)
    education: list[dict[str, Any]] = field(default_factory=list)
    certifications: list[str] = field(default_factory=list)
    job_titles: list[str] = field(default_factory=list)
    years_of_experience: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ---------------- helpers ----------------
def _heading(line: str) -> str | None:
    cleaned = re.sub(r"[^a-z& ]", "", line.lower()).replace("&", "and").strip()
    if not cleaned or len(line) > 45:
        return None
    return _HEADING_LOOKUP.get(cleaned)


def split_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in text.split("\n"):
        stripped = line.strip().rstrip(":")
        section = _heading(stripped)
        if section:
            current = section
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)
    return sections


def _parse_point(raw: str, *, is_end: bool, today: date) -> date | None:
    raw = raw.strip().lower().replace(",", " ")
    if raw in {"present", "current", "now", "till date", "to date", "ongoing"}:
        return today
    m = re.match(rf"({_MONTH})\.?\s*(\d{{4}})", raw, re.I)
    if m:
        month = MONTHS.get(m.group(1)[:4].rstrip("t") if m.group(1).startswith("sept") else m.group(1)[:3], 1)
        return date(int(m.group(2)), month, 1)
    m = re.match(r"(\d{1,2})\s*/\s*(\d{4})", raw)
    if m and 1 <= int(m.group(1)) <= 12:
        return date(int(m.group(2)), int(m.group(1)), 1)
    m = re.match(r"(\d{4})", raw)
    if m:
        return date(int(m.group(1)), 12 if is_end else 1, 1)
    return None


def parse_date_range(text: str, today: date | None = None) -> tuple[date, date, str] | None:
    """Return (start, end, matched_text) for the first plausible date range."""
    today = today or date.today()
    for match in DATE_RANGE_RE.finditer(text):
        start = _parse_point(match.group("start"), is_end=False, today=today)
        end = _parse_point(match.group("end"), is_end=True, today=today)
        if not start or not end:
            continue
        end = min(end, today)
        if 1960 <= start.year <= today.year and start <= end:
            return start, end, match.group(0)
    return None


def merged_months(ranges: list[tuple[date, date]]) -> int:
    """Total months covered by possibly overlapping ranges."""
    if not ranges:
        return 0
    spans = sorted((s.year * 12 + s.month, e.year * 12 + e.month) for s, e in ranges)
    total = 0
    cur_s, cur_e = spans[0]
    for s, e in spans[1:]:
        if s <= cur_e:
            cur_e = max(cur_e, e)
        else:
            total += cur_e - cur_s + 1
            cur_s, cur_e = s, e
    total += cur_e - cur_s + 1
    return total


def _looks_like_title(text: str) -> bool:
    lower = text.lower()
    return any(re.search(rf"\b{re.escape(w)}\b", lower) for w in TITLE_WORDS)


def _clean(part: str) -> str:
    return part.strip(" \t•·*-–—|,:;()")


def _split_header(header: str) -> tuple[str | None, str | None]:
    for sep in (" at ", " @ ", " | ", " – ", " — ", " - ", ", "):
        if sep in header:
            left, right = (_clean(p) for p in header.split(sep, 1))
            if _looks_like_title(left):
                return left or None, right or None
            if _looks_like_title(right):
                return right or None, left or None
    header = _clean(header)
    return (header, None) if header and _looks_like_title(header) else (None, header or None)


# ---------------- extractors ----------------
def extract_contact(text: str) -> tuple[str | None, str | None]:
    email = EMAIL_RE.search(text)
    phone_value: str | None = None
    for m in PHONE_RE.finditer(text[:3000]):
        candidate = m.group(1)
        digits = re.sub(r"\D", "", candidate)
        if 10 <= len(digits) <= 15 and not DATE_RANGE_RE.search(candidate):
            phone_value = re.sub(r"[^\d+]", "", candidate)
            break
    return (email.group(0).lower() if email else None), phone_value


def extract_name(header_lines: list[str], email: str | None) -> tuple[str | None, float]:
    for line in [ln.strip() for ln in header_lines if ln.strip()][:6]:
        if EMAIL_RE.search(line) or re.search(r"\d", line) or _heading(line):
            continue
        words = line.replace(",", " ").split()
        if not 2 <= len(words) <= 4:
            continue
        if all(re.fullmatch(r"[A-Za-z][A-Za-z'.\-]*", w) for w in words) and not _looks_like_title(line):
            if all(w[0].isupper() for w in words):
                confidence = 0.8 if email and words[0].lower()[:3] in email else 0.6
                return " ".join(words), confidence
    return None, 0.0


def extract_skill_lists(sections: dict[str, list[str]], full_text: str) -> tuple[list[str], list[str], dict[str, str]]:
    sources: dict[str, str] = {}
    listed: list[str] = []
    for line in sections.get("skills", []):
        line = re.sub(r"^[^:]{1,40}:\s*", "", line)  # "Languages: Python, Go"
        listed.extend(p for p in re.split(r"[,;|•·/\t]|\s{2,}", line) if p.strip())
    known, unknown = normalize_skill_list(listed)
    for skill in known:
        sources[skill] = "skills_section"
    for skill in extract_skills_from_text(full_text):
        sources.setdefault(skill, "text")
    skills = list(sources)
    # Unknown list items longer than a few words are probably sentences, not skills.
    unknown = [u for u in unknown if len(u.split()) <= 4][:30]
    return skills, unknown, sources


def extract_experience(lines: list[str], today: date) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    previous_text = ""
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        found = parse_date_range(stripped, today)
        if found:
            start, end, matched = found
            remainder = _clean(stripped.replace(matched, " "))
            header = remainder if len(remainder) > 3 else previous_text
            title, company = _split_header(header) if header else (None, None)
            if title is None and company and previous_text and previous_text != header:
                title2, _ = _split_header(previous_text)
                title = title2
            is_current = bool(re.search(r"present|current|now|ongoing|till date|to date", matched, re.I))
            entries.append(
                {
                    "title": title,
                    "company": company,
                    "start_date": start.isoformat(),
                    "end_date": None if is_current else end.isoformat(),
                    "is_current": is_current,
                    "duration_months": (end.year - start.year) * 12 + end.month - start.month + 1,
                    "is_internship": bool(title and re.search(r"\bintern", title, re.I)),
                    "confidence": 0.7 if title and company else (0.5 if title or company else 0.3),
                }
            )
        else:
            previous_text = stripped
    return entries


def extract_education(lines: list[str], today: date) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in [ln.strip() for ln in lines if ln.strip()]:
        degree = DEGREE_RE.search(line)
        institution = INSTITUTION_RE.search(line)
        years = parse_date_range(line, today)
        single_year = re.search(r"\b(19[6-9]\d|20[0-4]\d)\b", line)
        if degree:
            current = {
                "degree": _clean(DATE_RANGE_RE.sub("", line))[:200],
                "institution": None,
                "start_year": years[0].year if years else None,
                "end_year": years[1].year if years else (int(single_year.group(1)) if single_year else None),
                "confidence": 0.6,
            }
            entries.append(current)
        elif institution and current is not None and current["institution"] is None:
            current["institution"] = _clean(DATE_RANGE_RE.sub("", line))[:200]
            current["confidence"] = 0.75
            if years and current["start_year"] is None:
                current["start_year"] = years[0].year
            if current["end_year"] is None and (years or single_year):
                current["end_year"] = years[1].year if years else int(single_year.group(1))  # type: ignore[union-attr]
        elif institution and current is None:
            current = {"degree": None, "institution": _clean(line)[:200], "start_year": None,
                       "end_year": int(single_year.group(1)) if single_year else None, "confidence": 0.4}
            entries.append(current)
    return entries[:10]


def extract_certifications(sections: dict[str, list[str]], full_text: str) -> list[str]:
    items: dict[str, None] = {}
    for line in sections.get("certifications", []):
        cleaned = _clean(line)
        if 3 < len(cleaned) <= 150:
            items.setdefault(cleaned, None)
    if not items:
        for line in full_text.split("\n"):
            cleaned = _clean(line)
            if CERT_HINT_RE.search(cleaned) and 5 < len(cleaned) <= 150:
                items.setdefault(cleaned, None)
    return list(items)[:20]


def parse_cv_text(text: str, today: date | None = None) -> ParsedCV:
    """Parse normalised CV text into structured, confidence-annotated data."""
    today = today or date.today()
    result = ParsedCV()
    if not text.strip():
        return result
    sections = split_sections(text)
    result.email, result.phone = extract_contact(text)
    result.name, name_conf = extract_name(sections.get("header", []), result.email)
    result.skills, result.unrecognized_skills, skill_sources = extract_skill_lists(sections, text)

    exp_lines = sections.get("experience")
    exp_source = "experience_section"
    if not exp_lines:
        # No experience heading: use lines with date ranges outside the education section.
        exp_lines = [ln for key, ls in sections.items() if key not in {"education", "certifications", "projects"} for ln in ls]
        exp_source = "whole_document"
    experience = extract_experience(exp_lines, today)
    if exp_source == "whole_document":
        experience = [e for e in experience if e["title"]]  # require a job-title signal
    result.work_experience = experience
    result.job_titles = list(dict.fromkeys(e["title"] for e in experience if e["title"]))

    professional = [
        (date.fromisoformat(e["start_date"]), date.fromisoformat(e["end_date"]) if e["end_date"] else today)
        for e in experience
        if not e["is_internship"]
    ]
    years_method = None
    if professional:
        result.years_of_experience = round(merged_months(professional) / 12, 1)
        years_method = "date_ranges"
    else:
        summary = "\n".join(sections.get("summary", []) + sections.get("header", []))
        m = YEARS_STATEMENT_RE.search(summary)
        if m:
            result.years_of_experience = float(m.group(1))
            years_method = "explicit_statement"

    result.education = extract_education(sections.get("education", []), today)
    result.certifications = extract_certifications(sections, text)
    result.metadata = {
        "sections_found": sorted(k for k in sections if k != "header" and sections[k]),
        "confidence": {
            "name": name_conf,
            "email": 0.95 if result.email else 0.0,
            "phone": 0.7 if result.phone else 0.0,
            "skills": 0.85 if any(v == "skills_section" for v in skill_sources.values()) else (0.6 if result.skills else 0.0),
            "experience": round(sum(e["confidence"] for e in experience) / len(experience), 2) if experience else 0.0,
            "education": round(sum(e["confidence"] for e in result.education) / len(result.education), 2) if result.education else 0.0,
            "years_of_experience": 0.7 if years_method == "date_ranges" else (0.6 if years_method else 0.0),
        },
        "skill_sources": skill_sources,
        "experience_source": exp_source,
        "years_of_experience_method": years_method,
        "parser_version": "rules-1.0",
    }
    return result
