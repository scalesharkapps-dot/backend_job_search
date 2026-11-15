"""Profile endpoints (always scoped to the authenticated user)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, UploadFile

from app.api.deps import CurrentUser, DBSession
from app.core.config import get_settings
from app.core.exceptions import NotFoundError
from app.core.rate_limit import rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok
from app.schemas.profile import PhotoOut, ProfileOut, ProfilePatch, ProfileUpdate
from app.services import profile_service
from app.services.cv.validation import validate_image_upload
from app.services.storage import StorageBackend, get_storage

router = APIRouter(prefix="/profile", tags=["Profile"], responses=ERROR_RESPONSES)
Storage = Annotated[StorageBackend, Depends(get_storage)]


@router.get("/me", response_model=ApiResponse[ProfileOut], summary="Get my profile")
async def get_my_profile(user: CurrentUser, db: DBSession, storage: Storage) -> dict[str, Any]:
    """Returns the profile with a dynamically calculated `profile_completion` (0-100)."""
    return ok(await profile_service.profile_payload(db, user, storage))


@router.put(
    "/me",
    response_model=ApiResponse[ProfileOut],
    dependencies=[Depends(rate_limit("profile_write", "RATE_LIMIT_WRITE_PER_MINUTE"))],
    summary="Create or replace my profile",
)
async def put_my_profile(body: ProfileUpdate, user: CurrentUser, db: DBSession, storage: Storage) -> dict[str, Any]:
    """Full replacement of editable fields (omitted fields are cleared).

    `role`, `email` and `user_id` cannot be changed here (unknown fields → 422).
    """
    await profile_service.update_profile(db, user, body, partial=False)
    return ok(await profile_service.profile_payload(db, user, storage), "Profile saved")


@router.patch(
    "/me",
    response_model=ApiResponse[ProfileOut],
    dependencies=[Depends(rate_limit("profile_write", "RATE_LIMIT_WRITE_PER_MINUTE"))],
    summary="Partially update my profile",
)
async def patch_my_profile(body: ProfilePatch, user: CurrentUser, db: DBSession, storage: Storage) -> dict[str, Any]:
    await profile_service.update_profile(db, user, body, partial=True)
    return ok(await profile_service.profile_payload(db, user, storage), "Profile updated")


@router.post(
    "/me/photo",
    response_model=ApiResponse[PhotoOut],
    dependencies=[Depends(rate_limit("upload", "RATE_LIMIT_UPLOAD_PER_MINUTE"))],
    summary="Upload or replace my profile photo",
)
async def upload_photo(
    user: CurrentUser, db: DBSession, storage: Storage, file: Annotated[UploadFile, File(description="JPEG, PNG or WebP")]
) -> dict[str, Any]:
    """Validates the image, strips metadata by re-encoding, stores it privately and
    returns a short-lived signed URL."""
    settings = get_settings()
    image = await validate_image_upload(file, settings.image_max_bytes)
    profile = await profile_service.set_photo(db, user, storage, image)
    assert profile.profile_photo_path
    url = await storage.signed_url(settings.SUPABASE_STORAGE_MEDIA_BUCKET, profile.profile_photo_path, settings.SIGNED_URL_TTL_SECONDS)
    return ok({"profile_photo_url": url, "expires_in": settings.SIGNED_URL_TTL_SECONDS}, "Profile photo updated")


@router.delete("/me/photo", response_model=ApiResponse[None], summary="Remove my profile photo")
async def delete_photo(user: CurrentUser, db: DBSession, storage: Storage) -> dict[str, Any]:
    if not await profile_service.delete_photo(db, user, storage):
        raise NotFoundError("No profile photo to delete.", code="photo_not_found")
    return ok(None, "Profile photo removed")
