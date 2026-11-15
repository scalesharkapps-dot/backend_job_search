"""Reusable role/ownership dependencies for employer and admin endpoints.

Roles always come from the server-side ``users.role`` column (never from tokens
or request bodies). Ownership is enforced in the services by scoping every
query to the caller's own employer profile or user id.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from app.api.deps import DBSession, require_role
from app.models.employer import EmployerProfile
from app.models.user import User, UserRole
from app.services.employer_service import get_my_employer


@dataclass
class EmployerContext:
    user: User
    employer: EmployerProfile


async def get_employer_context(
    db: DBSession, user: Annotated[User, Depends(require_role(UserRole.EMPLOYER))]
) -> EmployerContext:
    """Authenticated employer-role user with a registered company profile."""
    return EmployerContext(user=user, employer=await get_my_employer(db, user))


Employer = Annotated[EmployerContext, Depends(get_employer_context)]
Admin = Annotated[User, Depends(require_role(UserRole.ADMIN))]
