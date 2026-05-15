"""
Clip selection via Claude API.
Sends a timestamped transcript to Claude and asks it to identify
the 3-5 most engaging self-contained segments suitable for Shorts.
Timestamps are snapped to real word boundaries after Claude responds.
"""

import json
import os

import anthropic
from dotenv import load_dotenv

load_dotenv()


def _build_timestamped_transcript(words: list[dict]) -> str:
    """
    Build a transcript string with timestamp markers every ~15 words.
    e.g.  [00:00] And I think the most important thing here is that...
          [00:14] ...you have to be willing to take that first step.
    """
    if not words:
        return ""

    lines = []
    chunk = []
    chunk_start = words[0]["start"]

    for w in words:
        chunk.append(w["word"].strip())
        if len(chunk) >= 15:
            m = int(chunk_start // 60)
            s = int(chunk_start % 60)
            lines.append(f"[{m:02d}:{s:02d}] {' '.join(chunk)}")
            chunk = []
            chunk_start = w["end"]

    if chunk:
        m = int(chunk_start // 60)
        s = int(chunk_start % 60)
        lines.append(f"[{m:02d}:{s:02d}] {' '.join(chunk)}")

    return "\n".join(lines)


def _snap_start(words: list[dict], t: float, tolerance: float = 3.0) -> float:
    """Snap t to the start of the first word at or after t (within tolerance lookback)."""
    candidates = [w for w in words if w["start"] >= t - tolerance]
    return candidates[0]["start"] if candidates else t


def _snap_end(words: list[dict], t: float, tolerance: float = 3.0, pause: float = 0.4) -> float:
    """Snap t to just after the last word ending at or before t (within tolerance lookahead)."""
    candidates = [w for w in words if w["end"] <= t + tolerance]
    return (candidates[-1]["end"] + pause) if candidates else t


def find_clips(transcript: dict, video_duration: float, producer_context: str = "", min_clip_secs: int = 45, max_clip_secs: int = 50) -> list[dict]:
    """
    Ask Claude to identify the best clip segments from a transcript.

    Args:
        transcript: output from transcribe() — {"text": str, "words": [...]}
        video_duration: total video length in seconds

    Returns:
        List of clips with start/end snapped to real word boundaries.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set. Add it to your .env file.")

    client = anthropic.Anthropic(api_key=api_key)

    words = transcript.get("words", [])
    timestamped = _build_timestamped_transcript(words)
    duration_min = int(video_duration // 60)
    duration_sec = int(video_duration % 60)

    system = producer_context.strip() if producer_context.strip() else (
        "You are a video editor helping identify the best short clips from a longer video."
    )

    prompt = f"""The video is {duration_min}m {duration_sec}s long. Below is the transcript with timestamps in [MM:SS] format every ~15 words. Use these timestamps as anchors when setting start_time and end_time — pick values that correspond to real timestamp markers so clips begin and end at natural sentence boundaries.

Your task: identify 3 to 5 self-contained segments that would work well as YouTube Shorts or social media clips ({min_clip_secs}–{max_clip_secs} seconds each). Look for:
- Strong, punchy openings (no mid-sentence starts — begin right at a [MM:SS] marker or just after)
- A clear single idea, insight, or story
- Natural ending points (end after a complete sentence, not mid-thought)
- High energy or quotable moments
- Clips must NOT overlap — each clip's start_time must be after the previous clip's end_time
- Sort clips in chronological order by start_time

Transcript:
{timestamped}

Return ONLY a JSON array (no markdown, no explanation). Times must be in seconds as floats:
[
  {{
    "title": "Short descriptive title",
    "start_time": 12.0,
    "end_time": 67.0,
    "reason": "Why this segment works as a clip",
    "description": "Cast Name | Show S#"
  }}
]"""

    message = client.messages.create(
        model="claude-opus-4-6",  # upgraded from sonnet on test/model-upgrades branch
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    clips = json.loads(raw)

    # Sort chronologically before de-overlapping
    clips.sort(key=lambda c: float(c["start_time"]))

    prev_end = 0.0
    keep = []
    for clip in clips:
        raw_start = max(prev_end, float(clip["start_time"]))
        raw_end   = min(video_duration, float(clip["end_time"]))

        # Snap start/end to real word boundaries so clips don't cut mid-sentence
        snapped_start = _snap_start(words, raw_start)
        snapped_end   = _snap_end(words, raw_end)

        # Enforce max duration after snapping — snap can overshoot when Claude
        # already returned a clip near the max and snap extends to the next sentence.
        # Trim back to a word boundary at max_clip_secs from the snapped start.
        duration = snapped_end - snapped_start
        if duration > max_clip_secs:
            trim_target = snapped_start + max_clip_secs
            snapped_end = _snap_end(words, trim_target)
            # Safety: if trim overshot again, hard-cap it
            if snapped_end - snapped_start > max_clip_secs + 5:
                snapped_end = trim_target

        # Enforce min duration — skip clips that are too short after snapping
        if snapped_end - snapped_start < min_clip_secs:
            continue

        # De-overlap and clamp to video length
        snapped_start = max(prev_end, snapped_start)
        snapped_end   = min(video_duration, snapped_end)

        # Drop clip if it collapsed to nothing after de-overlap
        if snapped_end <= snapped_start:
            continue

        # Return new dicts so callers always get the snapped values, not
        # references to the original parsed JSON which may be mutated elsewhere
        keep.append({
            **clip,
            "start_time": snapped_start,
            "end_time":   snapped_end,
        })
        prev_end = snapped_end

    return keep
