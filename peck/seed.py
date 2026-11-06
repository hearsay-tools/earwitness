"""Load the Given tables of a Peck spec into a fresh Earwitness database.

    PECK_DATA=/tmp/run1 uv run python peck/seed.py peck/spec/<feature>.md
    PECK_DATA=/tmp/run1 uv run python peck/seed.py --cookie "Marta Lis"

The first form parses the spec's `## Given` tables and writes them through the
app's own models. The second prints the value of the `session` cookie that
signs that person in (see peck/ADAPTER.md). Run both with the same
environment as the server (peck/env.sh), so DATABASE_URL, RECALL_DIR,
TRANSCRIPTS_DIR and SECRET_KEY match.

Supported tables (headings are matched case-insensitively):

- People:   Name | Role | Email
- Meetings: Meeting | Date | Start | Minutes | Platform | Status | Participants
- Jobs:     Meeting | Job | Status | Started by
- Webhook:  URL | Method | Bearer token
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if not os.environ.get("PECK_DATA"):
    sys.exit("PECK_DATA is not set: source peck/env.sh first")

import webapp.tasks  # noqa: E402,F401  (registers job kinds)
from webapp import webhook  # noqa: E402
from webapp.config import settings  # noqa: E402
from webapp.db import init_db, session_scope  # noqa: E402
from webapp.labels import JOB_KINDS, JOB_STATUSES, PLATFORMS  # noqa: E402
from webapp.models import (  # noqa: E402
    AppSetting,
    Job,
    Meeting,
    MeetingParticipant,
    Transcript,
    User,
)
from webapp.recall_sync import rebuild_search_blob  # noqa: E402


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def meeting_id(title: str) -> str:
    return f"bot-{slug(title)}"


def parse_tables(md: str) -> dict[str, list[dict[str, str]]]:
    """Every `### Name` table under `## Given` -> list of rows keyed by column."""
    given = md.split("\n## Given", 1)[1].split("\n## ", 1)[0]
    tables: dict[str, list[dict[str, str]]] = {}
    name, keys = None, None
    for line in given.splitlines():
        line = line.strip()
        if line.startswith("### "):
            name, keys = line[4:].strip().lower(), None
            tables[name] = []
            continue
        if not (name and line.startswith("|")):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-+:?", c) for c in cells):
            continue
        if keys is None:
            keys = [c.lower().replace(" ", "_") for c in cells]
            continue
        tables[name].append(dict(zip(keys, cells)))
    return tables


STATUS_FIELDS = {
    # visible status -> (status_group, status_code, asset_state, transcript_state, has recording)
    "ready": ("done", "done", "ready", "ready", True),
    "to process": ("done", "done", "ready", "none", True),
    "no recording": ("done", "done", "none", "none", False),
    "failed": ("failed", "fatal", "none", "none", False),
    "upcoming": ("scheduled", "scheduled", "none", "none", False),
}


def seed_people(s, rows) -> dict[str, str]:
    emails = {}
    for r in rows:
        email = r["email"].lower()
        emails[r["name"]] = email
        s.add(
            User(
                google_sub=f"peck-{slug(email)}",
                email=email,
                name=r["name"],
                domain=email.rsplit("@", 1)[-1],
            )
        )
    return emails


def seed_meetings(s, rows, emails: dict[str, str]) -> None:
    for r in rows:
        title = r["meeting"]
        mid = meeting_id(title)
        group, code, asset, transcript, has_rec = STATUS_FIELDS[r["status"].lower()]
        start = dt.datetime.fromisoformat(f"{r['date']}T{r['start']}").replace(
            tzinfo=dt.timezone.utc
        )
        minutes = float(r.get("minutes") or 30)
        platform = {v: k for k, v in PLATFORMS.items()}.get(
            r.get("platform", "Google Meet"), "google_meet"
        )
        rec_id = f"rec-{slug(title)}" if has_rec else None
        asset_dir = None
        if has_rec:
            rec_dir = settings.recall_dir / mid / rec_id
            rec_dir.mkdir(parents=True, exist_ok=True)
            (rec_dir / "audio_mixed.mp3").write_bytes(b"ID3peck-fake-audio")
            asset_dir = str(rec_dir)
        scheduled = group == "scheduled"
        m = Meeting(
            id=mid,
            title=title,
            title_source="calendar",
            platform=platform,
            join_at=start,
            started_at=None if scheduled else start,
            completed_at=None if scheduled else start + dt.timedelta(minutes=minutes),
            duration_seconds=None if scheduled else minutes * 60,
            status_code=code,
            status_group=group,
            recording_id=rec_id,
            has_audio_mixed=has_rec,
            asset_state=asset,
            asset_dir=asset_dir,
            transcript_state=transcript,
        )
        s.add(m)
        s.flush()
        names = [
            n.strip() for n in (r.get("participants") or "").split(",") if n.strip()
        ]
        for name in names:
            email = emails.get(name) or f"{slug(name).replace('-', '.')}@example.com"
            s.add(
                MeetingParticipant(
                    meeting_id=mid,
                    source="recall",
                    key=email,
                    name=name,
                    email=email,
                    speaking_seconds=60.0,
                )
            )
        s.flush()
        s.refresh(m)
        rebuild_search_blob(m)
        if transcript == "ready":
            path = settings.transcripts_dir / mid / "transcript.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            who = names or ["Speaker 1"]
            lines = [
                f"{who[i % len(who)]} [00:00:{5 * i + 2:02d}] Point {i + 1} of {title}."
                for i in range(3)
            ]
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            s.add(
                Transcript(
                    meeting_id=mid,
                    recording_id=rec_id,
                    engine="pipeline-recall",
                    language="en",
                    text_path=f"{mid}/transcript.txt",
                    speakers=[{"name": n, "seconds": 60} for n in who],
                    utterance_count=3,
                    word_count=len(" ".join(lines).split()),
                    duration_seconds=minutes * 60,
                )
            )


def seed_jobs(s, rows, emails: dict[str, str]) -> None:
    kinds = {v.lower(): k for k, v in JOB_KINDS.items()}
    statuses = {v.lower(): k for k, v in JOB_STATUSES.items()}
    base = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc)
    for i, r in enumerate(rows):
        status = statuses[r["status"].lower()]
        mid = meeting_id(r["meeting"])
        kind = kinds[r["job"].lower()]
        by = r.get("started_by") or ""
        final = status in ("done", "failed", "canceled")
        created = base + dt.timedelta(minutes=i)
        s.add(
            Job(
                kind=kind,
                status=status,
                meeting_id=mid,
                dedupe_key=f"{kind}:{mid}",
                args={},
                priority=20,
                created_by=emails.get(by, by) or None,
                attempts=3 if status == "failed" else (1 if final else 0),
                max_attempts=3,
                progress=100 if status == "done" else 0,
                error="Recording is not available any more"
                if status == "failed"
                else None,
                scheduled_at=created,
                created_at=created,
                started_at=created if final else None,
                finished_at=created + dt.timedelta(seconds=40) if final else None,
            )
        )


def seed_webhook(s, rows) -> None:
    for r in rows:
        values = {
            webhook.URL_KEY: r.get("url", ""),
            webhook.METHOD_KEY: r.get("method", "POST"),
            webhook.TOKEN_KEY: r.get("bearer_token", ""),
        }
        for key, value in values.items():
            s.merge(AppSetting(key=key, value=value))


def seed(spec: Path) -> None:
    tables = parse_tables(spec.read_text(encoding="utf-8"))
    init_db()
    with session_scope() as s:
        emails = seed_people(s, tables.get("people", []))
        s.flush()
        seed_meetings(s, tables.get("meetings", []), emails)
        s.flush()
        seed_jobs(s, tables.get("jobs", []), emails)
        seed_webhook(s, tables.get("webhook", []))
    print(json.dumps({k: len(v) for k, v in tables.items()}))


def cookie(name: str) -> None:
    """Value of Starlette's signed `session` cookie with this person's user_id."""
    from itsdangerous import TimestampSigner

    init_db()
    with session_scope() as s:
        user = s.query(User).filter(User.name == name).one()
        uid = user.id
    payload = base64.b64encode(json.dumps({"user_id": uid}).encode("utf-8"))
    print(TimestampSigner(str(settings.secret_key)).sign(payload).decode("utf-8"))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--cookie":
        cookie(sys.argv[2])
    elif len(sys.argv) == 2:
        seed(Path(sys.argv[1]))
    else:
        sys.exit(__doc__)
