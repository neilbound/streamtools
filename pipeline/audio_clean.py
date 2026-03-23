"""
Audio cleanup via Facebook's Denoiser.
Extracts the audio track from a video, applies AI speech denoising,
and saves the cleaned audio as a WAV file. Runs locally on GPU, no API cost.
"""

import os
import subprocess
import tempfile

# Ensure FFmpeg is findable regardless of PATH configuration
_FFMPEG_BIN = r"C:\Users\ntmas\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
if _FFMPEG_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _FFMPEG_BIN + os.pathsep + os.environ.get("PATH", "")

import torch
import torchaudio

_model = None


def _get_model(device: str):
    global _model
    if _model is None:
        from denoiser import pretrained
        _model = pretrained.dns64().to(device)
        _model.eval()
    return _model


def clean_audio(video_path: str, output_path: str) -> str:
    """
    Extract audio from video_path, run Facebook Denoiser speech enhancement,
    and save the result to output_path (.wav).

    Returns the output_path on success.
    """
    device = "cuda" if torch.cuda.is_available() else "cpu"

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        raw_audio_path = tmp.name

    try:
        # Extract audio from video at 16kHz mono (denoiser's native sample rate)
        subprocess.run(
            [
                "ffmpeg", "-y",
                "-i", video_path,
                "-vn",
                "-ar", "16000",
                "-ac", "1",
                "-f", "wav",
                raw_audio_path,
            ],
            check=True,
            capture_output=True,
        )

        # Load audio
        wav, sr = torchaudio.load(raw_audio_path)
        wav = wav.to(device)

        # Run denoiser
        model = _get_model(device)
        with torch.no_grad():
            wav_denoised = model(wav[None])[0]

        # Save
        torchaudio.save(output_path, wav_denoised.cpu(), sr)

    finally:
        if os.path.exists(raw_audio_path):
            os.remove(raw_audio_path)

    return output_path
