"""Authentication endpoints backed by Supabase Auth."""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials

from app.api.deps import AuthContext, CurrentUser, DBSession, bearer_scheme, get_auth_context
from app.core.config import get_settings
from app.core.exceptions import (
    AppError,
    AuthenticationError,
    BadRequestError,
    ExternalServiceError,
    PermissionDeniedError,
    RateLimitedError,
    ValidationAppError,
)
from app.core.rate_limit import client_ip, limiter, rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok
from app.core.security import verify_access_token
from app.schemas.auth import (
    ChangePasswordOut,
    ChangePasswordRequest,
    DeleteAccountOut,
    DeleteAccountRequest,
    EmailRequest,
    LoginRequest,
    LogoutOut,
    MeOut,
    RefreshRequest,
    ResendVerificationOut,
    ResetPasswordRequest,
    SessionOut,
    SignupOut,
    SignupRequest,
    UserOut,
    VerifyEmailRequest,
    password_policy_problems,
)
from app.services import account_deletion, email_verification
from app.services.billing.trial import ensure_trial
from app.services.storage import StorageBackend, get_storage
from app.services.supabase_auth import SupabaseAuthClient, get_supabase_auth
from app.services.user_service import sync_user, user_from_supabase_payload

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["Authentication"], responses=ERROR_RESPONSES)

Auth = Annotated[SupabaseAuthClient, Depends(get_supabase_auth)]

_RESEND_MSG = "If this email is registered and not yet verified, a new verification email has been sent."


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


async def _start_trial_for_signup(db: DBSession, payload: dict[str, Any] | None) -> None:
    """Start the 3-day app trial as soon as Supabase confirms a new account.

    Supabase returns an obfuscated user without identities for an e-mail that is
    already registered; no trial is created for those. Failures never fail the
    signup -- the trial is then created on the user's first authenticated request.
    """
    if not payload:
        return
    user_payload = payload.get("user") if isinstance(payload.get("user"), dict) else payload
    if not user_payload.get("id") or not (payload.get("access_token") or user_payload.get("identities")):
        return
    try:
        identity = user_from_supabase_payload(user_payload)
        user = await sync_user(db, **identity)
        await ensure_trial(db, user.id)
    except Exception:
        await db.rollback()
        logger.warning("Could not start the app trial at signup; it starts on first sign-in")


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
    # Repeated registrations of an unverified address make Supabase send another
    # confirmation e-mail, so they are counted against (and cannot reset) the resend limit.
    payload = await email_verification.signup_with_tracking(
        db, body.email, lambda: auth.sign_up(body.email, body.password, metadata)
    )

    await _start_trial_for_signup(db, payload)
    if payload and payload.get("access_token"):
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
    response_model=ApiResponse[ResendVerificationOut],
    # Separate per-IP bucket: password-recovery requests must not consume resends.
    dependencies=[Depends(rate_limit("auth_resend", "RATE_LIMIT_EMAIL_PER_HOUR", 3600))],
    summary="Resend the signup confirmation email",
)
async def resend_verification(body: EmailRequest, db: DBSession, auth: Auth) -> dict[str, Any]:
    """Resend the signup verification e-mail (max 2 resends per verification cycle).

    * `200` -- Supabase accepted the request; `data.remaining_attempts` is what is left.
    * `429 resend_limit_reached` -- limit used up (`data.remaining_attempts` = 0).
    * `429 resend_cooldown` -- requested too soon after the previous e-mail.
    * `409 email_already_verified` -- nothing to resend; sign in instead.

    Attempts are stored in PostgreSQL per e-mail address and only successful
    Supabase requests are counted.
    """
    result = await email_verification.resend_verification(
        db, body.email, lambda: auth.resend_signup_confirmation(body.email)
    )
    return ok(
        {
            "remaining_attempts": result.remaining_attempts,
            "max_resends": result.max_resends,
            "cooldown_seconds": result.cooldown_seconds,
        },
        _RESEND_MSG,
    )


_RESET_LINK_MSG = "If an account exists for this email, a reset link has been sent."
_WAIT_MSG = "Please wait a minute before trying again."
# Supabase marks sessions created from a recovery link with one of these AMR methods
# (GoTrue models.Session.IsRecovery: otp, magiclink, recovery). Password logins carry "password".
_RECOVERY_AMR_METHODS = {"otp", "magiclink", "recovery"}
# A recovery session may set a new password only this long after the link was opened.
RECOVERY_SESSION_MAX_AGE_SECONDS = 3600
_RECOVERY_INVALID = "RECOVERY_LINK_INVALID_OR_EXPIRED"
_RECOVERY_INVALID_MSG = "This password reset link is invalid or has expired. Please request a new one."


def _throttled(retry_after: int | None) -> RateLimitedError:
    return RateLimitedError(_WAIT_MSG, retry_after=retry_after, code="RATE_LIMITED")


def _hit(key: str, limit: int, window: int) -> None:
    if not get_settings().RATE_LIMIT_ENABLED:
        return
    allowed, retry_after = limiter.hit(key, limit, window)
    if not allowed:
        raise _throttled(retry_after)


def _retry_after(exc: AppError) -> int | None:
    return exc.details.get("retry_after") if isinstance(exc.details, dict) else None


def _recovery_invalid() -> AuthenticationError:
    return AuthenticationError(_RECOVERY_INVALID_MSG, code=_RECOVERY_INVALID)


@router.post(
    "/forgot-password",
    response_model=ApiResponse[None],
    summary="Send a password-recovery email",
)
async def forgot_password(request: Request, body: EmailRequest, auth: Auth) -> dict[str, Any]:
    """Always the same 200 response whether or not an account exists (no enumeration).

    Throttled per client IP (``RATE_LIMIT_EMAIL_PER_HOUR``) and per e-mail address
    (``RATE_LIMIT_PASSWORD_RESET_PER_EMAIL_PER_HOUR``); Supabase's own e-mail limits
    are passed through. All of them return ``429 RATE_LIMITED`` with ``Retry-After``.
    Infrastructure failures are reported as errors, never as success.
    """
    settings = get_settings()
    _hit(f"auth_forgot_password:ip:{client_ip(request)}", settings.RATE_LIMIT_EMAIL_PER_HOUR, 3600)
    # Hashed so raw e-mail addresses never sit in the limiter's memory keys.
    email_key = hashlib.sha256(body.email.strip().lower().encode()).hexdigest()
    _hit(f"auth_forgot_password:email:{email_key}", settings.RATE_LIMIT_PASSWORD_RESET_PER_EMAIL_PER_HOUR, 3600)
    try:
        await auth.send_password_recovery(body.email)
    except RateLimitedError as exc:
        raise _throttled(_retry_after(exc)) from None
    except AppError as exc:
        if exc.code != "password_reset_not_configured":  # that case already logged its own ERROR line
            logger.error("Forgot-password failed: %s (HTTP %s)", exc.code, exc.status_code)
        raise
    return ok(None, _RESET_LINK_MSG)


async def _recovery_access_token(
    body: ResetPasswordRequest, credentials: HTTPAuthorizationCredentials | None, auth: SupabaseAuthClient
) -> tuple[str, str | None]:
    """Return a verified recovery-session access token and the account e-mail."""
    if body.token_hash:
        try:
            session = await auth.verify_recovery_token_hash(body.token_hash)
        except AuthenticationError:
            raise _recovery_invalid() from None
        except RateLimitedError as exc:
            raise _throttled(_retry_after(exc)) from None
        token = session.get("access_token")
        if not token:
            raise _recovery_invalid()
        user = session.get("user") if isinstance(session.get("user"), dict) else {}
        return token, user.get("email")

    token = body.access_token or (credentials.credentials if credentials is not None else None)
    if not token:
        raise BadRequestError("Open the reset link from your e-mail again.", code="RECOVERY_TOKEN_REQUIRED")
    try:
        claims = await verify_access_token(token)  # signature, issuer, audience, expiry
    except AuthenticationError:
        raise _recovery_invalid() from None
    amr = claims.raw.get("amr")
    now = int(time.time())
    recovery_times: list[int] = []
    for entry in amr if isinstance(amr, list) else []:
        # Supabase AMR entries: {"method": "otp", "timestamp": <unix seconds>}.
        if not isinstance(entry, dict) or entry.get("method") not in _RECOVERY_AMR_METHODS:
            continue
        stamp = entry.get("timestamp")
        if isinstance(stamp, int | float) and not isinstance(stamp, bool):
            recovery_times.append(int(stamp))
    # An ordinary sign-in token (amr "password") or a stale recovery session is not accepted.
    if not recovery_times or now - max(recovery_times) > RECOVERY_SESSION_MAX_AGE_SECONDS:
        logger.info("Rejected a non-recovery or stale token on reset-password")
        raise _recovery_invalid()
    return token, claims.email


@router.post(
    "/reset-password",
    response_model=ApiResponse[dict[str, Any]],
    summary="Set a new password after recovery",
)
async def reset_password(
    request: Request,
    body: ResetPasswordRequest,
    auth: Auth,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> dict[str, Any]:
    """Complete password recovery with the credential from the recovery link.

    * Implicit flow (default): the deep link ``AUTH_PASSWORD_RESET_REDIRECT_URL`` opens the app with
      ``#access_token=...&type=recovery``; send that token as ``Authorization: Bearer`` (or in
      ``access_token``). Only tokens of a recovery session opened within the last hour are
      accepted -- an ordinary sign-in token is rejected.
    * Token-hash template: send ``token_hash``; the backend verifies it as ``recovery``.

    On success every session of the account (including this recovery session) is
    revoked, so the link cannot be reused and other devices must sign in again.
    """
    settings = get_settings()
    _hit(f"auth_reset_password:ip:{client_ip(request)}", settings.RATE_LIMIT_AUTH_PER_MINUTE, 60)
    token, email = await _recovery_access_token(body, credentials, auth)

    problems = password_policy_problems(body.new_password, email)
    if problems:
        raise ValidationAppError(
            "Password must contain " + ", ".join(problems),
            code="PASSWORD_POLICY_VIOLATION",
            details=[{"field": "new_password", "message": p, "type": "password_policy"} for p in problems],
        )
    try:
        await auth.update_password(token, body.new_password)
    except RateLimitedError as exc:
        raise _throttled(_retry_after(exc)) from None
    except ValidationAppError as exc:
        if exc.code == "same_password":
            raise ValidationAppError(
                "The new password must be different from your current password.",
                code="PASSWORD_UNCHANGED",
                details=[{"field": "new_password", "message": "same as current password", "type": "password_unchanged"}],
            ) from None
        if exc.code == "weak_password":
            raise ValidationAppError(
                "Password does not meet the security requirements.",
                code="PASSWORD_POLICY_VIOLATION",
                details=[{"field": "new_password", "message": "rejected by the authentication service",
                          "type": "password_policy"}],
            ) from None
        raise
    except AuthenticationError:
        # Revoked session (link already used), expired token, or Supabase refusing the session.
        raise _recovery_invalid() from None
    except AppError as exc:
        if exc.code == "reauthentication_required":
            raise _recovery_invalid() from None
        raise

    sessions_revoked = False
    try:
        sessions_revoked = await auth.sign_out(token, scope="global")
    except AppError:
        logger.warning("Password was reset but sessions could not be revoked")
    return ok(
        {"status": "SUCCESS", "sessions_revoked": sessions_revoked},
        "Password updated successfully. Please sign in again.",
    )


@router.post(
    "/verify-email",
    response_model=ApiResponse[SessionOut],
    dependencies=[Depends(rate_limit("auth_verify", "RATE_LIMIT_AUTH_PER_MINUTE"))],
    summary="Confirm an e-mail address with the token_hash from the confirmation link",
)
async def verify_email(body: VerifyEmailRequest, db: DBSession, auth: Auth) -> dict[str, Any]:
    """For confirmation e-mail templates that pass ``{{ .TokenHash }}`` to the app.

    Returns a session and marks the local user as verified. Expired, already used
    or unknown links -> ``401 invalid_token`` (request a new one via resend-verification).
    """
    payload = await auth.verify_email_token_hash(body.token_hash, body.type)
    session = await _session_from_payload(db, payload)
    return ok(session, "Email address verified")


@router.post(
    "/change-password",
    response_model=ApiResponse[ChangePasswordOut],
    dependencies=[Depends(rate_limit("auth_change_password", "RATE_LIMIT_AUTH_PER_MINUTE"))],
    summary="Change my password (requires the current password)",
)
async def change_password(
    body: ChangePasswordRequest, db: DBSession, auth: Auth, user: CurrentUser,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> dict[str, Any]:
    """Only the signed-in account owner, and only with the current password.

    The password is changed with the caller's own session. If Supabase demands a
    recent sign-in ("Secure password change"), the change is made with a fresh session,
    which is returned in ``data.session`` -- clients must then store those tokens.
    """
    if body.current_password == body.new_password:
        raise ValidationAppError("The new password must differ from the old password.", code="same_password")
    try:
        fresh = await auth.sign_in_with_password(user.email, body.current_password)
    except AuthenticationError as exc:
        if exc.code in {"invalid_credentials", "email_not_confirmed"}:
            raise PermissionDeniedError("The current password is incorrect.", code="invalid_password") from None
        raise
    fresh_user = fresh.get("user") if isinstance(fresh.get("user"), dict) else {}
    if str(fresh_user.get("id")) != str(user.id) or not fresh.get("access_token"):
        raise PermissionDeniedError("The current password is incorrect.", code="invalid_password")

    new_session = None
    try:
        await auth.update_password(ctx.access_token, body.new_password)
    except AppError as exc:
        if exc.code != "reauthentication_required":
            raise
        await auth.update_password(fresh["access_token"], body.new_password)
        new_session = await _session_from_payload(db, fresh)
    if new_session is None:
        try:  # the session opened to check the current password is not needed any more
            await auth.sign_out(fresh["access_token"])
        except AppError:
            logger.info("Could not revoke the password-check session")
    return ok({"password_changed": True, "session": new_session}, "Password changed successfully")


@router.post(
    "/delete-account",
    response_model=ApiResponse[DeleteAccountOut],
    dependencies=[Depends(rate_limit("auth_delete_account", "RATE_LIMIT_AUTH_PER_MINUTE"))],
    summary="Permanently delete my account and data",
)
async def delete_account(
    body: DeleteAccountRequest,
    db: DBSession,
    auth: Auth,
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
    storage: Annotated[StorageBackend, Depends(get_storage)],
) -> dict[str, Any]:
    """Irreversible. Requires the current password and ``confirmation: "DELETE"``.

    Deletes the user's files, the Supabase Auth user (all sessions) and every
    user-owned database record. Store subscriptions are billed by Google Play /
    the App Store and must be cancelled there (``active_store_subscription``).
    """
    result = await account_deletion.delete_account(
        db, ctx.user, body.password, body.confirmation, auth, storage
    )
    return ok(
        {
            "deleted": True,
            "user_id": result.user_id,
            "storage_objects_deleted": result.storage_objects_deleted,
            "active_store_subscription": result.active_store_subscription,
        },
        "Your account has been permanently deleted.",
    )

