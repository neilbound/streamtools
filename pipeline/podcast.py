"""
pipeline/podcast.py — Podcast MP3 export with ID3 metadata tagging.

Encodes a cleaned WAV to a 192kbps MP3 suitable for Spotify / Apple Podcasts
upload, then writes ID3 tags (title, artist, album, track, description, cover art).

Spotify for Podcasters has no public upload API — this module generates the file
and returns the Spotify dashboard URL for manual upload.

Functions:
    export_podcast_mp3(audio_path, output_path, title, description,
                       episode_number, show_name, cover_art_path) -> dict
"""

import os
import subprocess

# Re-use the same FFmpeg binary path already configured in export.py
_FFMPEG_BIN = r"C:\Users\ntmas\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
_FFMPEG_EXE = os.path.join(_FFMPEG_BIN, "ffmpeg.exe")
if _FFMPEG_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _FFMPEG_BIN + os.pathsep + os.environ.get("PATH", "")

_SPOTIFY_UPLOAD_URL = "https://podcasters.spotify.com/pod/dashboard/episodes/new"


def _run_ffmpeg(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr.decode(errors='replace')}")


def export_podcast_mp3(
    audio_path: str,
    output_path: str,
    title: str,
    description: str = "",
    episode_number: int | str = "",
    show_name: str = "",
    cover_art_path: str | None = None,
) -> dict:
    """
    Transcode a cleaned WAV to a podcast-standard MP3 and write ID3 tags.

    Args:
        audio_path:      Absolute path to the source WAV (48kHz mono from DeepFilterNet3).
        output_path:     Destination .mp3 path.
        title:           Episode title (ID3 TIT2).
        description:     Episode description / show notes (ID3 COMM).
        episode_number:  Track number for ID3 TRCK tag (e.g. 42 or "42/100").
        show_name:       Show name used for TPE1 (artist) and TALB (album) tags.
        cover_art_path:  Optional path to a JPEG/PNG cover art image for ID3 APIC tag.

    Returns:
        {
            "path": str,
            "title": str,
            "show_name": str,
            "episode_number": str,
            "duration_seconds": float,
            "size_mb": float,
            "spotify_upload_url": str,
        }
    """
    # ── Step 1: FFmpeg transcode WAV → MP3 ────────────────────────────────────
    # 192kbps CBR, 44.1kHz stereo — podcast standard
    # (DeepFilterNet3 outputs 48kHz mono; resample + upmix to stereo here)
    cmd = [
        _FFMPEG_EXE, "-y",
        "-i", audio_path,
        "-vn",
        "-acodec", "libmp3lame",
        "-b:a", "192k",
        "-ar", "44100",
        "-ac", "2",
        output_path,
    ]
    _run_ffmpeg(cmd)

    # ── Step 2: Write ID3 tags via mutagen ────────────────────────────────────
    try:
        from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1, TALB, TRCK, COMM, APIC

        try:
            tags = ID3(output_path)
        except ID3NoHeaderError:
            tags = ID3()

        if title:
            tags["TIT2"] = TIT2(encoding=3, text=title)
        if show_name:
            tags["TPE1"] = TPE1(encoding=3, text=show_name)
            tags["TALB"] = TALB(encoding=3, text=show_name)
        if episode_number:
            tags["TRCK"] = TRCK(encoding=3, text=str(episode_number))
        if description:
            tags["COMM"] = COMM(encoding=3, lang="eng", desc="desc", text=description)
        if cover_art_path and os.path.exists(cover_art_path):
            ext = os.path.splitext(cover_art_path)[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            with open(cover_art_path, "rb") as img_f:
                tags["APIC"] = APIC(
                    encoding=3,
                    mime=mime,
                    type=3,     # Cover (front)
                    desc="Cover",
                    data=img_f.read(),
                )

        tags.save(output_path)

    except ImportError:
        print("[podcast] Warning: mutagen not installed — ID3 tags skipped. Run: pip install mutagen")
    except Exception as e:
        print(f"[podcast] Warning: ID3 tagging failed ({e}) — MP3 is still valid, just untagged")

    # ── Step 3: Collect stats ─────────────────────────────────────────────────
    size_mb = os.path.getsize(output_path) / (1024 * 1024)

    # Probe duration from the encoded MP3 (not the WAV source)
    try:
        import ffmpeg as _ffmpeg
        probe = _ffmpeg.probe(output_path)
        duration_seconds = float(probe["format"]["duration"])
    except Exception:
        duration_seconds = 0.0

    return {
        "path":              output_path,
        "title":             title,
        "show_name":         show_name,
        "episode_number":    str(episode_number),
        "duration_seconds":  duration_seconds,
        "size_mb":           round(size_mb, 2),
        "spotify_upload_url": _SPOTIFY_UPLOAD_URL,
    }
