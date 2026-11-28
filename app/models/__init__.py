"""Import every model so ``Base.metadata`` is complete (Alembic, tests)."""

from app.models.account_deletion import DeletedAccount
from app.models.application import (
    Application,
    ApplicationLinkAction,
    ApplicationStatusHistory,
    Interview,
    SavedJob,
)
from app.models.base import Base
from app.models.email_verification import EmailVerificationTracker
from app.models.employer import (
    AdminAuditLog,
    EmployerProfile,
    EmployerVerificationRequest,
    Report,
)
from app.models.job import Job, JobSearchCache, JobSource
from app.models.profile import CVExtractedData, UserCV, UserProfile
from app.models.recommendation import (
    JobRecommendation,
    UserRecommendationPreferences,
)
from app.models.subscription import (
    PaymentHistory,
    Subscription,
    SubscriptionEvent,
    UserTrial,
)
from app.models.user import User, UserRole

__all__ = [
    "AdminAuditLog",
    "Application",
    "ApplicationLinkAction",
    "ApplicationStatusHistory",
    "Base",
    "CVExtractedData",
    "DeletedAccount",
    "EmailVerificationTracker",
    "EmployerProfile",
    "EmployerVerificationRequest",
    "Interview",
    "Job",
    "JobRecommendation",
    "JobSearchCache",
    "JobSource",
    "PaymentHistory",
    "Report",
    "SavedJob",
    "Subscription",
    "SubscriptionEvent",
    "User",
    "UserCV",
    "UserProfile",
    "UserRecommendationPreferences",
    "UserRole",
    "UserTrial",
]
