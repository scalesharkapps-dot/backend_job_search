"""Profile management and profile-completion scoring."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models.profile import CVProcessingStatus, UserCV, UserProfile
from app.models.user import User
from app.schemas.profile import ProfileBase
from app.services.cv.validation import ValidatedFile
from app.services.storage import StorageBackend, StorageError, build_object_path

logger = logging.getLogger(__name__)

# Documented completion formula (sums to 100).
COMPLETION_WEIGHTS: dict[str, int] = {
    "full_name": 10,
    "headline": 10,
    "location": 10,
    "professional_summary": 15,
    "skills": 15,  # at least 3 skills
    "education": 10,  # at least one entry
    "experience": 15,  # at least one entry or years_of_experience set
    "cv_uploaded": 15,  # at least one successfully processed CV
}


def completion(profile: UserProfile | None, has_processed_cv: bool) -> tuple[int, dict[str, bool]]:
    p = profile
    checks = {
        "full_name": bool(p and p.full_name),
        "headline": bool(p and p.headline),
        "location": bool(p and (p.city or p.country or p.location)),
        "professional_summary": bool(p and p.professional_summary and len(p.professional_summary) >= 30),
        "skills": bool(p and len(p.skills or []) >= 3),
        "education": bool(p and p.education),
        "experience": bool(p and (p.experience or p.years_of_experience is not None)),
        "cv_uploaded": has_processed_cv,
    }
    score = sum(COMPLETION_WEIGHTS[k] for k, v in checks.items() if v)
    return score, checks


async def get_profile(db: AsyncSession, user: User) -> UserProfile | None:
    return await db.scalar(select(UserProfile).where(UserProfile.user_id == user.id))


async def get_or_create_profile(db: AsyncSession, user: User) -> UserProfile:
    profile = await get_profile(db, user)
    if profile is None:
        profile = UserProfile(
            user_id=user.id,
            full_name=user.full_name,
            education=[],
            experience=[],
            skills=[],
            certifications=[],
            preferred_job_titles=[],
            preferred_employment_types=[],
            preferred_locations=[],
        )
        db.add(profile)
        await db.flush()
    return profile


def _dump(value: Any) -> Any:
    if isinstance(value, list):
        return [v.model_dump(mode="json") if hasattr(v, "model_dump") else getattr(v, "value", v) for v in value]
    return value


async def update_profile(db: AsyncSession, user: User, body: ProfileBase, *, partial: bool) -> UserProfile:
    profile = await get_or_create_profile(db, user)
    fields = body.model_fields_set if partial else set(type(body).model_fields)
    list_fields = {"education", "experience", "skills", "certifications", "preferred_job_titles",
                   "preferred_employment_types", "preferred_locations"}
    for name in fields:
        value = getattr(body, name)
        if name in list_fields:
            value = _dump(value) if value is not None else []
        setattr(profile, name, value)
    if "full_name" in fields and body.full_name and user.full_name != body.full_name:
        user.full_name = body.full_name
    await db.commit()
    await db.refresh(profile)
    return profile


async def has_processed_cv(db: AsyncSession, user: User) -> bool:
    found = await db.scalar(
        select(UserCV.id).where(
            UserCV.user_id == user.id, UserCV.processing_status == CVProcessingStatus.COMPLETED
        ).limit(1)
    )
    return found is not None


async def profile_payload(db: AsyncSession, user: User, storage: StorageBackend) -> dict[str, Any]:
    profile = await get_profile(db, user)
    score, checks = completion(profile, await has_processed_cv(db, user))
    photo_url = None
    if profile and profile.profile_photo_path:
        try:
            photo_url = await storage.signed_url(
                get_settings().SUPABASE_STORAGE_MEDIA_BUCKET,
                profile.profile_photo_path,
                get_settings().SIGNED_URL_TTL_SECONDS,
            )
        except StorageError:
            logger.warning("Could not sign profile photo URL")
    data: dict[str, Any] = {
        "user_id": user.id,
        "email": user.email,
        "role": user.role.value,
        "profile_photo_url": photo_url,
        "profile_completion": score,
        "completion_breakdown": checks,
    }
    if profile:
        for name in ProfileBase.model_fields:
            data[name] = getattr(profile, name)
        data["created_at"] = profile.created_at
        data["updated_at"] = profile.updated_at
    else:
        data["full_name"] = user.full_name
    return data


async def set_photo(db: AsyncSession, user: User, storage: StorageBackend, image: ValidatedFile) -> UserProfile:
    bucket = get_settings().SUPABASE_STORAGE_MEDIA_BUCKET
    profile = await get_or_create_profile(db, user)
    new_path = build_object_path("avatars", user.id, image.extension)
    await storage.put(bucket, new_path, image.data, image.content_type)
    old_path = profile.profile_photo_path
    profile.profile_photo_path = new_path
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        await storage.delete(bucket, new_path)  # no orphan on DB failure
        raise
    if old_path:
        try:
            await storage.delete(bucket, old_path)
        except StorageError:
            logger.warning("Old profile photo could not be deleted (orphaned object)")
    return profile


async def delete_photo(db: AsyncSession, user: User, storage: StorageBackend) -> bool:
    profile = await get_profile(db, user)
    if not profile or not profile.profile_photo_path:
        return False
    await storage.delete(get_settings().SUPABASE_STORAGE_MEDIA_BUCKET, profile.profile_photo_path)
    profile.profile_photo_path = None
    await db.commit()
    return True
