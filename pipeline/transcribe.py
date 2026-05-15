"""
Transcription via Deepgram Nova-3 REST API with word-level timestamps.

Returns the same {"text", "words"} shape as the previous WhisperX implementation
so the rest of the pipeline (captions, clip finder, export) is unaffected.

Deepgram is orders of magnitude faster than local WhisperX — a 1-hour recording
transcribes in ~10 seconds via the cloud API rather than ~5 minutes locally.

Requires DEEPGRAM_API_KEY in .env.

Upload strategy:
  WAV files from DeepFilterNet3 are 48kHz and large (~340MB/hr). We transcode
  to MP3 128kbps mono via FFmpeg first (~60MB/hr). We call the Deepgram REST API
  directly with httpx (timeout=300s) rather than via the SDK, because the SDK's
  internal httpx client has a hardcoded write timeout that can't be overridden.
"""

import os
import subprocess
import tempfile

import httpx

_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"

_FFMPEG_BIN = r"C:\Users\ntmas\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
_FFMPEG_EXE = os.path.join(_FFMPEG_BIN, "ffmpeg.exe")
if _FFMPEG_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _FFMPEG_BIN + os.pathsep + os.environ.get("PATH", "")


def _wav_to_mp3(wav_path: str) -> str:
    """
    Transcode a WAV to 128kbps mono MP3 in a temp file.
    Returns the temp MP3 path — caller is responsible for deletion.
    """
    tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    tmp.close()
    result = subprocess.run(
        [_FFMPEG_EXE, "-y", "-i", wav_path, "-ac", "1", "-b:a", "128k", tmp.name],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg MP3 transcode failed:\n{result.stderr.decode(errors='replace')}")
    return tmp.name


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

    # Transcode to MP3 (~60MB/hr vs ~340MB/hr WAV) before uploading
    mp3_path = _wav_to_mp3(audio_path)
    try:
        with open(mp3_path, "rb") as f:
            audio_bytes = f.read()
    finally:
        os.unlink(mp3_path)

    # Call Deepgram REST API directly with a 5-minute timeout
    timeout = httpx.Timeout(connect=10, write=300, read=120, pool=5)
    with httpx.Client(timeout=timeout) as http:
        response = http.post(
            _DEEPGRAM_URL,
            headers={
                "Authorization": f"Token {api_key}",
                "Content-Type": "audio/mpeg",
            },
            params={
                "model": "nova-3",
                "smart_format": "true",
                "words": "true",
            },
            content=audio_bytes,
        )
        response.raise_for_status()
        data = response.json()

    alt = data["results"]["channels"][0]["alternatives"][0]

    words = [
        {"word": w["word"], "start": w["start"], "end": w["end"]}
        for w in (alt.get("words") or [])
        if w.get("start") is not None and w.get("end") is not None
    ]

    return {"text": alt["transcript"], "words": words}
