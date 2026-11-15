"""Bootstrap the first administrator (server-side only; never exposed via the API).

Usage (inside the project venv, with DATABASE_URL configured):

    python -m app.scripts.promote_admin --email someone@example.com --reason "initial admin"

The user must have signed in at least once (so the account exists in ``users``).
The change is written to the admin audit log with no actor.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.database import dispose_engine, get_sessionmaker
from app.models.user import User, UserRole
from app.services import audit


async def promote(email: str, reason: str) -> int:
    async with get_sessionmaker()() as db:
        user = await db.scalar(select(User).where(User.email == email.lower()))
        if user is None:
            print("No user with that email. Sign in once through the app first.", file=sys.stderr)
            return 1
        if user.role == UserRole.ADMIN:
            print("User is already an admin.")
            return 0
        previous = user.role
        user.role = UserRole.ADMIN
        user.is_active = True
        audit.record(db, actor_id=None, action="user.role_change.bootstrap", target_type="user", target_id=user.id,
                     previous={"role": previous.value}, new={"role": "admin"}, reason=reason)
        await db.commit()
        print(f"Promoted user {user.id} to admin.")
    return 0


async def _run(email: str, reason: str) -> int:
    try:
        return await promote(email, reason)
    finally:
        await dispose_engine()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--reason", default="bootstrap administrator")
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(_run(args.email, args.reason)))


if __name__ == "__main__":
    main()
