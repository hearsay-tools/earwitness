"""Operator-only adoption of an existing, ownership-verified Calendar V1 user."""

from __future__ import annotations

import argparse

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from webapp.models import CalendarIdentity, User
from webapp.recall_calendar import CalendarClient, CalendarError


def adopt_identity(
    session: Session, user_email: str, external_id: str
) -> CalendarIdentity:
    user = session.scalar(
        select(User).where(
            User.email == user_email.strip().lower(), User.is_active.is_(True)
        )
    )
    if user is None:
        raise CalendarError("An active Earwitness account with that email is required.")
    existing = session.get(CalendarIdentity, user.id)
    if existing and existing.external_id != external_id:
        raise CalendarError(
            "This account already has a different Recall identity. Adoption cannot replace it."
        )
    claimed = session.scalar(
        select(CalendarIdentity).where(CalendarIdentity.external_id == external_id)
    )
    if claimed and claimed.user_id != user.id:
        raise CalendarError(
            "That Recall identity is already assigned to another account."
        )

    # The workspace lookup does not mint a token or create a remote identity.
    matches = [
        item
        for item in CalendarClient.list_users(user.email)
        if item.get("external_id") == external_id
        and any(
            connection.get("platform") == "google"
            and connection.get("connected") is True
            and (connection.get("email") or "").strip().lower() == user.email.lower()
            for connection in item.get("connections", [])
        )
    ]
    if not external_id.strip() or len(matches) != 1:
        raise CalendarError(
            "Cannot verify a unique connected Google account for this identity and email. "
            "Check the dashboard mapping or use the documented controlled reconnection."
        )
    if existing:
        return existing
    identity = CalendarIdentity(
        user_id=user.id,
        external_id=external_id,
        setup_pending=False,
    )
    try:
        with session.begin_nested():
            session.add(identity)
            session.flush()
        session.commit()
    except IntegrityError:
        session.rollback()
        # Concurrent identical adoption is safe; all other races fail closed.
        winner = session.get(CalendarIdentity, user.id)
        if winner and winner.external_id == external_id:
            return winner
        raise CalendarError(
            "The calendar mapping changed during adoption. Check it before retrying."
        ) from None
    return identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    adopt = commands.add_parser("adopt", help="Verify and adopt a dashboard identity")
    adopt.add_argument("--user-email", required=True)
    adopt.add_argument("--external-id", required=True)
    args = parser.parse_args(argv)

    from webapp.db import init_db, session_scope

    init_db()
    try:
        with session_scope() as session:
            adopt_identity(session, args.user_email, args.external_id)
    except CalendarError as error:
        parser.exit(1, f"Adoption failed: {error}\n")
    print("Calendar identity verified and adopted. Existing preferences are unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
