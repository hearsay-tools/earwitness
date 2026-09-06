"""Stable ownership mapping between local and Recall Calendar users."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from webapp.models import CalendarIdentity, User
from webapp.recall_calendar import CalendarClient


class CalendarIdentityAdoptionRequired(RuntimeError):
    """An existing Recall Google connection must be adopted by an operator."""


def find_identity(session: Session, user: User) -> CalendarIdentity | None:
    """Return the persisted mapping without allocating or calling Recall."""

    if user.id is None:
        return None
    return session.get(CalendarIdentity, user.id)


def _has_connected_google_owner(users: Sequence[dict], email: str) -> bool:
    expected = email.strip().casefold()
    for calendar_user in users:
        for connection in calendar_user.get("connections") or []:
            connection_email = str(connection.get("email") or "").strip().casefold()
            if (
                connection.get("platform") == "google"
                and connection.get("connected") is True
                # The workspace request itself is filtered by email. Older
                # connection payloads may omit the optional email field, so a
                # connected Google result without it must be treated as owned.
                and (not connection_email or connection_email == expected)
            ):
                return True
    return False


def _new_external_id() -> str:
    return str(uuid.uuid4())


def ensure_identity(
    session: Session,
    user: User,
    *,
    workspace_users: Sequence[dict] | None = None,
) -> CalendarIdentity:
    """Find or commit one stable identity before any per-user authentication.

    For a fresh mapping, the default path first asks Recall's workspace endpoint
    for this email. Callers that already completed that lookup can pass the
    exact result in ``workspace_users``. A connected Google owner is never
    replaced automatically because that could lose scheduled calendar bots;
    the operator adoption flow must bind its known external ID instead.
    """

    existing = find_identity(session, user)
    if existing is not None:
        return existing
    if user.id is None:
        raise ValueError("The local user must be persisted before calendar setup.")

    users = (
        CalendarClient.list_users(user.email)
        if workspace_users is None
        else workspace_users
    )
    if _has_connected_google_owner(users, user.email):
        raise CalendarIdentityAdoptionRequired(
            "This Google calendar is already connected. An operator must adopt "
            "the existing Recall calendar identity before setup can continue."
        )

    for _attempt in range(3):
        candidate = CalendarIdentity(
            user_id=user.id,
            external_id=_new_external_id(),
            setup_pending=True,
        )
        try:
            with session.begin_nested():
                session.add(candidate)
                session.flush()
            session.commit()
            return candidate
        except IntegrityError:
            session.rollback()
            winner = find_identity(session, user)
            if winner is not None:
                return winner

    raise RuntimeError("Could not allocate a unique calendar identity.")
