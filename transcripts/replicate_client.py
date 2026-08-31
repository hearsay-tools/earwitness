"""Klient do diaryzacji w cloudzie przez Replicate.

Cel: wystawić uniwersalny `diarize_cloud(audio_path) -> list[(speaker, start, end)]`
żeby istniejący `hybrid` mógł wziąć diaryzację z dowolnego źródła (lokalny
MacWhisper-Parakeet → cloud pyannote na Replicate).

Uwaga: Replicate ma kilka wariantów pyannote 3.x. Domyślnie używamy
`collectiveai-team/speaker-diarization-3`. Schema input/output różni się
między forkami — kod próbuje robust parser, jeśli format nie pasuje rzuca
wyjątek z pełnym responsem żeby było widać co przyszło.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_MODEL = "collectiveai-team/speaker-diarization-3"
DEFAULT_ASR_MODEL = "victor-upmeet/whisperx"


# Konfigi diaryzatorów dostępnych przez Replicate. Backend wybierany flagą
# `--diarizer` w `pipeline`. Łatwo dodać kolejny dostawcę: dorzuć wpis tutaj
# (model + nazwa pola input dla audio) i CLI go zobaczy automatycznie.
DIARIZER_BACKENDS: dict[str, dict] = {
    "nemo": {
        "model": "thomasmol/whisper-diarization",
        "audio_field": "file",
        "description": (
            "NeMo MSDD (NVIDIA) — stabilny dla 2-6 mówców, twardo trzyma "
            "num_speakers, mniej over-clusteringu niż pyannote 3.x."
        ),
    },
    "pyannote": {
        "model": "collectiveai-team/speaker-diarization-3",
        "audio_field": "audio",
        "description": (
            "pyannote 3.x — dobry dla 6+ mówców i czystego audio, ale "
            "over-segmentuje na Teams/Zoom (zmienne mikrofony, cross-talk)."
        ),
    },
}
DEFAULT_DIARIZER = "nemo"


@dataclass
class DiarSegment:
    speaker: str
    start: float
    end: float


def diarize_cloud(
    audio_path: Path,
    model: str = DEFAULT_MODEL,
    num_speakers: Optional[int] = None,
    audio_field: str = "audio",
    poll_seconds: float = 3.0,
) -> list[DiarSegment]:
    """Zlec diaryzację na Replicate. Zwraca listę segmentów.

    Używa explicit predictions.create + polling zamiast replicate.run(),
    bo długie audio (>5 min) przekracza domyślny HTTP timeout.
    """
    import time

    import replicate

    if not os.environ.get("REPLICATE_API_TOKEN"):
        raise RuntimeError(
            "Ustaw REPLICATE_API_TOKEN w env lub .env (https://replicate.com/account/api-tokens)"
        )

    if ":" in model:
        version_id = model.split(":", 1)[1]
    else:
        m = replicate.models.get(model)
        version_id = m.latest_version.id

    inputs: dict = {audio_field: open(audio_path, "rb")}
    if num_speakers is not None:
        inputs["num_speakers"] = num_speakers

    prediction = replicate.predictions.create(version=version_id, input=inputs)
    print(
        f"  prediction id={prediction.id} status={prediction.status} "
        f"(polling every {poll_seconds}s)..."
    )

    while prediction.status not in ("succeeded", "failed", "canceled"):
        time.sleep(poll_seconds)
        prediction.reload()
        print(f"  status={prediction.status}")

    if prediction.status != "succeeded":
        raise RuntimeError(
            f"Replicate prediction {prediction.status}: {prediction.error}"
        )

    return _normalize(prediction.output)


def _normalize(raw) -> list[DiarSegment]:
    """Pyannote-na-Replicate forki zwracają różnie. Próbujemy:
    - lista dictów [{speaker, start, end}, ...]
    - dict {"segments": [...]}, {"diarization": [...]}, {"output": [...]}
    - URL do pliku JSON / RTTM (replicate FileOutput)
    - tekst RTTM
    """
    # Replicate FileOutput / iterator
    if hasattr(raw, "read"):
        text = raw.read()
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        return _parse_text(text)

    if isinstance(raw, str):
        if raw.startswith("http"):
            import urllib.request

            with urllib.request.urlopen(raw) as f:
                return _parse_text(f.read().decode("utf-8"))
        return _parse_text(raw)

    if isinstance(raw, dict):
        for key in ("segments", "diarization", "output", "speakers"):
            if key in raw and isinstance(raw[key], list):
                return _from_list(raw[key])
        # może jest URL pod jakimś polem
        for v in raw.values():
            if isinstance(v, str) and v.startswith("http"):
                import urllib.request

                with urllib.request.urlopen(v) as f:
                    return _parse_text(f.read().decode("utf-8"))
        raise ValueError(f"Nieznany format dict z Replicate: keys={list(raw.keys())}")

    if isinstance(raw, list):
        return _from_list(raw)

    raise ValueError(f"Nieznany typ output Replicate: {type(raw).__name__} → {raw!r}")


def _to_seconds(v) -> float:
    """Akceptuje float, int, '1.5', '0:00:01.604414', '00:01:30'."""
    if v is None:
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            from .comparison import parse_timestamp

            return parse_timestamp(v)
    return 0.0


def _from_list(items: list) -> list[DiarSegment]:
    out: list[DiarSegment] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        sp = it.get("speaker") or it.get("label") or it.get("speaker_id") or "SPEAKER_?"
        start = _to_seconds(it.get("start") or it.get("start_time"))
        end = _to_seconds(it.get("end") or it.get("stop") or it.get("end_time"))
        if end < start:
            end = start
        out.append(DiarSegment(speaker=str(sp), start=start, end=end))
    out.sort(key=lambda s: s.start)
    return out


def _parse_text(text: str) -> list[DiarSegment]:
    text = text.strip()
    # JSON?
    if text.startswith(("[", "{")):
        data = json.loads(text)
        if isinstance(data, dict):
            for key in ("segments", "diarization", "output"):
                if key in data and isinstance(data[key], list):
                    return _from_list(data[key])
            raise ValueError(f"Nieznany format JSON: keys={list(data.keys())}")
        return _from_list(data)
    # RTTM:  SPEAKER file 1 START DUR <NA> <NA> SPEAKER_00 <NA> <NA>
    out: list[DiarSegment] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 8 and parts[0] == "SPEAKER":
            start = float(parts[3])
            duration = float(parts[4])
            sp = parts[7]
            out.append(DiarSegment(speaker=sp, start=start, end=start + duration))
    out.sort(key=lambda s: s.start)
    return out


def transcribe_cloud(
    audio_path: Path,
    model: str = DEFAULT_ASR_MODEL,
    language: str = "english",
    audio_field: str = "audio",
    poll_seconds: float = 5.0,
) -> dict:
    """Zlec ASR na Replicate. Zwraca dict z polami zgodnymi z formatem
    ElevenLabs raw response: {"words": [{text, start, end, type}, ...]}.
    Dzięki temu wynik można podać do `hybrid --elevenlabs-raw`.
    """
    import time

    import replicate

    if not os.environ.get("REPLICATE_API_TOKEN"):
        raise RuntimeError("Ustaw REPLICATE_API_TOKEN w env lub .env")

    if ":" in model:
        version_id = model.split(":", 1)[1]
    else:
        m = replicate.models.get(model)
        version_id = m.latest_version.id

    # WhisperX vs fast-whisper mają inne nazwy pól
    if "whisperx" in model.lower():
        # WhisperX: ISO 2-letter language code, align_output dla word-level
        lang_iso = language[:2] if language and language != "english" else "en"
        inputs = {
            "audio_file": open(audio_path, "rb"),
            "language": lang_iso,
            "align_output": True,
            "diarization": False,
            "batch_size": 8,
        }
    else:
        inputs = {
            audio_field: open(audio_path, "rb"),
            "language": language,
            "timestamp": "word",
            "diarise_audio": False,
            "batch_size": 8,
        }

    prediction = replicate.predictions.create(version=version_id, input=inputs)
    print(
        f"  prediction id={prediction.id} status={prediction.status} "
        f"(polling every {poll_seconds}s)..."
    )
    while prediction.status not in ("succeeded", "failed", "canceled"):
        time.sleep(poll_seconds)
        prediction.reload()
        print(f"  status={prediction.status}")

    if prediction.status != "succeeded":
        raise RuntimeError(
            f"Replicate prediction {prediction.status}: {prediction.error}"
        )

    return _normalize_asr(prediction.output)


def _normalize_asr(raw) -> dict:
    """Normalizuje output różnych ASR modeli na format ElevenLabs raw response.

    WhisperX zwraca: {segments: [{text, start, end, words: [{word, start, end}]}]}
    fast-whisper:    {text, chunks: [{text, timestamp: [s, e]}]}
    """
    if hasattr(raw, "read"):
        raw_text = raw.read()
        if isinstance(raw_text, bytes):
            raw_text = raw_text.decode("utf-8")
        raw = json.loads(raw_text)
    if isinstance(raw, str):
        raw = json.loads(raw)

    words: list[dict] = []

    # WhisperX: segments[].words[] z polami {word, start, end}
    segments = raw.get("segments") or []
    if segments and isinstance(segments[0], dict) and segments[0].get("words"):
        for seg in segments:
            for w in seg.get("words") or []:
                text = w.get("word") or w.get("text") or ""
                if not text.strip():
                    continue
                start = _to_seconds(w.get("start"))
                end = _to_seconds(w.get("end") or start)
                words.append(
                    {
                        "text": text if not words else " " + text.lstrip(),
                        "start": start,
                        "end": end,
                        "type": "word",
                    }
                )
    else:
        # fallback: chunks/segments na poziomie segmentu (bez word-level)
        items = raw.get("chunks") or segments or raw.get("words") or []
        for item in items:
            text = (item.get("text") or item.get("word") or "").strip()
            if not text:
                continue
            ts = item.get("timestamp")
            if isinstance(ts, list) and len(ts) >= 2:
                start, end = ts[0], ts[1]
            else:
                start = item.get("start") or 0.0
                end = item.get("end") or item.get("stop") or start
            words.append(
                {
                    "text": " " + text if words else text,
                    "start": _to_seconds(start),
                    "end": _to_seconds(end),
                    "type": "word",
                }
            )

    return {
        "text": raw.get("text", ""),
        "words": words,
        "language_code": raw.get("language") or raw.get("detected_language", ""),
    }


def transcribe_and_diarize(
    audio_path: Path,
    num_speakers: Optional[int] = None,
    language: Optional[str] = None,
    prompt: Optional[str] = None,
    model: str = "thomasmol/whisper-diarization",
    poll_seconds: float = 5.0,
) -> dict:
    """All-in-one ASR + diaryzacja w jednym API calle (thomasmol/whisper-diarization).

    Zwraca surowy response: {language, num_speakers, segments: [{speaker, start,
    end, text, words?}, ...]} — bez merge'u, bo Whisper i NeMo MSDD lecą razem
    i tekst jest już przypisany do speakerów.

    `prompt` to vocabulary hint dla Whispera — lista nazw własnych/akronimów
    z interpunkcją (np. 'Acme, Anna Nowak, Jan Kowalski.').
    """
    import time

    import replicate

    if not os.environ.get("REPLICATE_API_TOKEN"):
        raise RuntimeError("Ustaw REPLICATE_API_TOKEN w env lub .env")

    if ":" in model:
        version_id = model.split(":", 1)[1]
    else:
        m = replicate.models.get(model)
        version_id = m.latest_version.id

    inputs: dict = {"file": open(audio_path, "rb")}
    if num_speakers is not None:
        inputs["num_speakers"] = num_speakers
    if language:
        inputs["language"] = language
    if prompt:
        inputs["prompt"] = prompt

    prediction = replicate.predictions.create(version=version_id, input=inputs)
    print(
        f"  prediction id={prediction.id} status={prediction.status} "
        f"(polling every {poll_seconds}s)..."
    )
    while prediction.status not in ("succeeded", "failed", "canceled"):
        time.sleep(poll_seconds)
        prediction.reload()
        print(f"  status={prediction.status}")

    if prediction.status != "succeeded":
        raise RuntimeError(
            f"Replicate prediction {prediction.status}: {prediction.error}"
        )

    return prediction.output


def save_as_timeline(
    segments: list[DiarSegment],
    output_path: Path,
) -> None:
    """Zapisz segments w formacie zgodnym z Recall.ai speaker-timeline.json,
    żeby `hybrid --ground-truth` lub `compare --timeline` mogły go wczytać.
    """
    timeline = [
        {
            "participant": {"id": i, "name": s.speaker},
            "start_timestamp": {"relative": s.start},
            "end_timestamp": {"relative": s.end},
        }
        for i, s in enumerate(segments)
    ]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(timeline, indent=2), encoding="utf-8")
