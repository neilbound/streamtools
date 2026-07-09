"""
Clip selection via Claude API.
Sends a timestamped transcript to Claude and asks it to identify
the 3-5 most engaging self-contained segments suitable for Shorts.
Timestamps are snapped to real word boundaries after Claude responds.
"""

import json
import os
import time

import anthropic
from dotenv import load_dotenv

load_dotenv(override=True)


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


# Leading discourse-marker / filler tokens that make a Shorts opening weak.
# Trimming these so a clip OPENS on its first substantive word measurably helps
# retention (the first ~3s decide the swipe). Conservative: only consecutive
# leading filler is removed, capped, and never the whole opening sentence.
_FILLER_OPENERS = {
    "so", "yeah", "yes", "well", "okay", "ok", "um", "uh", "uhh", "hmm", "oh",
    "like", "and", "but", "anyway", "anyways", "right", "now", "look", "see",
}
_FILLER_PHRASES = (
    ("by", "the", "way"), ("you", "know"), ("i", "mean"), ("i", "think"),
    ("i", "guess"), ("i", "don't", "know"), ("kind", "of"), ("sort", "of"),
)


def _trim_leading_filler(words: list[dict], start: float, end: float,
                         max_trim: float = 4.0) -> float:
    """
    Advance `start` past leading filler so the clip opens on a substantive word.
    Removes consecutive leading filler tokens and known filler phrases, stopping at
    the first content word. Never trims more than `max_trim` seconds and always
    leaves the bulk of the clip intact. Returns the (possibly later) start time.
    """
    clip = [w for w in words if start - 0.01 <= w["start"] < end]
    if len(clip) < 6:
        return start  # too short to safely trim
    i = 0
    norm = [w["word"].lower().strip(".,!?\"' ") for w in clip]
    limit = start + max_trim
    while i < len(clip) - 4 and clip[i]["start"] <= limit:
        # try multi-word filler phrases first
        matched = False
        for ph in _FILLER_PHRASES:
            if tuple(norm[i:i + len(ph)]) == ph:
                i += len(ph)
                matched = True
                break
        if matched:
            continue
        if norm[i] in _FILLER_OPENERS:
            i += 1
            continue
        break
    return clip[i]["start"] if i > 0 else start


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

Your task: identify 3 to 5 self-contained segments that would work well as YouTube Shorts or social media clips ({min_clip_secs}–{max_clip_secs} seconds each).

THE OPENING IS THE MOST IMPORTANT THING. On Shorts the first 1–3 seconds decide whether viewers stay or swipe. Data from this channel: clips that open on a specific claim, fact, or number retain ~40%; clips that open mid-conversation on filler retain ~22%. So:
- The FIRST sentence of each clip must state a concrete claim, judgment, fact, name, or number that hooks immediately (e.g. "Andrew is 38, Libby is 22" or "This show is candy land for creeps").
- Do NOT start a clip on filler or throat-clearing: never begin with "so", "yeah", "well", "I think", "I mean", "you know", "by the way", "anyway", "like", "and", "but", or any mid-thought fragment. Move start_time to where the strong statement actually begins.
- Pick start_time at a [MM:SS] marker where a punchy, self-contained sentence starts.

Also look for:
- A clear single idea, insight, or story
- Natural ending points (end after a complete sentence, not mid-thought)
- High energy or quotable moments
- Clips must NOT overlap — each clip's start_time must be after the previous clip's end_time
- Sort clips in chronological order by start_time

Transcript:
{timestamped}

Also give each clip a "hook": a punchy on-screen hook line of AT MOST 5 words (all-caps friendly) stating the clip's single most arresting claim/fact/number — it is burned onto the opening frames to grab viewers regardless of the spoken lead-in. Examples: "HER SON IS OLDER THAN HIM", "27-YEAR AGE GAP", "HE LOVE-BOMBED HER ON DAY ONE". Make it concrete and specific, not vague ("A WILD MOMENT" is bad).

Return ONLY a JSON array (no markdown, no explanation). Times must be in seconds as floats:
[
  {{
    "title": "Short descriptive title",
    "hook": "PUNCHY <=5 WORD HOOK",
    "start_time": 12.0,
    "end_time": 67.0,
    "reason": "Why this segment works as a clip",
    "description": "Cast Name | Show S#"
  }}
]"""

    # ── Claude API call with retry (handles transient 529 / overload errors) ───────
    max_attempts = 3
    last_error: Exception | None = None
    clips = None

    for attempt in range(1, max_attempts + 1):
        try:
            message = client.messages.create(
                model="claude-opus-4-8",  # originally sonnet -> opus 4.6 -> opus 4.8
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
            break  # Success — exit retry loop

        except json.JSONDecodeError as exc:
            last_error = exc
            print(
                f"[clip_finder] Attempt {attempt}/{max_attempts}: Claude returned invalid JSON — {exc}\n"
                f"  Raw response (first 300 chars): {raw[:300] if 'raw' in dir() else '(no response)'}"
            )
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))  # 1s, 2s

        except Exception as exc:
            last_error = exc
            print(f"[clip_finder] Attempt {attempt}/{max_attempts}: API error — {type(exc).__name__}: {exc}")
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1))  # 1s, 2s

    if clips is None:
        raise RuntimeError(
            f"[clip_finder] All {max_attempts} attempts failed. Last error: {last_error}"
        )

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

        # Open on a substantive word, not "so/yeah/by the way" — first 3s drive retention
        snapped_start = _trim_leading_filler(words, snapped_start, snapped_end)

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
