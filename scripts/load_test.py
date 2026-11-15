"""Lightweight load test for a LOCAL/STAGING SharkHire instance (never production).

Seeds N synthetic SharkHire-irrelevant JSearch-style jobs plus one user/profile
directly in the configured database, mints an HS256 token with the instance's
test JWT secret, then fires concurrent requests and reports latency percentiles.

    python scripts/load_test.py --base-url http://127.0.0.1:8001 \
        --db-url postgresql://... --jwt-secret <test secret> --jobs 500 --requests 300 --concurrency 20

The JSearch provider is NOT called (only stored jobs are used).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
from datetime import timedelta

import httpx
import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, ".")
from app.core.database import normalize_database_url
from app.models import Job, User, UserProfile
from app.models.base import utcnow
from app.models.job import JobSource

TITLES = ["Python Developer", "Backend Engineer", "Data Analyst", "ML Engineer", "Frontend Developer", "DevOps Engineer"]
SKILLS = "Python, FastAPI, PostgreSQL, Docker, AWS, React, SQL, Kubernetes, Machine Learning"


async def seed(db_url: str, jobs: int) -> uuid.UUID:
    engine = create_async_engine(normalize_database_url(db_url))
    user_id = uuid.uuid4()
    async with async_sessionmaker(engine)() as db:
        db.add(User(id=user_id, email=f"load-{user_id.hex[:8]}@example.com", full_name="Load Test"))
        await db.flush()
        db.add(UserProfile(user_id=user_id, skills=["Python", "FastAPI", "PostgreSQL", "Docker"], current_job_title="Python Developer",
                           years_of_experience=4, city="Karachi", country="PK", education=[], experience=[], certifications=[],
                           preferred_job_titles=[], preferred_employment_types=["FULLTIME"], preferred_locations=[]))
        now = utcnow()
        for i in range(jobs):
            db.add(Job(source=JobSource.JSEARCH, external_id=f"load-{uuid.uuid4().hex}", title=TITLES[i % len(TITLES)],
                       company_name=f"Company {i}", description=f"We need {SKILLS.split(', ')[i % 9]} and {SKILLS}. {i % 6}+ years of experience.",
                       city="Karachi" if i % 2 else "Lahore", country="pk", location="Pakistan", is_remote=i % 5 == 0,
                       employment_type="FULLTIME", employment_types=["FULLTIME"], posted_at=now - timedelta(days=i % 30),
                       last_seen_at=now, highlights={}, required_skills=[], apply_options=[], provider_metadata={},
                       preferred_skills=[], benefits=[]))
        await db.commit()
    await engine.dispose()
    return user_id


async def run(base: str, token: str, path: str, total: int, concurrency: int) -> dict[str, float]:
    latencies: list[float] = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30) as client:
        async def one() -> None:
            nonlocal errors
            async with sem:
                start = time.perf_counter()
                try:
                    r = await client.get(path)
                    if r.status_code >= 400:
                        errors += 1
                except httpx.HTTPError:
                    errors += 1
                latencies.append((time.perf_counter() - start) * 1000)

        wall = time.perf_counter()
        await asyncio.gather(*(one() for _ in range(total)))
        wall = time.perf_counter() - wall
    lat = sorted(latencies)
    return {"requests": total, "errors": errors, "rps": round(total / wall, 1), "p50_ms": round(statistics.median(lat), 1),
            "p95_ms": round(lat[int(len(lat) * 0.95) - 1], 1), "max_ms": round(lat[-1], 1)}


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base-url", required=True)
    p.add_argument("--db-url", required=True)
    p.add_argument("--jwt-secret", required=True)
    p.add_argument("--issuer", default="https://placeholder.supabase.co/auth/v1")
    p.add_argument("--jobs", type=int, default=500)
    p.add_argument("--requests", type=int, default=300)
    p.add_argument("--concurrency", type=int, default=20)
    a = p.parse_args()
    user_id = await seed(a.db_url, a.jobs)
    token = jwt.encode({"sub": str(user_id), "aud": "authenticated", "role": "authenticated", "iss": a.issuer,
                        "exp": int(time.time()) + 3600}, a.jwt_secret, algorithm="HS256")
    for path, n in [("/health", a.requests), ("/api/v1/health/database", a.requests), ("/api/v1/profile/me", a.requests),
                    ("/api/v1/applications", a.requests), ("/api/v1/recommendations/for-me?page_size=20", max(20, a.requests // 10))]:
        print(f"{path:<48} {await run(a.base_url, token, path, n, a.concurrency)}")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
