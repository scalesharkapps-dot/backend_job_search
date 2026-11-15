"""User-facing abuse reporting."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.deps import CurrentUser, DBSession
from app.core.rate_limit import rate_limit
from app.core.responses import ERROR_RESPONSES, ApiResponse, ok, paginate
from app.models.employer import Report
from app.schemas.employer import ReportCreate, ReportOut
from app.services import admin_service

router = APIRouter(prefix="/reports", tags=["Reports"], responses=ERROR_RESPONSES)


@router.post("", response_model=ApiResponse[ReportOut], status_code=201,
             dependencies=[Depends(rate_limit("report", "RATE_LIMIT_REPORT_PER_HOUR", 3600))],
             summary="Report a suspicious job or employer")
async def create_report(body: ReportCreate, user: CurrentUser, db: DBSession) -> dict[str, Any]:
    """One open report per user and target. Reporter identity is visible only to admins."""
    report = await admin_service.create_report(db, user, body.target_type, body.target_id, body.reason, body.description)
    return ok(ReportOut.model_validate(report), "Report submitted. Thank you.")


@router.get("/mine", response_model=ApiResponse[dict[str, Any]], summary="My reports")
async def my_reports(user: CurrentUser, db: DBSession, page: Annotated[int, Query(ge=1)] = 1,
                     page_size: Annotated[int, Query(ge=1, le=50)] = 20) -> dict[str, Any]:
    stmt = select(Report).where(Report.reporter_id == user.id)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = await db.scalars(stmt.order_by(Report.created_at.desc(), Report.id).offset((page - 1) * page_size).limit(page_size))
    return ok(paginate([ReportOut.model_validate(r).model_dump(mode="json") for r in rows], page, page_size, int(total)))
