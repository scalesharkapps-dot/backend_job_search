"""Convert Techmap job postings into SharkHire's normalised ``Job`` fields.

Same rules as the JSearch normaliser: missing stays ``None``, ``"N/A"`` means
unknown, URLs must be http(s), nothing is invented. Techmap's ``estimatedSalary``
is a model estimate (``hasSalary: false``) and is therefore NOT presented as the
posting's salary; it is kept only in ``provider_metadata``.
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.models.job import JobSource
from app.services.jsearch.normalizer import MAX_DESCRIPTION, SALARY_PERIODS, _num, _str, safe_url
from app.services.skills import normalize_skill_list

_UNKNOWN = {"", "n/a", "na", "none", "null", "unknown"}
_TYPE_MAP = {
    "fulltime": "FULLTIME", "full-time": "FULLTIME", "full time": "FULLTIME",
    "parttime": "PARTTIME", "part-time": "PARTTIME", "part time": "PARTTIME",
    "internship": "INTERN", "intern": "INTERN",
    "contract": "CONTRACTOR", "contractor": "CONTRACTOR", "temporary": "CONTRACTOR", "freelance": "CONTRACTOR",
}


def _known(value: Any) -> Any:
    if isinstance(value, str):
        value = html.unescape(value)  # Techmap passes some names through HTML-escaped ("S&amp;P Global")
        if value.strip().lower() in _UNKNOWN:
            return None
    return value


def _values(value: Any) -> list[str]:
    """Techmap uses lists for workPlace/workType/contractType/careerLevel (sometimes a string)."""
    items = value if isinstance(value, list) else [value]
    return [str(v).strip() for v in items if isinstance(v, str) and _known(v) is not None]


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _identifier(value: Any) -> str | None:
    if isinstance(value, dict):  # schema.org PropertyValue
        value = value.get("value") or value.get("name")
    return _str(value, 512) if isinstance(value, str | int) and not isinstance(value, bool) else None


def _posted_at(raw: dict[str, Any], json_ld: dict[str, Any], now: datetime) -> datetime | None:
    for text in (raw.get("dateCreated"), json_ld.get("datePosted")):
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            value = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        if value.year >= 2000 and value <= now + timedelta(days=1):
            return value.astimezone(UTC)
    return None


def _stable_id(title: str, company: str | None, city: str | None, country: str | None, url: str | None) -> str:
    parts = [title, company or "", city or "", country or "", url or ""]
    return "h:" + hashlib.sha256("|".join(p.lower() for p in parts).encode()).hexdigest()


def normalize_techmap_job(raw: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    """Return column values for ``Job`` or ``None`` when the posting is unusable."""
    now = now or datetime.now(UTC)
    json_ld = _dict(raw.get("jsonLD"))
    title = _str(_known(raw.get("title")) or _known(json_ld.get("title")), 300)
    if not title:
        return None
    org = _dict(json_ld.get("hiringOrganization"))
    company = _str(_known(raw.get("company")) or _known(org.get("name")), 300)

    country = _str(_known(raw.get("countryCode")), 10)
    country = country.lower() if country and re.fullmatch(r"[A-Za-z]{2}", country) else None
    city = _str(_known(raw.get("city")), 200)
    state = _str(_known(raw.get("state")), 200)
    location = ", ".join(p for p in (city, state, country.upper() if country else None) if p) or _str(
        _known(_dict(json_ld.get("jobLocation")).get("name")), 300
    )

    apply_url = safe_url(json_ld.get("url")) or safe_url(raw.get("url"))
    external_id = _identifier(json_ld.get("identifier")) or _stable_id(title, company, city, country, apply_url)

    work_places = [v.lower() for v in _values(raw.get("workPlace"))]
    is_remote = "remote" in work_places or str(json_ld.get("jobLocationType") or "").upper() == "TELECOMMUTE"

    types: list[str] = []
    for value in _values(raw.get("workType")) + _values(raw.get("contractType")) + _values(json_ld.get("employmentType")):
        mapped = _TYPE_MAP.get(value.lower())
        if mapped and mapped not in types:
            types.append(mapped)

    has_salary = raw.get("hasSalary") is True
    min_salary = _num(raw.get("minSalary")) if has_salary else None
    max_salary = _num(raw.get("maxSalary")) if has_salary else None
    if min_salary and max_salary and min_salary > max_salary:
        min_salary, max_salary = max_salary, min_salary
    base = _dict(json_ld.get("baseSalary"))
    currency = _str(_known(json_ld.get("salaryCurrency")) or _known(base.get("currency")), 8)
    currency = currency.upper() if currency and re.fullmatch(r"[A-Za-z]{3}", currency) else None
    period = _str(_known(_dict(base.get("value")).get("unitText")) or _known(json_ld.get("employmentUnit")), 16)
    period = period.upper() if period and period.upper() in SALARY_PERIODS else None

    skills_raw = raw.get("skills") if isinstance(raw.get("skills"), list) else json_ld.get("skills")
    if isinstance(skills_raw, str):
        skills_raw = [s for s in skills_raw.split(",") if s.strip()]
    skills, _unknown = normalize_skill_list(skills_raw if isinstance(skills_raw, list) else [])

    months = _dict(json_ld.get("experienceRequirements")).get("monthsOfExperience")
    months = int(months) if isinstance(months, int | float) and not isinstance(months, bool) and 0 <= months <= 600 else None

    description = json_ld.get("description")
    estimated = _dict(json_ld.get("estimatedSalary"))
    metadata = {
        "portal": _known(raw.get("portal")),
        "portal_source": _known(raw.get("source")),
        "industry": _known(raw.get("industry")),
        "occupation": _known(raw.get("occupation")),
        "career_level": _values(raw.get("careerLevel")) or None,
        "work_place": _values(raw.get("workPlace")) or None,
        "is_recruiter": raw.get("isRecruiter") if isinstance(raw.get("isRecruiter"), bool) else None,
        "date_expired": _known(raw.get("dateExpired")),
        "estimated_salary": estimated or None,  # provider estimate, not part of the posting
    }
    return {
        "source": JobSource.TECHMAP,
        "external_id": external_id,
        "title": title,
        "company_name": company,
        "company_logo": safe_url(json_ld.get("image")) or safe_url(org.get("logo")),
        "company_website": safe_url(org.get("url")) or safe_url(org.get("sameAs")),
        "description": description.strip()[:MAX_DESCRIPTION] if isinstance(description, str) and description.strip() else None,
        "highlights": {},
        "required_skills": skills,
        "city": city,
        "state": state,
        "country": country,
        "location": location,
        "is_remote": is_remote,
        "employment_type": types[0] if types else None,
        "employment_types": types,
        "min_salary": min_salary,
        "max_salary": max_salary,
        "salary_currency": currency if (min_salary or max_salary) else None,
        "salary_period": period if (min_salary or max_salary) else None,
        "salary_text": None,
        "publisher": _str(_known(raw.get("portal")), 200),
        "apply_url": apply_url,
        "apply_is_direct": raw.get("isDirect") if isinstance(raw.get("isDirect"), bool) else None,
        "apply_options": [{"publisher": _str(_known(raw.get("portal")), 200), "apply_link": apply_url,
                           "is_direct": raw.get("isDirect") if isinstance(raw.get("isDirect"), bool) else None}]
        if apply_url else [],
        "google_link": None,
        "posted_at": _posted_at(raw, json_ld, now),
        "required_experience_months": months,
        "no_experience_required": None,
        "provider_metadata": {k: v for k, v in metadata.items() if v is not None},
    }
