"""Kolejka zadań oparta o bazę — celery-like, ale bez brokera.

Dlaczego nie Celery: PoC ma działać po `uv run`, a Celery ciągnie Redisa albo
RabbitMQ. Zadania tutaj są długie (minuty) i rzadkie (kilka na spotkanie), więc
narzut pollingu bazy jest bez znaczenia, a zysk operacyjny duży: jeden proces
workera, stan zadań widoczny w tej samej bazie co reszta appki, historia i logi
per job za darmo.

Kontrakt taki jak w Celery: `enqueue()` zwraca natychmiast, worker `claim()`uje
zadanie atomowo (compare-and-swap, więc działa i na SQLite, i na Postgresie),
raportuje `heartbeat()`, a padnięty worker jest wykrywany przez `reap_stale()`
i jego zadania wracają do kolejki. Retry z exponential backoff.
"""

from __future__ import annotations

import datetime as dt
import logging
import traceback
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from webapp.config import settings
from webapp.models import (
    ACTIVE_JOB_STATES,
    JOB_CANCELED,
    JOB_DONE,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    Job,
    Meeting,
    utcnow,
)

log = logging.getLogger("webapp.jobs")

MAX_LOG_CHARS = 40_000

# Zakolejkowanie od razu przestawia stan spotkania na „queued", żeby UI nie
# czekał na workera. To rezerwacja — i ktoś musi ją zdjąć, gdy zadanie umrze.
# Task tego nie dowiezie: `process` potrafi wysypać się w fetchu, zanim
# jakikolwiek `except` w tasku zdąży cokolwiek zapisać, a anulowanie i reaper
# nie wchodzą do taska w ogóle. Dlatego rezerwację zdejmuje kolejka.
TRANSCRIPT_JOB_KINDS = ("process", "transcribe")
ASSET_JOB_KINDS = ("process", "fetch_assets")
_TRANSCRIPT_PENDING = ("queued", "running")
_ASSET_PENDING = ("queued", "fetching")


def release_meeting_state(session: Session, job: Job, *, failed: bool) -> None:
    """Zdejmij rezerwację ze spotkania po zadaniu, które się nie udało.

    Bez tego spotkanie zostaje w stanie „queued" na zawsze: znika z filtra
    „bez transkryptu", pokazuje kłamliwy status w kolejce i — co gorsza —
    `meetings_ready_to_process()` przestaje je widzieć, więc autoprocess nigdy
    go już nie tknie. Porażka wraca na „failed" (jest błąd do pokazania),
    anulowanie na „none" (nic się nie wydarzyło, można kolejkować od nowa).
    """
    if not job.meeting_id:
        return
    meeting = session.get(Meeting, job.meeting_id)
    if meeting is None:
        return
    if (
        job.kind in TRANSCRIPT_JOB_KINDS
        and meeting.transcript_state in _TRANSCRIPT_PENDING
    ):
        meeting.transcript_state = "failed" if failed else "none"
        if failed and not meeting.transcript_error:
            meeting.transcript_error = (job.error or "")[:2000] or None
    # `_do_fetch` sam ustawia `failed` z sensownym opisem, więc tu łapiemy
    # tylko to, co nie zdążyło tam dojść (ubity worker, anulowanie w kolejce).
    if job.kind in ASSET_JOB_KINDS and meeting.asset_state in _ASSET_PENDING:
        meeting.asset_state = "failed" if failed else "none"


# --------------------------------------------------------------------------
# Rejestr tasków
# --------------------------------------------------------------------------

TaskFn = Callable[["JobContext"], dict[str, Any] | None]
_REGISTRY: dict[str, TaskFn] = {}


def task(kind: str) -> Callable[[TaskFn], TaskFn]:
    def deco(fn: TaskFn) -> TaskFn:
        _REGISTRY[kind] = fn
        return fn

    return deco


def get_task(kind: str) -> Optional[TaskFn]:
    return _REGISTRY.get(kind)


def registered_kinds() -> list[str]:
    return sorted(_REGISTRY)


class JobContext:
    """To, co dostaje funkcja taska: argumenty + kanał raportowania postępu.

    Każdy `progress()` robi commit, więc UI widzi ruch na żywo i jednocześnie
    odświeża heartbeat (worker żyje).
    """

    def __init__(self, session: Session, job: Job) -> None:
        self.session = session
        self.job = job
        self.args: dict[str, Any] = job.args or {}
        self.meeting_id = job.meeting_id

    def progress(
        self,
        pct: Optional[int] = None,
        step: Optional[str] = None,
        line: Optional[str] = None,
    ) -> None:
        if pct is not None:
            self.job.progress = max(0, min(100, int(pct)))
        if step is not None:
            self.job.step = step[:200]
        if line is not None:
            self.log(line)
        self.job.heartbeat_at = utcnow()
        self.session.commit()

    def log(self, line: str) -> None:
        stamp = utcnow().strftime("%H:%M:%S")
        prev = self.job.log or ""
        entry = f"[{stamp}] {line.rstrip()}\n"
        combined = prev + entry
        if len(combined) > MAX_LOG_CHARS:
            combined = "…(truncated)…\n" + combined[-MAX_LOG_CHARS:]
        self.job.log = combined


# --------------------------------------------------------------------------
# API kolejki
# --------------------------------------------------------------------------


def enqueue(
    session: Session,
    kind: str,
    *,
    meeting_id: Optional[str] = None,
    args: Optional[dict[str, Any]] = None,
    priority: int = 100,
    dedupe_key: Optional[str] = None,
    max_attempts: int = 3,
    created_by: Optional[str] = None,
    delay_seconds: float = 0.0,
) -> Job:
    """Dodaj zadanie. Idempotentne po `dedupe_key` wśród aktywnych zadań.

    Domyślny dedupe_key to `kind:meeting_id` — dwa kliknięcia "przetwórz" na
    tym samym spotkaniu nie odpalą dwóch pipeline'ów (a to realne pieniądze
    w ElevenLabs).
    """
    if kind not in _REGISTRY:
        raise ValueError(f"Unknown job type: {kind!r}. Available: {registered_kinds()}")

    key = dedupe_key if dedupe_key is not None else f"{kind}:{meeting_id or '-'}"
    existing = session.execute(
        select(Job)
        .where(Job.dedupe_key == key, Job.status.in_(ACTIVE_JOB_STATES))
        .order_by(Job.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        # Ten sam klucz, ale inne argumenty = użytkownik doprecyzował zamiar,
        # zanim worker zdążył zabrać zadanie (np. potwierdził redo z ASR-em na
        # spotkaniu, które już czekało w kolejce zwykłym `process`). Dopóki
        # zadanie stoi w kolejce, dociągamy je do nowszej intencji — inaczej
        # oddajemy stare i redirect udaje, że kliknięcie zadziałało.
        # Running zostawiamy: task odczytał już `args` i i tak ich nie zobaczy.
        if existing.status == JOB_QUEUED:
            # Eskalacja, nie rekonfiguracja: puste wartości pomijamy, żeby
            # zwykłe kliknięcie z listy nie zdjęło `force_asr` z zadania, na
            # które ktoś chwilę wcześniej kliknął „tak, zapłać za ASR”.
            # Cena: nie da się tak *wyłączyć* flagi na czekającym zadaniu —
            # i dobrze, bo UI takiej intencji nie ma, a priorytet niżej działa
            # dokładnie tak samo (tylko w stronę pilniejszego).
            merged = {
                **(existing.args or {}),
                **{k: v for k, v in (args or {}).items() if v},
            }
            if merged != (existing.args or {}):
                existing.args = merged
            if priority < existing.priority:
                existing.priority = priority
            session.commit()
        return existing

    job = Job(
        kind=kind,
        meeting_id=meeting_id,
        args=args or {},
        priority=priority,
        dedupe_key=key,
        max_attempts=max_attempts,
        created_by=created_by,
        scheduled_at=utcnow() + dt.timedelta(seconds=delay_seconds),
    )
    session.add(job)
    session.commit()
    return job


def claim(
    session: Session, worker_id: str, kinds: Optional[Iterable[str]] = None
) -> Optional[Job]:
    """Atomowo zabierz jedno zadanie z kolejki. None gdy pusto.

    Compare-and-swap zamiast `FOR UPDATE SKIP LOCKED`, bo ma działać też na
    SQLite. Przy kilku workerach przegrany po prostu próbuje następnego.
    """
    now = utcnow()
    for _ in range(10):
        q = (
            select(Job.id)
            .where(Job.status == JOB_QUEUED, Job.scheduled_at <= now)
            .order_by(Job.priority.asc(), Job.scheduled_at.asc(), Job.id.asc())
            .limit(5)
        )
        if kinds:
            q = q.where(Job.kind.in_(list(kinds)))
        candidates = list(session.execute(q).scalars())
        if not candidates:
            return None
        for job_id in candidates:
            res = session.execute(
                update(Job)
                .where(Job.id == job_id, Job.status == JOB_QUEUED)
                .values(
                    status=JOB_RUNNING,
                    worker_id=worker_id,
                    started_at=now,
                    heartbeat_at=now,
                    attempts=Job.attempts + 1,
                    progress=0,
                    error=None,
                )
            )
            session.commit()
            if res.rowcount:
                return session.get(Job, job_id)
    return None


def finish(session: Session, job: Job, result: Optional[dict[str, Any]] = None) -> None:
    job.status = JOB_DONE
    job.result = result or {}
    job.progress = 100
    job.finished_at = utcnow()
    job.heartbeat_at = utcnow()
    session.commit()


def fail(session: Session, job: Job, error: str, allow_retry: bool = True) -> None:
    """Oznacz porażkę. Retry z backoffem 30s / 2min / 8min, potem twardy fail."""
    job.error = error[-8000:]
    job.heartbeat_at = utcnow()
    if allow_retry and job.attempts < job.max_attempts:
        backoff = 30 * (4 ** (job.attempts - 1))
        job.status = JOB_QUEUED
        job.scheduled_at = utcnow() + dt.timedelta(seconds=min(backoff, 1800))
        job.step = (
            f"retry {job.attempts}/{job.max_attempts} in {int(min(backoff, 1800))}s"
        )
        job.worker_id = None
    else:
        job.status = JOB_FAILED
        job.finished_at = utcnow()
        # Dopiero teraz — przy retry spotkanie nadal czeka i „queued" jest prawdą.
        release_meeting_state(session, job, failed=True)
    session.commit()


def cancel(session: Session, job: Job) -> bool:
    """Anuluj tylko zadanie czekające w kolejce (running trzeba by ubić w workerze)."""
    if job.status != JOB_QUEUED:
        return False
    job.status = JOB_CANCELED
    job.finished_at = utcnow()
    release_meeting_state(session, job, failed=False)
    session.commit()
    return True


def reap_stale(session: Session) -> int:
    """Zadania `running` bez heartbeatu → z powrotem do kolejki (albo fail)."""
    cutoff = utcnow() - dt.timedelta(seconds=settings.job_stale_after)
    stale = list(
        session.execute(
            select(Job).where(
                Job.status == JOB_RUNNING,
                Job.heartbeat_at.is_not(None),
                Job.heartbeat_at < cutoff,
            )
        ).scalars()
    )
    for job in stale:
        log.warning("job %s (%s) zawieszony — reaping", job.id, job.kind)
        fail(
            session,
            job,
            f"The worker stopped responding (no heartbeat for > {settings.job_stale_after}s)",
        )
    return len(stale)


def release_orphaned_meetings(session: Session) -> int:
    """Spotkania czekające na zadanie, którego już nie ma.

    Rezerwacja bez żywego zadania to sierota — zostaje po zadaniu, które padło
    zanim ktokolwiek ją zdjął, po ubitym workerze albo po odtworzeniu bazy.
    Spotkanie wygląda wtedy na „w kolejce" bez końca i wypada z autoprocessu.
    Leci w pętli reapera, więc naprawia się samo, bez ręcznego zadania.
    """
    active = select(Job.meeting_id).where(
        Job.status.in_(ACTIVE_JOB_STATES), Job.meeting_id.is_not(None)
    )
    freed = 0
    for meeting in session.execute(
        select(Meeting).where(
            Meeting.id.not_in(active),
            or_(
                Meeting.transcript_state.in_(_TRANSCRIPT_PENDING),
                Meeting.asset_state.in_(_ASSET_PENDING),
            ),
        )
    ).scalars():
        last = session.execute(
            select(Job)
            .where(
                Job.meeting_id == meeting.id,
                Job.kind.in_(("process", "transcribe", "fetch_assets")),
            )
            .order_by(Job.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if meeting.transcript_state in _TRANSCRIPT_PENDING:
            meeting.transcript_state = (
                "failed" if last and last.status == JOB_FAILED else "none"
            )
            if meeting.transcript_state == "failed" and not meeting.transcript_error:
                meeting.transcript_error = (last.error or "")[:2000] or None
        if meeting.asset_state in _ASSET_PENDING:
            meeting.asset_state = (
                "failed" if last and last.status == JOB_FAILED else "none"
            )
        freed += 1
    if freed:
        session.commit()
        log.warning("zdjęto %d osieroconych rezerwacji na spotkaniach", freed)
    return freed


def run_job(session: Session, job: Job) -> None:
    """Wykonaj zadanie w bieżącym wątku. Używane przez workera."""
    fn = get_task(job.kind)
    if fn is None:
        # `enqueue()` sprawdza rodzaj przy dodawaniu, więc nieznany rodzaj tutaj
        # znaczy tylko jedno: ten proces workera wystartował przed dołożeniem
        # taska i siedzi na starym module. Kod jest dobry, zepsuty jest proces —
        # dlatego zostawiamy retry, żeby zadanie przeżyło do restartu.
        fail(
            session,
            job,
            f"No handler for {job.kind!r} — this worker is running older code. "
            f"Restart it. Known types: {registered_kinds()}",
        )
        return
    ctx = JobContext(session, job)
    ctx.progress(
        0, "start", f"start: {job.kind} (attempt {job.attempts}/{job.max_attempts})"
    )
    try:
        result = fn(ctx) or {}
    except Exception as e:  # noqa: BLE001 — worker nie może paść przez jeden task
        session.rollback()
        job = session.get(Job, job.id)
        detail = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=8)}"
        log.exception("job %s (%s) failed", job.id, job.kind)
        fail(session, job, detail)
        return
    finish(session, job, result)


# --------------------------------------------------------------------------
# Statystyki do UI
# --------------------------------------------------------------------------


def queue_stats(session: Session) -> dict[str, int]:
    rows = session.execute(
        select(Job.status, Job.id).where(
            Job.status.in_([JOB_QUEUED, JOB_RUNNING, JOB_FAILED])
        )
    ).all()
    out = {JOB_QUEUED: 0, JOB_RUNNING: 0, JOB_FAILED: 0}
    for status, _ in rows:
        out[status] = out.get(status, 0) + 1
    return out
