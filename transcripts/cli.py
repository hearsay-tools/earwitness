import argparse
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

from .comparison import (
    apply_aliases,
    format_report,
    load_elevenlabs,
    load_fireflies,
    load_ground_truth,
    load_macwhisper,
)
from .energy_diarization import diarize_by_energy
from .energy_diarization import format_transcript as format_energy_transcript
from .formatter import format_transcript
from .ground_truth import build_ground_truth
from .hybrid import (
    assign_speakers,
    load_speaker_intervals,
    load_word_timestamps,
    render_hybrid,
    render_segments,
)
from .recall_client import (
    DEFAULT_REGION,
    VALID_REGIONS,
    RecallClient,
    RecallConfig,
    collect_bot_assets,
    download_bot_assets,
)
from .replicate_client import (
    DEFAULT_ASR_MODEL,
    DEFAULT_DIARIZER,
    DEFAULT_MODEL,
    DIARIZER_BACKENDS,
    diarize_cloud,
    save_as_timeline,
    transcribe_and_diarize,
    transcribe_cloud,
)
from .text_comparison import format_text_report
from .transcribe import transcribe


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="earwitness",
        description="Earwitness — PoC transkrypcji spotkań z ElevenLabs Scribe.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # transcribe
    t = sub.add_parser(
        "transcribe",
        help="Transkrybuj plik audio przez ElevenLabs Scribe.",
        description=(
            "Output: 'Rozmówca N [HH:mm:ss] <treść>'. "
            "--num-speakers i --threshold są wzajemnie wykluczające."
        ),
    )
    t.add_argument("audio", type=Path, help="Ścieżka do pliku audio")
    t.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Plik wyjściowy (domyślnie stdout)",
    )
    g = t.add_mutually_exclusive_group()
    g.add_argument(
        "-n",
        "--num-speakers",
        type=int,
        default=None,
        help="Sztywna liczba rozmówców (wyklucza --threshold)",
    )
    g.add_argument(
        "-t",
        "--threshold",
        type=float,
        default=None,
        help=(
            "Diarization threshold 0.0–0.4 (API limit, nie 0–1). "
            "Wyższy = bardziej konserwatywny (mniej rozmówców)."
        ),
    )
    g.add_argument(
        "--no-diarize",
        action="store_true",
        help="Wyłącz diaryzację całkowicie (diarize=False na API). "
        "Wszystkie słowa bez speaker_id.",
    )
    t.add_argument(
        "--model", default="scribe_v2", help="Model ID (domyślnie scribe_v2)"
    )
    t.add_argument(
        "--language",
        default=None,
        help="Kod języka ISO (np. 'pl', 'en'). Pozostaw puste dla auto-detekcji.",
    )
    t.add_argument(
        "--save-raw",
        type=Path,
        default=None,
        help="Zapisz raw response (JSON) — potrzebne dla pipeline'u hybrid.",
    )

    # ground-truth
    gt = sub.add_parser(
        "ground-truth",
        help="Zbuduj ground-truth z speaker-timeline.json (+ opcjonalnie participants.json).",
    )
    gt.add_argument("timeline", type=Path, help="Ścieżka do speaker-timeline-*.json")
    gt.add_argument(
        "-p",
        "--participants",
        type=Path,
        default=None,
        help="Opcjonalny plik participants-*.json (oznacza hosta)",
    )
    gt.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Plik wyjściowy (domyślnie stdout)",
    )

    # compare
    cmp_ = sub.add_parser(
        "compare",
        help="Porównaj kandydatów (ElevenLabs/MacWhisper/Fireflies) z ground-truth.",
        description=(
            "Zlicza turns / talk time per realna osoba dla każdego źródła "
            "i pokazuje mapping speaker_id → realny mówca po największym "
            "overlapie czasowym."
        ),
    )
    cmp_.add_argument(
        "--timeline",
        type=Path,
        required=True,
        help="speaker-timeline-*.json (ground-truth)",
    )
    cmp_.add_argument(
        "--elevenlabs",
        type=Path,
        action="append",
        default=[],
        help="output naszego pipeline (Rozmówca N [HH:mm:ss] ...). "
        "Można podać wielokrotnie.",
    )
    cmp_.add_argument(
        "--macwhisper",
        type=Path,
        action="append",
        default=[],
        help="export MacWhisper (txt). Można podać wielokrotnie.",
    )
    cmp_.add_argument(
        "--fireflies",
        type=Path,
        action="append",
        default=[],
        help="export Fireflies (json). Można podać wielokrotnie.",
    )
    cmp_.add_argument(
        "--alias",
        action="append",
        default=[],
        help=(
            "Scal dwa GT speakery w jedną osobę: 'OryginalnyImię=Canonical'. "
            "Np. --alias 'Alex=alex smith' (ta sama osoba na 2 urządzeniach). "
            "Można podać wielokrotnie."
        ),
    )
    cmp_.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="Plik wyjściowy (domyślnie stdout)",
    )

    # transcribe-cloud
    tc_cloud = sub.add_parser(
        "transcribe-cloud",
        help="Zleć ASR na Replicate (Whisper-large-v3 turbo). Output kompatybilny z `hybrid --elevenlabs-raw`.",
    )
    tc_cloud.add_argument("audio", type=Path)
    tc_cloud.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="JSON output (kompatybilny z hybrid --elevenlabs-raw)",
    )
    tc_cloud.add_argument("--model", default=DEFAULT_ASR_MODEL)
    tc_cloud.add_argument("--language", default="english")
    tc_cloud.add_argument("--audio-field", default="audio")

    # diarize-cloud
    dc = sub.add_parser(
        "diarize-cloud",
        help="Zleć diaryzację na Replicate i zapisz w formacie speaker-timeline.json.",
        description=(
            "Output zapisuje jako speaker-timeline.json (kompatybilny z "
            "`hybrid --ground-truth` i `compare --timeline`)."
        ),
    )
    dc.add_argument("audio", type=Path, help="Plik audio/wideo")
    dc.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="JSON output (np. output/spotkanie.replicate.json)",
    )
    dc.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Replicate model:version (domyślnie {DEFAULT_MODEL})",
    )
    dc.add_argument(
        "-n",
        "--num-speakers",
        type=int,
        default=None,
        help="Sztywna liczba mówców (jeśli model przyjmuje)",
    )
    dc.add_argument(
        "--audio-field",
        default="audio",
        help="Nazwa pola input dla audio (zwykle 'audio', "
        "czasem 'audio_file' albo 'file')",
    )

    # hybrid
    h = sub.add_parser(
        "hybrid",
        help="Złóż transkrypt z 2 silników: tekst z jednego, diaryzacja z drugiego.",
        description=(
            "Łączy per-word timestamps z ElevenLabs raw response (no-diarize) "
            "z segmentami diaryzacji z innego źródła (parakeet/whisper/ground-truth). "
            "Dla każdego słowa przypisuje speakera o największym overlapie czasowym."
        ),
    )
    h.add_argument(
        "--elevenlabs-raw",
        type=Path,
        required=True,
        help="JSON z transcribe --save-raw (best: --no-diarize żeby tekst był najczystszy)",
    )
    diar_g = h.add_mutually_exclusive_group(required=True)
    diar_g.add_argument(
        "--macwhisper",
        type=Path,
        help="Źródło diaryzacji: txt z MacWhisper/Parakeet",
    )
    diar_g.add_argument(
        "--ground-truth",
        type=Path,
        help="Źródło diaryzacji: speaker-timeline-*.json (idealny benchmark)",
    )
    h.add_argument(
        "--real-names",
        action="store_true",
        help="Użyj oryginalnych etykiet speakerów (np. imion z ground-truth) "
        "zamiast anonimowego 'Rozmówca N'.",
    )
    h.add_argument("-o", "--output", type=Path, default=None)

    # text-compare
    tc = sub.add_parser(
        "text-compare",
        help="Porównaj jakość samej transkrypcji (bez diaryzacji).",
        description=(
            "Statystyki tekstu, pairwise Jaccard na słowach, słowa unikalne, "
            "side-by-side fragmenty."
        ),
    )
    tc.add_argument("--elevenlabs", type=Path, action="append", default=[])
    tc.add_argument("--macwhisper", type=Path, action="append", default=[])
    tc.add_argument("--fireflies", type=Path, action="append", default=[])
    tc.add_argument(
        "--window",
        action="append",
        default=[],
        help=(
            "Wytnij fragment do side-by-side w formacie 'MM:SS-MM:SS'. "
            "Można podać wielokrotnie. Domyślnie 3 fragmenty: początek, środek, koniec."
        ),
    )
    tc.add_argument("-o", "--output", type=Path, default=None)

    # pipeline
    pr = sub.add_parser(
        "pipeline-recall",
        help=(
            "DEFAULT: mixed ASR (ElevenLabs) + diaryzacja energią izolowanych "
            "kanałów z Recall audio_separate. Deterministyczne, bez Replicate."
        ),
        description=(
            "Produkcyjny pipeline (default od 2026-07-10, patrz "
            "experiments/RESULTS.md). Wymaga katalogu nagrania pobranego przez "
            "recall-fetch (<out>/<bot_id>/<recording_id>/ z audio_mixed.mp3, "
            "recording.json i audio_separate/). Intermediate raw.json lądujе "
            "obok --output i jest reused (idempotentnie). Dla nagrań bez "
            "audio_separate użyj fallbacku: subkomenda 'pipeline' (Replicate)."
        ),
    )
    pr.add_argument(
        "rec_dir",
        type=Path,
        help="Katalog nagrania z recall-fetch (<bot_id>/<recording_id>)",
    )
    pr.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Finalny transkrypt (np. output/spotkanie.txt)",
    )
    pr.add_argument(
        "--model",
        default="scribe_v2",
        help="ElevenLabs model (domyślnie scribe_v2)",
    )
    pr.add_argument(
        "--language",
        default=None,
        help="Kod języka ISO dla ASR (domyślnie: auto, tiebreak bierze "
        "language_code z raw.json)",
    )
    pr.add_argument(
        "--no-tiebreak",
        action="store_true",
        help="Bez mini-ASR spornych okien (szybciej/taniej, minimalnie gorzej "
        "na overlapach)",
    )
    pr.add_argument(
        "--no-overlap-recovery",
        action="store_true",
        help="Bez odzysku słów cichszego mówcy przy overlapie (mini-ASR okien "
        "z energią kanału bez przypisanych słów; domyślnie włączony)",
    )
    pr.add_argument(
        "--raw-out",
        type=Path,
        default=None,
        help="Override ścieżki raw.json (domyślnie <output-stem>.raw.json)",
    )
    pr.add_argument(
        "--force",
        action="store_true",
        help="Ignoruj istniejący raw.json i transkrybuj od zera.",
    )

    pipe = sub.add_parser(
        "pipeline",
        help=(
            "FALLBACK (nagrania bez Recall audio_separate): ElevenLabs ASR "
            "(no-diarize) + Replicate pyannote + hybrid merge."
        ),
        description=(
            "Produkcyjny pipeline. Intermediate pliki (raw.json, timeline.json) "
            "lądują obok --output; jeśli już istnieją, są reused (idempotentnie). "
            "Użyj --force żeby zrobić wszystko od zera."
        ),
    )
    pipe.add_argument("audio", type=Path, help="Plik audio/wideo")
    pipe.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Finalny transkrypt (np. output/spotkanie.txt)",
    )
    pipe.add_argument(
        "-n",
        "--num-speakers",
        type=int,
        default=None,
        help="Sztywna liczba mówców dla diaryzacji (przekazana do Replicate)",
    )
    pipe.add_argument(
        "--language",
        default=None,
        help="Kod języka ISO dla ElevenLabs (np. 'pl'). Domyślnie auto.",
    )
    pipe.add_argument(
        "--real-names",
        action="store_true",
        help="Zachowaj oryginalne etykiety speakerów (SPEAKER_00...) zamiast 'Rozmówca N'.",
    )
    pipe.add_argument(
        "--model",
        default="scribe_v2",
        help="ElevenLabs model (domyślnie scribe_v2)",
    )
    pipe.add_argument(
        "--diarizer",
        choices=list(DIARIZER_BACKENDS.keys()),
        default=None,
        help=(
            "Backend diaryzacji (Replicate). "
            + " | ".join(
                f"'{k}': {v['description']}" for k, v in DIARIZER_BACKENDS.items()
            )
            + f" (domyślnie {DEFAULT_DIARIZER}). "
            "UWAGA: jeśli obok audio jest speaker-timeline.json z Recall, "
            "pipeline użyje go zamiast Replicate — chyba że jawnie podasz "
            "--diarizer (wtedy wymusza Replicate)."
        ),
    )
    pipe.add_argument(
        "--speaker-timeline",
        type=Path,
        default=None,
        help="Użyj gotowego speaker-timeline.json (np. z Recall) jako źródła "
        "diaryzacji zamiast Replicate. Bez tego auto-wykrywa plik obok audio.",
    )
    pipe.add_argument(
        "--diarize-model",
        default=None,
        help="Advanced: override konkretnego Replicate model:version dla wybranego "
        "diarizera (np. fork tej samej rodziny). Zwykle używaj --diarizer.",
    )
    pipe.add_argument(
        "--audio-out",
        type=Path,
        default=None,
        help="Override ścieżki wyekstrahowanego audio (gdy input to wideo). "
        "Domyślnie <output-stem>.audio.m4a",
    )
    pipe.add_argument(
        "--raw-out",
        type=Path,
        default=None,
        help="Override ścieżki raw.json (domyślnie <output-stem>.raw.json)",
    )
    pipe.add_argument(
        "--timeline-out",
        type=Path,
        default=None,
        help="Override ścieżki timeline.json (domyślnie <output-stem>.timeline.json)",
    )
    pipe.add_argument(
        "--force",
        action="store_true",
        help="Ignoruj istniejące intermediate pliki i zrób wszystko od zera.",
    )

    # oneshot
    one = sub.add_parser(
        "oneshot",
        help=(
            "All-in-one transkrypt przez thomasmol/whisper-diarization "
            "(Whisper ASR + NeMo MSDD w jednym API calle, bez ElevenLabs, "
            "bez naszego merge'u)."
        ),
        description=(
            "Pojedyncze wywołanie Replicate — Whisper i diaryzacja lecą razem, "
            "więc tekst i speaker są przypisani w tym samym kroku (brak off-by "
            "z merge'u). Trade-off vs pipeline: Whisper ma słabsze polskie "
            "nazwy własne niż ElevenLabs — użyj --prompt żeby je podpowiedzieć."
        ),
    )
    one.add_argument("audio", type=Path, help="Plik audio/wideo")
    one.add_argument(
        "-o",
        "--output",
        type=Path,
        required=True,
        help="Finalny transkrypt",
    )
    one.add_argument(
        "-n",
        "--num-speakers",
        type=int,
        default=None,
        help="Sztywna liczba mówców (NeMo MSDD trzyma się tego twardo)",
    )
    one.add_argument(
        "--language",
        default=None,
        help="Kod języka ISO (np. 'pl'). Domyślnie auto-detect Whisper'a.",
    )
    one.add_argument(
        "--prompt",
        default=None,
        help=(
            "Vocabulary hint dla Whispera — nazwy własne, akronimy z "
            "interpunkcją. Np. 'Acme, Anna Nowak, Jan Kowalski.'"
        ),
    )
    one.add_argument(
        "--real-names",
        action="store_true",
        help="Zachowaj SPEAKER_00... zamiast 'Rozmówca N'.",
    )
    one.add_argument(
        "--audio-out",
        type=Path,
        default=None,
        help="Override ścieżki wyekstrahowanego audio (gdy input to wideo).",
    )
    one.add_argument(
        "--raw-out",
        type=Path,
        default=None,
        help="Override ścieżki raw response JSON (domyślnie <stem>.oneshot.json)",
    )
    one.add_argument(
        "--force",
        action="store_true",
        help="Ignoruj istniejące intermediate pliki.",
    )

    # recall-fetch
    rf = sub.add_parser(
        "recall-fetch",
        help="Pobierz audio (mixed + separate per participant) z Recall.ai.",
        description=(
            "Stłuczkuje wszystkie dostępne audio-assety per bot/recording. "
            "Pliki w Recall mają TTL (24h default), więc warto je ściągać "
            "zaraz po spotkaniu. Struktura wyjścia: "
            "<out>/<bot_id>/<recording_id>/audio_mixed.mp3 + "
            "audio_separate/<participant>/<part>.ogg. "
            "Wymaga RECALL_API_KEY (env lub --api-key). Domyślny region "
            f"to {DEFAULT_REGION} — zmień przez RECALL_REGION lub --region."
        ),
    )
    rf.add_argument(
        "bot_ids",
        nargs="*",
        help="Konkretne bot_id do pobrania. Jeśli puste — bierze wszystkie "
        "boty z workspace'u (filtrowane przez --status).",
    )
    rf.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=Path("output/recall"),
        help="Katalog wyjściowy (domyślnie output/recall)",
    )
    rf.add_argument(
        "--api-key",
        default=None,
        help="Recall API key (override RECALL_API_KEY z env)",
    )
    rf.add_argument(
        "--region",
        default=None,
        choices=list(VALID_REGIONS),
        help=f"Recall region (override RECALL_REGION). Default: {DEFAULT_REGION}",
    )
    rf.add_argument(
        "--status",
        action="append",
        default=None,
        help="Filtr statusu botów dla list-all (np. 'done', 'recording_done'). "
        "Można podać wielokrotnie. Bez tego — wszystkie statusy. "
        "Ignorowane gdy podajesz konkretne bot_ids.",
    )
    rf.add_argument(
        "--meeting-url",
        default=None,
        help="Filtr po meeting_url (tylko gdy nie podajesz bot_ids).",
    )
    rf.add_argument(
        "--no-mixed",
        action="store_true",
        help="Nie pobieraj audio_mixed (tylko per-participant).",
    )
    rf.add_argument(
        "--no-separate",
        action="store_true",
        help="Nie pobieraj audio_separate (tylko mixed).",
    )
    rf.add_argument(
        "--no-events",
        action="store_true",
        help="Nie pobieraj participant_events (speaker-timeline.json, "
        "participants.json, participant-events.json).",
    )
    rf.add_argument(
        "--force",
        action="store_true",
        help="Re-download nawet jeśli plik już istnieje.",
    )
    rf.add_argument(
        "--dry-run",
        action="store_true",
        help="Tylko wypisz co byłoby pobrane (bez pobierania).",
    )

    # recall-pause / recall-resume
    for name, verb in (("recall-pause", "Wstrzymaj"), ("recall-resume", "Wznów")):
        rp = sub.add_parser(
            name,
            help=f"{verb} nagrywanie bota Recall.ai (bot zostaje w callu).",
            description=(
                f"{verb} nagrywanie dla podanego bota. Bot nie opuszcza "
                "spotkania. Wymaga RECALL_API_KEY (env lub --api-key). "
                f"Domyślny region to {DEFAULT_REGION} — zmień przez "
                "RECALL_REGION lub --region."
            ),
        )
        rp.add_argument("bot_id", help="Bot ID (UUID) do sterowania nagrywaniem.")
        rp.add_argument(
            "--api-key",
            default=None,
            help="Recall API key (override RECALL_API_KEY z env)",
        )
        rp.add_argument(
            "--region",
            default=None,
            choices=list(VALID_REGIONS),
            help=f"Recall region (override RECALL_REGION). Default: {DEFAULT_REGION}",
        )

    return p


def _cmd_transcribe(args: argparse.Namespace) -> int:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ustaw ELEVENLABS_API_KEY (np. w .env)", file=sys.stderr)
        return 2
    if not args.audio.exists():
        print(f"ERROR: plik {args.audio} nie istnieje", file=sys.stderr)
        return 2

    diarize = not args.no_diarize
    print(
        f"Transcribing {args.audio} (model={args.model}, diarize={diarize}, "
        f"num_speakers={args.num_speakers}, threshold={args.threshold})...",
        file=sys.stderr,
    )

    response = transcribe(
        args.audio,
        api_key=api_key,
        num_speakers=args.num_speakers,
        diarization_threshold=args.threshold,
        diarize=diarize,
        model_id=args.model,
        language_code=args.language,
    )

    if args.save_raw:
        args.save_raw.parent.mkdir(parents=True, exist_ok=True)
        args.save_raw.write_text(
            response.model_dump_json(indent=2),
            encoding="utf-8",
        )
        print(f"Wrote raw response to {args.save_raw}", file=sys.stderr)

    formatted = format_transcript(response.words or [])
    _emit(formatted, args.output)
    return 0


def _cmd_ground_truth(args: argparse.Namespace) -> int:
    if not args.timeline.exists():
        print(f"ERROR: plik {args.timeline} nie istnieje", file=sys.stderr)
        return 2
    text = build_ground_truth(args.timeline, args.participants)
    _emit(text, args.output, append_newline=False)
    return 0


def _cmd_transcribe_cloud(args: argparse.Namespace) -> int:
    if not args.audio.exists():
        print(f"ERROR: {args.audio} nie istnieje", file=sys.stderr)
        return 2

    print(
        f"Transcribing {args.audio} via Replicate ({args.model})...",
        file=sys.stderr,
    )
    try:
        result = transcribe_cloud(
            args.audio,
            model=args.model,
            language=args.language,
            audio_field=args.audio_field,
        )
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    import json as _json

    args.output.write_text(_json.dumps(result, indent=2), encoding="utf-8")
    print(
        f"Wrote {len(result['words'])} words to {args.output}",
        file=sys.stderr,
    )
    return 0


def _cmd_diarize_cloud(args: argparse.Namespace) -> int:
    if not args.audio.exists():
        print(f"ERROR: {args.audio} nie istnieje", file=sys.stderr)
        return 2

    print(
        f"Diarizing {args.audio} via Replicate ({args.model})...",
        file=sys.stderr,
    )
    try:
        segments = diarize_cloud(
            args.audio,
            model=args.model,
            num_speakers=args.num_speakers,
            audio_field=args.audio_field,
        )
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    save_as_timeline(segments, args.output)
    print(
        f"Wrote {len(segments)} segments to {args.output}",
        file=sys.stderr,
    )
    return 0


def _cmd_hybrid(args: argparse.Namespace) -> int:
    if not args.elevenlabs_raw.exists():
        print(f"ERROR: {args.elevenlabs_raw} nie istnieje", file=sys.stderr)
        return 2

    words = load_word_timestamps(args.elevenlabs_raw)
    intervals = load_speaker_intervals(
        macwhisper_path=args.macwhisper,
        ground_truth_path=args.ground_truth,
    )

    print(
        f"Hybrid: {len(words)} słów + {len(intervals)} segmentów "
        f"→ przypisuję speakerów po overlapie czasowym...",
        file=sys.stderr,
    )

    enriched = assign_speakers(words, intervals)
    text = render_hybrid(enriched, use_real_names=args.real_names)
    _emit(text, args.output, append_newline=False)
    return 0


def _parse_window(spec: str) -> tuple[float, float]:
    a, b = spec.split("-")
    return _parse_mmss(a), _parse_mmss(b)


def _parse_mmss(s: str) -> float:
    parts = s.strip().split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    raise ValueError(f"Nieznany format czasu: {s!r}")


def _cmd_text_compare(args: argparse.Namespace) -> int:
    candidates: dict[str, list] = {}
    for path in args.elevenlabs:
        candidates[_short_label(path, "el")] = load_elevenlabs(path)
    for path in args.macwhisper:
        candidates[_short_label(path, "mw")] = load_macwhisper(path)
    for path in args.fireflies:
        candidates[_short_label(path, "ff")] = load_fireflies(path)

    if not candidates:
        print("ERROR: brak kandydatów", file=sys.stderr)
        return 2

    if args.window:
        windows = [_parse_window(w) for w in args.window]
    else:
        max_end = max(
            (max((u.end for u in utts), default=0) for utts in candidates.values()),
            default=0,
        )
        windows = [
            (60.0, 90.0),
            (max_end / 2, max_end / 2 + 30),
            (max_end - 60, max_end - 30),
        ]

    text = format_text_report(candidates, windows=windows)
    _emit(text, args.output, append_newline=False)
    return 0


def _short_label(path: Path, prefix: str) -> str:
    stem = path.stem
    if "." in stem:
        tail = stem.rsplit(".", 1)[1]
    else:
        tail = stem.rsplit("-", 1)[-1]
    return f"{prefix}:{tail}".lower()


def _cmd_compare(args: argparse.Namespace) -> int:
    if not args.timeline.exists():
        print(f"ERROR: {args.timeline} nie istnieje", file=sys.stderr)
        return 2

    aliases: dict[str, str] = {}
    for spec in args.alias:
        if "=" not in spec:
            print(
                f"ERROR: --alias musi mieć format 'A=B', dostałem {spec!r}",
                file=sys.stderr,
            )
            return 2
        src, dst = spec.split("=", 1)
        aliases[src.strip()] = dst.strip()

    gt = apply_aliases(load_ground_truth(args.timeline), aliases)
    candidates: dict[str, list] = {}

    for path in args.elevenlabs:
        candidates[_short_label(path, "el")] = load_elevenlabs(path)
    for path in args.macwhisper:
        candidates[_short_label(path, "mw")] = load_macwhisper(path)
    for path in args.fireflies:
        candidates[_short_label(path, "ff")] = load_fireflies(path)

    if not candidates:
        print(
            "ERROR: brak kandydatów (--elevenlabs/--macwhisper/--fireflies)",
            file=sys.stderr,
        )
        return 2

    text = format_report(gt, candidates)
    _emit(text, args.output, append_newline=False)
    return 0


def _has_video_stream(path: Path) -> bool:
    """True jeśli plik zawiera stream video (czyli warto wyciągnąć samo audio)."""
    import shutil
    import subprocess

    if not shutil.which("ffprobe"):
        return False
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return "video" in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False


def _extract_audio(video_path: Path, out_path: Path) -> None:
    """Wyciągnij audio bez transkodowania (-acodec copy). Fallback do mp3 jeśli copy się wywali."""
    import shutil
    import subprocess

    if not shutil.which("ffmpeg"):
        raise RuntimeError(
            "ffmpeg nie znaleziony w PATH. `brew install ffmpeg` "
            "albo skonwertuj plik ręcznie do audio i podaj go zamiast wideo."
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "copy",
            str(out_path),
        ],
        capture_output=True,
    )
    if result.returncode == 0:
        return

    # Fallback: re-encode do mp3 (np. gdy kontener nie akceptuje source codec)
    mp3_path = out_path.with_suffix(".mp3")
    print(
        f"  copy się nie udał, re-encode do mp3 → {mp3_path}",
        file=sys.stderr,
    )
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(video_path),
            "-vn",
            "-acodec",
            "libmp3lame",
            "-b:a",
            "96k",
            "-ac",
            "1",
            str(mp3_path),
        ],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed:\n{result.stderr.decode('utf-8', errors='replace')}"
        )
    # podmień ścieżkę docelową na faktycznie wytworzoną
    out_path.unlink(missing_ok=True)
    mp3_path.rename(out_path.with_suffix(".mp3"))


def _cmd_pipeline(args: argparse.Namespace) -> int:
    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ustaw ELEVENLABS_API_KEY (np. w .env)", file=sys.stderr)
        return 2
    if not os.environ.get("REPLICATE_API_TOKEN"):
        print("ERROR: ustaw REPLICATE_API_TOKEN (np. w .env)", file=sys.stderr)
        return 2
    if not args.audio.exists():
        print(f"ERROR: plik {args.audio} nie istnieje", file=sys.stderr)
        return 2

    out: Path = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    stem = out.with_suffix("")
    raw_path = args.raw_out or Path(f"{stem}.raw.json")
    timeline_path = args.timeline_out or Path(f"{stem}.timeline.json")
    audio_path = args.audio

    if _has_video_stream(args.audio):
        extracted = args.audio_out or Path(f"{stem}.audio.m4a")
        # fallback do .mp3 może istnieć z poprzedniego runu
        mp3_alt = extracted.with_suffix(".mp3")
        if not args.force and extracted.exists():
            print(f"[0/3] Reuse extracted audio: {extracted}", file=sys.stderr)
            audio_path = extracted
        elif not args.force and mp3_alt.exists():
            print(f"[0/3] Reuse extracted audio: {mp3_alt}", file=sys.stderr)
            audio_path = mp3_alt
        else:
            print(
                f"[0/3] Wideo wykryte → ekstrakcja audio → {extracted}",
                file=sys.stderr,
            )
            _extract_audio(args.audio, extracted)
            audio_path = extracted if extracted.exists() else mp3_alt
            print(
                f"      {audio_path.stat().st_size / (1024 * 1024):.1f} MB",
                file=sys.stderr,
            )

    if raw_path.exists() and not args.force:
        print(f"[1/3] Reuse ElevenLabs raw: {raw_path}", file=sys.stderr)
    else:
        print(
            f"[1/3] ElevenLabs ASR (no-diarize, model={args.model}) → {raw_path}",
            file=sys.stderr,
        )
        response = transcribe(
            audio_path,
            api_key=api_key,
            diarize=False,
            model_id=args.model,
            language_code=args.language,
        )
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(
            response.model_dump_json(indent=2),
            encoding="utf-8",
        )

    # Źródło diaryzacji: gotowy speaker-timeline (Recall) czy Replicate?
    # Priorytet: --speaker-timeline > auto-detekcja obok audio > Replicate.
    # Jawny --diarizer wymusza Replicate (pomija auto-detekcję).
    recall_tl: Path | None = args.speaker_timeline
    if recall_tl is None and args.diarizer is None:
        candidate = args.audio.parent / "speaker-timeline.json"
        if candidate.exists():
            recall_tl = candidate

    if recall_tl is not None:
        if not recall_tl.exists():
            print(f"ERROR: {recall_tl} nie istnieje", file=sys.stderr)
            return 2
        timeline_path = recall_tl
        print(
            f"[2/3] Diarization z gotowego speaker-timeline (Recall) → {timeline_path}",
            file=sys.stderr,
        )
    elif timeline_path.exists() and not args.force:
        print(f"[2/3] Reuse diarization timeline: {timeline_path}", file=sys.stderr)
    else:
        diarizer = args.diarizer or DEFAULT_DIARIZER
        backend = DIARIZER_BACKENDS[diarizer]
        model = args.diarize_model or backend["model"]
        audio_field = backend["audio_field"]
        print(
            f"[2/3] Diarization via {diarizer} ({model}"
            f"{f', n={args.num_speakers}' if args.num_speakers else ''})"
            f" → {timeline_path}",
            file=sys.stderr,
        )
        segments = diarize_cloud(
            audio_path,
            model=model,
            num_speakers=args.num_speakers,
            audio_field=audio_field,
        )
        save_as_timeline(segments, timeline_path)
        print(f"      {len(segments)} segments", file=sys.stderr)

    print(f"[3/3] Hybrid merge → {out}", file=sys.stderr)
    words = load_word_timestamps(raw_path)
    intervals = load_speaker_intervals(ground_truth_path=timeline_path)
    enriched = assign_speakers(words, intervals)
    text = render_hybrid(enriched, use_real_names=args.real_names)
    _emit(text, out, append_newline=False)
    return 0


def _cmd_oneshot(args: argparse.Namespace) -> int:
    if not os.environ.get("REPLICATE_API_TOKEN"):
        print("ERROR: ustaw REPLICATE_API_TOKEN (np. w .env)", file=sys.stderr)
        return 2
    if not args.audio.exists():
        print(f"ERROR: plik {args.audio} nie istnieje", file=sys.stderr)
        return 2

    out: Path = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    stem = out.with_suffix("")
    raw_path = args.raw_out or Path(f"{stem}.oneshot.json")
    audio_path = args.audio

    if _has_video_stream(args.audio):
        extracted = args.audio_out or Path(f"{stem}.audio.m4a")
        mp3_alt = extracted.with_suffix(".mp3")
        if not args.force and extracted.exists():
            print(f"[0/2] Reuse extracted audio: {extracted}", file=sys.stderr)
            audio_path = extracted
        elif not args.force and mp3_alt.exists():
            print(f"[0/2] Reuse extracted audio: {mp3_alt}", file=sys.stderr)
            audio_path = mp3_alt
        else:
            print(
                f"[0/2] Wideo wykryte → ekstrakcja audio → {extracted}",
                file=sys.stderr,
            )
            _extract_audio(args.audio, extracted)
            audio_path = extracted if extracted.exists() else mp3_alt
            print(
                f"      {audio_path.stat().st_size / (1024 * 1024):.1f} MB",
                file=sys.stderr,
            )

    if raw_path.exists() and not args.force:
        print(f"[1/2] Reuse oneshot response: {raw_path}", file=sys.stderr)
        import json as _json

        response = _json.loads(raw_path.read_text())
    else:
        print(
            f"[1/2] Replicate thomasmol/whisper-diarization "
            f"(lang={args.language or 'auto'}"
            f"{f', n={args.num_speakers}' if args.num_speakers else ''}"
            f"{', +prompt' if args.prompt else ''}"
            f") → {raw_path}",
            file=sys.stderr,
        )
        try:
            response = transcribe_and_diarize(
                audio_path,
                num_speakers=args.num_speakers,
                language=args.language,
                prompt=args.prompt,
            )
        except Exception as e:
            print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        import json as _json

        raw_path.write_text(_json.dumps(response, indent=2), encoding="utf-8")
        print(
            f"      {response.get('num_speakers', '?')} mówców, "
            f"{len(response.get('segments') or [])} segmentów",
            file=sys.stderr,
        )

    print(f"[2/2] Render → {out}", file=sys.stderr)
    segments = response.get("segments") or []
    text = render_segments(segments, use_real_names=args.real_names)
    _emit(text, out, append_newline=False)
    return 0


def _cmd_recall_fetch(args: argparse.Namespace) -> int:
    try:
        config = RecallConfig.from_env(api_key=args.api_key, region=args.region)
    except ValueError as e:
        print(
            f"ERROR: {e}. Ustaw RECALL_API_KEY (env/.env) albo podaj --api-key.",
            file=sys.stderr,
        )
        return 2

    client = RecallClient(config)
    print(
        f"Recall: region={config.region}, output={args.output_dir}",
        file=sys.stderr,
    )

    # Resolve listę bot_ids
    if args.bot_ids:
        bot_ids = list(args.bot_ids)
    else:
        print("Pobieranie listy botów...", file=sys.stderr)
        bots = list(
            client.list_bots(
                status=args.status,
                meeting_url=args.meeting_url,
            )
        )
        bot_ids = [b["id"] for b in bots]
        print(f"  → znaleziono {len(bot_ids)} botów", file=sys.stderr)

    if not bot_ids:
        print("Brak botów do pobrania.", file=sys.stderr)
        return 0

    if args.dry_run:
        for bot_id in bot_ids:
            try:
                bot = client.get_bot(bot_id)
            except Exception as e:
                print(f"[{bot_id}] ERROR: {e}", file=sys.stderr)
                continue
            assets = collect_bot_assets(client, bot)
            print(f"[{bot_id}] meeting={bot.get('meeting_url') or '?'}")
            for rec in assets:
                mixed = len(rec.audio_mixed)
                parts = sum(len(a.parts) for a in rec.audio_separate)
                events = sum(
                    bool(u)
                    for u in (
                        rec.speaker_timeline_url,
                        rec.participants_url,
                        rec.participant_events_url,
                    )
                )
                print(
                    f"  rec {rec.recording_id} expires={rec.expires_at} "
                    f"mixed={mixed} separate_parts={parts} events={events}"
                )
        return 0

    totals = {
        "mixed_downloaded": 0,
        "mixed_skipped": 0,
        "separate_parts_downloaded": 0,
        "separate_parts_skipped": 0,
        "events_downloaded": 0,
        "events_skipped": 0,
        "bytes": 0,
        "errors": 0,
    }
    for bot_id in bot_ids:
        print(f"[{bot_id}] start", file=sys.stderr)
        try:
            s = download_bot_assets(
                client,
                bot_id,
                args.output_dir,
                force=args.force,
                download_mixed=not args.no_mixed,
                download_separate=not args.no_separate,
                download_events=not args.no_events,
            )
        except Exception as e:
            print(f"[{bot_id}] ERROR: {type(e).__name__}: {e}", file=sys.stderr)
            totals["errors"] += 1
            continue
        totals["mixed_downloaded"] += s["mixed_downloaded"]
        totals["mixed_skipped"] += s["mixed_skipped"]
        totals["separate_parts_downloaded"] += s["separate_parts_downloaded"]
        totals["separate_parts_skipped"] += s["separate_parts_skipped"]
        totals["events_downloaded"] += s["events_downloaded"]
        totals["events_skipped"] += s["events_skipped"]
        totals["bytes"] += s["bytes"]
        totals["errors"] += len(s["errors"])
        print(
            f"[{bot_id}] done — mixed: {s['mixed_downloaded']} new / "
            f"{s['mixed_skipped']} skip, separate parts: "
            f"{s['separate_parts_downloaded']} new / "
            f"{s['separate_parts_skipped']} skip, events: "
            f"{s['events_downloaded']} new / {s['events_skipped']} skip, "
            f"{s['bytes'] / (1024 * 1024):.1f} MB"
            + (f", errors={len(s['errors'])}" if s["errors"] else ""),
            file=sys.stderr,
        )

    print(
        f"\nTotal: mixed {totals['mixed_downloaded']} new / "
        f"{totals['mixed_skipped']} skip, "
        f"separate parts {totals['separate_parts_downloaded']} new / "
        f"{totals['separate_parts_skipped']} skip, "
        f"events {totals['events_downloaded']} new / "
        f"{totals['events_skipped']} skip, "
        f"{totals['bytes'] / (1024 * 1024):.1f} MB"
        + (f", errors={totals['errors']}" if totals["errors"] else ""),
        file=sys.stderr,
    )
    return 1 if totals["errors"] else 0


def _emit(text: str, output: Path | None, append_newline: bool = True) -> None:
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        suffix = "\n" if append_newline and not text.endswith("\n") else ""
        output.write_text(text + suffix, encoding="utf-8")
        print(f"Wrote {output}", file=sys.stderr)
    else:
        print(text)


def _cmd_pipeline_recall(args: argparse.Namespace) -> int:
    import json

    api_key = os.environ.get("ELEVENLABS_API_KEY")
    if not api_key:
        print("ERROR: ustaw ELEVENLABS_API_KEY (np. w .env)", file=sys.stderr)
        return 2
    rec_dir: Path = args.rec_dir
    if not (rec_dir / "recording.json").exists():
        print(
            f"ERROR: {rec_dir} nie wygląda na katalog nagrania z recall-fetch "
            "(brak recording.json)",
            file=sys.stderr,
        )
        return 2

    out: Path = args.output
    out.parent.mkdir(parents=True, exist_ok=True)
    stem = out.with_suffix("")
    raw_path = args.raw_out or Path(f"{stem}.raw.json")

    if raw_path.exists() and not args.force:
        print(f"[1/2] Reuse ElevenLabs raw: {raw_path}", file=sys.stderr)
    else:
        mixed = sorted(rec_dir.glob("audio_mixed*.mp3"))
        if not mixed:
            print(
                f"ERROR: brak audio_mixed*.mp3 w {rec_dir} "
                "(pobierz przez recall-fetch)",
                file=sys.stderr,
            )
            return 2
        print(
            f"[1/2] ElevenLabs ASR (no-diarize, model={args.model}) "
            f"{mixed[0].name} → {raw_path}",
            file=sys.stderr,
        )
        t0 = time.perf_counter()
        response = transcribe(
            mixed[0],
            api_key=api_key,
            diarize=False,
            model_id=args.model,
            language_code=args.language,
        )
        print(f"      main ASR: {time.perf_counter() - t0:.1f}s", file=sys.stderr)
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(response.model_dump_json(indent=2), encoding="utf-8")

    data = json.loads(raw_path.read_text(encoding="utf-8"))
    words = data.get("words") or []
    language = args.language or data.get("language_code")
    print(
        f"[2/2] Diaryzacja energią kanałów (tiebreak: "
        f"{'off' if args.no_tiebreak else 'on'}, overlap-recovery: "
        f"{'off' if args.no_overlap_recovery else 'on'}) → {out}",
        file=sys.stderr,
    )
    utts, stats = diarize_by_energy(
        rec_dir,
        words,
        api_key=api_key,
        language=language,
        model_id=args.model,
        tiebreak=not args.no_tiebreak,
        recover_overlap=not args.no_overlap_recovery,
    )
    out.write_text(format_energy_transcript(utts), encoding="utf-8")
    print(f"      {json.dumps(stats, ensure_ascii=False)}", file=sys.stderr)
    print(f"OK → {out}", file=sys.stderr)
    return 0


def _cmd_recall_pause_resume(args: argparse.Namespace, resume: bool) -> int:
    try:
        config = RecallConfig.from_env(api_key=args.api_key, region=args.region)
    except ValueError as e:
        print(
            f"ERROR: {e}. Ustaw RECALL_API_KEY (env/.env) albo podaj --api-key.",
            file=sys.stderr,
        )
        return 2

    client = RecallClient(config)
    action = "resume" if resume else "pause"
    print(
        f"Recall: region={config.region}, {action} bot={args.bot_id}",
        file=sys.stderr,
    )
    try:
        bot = (
            client.resume_recording(args.bot_id)
            if resume
            else client.pause_recording(args.bot_id)
        )
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    changes = bot.get("status_changes") or []
    last = changes[-1]["code"] if changes else "?"
    verb = "wznowione" if resume else "wstrzymane"
    print(f"OK → nagrywanie {verb}. status={last}", file=sys.stderr)
    return 0


def run(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = _build_parser().parse_args(argv)
    if args.cmd == "transcribe":
        return _cmd_transcribe(args)
    if args.cmd == "ground-truth":
        return _cmd_ground_truth(args)
    if args.cmd == "compare":
        return _cmd_compare(args)
    if args.cmd == "hybrid":
        return _cmd_hybrid(args)
    if args.cmd == "diarize-cloud":
        return _cmd_diarize_cloud(args)
    if args.cmd == "transcribe-cloud":
        return _cmd_transcribe_cloud(args)
    if args.cmd == "text-compare":
        return _cmd_text_compare(args)
    if args.cmd == "pipeline-recall":
        return _cmd_pipeline_recall(args)
    if args.cmd == "pipeline":
        return _cmd_pipeline(args)
    if args.cmd == "oneshot":
        return _cmd_oneshot(args)
    if args.cmd == "recall-fetch":
        return _cmd_recall_fetch(args)
    if args.cmd == "recall-pause":
        return _cmd_recall_pause_resume(args, resume=False)
    if args.cmd == "recall-resume":
        return _cmd_recall_pause_resume(args, resume=True)
    return 2
