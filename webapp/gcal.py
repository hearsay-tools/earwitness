"""Google Calendar (read-only) — tytuły spotkań i lista zaproszonych.

Recall nie zna tytułu spotkania ani zaproszonych, którzy nie przyszli. Kalendarz
zna oba. Dopasowanie idzie po identyfikatorze konferencji (kod Meet / id Zooma
z `conferenceData`), a gdy go nie ma — po nakładaniu się czasu.

Token: refresh_token per użytkownik (access_type=offline). Odświeżanie robimy
sami przez `https://oauth2.googleapis.com/token`, żeby nie ciągnąć całego
`google-api-python-client`.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Iterable, Optional

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from webapp.config import settings
from webapp.identity import resolve_meeting
from webapp.models import Meeting, User, looks_like_bot, utcnow
from webapp.recall_sync import (
    _replace_participants,
    parse_ts,
    rebuild_search_blob,
    rekey_participants,
)

log = logging.getLogger("webapp.gcal")

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/{cal}/events"

_MEET_RE = re.compile(r"meet\.google\.com/([a-z0-9-]+)", re.I)
_ZOOM_RE = re.compile(r"zoom\.us/j/(\d+)", re.I)
# Tolerancja dopasowania po czasie, gdy nie ma identyfikatora konferencji.
TIME_MATCH_WINDOW = dt.timedelta(minutes=20)
# Górny limit okna backfillu — kalendarz trzyma eventy w nieskończoność,
# a jedno spotkanie z zepsutą datą nie ma ciągnąć zapytania przez dekadę.
MAX_BACKFILL_SPAN = dt.timedelta(days=550)
# Margines wokół skrajnych spotkań, żeby event przesunięty względem bota
# wciąż mieścił się w oknie zapytania.
BACKFILL_MARGIN = dt.timedelta(days=1)


class CalendarError(RuntimeError):
    pass


def ensure_access_token(session: Session, user: User) -> str:
    """Zwróć ważny access token, odświeżając go w razie potrzeby."""
    if not user.google_refresh_token:
        raise CalendarError(
            "No refresh token — sign in again and grant calendar access."
        )
    fresh = (
        user.google_access_token
        and user.google_token_expires_at
        and user.google_token_expires_at > utcnow() + dt.timedelta(seconds=60)
    )
    if fresh:
        return user.google_access_token  # type: ignore[return-value]

    resp = httpx.post(
        TOKEN_URL,
        data={
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "refresh_token": user.google_refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise CalendarError(
            f"Token refresh failed ({resp.status_code}): {resp.text[:300]}"
        )
    payload = resp.json()
    user.google_access_token = payload["access_token"]
    user.google_token_expires_at = utcnow() + dt.timedelta(
        seconds=int(payload.get("expires_in", 3600))
    )
    session.commit()
    return user.google_access_token


def list_events(
    session: Session,
    user: User,
    time_min: dt.datetime,
    time_max: dt.datetime,
    calendar_id: str = "primary",
) -> list[dict[str, Any]]:
    token = ensure_access_token(session, user)
    out: list[dict[str, Any]] = []
    page_token: Optional[str] = None
    url = EVENTS_URL.format(cal=calendar_id)
    with httpx.Client(timeout=40) as client:
        while True:
            resp = client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "timeMin": time_min.isoformat().replace("+00:00", "Z"),
                    "timeMax": time_max.isoformat().replace("+00:00", "Z"),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": 250,
                    "pageToken": page_token,
                },
            )
            if resp.status_code == 403:
                raise CalendarError(
                    "Google denied calendar access (403). Check whether the "
                    "calendar.readonly scope was granted."
                )
            if resp.status_code != 200:
                raise CalendarError(
                    f"Calendar API {resp.status_code}: {resp.text[:300]}"
                )
            data = resp.json()
            out.extend(data.get("items") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
    return out


# --------------------------------------------------------------------------
# Dopasowanie event ↔ meeting
# --------------------------------------------------------------------------


def conference_ids(event: dict[str, Any]) -> set[str]:
    """Wyciąga identyfikatory konferencji z eventu (Meet code, Zoom id)."""
    ids: set[str] = set()
    blobs = [
        event.get("hangoutLink") or "",
        event.get("location") or "",
        event.get("description") or "",
    ]
    conf = event.get("conferenceData") or {}
    for ep in conf.get("entryPoints") or []:
        blobs.append(ep.get("uri") or "")
    if conf.get("conferenceId"):
        ids.add(str(conf["conferenceId"]).replace("-", "").lower())
    for blob in blobs:
        for m in _MEET_RE.finditer(blob):
            ids.add(m.group(1).replace("-", "").lower())
        for m in _ZOOM_RE.finditer(blob):
            ids.add(m.group(1).lower())
    return {i for i in ids if i}


def _event_window(
    event: dict[str, Any],
) -> tuple[Optional[dt.datetime], Optional[dt.datetime]]:
    def _p(node: Optional[dict]) -> Optional[dt.datetime]:
        if not node:
            return None
        raw = node.get("dateTime") or node.get("date")
        if not raw:
            return None
        ts = parse_ts(raw if "T" in raw else f"{raw}T00:00:00+00:00")
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=dt.timezone.utc)
        return ts

    return _p(event.get("start")), _p(event.get("end"))


def _attendee_rows(event: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    organizer = event.get("organizer") or {}
    if organizer.get("email") and not organizer.get("self", False):
        rows.append(
            {
                "name": organizer.get("displayName"),
                "email": organizer.get("email"),
                "is_host": True,
                "is_bot": False,
                "response_status": "organizer",
            }
        )
    for a in event.get("attendees") or []:
        if a.get("resource"):
            continue  # sale konferencyjne
        name = a.get("displayName")
        rows.append(
            {
                "name": name,
                "email": a.get("email"),
                "is_host": bool(a.get("organizer")),
                "is_bot": looks_like_bot(name, a.get("email"))
                or bool((a.get("email") or "").endswith(".calendar.google.com")),
                "response_status": a.get("responseStatus"),
            }
        )
    return rows


def has_conference(event: dict[str, Any]) -> bool:
    """Czy event w ogóle jest callem?

    Dopasowanie po czasie widzi wszystko, co stoi w kalendarzu — także bloki
    typu „Lunch" czy „Focus time". Bot dołącza wyłącznie do konferencji, więc
    kandydatem może być tylko event, który jakąkolwiek konferencję ma. Bez
    tego filtra szeroki backfill podpina spotkaniom tytuły przypadkowych
    wpisów, które akurat zaczynały się o tej samej godzinie.
    """
    if event.get("hangoutLink"):
        return True
    conf = event.get("conferenceData") or {}
    if conf.get("entryPoints") or conf.get("conferenceId"):
        return True
    blob = f"{event.get('location') or ''} {event.get('description') or ''}"
    return bool(
        _MEET_RE.search(blob)
        or _ZOOM_RE.search(blob)
        or "teams.microsoft.com" in blob.lower()
    )


def match_event(
    meeting: Meeting, events_by_conf: dict[str, dict], events: Iterable[dict]
) -> Optional[dict]:
    native = (meeting.meeting_native_id or "").replace("-", "").lower()
    if native and native in events_by_conf:
        return events_by_conf[native]

    anchor = meeting.occurred_at
    if not anchor:
        return None
    best, best_delta = None, TIME_MATCH_WINDOW
    for ev in events:
        if not has_conference(ev):
            continue
        # Znamy id konferencji spotkania, a event pokazuje inne — to inny call,
        # choćby zaczynał się co do minuty tak samo (równoległe standupy).
        ev_ids = conference_ids(ev)
        if native and ev_ids and native not in ev_ids:
            continue
        start, _end = _event_window(ev)
        if not start:
            continue
        delta = abs(start - anchor)
        if delta <= best_delta:
            best, best_delta = ev, delta
    return best


def meetings_without_calendar_title(session: Session) -> list[Meeting]:
    """Spotkania, którym kalendarz nigdy nie dołożył tytułu."""
    return list(
        session.execute(
            select(Meeting).where(
                Meeting.join_at.is_not(None) | Meeting.started_at.is_not(None),
                or_(
                    Meeting.calendar_event_id.is_(None),
                    Meeting.title_source.is_distinct_from("calendar"),
                ),
            )
        ).scalars()
    )


def _backfill_window(
    meetings: Iterable[Meeting],
) -> Optional[tuple[dt.datetime, dt.datetime]]:
    """Okno zapytania obejmujące podane spotkania, przycięte do rozsądku."""
    stamps = sorted(m.occurred_at for m in meetings if m.occurred_at)
    if not stamps:
        return None
    time_max = stamps[-1] + BACKFILL_MARGIN
    time_min = max(stamps[0] - BACKFILL_MARGIN, time_max - MAX_BACKFILL_SPAN)
    return time_min, time_max


def _apply_event(session: Session, meeting: Meeting, ev: dict[str, Any]) -> int:
    """Przepisz na spotkanie tytuł, link i zaproszonych z eventu."""
    meeting.calendar_event_id = ev.get("id")
    meeting.calendar_html_link = ev.get("htmlLink")
    meeting.calendar_organizer = (ev.get("organizer") or {}).get("email")
    summary = (ev.get("summary") or "").strip()
    if summary:
        meeting.title = summary[:500]
        meeting.title_source = "calendar"
    _replace_participants(session, meeting, "calendar", _attendee_rows(ev))
    session.flush()
    # Zaproszenie właśnie doszło — dopasuj nazwy z callu do adresów,
    # zanim ktokolwiek zobaczy listę uczestników.
    linked = resolve_meeting(meeting)["matched"]
    rekey_participants(meeting)
    session.flush()
    rebuild_search_blob(meeting)
    return linked


def enrich_meetings(
    session: Session,
    user: User,
    *,
    lookback_days: int = 30,
    lookahead_days: int = 14,
    only_missing: bool = False,
    on_progress=None,
) -> dict[str, Any]:
    """Dociągnij tytuły i zaproszonych z kalendarza użytkownika.

    Domyślnie okno jest liczone od „teraz" i pokrywa świeży sync. To jednak
    znaczy, że spotkanie starsze niż `lookback_days` (albo zaplanowane dalej
    niż `lookahead_days`) nigdy nie zobaczy kalendarza — i zostaje z tytułem
    fallbackowym na zawsze, bo kolejne synchronizacje ruszają to samo okno.
    `only_missing=True` odwraca zależność: okno wynika z dat spotkań, którym
    tytułu brakuje, więc backfill sięga tak daleko wstecz, jak trzeba.
    """
    if only_missing:
        meetings = meetings_without_calendar_title(session)
        window = _backfill_window(meetings)
        if window is None:
            return {
                "events": 0,
                "matched": 0,
                "linked_participants": 0,
                "meetings_scanned": 0,
            }
        time_min, time_max = window
    else:
        now = utcnow()
        time_min = now - dt.timedelta(days=lookback_days)
        time_max = now + dt.timedelta(days=lookahead_days)
        meetings = list(
            session.execute(
                select(Meeting).where(
                    Meeting.join_at.is_not(None) | Meeting.started_at.is_not(None)
                )
            ).scalars()
        )

    if on_progress:
        on_progress(
            f"calendar: {time_min:%Y-%m-%d}..{time_max:%Y-%m-%d}, "
            f"{len(meetings)} meetings to check"
        )
    events = list_events(session, user, time_min, time_max)
    if on_progress:
        on_progress(f"calendar: {len(events)} events")

    by_conf: dict[str, dict] = {}
    for ev in events:
        for cid in conference_ids(ev):
            by_conf.setdefault(cid, ev)

    matched = linked = 0
    for meeting in meetings:
        anchor = meeting.occurred_at
        if not anchor or not (time_min <= anchor <= time_max):
            continue
        ev = match_event(meeting, by_conf, events)
        if not ev:
            continue
        matched += 1
        linked += _apply_event(session, meeting, ev)

    user.calendar_synced_at = utcnow()
    session.commit()
    return {
        "events": len(events),
        "matched": matched,
        "linked_participants": linked,
        "meetings_scanned": len(meetings),
        "window": [time_min.date().isoformat(), time_max.date().isoformat()],
    }


def pick_calendar_user(session: Session) -> Optional[User]:
    """Do zadań w tle: pierwszy użytkownik z ważnym refresh tokenem."""
    return session.execute(
        select(User)
        .where(User.google_refresh_token.is_not(None), User.is_active.is_(True))
        .order_by(User.calendar_synced_at.asc().nulls_first(), User.id.asc())
        .limit(1)
    ).scalar_one_or_none()
