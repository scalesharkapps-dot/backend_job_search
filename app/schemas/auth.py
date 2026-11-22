"""Authentication request/response schemas."""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.config import get_settings
from app.models.user import UserRole

_COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "qwerty123",
    "11111111", "iloveyou", "admin123", "welcome1", "letmein1", "abc12345",
}


def validate_password_policy(password: str) -> str:
    """Enforce the SharkHire password policy (Supabase may add its own rules)."""
    min_len = get_settings().PASSWORD_MIN_LENGTH
    problems = []
    if len(password) < min_len:
        problems.append(f"at least {min_len} characters")
    if len(password) > 72:
        problems.append("at most 72 characters")
    if not re.search(r"[a-z]", password):
        problems.append("a lowercase letter")
    if not re.search(r"[A-Z]", password):
        problems.append("an uppercase letter")
    if not re.search(r"\d", password):
        problems.append("a digit")
    if password.lower() in _COMMON_PASSWORDS:
        problems.append("not a commonly used password")
    if problems:
        raise ValueError("Password must contain " + ", ".join(problems))
    return password


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)
    full_name: str | None = Field(default=None, max_length=200)

    @field_validator("password")
    @classmethod
    def _policy(cls, value: str) -> str:
        return validate_password_policy(value)

    @field_validator("full_name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return value.strip() or None if value else None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(min_length=8, max_length=2048)


class EmailRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    """Set a new password.

    Supply either a Bearer recovery/access token in the Authorization header, or
    the ``token_hash`` from the recovery email link.
    """

    new_password: str = Field(min_length=1, max_length=128)
    token_hash: str | None = Field(default=None, min_length=8, max_length=512)

    @field_validator("new_password")
    @classmethod
    def _policy(cls, value: str) -> str:
        return validate_password_policy(value)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str | None = None
    role: UserRole
    is_active: bool
    email_verified: bool
    created_at: datetime | None = None


class SessionOut(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"  # noqa: S105 - OAuth token type label, not a secret
    expires_in: int | None = None
    expires_at: int | None = None
    user: UserOut


class SignupOut(BaseModel):
    user: UserOut | None = None
    session: SessionOut | None = None
    email_confirmation_required: bool


class ResendVerificationOut(BaseModel):
    remaining_attempts: int = Field(description="Successful resends still allowed in this verification cycle.")
    max_resends: int = Field(description="Resends allowed per verification cycle (initial e-mail not included).")
    cooldown_seconds: int = Field(description="Minimum wait before the next resend may be requested.")


class LogoutOut(BaseModel):
    revoked: bool
    detail: str


class MeOut(UserOut):
    session_expires_at: int
    aal: str | None = None
