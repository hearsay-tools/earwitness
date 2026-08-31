"""Buduje ground-truth z plików speaker-timeline.json + participants.json.

Format wyjściowy (nagłówek + linie per wypowiedź):

    # Ground truth: <stem>
    # Source: <timeline filename>
    # Span: <hh:mm:ss>, <N> turns, <K> unique speakers
    # Speakers:
    #   - <name> (id=<id>, talk=<seconds>s, turns=<n>) [host]
    #
    <Speaker Name> [HH:mm:ss-HH:mm:ss] (<duration>s)
    ...
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .formatter import format_seconds


@dataclass
class TimelineEntry:
    pid: int
    name: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


def _parse_timeline(path: Path) -> list[TimelineEntry]:
    raw = json.loads(path.read_text())
    out: list[TimelineEntry] = []
    for ev in raw:
        p = ev.get("participant") or {}
        start = (ev.get("start_timestamp") or {}).get("relative") or 0.0
        end = (ev.get("end_timestamp") or {}).get("relative") or 0.0
        out.append(
            TimelineEntry(
                pid=p.get("id"),
                name=p.get("name") or f"speaker_{p.get('id')}",
                start=float(start),
                end=float(end),
            )
        )
    out.sort(key=lambda e: e.start)
    return out


def _format_range(start: float, end: float) -> str:
    return f"{format_seconds(start)}-{format_seconds(end)}"


def build_ground_truth(
    timeline_path: Path,
    participants_path: Optional[Path] = None,
) -> str:
    entries = _parse_timeline(timeline_path)
    if not entries:
        return "# (empty timeline)\n"

    # statystyki per mówca
    talk_time: dict[int, float] = defaultdict(float)
    turns: dict[int, int] = defaultdict(int)
    name_by_id: dict[int, str] = {}
    for e in entries:
        talk_time[e.pid] += e.duration
        turns[e.pid] += 1
        name_by_id[e.pid] = e.name

    # rozszerzenie o participants.json (host flag, email itp.)
    host_ids: set[int] = set()
    if participants_path is not None and participants_path.exists():
        for p in json.loads(participants_path.read_text()):
            if p.get("is_host"):
                host_ids.add(p.get("id"))

    span_end = max(e.end for e in entries)

    lines: list[str] = []
    lines.append(f"# Ground truth: {timeline_path.stem}")
    lines.append(f"# Source: {timeline_path.name}")
    lines.append(
        f"# Span: {format_seconds(span_end)}, "
        f"{len(entries)} turns, {len(name_by_id)} unique speakers"
    )
    lines.append("# Speakers (sorted by talk time desc):")
    for pid, secs in sorted(talk_time.items(), key=lambda kv: -kv[1]):
        host_marker = " [host]" if pid in host_ids else ""
        lines.append(
            f"#   - {name_by_id[pid]} (id={pid}, "
            f"talk={secs:.1f}s, turns={turns[pid]}){host_marker}"
        )
    lines.append("#")

    for e in entries:
        lines.append(f"{e.name} [{_format_range(e.start, e.end)}] ({e.duration:.1f}s)")

    return "\n".join(lines) + "\n"
