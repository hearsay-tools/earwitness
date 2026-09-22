"""Webhook transkryptu: konfiguracja, payload, kolejka, sekrety."""

from __future__ import annotations

import datetime as dt
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from webapp import jobs as J
from webapp import tasks, webhook
from webapp.app import app
from webapp.models import AppSetting, Job, Meeting, MeetingParticipant, Transcript

HTML = {"accept": "text/html"}
TOKEN = "ew-bearer-9f3c1a7e"
PHRASE = "unique-transcript-phrase"


@pytest.fixture()
def client(session):
    return TestClient(app)


def _meeting(session, tmp_path, text: str | None = None) -> tuple[Meeting, Transcript]:
    body = text if text is not None else f"Ada [00:00:01] {PHRASE}\n"
    path = tmp_path / "transcript.txt"
    path.write_text(body, encoding="utf-8")
    meeting = Meeting(
        id="bot-hook",
        title="Roadmap sync",
        platform="google_meet",
        meeting_url="https://meet.example/abc",
        meeting_native_id="abc-def",
        started_at=dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc),
        completed_at=dt.datetime(2026, 9, 1, 10, 30, tzinfo=dt.timezone.utc),
        duration_seconds=1800,
        status_code="done",
        status_group="done",
        recording_id="rec-1",
        transcript_state="ready",
        calendar_organizer="ola@example.com",
        calendar_event_id="cal-1",
        calendar_html_link="https://calendar.example/event",
    )
    session.add(meeting)
    session.add(
        MeetingParticipant(
            meeting_id=meeting.id,
            source="recall",
            key="ada@example.com",
            name="Ada",
            email="ada@example.com",
            is_host=True,
            speaking_seconds=12.5,
        )
    )
    session.add(
        MeetingParticipant(
            meeting_id=meeting.id,
            source="calendar",
            key="ola@example.com",
            name="Ola",
            email="ola@example.com",
        )
    )
    session.add(
        MeetingParticipant(
            meeting_id=meeting.id,
            source="recall",
            key="otter",
            name="Otter Notetaker",
            is_bot=True,
        )
    )
    transcript = Transcript(
        meeting_id=meeting.id,
        recording_id="rec-1",
        engine="pipeline-recall",
        language="pl",
        text_path=str(path),
        speakers=[{"name": "Ada", "seconds": 12.5}],
        utterance_count=1,
        word_count=2,
        duration_seconds=12.5,
    )
    session.add(transcript)
    session.commit()
    return meeting, transcript


def _save(client: TestClient, **overrides: str) -> httpx.Response:
    data = {
        "url": "https://hooks.example/earwitness",
        "method": "POST",
        "bearer_token": TOKEN,
    }
    data.update(overrides)
    return client.post("/settings/webhook", data=data, follow_redirects=False)


def _configure(
    session, *, method: str = "POST", token: str = TOKEN, url: str | None = None
):
    webhook.save_config(
        session,
        url=url or "https://hooks.example/earwitness",
        method=method,
        token=token,
        clear_token=not token,
    )


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _run_delivery(session, meeting, transcript, handler, monkeypatch):
    seen: list[httpx.Request] = []

    def wrapped(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = webhook.send_request

    def send(method, url, *, headers, content=None, transport=None):
        return real(
            method,
            url,
            headers=headers,
            content=content,
            transport=_transport(wrapped),
        )

    monkeypatch.setattr(webhook, "send_request", send)
    job = J.enqueue(
        session,
        webhook.KIND,
        meeting_id=meeting.id,
        args={"transcript_id": transcript.id},
        max_attempts=webhook.MAX_ATTEMPTS,
    )
    J.run_job(session, J.claim(session, "w1", kinds=[webhook.KIND]))
    session.refresh(job)
    session.refresh(meeting)
    return job, seen


def test_settings_page_hides_saved_token(client, session):
    saved = _save(client)
    assert saved.status_code == 303
    assert TOKEN not in saved.headers["location"]
    assert saved.headers["location"].endswith("/settings?saved=1")

    page = client.get("/settings", headers=HTML)
    assert page.status_code == 200
    assert TOKEN not in page.text
    assert "A token is saved" in page.text
    assert 'value="https://hooks.example/earwitness"' in page.text
    assert 'name="bearer_token" value=""' in page.text
    assert "Webhook settings saved." in client.get("/settings?saved=1").text
    assert "token" not in webhook.public_settings(webhook.get_config(session))

    session.expire_all()
    stored = session.get(AppSetting, webhook.TOKEN_KEY)
    assert stored is not None and stored.value == TOKEN
    assert session.query(Job).count() == 0


def test_blank_token_is_kept_and_clear_removes_it(client, session):
    _save(client)
    again = _save(client, bearer_token="", url="https://hooks.example/other")
    assert again.status_code == 303
    session.expire_all()
    assert webhook.get_config(session).token == TOKEN
    assert webhook.get_config(session).url == "https://hooks.example/other"

    cleared = _save(client, bearer_token="", clear_token="true")
    assert cleared.status_code == 303
    session.expire_all()
    cfg = webhook.get_config(session)
    assert cfg.token == ""
    assert cfg.has_token is False
    page = client.get("/settings")
    assert TOKEN not in page.text
    assert "A token is saved" not in page.text
    assert "Optional" in page.text


def test_new_token_wins_over_clear(client, session):
    _save(client)
    _save(client, bearer_token="replacement-token-xyz", clear_token="true")
    session.expire_all()
    assert webhook.get_config(session).token == "replacement-token-xyz"
    assert "replacement-token-xyz" not in client.get("/settings").text


def test_invalid_settings_do_not_replace_a_saved_secret(client, session):
    _save(client)
    rejected = _save(
        client,
        url="https://user:pass@hooks.example/hook",
        bearer_token="another-secret-value",
    )
    assert rejected.headers["location"].endswith("error=invalid_url")
    assert "another-secret-value" not in rejected.headers["location"]
    session.expire_all()
    cfg = webhook.get_config(session)
    assert cfg.url == "https://hooks.example/earwitness"
    assert cfg.token == TOKEN
    page = client.get(rejected.headers["location"])
    assert "another-secret-value" not in page.text
    assert TOKEN not in page.text
    assert "username, password" in page.text


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "https://user:secret@hooks.example/hook",
        "https://hooks.example/hook?access_token=ew-bearer-9f3c1a7e",
        "https://hooks.example/hook?payload=1",
        "https://hooks.example/hook?payload",
        "https://hooks.example/hook?payload=",
        "https://hooks.example/hook?foo=1&payload=",
        "https://hooks.example/ew-bearer-9f3c1a7e",
        "not a url",
        "http:///missing-host",
    ],
)
def test_unsafe_urls_are_rejected(client, url):
    rejected = _save(client, url=url)
    assert rejected.headers["location"].endswith("error=invalid_url")
    assert TOKEN not in rejected.headers["location"]
    assert TOKEN not in client.get("/settings").text


def test_url_with_saved_or_short_token_is_rejected(client, session):
    _save(client)
    rotated = _save(
        client,
        url=f"https://hooks.example/hook?old={TOKEN}",
        bearer_token="replacement-token-xyz",
    )
    assert rotated.headers["location"].endswith("error=invalid_url")
    assert TOKEN not in rotated.headers["location"]
    session.expire_all()
    cfg = webhook.get_config(session)
    assert cfg.url == "https://hooks.example/earwitness"
    assert cfg.token == TOKEN
    page = client.get("/settings")
    assert TOKEN not in page.text
    assert "replacement-token-xyz" not in page.text

    cleared = _save(
        client,
        url=f"https://hooks.example/{TOKEN}",
        bearer_token="",
        clear_token="true",
    )
    assert cleared.headers["location"].endswith("error=invalid_url")
    session.expire_all()
    assert webhook.get_config(session).token == TOKEN
    assert TOKEN not in client.get("/settings").text

    short = "q7k"
    rejected = _save(
        client,
        url=f"https://hooks.example/hook?t={short}",
        bearer_token=short,
    )
    assert rejected.headers["location"].endswith("error=invalid_url")
    session.expire_all()
    assert webhook.get_config(session).token == TOKEN
    assert short not in client.get("/settings").text

    kept = _save(client, bearer_token=short)
    assert kept.status_code == 303
    session.expire_all()
    assert webhook.get_config(session).token == short


@pytest.mark.parametrize(
    "url",
    [
        "https://[bad",
        "http://hooks.example:99999/hook",
        "http://hooks.example:abc/hook",
        "http://hooks.example:0/hook",
        "https://hooks.example/hook?api_key=secret",
        "https://hooks.example/hook?api-key=secret",
        "https://hooks.example/hook?signature=abc",
        "https://hooks.example/hook?sig=abc",
        "https://hooks.example/hook?key=abc",
    ],
)
def test_malformed_or_credential_urls_are_rejected(client, url):
    rejected = _save(client, url=url, bearer_token="")
    assert rejected.status_code == 303
    assert rejected.headers["location"].endswith("error=invalid_url")


def test_explicit_port_and_ipv6_are_accepted(client, session):
    saved = _save(client, url="https://hooks.example:8443/hook", bearer_token="")
    assert saved.status_code == 303
    assert saved.headers["location"].endswith("saved=1")
    session.expire_all()
    assert webhook.get_config(session).url == "https://hooks.example:8443/hook"

    ipv6 = _save(client, url="https://[::1]:8443/hook", bearer_token="")
    assert ipv6.status_code == 303
    assert ipv6.headers["location"].endswith("saved=1")


def test_whitespace_token_is_rejected_and_plus_form_is_redacted(client, session):
    rejected = _save(client, bearer_token="a b")
    assert rejected.status_code == 303
    assert rejected.headers["location"].endswith("error=invalid_token")
    assert "a b" not in rejected.headers["location"]
    assert "a+b" not in rejected.headers["location"]
    page = client.get(rejected.headers["location"])
    assert "a b" not in page.text
    assert "a+b" not in page.text
    assert "whitespace" in page.text
    session.expire_all()
    assert webhook.get_config(session).token == ""

    session.add(AppSetting(key=webhook.TOKEN_KEY, value="a b"))
    session.commit()
    leaked = _save(client, url="https://hooks.example/hook?q=a+b", bearer_token="")
    assert leaked.headers["location"].endswith("error=invalid_url")
    assert "a+b" not in client.get("/settings").text
    assert "a b" not in client.get("/settings").text
    assert webhook.redact("failed a+b and a%20b", "a b") == (
        "failed [redacted] and [redacted]"
    )


def test_control_characters_and_non_latin1_tokens_are_rejected(client, session):
    for bad in ("abc\ndef", "token\x00x", "zażółć-token"):
        rejected = _save(client, bearer_token=bad)
        assert rejected.headers["location"].endswith("error=invalid_token")
        assert bad not in rejected.headers["location"]
        assert bad not in client.get("/settings").text
    session.expire_all()
    assert webhook.get_config(session).token == ""


def test_error_query_is_not_reflected(client):
    page = client.get("/settings?error=%3Cscript%3Ealert(1)%3C/script%3E")
    assert "alert(1)" not in page.text
    assert page.status_code == 200


def test_settings_nav_and_method_choice(client):
    assert 'href="/settings"' in client.get("/meetings", headers=HTML).text
    _save(client, method="GET", bearer_token="")
    page = client.get("/settings")
    assert 'name="method" value="GET" checked' in page.text
    assert "8000" in page.text


def _payload_from(request: httpx.Request) -> dict:
    if request.method == "POST":
        return json.loads(request.content)
    return json.loads(request.url.params["payload"])


def test_post_delivery_sends_meeting_and_transcript(
    session, tmp_path, monkeypatch, caplog
):
    meeting, transcript = _meeting(session, tmp_path)
    _configure(session)
    caplog.set_level(logging.INFO)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204, text=f"echo {TOKEN} {PHRASE}")

    job, seen = _run_delivery(session, meeting, transcript, handler, monkeypatch)
    assert job.status == "done"
    assert meeting.transcript_state == "ready"
    assert meeting.transcript_error is None
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://hooks.example/earwitness"
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert request.headers["content-type"].startswith("application/json")
    assert request.headers["x-earwitness-event"] == "transcript.ready"
    assert request.headers["x-earwitness-delivery"] == str(job.id)
    payload = _payload_from(request)
    assert payload["schema"] == "earwitness.transcript.ready.v1"
    assert payload["event"] == "transcript.ready"
    assert payload["delivery"] == {"job_id": job.id, "attempt": 1}
    assert payload["meeting"]["id"] == meeting.id
    assert payload["meeting"]["title"] == "Roadmap sync"
    assert payload["meeting"]["native_id"] == "abc-def"
    assert payload["meeting"]["user_status"] == "ready"
    assert payload["meeting"]["organizer"] == "ola@example.com"
    assert [p["email"] for p in payload["meeting"]["participants"]] == [
        "ada@example.com",
        "ola@example.com",
    ]
    assert payload["transcript"]["text"] == f"Ada [00:00:01] {PHRASE}\n"
    assert payload["transcript"]["id"] == transcript.id
    blob = json.dumps(job.result) + (job.log or "") + (job.error or "") + caplog.text
    assert TOKEN not in blob
    assert PHRASE not in (job.log or "")
    assert PHRASE not in caplog.text
    assert "echo" not in blob


def test_get_delivery_uses_payload_query_and_does_not_log_it(
    session, tmp_path, monkeypatch, caplog
):
    meeting, transcript = _meeting(session, tmp_path)
    _configure(session, method="GET")
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    job, seen = _run_delivery(session, meeting, transcript, handler, monkeypatch)
    request = seen[0]
    assert request.method == "GET"
    assert request.content == b""
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    payload = _payload_from(request)
    assert payload["transcript"]["text"].endswith(f"{PHRASE}\n")
    assert "payload" in str(request.url)
    assert job.status == "done"
    assert PHRASE not in (job.log or "")
    assert PHRASE not in caplog.text
    assert TOKEN not in caplog.text
    assert TOKEN not in (job.log or "")


def test_get_over_limit_fails_without_a_request(session, tmp_path, monkeypatch):
    meeting, transcript = _meeting(session, tmp_path, text="x" * 9000)
    _configure(session, method="GET")
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    job, _seen = _run_delivery(session, meeting, transcript, handler, monkeypatch)
    assert called is False
    assert job.status == "failed"
    assert "8000" in job.error
    assert "Traceback" not in job.error
    assert meeting.transcript_state == "ready"
    assert J.claim(session, "w2", kinds=[webhook.KIND]) is None


@pytest.mark.parametrize("status", [400, 401, 404, 302])
def test_client_errors_and_redirects_do_not_retry_or_touch_the_transcript(
    session, tmp_path, monkeypatch, status
):
    meeting, transcript = _meeting(session, tmp_path)
    _configure(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"location": f"https://evil.example/?token={TOKEN}"},
            text=TOKEN,
        )

    job, seen = _run_delivery(session, meeting, transcript, handler, monkeypatch)
    assert len(seen) == 1
    assert job.status == "failed"
    assert "Traceback" not in (job.error or "")
    assert TOKEN not in (job.error or "")
    assert TOKEN not in (job.log or "")
    assert "evil.example" not in (job.error or "")
    assert meeting.transcript_state == "ready"
    assert meeting.transcript_error is None
    assert J.claim(session, "w2", kinds=[webhook.KIND]) is None


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_http_failures_retry_without_invalidating_the_transcript(
    session, tmp_path, monkeypatch, status
):
    meeting, transcript = _meeting(session, tmp_path)
    _configure(session)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=TOKEN)

    job, _seen = _run_delivery(session, meeting, transcript, handler, monkeypatch)
    assert job.status == "queued"
    assert job.attempts == 1
    assert job.scheduled_at > dt.datetime.now(dt.timezone.utc)
    assert TOKEN not in (job.error or "")
    assert meeting.transcript_state == "ready"
    assert J.claim(session, "w2", kinds=[webhook.KIND]) is None


def test_connection_error_does_not_log_the_transcript_or_token(
    session, tmp_path, monkeypatch, caplog
):
    meeting, transcript = _meeting(session, tmp_path)
    _configure(session, method="GET")
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    job, _seen = _run_delivery(session, meeting, transcript, handler, monkeypatch)
    assert job.status == "queued"
    assert PHRASE not in (job.error or "")
    assert PHRASE not in (job.log or "")
    assert PHRASE not in caplog.text
    assert TOKEN not in caplog.text
    assert meeting.transcript_state == "ready"


def test_disabled_webhook_skips_without_a_request(session, tmp_path, monkeypatch):
    meeting, transcript = _meeting(session, tmp_path)
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200)

    job, _seen = _run_delivery(session, meeting, transcript, handler, monkeypatch)
    assert called is False
    assert job.status == "done"
    assert job.result["skipped"] is True
    assert "webhook disabled" in (job.log or "")
    assert meeting.transcript_state == "ready"


def test_token_embedded_in_a_stored_url_is_redacted(session, tmp_path, monkeypatch):
    meeting, transcript = _meeting(session, tmp_path)
    session.add(AppSetting(key=webhook.URL_KEY, value=f"https://hooks.example/{TOKEN}"))
    session.add(AppSetting(key=webhook.METHOD_KEY, value="POST"))
    session.add(AppSetting(key=webhook.TOKEN_KEY, value=TOKEN))
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    job, seen = _run_delivery(session, meeting, transcript, handler, monkeypatch)
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in job.result["url"]
    assert "[redacted]" in job.result["url"]
    assert TOKEN not in (job.log or "")


def test_missing_transcript_file_fails_without_changing_state(session, tmp_path):
    meeting, transcript = _meeting(session, tmp_path)
    _configure(session)
    tmp_path.joinpath("transcript.txt").unlink()
    job = J.enqueue(
        session,
        webhook.KIND,
        meeting_id=meeting.id,
        args={"transcript_id": transcript.id},
    )
    J.run_job(session, J.claim(session, "w1"))
    session.refresh(job)
    session.refresh(meeting)
    assert job.status == "failed"
    assert "missing" in job.error
    assert meeting.transcript_state == "ready"


def test_pipeline_queues_delivery_only_when_configured(session, tmp_path, monkeypatch):
    meeting, transcript = _meeting(session, tmp_path)

    def fake_pipeline(ctx, found, force_asr=False):
        found.transcript_state = "ready"
        ctx.session.commit()
        return {"transcript_id": transcript.id}

    monkeypatch.setattr(tasks, "_do_pipeline", fake_pipeline)
    job = J.enqueue(session, "transcribe", meeting_id=meeting.id)
    J.run_job(session, J.claim(session, "w1", kinds=["transcribe"]))
    assert session.query(Job).filter(Job.kind == webhook.KIND).count() == 0
    assert job.result["webhook_job"] is None
    assert meeting.transcript_state == "ready"

    _configure(session)
    job = J.enqueue(session, "transcribe", meeting_id=meeting.id)
    J.run_job(session, J.claim(session, "w1", kinds=["transcribe"]))
    queued = session.query(Job).filter(Job.kind == webhook.KIND).one()
    assert queued.args == {"transcript_id": transcript.id}
    assert queued.max_attempts == 3
    assert queued.created_by == "automatic"
    assert TOKEN not in json.dumps(queued.args)
    assert job.result["webhook_job"] == queued.id
    assert meeting.transcript_state == "ready"


def test_process_queues_webhook_and_a_failed_pipeline_does_not(
    session, tmp_path, monkeypatch
):
    meeting, transcript = _meeting(session, tmp_path)
    _configure(session)
    monkeypatch.setattr(
        tasks, "_do_fetch", lambda ctx, found, force=False: {"ok": True}
    )

    def fake_pipeline(ctx, found, force_asr=False):
        return {"transcript_id": transcript.id}

    monkeypatch.setattr(tasks, "_do_pipeline", fake_pipeline)
    J.enqueue(session, "process", meeting_id=meeting.id)
    J.run_job(session, J.claim(session, "w1", kinds=["process"]))
    assert session.query(Job).filter(Job.kind == webhook.KIND).count() == 1

    def boom(ctx, found, force_asr=False):
        raise RuntimeError("asr down")

    monkeypatch.setattr(tasks, "_do_pipeline", boom)
    J.enqueue(session, "transcribe", meeting_id=meeting.id)
    J.run_job(session, J.claim(session, "w1", kinds=["transcribe"]))
    assert session.query(Job).filter(Job.kind == webhook.KIND).count() == 1
    session.refresh(meeting)
    assert meeting.transcript_state == "failed"


def test_webhook_queue_db_error_does_not_fail_transcription(
    session, tmp_path, monkeypatch
):
    meeting, transcript = _meeting(session, tmp_path)

    def fake_pipeline(ctx, found, force_asr=False):
        found.transcript_state = "ready"
        ctx.session.commit()
        return {"transcript_id": transcript.id}

    def boom(db, *_args, **_kwargs):
        from sqlalchemy import text

        db.execute(text("SELECT * FROM missing_webhook_table"))

    monkeypatch.setattr(tasks, "_do_pipeline", fake_pipeline)
    monkeypatch.setattr(webhook, "queue_delivery", boom)
    job = J.enqueue(session, "transcribe", meeting_id=meeting.id)
    J.run_job(session, J.claim(session, "w1", kinds=["transcribe"]))
    session.refresh(job)
    session.refresh(meeting)
    assert job.status == "done"
    assert job.result["webhook_job"] is None
    assert meeting.transcript_state == "ready"
    assert "webhook not queued" in (job.log or "")
    assert TOKEN not in (job.log or "")


def test_webhook_queue_error_does_not_fail_transcription(
    session, tmp_path, monkeypatch
):
    meeting, transcript = _meeting(session, tmp_path)

    def fake_pipeline(ctx, found, force_asr=False):
        found.transcript_state = "ready"
        return {"transcript_id": transcript.id}

    def boom(*_args, **_kwargs):
        raise RuntimeError(TOKEN)

    monkeypatch.setattr(tasks, "_do_pipeline", fake_pipeline)
    monkeypatch.setattr(webhook, "queue_delivery", boom)
    job = J.enqueue(session, "transcribe", meeting_id=meeting.id)
    J.run_job(session, J.claim(session, "w1", kinds=["transcribe"]))
    session.refresh(job)
    session.refresh(meeting)
    assert job.status == "done"
    assert job.result["webhook_job"] is None
    assert meeting.transcript_state == "ready"
    assert TOKEN not in (job.log or "")
    assert TOKEN not in (job.error or "")


def test_meeting_page_can_queue_delivery_without_showing_the_token(
    client, session, tmp_path
):
    meeting, transcript = _meeting(session, tmp_path)
    quiet = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert "Set up delivery" in quiet.text
    assert f"/meetings/{meeting.id}/webhook" not in quiet.text
    missing = client.post(f"/meetings/{meeting.id}/webhook")
    assert missing.status_code == 400

    _configure(session)
    page = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert TOKEN not in page.text
    assert "Send to webhook" in page.text
    first = client.post(f"/meetings/{meeting.id}/webhook", follow_redirects=False)
    second = client.post(f"/meetings/{meeting.id}/webhook", follow_redirects=False)
    assert first.status_code == 303
    assert second.status_code == 303
    jobs = session.query(Job).filter(Job.kind == webhook.KIND).all()
    session.expire_all()
    jobs = session.query(Job).filter(Job.kind == webhook.KIND).all()
    assert len(jobs) == 1
    assert jobs[0].args == {"transcript_id": transcript.id}
    assert jobs[0].created_by == "dev@localhost"
    assert TOKEN not in json.dumps(jobs[0].args)
    queue = client.get("/jobs", headers=HTML)
    assert "Webhook delivery" in queue.text
    assert TOKEN not in queue.text
    log = client.get(f"/api/jobs/{jobs[0].id}/log")
    assert TOKEN not in log.text
