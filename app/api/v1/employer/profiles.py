"""Employer registration, company profile, logo and verification."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, UploadFile

from app.api.deps import CurrentUser, DBSession
from app.core.config import get_settings
from app.core.permissions import Employer
from app.core.rate_limit import rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok
from app.schemas.employer import (
    EmployerProfileIn,
    EmployerProfileOut,
    EmployerPublicOut,
    VerificationRequestOut,
    VerificationStateOut,
    VerificationSubmit,
)
from app.services import employer_service as es
from app.services.cv.validation import validate_image_upload
from app.services.storage import StorageBackend, get_storage

router = APIRouter(prefix="/employers", tags=["Employers"], responses=ERROR_RESPONSES)
Storage = Annotated[StorageBackend, Depends(get_storage)]


async def _profile_out(employer: Any, storage: StorageBackend) -> dict[str, Any]:
    data = EmployerProfileOut.model_validate(employer).model_dump()
    data["logo_url"] = await es.logo_url(storage, employer)
    return data


@router.post(
    "/register",
    response_model=ApiResponse[EmployerProfileOut],
    status_code=201,
    dependencies=[Depends(rate_limit("employer_register", "RATE_LIMIT_AUTH_PER_MINUTE"))],
    summary="Register a company (becomes an employer account)",
)
async def register(body: EmployerProfileIn, user: CurrentUser, db: DBSession, storage: Storage) -> dict[str, Any]:
    """Creates the company profile and switches the account role to `employer`.
    The company starts `unverified`; submit verification before publishing jobs."""
    employer = await es.register(db, user, body)
    return ok(await _profile_out(employer, storage), "Company registered")


@router.get("/me", response_model=ApiResponse[EmployerProfileOut], summary="My company profile")
async def get_me(ctx: Employer, storage: Storage) -> dict[str, Any]:
    return ok(await _profile_out(ctx.employer, storage))


@router.put("/me", response_model=ApiResponse[EmployerProfileOut], summary="Update my company profile")
async def put_me(body: EmployerProfileIn, ctx: Employer, db: DBSession, storage: Storage) -> dict[str, Any]:
    es.ensure_active(ctx.employer)
    employer = await es.update_profile(db, ctx.employer, body)
    return ok(await _profile_out(employer, storage), "Company profile updated")


@router.post("/me/logo", response_model=ApiResponse[EmployerProfileOut],
             dependencies=[Depends(rate_limit("upload", "RATE_LIMIT_UPLOAD_PER_MINUTE"))], summary="Upload company logo")
async def upload_logo(ctx: Employer, db: DBSession, storage: Storage,
                      file: Annotated[UploadFile, File(description="JPEG, PNG or WebP")]) -> dict[str, Any]:
    es.ensure_active(ctx.employer)
    image = await validate_image_upload(file, get_settings().image_max_bytes)
    await es.set_logo(db, ctx.employer, storage, image)
    return ok(await _profile_out(ctx.employer, storage), "Logo updated")


@router.delete("/me/logo", response_model=ApiResponse[None], summary="Remove company logo")
async def delete_logo(ctx: Employer, db: DBSession, storage: Storage) -> dict[str, Any]:
    await es.delete_logo(db, ctx.employer, storage)
    return ok(None, "Logo removed")


@router.get("/me/verification", response_model=ApiResponse[VerificationStateOut], summary="Verification status")
async def get_verification(ctx: Employer, db: DBSession) -> dict[str, Any]:
    latest = await es.latest_verification(db, ctx.employer)
    return ok({
        "verification_status": ctx.employer.verification_status,
        "can_submit": es.can_submit_verification(ctx.employer),
        "latest_request": VerificationRequestOut.model_validate(latest) if latest else None,
    })


@router.post("/me/verification", response_model=ApiResponse[VerificationRequestOut], status_code=201,
             summary="Submit (or resubmit) verification")
async def submit_verification(body: VerificationSubmit, ctx: Employer, db: DBSession) -> dict[str, Any]:
    """Allowed when the company is `unverified` or `rejected`. An administrator
    reviews the request; nothing is verified automatically."""
    request = await es.submit_verification(db, ctx.user, ctx.employer, body)
    return ok(VerificationRequestOut.model_validate(request), "Verification submitted")


@router.get("/{employer_id}", response_model=ApiResponse[EmployerPublicOut], summary="Public company profile")
async def public(employer_id: uuid.UUID, user: CurrentUser, db: DBSession, storage: Storage) -> dict[str, Any]:
    """Public company page for any signed-in user. Private contact details are never included."""
    employer, open_jobs = await es.public_profile(db, employer_id)
    return ok({
        "id": employer.id, "company_name": employer.company_name, "description": employer.description,
        "industry": employer.industry, "website": employer.website, "company_size": employer.company_size,
        "headquarters": employer.headquarters, "country": employer.country, "social_links": employer.social_links,
        "logo_url": await es.logo_url(storage, employer), "is_verified": employer.verification_status.value == "verified",
        "open_jobs": open_jobs,
    })
