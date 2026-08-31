"""Diaryzacja przez energię izolowanych kanałów (Recall audio_separate).

Nasz DEFAULT (od 2026-07-10). Idea: audio_separate
z Recall to osobne strumienie sieciowe per uczestnik — zero przesłuchu. Każde
słowo z mixed-audio transkryptu (per-word timestamps) przypisujemy do
uczestnika w trzech krokach:

1. energia RMS kanałów w oknie słowa (emisja),
2. Viterbi z karą za zmianę mówcy zależną od pauzy (wygładza flipy graniczne),
3. słowa sporne energetycznie rozstrzyga mini-ASR wycinków z izolowanych
   kanałów kandydatów (ElevenLabs, wycinki 1-3 s), a potwierdzenia wchodzą
   jako boost emisji do drugiego przebiegu Viterbi.

Dodatkowo odzysk słów cichszego mówcy przy overlapie (mixed ASR transkrybuje
wtedy tylko dominującego): okna, w których kanał ma ciągłą energię mowy, ale
zero przypisanych słów, idą do mini-ASR izolowanego kanału; wynik po dedupie
względem mixed wchodzi jako osobne wypowiedzi (patrz _recover_missing_speech).

Deterministyczne, bez ML/LLM w głównym torze. Zwalidowane na dwóch
wewnętrznych nagraniach (krótkie PL + dłuższe PL/EN).
"""

from __future__ import annotations

import array
import io
import json
import math
import os
import re
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

SR = 16000  # audio_separate raw = s16le 16 kHz mono
FRAME = 160  # 10 ms
WORD_PAD = 0.05  # s — margines okna energii wokół słowa
NOISE_FLOOR = 60.0  # RMS int16; poniżej = cisza
OVERLAP_RATIO = 0.5  # second/top powyżej progu -> słowo sporne
UTT_GAP = 1.5  # s — nowa wypowiedź przy dłuższej pauzie
TIEBREAK_PAD = 0.6  # s — margines wycinka do mini-ASR
TIEBREAK_MIN_TOKEN = 3  # krótkie tokeny ("i", "to") są w wielu kanałach

# odzysk słów cichszego mówcy (okna z energią kanału bez przypisanych słów)
RECOVERY_MIN_DUR = 0.2  # s ciągłej energii mowy -> kandydat na okno odzysku
# (backchannele "Sure"/"Okay" to bursty 0.2-0.5 s)
RECOVERY_MAX_GAP = 0.25  # s dziury w energii tolerowanej wewnątrz przebiegu
RECOVERY_JOIN_GAP = 1.5  # s — łączenie sąsiednich okien tego samego mówcy
# (mniej klipów, dłuższy kontekst = stabilniejszy mini-ASR)
RECOVERY_PAD = 0.6  # s — margines wycinka do mini-ASR
RECOVERY_MATCH_TOL = 1.0  # s — dedup: to samo słowo tego samego mówcy w tej odległości
RECOVERY_MAX_WPS = 5.0  # walidacja mini-ASR: więcej słów/s = halucynacja, odrzuć

# mini-ASR (tiebreak + recovery): równoległość i retry. STT ma wyższe limity
# concurrency niż TTS (10+ na niskich planach) — 8 to bezpieczny default.
ASR_RETRIES = 3


def _asr_concurrency() -> int:
    # czytane per-call, nie przy imporcie — load_dotenv() w cli.run() odpala
    # się już po imporcie tego modułu, więc stała module-level nie widziałaby .env
    return int(os.environ.get("ELEVENLABS_STT_CONCURRENCY", "8"))


@dataclass
class Utterance:
    speaker: str
    start: float
    end: float
    text: str


@dataclass
class _Part:
    name: str
    start: float
    duration: float
    path: Path


def _hms(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _norm(w: str) -> str:
    return re.sub(r"[^\w]+", "", w, flags=re.UNICODE).casefold()


def _safe_label(pid: int, name: str) -> str:
    # musi być zgodne z recall_client._safe_filename
    return f"{pid}-{re.sub(r'[^\w.\-]+', '_', (name or 'unknown').strip())[:80]}"


# ---------- kanały ----------


def load_parts(rec_dir: Path) -> list[_Part]:
    """Party raw z katalogu nagrania (recall-fetch). Wybiera artefakt format=raw
    z recording.json i czyta jego manifest parts_<id8>.json."""
    meta = json.loads((rec_dir / "recording.json").read_text())
    raw_artifacts = [
        a for a in meta.get("audio_separate", []) if a.get("format") == "raw"
    ]
    if not raw_artifacts:
        raise FileNotFoundError(
            f"{rec_dir}: brak artefaktu audio_separate w formacie raw "
            "(pobierz przez recall-fetch)"
        )
    manifest_path = (
        rec_dir / "audio_separate" / f"parts_{raw_artifacts[0]['id'][:8]}.json"
    )
    manifest = json.loads(manifest_path.read_text())
    parts: list[_Part] = []
    for m in manifest:
        p = m["participant"]
        path = (
            rec_dir
            / "audio_separate"
            / _safe_label(p.get("id") or 0, p.get("name") or "unknown")
            / f"{m['id']}.raw"
        )
        if not path.exists():
            print(f"WARN: brak pliku parta {path}", file=sys.stderr)
            continue
        parts.append(
            _Part(
                name=p.get("name") or "unknown",
                start=(m.get("start_timestamp") or {}).get("relative", 0.0),
                duration=m.get("duration") or 0.0,
                path=path,
            )
        )
    if not parts:
        raise FileNotFoundError(f"{rec_dir}: manifest bez dostępnych plików raw")
    return parts


def build_envelopes(parts: list[_Part], max_sec: float) -> dict[str, list[float]]:
    """speaker -> RMS per 10 ms frame dla [0, max_sec]."""
    n_frames = int(max_sec * SR / FRAME) + 1
    env: dict[str, list[float]] = {}
    for part in parts:
        if part.start >= max_sec:
            continue
        env.setdefault(part.name, [0.0] * n_frames)
        need_sec = min(part.duration, max_sec - part.start)
        raw = part.path.read_bytes()[: int(need_sec * SR) * 2]
        samples = array.array("h")
        samples.frombytes(raw[: len(raw) // 2 * 2])
        base_frame = int(part.start * SR / FRAME)
        row = env[part.name]
        for fi in range(len(samples) // FRAME):
            gi = base_frame + fi
            if gi >= n_frames:
                break
            chunk = samples[fi * FRAME : (fi + 1) * FRAME]
            acc = 0
            for v in chunk:
                acc += v * v
            row[gi] = math.sqrt(acc / FRAME)
    return env


def _word_energy(env: list[float], start: float, end: float) -> float:
    lo = max(0, int((start - WORD_PAD) * SR / FRAME))
    hi = min(len(env), int((end + WORD_PAD) * SR / FRAME) + 1)
    if hi <= lo:
        return 0.0
    window = env[lo:hi]
    return sum(window) / len(window)


def _channel_wav(
    parts: list[_Part], speaker: str, start: float, end: float
) -> Optional[bytes]:
    for part in parts:
        if part.name != speaker:
            continue
        if part.start <= start and end <= part.start + part.duration + 0.5:
            off = max(0.0, start - part.start)
            dur = min(end - start, part.duration - off)
            lo = int(off * SR) * 2
            with open(part.path, "rb") as f:
                f.seek(lo)
                pcm = f.read(int(dur * SR) * 2)
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SR)
                w.writeframes(pcm)
            return buf.getvalue()
    return None


# ---------- przypisanie ----------


def _switch_penalty(gap: float) -> float:
    if gap < 0.35:
        return 4.0  # środek frazy — flip musi być mocno uzasadniony
    if gap < 0.8:
        return 2.0
    return 0.5  # naturalna pauza — zmiana niemal darmowa


def _viterbi(
    words: list[dict],
    env: dict[str, list[float]],
    boosts: Optional[dict[int, str]] = None,
) -> list[str]:
    speakers = list(env)
    emis = [
        {
            s: math.log(_word_energy(env[s], w["start"], w["end"]) + 1.0)
            for s in speakers
        }
        for w in words
    ]
    for i, s in (boosts or {}).items():
        emis[i][s] += 10.0
    score = {s: emis[0][s] for s in speakers}
    back: list[dict[str, str]] = []
    for i in range(1, len(words)):
        gap = max(0.0, words[i]["start"] - words[i - 1]["end"])
        pen = _switch_penalty(gap)
        new_score: dict[str, float] = {}
        bp: dict[str, str] = {}
        for s in speakers:
            best_prev = max(speakers, key=lambda p: score[p] - (pen if p != s else 0.0))
            new_score[s] = (
                score[best_prev] - (pen if best_prev != s else 0.0) + emis[i][s]
            )
            bp[s] = best_prev
        score = new_score
        back.append(bp)
    last = max(score, key=score.get)
    path = [last]
    for bp in reversed(back):
        path.append(bp[path[-1]])
    return path[::-1]


def _flag_disputed(
    words: list[dict], env: dict[str, list[float]]
) -> list[tuple[int, str, str]]:
    out = []
    for i, w in enumerate(words):
        ranked = sorted(
            ((s, _word_energy(e, w["start"], w["end"])) for s, e in env.items()),
            key=lambda kv: -kv[1],
        )
        if len(ranked) < 2:
            continue
        (top, te), (sec, se) = ranked[0], ranked[1]
        if te >= NOISE_FLOOR and se / te > OVERLAP_RATIO:
            out.append((i, top, sec))
    return out


def _convert_with_retry(client, **kwargs):
    """client.speech_to_text.convert z retry + backoff (429 przy równoległości)."""
    for attempt in range(ASR_RETRIES):
        try:
            return client.speech_to_text.convert(**kwargs)
        except Exception:  # noqa: BLE001 — ostatnia próba propaguje
            if attempt == ASR_RETRIES - 1:
                raise
            time.sleep(1.5 * (attempt + 1))


def _tiebreak(
    words: list[dict],
    disputed: list[tuple[int, str, str]],
    parts: list[_Part],
    api_key: str,
    language: Optional[str],
    model_id: str,
) -> dict[int, str]:
    """Sporne przebiegi: sprawdź, w którym izolowanym kanale słowa faktycznie
    są (mini-ASR wycinka). Zwraca {index: mówca} jako boosty dla Viterbi #2."""
    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=api_key)

    def asr(wav: bytes) -> str:
        kwargs = dict(
            file=("clip.wav", wav),
            model_id=model_id,
            diarize=False,
            tag_audio_events=False,
            timestamps_granularity="none",
        )
        if language:
            kwargs["language_code"] = language
        resp = _convert_with_retry(client, **kwargs)
        return (resp.text or "").strip()

    runs: list[list[tuple[int, str, str]]] = []
    for item in disputed:
        if runs and item[0] == runs[-1][-1][0] + 1:
            runs[-1].append(item)
        else:
            runs.append([item])

    # mini-ASR per (run, kandydat) — niezależne wycinki, lecą równolegle
    jobs: list[tuple[int, str, float, float]] = []
    for ri, run in enumerate(runs):
        idxs = [i for i, _, _ in run]
        cands = {c for _, t, s in run for c in (t, s)}
        w_start = max(0.0, words[idxs[0]]["start"] - TIEBREAK_PAD)
        w_end = words[idxs[-1]]["end"] + TIEBREAK_PAD
        jobs.extend((ri, c, w_start, w_end) for c in cands)

    def work(job: tuple[int, str, float, float]):
        ri, c, w_start, w_end = job
        wav = _channel_wav(parts, c, w_start, w_end)
        if wav is None:
            return ri, c, None
        try:
            return ri, c, {_norm(t) for t in asr(wav).split()}
        except Exception as e:  # noqa: BLE001 — tiebreak jest best-effort
            print(f"WARN: tiebreak ASR fail ({c}): {e}", file=sys.stderr)
            return ri, c, None

    texts_by_run: list[dict[str, set[str]]] = [{} for _ in runs]
    with ThreadPoolExecutor(max_workers=_asr_concurrency()) as ex:
        for ri, c, tokens in ex.map(work, jobs):
            if tokens is not None:
                texts_by_run[ri][c] = tokens

    confirmed: dict[int, str] = {}
    for ri, run in enumerate(runs):
        texts = texts_by_run[ri]
        for i, _, _ in run:
            token = _norm(words[i]["text"])
            if len(token) < TIEBREAK_MIN_TOKEN:
                continue
            holders = [c for c, ws in texts.items() if token in ws]
            if len(holders) == 1:
                confirmed[i] = holders[0]
    return confirmed


# ---------- odzysk słów cichszego mówcy ----------


def _active_runs(row: list[float]) -> list[tuple[float, float]]:
    """Przebiegi energii > NOISE_FLOOR (>= RECOVERY_MIN_DUR, dziury do
    RECOVERY_MAX_GAP tolerowane)."""
    fs = FRAME / SR
    runs: list[tuple[float, float]] = []
    start: Optional[float] = None
    last_active = 0.0
    for i, v in enumerate(row):
        t = i * fs
        if v >= NOISE_FLOOR:
            if start is None:
                start = t
            last_active = t
        elif start is not None and t - last_active > RECOVERY_MAX_GAP:
            if last_active + fs - start >= RECOVERY_MIN_DUR:
                runs.append((start, last_active + fs))
            start = None
    if start is not None and last_active + fs - start >= RECOVERY_MIN_DUR:
        runs.append((start, last_active + fs))
    return runs


def find_recovery_windows(
    words: list[dict],
    assigned: list[str],
    env: dict[str, list[float]],
) -> list[tuple[str, float, float]]:
    """Okna (speaker, t0, t1), w których kanał mówcy ma ciągłą energię mowy,
    a transkrypt nie zawiera żadnego słowa przypisanego temu mówcy — czyli
    mowa najpewniej zagłuszona w mixed ASR przez dominującego mówcę."""
    by_speaker: dict[str, list[tuple[float, float]]] = {}
    for s, w in zip(assigned, words):
        by_speaker.setdefault(s, []).append((w["start"], w["end"]))

    def has_own_words(s: str, t0: float, t1: float) -> bool:
        return any(ws < t1 + 0.1 and we > t0 - 0.1 for ws, we in by_speaker.get(s, ()))

    windows: list[tuple[str, float, float]] = []
    for s, row in env.items():
        empty = [
            (t0, t1) for t0, t1 in _active_runs(row) if not has_own_words(s, t0, t1)
        ]
        merged: list[list[float]] = []
        for t0, t1 in empty:
            if (
                merged
                and t0 - merged[-1][1] <= RECOVERY_JOIN_GAP
                and not has_own_words(s, merged[-1][1], t0)
            ):
                merged[-1][1] = t1
            else:
                merged.append([t0, t1])
        windows.extend((s, t0, t1) for t0, t1 in merged)
    windows.sort(key=lambda w: w[1])
    return windows


def _recover_missing_speech(
    words: list[dict],
    assigned: list[str],
    windows: list[tuple[str, float, float]],
    parts: list[_Part],
    api_key: str,
    language: Optional[str],
    model_id: str,
) -> tuple[list[Utterance], dict]:
    """Mini-ASR okien odzysku na izolowanych kanałach. Dedup: odpada słowo,
    które w mixed jest już przypisane TEMU SAMEMU mówcy w RECOVERY_MATCH_TOL
    (klip z padem łapie brzegi jego sąsiednich, transkrybowanych wypowiedzi).
    Match z innym mówcą NIE jest duplikatem — kanał izolowany nie ma
    przesłuchu, więc to dowód, że mixed wchłonął słowo cichszego mówcy do
    wypowiedzi dominującego (np. backchannel w monologu).
    Zwraca odzyskane wypowiedzi + statystyki."""
    from elevenlabs.client import ElevenLabs

    client = ElevenLabs(api_key=api_key)
    mixed_index = [(_norm(w["text"]), w["start"], s) for w, s in zip(words, assigned)]

    def already_in_mixed(speaker: str, token: str, start: float) -> bool:
        return any(
            n == token and s == speaker and abs(ms - start) <= RECOVERY_MATCH_TOL
            for n, ms, s in mixed_index
        )

    stats = {
        "recovery_windows": len(windows),
        "recovery_asr_seconds": 0.0,
        "recovery_words": 0,
        "recovery_dropped_dup": 0,
        "recovery_dropped_invalid": 0,
    }

    def work(window: tuple[str, float, float]) -> dict:
        """Jedno okno: wycinek + mini-ASR + walidacja + dedup. Bez stanu
        współdzielonego (mixed_index jest read-only) — bezpieczne równolegle."""
        speaker, t0, t1 = window
        clip_start = max(0.0, t0 - RECOVERY_PAD)
        clip_end = t1 + RECOVERY_PAD
        res = {"asr_seconds": 0.0, "kept": [], "dup": 0, "invalid": 0}
        wav = _channel_wav(parts, speaker, clip_start, clip_end)
        if wav is None:
            return res
        res["asr_seconds"] = clip_end - clip_start
        try:
            kwargs = dict(
                file=("clip.wav", wav),
                model_id=model_id,
                diarize=False,
                tag_audio_events=False,
                timestamps_granularity="word",
            )
            if language:
                kwargs["language_code"] = language
            resp = _convert_with_retry(client, **kwargs)
        except Exception as e:  # noqa: BLE001 — odzysk jest best-effort
            print(
                f"WARN: recovery ASR fail ({speaker} @{t0:.1f}s): {e}", file=sys.stderr
            )
            return res
        asr_words = [w for w in (resp.words or []) if w.type == "word"]
        if not asr_words:
            return res
        if len(asr_words) / (clip_end - clip_start) > RECOVERY_MAX_WPS:
            res["invalid"] += len(asr_words)
            print(
                f"WARN: recovery {speaker} @{t0:.1f}s odrzucone — "
                f"{len(asr_words)} słów w {clip_end - clip_start:.1f}s "
                "wygląda na halucynację",
                file=sys.stderr,
            )
            return res
        for w in asr_words:
            token = _norm(w.text)
            start = clip_start + (w.start or 0.0)
            if not token:
                res["invalid"] += 1
                continue
            if already_in_mixed(speaker, token, start):
                res["dup"] += 1
                continue
            res["kept"].append(
                {
                    "text": w.text.strip(),
                    "start": start,
                    "end": clip_start + (w.end or 0.0),
                }
            )
        return res

    with ThreadPoolExecutor(max_workers=_asr_concurrency()) as ex:
        results = list(ex.map(work, windows))  # zachowuje kolejność okien

    # merge sekwencyjny w kolejności okien — identyczny wynik jak pętla serialna
    recovered: list[Utterance] = []
    for (speaker, _t0, _t1), res in zip(windows, results):
        stats["recovery_asr_seconds"] += res["asr_seconds"]
        stats["recovery_dropped_dup"] += res["dup"]
        stats["recovery_dropped_invalid"] += res["invalid"]
        stats["recovery_words"] += len(res["kept"])
        for w in res["kept"]:
            if (
                recovered
                and recovered[-1].speaker == speaker
                and w["start"] - recovered[-1].end <= UTT_GAP
            ):
                recovered[-1].text += f" {w['text']}"
                recovered[-1].end = w["end"]
            else:
                recovered.append(Utterance(speaker, w["start"], w["end"], w["text"]))
    stats["recovery_asr_seconds"] = round(stats["recovery_asr_seconds"], 1)
    stats["recovery_utterances"] = len(recovered)
    return recovered, stats


# ---------- API ----------


def diarize_by_energy(
    rec_dir: Path,
    words: list[dict],
    api_key: Optional[str] = None,
    language: Optional[str] = None,
    model_id: str = "scribe_v2",
    tiebreak: bool = True,
    recover_overlap: bool = True,
) -> tuple[list[Utterance], dict]:
    """Przypisz słowa mixed-transkryptu do mówców przez energię kanałów.

    words: lista wpisów z ElevenLabs raw response (type/start/end/text);
    wpisy inne niż "word" są ignorowane.
    """
    words = [w for w in words if w.get("type") == "word"]
    if not words:
        return [], {"total": 0}
    max_sec = max(w["end"] for w in words) + 1.0

    timings: dict[str, float] = {}
    _last = time.perf_counter()

    def lap(name: str) -> None:
        nonlocal _last
        now = time.perf_counter()
        timings[name] = round(now - _last, 2)
        _last = now

    parts = load_parts(rec_dir)
    env = build_envelopes(parts, max_sec)
    lap("envelopes_s")
    if not env:
        raise RuntimeError(f"{rec_dir}: nie zbudowano żadnego kanału energii")

    assigned = _viterbi(words, env)
    lap("viterbi_s")
    disputed = _flag_disputed(words, env)
    lap("flag_disputed_s")
    stats = {
        "total": len(words),
        "channels": sorted(env),
        "disputed": len(disputed),
        "tiebreak_confirmed": 0,
    }

    if tiebreak and disputed:
        if not api_key:
            raise ValueError("tiebreak=True wymaga api_key (ElevenLabs)")
        confirmed = _tiebreak(words, disputed, parts, api_key, language, model_id)
        stats["tiebreak_confirmed"] = len(confirmed)
        lap("tiebreak_asr_s")
        if confirmed:
            assigned = _viterbi(words, env, boosts=confirmed)
            lap("viterbi2_s")

    # grupowanie w wypowiedzi
    utts: list[Utterance] = []
    for speaker, w in zip(assigned, words):
        if (
            utts
            and utts[-1].speaker == speaker
            and w["start"] - utts[-1].end <= UTT_GAP
        ):
            utts[-1].text += f" {w['text']}"
            utts[-1].end = w["end"]
        else:
            utts.append(Utterance(speaker, w["start"], w["end"], w["text"]))

    # odzysk słów cichszego mówcy: mini-ASR okien z energią bez słów,
    # odzyskane wypowiedzi wstawiane między istniejące (istniejące bez zmian)
    if recover_overlap:
        if not api_key:
            raise ValueError("recover_overlap=True wymaga api_key (ElevenLabs)")
        _last = time.perf_counter()
        windows = find_recovery_windows(words, assigned, env)
        lap("recovery_windows_s")
        recovered, rec_stats = _recover_missing_speech(
            words,
            assigned,
            windows,
            parts,
            api_key,
            language,
            model_id,
        )
        lap("recovery_asr_s")
        stats.update(rec_stats)
        if recovered:
            utts = sorted(utts + recovered, key=lambda u: u.start)
    stats["timings"] = timings
    return utts, stats


def format_transcript(utts: list[Utterance]) -> str:
    return "\n".join(f"{u.speaker} [{_hms(u.start)}] {u.text}" for u in utts) + "\n"
