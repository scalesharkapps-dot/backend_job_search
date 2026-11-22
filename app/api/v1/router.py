"""Aggregate router for API version 1."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import require_app_access
from app.api.v1 import (
    applications,
    auth,
    cv,
    files,
    health,
    jobs,
    profile,
    recommendations,
    reports,
    saved_jobs,
    subscriptions,
)
from app.api.v1.admin import routes as admin_routes
from app.api.v1.employer import jobs as employer_jobs
from app.api.v1.employer import profiles as employer_profiles

api_router = APIRouter()

# Paywall: every route of these routers needs an active 3-day trial or a verified
# ``app_access`` subscription (app.api.deps.require_app_access -> 403 subscription_required).
PAID = [Depends(require_app_access)]

# ---- Always reachable (auth, account, billing, support, staff, public metadata) ----
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(subscriptions.router)
api_router.include_router(reports.router)  # abuse reports / support stay open
api_router.include_router(files.router)  # short-lived signed links minted by protected routes
api_router.include_router(admin_routes.router)  # admin-only; admins are exempt anyway
# /jobs: search, details and suggestions are protected per route (AppUser); /jobs/filters is public.
api_router.include_router(jobs.router)

# ---- Protected application functionality ----
api_router.include_router(profile.router, dependencies=PAID)
api_router.include_router(cv.router, dependencies=PAID)
api_router.include_router(recommendations.router, dependencies=PAID)
api_router.include_router(saved_jobs.router, dependencies=PAID)
api_router.include_router(applications.router, dependencies=PAID)
api_router.include_router(applications.interviews_router, dependencies=PAID)
api_router.include_router(employer_profiles.router, dependencies=PAID)
api_router.include_router(employer_jobs.router, dependencies=PAID)
