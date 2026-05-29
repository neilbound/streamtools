"""
pipeline/validate.py — Automated QA checks for exported clips.

Called from run_pipeline.py after each clip export. Returns structured issue
and warning lists that are logged to pipeline_status.json and printed to the
console so problems are caught before the clip is scheduled.

Checks:
  1. Duration   — clips should be 20–62 seconds for Shorts/Reels/TikTok.
  2. File size  — catch zero-byte or suspiciously small exports.
  3. Streams    — ffprobe verifies the file has both video and audio tracks.
  4. Duration match — ffprobe actual duration must be close to expected.
  5. Coverage   — the clip must have enough transcript words.
  6. Tail silence — warn if audio ends more than 3s before the clip end.

Functions:
    validate_clip(clip_path, start, end, title, transcript_words=None) -> (issues, warnings)
    format_validation(issues, warnings, title) -> str
"""

import os


def validate_clip(
    clip_path: str,
    start: float,
    end: float,
    title: str,
    transcript_words: list[dict] | None = None,
    max_duration: float = 62.0,
    min_duration: float = 20.0,
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
        max_duration:     Clips longer than this emit an ERROR (default 62s).
        min_duration:     Clips shorter than this emit a WARNING (default 20s).

    Returns:
        (issues, warnings) — issues are blockers; warnings are advisories.
    """
    issues:   list[str] = []
    warnings: list[str] = []

    # ── 1. Duration ─────────────────────────────────────────────────────────
    duration = end - start
    if duration > max_duration:
        issues.append(
            f"DURATION: {duration:.0f}s exceeds {max_duration:.0f}s limit "
            f"(YouTube Shorts / Reels / TikTok perform best under 60s)"
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

    # ── 3. Stream check (ffprobe) ─────────────────────────────────────────────
    try:
        import ffmpeg as _ffmpeg
        probe = _ffmpeg.probe(clip_path)
        streams = probe.get("streams", [])

        has_video = any(s.get("codec_type") == "video" for s in streams)
        has_audio = any(s.get("codec_type") == "audio" for s in streams)

        if not has_video:
            issues.append("STREAMS: no video stream found in exported file")
        if not has_audio:
            issues.append("STREAMS: no audio stream found in exported file")

        # ── 4. Duration match ────────────────────────────────────────────────
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
