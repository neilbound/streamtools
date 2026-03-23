"""
Video export via FFmpeg.
Cuts a clip from the source video, replaces the audio with the cleaned
version, burns in ASS karaoke captions, and outputs an mp4.
"""

import os
import ffmpeg

# Ensure FFmpeg is findable regardless of PATH configuration
_FFMPEG_BIN = r"C:\Users\ntmas\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
if _FFMPEG_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _FFMPEG_BIN + os.pathsep + os.environ.get("PATH", "")


def _description_filter(description: str) -> str:
    """
    Build FFmpeg drawtext filter chain for a top-of-screen description overlay.
    Supports up to 3 lines separated by \\n in the description string.
    """
    lines = [ln for ln in description.strip().split("\\n") if ln.strip()][:3]
    if not lines:
        return ""

    font_path = "C\\:/Windows/Fonts/arial.ttf"
    font_size = 28
    line_height = font_size + 10
    parts = []

    for i, line in enumerate(lines):
        y = 40 + i * line_height
        escaped = line.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
        parts.append(
            f"drawtext=fontfile={font_path}:text='{escaped}':"
            f"fontsize={font_size}:fontcolor=white:"
            f"x=(w-text_w)/2:y={y}:"
            f"box=1:boxcolor=black@0.6:boxborderw=10"
        )

    return ",".join(parts)


def export_clip(
    video_path: str,
    clean_audio_path: str,
    ass_path: str,
    start: float,
    end: float,
    output_path: str,
    description: str = "",
) -> str:
    """
    Cut a clip and export with cleaned audio and burned-in karaoke captions.

    Args:
        video_path:       source video file
        clean_audio_path: DeepFilterNet-cleaned WAV (full video length)
        ass_path:         ASS subtitle file (timestamps relative to clip start)
        start:            clip start time in seconds
        end:              clip end time in seconds
        output_path:      destination mp4 path

    Returns:
        output_path
    """
    duration = end - start

    # Escape the ASS path for FFmpeg's vf filter (backslashes and colons)
    ass_escaped = ass_path.replace("\\", "/").replace(":", "\\:")
    vf = f"ass={ass_escaped}"
    desc_filter = _description_filter(description)
    if desc_filter:
        vf = f"{vf},{desc_filter}"

    (
        ffmpeg
        .input(video_path, ss=start, t=duration)
        .output(
            ffmpeg.input(clean_audio_path, ss=start, t=duration).audio,
            output_path,
            vf=vf,
            vcodec="libx264",
            acodec="aac",
            audio_bitrate="192k",
            crf=18,
            preset="fast",
            movflags="+faststart",
        )
        .overwrite_output()
        .run(capture_stdout=True, capture_stderr=True)
    )

    return output_path


def export_clip_clean(
    video_path: str,
    clean_audio_path: str,
    start: float,
    end: float,
    output_path: str,
) -> str:
    """Export a clip with cleaned audio but no burned-in captions (for YouTube)."""
    duration = end - start

    (
        ffmpeg
        .input(video_path, ss=start, t=duration)
        .output(
            ffmpeg.input(clean_audio_path, ss=start, t=duration).audio,
            output_path,
            vcodec="libx264",
            acodec="aac",
            audio_bitrate="192k",
            crf=18,
            preset="fast",
            movflags="+faststart",
        )
        .overwrite_output()
        .run(capture_stdout=True, capture_stderr=True)
    )

    return output_path


def get_video_duration(video_path: str) -> float:
    """Return the duration of a video file in seconds."""
    probe = ffmpeg.probe(video_path)
    return float(probe["format"]["duration"])
