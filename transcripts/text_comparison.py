"""Porównanie jakości samego tekstu transkrypcji (bez diaryzacji).

Bez tekstowego ground-truth liczymy:
- statystyki sumaryczne (słowa, unique, fillery, WPM),
- pairwise Jaccard na zbiorach słów (proxy dla "kto z kim się zgadza"),
- słowa unikalne dla każdego źródła (potencjalne halucynacje albo wyłapane fragmenty),
- side-by-side fragmenty do oceny ludzkim okiem.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .comparison import Utterance

_WORD_RE = re.compile(r"[\w']+", re.UNICODE)

# rozszerzony zestaw fillerów / minimal-content dla EN i PL
_FILLERS = {
    "uh",
    "um",
    "ah",
    "eh",
    "er",
    "hmm",
    "mhm",
    "uhm",
    "yeah",
    "yep",
    "yup",
    "ok",
    "okay",
    "right",
    "alright",
    "tak",
    "no",
    "yhm",
    "eee",
    "yyy",
}


def tokenize(text: str) -> list[str]:
    return [w.lower() for w in _WORD_RE.findall(text)]


def join_text(utterances: list[Utterance]) -> str:
    return " ".join(
        u.text.strip()
        for u in sorted(utterances, key=lambda u: u.start)
        if u.text and u.text.strip()
    )


@dataclass
class TextStats:
    chars: int
    words: int
    unique_words: int
    fillers: int
    duration_s: float
    word_counts: Counter

    @property
    def wpm(self) -> float:
        return self.words / (self.duration_s / 60) if self.duration_s > 0 else 0

    @property
    def filler_pct(self) -> float:
        return self.fillers / self.words * 100 if self.words else 0


def text_stats(utterances: list[Utterance]) -> TextStats:
    text = join_text(utterances)
    words = tokenize(text)
    counts = Counter(words)
    duration = max((u.end for u in utterances), default=0.0)
    return TextStats(
        chars=len(text),
        words=len(words),
        unique_words=len(counts),
        fillers=sum(c for w, c in counts.items() if w in _FILLERS),
        duration_s=duration,
        word_counts=counts,
    )


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def extract_window(
    utterances: list[Utterance],
    start_s: float,
    end_s: float,
) -> list[Utterance]:
    return [u for u in utterances if u.start < end_s and u.end > start_s]


def _hms(s: float) -> str:
    s = int(s)
    return f"{s // 60:02d}:{s % 60:02d}"


def format_text_report(
    candidates: dict[str, list[Utterance]],
    windows: list[tuple[float, float]] | None = None,
    unique_top_n: int = 12,
) -> str:
    lines: list[str] = []
    names = list(candidates.keys())
    stats = {n: text_stats(utts) for n, utts in candidates.items()}

    # 1. Statystyki sumaryczne
    lines.append("=" * 78)
    lines.append("STATYSTYKI TEKSTU")
    lines.append("=" * 78)
    header = ["source", "chars", "words", "unique", "fillers", "filler%", "wpm"]
    widths = [22, 7, 7, 7, 8, 8, 6]
    lines.append(_row(header, widths))
    lines.append("-" * (sum(widths) + 2 * (len(widths) - 1)))
    for n in names:
        s = stats[n]
        lines.append(
            _row(
                [
                    n,
                    f"{s.chars}",
                    f"{s.words}",
                    f"{s.unique_words}",
                    f"{s.fillers}",
                    f"{s.filler_pct:.1f}%",
                    f"{s.wpm:.0f}",
                ],
                widths,
            )
        )
    lines.append("")

    # 2. Pairwise Jaccard
    lines.append("=" * 78)
    lines.append("PAIRWISE JACCARD (zbiór unikalnych słów)")
    lines.append("Wyższe = więcej wspólnego słownictwa")
    lines.append("=" * 78)
    word_sets = {n: set(stats[n].word_counts.keys()) for n in names}
    short = [_short(n) for n in names]
    cell_w = max(7, max(len(s) for s in short))
    head_w = 16
    lines.append(" " * head_w + "  ".join(s.rjust(cell_w) for s in short))
    for i, n in enumerate(names):
        row_cells = []
        for j, m in enumerate(names):
            if i == j:
                row_cells.append("—".rjust(cell_w))
            else:
                v = jaccard(word_sets[n], word_sets[m])
                row_cells.append(f"{v:.3f}".rjust(cell_w))
        lines.append(_short(n).ljust(head_w) + "  ".join(row_cells))
    lines.append("")

    # 3. Unikalne słowa (tylko w tym źródle)
    lines.append("=" * 78)
    lines.append(f"SŁOWA UNIKALNE DLA ŹRÓDŁA (top {unique_top_n} po częstości)")
    lines.append(
        "Wskazują na halucynacje albo na słowa wyłapane TYLKO przez ten silnik"
    )
    lines.append("=" * 78)
    for n in names:
        only_here = word_sets[n] - set().union(*(word_sets[m] for m in names if m != n))
        top = sorted(only_here, key=lambda w: -stats[n].word_counts[w])[:unique_top_n]
        if top:
            display = ", ".join(f"{w}×{stats[n].word_counts[w]}" for w in top)
        else:
            display = "(brak)"
        lines.append(f"  {n}: {display}")
    lines.append("")

    # 4. Side-by-side fragmentów
    if windows:
        lines.append("=" * 78)
        lines.append("FRAGMENTY SIDE-BY-SIDE")
        lines.append("=" * 78)
        for start, end in windows:
            lines.append(f"\n### {_hms(start)}–{_hms(end)}")
            for n in names:
                lines.append(f"\n--- {n} ---")
                window = extract_window(candidates[n], start, end)
                if not window:
                    lines.append("  (brak)")
                    continue
                for u in sorted(window, key=lambda u: u.start):
                    text = u.text.strip().replace("\n", " ")
                    if len(text) > 200:
                        text = text[:197] + "..."
                    lines.append(f"  [{_hms(u.start)}] {u.speaker[:24]:<24} {text}")

    return "\n".join(lines) + "\n"


def _row(cells: list[str], widths: list[int]) -> str:
    return "  ".join(
        c.ljust(w) if i == 0 else c.rjust(w)
        for i, (c, w) in enumerate(zip(cells, widths))
    )


def _short(name: str) -> str:
    return name.split(":", 1)[1] if ":" in name else name
