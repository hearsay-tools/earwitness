"""Dopasowanie eventów kalendarza do botów + eksport transkryptu."""

from __future__ import annotations

import datetime as dt

from webapp.auth import is_domain_allowed
from webapp.config import settings
from webapp.gcal import (
    MAX_BACKFILL_SPAN,
    _attendee_rows,
    _backfill_window,
    conference_ids,
    match_event,
    meetings_without_calendar_title,
)
from webapp.models import Meeting
from webapp.tasks import parse_transcript, to_markdown, to_vtt


def _event(**kw):
    base = {
        "id": "ev1",
        "summary": "Sales Review",
        "start": {"dateTime": "2026-08-01T10:00:00Z"},
        "end": {"dateTime": "2026-08-01T11:00:00Z"},
    }
    base.update(kw)
    return base


def test_conference_ids_from_hangout_link():
    ev = _event(hangoutLink="https://meet.google.com/hvw-huhn-qts")
    assert "hvwhuhnqts" in conference_ids(ev)


def test_conference_ids_from_entry_points_and_description():
    ev = _event(
        conferenceData={"entryPoints": [{"uri": "https://zoom.us/j/123456789"}]},
        description="Dołącz: https://meet.google.com/abc-defg-hij",
    )
    ids = conference_ids(ev)
    assert "123456789" in ids and "abcdefghij" in ids


def test_match_event_prefers_conference_id_over_time():
    right = _event(id="right", hangoutLink="https://meet.google.com/hvw-huhn-qts")
    decoy = _event(
        id="decoy", summary="Inne", start={"dateTime": "2026-08-01T10:01:00Z"}
    )
    m = Meeting(
        id="bot",
        meeting_native_id="hvw-huhn-qts",
        started_at=dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.timezone.utc),
    )
    by_conf = {"hvwhuhnqts": right}
    assert match_event(m, by_conf, [decoy, right])["id"] == "right"


def test_match_event_falls_back_to_time_window():
    ev = _event(id="close", hangoutLink="https://meet.google.com/hvw-huhn-qts")
    m = Meeting(
        id="bot", started_at=dt.datetime(2026, 8, 1, 10, 5, tzinfo=dt.timezone.utc)
    )
    assert match_event(m, {}, [ev])["id"] == "close"


def test_match_event_ignores_distant_events():
    ev = _event(id="far", hangoutLink="https://meet.google.com/hvw-huhn-qts")
    m = Meeting(
        id="bot", started_at=dt.datetime(2026, 8, 1, 14, 0, tzinfo=dt.timezone.utc)
    )
    assert match_event(m, {}, [ev]) is None


def test_match_event_ignores_events_without_a_call():
    """Blok „Lunch" o tej samej godzinie nie jest spotkaniem bota."""
    ev = _event(id="lunch", summary="Lunch")
    m = Meeting(
        id="bot", started_at=dt.datetime(2026, 8, 1, 10, 5, tzinfo=dt.timezone.utc)
    )
    assert match_event(m, {}, [ev]) is None


def test_match_event_ignores_a_different_call_at_the_same_time():
    """Równoległy standup ma swój kod Meet — tytuł z niego byłby kłamstwem."""
    other = _event(id="other", hangoutLink="https://meet.google.com/aaa-bbbb-ccc")
    m = Meeting(
        id="bot",
        meeting_native_id="hvw-huhn-qts",
        started_at=dt.datetime(2026, 8, 1, 10, 5, tzinfo=dt.timezone.utc),
    )
    assert match_event(m, {}, [other]) is None


def test_backfill_window_covers_the_oldest_meeting():
    """Okno backfillu wynika z dat spotkań, nie z zegara.

    To jest sedno buga: zwykły sync patrzy tylko wokół „teraz", więc spotkanie
    sprzed pół roku nigdy nie widziało kalendarza — a event z tytułem cały
    czas tam leży.
    """
    old = Meeting(
        id="a", started_at=dt.datetime(2026, 2, 3, 9, 0, tzinfo=dt.timezone.utc)
    )
    new = Meeting(
        id="b", started_at=dt.datetime(2026, 9, 9, 8, 0, tzinfo=dt.timezone.utc)
    )
    time_min, time_max = _backfill_window([new, old])
    assert time_min < old.started_at
    assert time_max > new.started_at


def test_backfill_window_is_clipped_to_a_sane_span():
    """Jedno spotkanie z rozjechaną datą nie ciągnie zapytania przez dekadę."""
    ancient = Meeting(
        id="a", started_at=dt.datetime(1999, 1, 1, tzinfo=dt.timezone.utc)
    )
    recent = Meeting(id="b", started_at=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc))
    time_min, time_max = _backfill_window([ancient, recent])
    assert time_max - time_min <= MAX_BACKFILL_SPAN


def test_backfill_window_is_none_without_dated_meetings():
    assert _backfill_window([Meeting(id="a")]) is None


def test_meetings_without_calendar_title_picks_fallbacks_only(session):
    when = dt.datetime(2026, 3, 1, 9, 0, tzinfo=dt.timezone.utc)
    session.add_all(
        [
            Meeting(
                id="titled",
                started_at=when,
                title="Sprint review",
                title_source="calendar",
                calendar_event_id="ev1",
            ),
            Meeting(
                id="fallback",
                started_at=when,
                title="Google Meet — 2026-03-01",
                title_source="fallback",
            ),
            # Kalendarz podpiął event, ale bez `summary` — tytułu wciąż brak.
            Meeting(
                id="linked-untitled",
                started_at=when,
                calendar_event_id="ev2",
                title_source="fallback",
            ),
            # Bot bez daty nie ma czego szukać w kalendarzu.
            Meeting(id="undated", title_source="fallback"),
        ]
    )
    session.commit()
    ids = {m.id for m in meetings_without_calendar_title(session)}
    assert ids == {"fallback", "linked-untitled"}


def test_attendee_rows_skip_rooms_and_flag_bots():
    ev = _event(
        attendees=[
            {"email": "a@x.pl", "displayName": "Anna", "responseStatus": "accepted"},
            {"email": "sala@resource.calendar.google.com", "resource": True},
            {"email": "bot@fireflies.ai", "displayName": "Fireflies.ai Notetaker"},
        ]
    )
    rows = _attendee_rows(ev)
    assert [r["email"] for r in rows] == ["a@x.pl", "bot@fireflies.ai"]
    assert rows[1]["is_bot"] is True


# --------------------------------------------------------------------------

TRANSCRIPT = (
    "Jan Kowalski [00:00:03] Cześć, dzięki że jesteś.\n"
    "Anna Nowak [00:00:07] Nie ma sprawy.\n"
    "Jan Kowalski [00:01:12] Ok, lecimy z agendą.\n"
)


def test_parse_transcript():
    utts = parse_transcript(TRANSCRIPT)
    assert len(utts) == 3
    assert utts[2]["seconds"] == 72
    assert utts[0]["speaker"] == "Jan Kowalski"


def test_parse_transcript_joins_wrapped_lines():
    utts = parse_transcript(
        "Ktoś [00:00:01] pierwsza linia\nkontynuacja bez nagłówka\n"
    )
    assert len(utts) == 1
    assert utts[0]["text"].endswith("kontynuacja bez nagłówka")


def test_vtt_has_monotonic_cues():
    vtt = to_vtt(parse_transcript(TRANSCRIPT), total_seconds=90)
    assert vtt.startswith("WEBVTT")
    assert "00:00:03.000 --> 00:00:07.000" in vtt
    assert "<v Jan Kowalski>" in vtt


def test_markdown_contains_header_and_people():
    m = Meeting(
        id="bot",
        title="Sales Review",
        started_at=dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.timezone.utc),
    )
    md = to_markdown(m, parse_transcript(TRANSCRIPT))
    assert md.startswith("# Sales Review")
    assert "`bot`" in md


# --------------------------------------------------------------------------


def test_domain_whitelist(monkeypatch):
    monkeypatch.setattr(settings, "allowed_domains", [])
    assert is_domain_allowed("ktos@gmail.com")

    monkeypatch.setattr(settings, "allowed_domains", ["acme.com"])
    assert is_domain_allowed("ktos@acme.com")
    assert is_domain_allowed("ktos@inna.pl", hd="acme.com")
    assert not is_domain_allowed("ktos@gmail.com")
