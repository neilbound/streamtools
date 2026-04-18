"""
Audio cleanup via DeepFilterNet3.
Extracts the audio track from a video, applies AI speech enhancement
(noise removal + perceptual quality improvement), and saves as WAV.
Runs locally on GPU/CPU — no API cost.

DeepFilterNet3 operates at 48kHz natively (higher quality than the old
Facebook Denoiser which ran at 16kHz). WhisperX handles resampling internally.
"""

import os
import subprocess
import tempfile

# Ensure FFmpeg is findable regardless of PATH configuration
_FFMPEG_BIN = r"C:\Users\ntmas\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
if _FFMPEG_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _FFMPEG_BIN + os.pathsep + os.environ.get("PATH", "")

import torch
from df.enhance import enhance, init_df, load_audio, save_audio

_model = None
_df_state = None

_CHUNK_SECS = 300  # process in 5-minute chunks to avoid CUDA OOM on long files


def _get_model():
    global _model, _df_state
    if _model is None:
        _model, _df_state, _ = init_df()
    return _model, _df_state


def _enhance_chunked(model, df_state, audio):
    """Enhance audio in chunks to stay within VRAM limits on long recordings."""
    sr = df_state.sr()
    chunk_samples = _CHUNK_SECS * sr
    total_samples = audio.shape[-1]

    if total_samples <= chunk_samples:
        return enhance(model, df_state, audio)

    chunks = []
    for start in range(0, total_samples, chunk_samples):
        chunk = audio[..., start:start + chunk_samples]
        chunks.append(enhance(model, df_state, chunk))
        torch.cuda.empty_cache()
    return torch.cat(chunks, dim=-1)


def clean_audio(video_path: str, output_path: str) -> str:
    """
    Extract audio from video_path, run DeepFilterNet3 speech enhancement,
    and save the result to output_path (.wav, 48kHz mono).

    Returns the output_path on success.
    """
    model, df_state = _get_model()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        raw_audio_path = tmp.name

    try:
        # Extract audio at DeepFilterNet3's native sample rate (48kHz mono)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", video_path,
                "-vn",
                "-ar", str(df_state.sr()),
                "-ac", "1",
                "-f", "wav",
                raw_audio_path,
            ],
            check=True,
            capture_output=True,
        )

        audio, _ = load_audio(raw_audio_path, sr=df_state.sr())
        enhanced = _enhance_chunked(model, df_state, audio)
        save_audio(output_path, enhanced, df_state.sr())

    finally:
        if os.path.exists(raw_audio_path):
            os.remove(raw_audio_path)

    # Free VRAM — WhisperX needs the headroom and DeepFilterNet3 is no longer needed
    global _model, _df_state
    _model = None
    _df_state = None
    torch.cuda.empty_cache()

    return output_path
