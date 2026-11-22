"""App-access billing: the 3-day app-managed trial, store subscriptions (via RevenueCat),
the RevenueCat webhook event log and verified payment transactions.

SharkHire has no feature tiers: an active trial or an active ``app_access`` entitlement
grants the whole application. Every value here comes from the server clock or from
RevenueCat (API or authenticated webhook) -- never from the client.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JSONType, TimestampMixin, UTCDateTime, UUIDPrimaryKeyMixin
from app.models.user import str_enum


class TrialStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CONVERTED = "converted"  # the user bought a subscription


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    CANCELLED = "cancelled"  # auto-renew off; access continues until expiration
    BILLING_ISSUE = "billing_issue"  # renewal failed; may still be in a grace period
    PAUSED = "paused"  # Google Play pause
    EXPIRED = "expired"
    REFUNDED = "refunded"
    TRANSFERRED = "transferred"  # moved to another App User ID by RevenueCat
    REVOKED = "revoked"  # no longer reported for this customer by RevenueCat


class EventProcessingStatus(str, enum.Enum):
    RECEIVED = "received"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"  # retryable; RevenueCat received a non-2xx response
    IGNORED = "ignored"  # test events, unknown types, disallowed environment
    UNMATCHED = "unmatched"  # no local SharkHire user for the App User ID


class TransactionStatus(str, enum.Enum):
    COMPLETED = "completed"
    REFUNDED = "refunded"


class UserTrial(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "user_trials"
    __table_args__ = (
        CheckConstraint("trial_ends_at > trial_started_at", name="trial_window"),
    )

    # One trial per user, ever (unique): login/logout/reinstall can never create another.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    trial_started_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    trial_ends_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, index=True)
    trial_status: Mapped[TrialStatus] = mapped_column(
        str_enum(TrialStatus, "trial_status"), nullable=False, default=TrialStatus.ACTIVE
    )


class Subscription(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Current state of one store product for one user (one row per user + product).

    Lifecycle history lives in ``subscription_events`` and ``payment_history``.
    """

    __tablename__ = "subscriptions"
    __table_args__ = (
        UniqueConstraint("user_id", "product_id", name="uq_subscriptions_user_product"),
        Index("ix_subscriptions_user_entitlement", "user_id", "entitlement_active"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revenuecat_app_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    store: Mapped[str] = mapped_column(String(32), nullable=False)
    product_id: Mapped[str] = mapped_column(String(200), nullable=False)
    entitlement_id: Mapped[str | None] = mapped_column(String(100))
    subscription_status: Mapped[SubscriptionStatus] = mapped_column(
        str_enum(SubscriptionStatus, "subscription_status"), nullable=False, index=True
    )
    # True only when RevenueCat reports the configured entitlement as granted by this product.
    entitlement_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_trial: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_period_start: Mapped[datetime | None] = mapped_column(UTCDateTime)
    current_period_end: Mapped[datetime | None] = mapped_column(UTCDateTime)
    will_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancellation_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    expiration_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    billing_issue_detected_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    grace_period_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    refunded_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    original_transaction_id: Mapped[str | None] = mapped_column(String(200))
    latest_transaction_id: Mapped[str | None] = mapped_column(String(200))
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="PRODUCTION")
    # Ordering guard: event state older than this is never applied over newer state.
    last_event_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_synced_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_sync_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Set when webhook data was applied but the RevenueCat API could not confirm it yet.
    needs_reconcile: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class SubscriptionEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Every RevenueCat webhook event, stored before processing (idempotency + audit)."""

    __tablename__ = "subscription_events"

    revenuecat_event_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    app_user_id: Mapped[str | None] = mapped_column(String(256))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    environment: Mapped[str | None] = mapped_column(String(16))
    event_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Payload without subscriber attributes (they may hold personal data).
    event_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    processing_status: Mapped[EventProcessingStatus] = mapped_column(
        str_enum(EventProcessingStatus, "event_processing_status"),
        nullable=False,
        default=EventProcessingStatus.RECEIVED,
        index=True,
    )
    processing_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


class PaymentHistory(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Verified store transactions reported by RevenueCat. Never card or bank data."""

    __tablename__ = "payment_history"
    __table_args__ = (
        UniqueConstraint("store", "transaction_id", name="uq_payment_history_store_transaction"),
        Index("ix_payment_history_user_date", "user_id", "transaction_date"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("subscriptions.id", ondelete="SET NULL"), index=True
    )
    transaction_id: Mapped[str] = mapped_column(String(200), nullable=False)
    original_transaction_id: Mapped[str | None] = mapped_column(String(200))
    product_id: Mapped[str] = mapped_column(String(200), nullable=False)
    store: Mapped[str] = mapped_column(String(32), nullable=False)
    transaction_amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))
    currency: Mapped[str | None] = mapped_column(String(3))
    transaction_status: Mapped[TransactionStatus] = mapped_column(
        str_enum(TransactionStatus, "transaction_status"), nullable=False
    )
    transaction_date: Mapped[datetime | None] = mapped_column(UTCDateTime)
    environment: Mapped[str] = mapped_column(String(16), nullable=False, default="PRODUCTION")
    revenuecat_event_id: Mapped[str | None] = mapped_column(String(128))
