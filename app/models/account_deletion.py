"""Tombstones for deleted accounts.

Supabase access tokens are stateless and stay valid until they expire (up to the
project's JWT expiry) even after the Auth user is deleted. Without this table the
first request carrying such a token would silently re-create an empty local user.
Only the user id and time are kept -- no personal data.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, UTCDateTime, utcnow


class DeletedAccount(Base):
    __tablename__ = "deleted_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
