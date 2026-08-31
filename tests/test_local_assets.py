"""Testy odzyskiwania audio, o którym baza zapomniała.

Recall po TTL przestaje zwracać `recordings` w payloadzie bota. Spotkanie
zostaje wtedy bez `recording_id`, choć komplet audio ściągnęliśmy przed
wygaśnięciem i leży na dysku. To jest ten scenariusz — realnie dotknął
109 spotkań, więc ma zostać przykryty testem.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient
from webapp.app import app
from webapp.config import settings
from webapp.models import Meeting, MeetingParticipant
from webapp.recall_sync import (
    adopt_local_recordings,
    local_asset_state,
    meetings_ready_to_process,
    upsert_bot,
)

HTML = {"accept": "text/html,application/xhtml+xml"}

BOT_ID = "bot-expired"
REC_ID = "rec-on-disk"


def make_recording_dir(
    bot_id: str = BOT_ID, rec_id: str = REC_ID, with_email: bool = True
) -> None:
    """Minimalny, ale kompletny zestaw assetów: mixed mp3 + separate raw.

    `with_email=False` odwzorowuje typowy `participants.json`: Recall podaje
    nazwę wyświetlaną, a adresu nie zna.
    """
    rec_dir = settings.recall_dir / bot_id / rec_id
    sep = rec_dir / "audio_separate" / "1-Ala"
    sep.mkdir(parents=True, exist_ok=True)
    (rec_dir / "recording.json").write_text(
        json.dumps(
            {
                "recording_id": rec_id,
                "bot_id": bot_id,
                "expires_at": "2026-07-24T10:25:17.628636Z",
                "started_at": "2026-07-10T09:28:13.149516Z",
                "completed_at": "2026-07-10T10:25:17.628636Z",
                "audio_mixed": [{"id": "aaaaaaaa-1111", "format": "mp3"}],
                "audio_separate": [
                    {"id": "bbbbbbbb-2222", "format": "raw", "parts": 1}
                ],
            }
        ),
        encoding="utf-8",
    )
    (rec_dir / "audio_mixed.mp3").write_bytes(b"fake")
    (rec_dir / "audio_separate" / "parts_bbbbbbbb.json").write_text(
        "{}", encoding="utf-8"
    )
    (sep / "part-1.raw").write_bytes(b"fake")
    (rec_dir / "participants.json").write_text(
        json.dumps(
            [
                {
                    "name": "Ala Kowalska",
                    "email": "akowalska@example.com" if with_email else None,
                    "is_host": True,
                },
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture()
def disk(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "recall_dir", tmp_path / "recall")
    (tmp_path / "recall").mkdir()
    return tmp_path / "recall"


def expired_bot_payload(bot_id: str = BOT_ID) -> dict:
    """To, co Recall zwraca po wygaśnięciu mediów — bez `recordings`."""
    return {
        "id": bot_id,
        "meeting_url": {"platform": "google_meet", "meeting_id": "abc-defg-hij"},
        "join_at": "2026-07-10T09:25:00Z",
        "recordings": [],
        "status_changes": [
            {"code": "done", "created_at": "2026-07-10T10:26:00Z"},
            {"code": "media_expired", "created_at": "2026-07-24T10:30:00Z"},
        ],
    }


# --------------------------------------------------------------------------
# Wykrywanie na dysku
# --------------------------------------------------------------------------


def test_finds_recording_when_db_has_no_id(disk):
    make_recording_dir()
    state, path, found = local_asset_state(BOT_ID, None)
    assert state == "ready"
    assert found == REC_ID
    assert path.endswith(f"{BOT_ID}/{REC_ID}")


def test_incomplete_dir_is_not_ready(disk):
    rec_dir = disk / BOT_ID / REC_ID
    rec_dir.mkdir(parents=True)
    (rec_dir / "recording.json").write_text('{"audio_separate": []}', encoding="utf-8")
    state, _, _ = local_asset_state(BOT_ID, REC_ID)
    assert state == "none", "sam recording.json bez audio nie może udawać kompletu"


def test_bot_dir_with_only_bot_json_stays_none(disk):
    bot_dir = disk / BOT_ID
    bot_dir.mkdir()
    (bot_dir / "bot.json").write_text("{}", encoding="utf-8")
    assert local_asset_state(BOT_ID, None) == ("none", None, None)


# --------------------------------------------------------------------------
# Sync i backfill
# --------------------------------------------------------------------------


def test_sync_adopts_recording_from_disk(session, disk):
    make_recording_dir()
    meeting = upsert_bot(session, expired_bot_payload(), fetch_participants=False)
    session.commit()

    assert meeting.asset_state == "ready", "audio na dysku ma wygrywać z TTL"
    assert meeting.recording_id == REC_ID
    assert meeting.started_at == dt.datetime(
        2026, 7, 10, 9, 28, 13, 149516, tzinfo=dt.timezone.utc
    )
    assert meeting.duration_seconds == pytest.approx(3424.479, abs=0.01)
    assert meeting.has_audio_mixed and meeting.has_audio_separate


def test_backfill_adopts_and_loads_participants(session, disk):
    session.add(
        Meeting(
            id=BOT_ID,
            status_code="media_expired",
            status_group="expired",
            asset_state="expired",
        )
    )
    session.commit()
    make_recording_dir()

    stat = adopt_local_recordings(session)
    assert stat["adopted"] == 1
    assert stat["participants"] == 1

    m = session.get(Meeting, BOT_ID)
    assert m.asset_state == "ready"
    assert m.recording_id == REC_ID
    assert m.asset_error is None
    assert [p.email for p in m.participants] == ["akowalska@example.com"]


def test_backfill_does_not_duplicate_people_from_calendar(session, disk):
    """Nazwa z nagrania i adres z zaproszenia to jedna osoba, nie dwie.

    `participants.json` podaje nazwę bez adresu, więc bez dopasowania
    tożsamości backfill sypie drugim kluczem dla kogoś, kto już jest
    w bazie z kalendarza — i człowiek dubluje się w filtrach.
    """
    m = Meeting(
        id=BOT_ID,
        status_code="media_expired",
        status_group="expired",
        asset_state="expired",
    )
    m.participants.append(
        MeetingParticipant(
            source="calendar",
            key="akowalska@example.com",
            email="akowalska@example.com",
        )
    )
    session.add(m)
    session.commit()
    make_recording_dir(with_email=False)  # z nagrania sama nazwa

    adopt_local_recordings(session)

    m = session.get(Meeting, BOT_ID)
    assert {p.key for p in m.participants} == {"akowalska@example.com"}, (
        "nazwa z nagrania musi trafić na klucz adresowy, nie założyć drugiego"
    )
    assert [p.display for p in m.human_participants] == ["Ala Kowalska"]


def test_backfill_leaves_meetings_without_audio_alone(session, disk):
    session.add(Meeting(id="bot-nic", status_group="expired", asset_state="expired"))
    session.commit()

    assert adopt_local_recordings(session)["adopted"] == 0
    assert session.get(Meeting, "bot-nic").asset_state == "expired"


def test_sync_resolves_identities_when_people_appeared(session, monkeypatch):
    """Sync, który dołożył ludzi, musi ich od razu skleić z adresami.

    Inaczej duplikaty wracają po każdej synchronizacji, a sprząta je dopiero
    ręczne odpalenie `repair_participants`.
    """
    from webapp import jobs as J
    from webapp import tasks as T

    calls: list[str] = []
    monkeypatch.setattr(settings, "recall_api_key", "x")
    monkeypatch.setattr(
        T, "sync_bots", lambda *a, **k: {"seen": 1, "created": 1, "updated": 0}
    )
    monkeypatch.setattr(
        "webapp.recall_sync.resolve_identities",
        lambda s, m=None: calls.append("resolve") or {"matched": 0, "left": 0},
    )

    job = J.enqueue(session, "sync_recall", dedupe_key="t")
    result = J.get_task("sync_recall")(J.JobContext(session, job))

    assert calls == ["resolve"]
    assert "identities" in result


# --------------------------------------------------------------------------
# Konsekwencje w kolejce i w UI
# --------------------------------------------------------------------------


def test_expired_bot_with_local_audio_is_processable(session, disk):
    make_recording_dir()
    upsert_bot(session, expired_bot_payload(), fetch_participants=False)
    session.commit()

    ready = meetings_ready_to_process(session)
    assert [m.id for m in ready] == [BOT_ID]


def test_expired_bot_without_local_audio_is_not_processable(session, disk):
    upsert_bot(session, expired_bot_payload(), fetch_participants=False)
    session.commit()
    assert meetings_ready_to_process(session) == []


def test_detail_does_not_claim_recording_is_gone(session, disk):
    make_recording_dir()
    upsert_bot(session, expired_bot_payload(), fetch_participants=False)
    session.commit()

    r = TestClient(app).get(f"/meetings/{BOT_ID}", headers=HTML)
    assert r.status_code == 200
    assert "Recall deleted the recording" not in r.text
    assert "Get transcript" in r.text, "skoro audio jest, ma być czym je puścić"


# --------------------------------------------------------------------------
# Ponowienie po nieudanym pobraniu
# --------------------------------------------------------------------------


def test_retry_after_failed_fetch_redownloads(session, disk, monkeypatch):
    """Po porażce na dysku zostaje ogryzek pliku, a `download_bot_assets`
    domyślnie pomija to, co już istnieje — bez wymuszenia retry wracałby
    z dokładnie tym samym błędem, w kółko."""
    from webapp import jobs as J
    from webapp import tasks

    session.add(
        Meeting(
            id=BOT_ID,
            recording_id=REC_ID,
            asset_state="failed",
            asset_error="połowa pliku",
        )
    )
    session.commit()

    seen = {}

    def fake_download(client, bot_id, out_dir, force=False):
        seen["force"] = force
        make_recording_dir()
        return {"bytes": 2048, "errors": []}

    monkeypatch.setattr(tasks, "download_bot_assets", fake_download)
    monkeypatch.setattr(tasks, "make_client", lambda: None)

    job = J.enqueue(session, "fetch_assets", meeting_id=BOT_ID)
    tasks.fetch_assets(J.JobContext(session, job))

    assert seen["force"] is True
    assert session.get(Meeting, BOT_ID).asset_state == "ready"


def test_complete_assets_are_not_refetched(session, disk, monkeypatch):
    """Druga strona tej samej monety: komplet na dysku nie jedzie do Recalla."""
    from webapp import jobs as J
    from webapp import tasks

    make_recording_dir()
    session.add(Meeting(id=BOT_ID, recording_id=REC_ID, asset_state="ready"))
    session.commit()

    def boom(*a, **kw):
        raise AssertionError("komplet na dysku nie ma powodu jechać do Recalla")

    monkeypatch.setattr(tasks, "download_bot_assets", boom)

    job = J.enqueue(session, "fetch_assets", meeting_id=BOT_ID)
    result = tasks.fetch_assets(J.JobContext(session, job))
    assert result["skipped"] is True
