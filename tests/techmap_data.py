"""Techmap test payloads.

Shapes follow a real ``/api/v2/jobs/search`` response captured on 2026-11-28
(lists for workPlace/workType/contractType/careerLevel, "N/A" for unknown values,
``jsonLD.identifier`` / ``jsonLD.url`` for id and apply link). Values are synthetic.
"""

from __future__ import annotations

import copy
from typing import Any

_BASE: dict[str, Any] = {
    "title": "Python Developer",
    "company": "Acme Software",
    "countryCode": "pk",
    "city": "Karāchi",
    "state": "Sindh",
    "language": "en",
    "locale": "en_PK",
    "portal": "linkedin",
    "source": "linkedin_pk",
    "industry": "Software Development",
    "occupation": "Software Developer",
    "dateCreated": "2026-11-27T10:00:00.000Z",
    "dateExpired": "2026-12-27T10:00:00.000Z",
    "workPlace": ["Onsite"],
    "workType": ["FullTime"],
    "contractType": ["N/A"],
    "careerLevel": ["Junior"],
    "skills": ["Python", "Django"],
    "hasSalary": False,
    "isDirect": True,
    "isRecruiter": False,
    "isDuplicate": False,
    "geoPoint": {"lon": 67.0, "lat": 24.86},
    "jsonLD": {
        "@type": "JobPosting",
        "identifier": "tm0000000000000000000001",
        "title": "Python Developer",
        "url": "https://pk.linkedin.com/jobs/view/python-developer-1",
        "description": "### Python Developer\n\nBuild APIs with **Python** and Django.",
        "employmentType": "FullTime",
        "employmentUnit": "N/A",
        "salaryCurrency": "PKR",
        "image": "https://media.licdn.com/dms/image/acme-logo.png",
        "datePosted": "2026-11-27",
        "hiringOrganization": {"@type": "Organization", "name": "Acme Software", "url": "https://acme.example"},
        "jobLocation": {"@type": "Place", "name": "Karāchi, Sindh, Pakistan"},
        "experienceRequirements": {"@type": "OccupationalExperienceRequirements", "monthsOfExperience": 24},
        "estimatedSalary": {"@type": "MonetaryAmount", "currency": "PKR",
                            "value": {"minValue": "150000", "maxValue": "250000", "unitText": "MONTH"}},
    },
}


def techmap_job(n: int = 1, **overrides: Any) -> dict[str, Any]:
    job = copy.deepcopy(_BASE)
    job["jsonLD"]["identifier"] = f"tm{n:022d}"
    job["jsonLD"]["url"] = f"https://pk.linkedin.com/jobs/view/python-developer-{n}"
    json_ld = overrides.pop("jsonLD", None)
    job.update(overrides)
    if json_ld:
        job["jsonLD"].update(json_ld)
    return job


def techmap_page(jobs: list[dict[str, Any]], *, page: int = 1, total: int | None = None) -> dict[str, Any]:
    return {
        "api": "Techmap.io Job Posting API",
        "apiVersion": "v2.7.2",
        "page": page,
        "pageSize": 10,
        "totalCount": total if total is not None else len(jobs),
        "result": jobs,
    }
