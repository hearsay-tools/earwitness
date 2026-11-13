from pathlib import Path
from typing import Optional

from elevenlabs.client import ElevenLabs
from elevenlabs.speech_to_text.types.speech_to_text_convert_response import (
    SpeechToTextConvertResponse,
)


def transcribe(
    file_path: Path,
    api_key: str,
    num_speakers: Optional[int] = None,
    diarization_threshold: Optional[float] = None,
    diarize: bool = True,
    model_id: str = "scribe_v3",
    language_code: Optional[str] = None,
) -> SpeechToTextConvertResponse:
    if num_speakers is not None and diarization_threshold is not None:
        raise ValueError(
            "num_speakers i diarization_threshold są wzajemnie wykluczające — "
            "ustaw tylko jeden."
        )
    if not diarize and (num_speakers is not None or diarization_threshold is not None):
        raise ValueError(
            "diarize=False nie ma sensu razem z num_speakers/diarization_threshold."
        )

    client = ElevenLabs(api_key=api_key, timeout=1800)
    with open(file_path, "rb") as audio_file:
        kwargs = dict(
            file=audio_file,
            model_id=model_id,
            diarize=diarize,
            timestamps_granularity="word",
        )
        if num_speakers is not None:
            kwargs["num_speakers"] = num_speakers
        if diarization_threshold is not None:
            kwargs["diarization_threshold"] = diarization_threshold
        if language_code is not None:
            kwargs["language_code"] = language_code
        return client.speech_to_text.convert(**kwargs)
