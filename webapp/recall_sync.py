"""Synchronizacja botów Recall.ai → tabela `meetings`.

Recall jest źródłem prawdy dla: statusu bota, czasów, nagrań, TTL mediów oraz
listy osób, które realnie były w callu (participants.json). Tytuł spotkania i
zaproszonych dokłada `webapp/gcal.py` z Google Calendar.

Sync jest idempotentny — leci po `bot_id` i nadpisuje tylko pola pochodzące
z Recall (tytuł z kalendarza zostaje nietknięty).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session
from transcripts.recall_client import RecallClient, RecallConfig

from webapp.config import settings
from webapp.models import (
    Meeting,
    MeetingParticipant,
    looks_like_bot,
    status_group,
    utcnow,
)

log = logging.getLogger("webapp.recall_sync")


def parse_ts(value: Optional[str]) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def make_client() -> RecallClient:
    config = RecallConfig.from_env(
        api_key=settings.recall_api_key or None,
        region=settings.recall_region or None,
    )
    return RecallClient(config)


def _fetch_json(url: str, timeout: float = 30.0) -> Any:
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _last_status(
    bot: dict,
) -> tuple[Optional[str], Optional[str], Optional[dt.datetime]]:
    changes = bot.get("status_changes") or []
    if not changes:
        return None, None, None
    last = changes[-1]
    return last.get("code"), last.get("sub_code"), parse_ts(last.get("created_at"))


def _pick_recording(bot: dict) -> Optional[dict]:
    """Bierzemy najdłuższe/najświeższe nagranie — w praktyce bot ma jedno."""
    recs = bot.get("recordings") or []
    if not recs:
        return None
    return sorted(recs, key=lambda r: r.get("started_at") or "", reverse=True)[0]


def _norm_key(name: Optional[str], email: Optional[str]) -> str:
    """Klucz tożsamości uczestnika. **Adres e-mail jest kanoniczny.**

    Adres jest stabilny i zawsze obecny w kalendarzu; nazwa wyświetlana to
    tylko etykieta i jedna osoba potrafi mieć ich kilka („Jan Kowalski",
    „Jan Kowalski (Acme)"). Nazwa jest kluczem wyłącznie wtedy, gdy
    adresu nie znamy i nie udało się go dopasować (patrz `webapp/identity.py`).
    """
    e = (email or "").strip().lower()
    if e:
        return e
    return " ".join((name or "").strip().lower().split())


def _read_rec_meta(rec_dir: Path) -> Optional[dict]:
    try:
        return json.loads((rec_dir / "recording.json").read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — brak pliku albo śmieci w środku
        return None


def _rec_dir_ready(rec_dir: Path) -> bool:
    """Czy w katalogu nagrania leży komplet potrzebny pipeline'owi?

    Pipeline potrzebuje dokładnie dwóch rzeczy: `audio_mixed*.mp3` (wejście ASR)
    oraz artefaktu `audio_separate` w formacie **raw** z manifestem i plikami
    partów (wejście diaryzacji energią). Wersja ogg nie wystarcza.
    """
    meta = _read_rec_meta(rec_dir)
    if meta is None:
        return False
    if not any(rec_dir.glob("audio_mixed*.mp3")):
        return False
    raw_artifacts = [
        a for a in meta.get("audio_separate", []) if a.get("format") == "raw"
    ]
    if not raw_artifacts:
        return False
    manifest = rec_dir / "audio_separate" / f"parts_{raw_artifacts[0]['id'][:8]}.json"
    if not manifest.exists():
        return False
    return any((rec_dir / "audio_separate").rglob("*.raw"))


def _discover_recording_dir(bot_id: str) -> Optional[Path]:
    """Znajdź nagranie na dysku, gdy baza nie zna właściwego `recording_id`.

    Po wygaśnięciu mediów Recall przestaje zwracać `recordings` w payloadzie
    bota, więc spotkanie zostaje z `recording_id = NULL` — nawet jeśli audio
    ściągnęliśmy przed TTL i komplet leży lokalnie. Bez tego skanu taki
    komplet jest dla webappki niewidoczny na zawsze.
    """
    bot_dir = settings.recall_dir / bot_id
    if not bot_dir.is_dir():
        return None
    ready = [
        p.parent for p in bot_dir.glob("*/recording.json") if _rec_dir_ready(p.parent)
    ]
    if not ready:
        return None
    # Bot ma w praktyce jedno nagranie; gdy jest ich kilka, bierzemy najnowsze.
    return max(ready, key=lambda d: (_read_rec_meta(d) or {}).get("started_at") or "")


def local_asset_state(
    bot_id: str, recording_id: Optional[str]
) -> tuple[str, Optional[str], Optional[str]]:
    """Czy assety wystarczają do pipeline'u? Zwraca (state, dir, recording_id).

    Zwrócony `recording_id` bywa inny niż ten z bazy — gdy baza go nie zna albo
    wskazuje na niekompletny katalog, schodzimy na dysk (`_discover_recording_dir`)
    i mówimy wołającemu, co realnie znaleźliśmy.
    """
    if recording_id:
        rec_dir = settings.recall_dir / bot_id / recording_id
        if _rec_dir_ready(rec_dir):
            return "ready", str(rec_dir), recording_id
        found = _discover_recording_dir(bot_id)
        if found is not None:
            return "ready", str(found), found.name
        # Ścieżkę zwracamy nawet przy niekompletnym katalogu — jest w komunikacie
        # błędu fetcha i mówi, gdzie szukać.
        if (rec_dir / "recording.json").exists():
            return "none", str(rec_dir), recording_id
        return "none", None, recording_id

    found = _discover_recording_dir(bot_id)
    if found is not None:
        return "ready", str(found), found.name
    return "none", None, None


def adopt_disk_recording(meeting: Meeting, rec_dir: Path, recording_id: str) -> None:
    """Przypisz spotkaniu nagranie znalezione na dysku.

    Metadane bierzemy z `recording.json` zapisanego przy pobieraniu, bo API po
    TTL już ich nie poda. Pola uzupełniamy tylko tam, gdzie ich nie ma — dysk
    dokłada braki, nie prostuje świeższych danych z Recalla.
    """
    meeting.recording_id = recording_id
    meta = _read_rec_meta(rec_dir) or {}
    meeting.started_at = meeting.started_at or parse_ts(meta.get("started_at"))
    meeting.completed_at = meeting.completed_at or parse_ts(meta.get("completed_at"))
    meeting.media_expires_at = meeting.media_expires_at or parse_ts(
        meta.get("expires_at")
    )
    if not meeting.duration_seconds and meeting.started_at and meeting.completed_at:
        meeting.duration_seconds = (
            meeting.completed_at - meeting.started_at
        ).total_seconds()
    # Katalog przeszedł `_rec_dir_ready`, więc oba assety fizycznie są.
    meeting.has_audio_mixed = True
    meeting.has_audio_separate = True


def _replace_participants(
    session: Session,
    meeting: Meeting,
    source: str,
    rows: Iterable[dict[str, Any]],
) -> int:
    """Zamień uczestników danego źródła (recall/calendar) na nowy zestaw.

    Klucz tożsamości bywa najpierw adresem (bo nazwa jeszcze nie dotarła),
    a dopiero potem nazwą. Gdyby liczyć go wyłącznie przy pierwszym zapisie,
    ta sama osoba zostawałaby w bazie pod dwoma kluczami i pojawiała się
    dwukrotnie na liście filtrów. Dlatego szukamy istniejącego wiersza także
    po adresie i po nazwie, a znaleziony wiersz przenosimy na aktualny klucz.
    """

    def norm(value: Optional[str]) -> str:
        return " ".join((value or "").strip().lower().split())

    existing = [p for p in meeting.participants if p.source == source]
    by_key = {p.key: p for p in existing}
    by_email = {norm(p.email): p for p in existing if p.email}
    by_name = {norm(p.name): p for p in existing if p.name}

    seen: list[MeetingParticipant] = []
    count = 0
    for row in rows:
        name, email = row.get("name"), row.get("email")
        key = _norm_key(name, email)
        if not key:
            continue

        p = by_key.get(key)
        if p is None and email:
            p = by_email.get(norm(email))
        if p is None and name:
            p = by_name.get(norm(name))
        if p is not None and any(p is s for s in seen):
            continue  # dwa wpisy w źródle wskazują tę samą osobę

        if p is None:
            p = MeetingParticipant(meeting_id=meeting.id, source=source, key=key)
            meeting.participants.append(p)
        else:
            p.key = key  # migracja na lepszy klucz, gdy doszła nazwa

        # Nazwa to etykieta, nie tożsamość — trzymamy najlepszą, jaką znamy.
        # Kalendarz często nie podaje `displayName`, a Recall podaje zawsze;
        # nadpisanie pustym zgubiłoby nazwisko widoczne w innych spotkaniach.
        if name or not p.name:
            p.name = name
        # Nie kasujemy adresu dopasowanego przez nas, gdy źródło go nie podaje.
        # `manual` bije wszystko: to decyzja człowieka o tym, że dwie skrzynki
        # tej samej osoby mają być jedną tożsamością — źródło jej nie zna
        # i przy każdym syncu rozjeżdżałoby ją z powrotem.
        if p.email_source == "manual":
            pass
        elif email:
            p.email = email
            p.email_source = source
            p.match_score = None
        elif p.email_source in ("matched", "propagated", "global"):
            pass  # zostaw nasze dopasowanie
        else:
            p.email = None
            p.email_source = None
        p.is_host = bool(row.get("is_host"))
        p.is_bot = bool(row.get("is_bot", looks_like_bot(name, p.email)))
        p.response_status = row.get("response_status")

        # Indeksy muszą widzieć również wiersze dodane w tym przebiegu,
        # inaczej ten sam człowiek podany dwa razy tworzy dwa wpisy.
        by_key[key] = p
        if email:
            by_email[norm(email)] = p
        if name:
            by_name[norm(name)] = p
        seen.append(p)
        count += 1

    # Usuwamy przez kolekcję, nie `session.delete` — `delete-orphan` skasuje
    # wiersz, a stan w pamięci od razu zgadza się z bazą.
    for p in existing:
        if not any(p is s for s in seen):
            meeting.participants.remove(p)
    return count


def rekey_participants(meeting: Meeting) -> int:
    """Przelicz klucze tożsamości po tym, jak doszły adresy."""
    changed = 0
    for p in meeting.participants:
        key = _norm_key(p.name, p.email)
        if key and p.key != key:
            p.key = key
            changed += 1
    return changed


def resolve_identities(
    session: Session, meeting: Optional[Meeting] = None
) -> dict[str, int]:
    """Dopasuj nazwy z Recall do adresów z zaproszeń i przeklucz uczestników.

    Bez argumentu leci po wszystkich spotkaniach — używane jako backfill
    i po każdej synchronizacji kalendarza.
    """
    from webapp.identity import (
        match_globally,
        propagate_known_emails,
        resolve_meeting,
    )

    meetings = (
        [meeting]
        if meeting is not None
        else list(session.execute(select(Meeting)).scalars())
    )
    total = {"meetings": 0, "matched": 0, "left": 0, "rekeyed": 0}
    for m in meetings:
        stat = resolve_meeting(m)
        total["meetings"] += 1
        total["matched"] += stat["matched"]
        total["left"] += stat["left"]
        total["rekeyed"] += rekey_participants(m)
        session.flush()
        rebuild_search_blob(m)
    # Druga runda: to, co poznaliśmy w jednym spotkaniu, uzupełnia braki
    # w pozostałych. Musi lecieć po dopasowaniu, bo z niego się uczy.
    all_meetings = (
        meetings
        if meeting is None
        else list(session.execute(select(Meeting)).scalars())
    )
    total["propagated"] = propagate_known_emails(all_meetings)
    # Trzecia runda: nazwy, których nie było w żadnym zaproszeniu tego
    # spotkania, dopasowujemy do adresów znanych z pozostałych spotkań.
    total["global"] = match_globally(all_meetings)
    for m in all_meetings:
        total["rekeyed"] += rekey_participants(m)
    session.flush()
    total["named"] = backfill_names_by_email(session)
    for m in all_meetings:
        rebuild_search_blob(m)
    session.commit()
    return total


def backfill_names_by_email(session: Session) -> int:
    """Rozprowadź znane nazwy na wszystkie wiersze z tym samym adresem.

    Skoro adres jest tożsamością, to nazwisko poznane w jednym spotkaniu
    obowiązuje wszędzie. Bez tego zaproszony, którego kalendarz podał bez
    `displayName`, wyświetlałby się jako goły adres, mimo że w nagraniu obok
    występuje pod imieniem i nazwiskiem.
    """
    rows = list(
        session.execute(
            select(MeetingParticipant).where(MeetingParticipant.email.is_not(None))
        ).scalars()
    )

    # Najkrótsza nazwa wygrywa: „Jan Kowalski" nad „Jan Kowalski (Acme)".
    best: dict[str, str] = {}
    for p in rows:
        if not p.name:
            continue
        mail = p.email.strip().lower()
        if mail not in best or len(p.name) < len(best[mail]):
            best[mail] = p.name

    filled = 0
    for p in rows:
        if p.name:
            continue
        name = best.get(p.email.strip().lower())
        if name:
            p.name = name
            filled += 1
    session.flush()
    return filled


def repair_participant_keys(session: Session) -> dict[str, int]:
    """Przelicz klucze tożsamości na istniejących wierszach i scal duplikaty.

    Naprawia dane sprzed poprawki w `_replace_participants`: osoba zapisana
    najpierw pod adresem, a dopiero potem uzupełniona o nazwę, siedzi w bazie
    pod starym kluczem i dubluje się na liście filtrów. Operacja jest
    deterministyczna i bezstratna — klucz liczymy tą samą funkcją co przy
    zapisie, a scalane wiersze dotyczą tego samego spotkania i źródła.
    """
    rows = list(session.execute(select(MeetingParticipant)).scalars())
    canonical: dict[tuple[str, str, str], MeetingParticipant] = {}
    rekeyed = merged = 0

    for p in sorted(rows, key=lambda r: (r.name is None, r.id)):
        key = _norm_key(p.name, p.email)
        if not key:
            continue
        ident = (p.meeting_id, p.source, key)
        winner = canonical.get(ident)
        if winner is None:
            canonical[ident] = p
            if p.key != key:
                p.key = key
                rekeyed += 1
            continue
        # Duplikat: zachowaj bogatszy wiersz, przenieś brakujące pola.
        winner.name = winner.name or p.name
        winner.email = winner.email or p.email
        winner.is_host = winner.is_host or p.is_host
        winner.speaking_seconds = winner.speaking_seconds or p.speaking_seconds
        winner.response_status = winner.response_status or p.response_status
        session.delete(p)
        merged += 1

    session.flush()
    for meeting in session.execute(select(Meeting)).scalars():
        rebuild_search_blob(meeting)
    session.commit()
    return {"rekeyed": rekeyed, "merged": merged, "scanned": len(rows)}


def rebuild_search_blob(meeting: Meeting) -> None:
    """Denormalizacja pod LIKE-search (tytuł + osoby + platforma + id)."""
    bits = [
        meeting.title or "",
        meeting.platform or "",
        meeting.meeting_native_id or "",
        meeting.id,
        meeting.status_code or "",
    ]
    bits += [p.display for p in meeting.participants]
    bits += [p.email or "" for p in meeting.participants]
    meeting.search_blob = " | ".join(b.lower() for b in bits if b)


def upsert_bot(session: Session, bot: dict, fetch_participants: bool = True) -> Meeting:
    bot_id = bot["id"]
    meeting = session.get(Meeting, bot_id)
    if meeting is None:
        meeting = Meeting(id=bot_id)
        session.add(meeting)
        session.flush()

    url = bot.get("meeting_url") or {}
    if isinstance(url, str):
        meeting.meeting_url = url
    else:
        meeting.platform = url.get("platform")
        meeting.meeting_native_id = url.get("meeting_id")
        meeting.meeting_url = _guess_url(url)

    meeting.join_at = parse_ts(bot.get("join_at"))
    code, sub, changed_at = _last_status(bot)
    meeting.status_code = code
    meeting.status_sub_code = sub
    meeting.status_updated_at = changed_at
    meeting.status_group = status_group(code)
    meeting.raw_bot = bot
    meeting.synced_at = utcnow()

    rec = _pick_recording(bot)
    if rec:
        meeting.recording_id = rec.get("id")
        meeting.started_at = parse_ts(rec.get("started_at"))
        meeting.completed_at = parse_ts(rec.get("completed_at"))
        meeting.media_expires_at = parse_ts(rec.get("expires_at"))
        if meeting.started_at and meeting.completed_at:
            meeting.duration_seconds = (
                meeting.completed_at - meeting.started_at
            ).total_seconds()
        shortcuts = rec.get("media_shortcuts") or {}
        meeting.has_audio_mixed = bool((shortcuts.get("audio_mixed") or {}).get("id"))
        # audio_separate nie ma shortcutu — obecność sprawdzamy dopiero przy
        # fetchu; tu zakładamy, że jest, jeśli nagranie w ogóle powstało.
        meeting.has_audio_separate = bool(rec.get("id"))

        if fetch_participants and not any(
            p.source == "recall" for p in meeting.participants
        ):
            people = _fetch_recall_participants(rec)
            if people:
                _replace_participants(session, meeting, "recall", people)
    else:
        meeting.started_at = meeting.started_at or None

    if not meeting.title:
        meeting.title = _fallback_title(meeting)
        meeting.title_source = "fallback"

    # Stan assetów: dysk wygrywa, potem TTL.
    state, path, found_rec = local_asset_state(bot_id, meeting.recording_id)
    if state == "ready":
        if found_rec and found_rec != meeting.recording_id:
            adopt_disk_recording(meeting, Path(path), found_rec)
        meeting.asset_state = "ready"
        meeting.asset_dir = path
    elif meeting.asset_state in ("none", "expired", "failed"):
        expired = (
            bool(meeting.media_expires_at and meeting.media_expires_at < utcnow())
            or meeting.status_code == "media_expired"
        )
        meeting.asset_state = "expired" if expired else "none"

    session.flush()
    rebuild_search_blob(meeting)
    return meeting


def _guess_url(url_obj: dict) -> Optional[str]:
    platform = (url_obj.get("platform") or "").lower()
    mid = url_obj.get("meeting_id")
    if not mid:
        return None
    if platform == "google_meet":
        return f"https://meet.google.com/{mid}"
    if platform == "zoom":
        return f"https://zoom.us/j/{mid}"
    if platform in ("microsoft_teams", "teams"):
        return None  # Teams ma nieodtwarzalne URLe z tokenem
    return None


def _fallback_title(meeting: Meeting) -> str:
    when = meeting.occurred_at
    stamp = when.strftime("%Y-%m-%d %H:%M") if when else "bez daty"
    plat = {
        "google_meet": "Google Meet",
        "zoom": "Zoom",
        "microsoft_teams": "Teams",
    }.get(meeting.platform or "", meeting.platform or "Spotkanie")
    return f"{plat} — {stamp}"


def _fetch_recall_participants(rec: dict) -> list[dict[str, Any]]:
    """participants.json spod presigned URLa z media_shortcuts."""
    data = ((rec.get("media_shortcuts") or {}).get("participant_events") or {}).get(
        "data"
    ) or {}
    url = data.get("participants_download_url")
    if not url:
        return []
    try:
        payload = _fetch_json(url)
    except Exception as e:  # noqa: BLE001 — presigned URL mógł wygasnąć
        log.warning("participants.json niedostępny: %s", e)
        return []
    out = []
    for p in payload or []:
        name = p.get("name")
        out.append(
            {
                "name": name,
                "email": p.get("email"),
                "is_host": bool(p.get("is_host")),
                "is_bot": looks_like_bot(name, p.get("email")),
            }
        )
    return out


def load_participants_from_disk(session: Session, meeting: Meeting) -> int:
    """Po pobraniu assetów mamy participants.json lokalnie — użyj go."""
    if not meeting.asset_dir:
        return 0
    path = Path(meeting.asset_dir) / "participants.json"
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return 0
    rows = [
        {
            "name": p.get("name"),
            "email": p.get("email"),
            "is_host": bool(p.get("is_host")),
            "is_bot": looks_like_bot(p.get("name"), p.get("email")),
        }
        for p in payload or []
    ]
    n = _replace_participants(session, meeting, "recall", rows)
    session.flush()
    rebuild_search_blob(meeting)
    return n


def sync_bots(
    session: Session,
    *,
    lookback_days: Optional[int] = None,
    bot_ids: Optional[list[str]] = None,
    on_progress=None,
) -> dict[str, Any]:
    """Zaciągnij boty z Recall i zapisz jako spotkania.

    `lookback_days` filtruje po `join_at` (Recall wspiera join_at_after).
    """
    client = make_client()
    created = updated = 0

    if bot_ids:
        bots: Iterable[dict] = (client.get_bot(b) for b in bot_ids)
        total_hint = len(bot_ids)
    else:
        days = (
            lookback_days if lookback_days is not None else settings.sync_lookback_days
        )
        after = (utcnow() - dt.timedelta(days=days)).replace(microsecond=0)
        bots = client.list_bots(join_at_after=after.isoformat().replace("+00:00", "Z"))
        total_hint = 0

    seen = 0
    for bot in bots:
        seen += 1
        existed = session.get(Meeting, bot["id"]) is not None
        # Lista botów zwraca payload bez pełnych `recordings.media_shortcuts`
        # tylko przy niektórych filtrach — dociągamy szczegóły gdy brakuje.
        if not bot.get("recordings") and (bot.get("status_changes") or []):
            last = (bot.get("status_changes") or [])[-1].get("code")
            if last in ("done", "analysis_done", "call_ended"):
                try:
                    bot = client.get_bot(bot["id"])
                except Exception as e:  # noqa: BLE001
                    log.warning("get_bot(%s) nieudany: %s", bot["id"], e)
        upsert_bot(session, bot)
        updated += int(existed)
        created += int(not existed)
        if seen % 20 == 0:
            session.commit()
            if on_progress:
                on_progress(seen, total_hint)
    session.commit()
    if on_progress:
        on_progress(seen, total_hint or seen)
    return {"seen": seen, "created": created, "updated": updated}


def adopt_local_recordings(session: Session) -> dict[str, int]:
    """Backfill: podłącz spotkania do nagrań, które leżą na dysku.

    Sam sync naprawia tylko boty, które akurat wrócą z API w oknie lookbacku.
    Reszta — w tym wszystko, czemu Recall wyzerował `recordings` po TTL —
    zostaje w bazie jako `expired`, mimo że komplet audio mamy lokalnie.
    Ta funkcja przechodzi po całej tabeli i nie rusza API.
    """
    stat = {"scanned": 0, "adopted": 0, "relinked": 0, "participants": 0}
    for meeting in session.execute(select(Meeting)).scalars():
        stat["scanned"] += 1
        if (
            meeting.asset_state == "ready"
            and meeting.recording_id
            and meeting.asset_dir
        ):
            continue
        state, path, found = local_asset_state(meeting.id, meeting.recording_id)
        if state != "ready" or not found or not path:
            continue
        if found != meeting.recording_id:
            adopt_disk_recording(meeting, Path(path), found)
            stat["adopted"] += 1
        else:
            stat["relinked"] += 1
        meeting.asset_state = "ready"
        meeting.asset_dir = path
        meeting.asset_error = None
        # participants.json leży obok audio i zwykle jest bogatszy niż to,
        # co API zdążyło podać przed wygaśnięciem mediów.
        stat["participants"] += load_participants_from_disk(session, meeting)
    session.commit()

    # Recall podaje nazwę, adres rzadko — bez dopasowania ta sama osoba
    # zostaje pod dwoma kluczami (nazwa z nagrania, e-mail z zaproszenia)
    # i dubluje się na liście uczestników oraz w filtrach.
    if stat["participants"]:
        ident = resolve_identities(session)
        stat["matched"] = ident["matched"]
        stat["rekeyed"] = ident["rekeyed"]
    return stat


def meetings_ready_to_process(session: Session, limit: int = 50) -> list[Meeting]:
    """Spotkania z nagraniem, bez transkryptu, z nieprzeterminowanym media TTL."""
    q = (
        select(Meeting)
        .where(
            Meeting.recording_id.is_not(None),
            Meeting.transcript_state.in_(("none", "failed")),
            # `expired` to status bota w Recallu, nie werdykt o naszym dysku —
            # audio ściągnięte przed TTL da się przetranskrybować do końca świata.
            Meeting.status_group.in_(("done", "expired")),
        )
        .order_by(Meeting.started_at.desc().nulls_last())
        .limit(limit)
    )
    out = []
    for m in session.execute(q).scalars():
        if m.asset_state == "ready":
            out.append(m)
        elif (
            m.status_group == "done"
            and m.asset_state == "none"
            and (m.media_expires_at is None or m.media_expires_at > utcnow())
        ):
            out.append(m)
    return out
