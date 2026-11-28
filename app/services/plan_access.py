"""Pro-only features, decided from the verified subscription (never from the client).

``require_app_access`` evaluates RevenueCat-verified state once per request and calls
``set_pro_for_request``. Pro = active ``app_access`` entitlement or an exempt staff role.
Outside user requests (admin tooling, scripts) nothing is set and nothing is restricted.

Remote jobs (``REMOTE_JOBS_PRO_ONLY``): Free users get ``403 PRO_FEATURE_REQUIRED`` when
they ask for remote jobs, and remote listings are removed from every job-discovery result
(search -- live, cached and paginated --, SharkHire listings, recommendations, history,
saved jobs) and refused when opened directly by id.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from app.core.config import get_settings
from app.core.exceptions import AppError

REMOTE_JOBS_MESSAGE = (
    "Remote job access is available exclusively to Pro members. "
    "Upgrade to Pro to explore remote opportunities."
)

# None = not inside a paywalled user request (no restriction applies).
_is_pro: ContextVar[bool | None] = ContextVar("is_pro", default=None)


def set_pro_for_request(pro: bool) -> None:
    _is_pro.set(pro)


def is_pro() -> bool | None:
    return _is_pro.get()


class ProFeatureRequired(AppError):
    status_code = 403
    code = "PRO_FEATURE_REQUIRED"

    def __init__(self, feature: str = "remote_jobs", message: str = REMOTE_JOBS_MESSAGE) -> None:
        payload = {"code": self.code, "feature": feature, "upgrade_available": True}
        super().__init__(message, details=payload, data=payload)


def remote_jobs_restricted() -> bool:
    """True for a Free user in a user request while remote jobs are Pro-only."""
    return get_settings().REMOTE_JOBS_PRO_ONLY and _is_pro.get() is False


def require_remote_access(requested: bool | None) -> None:
    """Refuse an explicit remote request (``remote_jobs_only=true``, ``remote=true``, ...)."""
    if requested and remote_jobs_restricted():
        raise ProFeatureRequired()


def ensure_job_accessible(job: Any) -> None:
    """Refuse opening a remote job directly (details, apply link, save, track, match)."""
    if getattr(job, "is_remote", False) and remote_jobs_restricted():
        raise ProFeatureRequired()
