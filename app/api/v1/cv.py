"""CV management endpoints (owner-only)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Query, UploadFile

from app.api.deps import CurrentUser, DBSession
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, ValidationAppError
from app.core.rate_limit import rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok
from app.models.profile import CVProcessingStatus
from app.schemas.profile import (
    ApplyToProfileRequest,
    CVDownloadOut,
    CVMeOut,
    CVOut,
    CVStatusOut,
    CVUploadOut,
    ExtractedDataOut,
    ExtractedDataUpdate,
    ProfileOut,
)
from app.services import profile_service
from app.services.cv import service as cv_service
from app.services.cv.validation import validate_cv_upload
from app.services.storage import StorageBackend, get_storage

router = APIRouter(prefix="/cv", tags=["CV"], responses=ERROR_RESPONSES)
Storage = Annotated[StorageBackend, Depends(get_storage)]

_UPLOAD_ERRORS = {
    409: {"model": ApiResponse[None], "description": "Duplicate file or CV limit reached"},
    413: {"model": ApiResponse[None], "description": "File too large"},
    415: {"model": ApiResponse[None], "description": "Unsupported or spoofed file type"},
}


@router.post(
    "/upload",
    response_model=ApiResponse[CVUploadOut],
    status_code=201,
    responses=_UPLOAD_ERRORS,
    dependencies=[Depends(rate_limit("upload", "RATE_LIMIT_UPLOAD_PER_MINUTE"))],
    summary="Upload a CV (PDF or DOCX)",
)
async def upload_cv(
    user: CurrentUser,
    db: DBSession,
    storage: Storage,
    file: Annotated[UploadFile, File(description="PDF or DOCX, max CV_MAX_FILE_SIZE_MB")],
    replace: Annotated[
        bool, Query(description="Make this the primary CV and delete the previous primary CV")
    ] = False,
) -> dict[str, Any]:
    """Validates extension, MIME type and actual file signature, stores the file
    privately, then extracts text and structured data synchronously.

    `processing_status` is one of: completed, no_text, ocr_required, failed.
    The first CV uploaded becomes the primary CV. `extracted_data` (skills,
    experience, education, qualifications, job_titles) is returned for user review
    when processing completed; it is not applied to the profile until the user
    confirms it (`PATCH /cv/{id}/extracted-data`, `POST /cv/{id}/apply-to-profile`).
    If extraction fails the file and record are kept (`processing_status=failed`)
    and the existing profile is never modified.
    """
    validated = await validate_cv_upload(file, get_settings().cv_max_bytes)
    user_id = user.id  # captured up front: a rollback inside upload_cv expires ORM objects
    cv, skills_found, replaced_id = await cv_service.upload_cv(db, user, storage, validated, replace=replace)
    cv = await cv_service.get_user_cv(db, user_id, cv.id, with_data=True)
    data = CVOut.model_validate(cv).model_dump()
    data["skills_found"] = skills_found
    data["extracted_data"] = cv_service.summary_payload(cv)
    data["replaced_cv_id"] = replaced_id
    message = "CV uploaded successfully" if cv.processing_status.value == "completed" else (
        "CV uploaded, but its information could not be extracted automatically"
    )
    return ok(data, message)


@router.get("", response_model=ApiResponse[list[CVOut]], summary="List my CVs")
async def list_cvs(user: CurrentUser, db: DBSession) -> dict[str, Any]:
    return ok([CVOut.model_validate(c) for c in await cv_service.list_user_cvs(db, user.id)])


# ---- "current CV" convenience routes: declared before /{cv_id} so "me"/"status" are not parsed as ids ----
@router.get("/me", response_model=ApiResponse[CVMeOut], summary="Get my current (primary) CV and its extracted data")
async def get_my_cv(user: CurrentUser, db: DBSession) -> dict[str, Any]:
    cv = await cv_service.get_primary_cv(db, user.id)
    if cv is None:
        return ok({"cv": None, "extracted_data": None}, "No CV uploaded yet")
    return ok({"cv": CVOut.model_validate(cv), "extracted_data": cv_service.summary_payload(cv)})


@router.get("/status", response_model=ApiResponse[CVStatusOut], summary="Get my CV upload/processing status")
async def get_my_cv_status(user: CurrentUser, db: DBSession) -> dict[str, Any]:
    cvs = await cv_service.list_user_cvs(db, user.id)
    cv = await cv_service.get_primary_cv(db, user.id)
    if cv is None:
        return ok({"has_cv": False, "total_cvs": 0})
    return ok(
        {
            "has_cv": True,
            "cv_id": cv.id,
            "processing_status": cv.processing_status,
            "extraction_error": cv.extraction_error,
            "is_reviewed": bool(cv.extracted and cv.extracted.is_user_confirmed),
            "total_cvs": len(cvs),
            "uploaded_at": cv.created_at,
            "processed_at": cv.processed_at,
        }
    )


@router.delete("/me", response_model=ApiResponse[None], summary="Delete my current (primary) CV")
async def delete_my_cv(user: CurrentUser, db: DBSession, storage: Storage) -> dict[str, Any]:
    """Deletes the primary CV file and its extracted data. The most recent remaining CV (if any)
    becomes primary. Profile data the user already applied from the CV is kept."""
    cv = await cv_service.get_primary_cv(db, user.id)
    if cv is None:
        raise NotFoundError("You have not uploaded a CV.", code="cv_not_found")
    await cv_service.delete_cv(db, user, storage, cv.id)
    return ok(None, "CV deleted")


@router.get("/{cv_id}", response_model=ApiResponse[CVOut], summary="Get CV metadata")
async def get_cv(cv_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    return ok(CVOut.model_validate(await cv_service.get_user_cv(db, user.id, cv_id)))


@router.get("/{cv_id}/download", response_model=ApiResponse[CVDownloadOut], summary="Get a short-lived download URL")
async def download_cv(cv_id: uuid.UUID, user: CurrentUser, db: DBSession, storage: Storage) -> dict[str, Any]:
    cv = await cv_service.get_user_cv(db, user.id, cv_id)
    return ok(await cv_service.signed_download(storage, cv))


@router.get("/{cv_id}/extracted-data", response_model=ApiResponse[ExtractedDataOut], summary="Get extracted CV data")
async def get_extracted(cv_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    cv = await cv_service.get_user_cv(db, user.id, cv_id, with_data=True)
    return ok(cv_service.extracted_payload(cv))


@router.patch(
    "/{cv_id}/extracted-data",
    response_model=ApiResponse[ExtractedDataOut],
    summary="Review/correct extracted data (marks it user-confirmed)",
)
async def update_extracted(cv_id: uuid.UUID, body: ExtractedDataUpdate, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    await cv_service.update_extracted(db, user, cv_id, body)
    cv = await cv_service.get_user_cv(db, user.id, cv_id, with_data=True)
    return ok(cv_service.extracted_payload(cv), "Extracted data confirmed")


@router.post(
    "/{cv_id}/apply-to-profile",
    response_model=ApiResponse[ProfileOut],
    summary="Copy extracted CV data into my profile",
)
async def apply_to_profile(
    cv_id: uuid.UUID, body: ApplyToProfileRequest, user: CurrentUser, db: DBSession, storage: Storage
) -> dict[str, Any]:
    """Explicit, user-initiated copy of extracted data into the (user-confirmed) profile.
    By default only empty profile fields are filled."""
    cv = await cv_service.get_user_cv(db, user.id, cv_id, with_data=True)
    data = cv.extracted
    if cv.processing_status != CVProcessingStatus.COMPLETED or data is None:
        raise ValidationAppError("This CV has not been processed successfully.", code="cv_not_processed")
    profile = await profile_service.get_or_create_profile(db, user)
    source = {
        "skills": data.skills,
        "education": [
            {k: e.get(k) for k in ("degree", "institution", "start_year", "end_year")} for e in data.education
        ],
        "experience": [
            {"title": e.get("title"), "company": e.get("company"), "start_date": e.get("start_date"),
             "end_date": e.get("end_date"), "is_current": e.get("is_current", False), "description": None}
            for e in data.work_experience if e.get("title")
        ],
        "certifications": data.certifications,
        "years_of_experience": data.years_of_experience,
        "current_job_title": data.job_titles[0] if data.job_titles else None,
        "full_name": data.extracted_name,
        "phone": data.extracted_phone,
    }
    for name in body.fields:
        value = source[name]
        if value in (None, [], ""):
            continue
        if body.overwrite or getattr(profile, name) in (None, [], ""):
            setattr(profile, name, value)
    await db.commit()
    return ok(await profile_service.profile_payload(db, user, storage), "Profile updated from CV")


@router.post(
    "/{cv_id}/process",
    response_model=ApiResponse[CVOut],
    dependencies=[Depends(rate_limit("upload", "RATE_LIMIT_UPLOAD_PER_MINUTE"))],
    summary="Re-run CV processing",
)
async def process_cv(cv_id: uuid.UUID, user: CurrentUser, db: DBSession, storage: Storage) -> dict[str, Any]:
    """Re-extracts the stored file. Previously confirmed extracted data is replaced
    and must be reviewed again."""
    cv = await cv_service.reprocess_cv(db, user, storage, cv_id)
    return ok(CVOut.model_validate(cv), "CV processed")


@router.post("/{cv_id}/primary", response_model=ApiResponse[CVOut], summary="Make this my primary CV")
async def make_primary(cv_id: uuid.UUID, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    return ok(CVOut.model_validate(await cv_service.set_primary(db, user, cv_id)), "Primary CV updated")


@router.delete("/{cv_id}", response_model=ApiResponse[None], summary="Delete a CV")
async def delete_cv(cv_id: uuid.UUID, user: CurrentUser, db: DBSession, storage: Storage) -> dict[str, Any]:
    """Deletes the stored file and its extracted data. If it was primary, the most
    recent remaining CV becomes primary."""
    await cv_service.delete_cv(db, user, storage, cv_id)
    return ok(None, "CV deleted")
