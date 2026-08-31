"""Tożsamość uczestnika — jedna osoba to jedna pozycja w filtrze."""

from __future__ import annotations

from webapp.models import Meeting, looks_like_bot
from webapp.queries import participant_facets
from webapp.recall_sync import _replace_participants


def _meeting(session, mid="bot-1"):
    m = Meeting(id=mid, title="Spotkanie", status_group="done")
    session.add(m)
    session.flush()
    return m


def test_email_is_the_canonical_key(session):
    """Adres wygrywa z nazwą — nazwa jest tylko etykietą."""
    m = _meeting(session)
    _replace_participants(
        session,
        m,
        "recall",
        [
            {"name": "Marek Zieliński", "email": "mzielinski@acme.com"},
        ],
    )
    session.flush()
    assert [p.key for p in m.participants] == ["mzielinski@acme.com"]


def test_key_migrates_from_name_to_email(session):
    """Recall daje samą nazwę; adres dochodzi później z dopasowania."""
    m = _meeting(session)
    _replace_participants(
        session, m, "recall", [{"name": "Jan Kowalski", "email": None}]
    )
    session.flush()
    assert [p.key for p in m.participants] == ["jan kowalski"]

    _replace_participants(
        session,
        m,
        "recall",
        [
            {"name": "Jan Kowalski", "email": "jkowalski@acme.com"},
        ],
    )
    session.flush()
    keys = [p.key for p in m.participants]
    assert keys == ["jkowalski@acme.com"], "wiersz miał zmienić klucz, nie powielić się"


def test_matched_email_survives_a_resync_without_email(session):
    """Recall nadal nie podaje adresu — nasze dopasowanie nie może zniknąć."""
    m = _meeting(session)
    _replace_participants(
        session, m, "recall", [{"name": "Jan Kowalski", "email": None}]
    )
    session.flush()
    p = m.participants[0]
    p.email = "jkowalski@acme.com"
    p.email_source = "matched"
    p.match_score = 0.95
    session.flush()

    _replace_participants(
        session, m, "recall", [{"name": "Jan Kowalski", "email": None}]
    )
    session.flush()
    assert m.participants[0].email == "jkowalski@acme.com"
    assert m.participants[0].email_source == "matched"


def test_same_person_from_both_sources_is_not_duplicated_within_source(session):
    m = _meeting(session)
    _replace_participants(
        session,
        m,
        "calendar",
        [
            {"name": "Jan Kowalski", "email": "jkowalski@acme.com"},
            {"name": None, "email": "jkowalski@acme.com"},
        ],
    )
    session.flush()
    assert len(m.participants) == 1


def test_removed_attendee_disappears(session):
    m = _meeting(session)
    _replace_participants(
        session,
        m,
        "calendar",
        [
            {"name": "A", "email": "a@x.pl"},
            {"name": "B", "email": "b@x.pl"},
        ],
    )
    session.flush()
    _replace_participants(session, m, "calendar", [{"name": "A", "email": "a@x.pl"}])
    session.commit()
    assert [p.name for p in m.participants] == ["A"]


def test_sources_stay_independent(session):
    """Zamiana listy z kalendarza nie rusza tego, co dał Recall."""
    m = _meeting(session)
    _replace_participants(
        session, m, "recall", [{"name": "Jan Kowalski", "email": None}]
    )
    _replace_participants(session, m, "calendar", [{"name": None, "email": "kto@x.pl"}])
    session.flush()
    _replace_participants(session, m, "calendar", [])
    session.commit()
    assert [(p.source, p.name) for p in m.participants] == [("recall", "Jan Kowalski")]


# --------------------------------------------------------------------------
# Boty
# --------------------------------------------------------------------------


def test_bot_detected_by_email_when_name_missing():
    assert looks_like_bot(None, "fred@fireflies.ai")
    assert looks_like_bot("Alex's Fathom Notetaker", None)
    assert not looks_like_bot("Jan Kowalski", "jkowalski@acme.com")


def test_bots_do_not_reach_the_participant_filter(session):
    m = _meeting(session)
    _replace_participants(
        session,
        m,
        "calendar",
        [
            {"name": None, "email": "fred@fireflies.ai"},
            {"name": None, "email": "jkowalski@acme.com"},
        ],
    )
    session.commit()
    labels = [p["label"] for p in participant_facets(session)]
    assert labels == ["jkowalski@acme.com"]


def test_facet_marks_entries_without_a_name(session):
    m = _meeting(session)
    _replace_participants(
        session, m, "recall", [{"name": "Jan Kowalski", "email": None}]
    )
    _replace_participants(session, m, "calendar", [{"name": None, "email": "kto@x.pl"}])
    session.commit()
    facets = {p["label"]: p["email_only"] for p in participant_facets(session)}
    assert facets == {"Jan Kowalski": False, "kto@x.pl": True}
