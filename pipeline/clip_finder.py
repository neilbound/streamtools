"""
Clip selection via Claude API.
Sends the full transcript to Claude and asks it to identify
the 3-5 most engaging self-contained segments suitable for Shorts.
"""

import json
import os

import anthropic
from dotenv import load_dotenv

load_dotenv()


def find_clips(transcript: dict, video_duration: float, producer_context: str = "") -> list[dict]:
    """
    Ask Claude to identify the best clip segments from a transcript.

    Args:
        transcript: output from transcribe() — {"text": str, "words": [...]}
        video_duration: total video length in seconds

    Returns:
        List of clips:
        [
            {
                "title": str,       # short descriptive title
                "start_time": float,  # seconds
                "end_time": float,
                "reason": str,      # why this segment is worth clipping
            },
            ...
        ]
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY not set. Add it to your .env file.")

    client = anthropic.Anthropic(api_key=api_key)

    duration_min = int(video_duration // 60)
    duration_sec = int(video_duration % 60)

    system = producer_context.strip() if producer_context.strip() else (
        "You are a video editor helping identify the best short clips from a longer video."
    )

    prompt = f"""The video is {duration_min}m {duration_sec}s long. Below is the full transcript.

Your task: identify 3 to 5 self-contained segments that would work well as YouTube Shorts or social media clips (45–90 seconds each). Look for:
- Strong, punchy openings (no mid-sentence starts)
- A clear single idea, insight, or story
- Natural ending points (not cut off mid-thought)
- High energy or quotable moments

Transcript:
{transcript["text"]}

Return ONLY a JSON array (no markdown, no explanation):
[
  {{
    "title": "Short descriptive title",
    "start_time": 12.5,
    "end_time": 67.0,
    "reason": "Why this segment works as a clip",
    "description": "2-3 line social caption burned into the video (use \\n for line breaks)"
  }}
]"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()

    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()

    clips = json.loads(raw)

    # Clamp timestamps to video bounds
    for clip in clips:
        clip["start_time"] = max(0.0, float(clip["start_time"]))
        clip["end_time"] = min(video_duration, float(clip["end_time"]))

    return clips
