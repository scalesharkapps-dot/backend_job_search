"""City matching shared by job search providers and backend-side filtering.

Providers spell places differently ("Karāchi", "Karachi Division", "Bangalore" vs
"Bengaluru"), so comparisons use accent-folded, punctuation-free text plus a small
alias table. Works for any city worldwide; the alias table only adds alternate
names and the diacritic spellings Techmap uses for major Pakistani cities.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Protocol

# canonical (folded) city -> spellings / alternate names seen at providers
CITY_VARIANTS: dict[str, tuple[str, ...]] = {
    "karachi": ("Karachi", "Karāchi"),
    "lahore": ("Lahore", "Lāhore"),
    "islamabad": ("Islamabad", "Islāmābād"),
    "rawalpindi": ("Rawalpindi", "Rāwalpindi"),
    "faisalabad": ("Faisalabad", "Faisalābād", "Lyallpur"),
    "multan": ("Multan", "Multān"),
    "peshawar": ("Peshawar", "Peshāwar"),
    "hyderabad": ("Hyderabad", "Hyderābād"),
    "sialkot": ("Sialkot", "Siālkot"),
    "gujranwala": ("Gujranwala", "Gujrānwāla"),
    "quetta": ("Quetta",),
    "bengaluru": ("Bengaluru", "Bangalore"),
    "mumbai": ("Mumbai", "Bombay"),
    "new york": ("New York", "New York City", "NYC"),
    "ho chi minh city": ("Ho Chi Minh City", "Saigon"),
    "frankfurt": ("Frankfurt", "Frankfurt am Main"),
}
_ALIAS_TO_CANONICAL = {
    re.sub(r"[^a-z0-9]+", " ", unicodedata.normalize("NFKD", v).encode("ascii", "ignore").decode().lower()).strip(): k
    for k, variants in CITY_VARIANTS.items()
    for v in variants
}

# Location values that name a whole country (used to reject them as a "city").
COUNTRY_NAMES = {
    "pakistan", "united states", "usa", "us", "united kingdom", "uk", "england", "canada", "australia",
    "germany", "uae", "united arab emirates", "saudi arabia", "qatar", "india", "ireland", "netherlands",
    "singapore", "france", "spain", "italy", "new zealand", "malaysia", "turkey", "oman", "bahrain",
    "kuwait", "sweden", "poland",
}


def fold(text: str | None) -> str:
    """Lower-case ASCII words: accents removed, punctuation collapsed ("Karāchi," -> "karachi")."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def canonical_city(city: str) -> str:
    folded = fold(city)
    return _ALIAS_TO_CANONICAL.get(folded, folded)


def city_variants(city: str) -> list[str]:
    """Spellings to send to a provider for ``city`` (the user's spelling first)."""
    known = CITY_VARIANTS.get(canonical_city(city))
    if not known:
        return [city.strip()]
    out = [city.strip()]
    for v in known:  # literal comparison: "Karāchi" must stay distinct from "Karachi"
        if v.lower() not in {o.lower() for o in out}:
            out.append(v)
    return out


def is_country_name(value: str) -> bool:
    return fold(value) in COUNTRY_NAMES


class _Located(Protocol):
    city: str | None
    state: str | None
    location: str | None
    country: str | None
    is_remote: bool


# Match ranks (lower = better); None = no match.
EXACT_CITY = 0  # job.city is the city ("Karāchi" == "Karachi")
IN_LOCATION = 1  # the city appears in the city/location text ("Karachi Division, Sindh")
REMOTE_ANYWHERE = 2  # remote job with no city, accepted only when remote jobs are requested


def city_match_rank(
    job: _Located, city: str, *, country: str | None = None, include_remote_anywhere: bool = False
) -> int | None:
    """How well ``job`` matches ``city`` (None = it does not).

    ``country`` (ISO-2), when given, must not contradict the job's country, so
    "Hyderabad" + ``pk`` never matches Hyderabad, India. Jobs without any location
    data never match, except remote jobs when ``include_remote_anywhere`` is set.
    """
    if country and job.country and job.country.lower() != country.lower():
        return None
    names = {fold(v) for v in city_variants(city)} | {canonical_city(city)}
    names.discard("")
    job_city = fold(job.city)
    if job_city and (job_city in names or _ALIAS_TO_CANONICAL.get(job_city) in names):
        return EXACT_CITY
    haystack = f" {fold(job.city)} {fold(job.location)} {fold(job.state)} "
    if any(f" {name} " in haystack for name in names):
        return IN_LOCATION
    if include_remote_anywhere and job.is_remote and not job_city:
        return REMOTE_ANYWHERE
    return None
