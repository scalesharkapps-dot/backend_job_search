"""Import every model so ``Base.metadata`` is complete (Alembic, tests)."""

from app.models.application import (
    Application,
    ApplicationStatusHistory,
    Interview,
    SavedJob,
)
from app.models.base import Base
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
from app.models.user import User, UserRole

__all__ = [
    "AdminAuditLog",
    "Application",
    "ApplicationStatusHistory",
    "Base",
    "CVExtractedData",
    "EmployerProfile",
    "EmployerVerificationRequest",
    "Interview",
    "Job",
    "JobRecommendation",
    "JobSearchCache",
    "JobSource",
    "Report",
    "SavedJob",
    "User",
    "UserCV",
    "UserProfile",
    "UserRecommendationPreferences",
    "UserRole",
]
