"""Authentication endpoints backed by Supabase Auth."""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.api.deps import AuthContext, DBSession, bearer_scheme, get_auth_context
from app.core.exceptions import AuthenticationError, BadRequestError, ExternalServiceError
from app.core.rate_limit import rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok
from app.schemas.auth import (
    EmailRequest,
    LoginRequest,
    LogoutOut,
    MeOut,
    RefreshRequest,
    ResetPasswordRequest,
    SessionOut,
    SignupOut,
    SignupRequest,
    UserOut,
)
from app.services.supabase_auth import SupabaseAuthClient, get_supabase_auth
from app.services.user_service import sync_user, user_from_supabase_payload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"], responses=ERROR_RESPONSES)

Auth = Annotated[SupabaseAuthClient, Depends(get_supabase_auth)]

_GENERIC_EMAIL_MSG = "If an account exists for this email, a message has been sent."


async def _session_from_payload(db: DBSession, payload: dict[str, Any]) -> dict[str, Any]:
    user_payload = payload.get("user")
    if not payload.get("access_token") or not isinstance(user_payload, dict):
        raise ExternalServiceError("The authentication service returned an invalid session.")
    user = await sync_user(db, **user_from_supabase_payload(user_payload), mark_login=True)
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token"),
        "token_type": str(payload.get("token_type") or "bearer").lower(),
        "expires_in": payload.get("expires_in"),
        "expires_at": payload.get("expires_at"),
        "user": UserOut.model_validate(user),
    }


@router.post(
    "/signup",
    response_model=ApiResponse[SignupOut],
    status_code=201,
    dependencies=[Depends(rate_limit("auth_signup", "RATE_LIMIT_AUTH_PER_MINUTE"))],
    summary="Register with email and password",
)
async def signup(body: SignupRequest, db: DBSession, auth: Auth) -> dict[str, Any]:
    """Create a Supabase Auth account.

    When email confirmation is enabled no session is returned and
    `email_confirmation_required` is true. For privacy, the response for an email
    that is already registered (with confirmation enabled) is indistinguishable.
    """
    metadata = {"full_name": body.full_name} if body.full_name else {}
    payload = await auth.sign_up(body.email, body.password, metadata)

    if payload.get("access_token"):
        session = await _session_from_payload(db, payload)
        return ok(
            {"user": session["user"], "session": session, "email_confirmation_required": False},
            "Account created successfully",
        )
    return ok(
        {"user": None, "session": None, "email_confirmation_required": True},
        "Please check your email to confirm your account.",
    )


@router.post(
    "/login",
    response_model=ApiResponse[SessionOut],
    dependencies=[Depends(rate_limit("auth_login", "RATE_LIMIT_AUTH_PER_MINUTE"))],
    summary="Sign in with email and password",
)
async def login(body: LoginRequest, db: DBSession, auth: Auth) -> dict[str, Any]:
    payload = await auth.sign_in_with_password(body.email, body.password)
    session = await _session_from_payload(db, payload)
    if not session["user"].is_active:
        # Do not hand out a usable session for suspended accounts.
        try:
            await auth.sign_out(session["access_token"])
        except Exception:
            logger.warning("Could not revoke session for suspended account")
        raise AuthenticationError("This account has been suspended.", code="account_suspended")
    return ok(session, "Login successful")


@router.post(
    "/refresh",
    response_model=ApiResponse[SessionOut],
    dependencies=[Depends(rate_limit("auth_refresh", "RATE_LIMIT_AUTH_PER_MINUTE"))],
    summary="Exchange a refresh token for new tokens",
)
async def refresh(body: RefreshRequest, db: DBSession, auth: Auth) -> dict[str, Any]:
    payload = await auth.refresh_session(body.refresh_token)
    session = await _session_from_payload(db, payload)
    if not session["user"].is_active:
        raise AuthenticationError("This account has been suspended.", code="account_suspended")
    return ok(session, "Session refreshed")


@router.post("/logout", response_model=ApiResponse[LogoutOut], summary="Revoke the current session")
async def logout(
    ctx: Annotated[AuthContext, Depends(get_auth_context)], auth: Auth
) -> dict[str, Any]:
    """Revoke the Supabase session (refresh token) server-side.

    The current access token remains cryptographically valid until it expires
    (Supabase JWTs are stateless); clients must also delete stored tokens.
    """
    revoked = await auth.sign_out(ctx.access_token)
    detail = "Session revoked." if revoked else "Session was already invalid."
    return ok({"revoked": revoked, "detail": detail}, "Logged out")


@router.get("/me", response_model=ApiResponse[MeOut], summary="Current authenticated user")
async def me(ctx: Annotated[AuthContext, Depends(get_auth_context)]) -> dict[str, Any]:
    data = UserOut.model_validate(ctx.user).model_dump()
    data.update(session_expires_at=ctx.claims.expires_at, aal=ctx.claims.aal)
    return ok(data)


@router.post(
    "/resend-verification",
    response_model=ApiResponse[None],
    dependencies=[Depends(rate_limit("auth_email", "RATE_LIMIT_EMAIL_PER_HOUR", 3600))],
    summary="Resend the signup confirmation email",
)
async def resend_verification(body: EmailRequest, auth: Auth) -> dict[str, Any]:
    await auth.resend_signup_confirmation(body.email)
    return ok(None, _GENERIC_EMAIL_MSG)


@router.post(
    "/forgot-password",
    response_model=ApiResponse[None],
    dependencies=[Depends(rate_limit("auth_email", "RATE_LIMIT_EMAIL_PER_HOUR", 3600))],
    summary="Send a password-recovery email",
)
async def forgot_password(body: EmailRequest, auth: Auth) -> dict[str, Any]:
    """Always returns the same message whether or not the account exists."""
    await auth.send_password_recovery(body.email)
    return ok(None, _GENERIC_EMAIL_MSG)


@router.post(
    "/reset-password",
    response_model=ApiResponse[None],
    dependencies=[Depends(rate_limit("auth_reset", "RATE_LIMIT_AUTH_PER_MINUTE"))],
    summary="Set a new password after recovery",
)
async def reset_password(
    request: Request,
    body: ResetPasswordRequest,
    auth: Auth,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict[str, Any]:
    """Complete password recovery.

    * Implicit flow: the recovery deep link yields an access token; send it as
      `Authorization: Bearer <token>` with `new_password`.
    * Token-hash flow: send the `token_hash` from the email link; the backend
      verifies it with Supabase, then updates the password.
    """
    if body.token_hash:
        session = await auth.verify_recovery_token_hash(body.token_hash)
        token = session.get("access_token")
        if not token:
            raise AuthenticationError("The recovery link is invalid or has expired.", code="invalid_token")
    elif credentials is not None:
        token = credentials.credentials
    else:
        raise BadRequestError("Provide a recovery token.", code="recovery_token_required")
    await auth.update_password(token, body.new_password)
    return ok(None, "Password updated successfully. Please sign in again.")
