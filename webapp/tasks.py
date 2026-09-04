"""Zadania wykonywane przez workera.

Trzy poziomy:
- `sync_recall` / `sync_calendar` — odświeżenie metadanych,
- `fetch_assets` — ściągnięcie audio z Recall (TTL 24h, więc pilne),
- `transcribe` — pipeline-recall (ElevenLabs + diaryzacja energią),
- `process` — fetch + transcribe w jednym zadaniu (to, co klika użytkownik).

`process` celowo nie jest łańcuchem dwóch jobów: dzięki temu jedno kliknięcie
= jeden wiersz w kolejce, jeden log i jeden retry, a fetch nigdy nie zostaje
osierocony bez pipeline'u.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import re
import threading
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import select
from transcripts.energy_diarization import diarize_by_energy
from transcripts.energy_diarization import format_transcript as format_energy_transcript
from transcripts.recall_client import download_bot_assets
from transcripts.transcribe import transcribe as elevenlabs_transcribe

from webapp.app_settings import get_autoprocess
from webapp.config import settings
from webapp.jobs import JobContext, enqueue, task
from webapp.models import Meeting, Transcript, User, utcnow
from webapp.recall_sync import (
    adopt_disk_recording,
    load_participants_from_disk,
    local_asset_state,
    make_client,
    rebuild_search_blob,
    sync_bots,
)

log = logging.getLogger("webapp.tasks")


class _CtxStream(io.TextIOBase):
    """Przekierowuje stderr bibliotek (recall_client / energy_diarization),
    które raportują postęp printem, do logu joba — linia po linii."""

    def __init__(self, ctx: JobContext, throttle: float = 1.0) -> None:
        self.ctx = ctx
        self._buf = ""
        self._lock = threading.Lock()
        self._last_flush = 0.0
        self._throttle = throttle

    def write(self, s: str) -> int:  # noqa: D102
        with self._lock:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if line:
                    self.ctx.log(line)
            now = dt.datetime.now().timestamp()
            if now - self._last_flush > self._throttle:
                self._last_flush = now
                self.ctx.job.heartbeat_at = utcnow()
                self.ctx.session.commit()
        return len(s)

    def flush(self) -> None:
        with self._lock:
            if self._buf.strip():
                self.ctx.log(self._buf.strip())
                self._buf = ""
            self.ctx.session.commit()


def _meeting(ctx: JobContext) -> Meeting:
    mid = ctx.meeting_id or ctx.args.get("meeting_id")
    if not mid:
        raise ValueError("job without meeting_id")
    meeting = ctx.session.get(Meeting, mid)
    if meeting is None:
        raise ValueError(f"unknown meeting {mid}")
    return meeting


# --------------------------------------------------------------------------
# Pobieranie assetów
# --------------------------------------------------------------------------


def _force_asr(ctx: JobContext) -> bool:
    """Czy pominąć cache `raw.json` i zapłacić za ASR jeszcze raz.

    `force` (stare zadania w kolejce, CLI, `job_retry` klonujący argumenty)
    nadal znaczy „wszystko od nowa”, więc pociąga też ASR.
    """
    return bool(ctx.args.get("force_asr") or ctx.args.get("force"))


def _do_fetch(ctx: JobContext, meeting: Meeting, force: bool = False) -> dict[str, Any]:
    # Po nieudanym pobraniu na dysku zostaje ogryzek pliku, a
    # `download_bot_assets` domyślnie pomija to, co już istnieje — retry bez
    # force skończyłby się dokładnie tym samym błędem, w kółko. Ponowienie po
    # porażce nigdy nie ma ufać bajtom, które sam ten błąd zostawił.
    force = force or meeting.asset_state == "failed"
    if meeting.asset_state == "ready" and not force:
        ctx.progress(
            step="assets already on disk", line=f"assets in {meeting.asset_dir}"
        )
        return {"skipped": True, "dir": meeting.asset_dir}

    meeting.asset_state = "fetching"
    meeting.asset_error = None
    ctx.progress(5, "downloading assets from Recall", f"recall-fetch {meeting.id}")

    stream = _CtxStream(ctx)
    try:
        with redirect_stderr(stream), redirect_stdout(stream):
            summary = download_bot_assets(
                make_client(),
                meeting.id,
                settings.recall_dir,
                force=force,
            )
    finally:
        stream.flush()

    if summary.get("errors"):
        for err in summary["errors"][:10]:
            ctx.log(f"ERROR: {err}")

    state, path, found = local_asset_state(meeting.id, meeting.recording_id)
    meeting.asset_dir = path
    if state == "ready":
        if found and found != meeting.recording_id:
            adopt_disk_recording(meeting, Path(path), found)
        meeting.asset_state = "ready"
        load_participants_from_disk(ctx.session, meeting)
    else:
        meeting.asset_state = "failed"
        meeting.asset_error = (
            "Incomplete asset set (audio_mixed.mp3 + audio_separate raw required). "
            + (
                "; ".join(summary.get("errors", []))[:1000]
                or "The media may have expired (TTL)."
            )
        )
        ctx.session.commit()
        raise RuntimeError(meeting.asset_error)

    mb = summary.get("bytes", 0) / 1024 / 1024
    ctx.progress(40, "assets downloaded", f"downloaded {mb:.1f} MB → {path}")
    ctx.session.commit()
    return {
        "dir": path,
        "megabytes": round(mb, 1),
        "parts": summary.get("separate_parts_downloaded", 0),
    }


@task("fetch_assets")
def fetch_assets(ctx: JobContext) -> dict[str, Any]:
    meeting = _meeting(ctx)
    result = _do_fetch(ctx, meeting, force=bool(ctx.args.get("force")))
    ctx.progress(100, "done")
    return result


# --------------------------------------------------------------------------
# Pipeline (ASR + diaryzacja)
# --------------------------------------------------------------------------


def _transcript_paths(meeting: Meeting) -> tuple[Path, Path]:
    base = settings.transcripts_dir / meeting.id
    base.mkdir(parents=True, exist_ok=True)
    stem = meeting.recording_id or "recording"
    return base / f"{stem}.txt", base / f"{stem}.raw.json"


def _do_pipeline(
    ctx: JobContext, meeting: Meeting, force_asr: bool = False
) -> dict[str, Any]:
    api_key = settings.elevenlabs_api_key
    if not api_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set — the pipeline cannot start")
    if meeting.asset_state != "ready" or not meeting.asset_dir:
        raise RuntimeError("No assets on disk — run fetch_assets first")

    rec_dir = Path(meeting.asset_dir)
    txt_path, raw_path = _transcript_paths(meeting)
    meeting.transcript_state = "running"
    meeting.transcript_error = None
    ctx.session.commit()

    language = ctx.args.get("language")
    model = ctx.args.get("model") or settings.asr_model
    tiebreak = ctx.args.get("tiebreak", True)
    recover = ctx.args.get("overlap_recovery", True)

    # Krok 1: ASR na mixed. raw.json jest reużywany — retry po padzie
    # diaryzacji nie płaci drugi raz za transkrypcję całego spotkania.
    if raw_path.exists() and not force_asr:
        ctx.progress(50, "ASR: reuse raw.json", f"reuse {raw_path.name}")
    else:
        mixed = sorted(rec_dir.glob("audio_mixed*.mp3"))
        if not mixed:
            raise RuntimeError(f"no audio_mixed*.mp3 in {rec_dir}")
        ctx.progress(45, f"ASR ElevenLabs ({model})", f"ASR: {mixed[0].name}")
        response = elevenlabs_transcribe(
            mixed[0],
            api_key=api_key,
            diarize=False,
            model_id=model,
            language_code=language,
        )
        raw_path.write_text(response.model_dump_json(indent=2), encoding="utf-8")
        ctx.progress(60, "ASR done", f"raw → {raw_path.name}")

    data = json.loads(raw_path.read_text(encoding="utf-8"))
    words = data.get("words") or []
    lang = language or data.get("language_code")

    # Krok 2: diaryzacja energią izolowanych kanałów.
    ctx.progress(
        65,
        "channel-energy diarization",
        f"words: {len([w for w in words if w.get('type') == 'word'])}",
    )
    stream = _CtxStream(ctx)
    try:
        with redirect_stderr(stream), redirect_stdout(stream):
            utts, stats = diarize_by_energy(
                rec_dir,
                words,
                api_key=api_key,
                language=lang,
                model_id=model,
                tiebreak=bool(tiebreak),
                recover_overlap=bool(recover),
            )
    finally:
        stream.flush()

    text = format_energy_transcript(utts)
    txt_path.write_text(text, encoding="utf-8")
    ctx.progress(92, "saving transcript", f"→ {txt_path}")

    speakers = sorted({u.speaker for u in utts})
    talk_time: dict[str, float] = {}
    for u in utts:
        talk_time[u.speaker] = talk_time.get(u.speaker, 0.0) + max(0.0, u.end - u.start)

    transcript = Transcript(
        meeting_id=meeting.id,
        recording_id=meeting.recording_id,
        engine="pipeline-recall",
        language=lang,
        text_path=str(txt_path.relative_to(settings.transcripts_dir)),
        raw_path=str(raw_path.relative_to(settings.transcripts_dir)),
        speakers=[
            {"name": s, "seconds": round(talk_time.get(s, 0.0), 1)} for s in speakers
        ],
        utterance_count=len(utts),
        word_count=sum(len(u.text.split()) for u in utts),
        duration_seconds=max((u.end for u in utts), default=0.0),
        stats=stats,
    )
    ctx.session.add(transcript)
    meeting.transcript_state = "ready"
    if not meeting.duration_seconds:
        meeting.duration_seconds = transcript.duration_seconds
    _merge_speaking_time(meeting, talk_time)
    ctx.session.flush()
    rebuild_search_blob(meeting)
    ctx.session.commit()

    return {
        "transcript_id": transcript.id,
        "utterances": len(utts),
        "speakers": speakers,
        "stats": stats,
    }


def _merge_speaking_time(meeting: Meeting, talk_time: dict[str, float]) -> None:
    """Wpisz czas mówienia do uczestników z Recall (dopasowanie po nazwie)."""
    by_key = {p.key: p for p in meeting.participants if p.source == "recall"}
    for speaker, seconds in talk_time.items():
        p = by_key.get(speaker.strip().lower())
        if p is not None:
            p.speaking_seconds = round(seconds, 1)


@task("transcribe")
def transcribe_task(ctx: JobContext) -> dict[str, Any]:
    meeting = _meeting(ctx)
    try:
        result = _do_pipeline(ctx, meeting, force_asr=_force_asr(ctx))
    except Exception as e:
        meeting.transcript_state = "failed"
        meeting.transcript_error = f"{type(e).__name__}: {e}"[:2000]
        ctx.session.commit()
        raise
    ctx.progress(100, "done")
    return result


@task("process")
def process_meeting(ctx: JobContext) -> dict[str, Any]:
    """Fetch + pipeline — jedno kliknięcie „przetwórz”."""
    meeting = _meeting(ctx)
    fetch_result = _do_fetch(ctx, meeting, force=bool(ctx.args.get("force")))
    try:
        pipe_result = _do_pipeline(ctx, meeting, force_asr=_force_asr(ctx))
    except Exception as e:
        meeting.transcript_state = "failed"
        meeting.transcript_error = f"{type(e).__name__}: {e}"[:2000]
        ctx.session.commit()
        raise
    ctx.progress(100, "done")
    return {"fetch": fetch_result, "pipeline": pipe_result}


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------


@task("sync_recall")
def sync_recall(ctx: JobContext) -> dict[str, Any]:
    if not settings.recall_api_key:
        raise RuntimeError("RECALL_API_KEY is not set")
    lookback = ctx.args.get("lookback_days")
    bot_ids = ctx.args.get("bot_ids")

    def on_progress(seen: int, total: int) -> None:
        pct = 10 + min(60, seen // 2)
        ctx.progress(pct, f"{seen} bots", f"synced {seen} bots")

    ctx.progress(5, "fetching bots from Recall")
    result = sync_bots(
        ctx.session, lookback_days=lookback, bot_ids=bot_ids, on_progress=on_progress
    )
    ctx.progress(75, "Recall sync done", json.dumps(result, ensure_ascii=False))

    if ctx.args.get("with_calendar", True):
        try:
            cal = _sync_calendar(ctx)
            result["calendar"] = cal
        except Exception as e:  # noqa: BLE001 — kalendarz to wzbogacenie, nie blocker
            ctx.log(f"calendar skipped: {type(e).__name__}: {e}")
            result["calendar"] = {"error": str(e)}

    # Nowi ludzie = nowe wiersze bez adresu (Recall podaje samą nazwę) albo
    # bez nazwy (kalendarz podaje sam adres). Dopóki ich nie skleimy, ta sama
    # osoba siedzi pod dwoma kluczami i dubluje się w filtrach. Wcześniej
    # robił to dopiero ręczny `repair_participants`, więc duplikaty wracały
    # po każdym syncu, który dołożył uczestników.
    cal = result.get("calendar") or {}
    if result.get("created") or cal.get("linked_participants"):
        ctx.progress(90, "matching identities")
        from webapp.recall_sync import resolve_identities

        ident = resolve_identities(ctx.session)
        result["identities"] = ident
        ctx.log(
            f"emails matched: {ident['matched']}, without an email: {ident['left']}"
        )

    if get_autoprocess(ctx.session):
        queued = _autoqueue(ctx)
        result["queued"] = queued
        ctx.log(f"queued {queued} meetings for processing")

    ctx.progress(100, "done")
    return result


def _sync_calendar(ctx: JobContext, *, only_missing: bool = False) -> dict[str, Any]:
    from webapp.gcal import enrich_meetings, pick_calendar_user

    user_id = ctx.args.get("user_id")
    user = (
        ctx.session.get(User, user_id) if user_id else pick_calendar_user(ctx.session)
    )
    if user is None:
        return {"skipped": "no user with calendar access"}
    ctx.progress(80, "Google Calendar", f"calendar: {user.email}")
    return enrich_meetings(
        ctx.session,
        user,
        lookback_days=ctx.args.get("lookback_days") or settings.sync_lookback_days,
        only_missing=only_missing,
        on_progress=ctx.log,
    )


@task("sync_calendar")
def sync_calendar(ctx: JobContext) -> dict[str, Any]:
    ctx.progress(10, "Google Calendar")
    result = _sync_calendar(ctx, only_missing=bool(ctx.args.get("only_missing")))
    ctx.progress(100, "done")
    return result


@task("backfill_titles")
def backfill_titles(ctx: JobContext) -> dict[str, Any]:
    """Dociągnij tytuły spotkaniom, które wypadły z okna zwykłego syncu.

    Zwykły sync patrzy w kalendarz tylko wokół „teraz", więc wszystko starsze
    niż lookback zostaje z tytułem fallbackowym bezterminowo. Tu okno wynika
    z dat spotkań bez tytułu, nie z zegara.
    """
    ctx.progress(10, "Google Calendar (backfill)")
    result = _sync_calendar(ctx, only_missing=True)
    if result.get("matched"):
        ctx.progress(80, "matching identities")
        from webapp.recall_sync import resolve_identities

        result["identities"] = resolve_identities(ctx.session)
    ctx.progress(100, "done")
    return result


def _autoqueue(ctx: JobContext) -> int:
    """Zakolejkuj przetwarzanie spotkań z nagraniem i bez transkryptu.

    Priorytet rośnie, im bliżej wygaśnięcia mediów — TTL Recall to 24h,
    a nieściągnięte audio jest stracone bezpowrotnie.
    """
    from webapp.recall_sync import meetings_ready_to_process

    n = 0
    for meeting in meetings_ready_to_process(ctx.session):
        expires = meeting.media_expires_at
        urgent = expires is not None and expires - utcnow() < dt.timedelta(hours=6)
        enqueue(
            ctx.session,
            "process",
            meeting_id=meeting.id,
            priority=10 if urgent else 100,
            created_by="autoprocess",
        )
        meeting.transcript_state = "queued"
        n += 1
    ctx.session.commit()
    return n


# --------------------------------------------------------------------------
# Utrzymanie
# --------------------------------------------------------------------------


@task("repair_participants")
def repair_participants(ctx: JobContext) -> dict[str, Any]:
    """Scal zdublowanych uczestników (ta sama osoba pod dwoma kluczami)."""
    from webapp.recall_sync import repair_participant_keys, resolve_identities

    ctx.progress(10, "matching names to emails")
    matched = resolve_identities(ctx.session)
    ctx.log(
        f"emails matched: {matched['matched']}, without an email: {matched['left']}"
    )
    ctx.progress(60, "recomputing identity keys")
    result = repair_participant_keys(ctx.session)
    result.update(matched)
    ctx.progress(100, "done", json.dumps(result, ensure_ascii=False))
    return result


@task("repair_assets")
def repair_assets(ctx: JobContext) -> dict[str, Any]:
    """Podłącz spotkania do audio, które leży na dysku, a baza o nim nie wie."""
    from webapp.recall_sync import adopt_local_recordings

    ctx.progress(10, "scanning the recordings directory", f"scan {settings.recall_dir}")
    result = adopt_local_recordings(ctx.session)
    ctx.log(
        f"recordings adopted: {result['adopted']}, state recovered: {result['relinked']}, "
        f"participants from disk: {result['participants']}"
    )
    ctx.progress(100, "done", json.dumps(result, ensure_ascii=False))
    return result


@task("cleanup_audio")
def cleanup_audio(ctx: JobContext) -> dict[str, Any]:
    """Skasuj audio spotkań, które mają już transkrypt (audio waży GB).

    Domyślnie starsze niż `older_than_days` (default 14). Transkrypty zostają.
    """
    days = int(ctx.args.get("older_than_days", 14))
    cutoff = utcnow() - dt.timedelta(days=days)
    freed = 0
    removed = 0
    rows = list(
        ctx.session.execute(
            select(Meeting).where(
                Meeting.transcript_state == "ready",
                Meeting.asset_state == "ready",
                Meeting.started_at.is_not(None),
                Meeting.started_at < cutoff,
            )
        ).scalars()
    )
    for meeting in rows:
        if not meeting.asset_dir:
            continue
        d = Path(meeting.asset_dir)
        if not d.exists():
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.suffix in (".raw", ".mp3", ".ogg", ".mp4"):
                freed += f.stat().st_size
                f.unlink()
        meeting.asset_state = "expired"
        removed += 1
        ctx.log(f"audio cleaned up: {meeting.title}")
    ctx.session.commit()
    return {"meetings": removed, "freed_mb": round(freed / 1024 / 1024, 1)}


# --------------------------------------------------------------------------
# Eksport transkryptu
# --------------------------------------------------------------------------

_LINE_RE = re.compile(r"^(?P<speaker>.+?) \[(?P<ts>\d{2}:\d{2}:\d{2})\] (?P<text>.*)$")


def parse_transcript(text: str) -> list[dict[str, Any]]:
    """Rozbij plik `Mówca [HH:MM:SS] tekst` na strukturę do UI/eksportu."""
    out: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        m = _LINE_RE.match(line)
        if not m:
            if out:
                out[-1]["text"] += " " + line.strip()
            continue
        h, mnt, s = (int(x) for x in m.group("ts").split(":"))
        out.append(
            {
                "speaker": m.group("speaker"),
                "timestamp": m.group("ts"),
                "seconds": h * 3600 + mnt * 60 + s,
                "text": m.group("text"),
            }
        )
    return out


def transcript_file_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else settings.transcripts_dir / path


def transcript_text(transcript: Transcript) -> str:
    path = transcript_file_path(transcript.text_path)
    if not path.exists():
        raise FileNotFoundError(f"the transcript file is gone: {path}")
    return path.read_text(encoding="utf-8")


def to_vtt(
    utterances: list[dict[str, Any]], total_seconds: Optional[float] = None
) -> str:
    def stamp(sec: float) -> str:
        ms = int((sec - int(sec)) * 1000)
        s = int(sec)
        return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}.{ms:03d}"

    lines = ["WEBVTT", ""]
    for i, u in enumerate(utterances):
        start = float(u["seconds"])
        nxt = utterances[i + 1]["seconds"] if i + 1 < len(utterances) else None
        end = float(nxt) if nxt is not None else (total_seconds or start + 5)
        end = max(end, start + 1)
        lines += [
            f"{stamp(start)} --> {stamp(end)}",
            f"<v {u['speaker']}>{u['text']}",
            "",
        ]
    return "\n".join(lines)


def to_markdown(meeting: Meeting, utterances: list[dict[str, Any]]) -> str:
    when = (
        meeting.occurred_at.strftime("%Y-%m-%d %H:%M UTC")
        if meeting.occurred_at
        else "—"
    )
    head = [f"# {meeting.title}", "", f"- Date: {when}"]
    people = [p.display for p in meeting.human_participants]
    if people:
        head.append(f"- Participants: {', '.join(people)}")
    head += [f"- Bot: `{meeting.id}`", "", "---", ""]
    body = [f"**{u['speaker']}** `[{u['timestamp']}]` {u['text']}" for u in utterances]
    return "\n".join(head + body) + "\n"
