"""Filtrowanie i wyszukiwanie spotkań."""

from __future__ import annotations

import datetime as dt

import pytest

from webapp.models import Meeting, MeetingParticipant, status_group
from webapp.queries import MeetingFilters, participant_facets, search_meetings, status_facets
from webapp.recall_sync import rebuild_search_blob


def _mk(session, mid, title, when, group="done", people=(), transcript="none", recording_id="rec-1"):
    m = Meeting(
        id=mid,
        title=title,
        platform="google_meet",
        meeting_native_id=mid[:8],
        started_at=when,
        status_code=group,
        status_group=group,
        transcript_state=transcript,
        recording_id=recording_id,
        duration_seconds=600,
    )
    session.add(m)
    session.flush()
    for name, source, is_bot in people:
        m.participants.append(MeetingParticipant(
            meeting_id=mid, source=source, key=name.lower(), name=name, is_bot=is_bot,
        ))
    session.flush()
    rebuild_search_blob(m)
    session.commit()
    return m


@pytest.fixture()
def seeded(session):
    base = dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.timezone.utc)
    _mk(session, "a" * 8, "Weekly Sync", base,
        people=[("Anna Nowak", "recall", False), ("Notetaker Bot", "recall", True)],
        transcript="ready")
    _mk(session, "b" * 8, "Project Kickoff", base + dt.timedelta(days=3), recording_id=None,
        people=[("Jan Kowalski", "recall", False), ("Anna Nowak", "calendar", False)])
    _mk(session, "c" * 8, "Retro zespołu", base - dt.timedelta(days=10), group="failed",
        people=[("Jan Kowalski", "recall", False)])
    _mk(session, "d" * 8, "Daily Standup", base + dt.timedelta(days=1))
    return session


def test_search_matches_title_and_people(seeded):
    rows, total = search_meetings(seeded, MeetingFilters(q="weekly"))
    assert total == 1 and rows[0].title == "Weekly Sync"

    rows, total = search_meetings(seeded, MeetingFilters(q="jan"))
    assert total == 2


def test_search_requires_every_token(seeded):
    _, total = search_meetings(seeded, MeetingFilters(q="jan kickoff"))
    assert total == 1
    _, total = search_meetings(seeded, MeetingFilters(q="jan nieistniejace"))
    assert total == 0


def test_filter_by_status_group(seeded):
    _, total = search_meetings(seeded, MeetingFilters(statuses=["failed"]))
    assert total == 1
    _, total = search_meetings(seeded, MeetingFilters(statuses=["in_meeting", "upcoming"]))
    assert total == 0


def test_filter_by_date_range(seeded):
    f = MeetingFilters(date_from=dt.date(2026, 8, 1), date_to=dt.date(2026, 8, 2))
    _, total = search_meetings(seeded, f)
    assert total == 2  # Aug 1 (Weekly Sync) + Aug 2 (Daily Standup)


def test_filter_by_participant_is_conjunctive(seeded):
    """Dwie osoby = spotkania, gdzie byli oboje (a nie suma)."""
    _, total = search_meetings(seeded, MeetingFilters(participants=["anna nowak"]))
    assert total == 2  # raz z Recall, raz z kalendarza
    _, total = search_meetings(
        seeded, MeetingFilters(participants=["anna nowak", "jan kowalski"])
    )
    assert total == 1


def test_filter_by_user_status(seeded):
    _, total = search_meetings(seeded, MeetingFilters(statuses=["failed"]))
    assert total == 1
    _, total = search_meetings(seeded, MeetingFilters(statuses=["no_recording"]))
    assert total == 1  # „Project Kickoff”: done bez nagrania
    _, total = search_meetings(seeded, MeetingFilters(statuses=["to_process"]))
    assert total == 1  # „Daily Standup”: done z nagraniem, bez transkryptu
    _, total = search_meetings(seeded, MeetingFilters(statuses=["ready", "failed", "no_recording"]))
    assert total == 3


def test_status_sort_ranks_lifecycle_first(seeded):
    rows, _ = search_meetings(seeded, MeetingFilters(sort="status_asc"))
    # ranga: to_process(3) < ready(4) < failed(5) < no_recording(6)
    assert [r.id for r in rows] == ["d" * 8, "a" * 8, "c" * 8, "b" * 8]
    assert [r.user_status for r in rows] == ["to_process", "ready", "failed", "no_recording"]


def test_sort_orders(seeded):
    rows, _ = search_meetings(seeded, MeetingFilters(sort="date_desc"))
    assert rows[0].title == "Project Kickoff"
    rows, _ = search_meetings(seeded, MeetingFilters(sort="date_asc"))
    assert rows[0].title == "Retro zespołu"
    rows, _ = search_meetings(seeded, MeetingFilters(sort="title_asc"))
    assert [r.title for r in rows] == sorted((r.title for r in rows), key=str.lower)
    rows, _ = search_meetings(seeded, MeetingFilters(sort="title_desc"))
    assert [r.title for r in rows] == sorted((r.title for r in rows), key=str.lower, reverse=True)
    rows, _ = search_meetings(seeded, MeetingFilters(sort="duration_desc"))
    secs = [r.duration_seconds or 0 for r in rows]
    assert secs == sorted(secs, reverse=True)
    rows, _ = search_meetings(seeded, MeetingFilters(sort="duration_asc"))
    secs = [r.duration_seconds or 0 for r in rows]
    assert secs == sorted(secs)


def test_next_sort_toggles_and_resets_to_primary():
    from webapp.queries import next_sort

    assert next_sort("date", "date_desc") == "date_asc"
    assert next_sort("date", "date_asc") == "date_desc"
    assert next_sort("date", "status_asc") == "date_desc"
    assert next_sort("title", "title_asc") == "title_desc"
    assert next_sort("title", "date_desc") == "title_asc"
    assert next_sort("duration", "duration_desc") == "duration_asc"
    assert next_sort("status", "status_asc") == "status_desc"
    assert next_sort("transcript", "transcript_desc") == "transcript_asc"


def test_pagination(seeded):
    rows, total = search_meetings(seeded, MeetingFilters(per_page=2, page=2))
    assert total == 4 and len(rows) == 2


def test_status_facets_ignore_status_filter(seeded):
    facets = status_facets(seeded, MeetingFilters(statuses=["failed"]))
    assert facets == {"ready": 1, "failed": 1, "no_recording": 1, "to_process": 1}


def test_participant_facets_exclude_bots(seeded):
    labels = [p["label"] for p in participant_facets(seeded)]
    assert "Notetaker Bot" not in labels
    assert labels[0] == "Anna Nowak"  # najwięcej spotkań


@pytest.mark.parametrize("code,expected", [
    ("in_call_recording", "recording"),
    ("done", "done"),
    ("media_expired", "expired"),
    ("fatal", "failed"),
    ("joining_call", "joining"),
    (None, "scheduled"),
])
def test_status_group_mapping(code, expected):
    assert status_group(code) == expected
