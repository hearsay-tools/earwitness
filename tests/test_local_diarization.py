import sys
import types
from pathlib import Path

from transcripts import local_diarization
from transcripts.cli import run


class _Seg:
    def __init__(self, speaker, start, end):
        self.speaker, self.start, self.end = speaker, start, end


def _fake_sherpa(segments, seen):
    mod = types.ModuleType("sherpa_onnx")

    class _Cfg:
        def __init__(self, **kw):
            seen.update(kw)

        def validate(self):
            return True

    class _Result(list):
        def sort_by_start_time(self):
            return self

    class _Diar:
        def __init__(self, config):
            pass

        def process(self, samples):
            return _Result(segments)

    mod.OfflineSpeakerDiarizationConfig = _Cfg
    mod.OfflineSpeakerSegmentationModelConfig = lambda **kw: kw
    mod.OfflineSpeakerSegmentationPyannoteModelConfig = lambda **kw: kw
    mod.SpeakerEmbeddingExtractorConfig = lambda **kw: kw
    mod.FastClusteringConfig = lambda **kw: kw
    mod.OfflineSpeakerDiarization = _Diar
    return mod


def test_diarize_local_maps_segments_and_passes_num_speakers(monkeypatch, tmp_path):
    seen = {}
    fake = _fake_sherpa([_Seg(1, 3.0, 4.5), _Seg(0, 0.0, 2.5)], seen)
    monkeypatch.setitem(sys.modules, "sherpa_onnx", fake)
    monkeypatch.setattr(
        local_diarization, "ensure_models", lambda d: (Path("seg"), Path("emb"))
    )
    monkeypatch.setattr(local_diarization, "load_audio_16k_mono", lambda p: [0.0])

    out = local_diarization.diarize_local(tmp_path / "a.wav", num_speakers=2)

    assert [(s.speaker, s.start, s.end) for s in out] == [
        ("SPEAKER_01", 3.0, 4.5),
        ("SPEAKER_00", 0.0, 2.5),
    ]
    assert seen["clustering"] == {"num_clusters": 2, "threshold": 0.5}


def test_diarize_local_unknown_speakers_uses_threshold_only(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setitem(sys.modules, "sherpa_onnx", _fake_sherpa([], seen))
    monkeypatch.setattr(
        local_diarization, "ensure_models", lambda d: (Path("seg"), Path("emb"))
    )
    monkeypatch.setattr(local_diarization, "load_audio_16k_mono", lambda p: [0.0])

    local_diarization.diarize_local(tmp_path / "a.wav")

    assert seen["clustering"]["num_clusters"] == -1


def test_pipeline_local_does_not_require_replicate_token(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    monkeypatch.setattr("transcripts.cli.load_dotenv", lambda *a, **k: None)
    missing = tmp_path / "nope.wav"
    monkeypatch.setattr(
        sys, "argv", ["earwitness", "pipeline", str(missing), "-o", str(tmp_path / "o.txt"),
                      "--diarizer", "local"],
    )  # fmt: skip

    assert run() == 2
    err = capsys.readouterr().err
    assert "REPLICATE_API_TOKEN" not in err
    assert "nie istnieje" in err


def test_pipeline_default_still_requires_replicate_token(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    monkeypatch.setattr("transcripts.cli.load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(
        sys, "argv", ["earwitness", "pipeline", str(tmp_path / "a.wav"), "-o", str(tmp_path / "o.txt")]
    )  # fmt: skip

    assert run() == 2
    assert "REPLICATE_API_TOKEN" in capsys.readouterr().err
