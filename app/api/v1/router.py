"""Aggregate router for API version 1."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import applications, auth, cv, files, health, jobs, profile, recommendations, reports, saved_jobs
from app.api.v1.admin import routes as admin_routes
from app.api.v1.employer import jobs as employer_jobs
from app.api.v1.employer import profiles as employer_profiles

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(profile.router)
api_router.include_router(cv.router)
api_router.include_router(files.router)
api_router.include_router(jobs.router)
api_router.include_router(recommendations.router)
api_router.include_router(saved_jobs.router)
api_router.include_router(applications.router)
api_router.include_router(applications.interviews_router)
api_router.include_router(employer_profiles.router)
api_router.include_router(employer_jobs.router)
api_router.include_router(reports.router)
api_router.include_router(admin_routes.router)
