"""Reusable FastAPI dependencies: database session, authentication and roles."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.exceptions import AuthenticationError, PermissionDeniedError
from app.core.security import TokenClaims, verify_access_token
from app.models.user import User, UserRole
from app.services.application_links import set_apply_links_visible
from app.services.billing.revenuecat import RevenueCatClient, get_revenuecat_client
from app.services.billing.subscriptions import evaluate_access
from app.services.plan_access import set_pro_for_request
from app.services.user_service import sync_user

bearer_scheme = HTTPBearer(auto_error=False, description="Supabase access token")

DBSession = Annotated[AsyncSession, Depends(get_db)]


@dataclass
class AuthContext:
    """Authenticated request context: verified claims, raw token and local user."""

    user: User
    claims: TokenClaims
    access_token: str


async def _authenticate(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
    db: AsyncSession,
) -> AuthContext:
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise AuthenticationError("Authentication is required.", code="not_authenticated")
    claims = await verify_access_token(credentials.credentials)
    user = await sync_user(
        db,
        user_id=claims.user_id,
        email=claims.email,
        full_name=claims.user_metadata.get("full_name"),
    )
    request.state.user_id = str(user.id)
    return AuthContext(user=user, claims=claims, access_token=credentials.credentials)


async def get_auth_context(
    request: Request,
    db: DBSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> AuthContext:
    """Verified token + synchronised user, regardless of account status."""
    return await _authenticate(request, credentials, db)


async def get_current_user(
    ctx: Annotated[AuthContext, Depends(get_auth_context)],
) -> User:
    """The authenticated user; suspended accounts are rejected."""
    if not ctx.user.is_active:
        raise PermissionDeniedError("This account has been suspended.", code="account_suspended")
    return ctx.user


async def get_optional_current_user(
    request: Request,
    db: DBSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User | None:
    """Authenticated user when a token is supplied; ``None`` for anonymous calls.

    An invalid token is still rejected (never silently treated as anonymous).
    """
    if credentials is None:
        return None
    ctx = await _authenticate(request, credentials, db)
    if not ctx.user.is_active:
        raise PermissionDeniedError("This account has been suspended.", code="account_suspended")
    return ctx.user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_role(*roles: UserRole) -> Callable[..., Coroutine[Any, Any, User]]:
    """Dependency factory: allow only users whose server-side role is in ``roles``."""
    allowed = set(roles)

    async def dependency(user: CurrentUser) -> User:
        if user.role not in allowed:
            raise PermissionDeniedError(
                "Your account role cannot access this resource.", code="insufficient_role"
            )
        return user

    return dependency


require_active_user = get_current_user

SUBSCRIPTION_REQUIRED_MESSAGE = "Your free trial or subscription has ended. Subscribe to continue using SharkHire."


async def require_app_access(
    request: Request,
    user: CurrentUser,
    db: DBSession,
    client: Annotated[RevenueCatClient, Depends(get_revenuecat_client)],
) -> User:
    """Paywall for the whole app: an active 3-day trial or a verified ``app_access`` entitlement.

    Decided only from server-side data (trial table + RevenueCat-verified subscription
    rows). Raises ``403 subscription_required`` with the reason in ``data``/``details``.
    """
    decision = await evaluate_access(db, user, client)
    # Pro (unlimited application links) = verified entitlement or exempt staff role. Never from the client.
    unlimited = decision.entitlement_active or decision.access_status == "exempt"
    request.state.application_links_unlimited = unlimited
    set_apply_links_visible(unlimited or not get_settings().HIDE_APPLY_LINKS_FOR_FREE_USERS)
    set_pro_for_request(unlimited)
    if not decision.allowed:
        summary = decision.summary()
        raise PermissionDeniedError(
            SUBSCRIPTION_REQUIRED_MESSAGE, code="subscription_required", details=summary, data=summary
        )
    return user


AppUser = Annotated[User, Depends(require_app_access)]
JobSeekerUser = Annotated[User, Depends(require_role(UserRole.JOB_SEEKER))]
EmployerUser = Annotated[User, Depends(require_role(UserRole.EMPLOYER))]
AdminUser = Annotated[User, Depends(require_role(UserRole.ADMIN))]
