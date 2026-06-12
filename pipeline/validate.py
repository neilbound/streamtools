"""
pipeline/validate.py — Automated QA checks for exported media.

Called from run_pipeline.py after each clip export, from the MCP scheduling
gate, and from the publisher daemon pre-flight. Returns structured issue and
warning lists; issues block scheduling (unless forced), warnings are advisory.

Checks:
  1. Duration   — clips should be 20–62 seconds for Shorts/Reels/TikTok.
  2. File size  — catch zero-byte or suspiciously small exports.
  3. Streams    — ffprobe verifies the file has both video and audio tracks.
  4. Format     — codec (h264/aac) and resolution match the QA profile.
  5. Duration match — ffprobe actual duration must be close to expected.
  6. Signal     — black-frame runs and silent/very-quiet audio (one ffmpeg pass).
  7. Coverage   — the clip must have enough transcript words.
  8. Tail silence — warn if audio ends more than 4s before the clip end.

Functions:
    validate_clip(clip_path, start, end, title, transcript_words=None, deep=True)
    validate_media(path, profile="clip", expected_duration=None, deep=True)
    quick_probe_check(path, expect_orientation="") -> str | None
    valid_intermediate(path, kind) -> bool
    format_validation(issues, warnings, title) -> str

CLI:
    python -m pipeline.validate <file-or-dir> [--profile clip|episode] [--quick]
"""

import json
import os
import re
import subprocess
import wave

# Importing export also injects the hardcoded FFmpeg dir into PATH, which
# ffmpeg-python's probe needs when this module runs standalone (CLI / daemon).
from pipeline.export import _FFMPEG_EXE


# ── QA profiles & thresholds ────────────────────────────────────────────────────

QA_PROFILES = {
    "clip": {
        # Orientation reference + minimum acceptable size (StreamYard MARS
        # vertical exports are 720x1280 — that's normal, not a defect).
        "width": 1080, "height": 1920,
        "min_width": 720, "min_height": 1280,
        "vcodec": "h264", "acodec": "aac",
        "min_duration": 20.0,
        "max_duration": 180.0,        # hard platform cap (Shorts/Reels) — ISSUE above
        "target_max_duration": 62.0,  # performance heuristic — WARNING above
        "signal_checks": True,
    },
    "episode": {
        "width": 1920, "height": 1080,
        "min_width": 1280, "min_height": 720,
        "vcodec": "h264", "acodec": "aac",
        "min_duration": 60.0,
        "max_duration": None,
        "target_max_duration": None,
        "signal_checks": False,   # decoding an 80-min episode is too slow; probe only
    },
}

BLACK_MIN_DUR   = 0.5     # blackdetect d= (shortest run the filter reports)
BLACK_ISSUE_DUR = 1.0     # black run at least this long becomes an ISSUE
BLACK_PIX_TH    = 0.10    # blackdetect pix_th=
SILENT_MEAN_DB  = -50.0   # mean_volume below this => ISSUE (effectively silent)
QUIET_MAX_DB    = -20.0   # max_volume below this => WARNING (very quiet)


# ── Probe & format checks ───────────────────────────────────────────────────────

def probe_media(path: str) -> dict:
    """ffprobe wrapper — isolated so tests can mock it."""
    import ffmpeg as _ffmpeg
    return _ffmpeg.probe(path)


def check_format(probe: dict, profile: dict) -> tuple[list[str], list[str]]:
    """
    Pure checks against a probe dict: streams present, codecs, resolution,
    container duration within profile bounds.
    """
    issues:   list[str] = []
    warnings: list[str] = []

    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if video is None:
        issues.append("STREAMS: no video stream found")
    if audio is None:
        issues.append("STREAMS: no audio stream found")

    if video is not None:
        vcodec = video.get("codec_name", "")
        if profile.get("vcodec") and vcodec != profile["vcodec"]:
            issues.append(
                f"CODEC: video is '{vcodec}', expected '{profile['vcodec']}'"
            )
        w, h = video.get("width"), video.get("height")
        ew, eh = profile.get("width"), profile.get("height")
        if ew and eh and w and h:
            # Wrong orientation (e.g. landscape clip destined for Shorts) is
            # unpublishable; same orientation at a smaller-than-floor size is
            # a quality concern worth a warning.
            if (w > h) != (ew > eh):
                issues.append(
                    f"ASPECT: {w}x{h} is the wrong orientation — expected {ew}x{eh}"
                )
            elif (w < profile.get("min_width", 0)
                  or h < profile.get("min_height", 0)):
                warnings.append(
                    f"RESOLUTION: {w}x{h} is below the "
                    f"{profile.get('min_width')}x{profile.get('min_height')} quality floor"
                )

    if audio is not None:
        acodec = audio.get("codec_name", "")
        if profile.get("acodec") and acodec != profile["acodec"]:
            warnings.append(
                f"CODEC: audio is '{acodec}', expected '{profile['acodec']}'"
            )

    dur = float(probe.get("format", {}).get("duration", 0) or 0)
    if dur <= 0:
        issues.append("DURATION: container reports zero/unknown duration")
    else:
        max_d    = profile.get("max_duration")
        target_d = profile.get("target_max_duration")
        min_d    = profile.get("min_duration")
        if max_d and dur > max_d:
            issues.append(f"DURATION: {dur:.0f}s exceeds the {max_d:.0f}s platform limit")
        elif target_d and dur > target_d:
            warnings.append(
                f"DURATION: {dur:.0f}s is over the {target_d:.0f}s short-form "
                f"sweet spot — fine to post, may underperform"
            )
        elif min_d and dur < min_d:
            warnings.append(f"DURATION: {dur:.0f}s is shorter than expected ({min_d:.0f}s minimum)")

    return issues, warnings


# ── Signal checks (one ffmpeg decode pass) ──────────────────────────────────────

def parse_blackdetect(stderr: str) -> list[tuple[float, float]]:
    """Parse blackdetect filter output into (start, end) tuples."""
    runs = []
    for m in re.finditer(r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)", stderr):
        runs.append((float(m.group(1)), float(m.group(2))))
    return runs


def parse_volumedetect(stderr: str) -> dict:
    """Parse volumedetect filter output into {'mean_volume': dB, 'max_volume': dB}."""
    out = {}
    m = re.search(r"mean_volume:\s*(-?[\d.]+)\s*dB", stderr)
    if m:
        out["mean_volume"] = float(m.group(1))
    m = re.search(r"max_volume:\s*(-?[\d.]+)\s*dB", stderr)
    if m:
        out["max_volume"] = float(m.group(1))
    return out


def parse_decoded_duration(stderr: str) -> float | None:
    """
    Parse the last progress 'time=HH:MM:SS.cc' from an ffmpeg decode pass —
    how much media was actually decodable. A truncated file (with an intact
    +faststart moov header claiming the full duration) decodes short and
    exits 0, so this is the only reliable truncation signal.
    """
    last = None
    for m in re.finditer(r"time=(\d+):(\d+):([\d.]+)", stderr):
        last = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return last


def run_signal_checks(path: str, container_duration: float | None = None) -> tuple[list[str], list[str]]:
    """
    Decode the file once through blackdetect + volumedetect and flag
    black-frame runs, silent/very-quiet audio, and truncation (decoded
    duration falling short of the container's claim). ~1-3s for a <62s clip.
    """
    issues:   list[str] = []
    warnings: list[str] = []

    cmd = [
        _FFMPEG_EXE, "-hide_banner", "-i", path,
        "-vf", f"blackdetect=d={BLACK_MIN_DUR}:pix_th={BLACK_PIX_TH}",
        "-af", "volumedetect",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True)
    stderr = result.stderr.decode(errors="replace")
    if result.returncode != 0:
        warnings.append(f"SIGNAL: ffmpeg decode failed during QA ({stderr[-300:]})")
        return issues, warnings

    if container_duration:
        decoded = parse_decoded_duration(stderr)
        if decoded is not None and decoded < container_duration - 2.0:
            issues.append(
                f"TRUNCATED: only {decoded:.1f}s of {container_duration:.1f}s "
                f"is decodable — file is cut short"
            )

    for black_start, black_end in parse_blackdetect(stderr):
        run = black_end - black_start
        if run >= BLACK_ISSUE_DUR:
            issues.append(
                f"BLACK: {run:.1f}s of black frames at {black_start:.1f}-{black_end:.1f}s"
            )

    vol = parse_volumedetect(stderr)
    mean = vol.get("mean_volume")
    peak = vol.get("max_volume")
    if mean is not None and mean < SILENT_MEAN_DB:
        issues.append(f"AUDIO: effectively silent (mean {mean:.1f} dB)")
    elif peak is not None and peak < QUIET_MAX_DB:
        warnings.append(f"AUDIO: very quiet (peak {peak:.1f} dB) — check levels")

    return issues, warnings


# ── Combined media validation ───────────────────────────────────────────────────

def validate_media(
    path: str,
    profile: str = "clip",
    expected_duration: float | None = None,
    deep: bool = True,
) -> tuple[list[str], list[str]]:
    """
    Validate a media file against a QA profile: existence/size, format/codec/
    resolution, duration bounds, and (deep, clip profile only) signal checks.
    """
    prof = QA_PROFILES[profile]
    issues:   list[str] = []
    warnings: list[str] = []

    if not os.path.exists(path):
        return [f"FILE: not found at {path}"], warnings
    size_mb = os.path.getsize(path) / (1024 * 1024)
    if size_mb < 0.1:
        issues.append(f"FILE: suspiciously small ({size_mb:.2f} MB) — may be corrupt or empty")
        return issues, warnings

    try:
        probe = probe_media(path)
    except Exception as exc:
        issues.append(f"PROBE: unreadable as media ({exc})")
        return issues, warnings

    fi, fw = check_format(probe, prof)
    issues.extend(fi)
    warnings.extend(fw)

    if expected_duration is not None:
        actual = float(probe.get("format", {}).get("duration", 0) or 0)
        if actual > 0 and abs(actual - expected_duration) > 4:
            warnings.append(
                f"DURATION MISMATCH: expected {expected_duration:.1f}s "
                f"but encoded file is {actual:.1f}s — check for audio/video desync"
            )

    if deep and prof["signal_checks"] and not issues:
        container_dur = float(probe.get("format", {}).get("duration", 0) or 0)
        si, sw = run_signal_checks(path, container_duration=container_dur or None)
        issues.extend(si)
        warnings.extend(sw)

    return issues, warnings


def quick_probe_check(path: str, expect_orientation: str = "") -> str | None:
    """
    Cheap single-ffprobe sanity check for daemon pre-flight and resume
    validation. Returns an error string, or None if the file looks sane.
    """
    if not os.path.exists(path):
        return f"file not found: {path}"
    if os.path.getsize(path) < 0.1 * 1024 * 1024:
        return f"file suspiciously small ({os.path.getsize(path)} bytes): {path}"
    try:
        probe = probe_media(path)
    except Exception as exc:
        return f"unreadable as media ({exc}): {path}"

    streams = probe.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        return f"no video stream: {path}"
    if not any(s.get("codec_type") == "audio" for s in streams):
        return f"no audio stream: {path}"
    if float(probe.get("format", {}).get("duration", 0) or 0) <= 0:
        return f"zero/unknown duration: {path}"

    if expect_orientation in ("portrait", "landscape"):
        w, h = video.get("width", 0), video.get("height", 0)
        if w and h:
            actual = "portrait" if h > w else "landscape"
            if actual != expect_orientation:
                return f"wrong orientation ({w}x{h} is {actual}, expected {expect_orientation}): {path}"
    return None


def valid_intermediate(path: str, kind: str) -> bool:
    """
    Cheap integrity check for resume-time reuse of intermediate files.
    kind: "video" (probe), "wav" (header + frames), "transcript" (json shape).
    """
    if not os.path.exists(path):
        return False
    try:
        if kind == "video":
            return quick_probe_check(path) is None
        if kind == "wav":
            if os.path.getsize(path) < 1024 * 1024:
                return False
            with wave.open(path, "rb") as w:
                return w.getnframes() > 0
        if kind == "transcript":
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            words = data.get("words")
            if not isinstance(data.get("text"), str) or not words:
                return False
            first = words[0]
            return all(k in first for k in ("word", "start", "end"))
    except Exception:
        return False
    raise ValueError(f"Unknown intermediate kind: {kind}")


def validate_clip(
    clip_path: str,
    start: float,
    end: float,
    title: str,
    transcript_words: list[dict] | None = None,
    max_duration: float = 62.0,
    min_duration: float = 20.0,
    deep: bool = True,
) -> tuple[list[str], list[str]]:
    """
    Run automated QA checks on an exported clip file.

    Args:
        clip_path:        Absolute path to the exported MP4.
        start:            Clip start time in seconds (from the source video).
        end:              Clip end time in seconds (after sentence-snap).
        title:            Clip title (for readable error messages).
        transcript_words: Optional list of word dicts from transcript.json.
                          If provided, enables transcript coverage checks.
        max_duration:     Clips longer than this emit a WARNING (default 62s) —
                          a performance heuristic, not a platform limit. The hard
                          ERROR cap is QA_PROFILES["clip"]["max_duration"] (180s).
        min_duration:     Clips shorter than this emit a WARNING (default 20s).

    Returns:
        (issues, warnings) — issues are blockers; warnings are advisories.
    """
    issues:   list[str] = []
    warnings: list[str] = []

    # ── 1. Duration ─────────────────────────────────────────────────────────
    duration  = end - start
    platform_cap = QA_PROFILES["clip"]["max_duration"]
    if platform_cap and duration > platform_cap:
        issues.append(
            f"DURATION: {duration:.0f}s exceeds the {platform_cap:.0f}s platform limit"
        )
    elif duration > max_duration:
        warnings.append(
            f"DURATION: {duration:.0f}s is over the {max_duration:.0f}s short-form "
            f"sweet spot — fine to post, may underperform"
        )
    elif duration < min_duration:
        warnings.append(
            f"DURATION: {duration:.0f}s is very short — clips under 20s "
            f"rarely perform well on short-form platforms"
        )

    # ── 2. File exists & size ────────────────────────────────────────────────
    if not os.path.exists(clip_path):
        issues.append(f"FILE: output file not found at {clip_path}")
        return issues, warnings   # No point probing a missing file

    size_mb = os.path.getsize(clip_path) / (1024 * 1024)
    if size_mb < 0.1:
        issues.append(
            f"FILE: suspiciously small ({size_mb:.2f} MB) — "
            f"file may be corrupt or empty"
        )

    # ── 3 & 4. Format + duration match (ffprobe) ──────────────────────────────
    actual_dur = 0.0
    try:
        probe = probe_media(clip_path)

        # Clip duration was already checked from start/end above — strip the
        # profile's duration bounds so check_format doesn't double-report.
        prof = dict(QA_PROFILES["clip"],
                    min_duration=None, max_duration=None, target_max_duration=None)
        fi, fw = check_format(probe, prof)
        issues.extend(fi)
        warnings.extend(fw)

        actual_dur = float(probe["format"].get("duration", 0))
        if actual_dur > 0 and abs(actual_dur - duration) > 4:
            warnings.append(
                f"DURATION MISMATCH: transcript suggests {duration:.1f}s "
                f"but encoded file is {actual_dur:.1f}s — "
                f"check for audio/video desync"
            )

    except ImportError:
        warnings.append("PROBE: ffmpeg-python not installed — stream check skipped")
    except Exception as exc:
        warnings.append(f"PROBE: could not verify streams ({exc})")

    # ── Signal checks: black frames, audio levels, truncation (one decode) ────
    if deep and not issues:
        si, sw = run_signal_checks(clip_path, container_duration=actual_dur or None)
        issues.extend(si)
        warnings.extend(sw)

    # ── 5 & 6. Transcript coverage ────────────────────────────────────────────
    if transcript_words:
        words_in_clip = [
            w for w in transcript_words
            if start <= w.get("start", 0) <= end
        ]

        if len(words_in_clip) < 10:
            issues.append(
                f"TRANSCRIPT: only {len(words_in_clip)} words found in clip window "
                f"— clip may be mis-timed or fall on a silent section"
            )

        if words_in_clip:
            last_word_end = words_in_clip[-1].get("end", end)
            tail_silence  = end - last_word_end
            if tail_silence > 4.0:
                warnings.append(
                    f"SILENCE: {tail_silence:.1f}s of silence at end of clip "
                    f"— consider trimming the end point"
                )

    return issues, warnings


def format_validation(issues: list[str], warnings: list[str], title: str) -> str:
    """Format validation results as a readable string for console / status log."""
    if not issues and not warnings:
        return f"  QA: PASS — {title}"

    lines = [f"  QA: {'FAIL' if issues else 'WARN'} — {title}"]
    for issue in issues:
        lines.append(f"    [ERROR]   {issue}")
    for warning in warnings:
        lines.append(f"    [WARNING] {warning}")
    return "\n".join(lines)


# ── CLI: manual QA over a file or a clips directory ─────────────────────────────

if __name__ == "__main__":
    import argparse
    import glob as _glob

    parser = argparse.ArgumentParser(description="Run QA checks on exported media.")
    parser.add_argument("target", help="A media file or a directory of clips")
    parser.add_argument("--profile", choices=list(QA_PROFILES), default="clip")
    parser.add_argument("--quick", action="store_true",
                        help="Probe-only (skip the signal-check decode pass)")
    args = parser.parse_args()

    if os.path.isdir(args.target):
        files = sorted(
            _glob.glob(os.path.join(args.target, "*_social.mp4"))
            + _glob.glob(os.path.join(args.target, "*_youtube.mp4"))
        )
        if not files:
            files = sorted(_glob.glob(os.path.join(args.target, "*.mp4")))
    else:
        files = [args.target]

    if not files:
        print(f"No media files found under {args.target}")
        raise SystemExit(1)

    passed = warned = failed = 0
    for f in files:
        issues, warnings = validate_media(
            f, profile=args.profile, deep=not args.quick
        )
        print(format_validation(issues, warnings, os.path.basename(f)))
        if issues:
            failed += 1
        elif warnings:
            warned += 1
        else:
            passed += 1

    print(f"\n{len(files)} file(s): {passed} pass, {warned} warn, {failed} fail")
    raise SystemExit(1 if failed else 0)
