"""
streamtools MCP server — exposes the podcast pipeline as tools for Claude Desktop Cowork.

Tools:
  get_video_info         → duration + file size
  compose_portrait       → stack 2-4 landscape recordings into 1080x1920
  clean_audio            → DeepFilterNet3 speech enhancement
  transcribe_audio       → Deepgram Nova-3, saves transcript JSON, returns path
  suggest_clips          → Claude Opus 4.6 clip suggestions from transcript
  export_clip_social     → burned-in karaoke captions, social MP4
  export_clip_youtube    → clean MP4 + SRT for YouTube

Data flow: tools pass file paths. transcribe_audio saves transcript JSON to cache/;
subsequent tools accept transcript_path to avoid putting large transcripts in context.
"""

import json
import os
import sys

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Ensure pipeline/ is importable
sys.path.insert(0, os.path.dirname(__file__))
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

TEMP_DIR   = os.path.join(os.path.dirname(__file__), "temp")
OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")
CACHE_DIR  = os.path.join(os.path.dirname(__file__), "cache")
for _d in (TEMP_DIR, OUTPUT_DIR, CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

mcp = FastMCP("streamtools")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _auto_output(base_name: str, suffix: str, directory: str) -> str:
    return os.path.join(directory, f"{base_name}{suffix}")


def _load_transcript(transcript_path: str) -> dict:
    with open(transcript_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _load_config_style() -> dict:
    import config as _cfg_module
    cfg = _cfg_module.load()
    return _cfg_module.active_style(cfg)


def _load_producer_context() -> str:
    import config as _cfg_module
    cfg = _cfg_module.load()
    return _cfg_module.active_context(cfg)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def get_video_info(video_path: str) -> str:
    """
    Return duration and file size for a video file.

    Args:
        video_path: Absolute path to the video file.
    """
    from pipeline.export import get_video_duration
    duration = get_video_duration(video_path)
    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    minutes, seconds = divmod(int(duration), 60)
    return (
        f"Duration: {minutes}m {seconds}s ({duration:.1f}s)\n"
        f"Size: {size_mb:.1f} MB\n"
        f"Path: {video_path}"
    )


@mcp.tool()
def compose_portrait(
    video_paths: list[str],
    output_path: str = "",
    fill: bool = True,
) -> str:
    """
    Stack 2-4 landscape Streamyard recordings vertically into a single 1080x1920 portrait video.
    Uses NVENC GPU encoding. Each local recording only carries its participant's audio — this
    mixes all tracks automatically.

    Args:
        video_paths: List of 2-4 absolute paths to landscape MP4/MOV/MKV files.
                     Order is top-to-bottom in the final portrait.
        output_path: Where to save the composed video. Auto-generated in temp/ if omitted.
        fill:        True = scale-to-fill with center crop (recommended for talking heads).
                     False = letterbox with black bars.
    """
    from pipeline.export import compose_portrait as _compose

    if not output_path:
        output_path = os.path.join(TEMP_DIR, "composed_portrait.mp4")

    _compose(video_paths, output_path, fill=fill)

    from pipeline.export import get_video_duration
    duration = get_video_duration(output_path)
    minutes, seconds = divmod(int(duration), 60)
    return f"Portrait composed: {output_path}\nDuration: {minutes}m {seconds}s"


@mcp.tool()
def clean_audio(
    video_path: str,
    output_path: str = "",
) -> str:
    """
    Enhance speech quality using DeepFilterNet3 (GPU-accelerated).
    Extracts audio at 48kHz mono, removes background noise, saves as WAV.
    Takes ~30-60 seconds for a 45-minute episode.

    Args:
        video_path:  Absolute path to the source video file.
        output_path: Where to save the cleaned WAV. Auto-generated in cache/ if omitted.
    """
    from pipeline.audio_clean import clean_audio as _clean

    base = os.path.splitext(os.path.basename(video_path))[0]
    if not output_path:
        output_path = os.path.join(CACHE_DIR, f"{base}_clean.wav")

    _clean(video_path, output_path)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    return f"Audio cleaned: {output_path}\nSize: {size_mb:.1f} MB"


@mcp.tool()
def transcribe_audio(
    audio_path: str,
    transcript_path: str = "",
) -> str:
    """
    Transcribe cleaned audio using Deepgram Nova-3 (cloud API, ~10 seconds per episode).
    Saves the full word-level transcript to a JSON file for use by suggest_clips and export tools.
    Requires DEEPGRAM_API_KEY in .env.

    Args:
        audio_path:      Absolute path to a WAV file (output of clean_audio).
        transcript_path: Where to save the transcript JSON. Auto-generated in cache/ if omitted.

    Returns:
        Path to transcript JSON, word count, and a short text preview.
    """
    from pipeline.transcribe import transcribe as _transcribe

    base = os.path.splitext(os.path.basename(audio_path))[0]
    base = base.replace("_clean", "")
    if not transcript_path:
        transcript_path = os.path.join(CACHE_DIR, f"{base}_transcript.json")

    transcript = _transcribe(audio_path)

    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(transcript, f)

    word_count = len(transcript["words"])
    preview = transcript["text"][:300].strip()
    if len(transcript["text"]) > 300:
        preview += "…"

    return (
        f"Transcript saved: {transcript_path}\n"
        f"Words: {word_count:,}\n"
        f"Preview: {preview}"
    )


@mcp.tool()
def suggest_clips(
    transcript_path: str,
    video_duration: float,
    producer_context: str = "",
    min_duration: int = 45,
    max_duration: int = 90,
) -> str:
    """
    Use Claude Opus 4.6 to analyse the transcript and suggest the best clips for social media.
    Returns a JSON list of clips with title, start_time, end_time, description, and reason.
    Requires ANTHROPIC_API_KEY in .env.

    Args:
        transcript_path:  Path to transcript JSON saved by transcribe_audio.
        video_duration:   Total video duration in seconds (from get_video_info).
        producer_context: Optional show context or episode notes to guide clip selection.
                          If omitted, the active show profile context from config.json is used.
        min_duration:     Minimum clip length in seconds. Default 45.
        max_duration:     Maximum clip length in seconds. Default 90.
    """
    from pipeline.clip_finder import find_clips as _find_clips

    transcript = _load_transcript(transcript_path)

    if not producer_context:
        producer_context = _load_producer_context()

    clips = _find_clips(
        transcript, video_duration,
        producer_context=producer_context,
        min_clip_secs=min_duration,
        max_clip_secs=max_duration,
    )

    if not clips:
        return "No clips suggested."

    lines = [f"{len(clips)} clip(s) suggested:\n"]
    for i, c in enumerate(clips, 1):
        start = c.get("start_time", 0)
        end   = c.get("end_time", 0)
        lines.append(
            f"{i}. [{start:.0f}s – {end:.0f}s] {c.get('title', '')}\n"
            f"   {c.get('description', '')}\n"
            f"   Reason: {c.get('reason', '')}"
        )
    lines.append(f"\nFull JSON:\n{json.dumps(clips, indent=2)}")
    return "\n".join(lines)


@mcp.tool()
def export_clip_social(
    video_path: str,
    clean_audio_path: str,
    transcript_path: str,
    start: float,
    end: float,
    title: str,
    description: str = "",
    output_path: str = "",
) -> str:
    """
    Export a clip with burned-in karaoke captions (ASS) and optional chyron bar for social media.
    Uses caption style from the active show profile in config.json.
    Uses libx264 CRF 18 for delivery quality.

    Args:
        video_path:        Absolute path to source video.
        clean_audio_path:  Absolute path to cleaned WAV (output of clean_audio).
        transcript_path:   Absolute path to transcript JSON (output of transcribe_audio).
        start:             Clip start time in seconds.
        end:               Clip end time in seconds.
        title:             Clip title — used for the output filename.
        description:       Chyron text shown at bottom of frame. Format: "Name | Show S#".
                           Leave empty for no chyron.
        output_path:       Where to save the MP4. Auto-generated in output/ if omitted.
    """
    from pipeline.captions import build_karaoke_ass
    from pipeline.export import export_clip as _export_clip

    transcript = _load_transcript(transcript_path)
    style = _load_config_style()

    safe_title = "".join(
        c if c.isalnum() or c in " -_" else "_"
        for c in title
    ).strip().replace(" ", "_") or "clip"

    if not output_path:
        output_path = os.path.join(OUTPUT_DIR, f"{safe_title}_social.mp4")

    ass_path = os.path.join(TEMP_DIR, f"{safe_title}.ass")

    clip_words = [
        w for w in transcript["words"]
        if start <= w["start"] <= end
    ]

    build_karaoke_ass(clip_words, style, ass_path, start_offset=start)
    _export_clip(video_path, clean_audio_path, ass_path, start, end, output_path, description)

    duration = end - start
    return (
        f"Social clip exported: {output_path}\n"
        f"Duration: {duration:.1f}s ({start:.1f}s – {end:.1f}s)\n"
        f"Words captioned: {len(clip_words)}"
    )


@mcp.tool()
def export_clip_youtube(
    video_path: str,
    clean_audio_path: str,
    transcript_path: str,
    start: float,
    end: float,
    title: str,
    output_dir: str = "",
) -> str:
    """
    Export a clean MP4 (no burned-in captions) plus an SRT file for YouTube upload.
    Uses libx264 CRF 18.

    Args:
        video_path:       Absolute path to source video.
        clean_audio_path: Absolute path to cleaned WAV (output of clean_audio).
        transcript_path:  Absolute path to transcript JSON (output of transcribe_audio).
        start:            Clip start time in seconds.
        end:              Clip end time in seconds.
        title:            Clip title — used for output filenames.
        output_dir:       Directory to save MP4 and SRT. Defaults to output/.
    """
    from pipeline.captions import build_srt
    from pipeline.export import export_clip_clean as _export_clean

    transcript = _load_transcript(transcript_path)

    safe_title = "".join(
        c if c.isalnum() or c in " -_" else "_"
        for c in title
    ).strip().replace(" ", "_") or "clip"

    out_dir = output_dir or OUTPUT_DIR
    mp4_path = os.path.join(out_dir, f"{safe_title}_youtube.mp4")
    srt_path = os.path.join(out_dir, f"{safe_title}.srt")

    clip_words = [
        w for w in transcript["words"]
        if start <= w["start"] <= end
    ]

    _export_clean(video_path, clean_audio_path, start, end, mp4_path)
    build_srt(clip_words, srt_path, start_offset=start)

    duration = end - start
    return (
        f"YouTube clip exported:\n"
        f"  MP4: {mp4_path}\n"
        f"  SRT: {srt_path}\n"
        f"Duration: {duration:.1f}s ({start:.1f}s – {end:.1f}s)"
    )


if __name__ == "__main__":
    mcp.run()
