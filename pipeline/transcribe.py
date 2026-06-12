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

import json
import os
import subprocess
import tempfile
import time

import httpx

_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
_MAX_ATTEMPTS = 3

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


def _parse_deepgram_response(data: dict) -> dict:
    """
    Unpack Deepgram's nested response into the pipeline's {"text", "words"}
    contract, with clear errors instead of KeyError/IndexError crashes when
    the response shape is unexpected.
    """
    try:
        alt = data["results"]["channels"][0]["alternatives"][0]
    except (KeyError, IndexError, TypeError):
        snippet = json.dumps(data)[:300]
        raise RuntimeError(
            f"Deepgram returned an unexpected response shape: {snippet}"
        )

    words = [
        {"word": w["word"], "start": w["start"], "end": w["end"]}
        for w in (alt.get("words") or [])
        if w.get("start") is not None and w.get("end") is not None
    ]
    text = alt.get("transcript", "")

    if not text and not words:
        raise RuntimeError(
            "Deepgram returned an empty transcript — check the audio file "
            "(silent, truncated, or wrong format?)"
        )

    return {"text": text, "words": words}


def transcribe(audio_path: str) -> dict:
    """
    Transcribe audio_path using Deepgram Nova-3 with word-level timestamps.

    Retries transient failures (network errors, 429, 5xx) up to 3 times with
    exponential backoff. Other 4xx errors (e.g. a bad API key) raise
    immediately with the response body so the cause is visible.

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
    last_error: Exception | None = None
    with httpx.Client(timeout=timeout) as http:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
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
            except httpx.TransportError as exc:
                last_error = exc
                print(f"[transcribe] Attempt {attempt}/{_MAX_ATTEMPTS}: "
                      f"network error — {type(exc).__name__}: {exc}")
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(2 ** (attempt - 1))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_error = RuntimeError(
                    f"Deepgram HTTP {response.status_code}: {response.text[:300]}"
                )
                print(f"[transcribe] Attempt {attempt}/{_MAX_ATTEMPTS}: "
                      f"HTTP {response.status_code} — retrying")
                if attempt < _MAX_ATTEMPTS:
                    time.sleep(2 ** (attempt - 1))
                continue

            if response.status_code >= 400:
                # Other 4xx won't heal on retry (bad key, bad request) — fail
                # loudly with the body so the operator sees why.
                raise RuntimeError(
                    f"Deepgram rejected the request (HTTP {response.status_code}): "
                    f"{response.text[:500]}"
                )

            return _parse_deepgram_response(response.json())

    raise RuntimeError(
        f"Deepgram transcription failed after {_MAX_ATTEMPTS} attempts. "
        f"Last error: {last_error}"
    )
