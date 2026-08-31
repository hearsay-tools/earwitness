"""Worker kolejki — osobny proces obok serwera.

    uv run python -m webapp.worker --concurrency 2

Concurrency realizujemy **procesami**, nie wątkami. Powód jest konkretny:
taski przechwytują `stderr` bibliotek (`recall_client`, `energy_diarization`
raportują postęp printem), a `redirect_stderr` jest globalny dla procesu —
dwa taski w wątkach mieszałyby sobie logi. Do tego pipeline jest CPU-bound
(RMS po 10 ms ramkach), więc GIL i tak by je zserializował.

Proces-rodzic nie bierze zadań, tylko pilnuje harmonogramu: co `AUTOSYNC_INTERVAL`
kolejkuje `sync_recall`, oraz odzyskuje zadania po padniętych workerach.
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing as mp
import os
import signal
import socket
import sys
import time
from typing import Optional

from webapp import tasks  # noqa: F401 — rejestracja tasków w dekoratorach
from webapp.config import settings
from webapp.db import init_db, session_scope
from webapp.jobs import claim, enqueue, reap_stale, release_orphaned_meetings, run_job

log = logging.getLogger("webapp.worker")

_stop = False


def _handle_signal(signum, _frame) -> None:  # noqa: ANN001
    global _stop
    _stop = True
    log.info("sygnał %s — kończę po bieżącym zadaniu", signum)


def worker_loop(slot: int, kinds: Optional[list[str]] = None) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [w{slot}] %(levelname)s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    worker_id = f"{socket.gethostname()}:{os.getpid()}:{slot}"
    log.info("worker start (%s)", worker_id)
    idle_since = time.monotonic()

    while not _stop:
        try:
            with session_scope() as session:
                job = claim(session, worker_id, kinds=kinds)
                if job is None:
                    session.commit()
                else:
                    log.info("job %s: %s (%s)", job.id, job.kind, job.meeting_id or "-")
                    t0 = time.perf_counter()
                    run_job(session, job)
                    log.info(
                        "job %s: %s w %.1fs",
                        job.id,
                        job.status,
                        time.perf_counter() - t0,
                    )
                    idle_since = time.monotonic()
                    continue
        except Exception:  # noqa: BLE001 — pętla workera nie może umrzeć
            log.exception("błąd pętli workera")
            time.sleep(5)
            continue

        # Backoff przy pustej kolejce: 2s → 10s po minucie bezczynności.
        idle = time.monotonic() - idle_since
        time.sleep(settings.worker_poll_interval if idle < 60 else 10.0)

    log.info("worker stop (%s)", worker_id)


def scheduler_loop(children: list[mp.Process]) -> None:
    """Reaper + autosync. Kręci się w procesie-rodzicu."""
    last_sync = 0.0
    while not _stop:
        try:
            with session_scope() as session:
                n = reap_stale(session)
                if n:
                    log.warning("odzyskano %d zawieszonych zadań", n)
                # Po reapie, bo dopiero on domyka zadania ubitego workera.
                release_orphaned_meetings(session)

                if settings.autosync_interval > 0 and settings.recall_api_key:
                    now = time.monotonic()
                    if now - last_sync >= settings.autosync_interval:
                        last_sync = now
                        job = enqueue(
                            session,
                            "sync_recall",
                            args={
                                "lookback_days": settings.sync_lookback_days,
                                "autoprocess": settings.autoprocess,
                            },
                            priority=50,
                            dedupe_key="sync_recall:auto",
                            created_by="scheduler",
                        )
                        log.info("autosync → job %s", job.id)
        except Exception:  # noqa: BLE001
            log.exception("błąd schedulera")

        for _ in range(20):
            if _stop:
                break
            for c in children:
                if not c.is_alive() and c.exitcode not in (0, None):
                    log.error("worker %s padł (exit=%s) — restart", c.name, c.exitcode)
            time.sleep(0.5)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="webapp.worker",
        description="Worker kolejki zadań (fetch z Recall + pipeline transkrypcji).",
    )
    p.add_argument(
        "-c",
        "--concurrency",
        type=int,
        default=settings.worker_concurrency,
        help=f"Ile równoległych procesów (default {settings.worker_concurrency}).",
    )
    p.add_argument(
        "--kinds",
        default=None,
        help="Ogranicz do typów zadań (przecinkami), np. 'process,transcribe'.",
    )
    p.add_argument(
        "--once",
        action="store_true",
        help="Wykonaj jedno zadanie i wyjdź (do debugowania).",
    )
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    init_db()
    kinds = [k.strip() for k in args.kinds.split(",")] if args.kinds else None

    if args.once:
        with session_scope() as session:
            job = claim(session, "once", kinds=kinds)
            if job is None:
                print("kolejka pusta", file=sys.stderr)
                return 0
            run_job(session, job)
            print(f"job {job.id} ({job.kind}) → {job.status}", file=sys.stderr)
            if job.error:
                print(job.error, file=sys.stderr)
            return 0 if job.status == "done" else 1

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    ctx = mp.get_context("spawn")
    children: list[mp.Process] = []
    for slot in range(max(1, args.concurrency)):
        c = ctx.Process(
            target=worker_loop, args=(slot, kinds), name=f"worker-{slot}", daemon=False
        )
        c.start()
        children.append(c)
    log.info(
        "wystartowało %d workerów, autosync co %ss",
        len(children),
        settings.autosync_interval,
    )

    try:
        scheduler_loop(children)
    finally:
        for c in children:
            if c.is_alive():
                c.terminate()
        for c in children:
            c.join(timeout=30)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
