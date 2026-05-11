"""
Transcription via Deepgram Nova-3 API with word-level timestamps.

Returns the same {"text", "words"} shape as the previous WhisperX implementation
so the rest of the pipeline (captions, clip finder, export) is unaffected.

Deepgram is orders of magnitude faster than local WhisperX — a 1-hour recording
transcribes in ~10 seconds via the cloud API rather than ~5 minutes locally.

Requires DEEPGRAM_API_KEY in .env.
Uses deepgram-sdk v7+ API: client.listen.v1.media.transcribe_file()
"""

import os

from deepgram import DeepgramClient


def transcribe(audio_path: str) -> dict:
    """
    Transcribe audio_path using Deepgram Nova-3 with word-level timestamps.

    Args:
        audio_path: Path to a WAV file (output of audio_clean.py).

    Returns:
        {"text": str, "words": [{"word": str, "start": float, "end": float}, ...]}
    """
    api_key = os.environ.get("DEEPGRAM_API_KEY")
    if not api_key:
        raise ValueError("DEEPGRAM_API_KEY not set — add it to your .env file.")

    client = DeepgramClient(api_key=api_key)

    with open(audio_path, "rb") as f:
        audio_bytes = f.read()

    response = client.listen.v1.media.transcribe_file(
        request=audio_bytes,
        model="nova-3",
        smart_format=True,
    )

    alt = response.results.channels[0].alternatives[0]

    words = [
        {"word": w.word, "start": w.start, "end": w.end}
        for w in (alt.words or [])
        if w.start is not None and w.end is not None
    ]

    return {"text": alt.transcript, "words": words}
