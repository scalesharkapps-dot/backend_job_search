"""Admin/sensitive-action audit logging. Adds rows to the caller's transaction."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import request_id_ctx
from app.models.employer import AdminAuditLog

_SENSITIVE_KEYS = {"password", "token", "access_token", "refresh_token", "secret", "api_key", "storage_path"}


def _scrub(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    return {k: ("[REDACTED]" if k.lower() in _SENSITIVE_KEYS else v) for k, v in value.items()}


def record(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    action: str,
    target_type: str,
    target_id: uuid.UUID | str,
    previous: dict[str, Any] | None = None,
    new: dict[str, Any] | None = None,
    reason: str | None = None,
) -> AdminAuditLog:
    entry = AdminAuditLog(
        actor_user_id=actor_id,
        action=action,
        target_type=target_type,
        target_id=str(target_id),
        previous_value=_scrub(previous),
        new_value=_scrub(new),
        reason=reason,
        request_id=request_id_ctx.get(),
    )
    db.add(entry)
    return entry
