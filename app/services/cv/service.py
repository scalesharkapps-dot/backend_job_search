"""CV lifecycle: upload, processing, retrieval, primary selection, deletion.

Ownership is enforced in every lookup (``user_id`` filter), so another user's CV
id behaves exactly like a non-existent one (404, no IDOR leak).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.config import get_settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationAppError
from app.models.base import utcnow
from app.models.profile import CVExtractedData, CVFileType, CVProcessingStatus, UserCV
from app.models.user import User
from app.schemas.profile import ExtractedDataUpdate
from app.services.cv.extraction import extract_text
from app.services.cv.parser import parse_cv_text
from app.services.cv.validation import ValidatedFile
from app.services.storage import StorageBackend, StorageError, build_object_path

logger = logging.getLogger(__name__)

PROCESSING_STALE_AFTER = timedelta(minutes=5)


def _bucket() -> str:
    return get_settings().SUPABASE_STORAGE_CV_BUCKET


async def get_user_cv(db: AsyncSession, user_id: uuid.UUID, cv_id: uuid.UUID, *, with_data: bool = False) -> UserCV:
    stmt = select(UserCV).where(UserCV.id == cv_id, UserCV.user_id == user_id)
    if with_data:
        # populate_existing: the CV may already be in the identity map without its extracted data loaded.
        stmt = stmt.options(selectinload(UserCV.extracted)).execution_options(populate_existing=True)
    cv = await db.scalar(stmt)
    if cv is None:
        raise NotFoundError("CV not found.", code="cv_not_found")
    return cv


async def list_user_cvs(db: AsyncSession, user_id: uuid.UUID) -> list[UserCV]:
    rows = await db.scalars(
        select(UserCV).where(UserCV.user_id == user_id).order_by(UserCV.is_primary.desc(), UserCV.created_at.desc())
    )
    return list(rows)


async def get_primary_cv(db: AsyncSession, user_id: uuid.UUID) -> UserCV | None:
    return await db.scalar(
        select(UserCV)
        .where(UserCV.user_id == user_id)
        .options(selectinload(UserCV.extracted))
        .order_by(UserCV.is_primary.desc(), UserCV.created_at.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


async def _run_extraction(cv: UserCV, data: bytes) -> tuple[CVProcessingStatus, dict[str, Any], str | None, int | None]:
    settings = get_settings()
    result = await asyncio.to_thread(extract_text, data, cv.file_type.value, settings.CV_MAX_PDF_PAGES)
    fields: dict[str, Any] = {"extracted_text": result.text or None, "extraction_metadata": dict(result.metadata)}
    if result.status == CVProcessingStatus.COMPLETED:
        parsed = await asyncio.to_thread(parse_cv_text, result.text)
        fields.update(
            extracted_name=parsed.name,
            extracted_email=parsed.email,
            extracted_phone=parsed.phone,
            skills=parsed.skills,
            unrecognized_skills=parsed.unrecognized_skills,
            work_experience=parsed.work_experience,
            education=parsed.education,
            certifications=parsed.certifications,
            job_titles=parsed.job_titles,
            years_of_experience=parsed.years_of_experience,
        )
        fields["extraction_metadata"].update(parsed.metadata)
    return result.status, fields, result.error, result.page_count


async def _apply_processing(db: AsyncSession, cv: UserCV, data: bytes) -> UserCV:
    try:
        status, fields, error, pages = await _run_extraction(cv, data)
    except Exception:
        logger.exception("CV processing crashed")
        status, fields, error, pages = CVProcessingStatus.FAILED, {}, "Processing failed unexpectedly.", None

    existing = await db.scalar(select(CVExtractedData).where(CVExtractedData.cv_id == cv.id))
    if fields:
        empty = {"skills": [], "unrecognized_skills": [], "work_experience": [], "education": [],
                 "certifications": [], "job_titles": [], "extraction_metadata": {}}
        values = {**empty, "extracted_name": None, "extracted_email": None, "extracted_phone": None,
                  "years_of_experience": None, **fields, "is_user_confirmed": False, "confirmed_at": None}
        if existing is None:
            db.add(CVExtractedData(cv_id=cv.id, **values))
        else:
            for key, value in values.items():
                setattr(existing, key, value)
    cv.processing_status = status
    cv.extraction_error = error
    cv.page_count = pages
    cv.processed_at = utcnow()
    await db.commit()
    await db.refresh(cv)
    return cv


async def upload_cv(
    db: AsyncSession, user: User, storage: StorageBackend, file: ValidatedFile, *, replace: bool = False
) -> tuple[UserCV, int, uuid.UUID | None]:
    """Store, record and process a CV. Returns (cv, skills_found, replaced_cv_id).

    With ``replace=True`` the new CV becomes primary and the previous primary CV
    (file + records) is removed only AFTER the new one is safely stored and recorded.
    """
    settings = get_settings()
    count = await db.scalar(select(func.count()).select_from(UserCV).where(UserCV.user_id == user.id)) or 0
    previous = None
    if replace:
        previous = await db.scalar(
            select(UserCV).where(UserCV.user_id == user.id).order_by(UserCV.is_primary.desc(), UserCV.created_at.desc()).limit(1)
        )
    if count - (1 if previous else 0) >= settings.CV_MAX_FILES_PER_USER:
        raise ConflictError(
            f"You can store at most {settings.CV_MAX_FILES_PER_USER} CVs. Delete one first.",
            code="cv_limit_reached",
        )
    duplicate = await db.scalar(
        select(UserCV.id).where(UserCV.user_id == user.id, UserCV.file_hash == file.sha256)
    )
    if duplicate:
        raise ConflictError(
            "You have already uploaded this file.", code="duplicate_cv", details={"cv_id": str(duplicate)}
        )

    path = build_object_path("cvs", user.id, file.extension)
    await storage.put(_bucket(), path, file.data, file.content_type)
    cv = UserCV(
        user_id=user.id,
        original_filename=file.original_filename,
        storage_path=path,
        file_type=CVFileType(file.extension),
        file_size=len(file.data),
        file_hash=file.sha256,
        processing_status=CVProcessingStatus.PROCESSING,
        is_primary=count == 0 or replace,
    )
    try:
        if replace:
            # One primary per user (unique index): clear the flag before inserting the new primary.
            await db.execute(update(UserCV).where(UserCV.user_id == user.id).values(is_primary=False))
        db.add(cv)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        await _safe_delete(storage, path)
        raise ConflictError("You have already uploaded this file.", code="duplicate_cv") from None
    except Exception:
        await db.rollback()
        await _safe_delete(storage, path)  # no orphaned files on failure
        raise
    cv = await _apply_processing(db, cv, file.data)
    skills = await db.scalar(select(CVExtractedData.skills).where(CVExtractedData.cv_id == cv.id))
    replaced_id = None
    if previous is not None:
        replaced_id = previous.id
        try:
            await delete_cv(db, user, storage, previous.id)
        except (StorageError, NotFoundError):
            # The new CV is already stored and primary; the old one stays listed so the user can retry.
            logger.warning("Replacing CV: previous CV could not be removed")
            replaced_id = None
        await db.refresh(cv)
    return cv, len(skills or []), replaced_id


def summary_payload(cv: UserCV) -> dict[str, Any] | None:
    """Review structure (skills/experience/education/qualifications/job_titles) or None."""
    data = cv.extracted
    if data is None or cv.processing_status != CVProcessingStatus.COMPLETED:
        return None
    return {
        "skills": list(data.skills or []),
        "experience": list(data.work_experience or []),
        "education": list(data.education or []),
        "qualifications": list(data.certifications or []),
        "job_titles": list(data.job_titles or []),
        "years_of_experience": data.years_of_experience,
        "is_user_confirmed": data.is_user_confirmed,
    }


async def _safe_delete(storage: StorageBackend, path: str) -> None:
    try:
        await storage.delete(_bucket(), path)
    except StorageError:
        logger.warning("Cleanup of stored CV failed (orphaned object)")


async def reprocess_cv(db: AsyncSession, user: User, storage: StorageBackend, cv_id: uuid.UUID) -> UserCV:
    cv = await get_user_cv(db, user.id, cv_id)
    if cv.processing_status == CVProcessingStatus.PROCESSING and cv.updated_at > utcnow() - PROCESSING_STALE_AFTER:
        raise ConflictError("This CV is already being processed.", code="cv_processing")
    # Atomic claim so concurrent retries do not both run.
    claimed = await db.execute(
        update(UserCV)
        .where(UserCV.id == cv.id, UserCV.updated_at == cv.updated_at)
        .values(processing_status=CVProcessingStatus.PROCESSING, updated_at=utcnow())
    )
    if claimed.rowcount != 1:
        await db.rollback()
        raise ConflictError("This CV is already being processed.", code="cv_processing")
    await db.commit()
    await db.refresh(cv)
    try:
        data = await storage.get(_bucket(), cv.storage_path)
    except StorageError:
        cv.processing_status = CVProcessingStatus.FAILED
        cv.extraction_error = "The stored file could not be read."
        await db.commit()
        raise
    return await _apply_processing(db, cv, data)


async def set_primary(db: AsyncSession, user: User, cv_id: uuid.UUID) -> UserCV:
    cv = await get_user_cv(db, user.id, cv_id)
    await db.execute(update(UserCV).where(UserCV.user_id == user.id, UserCV.id != cv.id).values(is_primary=False))
    await db.flush()
    cv.is_primary = True
    await db.commit()
    await db.refresh(cv)
    return cv


async def delete_cv(db: AsyncSession, user: User, storage: StorageBackend, cv_id: uuid.UUID) -> None:
    """Delete DB records (cascade) and the stored file atomically.

    The DB deletion is flushed first, the file deleted, then the transaction
    committed; a storage failure rolls back so records never point at missing files.
    """
    cv = await get_user_cv(db, user.id, cv_id)
    was_primary, path = cv.is_primary, cv.storage_path
    await db.delete(cv)
    await db.flush()
    try:
        await storage.delete(_bucket(), path)
    except StorageError:
        await db.rollback()
        raise
    await db.commit()
    if was_primary:
        newest = await db.scalar(
            select(UserCV).where(UserCV.user_id == user.id).order_by(UserCV.created_at.desc()).limit(1)
        )
        if newest:
            newest.is_primary = True
            await db.commit()


async def signed_download(storage: StorageBackend, cv: UserCV) -> dict[str, Any]:
    ttl = get_settings().SIGNED_URL_TTL_SECONDS
    url = await storage.signed_url(_bucket(), cv.storage_path, ttl, cv.original_filename)
    return {"url": url, "expires_in": ttl, "filename": cv.original_filename}


async def update_extracted(db: AsyncSession, user: User, cv_id: uuid.UUID, body: ExtractedDataUpdate) -> UserCV:
    cv = await get_user_cv(db, user.id, cv_id, with_data=True)
    data = cv.extracted
    if data is None:
        raise ValidationAppError("This CV has no extracted data to review yet.", code="cv_not_processed")
    for name in body.model_fields_set:
        value = getattr(body, name)
        if isinstance(value, list):
            value = [v.model_dump(mode="json") if hasattr(v, "model_dump") else v for v in value]
        setattr(data, name, value if value is not None else ([] if name not in {"extracted_name", "years_of_experience"} else None))
    data.is_user_confirmed = True
    data.confirmed_at = utcnow()
    await db.commit()
    return cv


def extracted_payload(cv: UserCV) -> dict[str, Any]:
    data = cv.extracted
    payload: dict[str, Any] = {"cv_id": cv.id, "processing_status": cv.processing_status}
    if data is not None:
        for field in ("extracted_name", "extracted_email", "extracted_phone", "skills", "unrecognized_skills",
                      "work_experience", "education", "certifications", "job_titles", "years_of_experience",
                      "extraction_metadata", "is_user_confirmed", "confirmed_at"):
            payload[field] = getattr(data, field)
        payload["text_preview"] = (data.extracted_text or "")[:500] or None
    return payload
