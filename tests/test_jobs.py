"""Kolejka: deduplikacja, atomowy claim, retry z backoffem, reaping."""

from __future__ import annotations

import datetime as dt
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from webapp import jobs as J
from webapp import tasks  # noqa: F401 — rejestruje typy zadań
from webapp.db import SessionLocal
from webapp.models import (
    JOB_CANCELED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    Job,
    Meeting,
    utcnow,
)


def test_enqueue_dedupes_active_jobs(session):
    a = J.enqueue(session, "process", meeting_id="m1")
    b = J.enqueue(session, "process", meeting_id="m1")
    assert a.id == b.id, "drugie kliknięcie nie może odpalić drugiego pipeline'u"

    other = J.enqueue(session, "process", meeting_id="m2")
    assert other.id != a.id


def test_enqueue_escalates_args_of_a_waiting_job(session):
    """Redo potwierdzony nad zadaniem, które jeszcze czeka, ma je doprecyzować.

    Inaczej dedupe oddaje stare zadanie bez `force_asr`, redirect wygląda na
    sukces, a użytkownik dostaje dokładnie ten sam transkrypt co miał.
    """
    a = J.enqueue(
        session, "process", meeting_id="m1", args={"force_asr": False}, priority=100
    )
    b = J.enqueue(
        session, "process", meeting_id="m1", args={"force_asr": True}, priority=20
    )

    assert b.id == a.id, "nadal jedno zadanie, nie dwa pipeline'y"
    assert b.args["force_asr"] is True
    assert b.priority == 20, "pilniejsze kliknięcie ma wyprzedzić kolejkę"


def test_enqueue_never_downgrades_a_confirmed_redo(session):
    """Zwykłe kliknięcie z listy nie może zdjąć `force_asr` z zadania, na które
    ktoś chwilę wcześniej potwierdził płatny redo."""
    a = J.enqueue(
        session, "process", meeting_id="m1", args={"force_asr": True}, priority=20
    )
    b = J.enqueue(
        session, "process", meeting_id="m1", args={"force_asr": False}, priority=100
    )

    assert b.id == a.id
    assert b.args["force_asr"] is True
    assert b.priority == 20, "priorytet też idzie tylko w stronę pilniejszego"


def test_enqueue_leaves_a_running_job_alone(session):
    """Task odczytał już `args` do `ctx` — podmiana pod nim niczego nie zmieni,
    a zafałszowałaby historię zadania."""
    a = J.enqueue(session, "process", meeting_id="m1", args={"force_asr": False})
    J.claim(session, "w1")

    b = J.enqueue(session, "process", meeting_id="m1", args={"force_asr": True})
    assert b.id == a.id
    assert b.args["force_asr"] is False


def test_enqueue_allows_requeue_after_finish(session):
    a = J.enqueue(session, "process", meeting_id="m1")
    J.finish(session, a, {"ok": True})
    b = J.enqueue(session, "process", meeting_id="m1")
    assert b.id != a.id


def test_enqueue_can_defer_commit_for_atomic_batch_creation(session):
    job = J.enqueue(session, "process", meeting_id="m1", commit=False)
    assert job.id is not None

    with SessionLocal() as other:
        assert other.get(Job, job.id) is None

    session.commit()
    with SessionLocal() as other:
        assert other.get(Job, job.id) is not None


def test_enqueue_serializes_concurrent_work_for_the_same_meeting(session):
    session.add(Meeting(id="locked-meeting"))
    session.commit()
    first = J.enqueue(session, "process", meeting_id="locked-meeting", commit=False)

    def enqueue_again() -> int:
        with SessionLocal() as other:
            return J.enqueue(other, "process", meeting_id="locked-meeting").id

    with ThreadPoolExecutor(max_workers=1) as pool:
        second = pool.submit(enqueue_again)
        time.sleep(0.1)
        assert not second.done(), (
            "the meeting reservation must block a concurrent enqueue"
        )
        session.commit()
        assert second.result(timeout=2) == first.id


def test_enqueue_rejects_unknown_kind(session):
    with pytest.raises(ValueError):
        J.enqueue(session, "nie-ma-takiego")


def test_claim_is_exclusive(session):
    J.enqueue(session, "process", meeting_id="m1")
    first = J.claim(session, "w1")
    second = J.claim(session, "w2")
    assert first is not None and second is None
    assert first.status == JOB_RUNNING
    assert first.attempts == 1


def test_claim_respects_priority_and_schedule(session):
    J.enqueue(session, "process", meeting_id="low", priority=100)
    J.enqueue(session, "process", meeting_id="high", priority=10)
    J.enqueue(session, "process", meeting_id="later", priority=1, delay_seconds=600)

    got = J.claim(session, "w1")
    assert got.meeting_id == "high", "priorytet 1 jest zaplanowany na później"


def test_claim_does_not_starve_an_overdue_retry(session):
    """A due retry must still be claimed while higher-priority work keeps arriving.

    Without aging, priority is compared before scheduled_at, so a priority-90
    retry sits forever behind a stream of priority-20 jobs and never increments
    attempts — silent starvation.
    """
    job = J.enqueue(session, "process", meeting_id="low", priority=90, max_attempts=3)
    claimed = J.claim(session, "w1")
    assert claimed.id == job.id
    J.fail(session, job, "boom")
    assert job.status == JOB_QUEUED

    job.scheduled_at = utcnow() - dt.timedelta(hours=1)
    session.commit()

    for i in range(8):
        J.enqueue(session, "process", meeting_id=f"hot-{i}", priority=20)

    got = J.claim(session, "w1")
    assert got is not None
    assert got.id == job.id, "overdue retry must not be skipped indefinitely"


def test_claim_fresh_jobs_still_preempt_a_just_due_retry(session):
    """Aging must not let a retry that just became due jump the queue."""
    job = J.enqueue(session, "process", meeting_id="low", priority=90, max_attempts=3)
    J.claim(session, "w1")
    J.fail(session, job, "boom")
    job.scheduled_at = utcnow()
    session.commit()

    hot = J.enqueue(session, "process", meeting_id="hot", priority=20)
    got = J.claim(session, "w1")
    assert got.id == hot.id


def test_claim_filters_by_kind(session):
    J.enqueue(session, "sync_recall", dedupe_key="s")
    assert J.claim(session, "w1", kinds=["process"]) is None
    assert J.claim(session, "w1", kinds=["sync_recall"]) is not None


def test_claim_runs_batch_strictly_in_position_order(session):
    third = J.enqueue(
        session,
        "process",
        meeting_id="m3",
        batch_id="batch",
        batch_position=3,
        batch_size=3,
    )
    first = J.enqueue(
        session,
        "process",
        meeting_id="m1",
        batch_id="batch",
        batch_position=1,
        batch_size=3,
    )
    second = J.enqueue(
        session,
        "process",
        meeting_id="m2",
        batch_id="batch",
        batch_position=2,
        batch_size=3,
    )

    assert J.claim(session, "w1").id == first.id
    assert J.claim(session, "w2") is None
    J.finish(session, first)
    assert J.claim(session, "w2").id == second.id
    J.finish(session, second)
    assert J.claim(session, "w3").id == third.id


def test_terminal_batch_failure_cancels_every_later_item(session):
    first = J.enqueue(
        session, "process", meeting_id="m1", batch_id="batch", batch_position=1
    )
    second = J.enqueue(
        session, "process", meeting_id="m2", batch_id="batch", batch_position=2
    )
    third = J.enqueue(
        session, "process", meeting_id="m3", batch_id="batch", batch_position=3
    )

    assert J.claim(session, "w1").id == first.id
    J.fail(session, first, "terminal", allow_retry=False)

    assert first.status == JOB_FAILED
    assert second.status == JOB_CANCELED
    assert third.status == JOB_CANCELED
    assert "batch stopped" in second.step
    assert J.claim(session, "w2") is None


def test_batch_does_not_block_unrelated_jobs(session):
    first = J.enqueue(
        session, "process", meeting_id="m1", batch_id="batch", batch_position=1
    )
    J.enqueue(session, "process", meeting_id="m2", batch_id="batch", batch_position=2)
    unrelated = J.enqueue(session, "sync_recall", dedupe_key="sync")

    assert J.claim(session, "w1").id == first.id
    assert J.claim(session, "w2").id == unrelated.id


def test_claim_cas_rechecks_batch_predecessors(session):
    first = J.enqueue(
        session, "process", meeting_id="m1", batch_id="batch", batch_position=1
    )
    second = J.enqueue(
        session, "process", meeting_id="m2", batch_id="batch", batch_position=2
    )
    first.status = JOB_RUNNING
    session.commit()

    assert J._claim_candidate(session, second.id, "stale-worker", utcnow()) is False
    assert second.status == JOB_QUEUED


def test_fail_retries_then_gives_up(session):
    job = J.enqueue(session, "process", meeting_id="m1", max_attempts=2)
    J.claim(session, "w1")
    J.fail(session, job, "boom")
    assert job.status == JOB_QUEUED
    assert job.scheduled_at > utcnow()

    job.scheduled_at = utcnow()
    session.commit()
    J.claim(session, "w1")
    J.fail(session, job, "boom again")
    assert job.status == JOB_FAILED


def test_reap_stale_returns_job_to_queue(session):
    J.enqueue(session, "process", meeting_id="m1")
    job = J.claim(session, "w1")
    job.heartbeat_at = utcnow() - dt.timedelta(days=1)
    session.commit()

    assert J.reap_stale(session) == 1
    assert session.get(Job, job.id).status == JOB_QUEUED


def _queued_meeting(session, mid="m1"):
    """Spotkanie tak, jak zostawia je kliknięcie „przetwórz" w UI."""
    m = Meeting(id=mid, transcript_state="queued", asset_state="queued")
    session.add(m)
    session.commit()
    return m


def test_terminal_failure_releases_the_meeting(session):
    """Padnięte zadanie nie może zostawić spotkania w „queued" na zawsze.

    Inaczej spotkanie znika z filtra „bez transkryptu" i wypada
    z `meetings_ready_to_process()` — nikt go już nigdy nie przetworzy.
    """
    m = _queued_meeting(session)
    job = J.enqueue(session, "process", meeting_id=m.id, max_attempts=1)
    J.claim(session, "w1")
    J.fail(session, job, "assets missing")

    assert m.transcript_state == "failed"
    assert m.asset_state == "failed"
    assert "assets missing" in m.transcript_error


def test_retry_keeps_the_meeting_queued(session):
    """Dopóki zadanie wróci do kolejki, „queued" jest prawdą — nie ruszamy go."""
    m = _queued_meeting(session)
    job = J.enqueue(session, "process", meeting_id=m.id, max_attempts=3)
    J.claim(session, "w1")
    J.fail(session, job, "boom")

    assert job.status == JOB_QUEUED
    assert m.transcript_state == "queued"


def test_cancel_releases_the_meeting_to_none(session):
    """Anulowanie to nie porażka — nic się nie wydarzyło, można kolejkować od nowa."""
    m = _queued_meeting(session)
    job = J.enqueue(session, "process", meeting_id=m.id)

    assert J.cancel(session, job) is True
    assert m.transcript_state == "none"
    assert m.asset_state == "none"
    assert m.transcript_error is None


def test_transcribe_failure_leaves_assets_alone(session):
    """`transcribe` nie dotyka assetów — audio na dysku zostaje audio na dysku."""
    m = Meeting(id="m1", transcript_state="queued", asset_state="ready")
    session.add(m)
    session.commit()
    job = J.enqueue(session, "transcribe", meeting_id="m1", max_attempts=1)
    J.claim(session, "w1")
    J.fail(session, job, "pipeline boom")

    assert m.transcript_state == "failed"
    assert m.asset_state == "ready"


def test_release_orphaned_meetings_frees_stuck_rows(session):
    """Rezerwacja bez żywego zadania to sierota — po padzie workera albo
    po zadaniu, które umarło zanim ktokolwiek ją zdjął."""
    stuck = _queued_meeting(session, "stuck")
    job = J.enqueue(session, "process", meeting_id="stuck", max_attempts=1)
    job.status = JOB_FAILED
    job.error = "assets missing"
    session.commit()

    assert J.release_orphaned_meetings(session) == 1
    assert stuck.transcript_state == "failed"
    assert "assets missing" in stuck.transcript_error


def test_release_orphaned_meetings_spares_live_work(session):
    """Spotkanie z żywym zadaniem naprawdę czeka — nie wolno go zwolnić."""
    live = _queued_meeting(session, "live")
    J.enqueue(session, "process", meeting_id="live")

    assert J.release_orphaned_meetings(session) == 0
    assert live.transcript_state == "queued"


def test_run_job_records_failure_without_killing_worker(session):
    @J.task("boom_test")
    def _boom(ctx):
        raise RuntimeError("celowy wybuch")

    job = J.enqueue(session, "boom_test", max_attempts=1, dedupe_key="boom")
    J.claim(session, "w1")
    J.run_job(session, job)

    fresh = session.get(Job, job.id)
    assert fresh.status == JOB_FAILED
    assert "celowy wybuch" in fresh.error


def test_job_context_logs_are_capped(session):
    job = J.enqueue(session, "process", meeting_id="m1")
    ctx = J.JobContext(session, job)
    for i in range(4000):
        ctx.log(f"linia {i} " + "x" * 40)
    assert len(job.log) <= J.MAX_LOG_CHARS + 200
    assert "truncated" in job.log
