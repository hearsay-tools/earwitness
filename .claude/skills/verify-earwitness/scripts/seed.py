"""Seed the isolated verification database with synthetic meetings.

Run from the repo root AFTER `source scripts/env.sh` (the DATABASE_URL,
RECALL_DIR and TRANSCRIPTS_DIR exports decide which database this touches):

    uv run python .claude/skills/verify-earwitness/scripts/seed.py transcript
    uv run python .claude/skills/verify-earwitness/scripts/seed.py recording
    uv run python .claude/skills/verify-earwitness/scripts/seed.py expired-disk
    uv run python .claude/skills/verify-earwitness/scripts/seed.py enqueue <kind> [meeting_id]
    uv run python .claude/skills/verify-earwitness/scripts/seed.py dump

Scenarios mirror the fixtures in tests/test_views.py and
tests/test_local_assets.py, so the seeded shapes are the ones the product
already treats as valid. Every scenario prints one JSON object with the ids
it created. All data is fictional: no real names, bots or recordings.

Refuses to run when DATABASE_URL does not point at an output/verify/<run> db.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

# `python path/to/seed.py` puts THIS directory first on sys.path, not the
# repo root, so `webapp` would not import. The skill lives four levels deep:
# .claude/skills/verify-earwitness/scripts/seed.py
sys.path.insert(
    0, os.environ.get("VERIFY_REPO_ROOT") or str(Path(__file__).resolve().parents[4])
)


def _db_inside_run_state() -> bool:
    """True only when DATABASE_URL is a SQLite file inside this run's state dir.

    A substring test on the URL is not enough: `output/verify/../webapp.db`
    contains the marker and resolves to the shared dev database.
    """
    url = os.environ.get("DATABASE_URL", "")
    state = os.environ.get("VERIFY_STATE", "")
    if not state or not url.startswith("sqlite:///"):
        return False
    db = Path(url[len("sqlite:///") :]).resolve()
    return db.is_relative_to(Path(state).resolve())


if not _db_inside_run_state():
    sys.exit(
        "refusing: DATABASE_URL does not resolve inside VERIFY_STATE "
        "(output/verify/<run>); source env.sh first"
    )

import webapp.tasks  # noqa: E402,F401  (registers job kinds for enqueue)
from webapp.config import settings  # noqa: E402
from webapp.db import init_db, session_scope  # noqa: E402
from webapp.jobs import enqueue  # noqa: E402
from webapp.models import Job, Meeting, MeetingParticipant, Transcript  # noqa: E402

WHEN = dt.datetime(2026, 8, 1, 10, 0, tzinfo=dt.timezone.utc)
TRANSCRIPT_TEXT = (
    "Ala Testowa [00:00:03] Cześć, dzięki że jesteś.\n"
    "Bob Example [00:00:05] Nie ma sprawy, lecimy z agendą.\n"
    "Ala Testowa [00:00:09] Punkt pierwszy: weryfikacja end-to-end.\n"
)


def scenario_transcript(s) -> dict:
    """Finished meeting with a ready transcript on disk (user_status=ready)."""
    mid = "bot-verify-transcript"
    m = Meeting(
        id=mid,
        title="Verification Sync",
        platform="google_meet",
        started_at=WHEN,
        completed_at=WHEN + dt.timedelta(seconds=1800),
        duration_seconds=1800,
        status_code="done",
        status_group="done",
        recording_id="rec-verify-1",
        asset_state="ready",
        transcript_state="ready",
    )
    s.merge(m)
    s.flush()
    # Re-drives in one run must not trip the (meeting_id, source, key) unique
    # constraint or pile up transcripts: replace this meeting's fixtures.
    s.query(MeetingParticipant).filter_by(meeting_id=mid).delete()
    s.query(Transcript).filter_by(meeting_id=mid).delete()
    s.flush()
    for name, email, secs in (
        ("Ala Testowa", "ala@example.com", 12.0),
        ("Bob Example", "bob@example.com", 6.0),
    ):
        s.add(
            MeetingParticipant(
                meeting_id=mid,
                source="recall",
                key=email,
                name=name,
                email=email,
                speaking_seconds=secs,
            )
        )
    path = settings.transcripts_dir / mid / "transcript.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TRANSCRIPT_TEXT, encoding="utf-8")
    t = Transcript(
        meeting_id=mid,
        recording_id="rec-verify-1",
        engine="pipeline-recall",
        language="pl",
        text_path=f"{mid}/transcript.txt",
        speakers=[
            {"name": "Ala Testowa", "seconds": 12},
            {"name": "Bob Example", "seconds": 6},
        ],
        utterance_count=3,
        word_count=17,
        duration_seconds=1800,
    )
    s.add(t)
    s.flush()
    return {"meeting_id": mid, "transcript_id": t.id, "text_path": str(path)}


def scenario_recording(s) -> dict:
    """Finished meeting, no transcript, mixed mp3 on disk (user_status=to_process)."""
    mid = "bot-verify-recording"
    rec_dir = settings.recall_dir / mid / "rec-verify-2"
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / "audio_mixed.mp3").write_bytes(b"ID3fake-mp3-bytes-for-verification")
    s.merge(
        Meeting(
            id=mid,
            title="Recording Only",
            platform="google_meet",
            started_at=WHEN + dt.timedelta(days=1),
            duration_seconds=600,
            status_code="done",
            status_group="done",
            recording_id="rec-verify-2",
            asset_state="ready",
            asset_dir=str(rec_dir),
            transcript_state="none",
        )
    )
    return {"meeting_id": mid, "asset_dir": str(rec_dir)}


def scenario_expired_disk(s) -> dict:
    """Recall forgot the recording (recording_id NULL) but the full asset set
    is on disk under RECALL_DIR. `repair_assets` must adopt it."""
    mid, rid = "bot-verify-expired", "rec-verify-3"
    rec_dir = settings.recall_dir / mid / rid
    sep = rec_dir / "audio_separate" / "1-Ala"
    sep.mkdir(parents=True, exist_ok=True)
    (rec_dir / "recording.json").write_text(
        json.dumps(
            {
                "recording_id": rid,
                "bot_id": mid,
                "expires_at": "2026-07-24T10:25:17Z",
                "started_at": "2026-07-10T09:28:13Z",
                "completed_at": "2026-07-10T10:25:17Z",
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
            [{"name": "Ala Testowa", "email": "ala@example.com", "is_host": True}]
        ),
        encoding="utf-8",
    )
    s.merge(
        Meeting(
            id=mid,
            title="Expired In Recall",
            platform="google_meet",
            started_at=dt.datetime(2026, 7, 10, 9, 28, tzinfo=dt.timezone.utc),
            duration_seconds=3420,
            status_code="media_expired",
            status_group="expired",
            recording_id=None,
            asset_state="expired",
            transcript_state="none",
        )
    )
    return {"meeting_id": mid, "recording_id_on_disk": rid, "rec_dir": str(rec_dir)}


def scenario_enqueue(s, kind: str, meeting_id: str | None) -> dict:
    """Queue a job exactly as the app does (same jobs.enqueue contract)."""
    job = enqueue(s, kind, meeting_id=meeting_id, created_by="verify-earwitness")
    return {"job_id": job.id, "kind": job.kind, "status": job.status}


def scenario_dump(s) -> dict:
    """Read-only snapshot of stored state, for evidence of side effects."""
    return {
        "meetings": [
            {
                "id": m.id,
                "title": m.title,
                "recording_id": m.recording_id,
                "asset_state": m.asset_state,
                "asset_dir": m.asset_dir,
                "transcript_state": m.transcript_state,
                "user_status": m.user_status,
                "participants": [p.display for p in m.participants],
            }
            for m in s.query(Meeting).order_by(Meeting.id)
        ],
        "jobs": [
            {
                "id": j.id,
                "kind": j.kind,
                "status": j.status,
                "meeting_id": j.meeting_id,
                "progress": j.progress,
                "error": j.error,
                "result": j.result,
            }
            for j in s.query(Job).order_by(Job.id)
        ],
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    init_db()
    name, rest = argv[0], argv[1:]
    with session_scope() as s:
        if name == "transcript":
            out = scenario_transcript(s)
        elif name == "recording":
            out = scenario_recording(s)
        elif name == "expired-disk":
            out = scenario_expired_disk(s)
        elif name == "enqueue":
            out = scenario_enqueue(s, rest[0], rest[1] if len(rest) > 1 else None)
        elif name == "dump":
            out = scenario_dump(s)
        else:
            sys.exit(f"unknown scenario {name!r}")
        s.commit()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
