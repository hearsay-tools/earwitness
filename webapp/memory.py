"""Pamięć spotkań w Honcho (opcjonalny sidecar) — issue #29.

Gotowe transkrypty trafiają do self-hostowanej instancji Honcho, a użytkownik
zadaje pytania w języku naturalnym — o jedno spotkanie albo o wszystkie,
w których był. Bez Honcho appka działa dokładnie tak jak wcześniej:
`HONCHO_ENABLED` domyślnie jest wyłączone.

Mapowanie danych:

| Earwitness                  | Honcho                                   |
|-----------------------------|------------------------------------------|
| instancja                   | workspace (`HONCHO_WORKSPACE`)           |
| uczestnik                   | peer, id z adresu e-mail (fallback nazwa)|
| spotkanie                   | session, id = `Meeting.id` (bot_id)      |
| wypowiedź                   | message od peera mówcy + metadane        |

**Ask-as-self.** Pytania idą z perspektywy peera zalogowanego użytkownika
(`peer.chat`), więc to Honcho ogranicza odpowiedzi do sesji, których ten
peer jest członkiem. Dlatego tożsamość musi się zgadzać po obu stronach:
peer mówcy i peer użytkownika są liczone z tego samego znormalizowanego
adresu. Do sesji dodajemy **wszystkich** ludzi ze spotkania (Recall +
kalendarz), także tych, którzy nic nie powiedzieli — dostęp do pamięci
spotkania daje obecność, nie mówienie.

Identyfikatory w Honcho muszą pasować do `^[a-zA-Z0-9_-]+$`, a adres e-mail
nie pasuje — stąd `peer_id()` robi slug plus krótki hash oryginału, żeby dwa
różne adresy o tym samym slugu nie zlały się w jednego peera.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import time
from functools import lru_cache
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from webapp.config import settings
from webapp.models import (
    ACTIVE_JOB_STATES,
    JOB_RUNNING,
    Job,
    Meeting,
    Transcript,
    User,
    utcnow,
)

log = logging.getLogger("webapp.memory")

# Reguła Honcho dla id workspace'u / peera / sesji.
RESOURCE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
_BAD_CHARS = re.compile(r"[^a-zA-Z0-9_-]+")
# DELETE /sessions w Honcho to soft delete: wiersz zostaje jako
# `is_active=False`, aż asynchroniczny pracownik serwera (deriver) usunie go
# fizycznie. Dopóki istnieje, get-or-create (`h.session(...)`) odpowiada 404
# zamiast utworzyć sesję — create tuż po delete trzeba więc powtarzać.
_CREATE_ATTEMPTS = 10
_CREATE_BACKOFF_S = 0.25  # wykładniczo, z górką 2 s

# Limit serwera na jeden POST /messages.
MESSAGE_BATCH = 100
# Domyślny `MAX_MESSAGE_SIZE` serwera — dłuższą wypowiedź przycinamy, nie tracimy joba.
MAX_MESSAGE_CHARS = 25_000

ProgressFn = Callable[..., None]
LogFn = Callable[[str], None]


class MemoryError(RuntimeError):
    """Błąd po stronie Honcho w formie, którą można pokazać człowiekowi."""


# --------------------------------------------------------------------------
# Tożsamość
# --------------------------------------------------------------------------


def identity_key(name: Optional[str], email: Optional[str]) -> str:
    """Ten sam klucz co `MeetingParticipant.key`: adres, a gdy go brak — nazwa."""
    from webapp.recall_sync import _norm_key

    return _norm_key(name, email)


def peer_id(key: str) -> str:
    """Id peera z klucza tożsamości. Deterministyczne, zgodne z regułą Honcho.

    `jan.kowalski@acme.com` → `jan-kowalski_at_acme-com-3f9a1c`. Hash dokleja
    się zawsze, żeby `a.b@x` i `a-b@x` nie wylądowały w jednym peerze.
    """
    raw = (key or "").strip().lower()
    if not raw:
        raise ValueError("empty identity key")
    slug = _BAD_CHARS.sub("-", raw.replace("@", "_at_")).strip("-") or "peer"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
    return f"{slug[:200]}-{digest}"


def user_peer_id(user: User) -> str:
    return peer_id(identity_key(user.name, user.email))


def user_can_ask(meeting: Meeting, user: User) -> bool:
    """Czy zalogowany był na spotkaniu — wtedy jego peer jest w sesji Honcho."""
    mine = (user.email or "").strip().lower()
    if not mine:
        return False
    return any(
        (p.email or "").strip().lower() == mine for p in meeting.human_participants
    )


# --------------------------------------------------------------------------
# Klient
# --------------------------------------------------------------------------


@lru_cache(maxsize=1)
def client():  # noqa: ANN201 — typ z SDK, import leniwy
    """Jeden klient na proces. Import SDK dopiero tu, żeby appka bez Honcho
    nie płaciła za niego przy starcie."""
    from honcho import Honcho

    if not RESOURCE_ID_RE.match(settings.honcho_workspace or ""):
        raise MemoryError(
            f"HONCHO_WORKSPACE={settings.honcho_workspace!r} — allowed characters: "
            "letters, digits, '_' and '-'"
        )
    return Honcho(
        base_url=settings.honcho_url,
        workspace_id=settings.honcho_workspace,
        api_key=settings.honcho_api_key or None,
        timeout=settings.honcho_timeout,
    )


def health(timeout: float = 3.0) -> bool:
    """Czy `HONCHO_URL` odpowiada na `/health`. Do ostrzeżenia przy starcie."""
    try:
        r = httpx.get(f"{settings.honcho_url.rstrip('/')}/health", timeout=timeout)
        return r.status_code < 500
    except httpx.HTTPError:
        return False


def startup_warnings() -> list[str]:
    if not settings.honcho_enabled:
        return []
    warn = []
    if not health():
        warn.append(
            f"HONCHO_ENABLED=1, but {settings.honcho_url} does not answer /health — "
            "memory ingest and questions will fail until Honcho is up "
            "(docker compose --profile honcho up -d)."
        )
    return warn


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------


def _speaker_resolver(
    meeting: Meeting,
) -> Callable[[str], tuple[str, Optional[str], str]]:
    """Mówca z transkryptu (nazwa z Recall) → (klucz tożsamości, e-mail, nazwa).

    Transkrypt zna tylko nazwę wyświetlaną. Jeśli ta nazwa należy do
    uczestnika z dopasowanym adresem, mówca dostaje peera z adresu — tego
    samego, którym zaloguje się później do „Ask".
    """
    by_name: dict[str, Any] = {}
    for p in meeting.human_participants:
        if p.name:
            by_name.setdefault(identity_key(p.name, None), p)

    def resolve(speaker: str) -> tuple[str, Optional[str], str]:
        p = by_name.get(identity_key(speaker, None))
        if p is not None:
            return identity_key(p.name, p.email), p.email, p.display
        return identity_key(speaker, None), None, speaker

    return resolve


def _session_metadata(meeting: Meeting, transcript: Transcript) -> dict[str, Any]:
    return {
        "meeting_id": meeting.id,
        "title": meeting.title,
        "platform": meeting.platform,
        "started_at": meeting.started_at.isoformat() if meeting.started_at else None,
        "duration_seconds": meeting.duration_seconds,
        "transcript_id": transcript.id,
        "source": "earwitness",
    }


def _roster_ids(meeting: Meeting, utterances: list[dict[str, Any]]) -> set[str]:
    """Kto ma być w sesji: wszyscy ludzie ze spotkania + mówcy spoza listy."""
    ids = {peer_id(identity_key(p.name, p.email)) for p in meeting.human_participants}
    resolve = _speaker_resolver(meeting)
    ids |= {peer_id(resolve(u["speaker"])[0]) for u in utterances}
    return ids


def _roster(
    h: Any, meeting: Meeting, utterances: list[dict[str, Any]]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Peerzy sesji (z metadanymi, założeni w Honcho) + mapa mówca → peer.

    Wszyscy ludzie ze spotkania (obecni i zaproszeni), do tego mówcy, których
    nie ma na liście uczestników (np. nazwa spoza Recall).
    """
    peers: dict[str, Any] = {}

    def ensure_peer(key: str, email: Optional[str], name: str) -> Any:
        pid = peer_id(key)
        if pid not in peers:
            meta = {"name": name, "email": email or None, "source": "earwitness"}
            peers[pid] = h.peer(pid, metadata=meta)
        return peers[pid]

    for p in meeting.human_participants:
        ensure_peer(identity_key(p.name, p.email), p.email, p.display)
    resolve = _speaker_resolver(meeting)
    speaker_peer: dict[str, Any] = {}
    for u in utterances:
        if u["speaker"] not in speaker_peer:
            key, email, name = resolve(u["speaker"])
            speaker_peer[u["speaker"]] = ensure_peer(key, email, name)
    return peers, speaker_peer


def _peer_config() -> Any:
    from honcho.api_types import SessionPeerConfig

    return SessionPeerConfig(
        observe_me=True, observe_others=settings.honcho_observe_others
    )


def sync_peers(db: Session, meeting: Meeting) -> dict[str, Any]:
    """Doprowadź członków sesji Honcho do aktualnej listy uczestników.

    Lista peerów z ingestu to migawka. Później sync dokłada zaproszonych
    z kalendarza, `resolve_identities` dopina adresy do nazw, ktoś wypada
    z listy — a członkostwo w sesji decyduje, kto może pytać i o co. Bez
    tego użytkownik, który dopiero co dostał adres, przechodzi
    `user_can_ask()`, ale jego peer nie jest w sesji; usunięty uczestnik
    zachowuje dostęp przez `/ask`. Tanio: jedno `peers()`, `set_peers` tylko
    przy różnicy. Bez sesji w Honcho (nic nie wgrane) — nic do roboty:
    sprawdzenie przez listowanie, bo leniwe `h.session()` jest
    get-or-create i zamiast 404 ożywiłoby pustą sesję.
    """
    from webapp.tasks import parse_transcript, transcript_text

    transcript = meeting.latest_transcript
    if transcript is None or transcript.honcho_synced_at is None:
        return {"skipped": "not ingested"}
    h = client()
    if not h.sessions(filters={"id": meeting.id}):
        return {"skipped": "no session"}
    sess = h.session(meeting.id)
    current = {p.id for p in sess.peers()}
    utterances = parse_transcript(transcript_text(transcript))
    wanted = _roster_ids(meeting, utterances)
    if wanted == current:
        return {"changed": False, "peers": len(current)}
    peers, _ = _roster(h, meeting, utterances)
    cfg = _peer_config()
    sess.set_peers([(peer, cfg) for peer in peers.values()])
    return {
        "changed": True,
        "peers": len(peers),
        "added": sorted(wanted - current),
        "removed": sorted(current - wanted),
    }


def ingest_transcript(
    db: Session,
    transcript: Transcript,
    *,
    log_line: LogFn = log.info,
    progress: Optional[ProgressFn] = None,
) -> dict[str, Any]:
    """Wgraj transkrypt jako sesję Honcho. Idempotentne: sesja o id spotkania
    jest kasowana i tworzona od nowa, więc ponowna transkrypcja nadpisuje
    pamięć zamiast dokładać drugi komplet wypowiedzi.

    Jeden `commit` w środku, celowo: znaczniki `honcho_synced_at` znikają
    i idą do bazy ZANIM skasujemy starą sesję. Gdyby Honcho padło między
    delete a create, rollback joba przywróciłby „w pamięci" nad sesją, której
    już nie ma — a backfill by ją pominął. Znacznik sukcesu zapisuje task.

    Dwa podchwytliwe semantyki serwera (issue #35, #41):
    - leniwe `h.session(id)` to get-or-create — dla spotkania bez sesji
      stworzyłoby pustą sesję tylko po to, żeby ją skasować; dlatego delete
      poprzedza sprawdzenie, że aktywna sesja w ogóle istnieje (listowanie
      nie tworzy i widzi tylko aktywne wiersze);
    - delete to soft delete: przez chwilę get-or-create zamiast utworzyć nową
      sesję odpiera 404, więc create jest powtarzane z backoffem, aż deriver
      usunie stary wiersz. Gdy okno in-process nie wystarcza (wolny deriver
      albo retry joba, gdy listowanie już nic nie zwraca), 404 to
      `RetryLater`, nie twardy błąd — job czeka bez zużycia próby.
      Wypowiedzi idą dopiero po udanym create.
    """
    from honcho import NotFoundError

    from webapp.tasks import parse_transcript, transcript_text

    if not RESOURCE_ID_RE.match(transcript.meeting_id):
        raise MemoryError(
            f"meeting id {transcript.meeting_id!r} is not a valid Honcho session id"
        )

    meeting = transcript.meeting
    utterances = parse_transcript(transcript_text(transcript))
    h = client()

    def step(pct: int, msg: str) -> None:
        if progress:
            progress(pct, msg)
        log_line(msg)

    peers, speaker_peer = _roster(h, meeting, utterances)
    step(20, f"peers: {len(peers)} ({len(speaker_peer)} speaking)")

    # Sesja od zera. Najpierw znaczniki — patrz docstring.
    for t in meeting.transcripts:
        t.honcho_synced_at = None
    db.commit()
    # Kasujemy tylko aktywną sesję: leniwe `h.session()` jest get-or-create,
    # a soft-deleted wiersz blokuje create (404), dopóki deriver go nie usunie.
    if h.sessions(filters={"id": meeting.id}):
        try:
            h.session(meeting.id).delete()
            log_line("previous Honcho session removed")
        except NotFoundError:
            pass
    cfg = _peer_config()
    sess = None
    for attempt in range(_CREATE_ATTEMPTS):
        try:
            sess = h.session(
                meeting.id,
                metadata=_session_metadata(meeting, transcript),
                peers=[(peer, cfg) for peer in peers.values()],
            )
            break
        except NotFoundError:
            if attempt + 1 == _CREATE_ATTEMPTS:
                from webapp.jobs import RetryLater

                raise RetryLater(
                    "previous Honcho session still being removed server-side"
                ) from None
            time.sleep(min(_CREATE_BACKOFF_S * 2**attempt, 2.0))
            log_line(
                "previous session still being removed server-side — retrying "
                f"create ({attempt + 1}/{_CREATE_ATTEMPTS - 1})"
            )
    step(30, f"session {meeting.id} created")

    # Wypowiedzi, w paczkach po MESSAGE_BATCH. `created_at` = occurred_at
    # (started_at albo join_at) + offset z transkryptu, żeby Honcho widziało
    # realną oś czasu. Bez obu znaczników Honcho stempluje czas ingestu.
    base = meeting.occurred_at
    if base is None:
        log_line(
            "no meeting start or join time — message timestamps fall back to ingest time"
        )
    batch: list[Any] = []
    sent = 0
    for u in utterances:
        created = base + dt.timedelta(seconds=u["seconds"]) if base else None
        batch.append(
            speaker_peer[u["speaker"]].message(
                u["text"][:MAX_MESSAGE_CHARS],
                metadata={
                    "meeting_id": meeting.id,
                    "title": meeting.title,
                    "timestamp": u["timestamp"],
                    "seconds": u["seconds"],
                    "speaker": u["speaker"],
                },
                created_at=created,
            )
        )
        if len(batch) >= MESSAGE_BATCH:
            sess.add_messages(batch)
            sent += len(batch)
            batch = []
            step(
                30 + int(60 * sent / max(1, len(utterances))),
                f"messages: {sent}/{len(utterances)}",
            )
    if batch:
        sess.add_messages(batch)
        sent += len(batch)
    step(95, f"messages: {sent}/{len(utterances)}")

    now = utcnow()
    for t in meeting.transcripts:
        t.honcho_synced_at = now if t.id == transcript.id else None
    return {
        "session": meeting.id,
        "peers": len(peers),
        "messages": sent,
        "silent_attendees": len(peers) - len(speaker_peer),
    }


def queue_ingest(
    db: Session,
    meeting: Meeting,
    transcript_id: int,
    *,
    priority: int,
    created_by: Optional[str],
) -> Job:
    """Zakolejkuj ingest konkretnego transkryptu.

    Klucz deduplikacji zawiera id transkryptu, nie tylko spotkania: gdyby był
    per spotkanie, ponowna transkrypcja w trakcie trwającego ingestu dostałaby
    z `enqueue()` ten *biegnący* job (argumentów biegnącego nie ruszamy) —
    stary transkrypt wszedłby do Honcho, a nowy nigdy. Dwa joby na jedno
    spotkanie nie biegną naraz: task sprawdza `running_ingest()` i odkłada
    się (`RetryLater`, bez zużycia próby), a przestarzały transkrypt pomija
    (`latest_transcript`). Ingest, który skończył, sam kolejkuje nowszy
    transkrypt, jeśli taki pojawił się w trakcie.
    """
    from webapp.jobs import enqueue

    return enqueue(
        db,
        "honcho_ingest",
        meeting_id=meeting.id,
        args={"transcript_id": transcript_id},
        priority=priority,
        dedupe_key=f"honcho_ingest:{meeting.id}:{transcript_id}",
        created_by=created_by,
    )


def queue_backfill(db: Session, *, created_by: Optional[str]) -> Optional[Job]:
    """Zakolejkuj `honcho_backfill` po zmianie tożsamości uczestników.

    Backfill nie tylko wgrywa brakujące transkrypty — dla wgranych robi
    `sync_peers()`. Wywołują go miejsca, które zmieniają listę uczestników
    (sync, naprawa tożsamości). Nic, gdy Honcho wyłączone.
    """
    if not settings.honcho_enabled:
        return None
    from webapp.jobs import enqueue

    return enqueue(
        db,
        "honcho_backfill",
        priority=100,
        dedupe_key="honcho_backfill:auto",
        created_by=created_by,
    )


def running_ingest(db: Session, meeting_id: str, *, exclude: int) -> Optional[int]:
    """Id innego biegnącego `honcho_ingest` dla spotkania albo None."""
    return db.execute(
        select(Job.id).where(
            Job.kind == "honcho_ingest",
            Job.meeting_id == meeting_id,
            Job.status == JOB_RUNNING,
            Job.id != exclude,
        )
    ).scalar()


def _latest_ready_transcripts(db: Session) -> list[Transcript]:
    """Najnowszy transkrypt każdego gotowego spotkania.

    Liczy się wyłącznie najnowszy: sesja Honcho odzwierciedla jeden transkrypt,
    a po ponownej transkrypcji stary wiersz trzyma znacznik `honcho_synced_at`
    aż do udanego ingestu nowego. Patrzenie na dowolny wiersz ze znacznikiem
    pokazywałoby spotkanie jako „w pamięci", gdy Honcho ma nieaktualne dane.
    """
    meetings = db.execute(
        select(Meeting)
        .where(Meeting.transcript_state == "ready")
        .order_by(
            func.coalesce(Meeting.started_at, Meeting.join_at).asc().nulls_last(),
            Meeting.id,
        )
    ).scalars()
    return [m.latest_transcript for m in meetings if m.latest_transcript is not None]


def transcripts_to_sync(db: Session, *, force: bool = False) -> list[Transcript]:
    """Najnowszy transkrypt każdego gotowego spotkania, którego nie ma w Honcho."""
    return [
        t for t in _latest_ready_transcripts(db) if force or t.honcho_synced_at is None
    ]


def status(db: Session) -> dict[str, int]:
    """Liczby do panelu pamięci: gotowe transkrypty vs wgrane vs w kolejce."""
    ready = db.execute(
        select(func.count(Meeting.id)).where(Meeting.transcript_state == "ready")
    ).scalar_one()
    synced = sum(
        1 for t in _latest_ready_transcripts(db) if t.honcho_synced_at is not None
    )
    pending = db.execute(
        select(func.count(Job.id)).where(
            Job.kind.in_(("honcho_ingest", "honcho_backfill")),
            Job.status.in_(ACTIVE_JOB_STATES),
        )
    ).scalar_one()
    return {"ready": int(ready), "synced": int(synced), "pending": int(pending)}


# --------------------------------------------------------------------------
# Pytania
# --------------------------------------------------------------------------


def ask(user: User, question: str, meeting: Optional[Meeting] = None) -> Optional[str]:
    """Zapytaj pamięć jako peer użytkownika. `meeting` zawęża do jednej sesji.

    Zwraca tekst odpowiedzi albo None, gdy Honcho nie ma z czego odpowiedzieć.
    Błędy transportu i serwera lecą jako `MemoryError` z komunikatem do UI.
    """
    from honcho import HonchoError, NotFoundError

    q = (question or "").strip()
    if not q:
        raise MemoryError("Type a question first.")
    try:
        # `peer()` już gada z serwerem (get-or-create workspace'u), więc też
        # musi być w try — padnięte Honcho ma dać komunikat, nie traceback.
        peer = client().peer(user_peer_id(user))
        answer = peer.chat(
            q,
            session=meeting.id if meeting else None,
            reasoning_level=settings.honcho_reasoning_level,  # type: ignore[arg-type]
        )
    except NotFoundError as e:
        raise MemoryError(
            "Memory has nothing about you yet — no meeting you attended has been "
            "ingested. Check that your e-mail matches the participant list."
        ) from e
    except HonchoError as e:
        raise MemoryError(f"Honcho did not answer: {type(e).__name__}: {e}") from e
    except httpx.HTTPError as e:
        raise MemoryError(f"Cannot reach Honcho at {settings.honcho_url}: {e}") from e
    if answer is None:
        return None
    text = answer if isinstance(answer, str) else str(answer)
    return text.strip() or None
