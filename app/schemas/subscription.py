"""Subscription / app-access request and response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

AccessStatus = Literal[
    "trial_active",
    "subscription_active",
    "subscription_cancelled_but_active",
    "billing_issue",
    "subscription_expired",
    "payment_required",
    "exempt",
]


class SubscriptionStatusOut(BaseModel):
    user_id: uuid.UUID
    access_allowed: bool
    access_status: AccessStatus
    trial_status: str | None = None
    trial_started_at: datetime | None = None
    trial_ends_at: datetime | None = None
    trial_remaining_seconds: int = 0
    subscription_status: str | None = None
    product_id: str | None = None
    plan: str | None = Field(default=None, description="international | pakistan | null (unrecognised product)")
    store: str | None = None
    current_period_end: datetime | None = None
    will_renew: bool | None = None
    entitlement_active: bool = False
    environment: str | None = None
    last_verified_at: datetime | None = Field(default=None, description="Last RevenueCat API confirmation")


class SubscriptionSyncOut(SubscriptionStatusOut):
    synced: bool = True
    revenuecat_customer_found: bool = True


class SubscriptionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    store: str
    product_id: str
    entitlement_id: str | None = None
    subscription_status: str
    entitlement_active: bool
    is_trial: bool
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    will_renew: bool
    cancellation_at: datetime | None = None
    expiration_at: datetime | None = None
    billing_issue_detected_at: datetime | None = None
    grace_period_expires_at: datetime | None = None
    refunded_at: datetime | None = None
    environment: str
    created_at: datetime
    updated_at: datetime


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    subscription_id: uuid.UUID | None = None
    transaction_id: str
    product_id: str
    store: str
    transaction_amount: Decimal | None = None
    currency: str | None = None
    transaction_status: str
    transaction_date: datetime | None = None
    environment: str


class PlanOut(BaseModel):
    plan_id: Literal["international", "pakistan"]
    name: str
    billing_interval: Literal["month"] = "month"
    reference_price: str = Field(description="Configured list price; the store shows the localized price")
    currency: str
    product_ids: list[str]
    entitlement_id: str
    offering_id: str


class PlansOut(BaseModel):
    plans: list[PlanOut]
    trial_days: int
    trial_type: Literal["app_managed"] = "app_managed"
    note: str


class WebhookAckOut(BaseModel):
    event_id: str
    status: str
    duplicate: bool


class WebhookEventIn(BaseModel):
    """Minimal structural validation; the full event is processed as RevenueCat sends it."""

    model_config = ConfigDict(extra="allow")

    id: str = Field(min_length=1, max_length=128)
    type: str = Field(min_length=1, max_length=64)


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="allow")

    api_version: str | None = None
    event: WebhookEventIn

    def event_dict(self) -> dict[str, Any]:
        return self.event.model_dump()
