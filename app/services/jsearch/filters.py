"""Search-parameter validation and mapping to JSearch query parameters.

Filters applied BY JSEARCH: query, country, language, date_posted, work_from_home
(remote_jobs_only), employment_types, job_requirements (experience), radius,
exclude_job_publishers, num_pages, cursor.
Filters applied LOCALLY after normalisation: company (substring match on employer
name; also appended to the query text), min_salary.
"""

from __future__ import annotations

import enum
import hashlib
import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import get_settings


class DatePosted(str, enum.Enum):
    ALL = "all"
    TODAY = "today"
    THREE_DAYS = "3days"
    WEEK = "week"
    MONTH = "month"


class SearchEmploymentType(str, enum.Enum):
    FULLTIME = "FULLTIME"
    PARTTIME = "PARTTIME"
    CONTRACTOR = "CONTRACTOR"
    INTERN = "INTERN"


class ExperienceRequirement(str, enum.Enum):
    UNDER_3_YEARS = "under_3_years_experience"
    MORE_THAN_3_YEARS = "more_than_3_years_experience"
    NO_EXPERIENCE = "no_experience"
    NO_DEGREE = "no_degree"


# Location → ISO country inference (only used when the user gives no country).
PAKISTAN_CITIES = (
    "karachi", "lahore", "islamabad", "rawalpindi", "hyderabad, pakistan", "faisalabad", "peshawar",
    "quetta", "multan", "sialkot", "gujranwala", "sukkur", "abbottabad", "bahawalpur", "sargodha",
)
COUNTRY_ALIASES: dict[str, str] = {
    "pakistan": "pk", "united states": "us", "usa": "us", "united kingdom": "gb", "uk": "gb",
    "england": "gb", "london": "gb", "canada": "ca", "toronto": "ca", "australia": "au",
    "sydney": "au", "germany": "de", "berlin": "de", "uae": "ae", "united arab emirates": "ae",
    "dubai": "ae", "abu dhabi": "ae", "saudi arabia": "sa", "riyadh": "sa", "jeddah": "sa",
    "qatar": "qa", "doha": "qa", "india": "in", "ireland": "ie", "netherlands": "nl",
    "singapore": "sg", "new york": "us", "san francisco": "us", "france": "fr", "spain": "es",
    "italy": "it", "new zealand": "nz", "malaysia": "my", "turkey": "tr", "oman": "om",
    "bahrain": "bh", "kuwait": "kw", "sweden": "se", "poland": "pl",
}


def infer_country(location: str | None) -> str | None:
    if not location:
        return None
    lower = location.lower()
    if any(city in lower for city in PAKISTAN_CITIES):
        return "pk"
    for alias in sorted(COUNTRY_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", lower):
            return COUNTRY_ALIASES[alias]
    return None


_TEXT_RE = re.compile(r"^[\w\s.,+#&/()'\-]+$", re.UNICODE)


class JobSearchParams(BaseModel):
    """Validated search parameters (query-string model for GET /jobs/search)."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=2, max_length=200, description="Job title or keywords")
    location: str | None = Field(default=None, max_length=100, description="City/region, e.g. Karachi")
    country: str | None = Field(default=None, description="ISO 3166-1 alpha-2, e.g. pk, us, gb")
    company: str | None = Field(default=None, max_length=100)
    remote_jobs_only: bool = False
    employment_types: list[SearchEmploymentType] = Field(default_factory=list)
    experience: list[ExperienceRequirement] = Field(default_factory=list)
    date_posted: DatePosted = DatePosted.ALL
    radius: int | None = Field(default=None, ge=1, le=500, description="km")
    language: str | None = Field(default=None, description="ISO 639-1, e.g. en")
    exclude_job_publishers: list[str] = Field(default_factory=list, max_length=10)
    min_salary: float | None = Field(default=None, ge=0, description="Applied locally")
    num_pages: int = Field(default=1, ge=1)
    cursor: str | None = Field(default=None, max_length=1024)

    @field_validator("query", "location", "company")
    @classmethod
    def _text(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = re.sub(r"\s+", " ", v).strip()
        if not v:
            return None
        if not _TEXT_RE.match(v):
            raise ValueError("contains unsupported characters")
        return v

    @field_validator("country")
    @classmethod
    def _country(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        v = v.strip().lower()
        if not re.fullmatch(r"[a-z]{2}", v):
            raise ValueError("country must be a 2-letter ISO code")
        return v

    @field_validator("language")
    @classmethod
    def _lang(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        if not re.fullmatch(r"[a-zA-Z]{2}", v.strip()):
            raise ValueError("language must be a 2-letter ISO 639-1 code")
        return v.strip().lower()

    @field_validator("num_pages")
    @classmethod
    def _pages(cls, v: int) -> int:
        limit = get_settings().JSEARCH_MAX_NUM_PAGES
        if v > limit:
            raise ValueError(f"num_pages must be at most {limit}")
        return v

    @field_validator("exclude_job_publishers")
    @classmethod
    def _publishers(cls, v: list[str]) -> list[str]:
        out = []
        for p in v:
            p = p.strip()
            if p:
                if len(p) > 60 or not _TEXT_RE.match(p) or "," in p:
                    raise ValueError("invalid publisher name")
                out.append(p)
        return out

    @property
    def effective_country(self) -> str | None:
        return self.country or infer_country(self.location) or (get_settings().JSEARCH_DEFAULT_COUNTRY or None)

    def provider_params(self) -> dict[str, Any]:
        text = self.query
        if self.company:
            text += f" at {self.company}"
        if self.location:
            text += f" in {self.location}"
        params: dict[str, Any] = {
            "query": text,
            "num_pages": self.num_pages,
            "date_posted": self.date_posted.value,
            "country": self.effective_country,
            "language": self.language,
            "radius": self.radius,
        }
        if self.remote_jobs_only:
            params["work_from_home"] = "true"
        if self.employment_types:
            params["employment_types"] = ",".join(sorted({e.value for e in self.employment_types}))
        if self.experience:
            params["job_requirements"] = ",".join(sorted({e.value for e in self.experience}))
        if self.exclude_job_publishers:
            params["exclude_job_publishers"] = ",".join(sorted(self.exclude_job_publishers))
        return {k: v for k, v in params.items() if v not in (None, "")}

    def cache_key(self) -> str:
        settings = get_settings()
        material = {
            "path": settings.JSEARCH_SEARCH_PATH,
            "provider": self.provider_params(),
            "cursor": self.cursor,
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


FILTER_CATALOG: dict[str, Any] = {
    "employment_types": [e.value for e in SearchEmploymentType],
    "date_posted": [d.value for d in DatePosted],
    "experience": [e.value for e in ExperienceRequirement],
    "remote_jobs_only": [True, False],
    "pakistan_cities": [c.title() for c in PAKISTAN_CITIES if "," not in c] + ["Hyderabad, Pakistan"],
    "example_countries": {
        "pk": "Pakistan", "us": "United States", "gb": "United Kingdom", "ca": "Canada",
        "au": "Australia", "de": "Germany", "ae": "United Arab Emirates", "sa": "Saudi Arabia",
    },
    "applied_by_provider": [
        "query", "location", "country", "remote_jobs_only", "employment_types", "experience",
        "date_posted", "radius", "language", "exclude_job_publishers", "num_pages", "cursor",
    ],
    "applied_locally": ["company", "min_salary"],
    "pagination": "cursor",
    "notes": [
        "Coverage depends on the JSearch provider (Google for Jobs); not every job worldwide is indexed.",
        "Remote listings are flagged with job_is_remote; location restrictions in descriptions still apply.",
    ],
}
