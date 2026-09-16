"""Pamięć spotkań w Honcho (issue #29): tożsamość, ingest, kolejka, widoki.

Honcho nie stoi w testach — podmieniamy klienta na atrapę, która pamięta,
co appka by wysłała. Sprawdzamy kontrakt, nie transport: ten sam peer dla
mówcy i zalogowanego użytkownika, komplet uczestników w sesji, paczki
wiadomości w limicie serwera, idempotentne nadpisanie sesji.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from webapp import jobs as J
from webapp import memory, tasks
from webapp.app import app
from webapp.config import settings
from webapp.db import add_missing_columns, engine
from webapp.models import Job, Meeting, MeetingParticipant, Transcript, User

HTML = {"accept": "text/html,application/xhtml+xml"}


# --------------------------------------------------------------------------
# Atrapa Honcho
# --------------------------------------------------------------------------


class _NotFound(Exception):
    pass


class FakePeer:
    def __init__(self, hub: "FakeHoncho", id: str, metadata: Any = None) -> None:
        self.hub, self.id, self.metadata = hub, id, metadata

    def message(
        self, content: str, *, metadata: Any = None, created_at: Any = None
    ) -> dict:
        return {
            "peer_id": self.id,
            "content": content,
            "metadata": metadata,
            "created_at": created_at,
        }

    def chat(
        self, query: str, *, session: Any = None, reasoning_level: Any = None
    ) -> str | None:
        self.hub.chats.append(
            {
                "peer": self.id,
                "query": query,
                "session": session,
                "level": reasoning_level,
            }
        )
        if self.hub.chat_error:
            raise self.hub.chat_error
        return self.hub.chat_answer


class FakeSession:
    def __init__(
        self, hub: "FakeHoncho", id: str, metadata: Any = None, peers: Any = None
    ) -> None:
        self.hub, self.id, self.metadata = hub, id, metadata
        self._peers = list(peers or [])
        self.messages: list[dict] = []
        self.batches: list[int] = []
        self.set_peers_calls = 0

    def peers(self) -> list[FakePeer]:
        if self.id not in self.hub.sessions:
            raise self.hub.not_found_cls("no such session")
        return [p for p, _cfg in self._peers]

    def set_peers(self, peers: list) -> None:
        self.set_peers_calls += 1
        self._peers = list(peers)

    def delete(self) -> None:
        if self.id not in self.hub.sessions:
            raise self.hub.not_found_cls("no such session")
        self.hub.deleted.append(self.id)
        del self.hub.sessions[self.id]

    def add_messages(self, messages: list[dict]) -> list[dict]:
        assert len(messages) <= memory.MESSAGE_BATCH, "batch above the server limit"
        self.batches.append(len(messages))
        self.messages.extend(messages)
        if self.hub.on_add_messages is not None:
            self.hub.on_add_messages()
        return messages


class FakeHoncho:
    def __init__(self, not_found_cls: type[Exception]) -> None:
        self.not_found_cls = not_found_cls
        self.create_error: Exception | None = None
        self.on_add_messages: Any = None
        self.peers: dict[str, FakePeer] = {}
        self.sessions: dict[str, FakeSession] = {}
        self.deleted: list[str] = []
        self.chats: list[dict] = []
        self.chat_answer: str | None = "We agreed to ship on Friday."
        self.chat_error: Exception | None = None

    def peer(
        self, id: str, *, metadata: Any = None, configuration: Any = None
    ) -> FakePeer:
        if id not in self.peers or metadata is not None:
            self.peers[id] = FakePeer(self, id, metadata)
        return self.peers[id]

    def session(
        self,
        id: str,
        *,
        metadata: Any = None,
        peers: Any = None,
        configuration: Any = None,
    ) -> FakeSession:
        if metadata is None and peers is None:
            # Leniwe uchwyty, jak w SDK — nie tworzą sesji po stronie serwera.
            return self.sessions.get(id) or FakeSession(self, id)
        if self.create_error is not None:
            raise self.create_error
        self.sessions[id] = FakeSession(self, id, metadata, peers)
        return self.sessions[id]


@pytest.fixture()
def honcho(monkeypatch):
    from honcho import NotFoundError

    fake = FakeHoncho(NotFoundError)
    monkeypatch.setattr(memory, "client", lambda: fake)
    monkeypatch.setattr(settings, "honcho_enabled", True)
    return fake


@pytest.fixture()
def client(session):
    return TestClient(app)


@pytest.fixture()
def meeting(session, tmp_path):
    """Trzy osoby: dwie mówią, jedna (Ola) była tylko zaproszona."""
    m = Meeting(
        id="bot-42",
        title="Roadmap sync",
        platform="google_meet",
        started_at=dt.datetime(2026, 9, 1, 10, 0, tzinfo=dt.timezone.utc),
        status_code="done",
        status_group="done",
        recording_id="rec-42",
        transcript_state="ready",
    )
    m.participants = [
        MeetingParticipant(
            source="recall",
            key="jan@acme.com",
            name="Jan Kowalski",
            email="jan@acme.com",
        ),
        MeetingParticipant(source="recall", key="guest speaker", name="Guest Speaker"),
        MeetingParticipant(
            source="calendar",
            key="ola@acme.com",
            name="Ola Nowak",
            email="ola@acme.com",
        ),
        MeetingParticipant(
            source="calendar",
            key="fred@fireflies.ai",
            email="fred@fireflies.ai",
            is_bot=True,
        ),
    ]
    path = tmp_path / "t.txt"
    lines = ["Jan Kowalski [00:00:01] Let's start.", "Guest Speaker [00:00:05] Sure."]
    lines += [
        f"Jan Kowalski [00:{i // 60:02d}:{i % 60:02d}] line {i}" for i in range(10, 240)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    t = Transcript(
        meeting_id=m.id, text_path=str(path), utterance_count=len(lines), speakers=[]
    )
    session.add(m)
    session.add(t)
    session.commit()
    return m


def _user(session, email: str, name: str = "Someone") -> User:
    u = User(google_sub=email, email=email, name=name)
    session.add(u)
    session.commit()
    return u


# --------------------------------------------------------------------------
# Tożsamość
# --------------------------------------------------------------------------


def test_peer_id_is_honcho_safe_and_stable():
    pid = memory.peer_id("Jan.Kowalski@Acme.com")
    assert memory.RESOURCE_ID_RE.match(pid), pid
    assert pid == memory.peer_id("jan.kowalski@acme.com"), (
        "wielkość liter nie może rozdwoić osoby"
    )
    assert memory.peer_id("a.b@x.com") != memory.peer_id("a-b@x.com"), (
        "slug ten sam, adres inny"
    )


def test_speaker_and_logged_in_user_share_a_peer(session, meeting):
    """Bez tego „ask-as-self” jest pustym hasłem: użytkownik pytałby jako
    ktoś, kto na żadnym spotkaniu nie był."""
    jan = _user(session, "jan@acme.com", "Jan Kowalski")
    p = next(p for p in meeting.participants if p.email == "jan@acme.com")
    assert memory.user_peer_id(jan) == memory.peer_id(
        memory.identity_key(p.name, p.email)
    )


def test_user_can_ask_only_when_on_the_participant_list(session, meeting):
    assert memory.user_can_ask(
        meeting, _user(session, "ola@acme.com")
    )  # zaproszona, nie mówiła
    assert memory.user_can_ask(meeting, _user(session, "JAN@acme.com"))
    assert not memory.user_can_ask(meeting, _user(session, "nobody@acme.com"))
    assert not memory.user_can_ask(meeting, _user(session, "fred@fireflies.ai")), (
        "bot to nie uczestnik"
    )


# --------------------------------------------------------------------------
# Ingest
# --------------------------------------------------------------------------


def test_ingest_builds_one_session_per_meeting_with_every_attendee(
    session, meeting, honcho
):
    t = meeting.latest_transcript
    result = memory.ingest_transcript(session, t)
    session.commit()

    sess = honcho.sessions[meeting.id]
    assert sess.metadata["title"] == "Roadmap sync"
    peer_ids = {p.id for p in sess.peers()}
    # Obecność daje dostęp: Ola (tylko zaproszona) też jest w sesji, bot nie.
    assert memory.peer_id("ola@acme.com") in peer_ids
    assert memory.peer_id("fred@fireflies.ai") not in peer_ids
    assert result["silent_attendees"] == 1

    # Mówca z adresem pisze jako peer z adresu; bez adresu — jako peer z nazwy.
    authors = {m["peer_id"] for m in sess.messages}
    assert memory.peer_id("jan@acme.com") in authors
    assert memory.peer_id("guest speaker") in authors
    assert len(sess.messages) == t.utterance_count == result["messages"]
    assert (
        all(n <= memory.MESSAGE_BATCH for n in sess.batches) and len(sess.batches) >= 3
    )

    first = sess.messages[0]
    assert first["created_at"] == meeting.started_at + dt.timedelta(seconds=1)
    assert first["metadata"]["timestamp"] == "00:00:01"
    assert t.honcho_synced_at is not None


def test_ingest_uses_join_at_when_started_at_is_missing(session, meeting, honcho):
    meeting.started_at = None
    meeting.join_at = dt.datetime(2026, 4, 1, 9, 0, tzinfo=dt.timezone.utc)
    session.commit()
    memory.ingest_transcript(session, meeting.latest_transcript)
    first = honcho.sessions[meeting.id].messages[0]
    assert first["created_at"] == meeting.join_at + dt.timedelta(seconds=1)


def test_ingest_without_meeting_time_logs_ingest_time_fallback(
    session, meeting, honcho
):
    meeting.started_at = None
    meeting.join_at = None
    session.commit()
    job = J.enqueue(session, "honcho_ingest", meeting_id=meeting.id)
    J.run_job(session, J.claim(session, "w1", kinds=["honcho_ingest"]))
    assert job.status == "done"
    assert honcho.sessions[meeting.id].messages[0]["created_at"] is None
    assert "ingest time" in (job.log or "")


def test_reingest_recreates_the_session(session, meeting, honcho):
    """Ponowna transkrypcja nie może dołożyć drugiego kompletu wypowiedzi."""
    memory.ingest_transcript(session, meeting.latest_transcript)
    assert honcho.deleted == []
    memory.ingest_transcript(session, meeting.latest_transcript)
    assert honcho.deleted == [meeting.id]
    assert (
        len(honcho.sessions[meeting.id].messages)
        == meeting.latest_transcript.utterance_count
    )


def test_ingest_marks_only_the_transcript_the_session_reflects(
    session, meeting, honcho
):
    old = meeting.latest_transcript
    memory.ingest_transcript(session, old)
    session.commit()
    new = Transcript(
        meeting_id=meeting.id,
        text_path=old.text_path,
        utterance_count=old.utterance_count,
        created_at=old.created_at + dt.timedelta(minutes=5),
    )
    session.add(new)
    session.commit()
    session.refresh(meeting)
    memory.ingest_transcript(session, new)
    session.commit()
    assert new.honcho_synced_at is not None
    assert old.honcho_synced_at is None


# --------------------------------------------------------------------------
# Kolejka
# --------------------------------------------------------------------------


def test_failed_reingest_does_not_leave_the_meeting_marked_as_in_memory(
    session, meeting, honcho
):
    """Delete poszło, create padło: Honcho nie ma już sesji, więc baza też
    nie może twierdzić, że spotkanie jest w pamięci — inaczej backfill je
    pominie na zawsze."""
    from honcho import ServerError

    t = meeting.latest_transcript
    memory.ingest_transcript(session, t)
    session.commit()
    assert t.honcho_synced_at is not None

    honcho.create_error = ServerError("boom")
    with pytest.raises(ServerError):
        memory.ingest_transcript(session, t)
    session.rollback()  # to samo robi `run_job` po wyjątku z taska
    session.expire_all()
    assert meeting.id in honcho.deleted
    assert session.get(Transcript, t.id).honcho_synced_at is None
    assert [x.id for x in memory.transcripts_to_sync(session)] == [t.id]


def test_new_transcript_gets_its_own_ingest_while_the_old_one_runs(
    session, meeting, honcho
):
    """Klucz per spotkanie oddałby biegnący job ze starym transcript_id
    i nowy transkrypt nigdy nie trafiłby do Honcho."""
    old = meeting.latest_transcript
    first = memory.queue_ingest(session, meeting, old.id, priority=50, created_by="t")
    claimed = J.claim(session, "w1")
    assert claimed.id == first.id

    new = Transcript(
        meeting_id=meeting.id,
        text_path=old.text_path,
        created_at=old.created_at + dt.timedelta(minutes=5),
    )
    session.add(new)
    session.commit()
    session.expire_all()
    second = memory.queue_ingest(session, meeting, new.id, priority=50, created_by="t")
    assert second.id != first.id, "nowy transkrypt musi dostać własny job"

    # Stary job widzi, że go wyprzedzono — nie wgrywa przestarzałego tekstu.
    J.run_job(session, claimed)
    assert claimed.result == {"skipped": "superseded", "latest": new.id}
    assert honcho.sessions == {}

    J.run_job(session, J.claim(session, "w1"))
    assert second.status == "done"
    assert len(honcho.sessions[meeting.id].messages) == old.utterance_count


def test_ingest_waits_for_another_running_ingest_of_the_same_meeting(
    session, meeting, honcho
):
    """Dwa ingesty naraz kasowałyby sobie sesję Honcho nawzajem."""
    t = meeting.latest_transcript
    session.add(Job(kind="honcho_ingest", status="running", meeting_id=meeting.id))
    session.commit()
    job = memory.queue_ingest(session, meeting, t.id, priority=50, created_by="t")
    J.run_job(session, J.claim(session, "w1"))
    assert job.status == "queued", "ma wrócić do kolejki, nie paść na twardo"
    # Czekanie nie zużywa próby: długi poprzednik nie może wyczerpać retry
    # i zostawić najnowszego transkryptu w stanie `failed` na zawsze.
    assert job.attempts == 0 and job.error is None
    assert job.step.startswith("waiting:") and "still running" in job.step
    assert job.scheduled_at > dt.datetime.now(dt.timezone.utc)
    assert honcho.sessions == {}


def test_finished_ingest_hands_off_to_a_transcript_that_appeared_meanwhile(
    session, meeting, honcho
):
    """Zamyka wyścig „czekający odłożył się, poprzednik już sprawdził": to
    poprzednik na koniec kolejkuje najnowszy transkrypt."""
    old = meeting.latest_transcript
    job = memory.queue_ingest(session, meeting, old.id, priority=50, created_by="t")
    created: dict[str, int] = {}

    def add_new_transcript_midway():
        if created:
            return
        from webapp.db import SessionLocal

        with SessionLocal() as other:
            t = Transcript(
                meeting_id=meeting.id,
                text_path=old.text_path,
                created_at=old.created_at + dt.timedelta(minutes=5),
            )
            other.add(t)
            other.commit()
            created["id"] = t.id

    honcho.on_add_messages = add_new_transcript_midway
    J.run_job(session, J.claim(session, "w1"))
    assert job.status == "done"
    follow = session.get(Job, job.result["follow_up"])
    assert follow.kind == "honcho_ingest" and follow.args == {
        "transcript_id": created["id"]
    }
    assert follow.status == "queued"


def test_sync_peers_reconciles_membership_with_the_participant_list(
    session, meeting, honcho
):
    """Lista uczestników żyje po ingeście: dopięty adres ma wpuścić człowieka
    do sesji, usunięty uczestnik ma z niej wypaść."""
    memory.ingest_transcript(session, meeting.latest_transcript)
    session.commit()
    sess = honcho.sessions[meeting.id]
    assert memory.sync_peers(session, meeting) == {"changed": False, "peers": 3}
    assert sess.set_peers_calls == 0, "bez różnicy nie ma zapisu"

    # Gość dostaje adres (resolve_identities), Ola wypada z zaproszenia.
    guest = next(p for p in meeting.participants if p.name == "Guest Speaker")
    guest.email = "guest@partner.com"
    guest.key = "guest@partner.com"
    ola = next(p for p in meeting.participants if p.name == "Ola Nowak")
    meeting.participants.remove(ola)
    session.commit()

    out = memory.sync_peers(session, meeting)
    assert out["changed"] and sess.set_peers_calls == 1
    ids = {p.id for p in sess.peers()}
    assert memory.peer_id("guest@partner.com") in ids
    assert memory.peer_id("guest speaker") not in ids, "stary peer z nazwy znika"
    assert memory.peer_id("ola@acme.com") not in ids
    assert memory.peer_id("jan@acme.com") in ids

    # Bez sesji w Honcho (nic nie wgrane) — nic do roboty, zero wywołań.
    fresh = Meeting(
        id="bot-fresh", title="x", transcript_state="ready", status_group="done"
    )
    session.add(fresh)
    session.add(Transcript(meeting_id="bot-fresh", text_path="x"))
    session.commit()
    assert memory.sync_peers(session, fresh) == {"skipped": "not ingested"}


def test_backfill_reconciles_rosters_of_ingested_meetings(session, meeting, honcho):
    memory.ingest_transcript(session, meeting.latest_transcript)
    session.commit()
    meeting.participants.append(
        MeetingParticipant(
            source="calendar", key="new@acme.com", email="new@acme.com", name="New"
        )
    )
    session.commit()
    job = J.enqueue(session, "honcho_backfill")
    J.run_job(session, J.claim(session, "w1", kinds=["honcho_backfill"]))
    assert job.result == {"queued": 0, "reconciled": 1, "force": False}
    assert memory.peer_id("new@acme.com") in {
        p.id for p in honcho.sessions[meeting.id].peers()
    }


def test_identity_resolution_queues_a_backfill(session, meeting, honcho, monkeypatch):
    from webapp import recall_sync

    monkeypatch.setattr(
        recall_sync, "resolve_identities", lambda s: {"matched": 2, "left": 0}
    )
    monkeypatch.setattr(
        recall_sync,
        "repair_participant_keys",
        lambda s: {"rekeyed": 0, "merged": 0, "scanned": 3},
    )
    J.enqueue(session, "repair_participants")
    J.run_job(session, J.claim(session, "w1"))
    backfills = session.query(Job).filter(Job.kind == "honcho_backfill").all()
    assert [b.dedupe_key for b in backfills] == ["honcho_backfill:auto"]

    monkeypatch.setattr(settings, "honcho_enabled", False)
    assert memory.queue_backfill(session, created_by="x") is None


def test_pipeline_success_queues_memory_ingest_only_when_enabled(
    session, meeting, honcho, monkeypatch
):
    monkeypatch.setattr(
        tasks, "_do_pipeline", lambda ctx, m, force_asr=False: {"transcript_id": 7}
    )
    job = J.enqueue(session, "transcribe", meeting_id=meeting.id)
    J.run_job(session, J.claim(session, "w1"))
    queued = session.query(Job).filter(Job.kind == "honcho_ingest").all()
    assert [q.args["transcript_id"] for q in queued] == [7]
    assert job.result["memory_job"] == queued[0].id

    monkeypatch.setattr(settings, "honcho_enabled", False)
    J.enqueue(session, "transcribe", meeting_id=meeting.id)
    J.run_job(session, J.claim(session, "w1"))
    assert session.query(Job).filter(Job.kind == "honcho_ingest").count() == 1


def test_honcho_ingest_task_is_a_noop_when_disabled(
    session, meeting, honcho, monkeypatch
):
    monkeypatch.setattr(settings, "honcho_enabled", False)
    job = J.enqueue(session, "honcho_ingest", meeting_id=meeting.id)
    J.run_job(session, J.claim(session, "w1"))
    assert job.status == "done" and job.result == {"skipped": True}
    assert honcho.sessions == {}


def test_backfill_fans_out_one_ingest_per_unsynced_meeting(session, meeting, honcho):
    synced = Meeting(
        id="bot-done", title="Old", transcript_state="ready", status_group="done"
    )
    session.add(synced)
    session.add(
        Transcript(
            meeting_id="bot-done",
            text_path="x",
            honcho_synced_at=dt.datetime.now(dt.timezone.utc),
        )
    )
    session.add(
        Meeting(
            id="bot-none",
            title="No transcript",
            transcript_state="none",
            status_group="done",
        )
    )
    session.commit()

    job = J.enqueue(session, "honcho_backfill")
    J.run_job(session, J.claim(session, "w1"))
    assert job.result == {"queued": 1, "reconciled": 0, "force": False}
    ingests = session.query(Job).filter(Job.kind == "honcho_ingest").all()
    assert [j.meeting_id for j in ingests] == [meeting.id]

    # Zakolejkowany ingest (priorytet 90) wyprzedza backfill (100) — bierzemy
    # więc jawnie backfill, jak worker z `--kinds`.
    forced = J.enqueue(
        session, "honcho_backfill", args={"force": True}, dedupe_key="force"
    )
    J.run_job(session, J.claim(session, "w1", kinds=["honcho_backfill"]))
    assert forced.result["queued"] == 2


def test_backfill_enqueues_ingests_in_meeting_chronology(session, tmp_path, honcho):
    def add(bot_id: str, when: dt.datetime) -> None:
        session.add(
            Meeting(
                id=bot_id,
                title=bot_id,
                started_at=when,
                transcript_state="ready",
                status_group="done",
            )
        )
        path = tmp_path / f"{bot_id}.txt"
        path.write_text("Jan [00:00:01] hi\n", encoding="utf-8")
        session.add(
            Transcript(
                meeting_id=bot_id,
                text_path=str(path),
                utterance_count=1,
                speakers=[],
            )
        )

    add("bot-june", dt.datetime(2026, 6, 1, 10, 0, tzinfo=dt.timezone.utc))
    add("bot-march", dt.datetime(2026, 3, 1, 10, 0, tzinfo=dt.timezone.utc))
    add("bot-may", dt.datetime(2026, 5, 1, 10, 0, tzinfo=dt.timezone.utc))
    session.commit()

    job = J.enqueue(session, "honcho_backfill")
    J.run_job(session, J.claim(session, "w1", kinds=["honcho_backfill"]))
    assert job.result["queued"] == 3
    ingests = (
        session.query(Job).filter(Job.kind == "honcho_ingest").order_by(Job.id).all()
    )
    assert [j.meeting_id for j in ingests] == ["bot-march", "bot-may", "bot-june"]


def test_status_counts_only_the_latest_transcript_per_meeting(session, meeting, honcho):
    """Po ponownej transkrypcji stary znacznik nie może udawać, że spotkanie
    jest w pamięci — Honcho ma wtedy nieaktualne dane, a /ask chowałby
    przycisk backfillu."""
    old = meeting.latest_transcript
    memory.ingest_transcript(session, old)
    session.commit()
    assert memory.status(session) == {"ready": 1, "synced": 1, "pending": 0}

    session.add(
        Transcript(
            meeting_id=meeting.id,
            text_path=old.text_path,
            created_at=old.created_at + dt.timedelta(minutes=5),
        )
    )
    session.commit()
    session.expire_all()
    assert memory.status(session) == {"ready": 1, "synced": 0, "pending": 0}
    assert [t.id for t in memory.transcripts_to_sync(session)] == [
        meeting.latest_transcript.id
    ]


def test_compose_points_containers_at_honcho_api():
    """`${HONCHO_URL}` brałby adres z .env (localhost:HONCHO_HOST_PORT), który
    w kontenerze wskazuje sam kontener Earwitness, nie Honcho."""
    import yaml

    compose = yaml.safe_load(
        (
            pathlib.Path(__file__).resolve().parent.parent / "docker-compose.yml"
        ).read_text()
    )
    for name in ("web", "worker"):
        assert (
            compose["services"][name]["environment"]["HONCHO_URL"]
            == "http://honcho-api:8000"
        )


def test_add_missing_columns_survives_a_concurrent_alter(session, monkeypatch):
    """Web i worker startują razem: oba widzą brak kolumny, drugi ALTER
    dostaje „duplicate column". To nie powód, żeby proces padł."""
    from webapp import db as db_module

    class StaleInspector:
        """Widzi bazę sprzed cudzego ALTER-a: bez `honcho_synced_at`."""

        def __init__(self, real):
            self._real = real

        def has_table(self, name):
            return self._real.has_table(name)

        def get_columns(self, name, **kw):
            return [
                c
                for c in self._real.get_columns(name, **kw)
                if c["name"] != "honcho_synced_at"
            ]

    real_inspect = db_module.inspect
    calls = {"n": 0}

    def inspect_once_stale(bind):
        calls["n"] += 1
        insp = real_inspect(bind)
        return StaleInspector(insp) if calls["n"] == 1 else insp

    monkeypatch.setattr(db_module, "inspect", inspect_once_stale)
    assert add_missing_columns() == 0
    assert calls["n"] == 2, "po błędzie ALTER sprawdzamy stan jeszcze raz"
    assert "honcho_synced_at" in {
        c["name"] for c in inspect(engine).get_columns("transcripts")
    }


def test_honcho_url_default_follows_the_compose_host_port(monkeypatch):
    from webapp.config import _honcho_url_default

    monkeypatch.delenv("HONCHO_HOST_PORT", raising=False)
    assert _honcho_url_default() == "http://localhost:8100"
    monkeypatch.setenv("HONCHO_HOST_PORT", "9123")
    assert _honcho_url_default() == "http://localhost:9123"


def test_add_missing_columns_upgrades_an_old_schema(session):
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE transcripts DROP COLUMN honcho_synced_at"))
    assert "honcho_synced_at" not in {
        c["name"] for c in inspect(engine).get_columns("transcripts")
    }
    assert add_missing_columns() == 1
    assert "honcho_synced_at" in {
        c["name"] for c in inspect(engine).get_columns("transcripts")
    }
    assert add_missing_columns() == 0


# --------------------------------------------------------------------------
# Widoki
# --------------------------------------------------------------------------


def test_memory_surface_is_hidden_when_disabled(client, session, meeting, monkeypatch):
    monkeypatch.setattr(settings, "honcho_enabled", False)
    assert client.get("/ask", headers=HTML).status_code == 404
    assert client.post("/memory/backfill", headers=HTML).status_code == 404
    r = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert 'href="/ask"' not in r.text
    assert "/ask" not in r.text.split("</header>")[0]
    assert "in memory" not in r.text


def test_ask_page_asks_as_the_logged_in_user(
    client, session, meeting, honcho, monkeypatch
):
    monkeypatch.setattr(
        memory, "status", lambda db: {"ready": 1, "synced": 1, "pending": 0}
    )
    r = client.get("/ask", headers=HTML)
    assert r.status_code == 200 and 'href="/ask"' in r.text

    r = client.post("/ask", data={"question": "What did we decide?"}, headers=HTML)
    assert r.status_code == 200
    assert "We agreed to ship on Friday." in r.text
    from webapp.auth import dev_user

    assert honcho.chats == [
        {
            "peer": memory.user_peer_id(dev_user(session)),
            "query": "What did we decide?",
            "session": None,
            "level": settings.honcho_reasoning_level,
        }
    ]


def test_ask_page_empty_memory_offers_backfill(client, session, meeting, honcho):
    r = client.get("/ask", headers=HTML)
    assert 'class="art"' in r.text
    assert 'action="/memory/backfill"' in r.text
    r = client.post("/memory/backfill", headers=HTML, follow_redirects=False)
    assert r.status_code == 303
    assert session.query(Job).filter(Job.kind == "honcho_backfill").count() == 1


def test_meeting_ask_panel_is_for_attendees_only(
    client, session, meeting, honcho, monkeypatch
):
    # Konto deweloperskie (dev@localhost) nie było na spotkaniu.
    r = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert f'action="/meetings/{meeting.id}/ask"' not in r.text
    assert "Ask is available to attendees" in r.text
    assert f'action="/meetings/{meeting.id}/memory"' in r.text, (
        "ręczny ingest ma być dostępny"
    )
    r = client.post(f"/meetings/{meeting.id}/ask", data={"question": "x"}, headers=HTML)
    assert r.status_code == 403

    # Teraz jest na liście — panel się pojawia, a pytanie idzie w zakresie sesji.
    meeting.participants.append(
        MeetingParticipant(
            source="calendar", key="dev@localhost", email="dev@localhost", name="Dev"
        )
    )
    session.commit()
    r = client.get(f"/meetings/{meeting.id}", headers=HTML)
    assert f'action="/meetings/{meeting.id}/ask"' in r.text
    memory.ingest_transcript(session, meeting.latest_transcript)
    session.commit()
    r = client.post(
        f"/meetings/{meeting.id}/ask", data={"question": "Who owns it?"}, headers=HTML
    )
    assert r.status_code == 200 and "We agreed to ship on Friday." in r.text
    assert honcho.chats[-1]["session"] == meeting.id
    # Zalogowany dołączył do listy po ingeście — przed pytaniem sesja ma go
    # już jako członka, inaczej „ask-as-self" nie ma na czym pracować.
    ids = {p.id for p in honcho.sessions[meeting.id].peers()}
    assert memory.peer_id("dev@localhost") in ids


def test_ask_errors_are_shown_not_raised(client, session, meeting, honcho, monkeypatch):
    from honcho import ServerError

    monkeypatch.setattr(
        memory, "status", lambda db: {"ready": 1, "synced": 1, "pending": 0}
    )
    honcho.chat_error = ServerError("boom")
    r = client.post("/ask", data={"question": "?"}, headers=HTML)
    assert r.status_code == 200
    assert "Honcho did not answer" in r.text

    honcho.chat_error = None
    honcho.chat_answer = None
    r = client.post("/ask", data={"question": "?"}, headers=HTML)
    assert "Nothing in memory answers that yet." in r.text


def test_manual_ingest_button_queues_a_job(client, session, meeting, honcho):
    r = client.post(
        f"/meetings/{meeting.id}/memory", headers=HTML, follow_redirects=False
    )
    assert r.status_code == 303
    job = session.query(Job).filter(Job.kind == "honcho_ingest").one()
    assert job.args == {"transcript_id": meeting.latest_transcript.id}
