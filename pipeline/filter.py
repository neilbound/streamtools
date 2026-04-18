"""
Profanity filter for cleaned audio.
Replaces offensive words with a 1kHz beep tone, using Whisper word timestamps
to locate each word precisely in the audio stream.
"""

import math

import torch
import torchaudio
from better_profanity import profanity as _profanity

_profanity.load_censor_words(whitelist_words=["god", "trashy"])


def censor_transcript(transcript: dict) -> tuple[dict, list[str]]:
    """
    Return a new transcript with profane words replaced by **** in both
    the full text string and the per-word list (used for captions and Claude).

    Returns:
        (censored_transcript_dict, list_of_original_censored_words)
    """
    censored_originals = []
    new_words = []

    for w in transcript["words"]:
        if _profanity.contains_profanity(w["word"].strip()):
            censored_originals.append(w["word"].strip())
            new_words.append({**w, "word": _profanity.censor(w["word"])})
        else:
            new_words.append(w)

    return (
        {"text": _profanity.censor(transcript["text"]), "words": new_words},
        censored_originals,
    )


def filter_profanity(audio_path: str, words: list[dict], output_path: str) -> tuple[str, list[str]]:
    """
    Replace each offensive word in audio_path with a 1kHz beep tone.

    Args:
        audio_path:  Path to the cleaned WAV file (output of audio_clean.py).
        words:       Word list from transcription — [{word, start, end}].
        output_path: Where to save the filtered WAV.

    Returns:
        (output_path, list_of_censored_words)
    """
    waveform, sr = torchaudio.load(audio_path)
    censored = []

    for w in words:
        if not _profanity.contains_profanity(w["word"].strip()):
            continue

        start_sample = int(w["start"] * sr)
        end_sample   = int(w["end"]   * sr)
        duration     = end_sample - start_sample

        if duration <= 0:
            continue

        # 1kHz sine wave at -10 dBFS (amplitude ≈ 0.316)
        t    = torch.linspace(0, duration / sr, duration)
        beep = (0.316 * torch.sin(2 * math.pi * 1000 * t)).unsqueeze(0)

        # Broadcast across all channels (handles mono and stereo)
        beep = beep.expand(waveform.shape[0], -1)
        waveform[:, start_sample:end_sample] = beep
        censored.append(w["word"].strip())

    torchaudio.save(output_path, waveform, sr)
    return output_path, censored
