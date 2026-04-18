"""
Transcription via WhisperX (large-v3, CUDA) with phoneme-level forced alignment.

WhisperX runs two passes:
  1. Whisper large-v3  — high-quality transcription
  2. wav2vec2 aligner  — frame-accurate per-word timestamps

Returns a dict with the full text and per-word timestamps.
"""

import whisperx

_model = None
_align_model = None
_align_metadata = None
_DEVICE = "cuda"
_COMPUTE_TYPE = "float16"


def _get_model():
    global _model
    if _model is None:
        _model = whisperx.load_model("large-v3", _DEVICE, compute_type=_COMPUTE_TYPE)
    return _model


def _get_align_model(language: str):
    global _align_model, _align_metadata
    if _align_model is None or getattr(_align_model, "_language", None) != language:
        _align_model, _align_metadata = whisperx.load_align_model(
            language_code=language, device=_DEVICE
        )
        _align_model._language = language
    return _align_model, _align_metadata


def transcribe(audio_path: str) -> dict:
    """
    Transcribe audio with WhisperX and return phoneme-aligned word timestamps.

    Args:
        audio_path: Path to a WAV file (output of audio_clean.py).

    Returns:
        {
            "text": str,
            "words": [{"word": str, "start": float, "end": float}, ...]
        }
    """
    model = _get_model()

    # Step 1: Whisper transcription
    audio = whisperx.load_audio(audio_path)
    result = model.transcribe(audio, batch_size=4)
    language = result.get("language", "en")

    # Step 2: Forced phoneme alignment
    align_model, metadata = _get_align_model(language)
    aligned = whisperx.align(
        result["segments"], align_model, metadata, audio, _DEVICE,
        return_char_alignments=False,
    )

    words = []
    full_text_parts = []

    for segment in aligned["segments"]:
        full_text_parts.append(segment["text"].strip())
        for w in segment.get("words", []):
            # WhisperX may omit start/end on rare words — skip those
            if "start" not in w or "end" not in w:
                continue
            words.append({
                "word": w["word"],
                "start": round(w["start"], 3),
                "end": round(w["end"], 3),
            })

    return {
        "text": " ".join(full_text_parts),
        "words": words,
    }
