"""Administrative API (admin role required on every endpoint)."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import DBSession, require_role
from app.core.permissions import Admin
from app.core.rate_limit import rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok, paginate
from app.models.employer import EmployerAccountStatus, ReportStatus, ReportTargetType, VerificationStatus
from app.models.job import JobSource, PostingStatus
from app.models.user import UserRole
from app.schemas.employer import (
    AdminEmployerStatusUpdate,
    AdminJobStatusUpdate,
    AdminModerationDecision,
    AdminReportOut,
    AdminRoleUpdate,
    AdminUserOut,
    AdminUserStatusUpdate,
    AdminVerificationDecision,
    AuditLogOut,
    EmployerProfileOut,
    ReportStatusUpdate,
    VerificationRequestOut,
)
from app.schemas.job import job_detail
from app.services import admin_service as admin
from app.services import employer_service as es
from app.services.supabase_auth import SupabaseAuthClient, get_supabase_auth

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    responses=ERROR_RESPONSES,
    dependencies=[Depends(require_role(UserRole.ADMIN)), Depends(rate_limit("admin", "RATE_LIMIT_WRITE_PER_MINUTE"))],
)
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]


def _user(u: Any) -> dict[str, Any]:
    return AdminUserOut(id=u.id, email=u.email, full_name=u.full_name, role=u.role, is_active=u.is_active,
                        email_verified=u.email_verified, suspended_reason=u.suspended_reason,
                        last_login_at=u.last_login_at, created_at=u.created_at).model_dump(mode="json")


# ---------------- Dashboard ----------------
@router.get("/dashboard", response_model=ApiResponse[dict[str, Any]], summary="Platform statistics")
async def dashboard(_: Admin, db: DBSession) -> dict[str, Any]:
    return ok(await admin.dashboard(db))


# ---------------- Users ----------------
@router.get("/users", response_model=ApiResponse[dict[str, Any]], summary="List users")
async def list_users(_: Admin, db: DBSession, q: Annotated[str | None, Query(max_length=100)] = None,
                     role: UserRole | None = None, is_active: bool | None = None, page: Page = 1, page_size: PageSize = 20) -> dict[str, Any]:
    users, total = await admin.list_users(db, q=q, role=role, is_active=is_active, page=page, page_size=page_size)
    return ok(paginate([_user(u) for u in users], page, page_size, total))


@router.get("/users/{user_id}", response_model=ApiResponse[dict[str, Any]], summary="User details")
async def get_user(user_id: uuid.UUID, _: Admin, db: DBSession) -> dict[str, Any]:
    user = await admin.get_user(db, user_id)
    return ok({**_user(user), **(await admin.user_detail(db, user))})


@router.patch("/users/{user_id}/status", response_model=ApiResponse[dict[str, Any]], summary="Suspend or reactivate a user")
async def user_status(user_id: uuid.UUID, body: AdminUserStatusUpdate, actor: Admin, db: DBSession,
                      auth: Annotated[SupabaseAuthClient, Depends(get_supabase_auth)]) -> dict[str, Any]:
    """Suspension takes effect on the user's next API call. `auth_provider_updated`
    is true only when the Supabase account was also banned/unbanned (requires a
    server-side service-role key)."""
    user, provider = await admin.set_user_status(db, actor, user_id, body.is_active, body.reason, auth)
    return ok({**_user(user), "auth_provider_updated": provider}, "User status updated")


@router.patch("/users/{user_id}/role", response_model=ApiResponse[dict[str, Any]], summary="Change a user's role")
async def user_role(user_id: uuid.UUID, body: AdminRoleUpdate, actor: Admin, db: DBSession) -> dict[str, Any]:
    """Admins cannot change their own role, and the last active admin cannot be demoted."""
    return ok(_user(await admin.set_user_role(db, actor, user_id, body.role, body.reason)), "Role updated")


# ---------------- Employers ----------------
@router.get("/employers", response_model=ApiResponse[dict[str, Any]], summary="List employers")
async def list_employers(_: Admin, db: DBSession, verification_status: VerificationStatus | None = None,
                         account_status: EmployerAccountStatus | None = None,
                         q: Annotated[str | None, Query(max_length=100)] = None, page: Page = 1, page_size: PageSize = 20) -> dict[str, Any]:
    rows, total = await admin.list_employers(db, verification=verification_status, account=account_status, q=q,
                                             page=page, page_size=page_size)
    return ok(paginate([EmployerProfileOut.model_validate(e).model_dump(mode="json") for e in rows], page, page_size, total))


@router.get("/employers/{employer_id}", response_model=ApiResponse[dict[str, Any]], summary="Employer details")
async def get_employer(employer_id: uuid.UUID, _: Admin, db: DBSession) -> dict[str, Any]:
    employer = await admin.get_employer(db, employer_id)
    requests = await admin.verification_requests(db, employer)
    return ok({
        "employer": EmployerProfileOut.model_validate(employer).model_dump(mode="json"),
        "owner_user_id": str(employer.owner_user_id),
        "verification_requests": [VerificationRequestOut.model_validate(r).model_dump(mode="json") for r in requests],
    })


@router.patch("/employers/{employer_id}/verification", response_model=ApiResponse[EmployerProfileOut],
              summary="Review a verification request")
async def verification(employer_id: uuid.UUID, body: AdminVerificationDecision, actor: Admin, db: DBSession) -> dict[str, Any]:
    """`decision`: under_review | approve | reject (reject requires a reason)."""
    employer = await admin.decide_verification(db, actor, employer_id, body.decision, body.reason)
    return ok(EmployerProfileOut.model_validate(employer), "Verification decision recorded")


@router.patch("/employers/{employer_id}/status", response_model=ApiResponse[EmployerProfileOut],
              summary="Suspend or reactivate an employer")
async def employer_status(employer_id: uuid.UUID, body: AdminEmployerStatusUpdate, actor: Admin, db: DBSession) -> dict[str, Any]:
    employer = await admin.set_employer_status(db, actor, employer_id, body.account_status, body.reason)
    return ok(EmployerProfileOut.model_validate(employer), "Employer status updated")


# ---------------- Jobs ----------------
@router.get("/jobs", response_model=ApiResponse[dict[str, Any]], summary="List jobs for moderation")
async def list_jobs(_: Admin, db: DBSession, status: PostingStatus | None = None, source: JobSource | None = None,
                    employer_id: uuid.UUID | None = None, q: Annotated[str | None, Query(max_length=100)] = None,
                    page: Page = 1, page_size: PageSize = 20) -> dict[str, Any]:
    await es.expire_due_jobs(db)
    jobs, total = await admin.list_jobs(db, status=status, source=source, employer_id=employer_id, q=q, page=page, page_size=page_size)
    items = [{**job_detail(j), "posting_status": j.posting_status.value if j.posting_status else None,
              "employer_id": j.employer_id, "moderation_note": j.moderation_note} for j in jobs]
    return ok(paginate(items, page, page_size, total))


@router.get("/jobs/{job_id}", response_model=ApiResponse[dict[str, Any]], summary="Job details (any status)")
async def get_job(job_id: uuid.UUID, _: Admin, db: DBSession) -> dict[str, Any]:
    job = await admin.get_job(db, job_id)
    extra = es.job_out(job) if job.employer_id else {}
    return ok({**job_detail(job), **extra, "employer_id": job.employer_id})


@router.patch("/jobs/{job_id}/moderation", response_model=ApiResponse[dict[str, Any]], summary="Approve or reject a pending job")
async def moderate(job_id: uuid.UUID, body: AdminModerationDecision, actor: Admin, db: DBSession) -> dict[str, Any]:
    job = await admin.moderate_job(db, actor, job_id, body.decision, body.reason)
    return ok(es.job_out(job), "Moderation decision recorded")


@router.patch("/jobs/{job_id}/status", response_model=ApiResponse[dict[str, Any]], summary="Suspend, close or archive a job")
async def job_status(job_id: uuid.UUID, body: AdminJobStatusUpdate, actor: Admin, db: DBSession) -> dict[str, Any]:
    """Targets: suspended | paused | closed | archived, or published (reinstating a suspended job)."""
    return ok(es.job_out(await admin.set_job_status(db, actor, job_id, body.status, body.reason)), "Job status updated")


# ---------------- Reports ----------------
@router.get("/reports", response_model=ApiResponse[dict[str, Any]], summary="List abuse reports")
async def list_reports(_: Admin, db: DBSession, status: ReportStatus | None = None, target_type: ReportTargetType | None = None,
                       page: Page = 1, page_size: PageSize = 20) -> dict[str, Any]:
    rows, total = await admin.list_reports(db, status=status, target_type=target_type, page=page, page_size=page_size)
    return ok(paginate([AdminReportOut.model_validate(r).model_dump(mode="json") for r in rows], page, page_size, total))


@router.get("/reports/{report_id}", response_model=ApiResponse[AdminReportOut], summary="Report details")
async def get_report(report_id: uuid.UUID, _: Admin, db: DBSession) -> dict[str, Any]:
    return ok(AdminReportOut.model_validate(await admin.get_report(db, report_id)))


@router.patch("/reports/{report_id}/status", response_model=ApiResponse[AdminReportOut], summary="Update a report")
async def report_status(report_id: uuid.UUID, body: ReportStatusUpdate, actor: Admin, db: DBSession) -> dict[str, Any]:
    """Resolving or rejecting requires a resolution note."""
    report = await admin.set_report_status(db, actor, report_id, body.status, body.resolution_note)
    return ok(AdminReportOut.model_validate(report), "Report updated")


# ---------------- Audit log ----------------
@router.get("/audit-logs", response_model=ApiResponse[dict[str, Any]], summary="Administrative audit log")
async def audit_logs(_: Admin, db: DBSession, action: Annotated[str | None, Query(max_length=64)] = None,
                     actor_id: uuid.UUID | None = None, target_type: Annotated[str | None, Query(max_length=32)] = None,
                     target_id: Annotated[str | None, Query(max_length=64)] = None,
                     page: Page = 1, page_size: PageSize = 50) -> dict[str, Any]:
    rows, total = await admin.list_audit_logs(db, action=action, actor_id=actor_id, target_type=target_type,
                                              target_id=target_id, page=page, page_size=page_size)
    return ok(paginate([AuditLogOut.model_validate(r).model_dump(mode="json") for r in rows], page, page_size, total))
