from dataclasses import dataclass
from typing import Iterable


@dataclass
class Utterance:
    speaker_id: str | None
    start: float
    text: str


def format_seconds(seconds: float) -> str:
    total = int(seconds)
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def group_by_speaker(words: Iterable) -> list[Utterance]:
    """Łączy kolejne słowa tego samego rozmówcy w wypowiedzi.

    Zmiana rozmówcy jest wykrywana tylko na słowach typu "word"; tokeny
    "spacing" doklejają się do bieżącej wypowiedzi (zachowują białe znaki),
    a "audio_event" są pomijane.
    """
    utterances: list[Utterance] = []
    current: Utterance | None = None

    for w in words:
        wtype = getattr(w, "type", "word")
        text = getattr(w, "text", "") or ""
        speaker = getattr(w, "speaker_id", None)
        start = getattr(w, "start", None) or 0.0

        if wtype == "audio_event":
            continue

        if wtype == "spacing":
            if current is not None:
                current.text += text
            continue

        if current is None or current.speaker_id != speaker:
            if current is not None:
                utterances.append(current)
            current = Utterance(speaker_id=speaker, start=start, text=text)
        else:
            current.text += text

    if current is not None:
        utterances.append(current)

    return utterances


def _label_for(speaker_id: str | None, mapping: dict[str | None, int]) -> str:
    if speaker_id not in mapping:
        mapping[speaker_id] = len(mapping) + 1
    return f"Rozmówca {mapping[speaker_id]}"


def format_transcript(words: Iterable) -> str:
    utterances = group_by_speaker(words)
    label_map: dict[str | None, int] = {}
    lines = []
    for u in utterances:
        label = _label_for(u.speaker_id, label_map)
        ts = format_seconds(u.start)
        text = u.text.strip()
        lines.append(f"{label} [{ts}] {text}")
    return "\n".join(lines)
