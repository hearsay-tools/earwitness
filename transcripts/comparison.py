"""Importery transkryptów z różnych źródeł i raport porównawczy.

Cel: wziąć ground-truth (z speaker-timeline.json) i dowolną liczbę kandydatów
(ElevenLabs, MacWhisper, Fireflies, ...) i pokazać per-osoba ile turns / ile
sekund mówienia każdy silnik przypisał do każdej realnej osoby.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Utterance:
    speaker: str
    start: float
    end: float
    text: str = ""


# ---------- timestamp parsing ----------

_TS_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.(\d+))?$")


def parse_timestamp(s: str) -> float:
    """Parsuje 'MM:SS', 'H:MM:SS', 'HH:MM:SS' (opcjonalne ułamki)."""
    s = s.strip()
    m = _TS_RE.match(s)
    if not m:
        raise ValueError(f"Nieznany format timestamp: {s!r}")
    h = int(m.group(1) or 0)
    mm = int(m.group(2))
    ss = int(m.group(3))
    frac = float("0." + m.group(4)) if m.group(4) else 0.0
    return h * 3600 + mm * 60 + ss + frac


# ---------- loaders ----------


def load_ground_truth(timeline_path: Path) -> list[Utterance]:
    raw = json.loads(timeline_path.read_text())
    out: list[Utterance] = []
    for ev in raw:
        p = ev.get("participant") or {}
        start = (ev.get("start_timestamp") or {}).get("relative") or 0.0
        end = (ev.get("end_timestamp") or {}).get("relative") or 0.0
        out.append(
            Utterance(
                speaker=p.get("name") or f"speaker_{p.get('id')}",
                start=float(start),
                end=float(end),
            )
        )
    out.sort(key=lambda u: u.start)
    return out


def load_fireflies(json_path: Path) -> list[Utterance]:
    raw = json.loads(json_path.read_text())
    out: list[Utterance] = []
    for s in raw:
        # zachowujemy speaker_id w etykiecie, bo Fireflies trzyma 9 unikalnych
        # profili głosowych pod 6 nazw — chcemy widzieć każdy oryginalny profil
        sid = s.get("speaker_id")
        name = s.get("speaker_name") or f"speaker_{sid}"
        label = f"{name} #{sid}" if sid is not None else name
        out.append(
            Utterance(
                speaker=label,
                start=parse_timestamp(s["startTime"]),
                end=parse_timestamp(s["endTime"]),
                text=s.get("sentence", ""),
            )
        )
    out.sort(key=lambda u: u.start)
    return out


_MW_SPEAKER_RE = re.compile(r"^Speaker \d+$")


def load_macwhisper(txt_path: Path) -> list[Utterance]:
    blocks = txt_path.read_text().split("\n\n")
    items: list[Utterance] = []
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if len(lines) < 2:
            continue
        if not _MW_SPEAKER_RE.match(lines[0]):
            continue
        try:
            start = parse_timestamp(lines[1])
        except ValueError:
            continue
        text = "\n".join(lines[2:]).strip()
        items.append(
            Utterance(
                speaker=lines[0],
                start=start,
                end=start,  # placeholder; ustawiamy przez next.start poniżej
                text=text,
            )
        )
    items.sort(key=lambda u: u.start)
    # estymuj end z następnej wypowiedzi
    for i in range(len(items) - 1):
        items[i].end = max(items[i].start, items[i + 1].start)
    if items:
        items[-1].end = items[-1].start + 2.0
    return items


_EL_LINE_RE = re.compile(r"^(?!#)(.+?) \[(\d{1,2}:\d{2}:\d{2})\] (.*)$")


def load_elevenlabs(txt_path: Path) -> list[Utterance]:
    items: list[Utterance] = []
    for line in txt_path.read_text().splitlines():
        m = _EL_LINE_RE.match(line)
        if not m:
            continue
        items.append(
            Utterance(
                speaker=m.group(1),
                start=parse_timestamp(m.group(2)),
                end=0.0,
                text=m.group(3),
            )
        )
    items.sort(key=lambda u: u.start)
    for i in range(len(items) - 1):
        items[i].end = max(items[i].start, items[i + 1].start)
    if items:
        items[-1].end = items[-1].start + 2.0
    return items


# ---------- aliasy ----------


def apply_aliases(
    utterances: list[Utterance],
    aliases: dict[str, str],
) -> list[Utterance]:
    """Zwraca nową listę z renamowanymi speakerami wg mapy {oryginał: canonical}."""
    if not aliases:
        return utterances
    return [
        Utterance(
            speaker=aliases.get(u.speaker, u.speaker),
            start=u.start,
            end=u.end,
            text=u.text,
        )
        for u in utterances
    ]


# ---------- statystyki + mapowanie ----------


def speaker_stats(utterances: list[Utterance]) -> dict[str, dict]:
    stats: dict[str, dict] = {}
    for u in utterances:
        s = stats.setdefault(u.speaker, {"turns": 0, "talk_s": 0.0})
        s["turns"] += 1
        s["talk_s"] += max(0.0, u.end - u.start)
    return stats


def _intervals_by_speaker(
    utterances: list[Utterance],
) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for u in utterances:
        if u.end > u.start:
            out.setdefault(u.speaker, []).append((u.start, u.end))
    for v in out.values():
        v.sort()
    return out


def _total_overlap(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> float:
    i = j = 0
    total = 0.0
    while i < len(a) and j < len(b):
        s = max(a[i][0], b[j][0])
        e = min(a[i][1], b[j][1])
        if s < e:
            total += e - s
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return total


@dataclass
class SpeakerMapping:
    cand_speaker: str
    gt_speaker: str | None
    overlap_s: float
    cand_total_s: float
    gt_total_s: float
    breakdown: list[tuple[str, float]]  # top-N (gt_speaker, overlap_s)


def map_to_ground_truth(
    candidate: list[Utterance],
    gt: list[Utterance],
    top_n: int = 3,
) -> list[SpeakerMapping]:
    cand_iv = _intervals_by_speaker(candidate)
    gt_iv = _intervals_by_speaker(gt)
    gt_totals = {sp: sum(e - s for s, e in iv) for sp, iv in gt_iv.items()}

    mappings: list[SpeakerMapping] = []
    for cand_sp, cand_intervals in cand_iv.items():
        cand_total = sum(e - s for s, e in cand_intervals)
        scored = [
            (gt_sp, _total_overlap(cand_intervals, gt_iv[gt_sp])) for gt_sp in gt_iv
        ]
        scored.sort(key=lambda x: -x[1])
        best_sp, best_ov = scored[0] if scored else (None, 0.0)
        mappings.append(
            SpeakerMapping(
                cand_speaker=cand_sp,
                gt_speaker=best_sp if best_ov > 0 else None,
                overlap_s=best_ov,
                cand_total_s=cand_total,
                gt_total_s=gt_totals.get(best_sp, 0.0) if best_sp else 0.0,
                breakdown=[(sp, ov) for sp, ov in scored[:top_n] if ov > 0],
            )
        )
    mappings.sort(key=lambda m: -m.cand_total_s)
    return mappings


# ---------- raport ----------


def _fmt_row(cells: list[str], widths: list[int]) -> str:
    return "  ".join(
        c.ljust(w) if i == 0 else c.rjust(w)
        for i, (c, w) in enumerate(zip(cells, widths))
    )


def _hms(s: float) -> str:
    s = int(s)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def format_report(
    gt: list[Utterance],
    candidates: dict[str, list[Utterance]],
) -> str:
    lines: list[str] = []

    # Sumarycznie
    lines.append("=" * 78)
    lines.append("PODSUMOWANIE")
    lines.append("=" * 78)
    header_w = [22, 10, 8, 12]
    lines.append(_fmt_row(["source", "speakers", "turns", "talk(hh:mm:ss)"], header_w))
    lines.append("-" * sum(header_w + [3 * 3]))
    gt_stats = speaker_stats(gt)
    gt_total_talk = sum(s["talk_s"] for s in gt_stats.values())
    lines.append(
        _fmt_row(
            [
                "ground_truth",
                str(len(gt_stats)),
                str(sum(s["turns"] for s in gt_stats.values())),
                _hms(gt_total_talk),
            ],
            header_w,
        )
    )
    for name, utts in candidates.items():
        st = speaker_stats(utts)
        lines.append(
            _fmt_row(
                [
                    name,
                    str(len(st)),
                    str(sum(s["turns"] for s in st.values())),
                    _hms(sum(s["talk_s"] for s in st.values())),
                ],
                header_w,
            )
        )
    lines.append("")

    # Mapping per kandydat
    cand_mappings: dict[str, list[SpeakerMapping]] = {
        name: map_to_ground_truth(utts, gt) for name, utts in candidates.items()
    }

    # Per-speaker turns / talk-time mapped to GT
    gt_speakers_sorted = sorted(gt_stats.keys(), key=lambda sp: -gt_stats[sp]["talk_s"])

    lines.append("=" * 78)
    lines.append("TURNS PER OSOBA (kandydaci → ground-truth speaker)")
    lines.append("=" * 78)
    cand_names = list(candidates.keys())
    sp_w = 26
    col_w = max(8, max((len(n) for n in cand_names), default=8))
    lines.append(
        _fmt_row(
            ["real speaker", "gt"] + cand_names,
            [sp_w, 5] + [col_w] * len(cand_names),
        )
    )
    lines.append("-" * (sp_w + 5 + (col_w + 2) * len(cand_names)))
    for gt_sp in gt_speakers_sorted:
        row = [gt_sp[:sp_w], str(gt_stats[gt_sp]["turns"])]
        for name in cand_names:
            # zsumuj turns w kandydatach które mapują się na tego GT speakera
            n = sum(
                speaker_stats(candidates[name])[m.cand_speaker]["turns"]
                for m in cand_mappings[name]
                if m.gt_speaker == gt_sp
            )
            row.append(str(n) if n else "·")
        lines.append(_fmt_row(row, [sp_w, 5] + [col_w] * len(cand_names)))
    lines.append("")

    lines.append("=" * 78)
    lines.append("TALK TIME PER OSOBA (sekundy)")
    lines.append("=" * 78)
    lines.append(
        _fmt_row(
            ["real speaker", "gt"] + cand_names,
            [sp_w, 7] + [col_w] * len(cand_names),
        )
    )
    lines.append("-" * (sp_w + 7 + (col_w + 2) * len(cand_names)))
    for gt_sp in gt_speakers_sorted:
        row = [gt_sp[:sp_w], f"{gt_stats[gt_sp]['talk_s']:.0f}"]
        for name in cand_names:
            secs = sum(
                speaker_stats(candidates[name])[m.cand_speaker]["talk_s"]
                for m in cand_mappings[name]
                if m.gt_speaker == gt_sp
            )
            row.append(f"{secs:.0f}" if secs > 0 else "·")
        lines.append(_fmt_row(row, [sp_w, 7] + [col_w] * len(cand_names)))
    lines.append("")

    # Audyt mappingu — co dokładnie z czym zostało zmergowane
    lines.append("=" * 78)
    lines.append("MAPPING AUDIT (każdy cand speaker → GT speaker po overlap)")
    lines.append("=" * 78)
    for name, maps in cand_mappings.items():
        lines.append(f"\n--- {name} ---")
        for m in maps:
            if m.cand_total_s == 0:
                continue
            lead = f"  {m.cand_speaker:<22} talk={m.cand_total_s:6.1f}s → "
            if m.gt_speaker is None:
                lines.append(lead + "(brak overlap)")
                continue
            pct_cand = m.overlap_s / m.cand_total_s * 100 if m.cand_total_s else 0
            pct_gt = m.overlap_s / m.gt_total_s * 100 if m.gt_total_s else 0
            lines.append(
                lead
                + f"{m.gt_speaker[:32]:<32} "
                + f"overlap={m.overlap_s:6.1f}s "
                + f"({pct_cand:.0f}% cand, {pct_gt:.0f}% gt)"
            )
            if len(m.breakdown) > 1:
                rest = ", ".join(f"{sp[:20]}={ov:.0f}s" for sp, ov in m.breakdown[1:])
                lines.append(f"      also overlaps: {rest}")

    return "\n".join(lines) + "\n"
