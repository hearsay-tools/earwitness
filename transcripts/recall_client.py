"""Klient do pobierania assetów (audio) z Recall.ai.

Po co: nagrania mają TTL (default 24h, max 30 dni przy retention config) — po
wygaśnięciu `media_expired` URLe znikają. Ten moduł pozwala szybko stłuczkować
wszystko co dostępne lokalnie i pogrupować per bot → recording → participant,
żeby mieć materiał do (re-)transkrypcji i (re-)diaryzacji.

Pobiera:
- `audio_mixed` (jeden plik mp3/raw na recording) — przez `media_shortcuts`
- `audio_separate` (per-participant parts) — przez dedykowany endpoint,
  bo nie jest w `media_shortcuts`
- `participant_events` (speaker-timeline.json + participants.json +
  participant-events.json) — przez `media_shortcuts`; speaker-timeline to
  gotowy ground-truth diaryzacji (patrz `ground-truth` / `hybrid --ground-truth`)

Region + API key są konfigurowalne (env vars RECALL_API_KEY / RECALL_REGION
albo argumenty konstruktora). Default region: `eu-central-1`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

VALID_REGIONS = ("us-west-2", "us-east-1", "eu-central-1", "ap-northeast-1")
DEFAULT_REGION = "eu-central-1"


@dataclass
class RecallConfig:
    api_key: str
    region: str = DEFAULT_REGION

    def __post_init__(self) -> None:
        if self.region not in VALID_REGIONS:
            raise ValueError(
                f"Nieznany region {self.region!r}. Dozwolone: {VALID_REGIONS}"
            )
        if not self.api_key:
            raise ValueError("api_key jest wymagany")

    @property
    def base_url(self) -> str:
        return f"https://{self.region}.recall.ai/api/v1"

    @classmethod
    def from_env(
        cls,
        api_key: Optional[str] = None,
        region: Optional[str] = None,
    ) -> "RecallConfig":
        key = api_key or os.environ.get("RECALL_API_KEY", "")
        reg = region or os.environ.get("RECALL_REGION", DEFAULT_REGION)
        return cls(api_key=key, region=reg)


class RecallClient:
    def __init__(self, config: RecallConfig, timeout: float = 60.0) -> None:
        self.config = config
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Token {self.config.api_key}",
            "Accept": "application/json",
        }

    def _get(self, path_or_url: str, params: Optional[dict] = None) -> dict:
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            url = f"{self.config.base_url}{path_or_url}"
        if params:
            qs = urllib.parse.urlencode(
                {k: v for k, v in params.items() if v is not None},
                doseq=True,
            )
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{qs}"
        req = urllib.request.Request(url, headers=self._headers())
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Recall API {e.code} {e.reason} on GET {url}: {body}"
            ) from e

    def _post(self, path_or_url: str, body: Optional[dict] = None) -> dict:
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            url = f"{self.config.base_url}{path_or_url}"
        data = json.dumps(body or {}).encode("utf-8")
        headers = {**self._headers(), "Content-Type": "application/json"}
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            body_err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Recall API {e.code} {e.reason} on POST {url}: {body_err}"
            ) from e

    # ---- paginated list helper (works for both /bot/ and /audio_*/) ----

    def _paginate(self, path: str, params: dict) -> Iterator[dict]:
        # Recall używa offset paginacji (page=N), albo cursor (use_cursor=true).
        # Cursor jest stabilny przy zmianach danych — bierzemy go.
        params = {**params, "use_cursor": "true"}
        url: Optional[str] = path
        first = True
        while url:
            payload = self._get(url, params if first else None)
            for item in payload.get("results", []):
                yield item
            url = payload.get("next")
            first = False

    # ---- bots ----

    def list_bots(
        self,
        status: Optional[list[str]] = None,
        meeting_url: Optional[str] = None,
        join_at_after: Optional[str] = None,
        join_at_before: Optional[str] = None,
    ) -> Iterator[dict]:
        """Iteruje po wszystkich botach (z paginacją)."""
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if meeting_url:
            params["meeting_url"] = meeting_url
        if join_at_after:
            params["join_at_after"] = join_at_after
        if join_at_before:
            params["join_at_before"] = join_at_before
        yield from self._paginate("/bot/", params)

    def get_bot(self, bot_id: str) -> dict:
        return self._get(f"/bot/{bot_id}/")

    def pause_recording(self, bot_id: str) -> dict:
        """Wstrzymuje nagrywanie (bot zostaje w callu). Zwraca payload bota."""
        return self._post(f"/bot/{bot_id}/pause_recording/")

    def resume_recording(self, bot_id: str) -> dict:
        """Wznawia nagrywanie po pause_recording. Zwraca payload bota."""
        return self._post(f"/bot/{bot_id}/resume_recording/")

    # ---- audio artifacts (per-recording) ----

    def list_audio_mixed(self, recording_id: str) -> list[dict]:
        return list(
            self._paginate(
                "/audio_mixed/",
                {"recording_id": recording_id, "status_code": "done"},
            )
        )

    def list_audio_separate(self, recording_id: str) -> list[dict]:
        return list(
            self._paginate(
                "/audio_separate/",
                {"recording_id": recording_id, "status_code": "done"},
            )
        )


# ---------- asset collection + download ----------


@dataclass
class AudioMixedAsset:
    artifact_id: str
    recording_id: str
    fmt: str  # mp3 / raw
    download_url: str


@dataclass
class AudioSeparatePart:
    part_id: str
    participant_id: Optional[int]
    participant_name: Optional[str]
    start_relative: float
    duration: float
    download_url: str


@dataclass
class AudioSeparateAsset:
    artifact_id: str
    recording_id: str
    fmt: str  # raw / ogg
    manifest_url: str  # URL do JSON-a z parts
    raw_manifest: list = field(default_factory=list)  # surowy payload z manifest_url
    parts: list[AudioSeparatePart] = field(default_factory=list)


@dataclass
class RecordingAssets:
    recording_id: str
    bot_id: str
    expires_at: Optional[str]
    started_at: Optional[str]
    completed_at: Optional[str]
    audio_mixed: list[AudioMixedAsset] = field(default_factory=list)
    audio_separate: list[AudioSeparateAsset] = field(default_factory=list)
    # participant_events shortcut — presigned URLe do JSON-ów (bez auth).
    speaker_timeline_url: Optional[str] = None
    participants_url: Optional[str] = None
    participant_events_url: Optional[str] = None


def _safe_filename(s: str, fallback: str = "unknown") -> str:
    s = (s or fallback).strip()
    s = re.sub(r"[^\w.\-]+", "_", s)
    return s[:80] or fallback


def _fetch_json(url: str, timeout: float = 60.0) -> Any:
    """Pobiera JSON spod *presigned* URLa (bez auth headerów)."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_stream(url: str, dest: Path, chunk: int = 1024 * 256) -> int:
    """Strumieniowy download (presigned URL, bez auth). Zwraca rozmiar w bajtach."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    total = 0
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as f:
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            f.write(buf)
            total += len(buf)
    tmp.rename(dest)
    return total


def collect_bot_assets(
    client: RecallClient,
    bot: dict,
) -> list[RecordingAssets]:
    """Z payloadu bota wyciąga wszystkie dostępne audio-assety (per recording).

    Wymaga pełnego bot payloadu (z `recordings` array — zwraca `get_bot`).
    Robi dodatkowe zapytania do `/audio_separate/` (oraz `/audio_mixed/` dla
    pełności, fallback gdy shortcut jest pusty).
    """
    out: list[RecordingAssets] = []
    bot_id = bot["id"]
    for rec in bot.get("recordings") or []:
        rec_id = rec["id"]
        assets = RecordingAssets(
            recording_id=rec_id,
            bot_id=bot_id,
            expires_at=rec.get("expires_at"),
            started_at=rec.get("started_at"),
            completed_at=rec.get("completed_at"),
        )

        # Audio mixed: najpierw shortcut, potem fallback do listy
        shortcut = (rec.get("media_shortcuts") or {}).get("audio_mixed")
        if shortcut and (shortcut.get("data") or {}).get("download_url"):
            assets.audio_mixed.append(
                AudioMixedAsset(
                    artifact_id=shortcut["id"],
                    recording_id=rec_id,
                    fmt=shortcut.get("format") or "mp3",
                    download_url=shortcut["data"]["download_url"],
                )
            )
        else:
            for art in client.list_audio_mixed(rec_id):
                url = (art.get("data") or {}).get("download_url")
                if not url:
                    continue
                assets.audio_mixed.append(
                    AudioMixedAsset(
                        artifact_id=art["id"],
                        recording_id=rec_id,
                        fmt=art.get("format") or "mp3",
                        download_url=url,
                    )
                )

        # Audio separate: zawsze przez dedykowany endpoint
        for art in client.list_audio_separate(rec_id):
            url = (art.get("data") or {}).get("download_url")
            if not url:
                continue
            # manifest_url to JSON z listą parts — każdy part ma swój download_url.
            # Trzymamy raw payload w sep.raw_manifest żeby download_bot_assets()
            # mogło go zapisać na dysk bez ponownego fetcha.
            try:
                manifest = _fetch_json(url)
            except Exception as e:
                print(
                    f"  WARN: nie udało się pobrać manifestu audio_separate "
                    f"{art['id']}: {e}",
                    file=sys.stderr,
                )
                manifest = []
            sep = AudioSeparateAsset(
                artifact_id=art["id"],
                recording_id=rec_id,
                fmt=art.get("format") or "ogg",
                manifest_url=url,
                raw_manifest=manifest,
            )
            for part in manifest:
                p = part.get("participant") or {}
                sep.parts.append(
                    AudioSeparatePart(
                        part_id=part.get("id", ""),
                        participant_id=p.get("id"),
                        participant_name=p.get("name"),
                        start_relative=(part.get("start_timestamp") or {}).get(
                            "relative", 0.0
                        ),
                        duration=part.get("duration") or 0.0,
                        download_url=part.get("download_url") or "",
                    )
                )
            assets.audio_separate.append(sep)

        # participant_events: speaker_timeline + participants + events (JSON-y).
        # W media_shortcuts pod .data — presigned URLe (fetch bez auth).
        pe_data = (
            (rec.get("media_shortcuts") or {}).get("participant_events") or {}
        ).get("data") or {}
        assets.speaker_timeline_url = pe_data.get("speaker_timeline_download_url")
        assets.participants_url = pe_data.get("participants_download_url")
        assets.participant_events_url = pe_data.get("participant_events_download_url")

        out.append(assets)
    return out


def download_bot_assets(
    client: RecallClient,
    bot_id: str,
    out_dir: Path,
    force: bool = False,
    download_mixed: bool = True,
    download_separate: bool = True,
    download_events: bool = True,
) -> dict:
    """Pobierz wszystkie audio-assety bota do `out_dir/<bot_id>/...`.

    Struktura:
      <out_dir>/<bot_id>/
        bot.json                          # surowy payload bota
        <recording_id>/
          recording.json                  # metadata + expires_at + uporządkowane assety
          audio_mixed.<ext>               # jeśli dostępne
          audio_separate/
            parts.json                    # surowy manifest
            <participant>/<part_id>.<ext> # per-part pliki

    Idempotentne: pomija pliki, które już istnieją (chyba że `force=True`).
    Zwraca podsumowanie (counts).
    """
    bot = client.get_bot(bot_id)
    bot_dir = out_dir / bot_id
    bot_dir.mkdir(parents=True, exist_ok=True)
    (bot_dir / "bot.json").write_text(
        json.dumps(bot, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    all_assets = collect_bot_assets(client, bot)
    summary = {
        "bot_id": bot_id,
        "recordings": len(all_assets),
        "mixed_downloaded": 0,
        "mixed_skipped": 0,
        "separate_parts_downloaded": 0,
        "separate_parts_skipped": 0,
        "events_downloaded": 0,
        "events_skipped": 0,
        "bytes": 0,
        "errors": [],
    }

    for rec in all_assets:
        rec_dir = bot_dir / rec.recording_id
        rec_dir.mkdir(parents=True, exist_ok=True)

        # Recording metadata (lekka)
        rec_meta = {
            "recording_id": rec.recording_id,
            "bot_id": rec.bot_id,
            "expires_at": rec.expires_at,
            "started_at": rec.started_at,
            "completed_at": rec.completed_at,
            "audio_mixed": [
                {"id": a.artifact_id, "format": a.fmt} for a in rec.audio_mixed
            ],
            "audio_separate": [
                {
                    "id": a.artifact_id,
                    "format": a.fmt,
                    "participants": sorted(
                        {f"{p.participant_id}:{p.participant_name}" for p in a.parts}
                    ),
                    "parts": len(a.parts),
                }
                for a in rec.audio_separate
            ],
            "participant_events": {
                "speaker_timeline": bool(rec.speaker_timeline_url),
                "participants": bool(rec.participants_url),
                "participant_events": bool(rec.participant_events_url),
            },
        }
        (rec_dir / "recording.json").write_text(
            json.dumps(rec_meta, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # Audio mixed
        if download_mixed:
            for i, art in enumerate(rec.audio_mixed):
                ext = "mp3" if art.fmt == "mp3" else "raw"
                # Jeśli >1 mixed, dodaj suffix
                name = (
                    f"audio_mixed.{ext}"
                    if len(rec.audio_mixed) == 1
                    else f"audio_mixed_{i}_{art.artifact_id[:8]}.{ext}"
                )
                dest = rec_dir / name
                if dest.exists() and not force:
                    summary["mixed_skipped"] += 1
                    print(f"  skip (exists): {dest}", file=sys.stderr)
                    continue
                try:
                    print(
                        f"  ↓ audio_mixed {art.artifact_id[:8]} → {dest.name}",
                        file=sys.stderr,
                    )
                    n = _download_stream(art.download_url, dest)
                    summary["mixed_downloaded"] += 1
                    summary["bytes"] += n
                except Exception as e:
                    err = f"audio_mixed {art.artifact_id}: {e}"
                    summary["errors"].append(err)
                    print(f"  ERROR: {err}", file=sys.stderr)

        # Audio separate
        if download_separate:
            for art in rec.audio_separate:
                sep_dir = rec_dir / "audio_separate"
                sep_dir.mkdir(parents=True, exist_ok=True)
                # Zapisz surowy manifest dla referencji (zawsze overwrite —
                # download_url-e w manifeście są presigned i mogą wygasać,
                # więc disk version ma zostać aligned z tym, czego realnie
                # użyliśmy do downloadu).
                manifest_path = sep_dir / f"parts_{art.artifact_id[:8]}.json"
                manifest_path.write_text(
                    json.dumps(art.raw_manifest, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )

                ext = "ogg" if art.fmt == "ogg" else "raw"
                for part in art.parts:
                    if not part.download_url:
                        continue
                    p_label = _safe_filename(
                        f"{part.participant_id or 0}-{part.participant_name or 'unknown'}"
                    )
                    p_dir = sep_dir / p_label
                    dest = p_dir / f"{_safe_filename(part.part_id, 'part')}.{ext}"
                    if dest.exists() and not force:
                        summary["separate_parts_skipped"] += 1
                        continue
                    try:
                        print(
                            f"  ↓ audio_separate {p_label} part "
                            f"{part.part_id[:8]} ({part.duration:.1f}s) "
                            f"→ {dest.relative_to(out_dir)}",
                            file=sys.stderr,
                        )
                        n = _download_stream(part.download_url, dest)
                        summary["separate_parts_downloaded"] += 1
                        summary["bytes"] += n
                    except Exception as e:
                        err = f"audio_separate part {part.part_id} ({p_label}): {e}"
                        summary["errors"].append(err)
                        print(f"  ERROR: {err}", file=sys.stderr)

        # participant_events (JSON-y: speaker-timeline / participants / events).
        # Presigned URLe — fetch bez auth. Idempotentne jak reszta.
        if download_events:
            events = [
                (rec.speaker_timeline_url, "speaker-timeline.json"),
                (rec.participants_url, "participants.json"),
                (rec.participant_events_url, "participant-events.json"),
            ]
            for url, name in events:
                if not url:
                    continue
                dest = rec_dir / name
                if dest.exists() and not force:
                    summary["events_skipped"] += 1
                    print(f"  skip (exists): {dest}", file=sys.stderr)
                    continue
                try:
                    print(f"  ↓ {name}", file=sys.stderr)
                    n = _download_stream(url, dest)
                    summary["events_downloaded"] += 1
                    summary["bytes"] += n
                except Exception as e:
                    err = f"participant_events {name}: {e}"
                    summary["errors"].append(err)
                    print(f"  ERROR: {err}", file=sys.stderr)

    return summary
