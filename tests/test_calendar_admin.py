"""Operator adoption must verify ownership before persisting any identity."""

import pytest
from sqlalchemy import select
from webapp.models import User


@pytest.fixture
def owner(session):
    user = User(google_sub="owner", email="owner@example.com", is_active=True)
    session.add(user)
    session.commit()
    return user


def remote_user(email="owner@example.com", external_id="dashboard-owner"):
    return {
        "id": "remote-user",
        "external_id": external_id,
        "connections": [
            {"platform": "google", "connected": True, "email": email, "id": "gcal"}
        ],
        "preferences": {"record_external": True, "bot_name": "Existing bot"},
    }


def test_adoption_verifies_google_email_and_reuses_identity(
    session, owner, monkeypatch
):
    from webapp.calendar_admin import adopt_identity
    from webapp.models import CalendarIdentity
    from webapp.recall_calendar import CalendarClient

    monkeypatch.setattr(CalendarClient, "list_users", lambda email: [remote_user()])
    first = adopt_identity(session, owner.email, "dashboard-owner")
    second = adopt_identity(session, owner.email, "dashboard-owner")
    assert first.external_id == second.external_id == "dashboard-owner"
    assert first.user_id == owner.id
    assert first.setup_pending is False
    assert len(session.scalars(select(CalendarIdentity)).all()) == 1


@pytest.mark.parametrize(
    "case",
    ["different_email", "different_id", "microsoft", "disconnected", "duplicate"],
)
def test_adoption_rejects_unverified_remote_ownership(
    session, owner, monkeypatch, case
):
    from webapp.calendar_admin import adopt_identity
    from webapp.models import CalendarIdentity
    from webapp.recall_calendar import CalendarClient, CalendarError

    candidate = remote_user()
    if case == "different_email":
        candidate["connections"][0]["email"] = "someone@example.com"
    elif case == "different_id":
        candidate["external_id"] = "different-id"
    elif case == "microsoft":
        candidate["connections"][0]["platform"] = "microsoft"
    elif case == "disconnected":
        candidate["connections"][0]["connected"] = False
    candidates = [candidate, candidate] if case == "duplicate" else [candidate]
    monkeypatch.setattr(CalendarClient, "list_users", lambda email: candidates)
    with pytest.raises(CalendarError):
        adopt_identity(session, owner.email, "dashboard-owner")
    assert session.scalars(select(CalendarIdentity)).all() == []


def test_adoption_cannot_replace_an_issued_identity(session, owner, monkeypatch):
    from webapp.calendar_admin import adopt_identity
    from webapp.models import CalendarIdentity
    from webapp.recall_calendar import CalendarClient, CalendarError

    session.add(CalendarIdentity(user_id=owner.id, external_id="already-issued"))
    session.commit()
    monkeypatch.setattr(CalendarClient, "list_users", lambda email: [remote_user()])
    with pytest.raises(CalendarError):
        adopt_identity(session, owner.email, "dashboard-owner")
    assert session.get(CalendarIdentity, owner.id).external_id == "already-issued"


def test_adoption_cannot_take_another_local_users_identity(session, owner, monkeypatch):
    from webapp.calendar_admin import adopt_identity
    from webapp.models import CalendarIdentity
    from webapp.recall_calendar import CalendarClient, CalendarError

    other = User(google_sub="other", email="other@example.com")
    session.add(other)
    session.commit()
    session.add(CalendarIdentity(user_id=other.id, external_id="dashboard-owner"))
    session.commit()
    monkeypatch.setattr(CalendarClient, "list_users", lambda email: [remote_user()])
    with pytest.raises(CalendarError):
        adopt_identity(session, owner.email, "dashboard-owner")
    assert session.get(CalendarIdentity, owner.id) is None


@pytest.mark.parametrize("active", [False, None])
def test_adoption_requires_existing_active_local_user(
    session, owner, monkeypatch, active
):
    from webapp.calendar_admin import adopt_identity
    from webapp.recall_calendar import CalendarClient, CalendarError

    owner.is_active = False
    session.commit()
    monkeypatch.setattr(
        CalendarClient, "list_users", lambda email: pytest.fail("Unexpected lookup")
    )
    with pytest.raises(CalendarError):
        adopt_identity(
            session,
            owner.email if active is False else "missing@example.com",
            "dashboard-owner",
        )


def test_operator_cli_persists_verified_mapping_without_printing_identity(
    session, owner, monkeypatch, capsys
):
    from webapp.calendar_admin import main
    from webapp.models import CalendarIdentity
    from webapp.recall_calendar import CalendarClient

    monkeypatch.setattr(CalendarClient, "list_users", lambda email: [remote_user()])
    assert (
        main(["adopt", "--user-email", owner.email, "--external-id", "dashboard-owner"])
        == 0
    )
    session.expire_all()
    assert session.get(CalendarIdentity, owner.id).external_id == "dashboard-owner"
    output = capsys.readouterr().out
    assert "adopted" in output
    assert owner.email not in output
    assert "dashboard-owner" not in output
