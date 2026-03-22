"""
Transcription via faster-whisper (large-v3, CUDA).
Returns a dict with the full text and per-word timestamps.
"""

from faster_whisper import WhisperModel

_model: WhisperModel | None = None


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        _model = WhisperModel("large-v3", device="cuda", compute_type="float16")
    return _model


def transcribe(video_path: str) -> dict:
    """
    Transcribe the audio track of a video file.

    Returns:
        {
            "text": str,           # full transcript
            "words": [
                {"word": str, "start": float, "end": float},
                ...
            ]
        }
    """
    model = _get_model()
    segments, _ = model.transcribe(video_path, word_timestamps=True)

    words = []
    full_text_parts = []

    for segment in segments:
        full_text_parts.append(segment.text.strip())
        if segment.words:
            for w in segment.words:
                words.append({
                    "word": w.word,
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                })

    return {
        "text": " ".join(full_text_parts),
        "words": words,
    }
