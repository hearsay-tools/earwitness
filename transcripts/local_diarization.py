"""Lokalna diaryzacja (bez Replicate): sherpa-onnx = pyannote segmentation 3.0
+ embedding mówcy + klasteryzacja, wszystko na CPU przez onnxruntime.

Po co: fallback `pipeline` nie powinien zależeć od kredytów w chmurze. Modele
(~35 MB) pobieramy z GitHub Releases przy pierwszym użyciu, bez tokenu HF.
Zależności są opcjonalne: `uv sync --extra local`.
"""

from __future__ import annotations

import shutil
import subprocess
import tarfile
import urllib.request
from pathlib import Path

from .replicate_client import DiarSegment

SAMPLE_RATE = 16_000
_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_URL = (
    f"{_RELEASES}/speaker-segmentation-models/"
    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2"
)
# zh+en: embeddingi mówcy słabo zależą od języka, a modele czysto angielskie
# gorzej radzą sobie z polskim.
EMBEDDING_URL = (
    f"{_RELEASES}/speaker-recongition-models/"
    "3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced.onnx"
)
DEFAULT_MODELS_DIR = Path.home() / ".cache" / "earwitness" / "models"
# Próg klasteryzacji używany gdy nie znamy liczby mówców: wyższy = mniej mówców.
DEFAULT_THRESHOLD = 0.5


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url) as resp, tmp.open("wb") as f:
        shutil.copyfileobj(resp, f)
    tmp.replace(dest)


def ensure_models(models_dir: Path = DEFAULT_MODELS_DIR) -> tuple[Path, Path]:
    """Zwraca (segmentation.onnx, embedding.onnx), pobierając je jeśli brak."""
    segmentation = models_dir / "pyannote-segmentation-3-0" / "model.onnx"
    if not segmentation.exists():
        archive = models_dir / "segmentation.tar.bz2"
        print("  pobieram model segmentacji (pyannote 3.0)...")
        _download(SEGMENTATION_URL, archive)
        with tarfile.open(archive) as tar:
            member = next(m for m in tar.getmembers() if m.name.endswith("model.onnx"))
            segmentation.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            assert src is not None
            with segmentation.open("wb") as out:
                shutil.copyfileobj(src, out)
        archive.unlink()

    embedding = models_dir / Path(EMBEDDING_URL).name
    if not embedding.exists():
        print("  pobieram model embeddingów mówcy...")
        _download(EMBEDDING_URL, embedding)
    return segmentation, embedding


def load_audio_16k_mono(path: Path):
    """Dekoduje dowolne audio/wideo do float32 mono 16 kHz przez ffmpeg."""
    import numpy as np

    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg nie znaleziony w PATH. `brew install ffmpeg`")
    result = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "f32le",
            "-",
        ],  # fmt: skip
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed:\n{result.stderr.decode('utf-8', errors='replace')}"
        )
    return np.frombuffer(result.stdout, dtype=np.float32)


def diarize_local(
    audio_path: Path,
    num_speakers: int | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    models_dir: Path = DEFAULT_MODELS_DIR,
    num_threads: int = 4,
) -> list[DiarSegment]:
    """Diaryzacja lokalna. Zwraca segmenty w tym samym formacie co diarize_cloud."""
    try:
        import sherpa_onnx
    except ImportError as e:
        raise RuntimeError(
            "Brak sherpa-onnx. Zainstaluj: `uv sync --extra local`"
        ) from e

    segmentation, embedding = ensure_models(models_dir)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(segmentation)
            ),
            num_threads=num_threads,
        ),
        embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
            model=str(embedding), num_threads=num_threads
        ),
        clustering=sherpa_onnx.FastClusteringConfig(
            num_clusters=num_speakers or -1, threshold=threshold
        ),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("Nieprawidłowa konfiguracja diaryzacji sherpa-onnx")

    samples = load_audio_16k_mono(audio_path)
    result = (
        sherpa_onnx.OfflineSpeakerDiarization(config)
        .process(samples)
        .sort_by_start_time()
    )
    return [
        DiarSegment(speaker=f"SPEAKER_{s.speaker:02d}", start=s.start, end=s.end)
        for s in result
    ]
