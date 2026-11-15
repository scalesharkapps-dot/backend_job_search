"""Sample JSearch payloads (shape taken from the documented /search-v2 response)."""

from __future__ import annotations

import time
from typing import Any


def raw_job(job_id: str = "abc123==", **overrides: Any) -> dict[str, Any]:
    job = {
        "job_id": job_id,
        "job_title": "Python Developer",
        "employer_name": "TechCorp",
        "employer_logo": "https://logo.example.com/techcorp.png",
        "employer_website": "https://techcorp.example.com",
        "job_publisher": "LinkedIn",
        "job_employment_type": "Full-time",
        "job_employment_types": ["FULLTIME"],
        "job_apply_link": "https://jobs.example.com/apply/123",
        "job_apply_is_direct": False,
        "apply_options": [
            {"publisher": "LinkedIn", "apply_link": "https://linkedin.example.com/jobs/1", "is_direct": False},
            {"publisher": "Evil", "apply_link": "javascript:alert(1)", "is_direct": True},
        ],
        "job_description": "We need a Python developer with FastAPI, PostgreSQL and Docker. 3+ years of experience required. AWS is a plus.",
        "job_is_remote": False,
        "job_posted_at": "2 days ago",
        "job_posted_at_timestamp": int(time.time()) - 2 * 86400,
        "job_posted_at_datetime_utc": None,
        "job_location": "Karachi, Sindh, PK",
        "job_city": "Karachi",
        "job_state": "Sindh",
        "job_country": "PK",
        "job_benefits": None,
        "job_google_link": "https://www.google.com/search?q=job",
        "job_salary": None,
        "job_min_salary": None,
        "job_max_salary": None,
        "job_salary_period": None,
        "job_highlights": {
            "Qualifications": ["3+ years of Python", "Experience with FastAPI"],
            "Responsibilities": ["Build APIs"],
        },
        "job_onet_soc": "15113200",
    }
    job.update(overrides)
    return job


def search_payload(jobs: list[dict[str, Any]], cursor: str | None = "next-cursor-1") -> dict[str, Any]:
    return {
        "status": "OK",
        "request_id": "req-1",
        "parameters": {"query": "python developer", "num_pages": 1},
        "data": {"jobs": jobs, "cursor": cursor},
    }
