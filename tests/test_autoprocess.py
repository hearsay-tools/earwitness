"""Process automatically is a durable setting, not a one-shot Sync flag."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from webapp.app import app
from webapp.config import settings
from webapp.models import Job, Meeting

HTML = {"accept": "text/html,application/xhtml+xml"}


@pytest.fixture()
def client(session):
    return TestClient(app)


def _autoprocess_input(html: str) -> str:
    match = re.search(r"<input\b[^>]*\bname=\"autoprocess\"[^>]*>", html)
    assert match is not None, "process automatically checkbox missing"
    return match.group(0)


def _ready_meeting(session) -> Meeting:
    meeting = Meeting(
        id="bot-ready",
        title="Standup",
        status_code="done",
        status_group="done",
        recording_id="rec-1",
        asset_state="none",
        transcript_state="none",
    )
    session.add(meeting)
    session.commit()
    return meeting


def _run_sync(session, monkeypatch):
    from webapp import jobs as J
    from webapp import tasks as T

    monkeypatch.setattr(settings, "recall_api_key", "x")
    monkeypatch.setattr(
        T, "sync_bots", lambda *a, **k: {"seen": 0, "created": 0, "updated": 0}
    )
    job = J.enqueue(
        session,
        "sync_recall",
        args={"with_calendar": False},
        dedupe_key="sync_recall:test",
    )
    return J.get_task("sync_recall")(J.JobContext(session, job))


def test_checking_process_automatically_survives_refresh(client: TestClient):
    posted = client.post(
        "/sync",
        data={"autoprocess": "true", "with_calendar": "true"},
        follow_redirects=False,
    )
    assert posted.status_code == 303

    page = client.get("/meetings", headers=HTML)
    assert page.status_code == 200
    assert "checked" in _autoprocess_input(page.text)


def test_unchecking_process_automatically_stays_off_after_refresh(client: TestClient):
    client.post(
        "/sync",
        data={"autoprocess": "true", "with_calendar": "true"},
        follow_redirects=False,
    )
    client.post("/sync", data={"with_calendar": "true"}, follow_redirects=False)

    page = client.get("/meetings", headers=HTML)
    assert page.status_code == 200
    assert "checked" not in _autoprocess_input(page.text)


def test_checking_survives_when_manual_sync_is_already_running(client, session):
    from webapp import jobs as J

    job = J.enqueue(session, "sync_recall", dedupe_key="sync_recall:manual")
    claimed = J.claim(session, "w1")
    assert claimed is not None and claimed.id == job.id

    posted = client.post(
        "/sync",
        data={"autoprocess": "true", "with_calendar": "true"},
        follow_redirects=False,
    )
    assert posted.status_code == 303

    page = client.get("/meetings", headers=HTML)
    assert "checked" in _autoprocess_input(page.text)


def test_conflicting_first_insert_still_saves(session):
    from webapp.app_settings import get_autoprocess, save_autoprocess, set_autoprocess
    from webapp.db import SessionLocal

    first = SessionLocal()
    second = SessionLocal()
    try:
        set_autoprocess(first, True)
        set_autoprocess(second, False)
        first.commit()
        save_autoprocess(second, False)
        assert get_autoprocess(session) is False
    finally:
        first.close()
        second.close()


def _process_jobs(session) -> list[Job]:
    return list(session.execute(select(Job).where(Job.kind == "process")).scalars())


def test_sync_queues_ready_meetings_when_autoprocess_is_on(
    client, session, monkeypatch
):
    monkeypatch.setattr(settings, "autoprocess", False)
    _ready_meeting(session)
    client.post(
        "/sync",
        data={"autoprocess": "true", "with_calendar": "true"},
        follow_redirects=False,
    )

    result = _run_sync(session, monkeypatch)

    assert result.get("queued") == 1
    jobs = _process_jobs(session)
    assert [j.meeting_id for j in jobs] == ["bot-ready"]
    assert jobs[0].created_by == "autoprocess"


def test_sync_does_not_queue_when_autoprocess_is_off(client, session, monkeypatch):
    monkeypatch.setattr(settings, "autoprocess", True)
    _ready_meeting(session)
    client.post("/sync", data={"with_calendar": "true"}, follow_redirects=False)

    result = _run_sync(session, monkeypatch)

    assert result.get("queued") in (None, 0)
    assert _process_jobs(session) == []


def test_manual_sync_with_checkbox_on_still_queues(client, session, monkeypatch):
    from webapp import jobs as J
    from webapp import tasks as T

    monkeypatch.setattr(settings, "autoprocess", False)
    monkeypatch.setattr(settings, "recall_api_key", "x")
    monkeypatch.setattr(
        T, "sync_bots", lambda *a, **k: {"seen": 0, "created": 0, "updated": 0}
    )
    _ready_meeting(session)
    posted = client.post(
        "/sync",
        data={"autoprocess": "true", "with_calendar": "false"},
        follow_redirects=False,
    )
    assert posted.status_code == 303

    job = session.execute(
        select(Job).where(Job.dedupe_key == "sync_recall:manual")
    ).scalar_one()
    result = J.get_task("sync_recall")(J.JobContext(session, job))

    assert result.get("queued") == 1
    assert [j.meeting_id for j in _process_jobs(session)] == ["bot-ready"]
