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

    font_size = 27
    bar_h = font_size + 24
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


def _run_ffmpeg(
    cmd: list[str],
    cwd: str | None = None,
    expected_output: str | None = None,
    min_bytes: int = 10_000,
    probe_output: bool = False,
) -> None:
    """
    Run an FFmpeg command list, raising RuntimeError with stderr on failure.

    If expected_output is given, also verify the output file exists and is at
    least min_bytes — FFmpeg can exit 0 after writing a truncated file (disk
    full, NVENC session errors). With probe_output=True, additionally ffprobe
    the file and require a video stream; use for intermediates that are reused
    on resume, where a corrupt file would silently poison later steps.
    """
    result = subprocess.run(cmd, capture_output=True, cwd=cwd)
    stderr = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg failed:\n{stderr}")

    if expected_output is not None:
        if not os.path.exists(expected_output):
            raise RuntimeError(
                f"FFmpeg exited 0 but produced no output file: {expected_output}\n"
                f"stderr tail:\n{stderr[-2000:]}"
            )
        size = os.path.getsize(expected_output)
        if size < min_bytes:
            raise RuntimeError(
                f"FFmpeg exited 0 but output is only {size} bytes "
                f"(< {min_bytes}): {expected_output}\n"
                f"stderr tail:\n{stderr[-2000:]}"
            )
        if probe_output:
            try:
                probe = ffmpeg.probe(expected_output)
                streams = probe.get("streams", [])
                if not any(s.get("codec_type") == "video" for s in streams):
                    raise RuntimeError(
                        f"FFmpeg output has no video stream: {expected_output}"
                    )
            except RuntimeError:
                raise
            except Exception as exc:
                raise RuntimeError(
                    f"FFmpeg output failed probe ({exc}): {expected_output}"
                )


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

    The video source must already be in its final aspect ratio — the shorts
    pipeline sources clips from the vertical StreamYard file (already 9:16).
    When clean_audio_path is given the audio track is replaced; both video and
    audio are seeked to the same [start, end] window so they stay in sync.
    """
    duration = end - start

    # Per-call temp dir so concurrent exports never clobber each other's staged
    # ASS/font files (FFmpeg references them by basename via cwd=tmp_dir).
    tmp_dir = tempfile.mkdtemp(prefix="st_export_")
    try:
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
        _run_ffmpeg(cmd, cwd=tmp_dir, expected_output=output_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return output_path


def export_clip_clean(
    video_path: str,
    clean_audio_path: str,
    start: float,
    end: float,
    output_path: str,
) -> str:
    """
    Export a clip with cleaned audio but no burned-in captions (for YouTube).

    The video source must already be in its final aspect ratio. When
    clean_audio_path is given the audio track is replaced; both video and audio
    are seeked to the same [start, end] window so they stay in sync.
    """
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
    _run_ffmpeg(cmd, expected_output=output_path)

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

    # Reset each video stream's timestamps to zero before stacking.
    # Streamyard local recordings start at slightly different wall-clock times
    # (visible in the ms offset in filenames), so raw PTS values diverge and
    # cause audio/video drift in the composed output.
    for i in range(n):
        if fill:
            filt = (
                f"[{i}:v]setpts=PTS-STARTPTS,scale={canvas_w}:{slot_h}:force_original_aspect_ratio=increase,"
                f"crop={canvas_w}:{slot_h}[v{i}]"
            )
        else:
            filt = (
                f"[{i}:v]setpts=PTS-STARTPTS,scale={canvas_w}:{slot_h}:force_original_aspect_ratio=decrease,"
                f"pad={canvas_w}:{slot_h}:(ow-iw)/2:(oh-ih)/2:black[v{i}]"
            )
        filter_parts.append(filt)

    vstack_in = "".join(f"[v{i}]" for i in range(n))
    filter_parts.append(f"{vstack_in}vstack=inputs={n}[vout]")

    # Reset audio timestamps and mix all tracks.
    # asetpts=PTS-STARTPTS aligns each audio stream to t=0 before mixing,
    # matching the video PTS reset above.
    for i in range(n):
        filter_parts.append(f"[{i}:a]asetpts=PTS-STARTPTS[a{i}]")
    audio_in = "".join(f"[a{i}]" for i in range(n))
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
    # probe_output: this NVENC intermediate is reused on resume — a corrupt
    # file here silently poisons every later step.
    _run_ffmpeg(cmd, expected_output=output_path, probe_output=True)
    return output_path


def export_episode_youtube(
    video_path: str,
    audio_path: str,
    output_path: str,
) -> str:
    """
    Mux the full 16:9 episode video with cleaned audio and encode for YouTube.

    No trimming — full duration. No subtitles burned in (SRT uploaded separately).

    Stream-copies the video track (no re-encode) and only encodes the swapped
    audio. The source is always already H.264 in an MP4 container — StreamYard
    downloads, stream-copied segment stitches, and NVENC-composed portraits are
    all H.264 — so a copy is lossless, YouTube-compatible, and turns a multi-minute
    encode of an 80-minute episode into a ~10-second mux.

    Args:
        video_path:  Absolute path to the source video (16:9 StreamYard horizontal download).
        audio_path:  Absolute path to the cleaned/filtered WAV.
        output_path: Destination MP4 path.

    Returns:
        output_path
    """
    cmd = [
        _FFMPEG_EXE, "-y",
        "-i", video_path,
        "-i", audio_path,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-acodec", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_ffmpeg(cmd, expected_output=output_path)
    return output_path


def stitch_segments(
    video_paths: list[str],
    output_path: str,
) -> tuple[str, list[float]]:
    """
    Concatenate video files in recording order into a single output file.

    Uses the FFmpeg concat demuxer with stream-copy (no re-encode) — instant
    and lossless. All inputs must share the same codec, resolution, and frame
    rate, which is guaranteed for files from the same StreamYard session.

    Args:
        video_paths:  Ordered list of absolute paths to the source MP4s.
        output_path:  Destination MP4 path for the stitched file.

    Returns:
        (output_path, segment_offsets) where segment_offsets[i] is the
        wall-clock start time (seconds) of video_paths[i] in the output.
    """
    import json as _json

    # Probe each file to get its duration and build cumulative offsets
    offsets: list[float] = []
    t = 0.0
    for vp in video_paths:
        offsets.append(t)
        t += get_video_duration(vp)

    # Write the FFmpeg concat list to a unique temp file so concurrent stitches
    # (e.g. horizontal + vertical) never overwrite each other's list.
    fd, list_path = tempfile.mkstemp(prefix="st_stitch_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for vp in video_paths:
                # Forward slashes are fine on Windows for FFmpeg concat demuxer
                f.write(f"file '{vp.replace(chr(92), '/')}'\n")

        cmd = [
            _FFMPEG_EXE, "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            "-movflags", "+faststart",
            output_path,
        ]
        # probe_output: stream-copy concat is the classic silent-corruption
        # case, and the stitched file is reused on resume.
        _run_ffmpeg(cmd, expected_output=output_path, probe_output=True)
    finally:
        if os.path.exists(list_path):
            os.remove(list_path)
    return output_path, offsets


def get_video_duration(video_path: str) -> float:
    """Return the duration of a video file in seconds."""
    probe = ffmpeg.probe(video_path)
    return float(probe["format"]["duration"])
