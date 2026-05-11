"""
Video export via FFmpeg.
Cuts a clip from the source video, replaces the audio with the cleaned
version, burns in ASS karaoke captions, and optionally composites a
full-width chyron bar at the bottom for the description.

Uses subprocess directly (not ffmpeg-python) for the export so the ASS
filename can be passed without a Windows drive-letter colon, which
FFmpeg's filter parser cannot handle reliably on Windows.
"""

import os
import shutil
import subprocess
import tempfile

import ffmpeg  # still used for probe / get_video_duration

# Ensure FFmpeg is findable regardless of PATH configuration
_FFMPEG_BIN = r"C:\Users\ntmas\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-8.1-full_build\bin"
_FFMPEG_EXE = os.path.join(_FFMPEG_BIN, "ffmpeg.exe")
if _FFMPEG_BIN not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _FFMPEG_BIN + os.pathsep + os.environ.get("PATH", "")


def _find_badge_font() -> str:
    """Return absolute path to description font, falling back to common system fonts."""
    candidates = [
        r"C:\Windows\Fonts\Montserrat-Bold.ttf",
        r"C:\Users\ntmas\AppData\Local\Microsoft\Windows\Fonts\Montserrat-Bold.ttf",
        r"C:\Windows\Fonts\Montserrat-SemiBold.ttf",
        r"C:\Users\ntmas\AppData\Local\Microsoft\Windows\Fonts\Montserrat-SemiBold.ttf",
        r"C:\Windows\Fonts\Montserrat-ExtraBold.ttf",
        r"C:\Users\ntmas\AppData\Local\Microsoft\Windows\Fonts\Montserrat-ExtraBold.ttf",
        r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\Arial.ttf",
        r"C:\Windows\Fonts\calibri.ttf",
        r"C:\Windows\Fonts\segoeui.ttf",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return ""


def _description_filter(description: str, font_basename: str) -> str:
    """
    Build a full-width chyron bar filter: drawbox (dark background) +
    drawtext (white text centred in the bar).
    font_basename is the filename only — FFmpeg resolves it via cwd=tmp_dir.
    """
    normalized = description.strip().replace("\\n", "\n")
    line = normalized.split("\n")[0].strip()
    if not line:
        return ""

    font_size = 54
    bar_h = font_size + 48
    bar_y = f"ih-{bar_h}"
    text_y = f"h-{bar_h // 2 + font_size // 2}"

    escaped = line.replace("\\", "\\\\").replace("'", "\u2019").replace(":", "\\:").replace("|", "\\|")
    font_opt = f"fontfile={font_basename}:" if font_basename else ""

    bar = f"drawbox=x=0:y={bar_y}:w=iw:h={bar_h}:color=black:t=fill"
    text = (
        f"drawtext={font_opt}text='{escaped}':"
        f"fontsize={font_size}:fontcolor=white:"
        f"borderw=2:bordercolor=black@0.8:"
        f"shadowx=3:shadowy=3:shadowcolor=black@0.9:"
        f"x=(w-text_w)/2:y={text_y}"
    )
    return f"{bar},{text}"


def _run_ffmpeg(cmd: list[str], cwd: str | None = None) -> None:
    """Run an FFmpeg command list, raising RuntimeError with stderr on failure."""
    result = subprocess.run(cmd, capture_output=True, cwd=cwd)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{result.stderr.decode(errors='replace')}")


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
    Cut a clip and export with cleaned audio, burned-in karaoke captions,
    and an optional chyron bar at the bottom for the description.
    """
    duration = end - start
    tmp_dir = tempfile.gettempdir()

    # Stage ASS by filename so FFmpeg can reference it without a drive-letter path
    shutil.copy2(ass_path, os.path.join(tmp_dir, "st_caps.ass"))

    font_src = _find_badge_font()
    font_basename = ""
    if font_src:
        font_basename = os.path.basename(font_src)
        shutil.copy2(font_src, os.path.join(tmp_dir, font_basename))

    vf = "ass=st_caps.ass"
    desc_filter = _description_filter(description, font_basename)
    if desc_filter:
        vf = f"{vf},{desc_filter}"

    cmd = [_FFMPEG_EXE, "-y",
           "-accurate_seek", "-ss", str(start), "-t", str(duration), "-i", video_path]
    if clean_audio_path:
        cmd += ["-accurate_seek", "-ss", str(start), "-t", str(duration), "-i", clean_audio_path]
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    cmd += ["-vf", vf,
            "-vcodec", "libx264", "-acodec", "aac", "-b:a", "192k",
            "-crf", "18", "-preset", "fast", "-movflags", "+faststart",
            output_path]

    _run_ffmpeg(cmd, cwd=tmp_dir)
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

    cmd = [_FFMPEG_EXE, "-y",
           "-accurate_seek", "-ss", str(start), "-t", str(duration), "-i", video_path]
    if clean_audio_path:
        cmd += ["-accurate_seek", "-ss", str(start), "-t", str(duration), "-i", clean_audio_path]
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a:0"]
    cmd += ["-vcodec", "libx264", "-acodec", "aac", "-b:a", "192k",
            "-crf", "18", "-preset", "fast", "-movflags", "+faststart",
            output_path]

    _run_ffmpeg(cmd)
    return output_path


def compose_portrait(
    video_paths: list[str],
    output_path: str,
    canvas_w: int = 1080,
    canvas_h: int = 1920,
    fill: bool = True,
) -> str:
    """
    Stack 2–4 landscape recordings vertically into a single portrait video.

    fill=True  — scale each clip to fill its slot (center crop, no black bars)
    fill=False — scale each clip to fit its slot (letterbox with black bars)

    Audio is taken from the first input; all Streamyard local recordings
    carry the full mixed audio so any input would work.
    """
    n = len(video_paths)
    if not 2 <= n <= 4:
        raise ValueError(f"compose_portrait requires 2–4 videos, got {n}")

    slot_h = canvas_h // n
    filter_parts = []

    for i in range(n):
        if fill:
            filt = (
                f"[{i}:v]scale={canvas_w}:{slot_h}:force_original_aspect_ratio=increase,"
                f"crop={canvas_w}:{slot_h}[v{i}]"
            )
        else:
            filt = (
                f"[{i}:v]scale={canvas_w}:{slot_h}:force_original_aspect_ratio=decrease,"
                f"pad={canvas_w}:{slot_h}:(ow-iw)/2:(oh-ih)/2:black[v{i}]"
            )
        filter_parts.append(filt)

    vstack_in = "".join(f"[v{i}]" for i in range(n))
    filter_parts.append(f"{vstack_in}vstack=inputs={n}[vout]")

    # Mix all audio tracks — each local Streamyard recording only carries
    # that participant's own audio, so we need to combine them all.
    audio_in = "".join(f"[{i}:a]" for i in range(n))
    filter_parts.append(f"{audio_in}amix=inputs={n}:duration=longest:normalize=0[aout]")

    cmd = [_FFMPEG_EXE, "-y"]
    for vp in video_paths:
        cmd += ["-i", vp]
    cmd += [
        "-filter_complex", ";".join(filter_parts),
        "-map", "[vout]",
        "-map", "[aout]",
        "-vcodec", "h264_nvenc", "-preset", "p4", "-cq", "18",
        "-acodec", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(cmd)
    return output_path


def get_video_duration(video_path: str) -> float:
    """Return the duration of a video file in seconds."""
    probe = ffmpeg.probe(video_path)
    return float(probe["format"]["duration"])
