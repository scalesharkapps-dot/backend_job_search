"""Convert raw JSearch job objects into SharkHire's normalised job fields.

Rules: missing values stay ``None`` (missing salary is never 0), currencies are
kept as supplied (never assumed), URLs must be http(s) with a host, timestamps
are sanity-checked, and nothing is invented.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from app.models.job import JobSource
from app.services.skills import normalize_skill_list

logger = logging.getLogger(__name__)

MAX_DESCRIPTION = 20_000
EMPLOYMENT_MAP = {
    "FULLTIME": "FULLTIME", "FULL-TIME": "FULLTIME", "FULL TIME": "FULLTIME", "FULL_TIME": "FULLTIME",
    "PARTTIME": "PARTTIME", "PART-TIME": "PARTTIME", "PART TIME": "PARTTIME", "PART_TIME": "PARTTIME",
    "CONTRACTOR": "CONTRACTOR", "CONTRACT": "CONTRACTOR", "TEMPORARY": "CONTRACTOR",
    "INTERN": "INTERN", "INTERNSHIP": "INTERN",
}
SALARY_PERIODS = {"HOUR", "DAY", "WEEK", "MONTH", "YEAR"}


def safe_url(value: Any) -> str | None:
    """Return the URL only when it is an http(s) URL with a host (rejects javascript:, data:, etc.)."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 2048 or any(c in value for c in "\r\n\t <>\"'`"):
        return None
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or "@" in parsed.netloc:
        return None
    return value


def _str(value: Any, max_len: int) -> str | None:
    if value is None:
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text[:max_len] or None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def employment_type(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return EMPLOYMENT_MAP.get(value.strip().upper())


def parse_posted_at(raw: dict[str, Any], now: datetime | None = None) -> datetime | None:
    now = now or datetime.now(UTC)
    ts = raw.get("job_posted_at_timestamp")
    candidate: datetime | None = None
    if isinstance(ts, int | float) and not isinstance(ts, bool):
        try:
            candidate = datetime.fromtimestamp(float(ts), tz=UTC)
        except (OverflowError, OSError, ValueError):
            candidate = None
    if candidate is None and isinstance(raw.get("job_posted_at_datetime_utc"), str):
        text = raw["job_posted_at_datetime_utc"].replace("Z", "+00:00")
        try:
            candidate = datetime.fromisoformat(text)
            if candidate.tzinfo is None:
                candidate = candidate.replace(tzinfo=UTC)
        except ValueError:
            candidate = None
    if candidate is None:
        return None
    if candidate.year < 2000 or candidate > now + timedelta(days=1):
        return None
    return candidate.astimezone(UTC)


def stable_external_id(raw: dict[str, Any]) -> str:
    """Composite identity for listings without a provider id (never title alone)."""
    parts = [
        _str(raw.get("job_title"), 300) or "",
        _str(raw.get("employer_name"), 300) or "",
        _str(raw.get("job_city") or raw.get("job_location"), 200) or "",
        _str(raw.get("job_country"), 10) or "",
        safe_url(raw.get("job_apply_link")) or "",
    ]
    return "h:" + hashlib.sha256("|".join(p.lower() for p in parts).encode()).hexdigest()


def _highlights(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, list[str]] = {}
    for key, items in value.items():
        if isinstance(items, list):
            cleaned = [s for s in (_str(i, 1000) for i in items[:50]) if s]
            if cleaned:
                out[str(key)[:50]] = cleaned
    return out


def normalize_job(raw: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    """Return column values for ``Job`` or ``None`` when the listing is unusable."""
    title = _str(raw.get("job_title"), 300)
    if not title:
        logger.debug("Skipping JSearch job without a title")
        return None
    external_id = _str(raw.get("job_id"), 512) or stable_external_id(raw)

    types_raw = raw.get("job_employment_types")
    types = [t for t in (employment_type(v) for v in (types_raw if isinstance(types_raw, list) else [])) if t]
    primary = employment_type(raw.get("job_employment_type")) or (types[0] if types else None)
    if primary and primary not in types:
        types.insert(0, primary)

    country = _str(raw.get("job_country"), 10)
    country = country.lower() if country and re.fullmatch(r"[A-Za-z]{2}", country) else None
    city, state = _str(raw.get("job_city"), 200), _str(raw.get("job_state"), 200)
    location = _str(raw.get("job_location"), 300) or ", ".join(p for p in (city, state, country.upper() if country else None) if p) or None

    min_salary, max_salary = _num(raw.get("job_min_salary")), _num(raw.get("job_max_salary"))
    if min_salary and max_salary and min_salary > max_salary:
        min_salary, max_salary = max_salary, min_salary
    period = _str(raw.get("job_salary_period"), 16)
    period = period.upper() if period and period.upper() in SALARY_PERIODS else None
    currency = _str(raw.get("job_salary_currency"), 8)
    currency = currency.upper() if currency and re.fullmatch(r"[A-Za-z]{3}", currency) else None

    skills, _unknown = normalize_skill_list(
        raw.get("job_required_skills") if isinstance(raw.get("job_required_skills"), list) else []
    )
    experience = raw.get("job_required_experience") if isinstance(raw.get("job_required_experience"), dict) else {}
    months = experience.get("required_experience_in_months")
    months = int(months) if isinstance(months, int | float) and not isinstance(months, bool) and 0 <= months <= 600 else None
    no_exp = experience.get("no_experience_required")

    apply_options = []
    for option in raw.get("apply_options") or []:
        if isinstance(option, dict) and safe_url(option.get("apply_link")):
            apply_options.append(
                {
                    "publisher": _str(option.get("publisher"), 200),
                    "apply_link": safe_url(option.get("apply_link")),
                    "is_direct": bool(option.get("is_direct")) if option.get("is_direct") is not None else None,
                }
            )
        if len(apply_options) >= 10:
            break

    is_remote = raw.get("job_is_remote")
    description = raw.get("job_description")
    return {
        "source": JobSource.JSEARCH,
        "external_id": external_id,
        "title": title,
        "company_name": _str(raw.get("employer_name"), 300),
        "company_logo": safe_url(raw.get("employer_logo")),
        "company_website": safe_url(raw.get("employer_website")),
        "description": description.strip()[:MAX_DESCRIPTION] if isinstance(description, str) and description.strip() else None,
        "highlights": _highlights(raw.get("job_highlights")),
        "required_skills": skills,
        "city": city,
        "state": state,
        "country": country,
        "location": location,
        "is_remote": is_remote is True,
        "employment_type": primary,
        "employment_types": types,
        "min_salary": min_salary,
        "max_salary": max_salary,
        "salary_currency": currency,
        "salary_period": period if (min_salary or max_salary) else None,
        "salary_text": _str(raw.get("job_salary"), 200),
        "publisher": _str(raw.get("job_publisher"), 200),
        "apply_url": safe_url(raw.get("job_apply_link")) or (apply_options[0]["apply_link"] if apply_options else None),
        "apply_is_direct": raw.get("job_apply_is_direct") if isinstance(raw.get("job_apply_is_direct"), bool) else None,
        "apply_options": apply_options,
        "google_link": safe_url(raw.get("job_google_link")),
        "posted_at": parse_posted_at(raw, now),
        "required_experience_months": months,
        "no_experience_required": no_exp if isinstance(no_exp, bool) else None,
        "provider_metadata": {
            k: raw.get(k)
            for k in ("job_onet_soc", "job_onet_job_zone", "job_latitude", "job_longitude", "job_benefits")
            if raw.get(k) is not None
        },
    }
