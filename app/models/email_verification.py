"""Persistent tracking of signup verification e-mails (resend limits).

One row per (normalised) e-mail address. The resend endpoint is unauthenticated
(an unverified user cannot sign in), so the e-mail is the stable key; the
Supabase Auth user id is recorded when Supabase returns it.

A *verification cycle* starts with the initial signup e-mail (or the first
resend for accounts created before tracking existed). Within one cycle at most
``EMAIL_VERIFICATION_MAX_RESENDS`` successful resends are allowed. Nothing a
client does (login, logout, new device, registering again) resets the counter;
only the configured cycle window elapsing starts a new cycle.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Integer, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin, utcnow


class EmailVerificationTracker(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "email_verification_trackers"

    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    # Supabase Auth user id (= users.id once the user first signs in). No FK: the
    # local users row is only created on the first session.
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    cycle: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    cycle_started_at: Mapped[datetime] = mapped_column(
        UTCDateTime, nullable=False, default=utcnow
    )
    # Initial signup e-mail of the current cycle (NULL when the cycle began with a resend).
    initial_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Successful resends (accepted by Supabase) in the current cycle.
    resend_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Last e-mail accepted by Supabase (initial or resend); drives the cooldown.
    last_sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_resend_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
