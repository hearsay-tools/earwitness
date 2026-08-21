"""Filtrowanie i wyszukiwanie spotkań.

Wydzielone z warstwy HTTP, bo dokładnie ta sama logika obsługuje widok HTML
i endpoint JSON (`/api/meetings`).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import Case, Select, and_, case, exists, func, or_, select
from sqlalchemy.orm import Session

from webapp.models import (
    USER_STATUS_ORDER,
    Meeting,
    MeetingParticipant,
    Transcript,
    looks_like_bot,
    user_status_case,
    utcnow,
)

OCCURRED = func.coalesce(Meeting.started_at, Meeting.join_at)

SORTS = {
    "date_desc": "Newest",
    "date_asc": "Oldest",
    "duration_desc": "Longest",
    "duration_asc": "Shortest",
    "title_asc": "Title A-Z",
    "title_desc": "Title Z-A",
    "status_asc": "Status",
    "status_desc": "Status (rev.)",
    "transcript_asc": "Transcript",
    "transcript_desc": "Transcript (rev.)",
}

# Kolumna → (kierunek pierwotny, odwrotny). Klik w inną kolumnę bierze
# pierwotny; drugi klik w tę samą przełącza.
_SORT_PAIR: dict[str, tuple[str, str]] = {
    "date": ("date_desc", "date_asc"),
    "title": ("title_asc", "title_desc"),
    "duration": ("duration_desc", "duration_asc"),
    "status": ("status_asc", "status_desc"),
    "transcript": ("transcript_asc", "transcript_desc"),
}


def next_sort(column: str, current: str) -> str:
    """Następny klucz sortowania po kliknięciu w nagłówek kolumny."""
    primary, reverse = _SORT_PAIR[column]
    return reverse if current == primary else primary

# Rangi statusu użytkownika budujemy z tego samego wyrażenia CASE co filtry
# i fasetty — jedna para reguł, zero rozjazdu (patrz `user_status_case`).
def _user_status_rank() -> Case:
    expr = user_status_case(utcnow())
    return case(*[(expr == s, i) for i, s in enumerate(USER_STATUS_ORDER)], else_=9)

# Sortowanie po kolumnie „Transcript” zostaje — to informacja, nie filtr.
_TRANSCRIPT_RANK: Case = case(
    (Meeting.transcript_state == "none", 0),
    (Meeting.transcript_state == "queued", 1),
    (Meeting.transcript_state == "running", 2),
    (Meeting.transcript_state == "ready", 3),
    (Meeting.transcript_state == "failed", 4),
    else_=9,
)

@dataclass
class MeetingFilters:
    q: str = ""
    statuses: list[str] = field(default_factory=list)  # klucze USER_STATUS_ORDER
    participants: list[str] = field(default_factory=list)
    date_from: Optional[dt.date] = None
    date_to: Optional[dt.date] = None
    # Widok domyślny „Finished” seeduje `statuses` w warstwie HTTP (app.py).
    # `default_view=True` oznacza „statuses są zasiane, to nie jest filtr
    # użytkownika” — steruje chipami, podtytułem i as_query_dict.
    view: str = ""  # "" (domyślny Finished) | "all"
    default_view: bool = False
    sort: str = "date_desc"
    page: int = 1
    per_page: int = 25

    def is_active(self) -> bool:
        return bool(
            self.q or (self.statuses and not self.default_view) or self.participants
            or self.date_from or self.date_to
        )

    def as_query_dict(self, **overrides: Any) -> dict[str, Any]:
        d: dict[str, Any] = {}
        if self.q:
            d["q"] = self.q
        if self.statuses and not self.default_view:
            d["status"] = self.statuses
        if self.participants:
            d["participant"] = self.participants
        if self.date_from:
            d["date_from"] = self.date_from.isoformat()
        if self.date_to:
            d["date_to"] = self.date_to.isoformat()
        if self.view == "all":
            d["view"] = "all"
        if self.sort != "date_desc":
            d["sort"] = self.sort
        if self.page > 1:
            d["page"] = self.page
        d.update({k: v for k, v in overrides.items() if v not in (None, "", [])})
        for k, v in overrides.items():
            if v in (None, "", []):
                d.pop(k, None)
        return d


def _as_utc(day: dt.date, end: bool = False) -> dt.datetime:
    t = dt.time(23, 59, 59) if end else dt.time(0, 0, 0)
    return dt.datetime.combine(day, t, tzinfo=dt.timezone.utc)


def apply_filters(stmt: Select, f: MeetingFilters) -> Select:
    if f.q:
        # Każde słowo musi trafić — pozwala pisać "beata housy" i zawęzić.
        for token in f.q.lower().split():
            like = f"%{token}%"
            stmt = stmt.where(
                or_(
                    Meeting.search_blob.like(like),
                    func.lower(func.coalesce(Meeting.title, "")).like(like),
                )
            )
    if f.statuses:
        stmt = stmt.where(user_status_case(utcnow()).in_(f.statuses))
    if f.date_from:
        stmt = stmt.where(OCCURRED >= _as_utc(f.date_from))
    if f.date_to:
        stmt = stmt.where(OCCURRED <= _as_utc(f.date_to, end=True))
    for key in f.participants:
        stmt = stmt.where(
            exists().where(
                and_(
                    MeetingParticipant.meeting_id == Meeting.id,
                    MeetingParticipant.key == key.lower(),
                )
            )
        )
    return stmt


def _order(stmt: Select, sort: str) -> Select:
    if sort == "date_asc":
        return stmt.order_by(OCCURRED.asc().nulls_last(), Meeting.id)
    if sort == "duration_desc":
        return stmt.order_by(Meeting.duration_seconds.desc().nulls_last(), Meeting.id)
    if sort == "duration_asc":
        return stmt.order_by(Meeting.duration_seconds.asc().nulls_last(), Meeting.id)
    if sort == "title_asc":
        return stmt.order_by(func.lower(Meeting.title).asc(), Meeting.id)
    if sort == "title_desc":
        return stmt.order_by(func.lower(Meeting.title).desc(), Meeting.id)
    if sort == "status_asc":
        return stmt.order_by(_user_status_rank().asc(), OCCURRED.desc().nulls_last(), Meeting.id)
    if sort == "status_desc":
        return stmt.order_by(_user_status_rank().desc(), OCCURRED.desc().nulls_last(), Meeting.id)
    if sort == "transcript_asc":
        return stmt.order_by(_TRANSCRIPT_RANK.asc(), OCCURRED.desc().nulls_last(), Meeting.id)
    if sort == "transcript_desc":
        return stmt.order_by(_TRANSCRIPT_RANK.desc(), OCCURRED.desc().nulls_last(), Meeting.id)
    return stmt.order_by(OCCURRED.desc().nulls_last(), Meeting.id)


def search_meetings(session: Session, f: MeetingFilters) -> tuple[list[Meeting], int]:
    base = apply_filters(select(Meeting), f)
    total = session.execute(
        apply_filters(select(func.count(Meeting.id)), f)
    ).scalar_one()
    stmt = _order(base, f.sort).limit(f.per_page).offset((max(1, f.page) - 1) * f.per_page)
    return list(session.execute(stmt).scalars().unique()), int(total)


def status_facets(session: Session, f: MeetingFilters) -> dict[str, int]:
    """Liczniki statusów przy pozostałych filtrach (bez filtra statusu)."""
    probe = MeetingFilters(**{**f.__dict__, "statuses": []})
    expr = user_status_case(utcnow())
    rows = session.execute(
        apply_filters(
            select(expr, func.count(Meeting.id)).group_by(expr),
            probe,
        )
    ).all()
    return {str(k): int(v) for k, v in rows}


def untitled_count(session: Session) -> int:
    """Ile spotkań siedzi na tytule zastępczym zamiast tego z kalendarza."""
    return int(session.execute(
        select(func.count(Meeting.id)).where(
            Meeting.join_at.is_not(None) | Meeting.started_at.is_not(None),
            or_(
                Meeting.calendar_event_id.is_(None),
                Meeting.title_source.is_distinct_from("calendar"),
            ),
        )
    ).scalar_one())


def participant_facets(session: Session, limit: int = 200) -> list[dict[str, Any]]:
    """Lista osób do filtra — ludzie, nie boty, posortowani po liczbie spotkań.

    `is_bot` w bazie bywa nieaktualne (do niedawna sprawdzaliśmy tylko nazwę,
    a kalendarz podaje same adresy w rodzaju `fred@fireflies.ai`). Dlatego
    filtrujemy jeszcze raz przy odczycie — inaczej trzeba by przesynchronizować
    wszystko, żeby posprzątać listę.
    """
    rows = session.execute(
        select(
            MeetingParticipant.key,
            # `min` woli krótszą etykietę: „Jan Kowalski" zamiast
            # „Jan Kowalski (Acme)" — to prefiks, więc wygrywa.
            func.min(MeetingParticipant.name),
            func.max(MeetingParticipant.email),
            func.count(func.distinct(MeetingParticipant.meeting_id)).label("n"),
        )
        .where(MeetingParticipant.is_bot.is_(False))
        .group_by(MeetingParticipant.key)
        .order_by(func.count(func.distinct(MeetingParticipant.meeting_id)).desc())
        .limit(limit)
    ).all()
    out = []
    for key, name, email, n in rows:
        if looks_like_bot(name, email) or looks_like_bot(None, key):
            continue
        out.append({
            "key": key,
            "label": name or email or key,
            "email": email,
            # Wpis bez nazwy pochodzi wyłącznie z kalendarza — ktoś, kogo
            # zaproszono, ale kto nigdy nie pojawił się w żadnym nagraniu.
            "email_only": not name,
            "count": int(n),
        })
    out.sort(key=lambda r: (-r["count"], r["label"].lower()))
    return out


def transcript_rows(
    session: Session, f: MeetingFilters
) -> tuple[list[tuple[Transcript, Meeting]], int]:
    """Widok „Transkrypty” — te same filtry, ale zwracamy pary (transkrypt, spotkanie)."""
    joined = select(Transcript, Meeting).join(Meeting, Transcript.meeting_id == Meeting.id)
    stmt = apply_filters(joined, f)
    total = session.execute(
        apply_filters(
            select(func.count(Transcript.id)).join(Meeting, Transcript.meeting_id == Meeting.id), f
        )
    ).scalar_one()
    stmt = stmt.order_by(Transcript.created_at.desc()).limit(f.per_page).offset(
        (max(1, f.page) - 1) * f.per_page
    )
    return [(t, m) for t, m in session.execute(stmt).all()], int(total)
