"""
streamtools MCP server — exposes the podcast pipeline as tools for Claude Desktop Cowork.

Tools:
  get_video_info           → duration + file size
  compose_portrait         → stack 2-4 landscape recordings into 1080x1920
  clean_audio              → DeepFilterNet3 speech enhancement
  transcribe_audio         → Deepgram Nova-3, saves transcript JSON, returns path
  suggest_clips            → Claude Opus 4.6 clip suggestions from transcript
  export_clip_social       → burned-in karaoke captions, social MP4
  export_clip_youtube      → clean MP4 + SRT for YouTube
  run_full_pipeline        → run complete pipeline in background, returns immediately
  check_pipeline_status    → read progress/results from a running or completed pipeline

Data flow: tools pass file paths. transcribe_audio saves transcript JSON to cache/;
subsequent tools accept transcript_path to avoid putting large transcripts in context.

Long-running tools (compose, clean, transcribe, export) exceed Claude Desktop's request
timeout. Use run_full_pipeline to launch the full pipeline as a background process —
it returns immediately with the status file path. Poll with check_pipeline_status.
"""

import json
import os
import subprocess
import sys

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

# Ensure pipeline/ is importable
sys.path.insert(0, os.path.dirname(__file__))
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)

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
    max_duration: int = 50,
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
        max_duration:     Maximum clip length in seconds. Default 50.
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


@mcp.tool()
def run_full_pipeline(
    episode_id: str,
    source_paths: list[str],
    show_name: str = "",
    min_clip: int = 45,
    max_clip: int = 50,
    export_format: str = "both",
    fill: bool = True,
    clips_only: bool = False,
    export_only: bool = False,
    filter_audio: bool = True,
) -> str:
    """
    Run the complete podcast pipeline in the background and return immediately.
    Poll progress with check_pipeline_status using the returned status_path.

    Pipeline steps: compose portrait (if multiple sources) → clean audio →
    transcribe → profanity filter → suggest clips → export social + YouTube clips.

    All outputs are saved to:
      output/{show_slug}_{episode_id}_{YYYY-MM-DD}/

    Args:
        episode_id:     Short label for this episode, e.g. "s11e01" or "men_accountability".
        source_paths:   List of source video paths. Provide 2-4 paths to compose a portrait,
                        or a single path to use as-is.
        show_name:      Show name for directory naming. Defaults to active profile.
        min_clip:       Minimum clip duration in seconds. Default 45.
        max_clip:       Maximum clip duration in seconds. Default 50.
        export_format:  "social", "youtube", or "both". Default "both".
        fill:           True = scale-to-fill crop (default). False = letterbox.
        clips_only:     Skip compose/clean/transcribe — re-run suggest + export only.
        export_only:    Skip everything except export — uses clips from existing status JSON.
        filter_audio:   Replace profanity with beep tone in audio and captions. Default True.
    """
    python_exe = os.path.join(os.path.dirname(__file__), ".venv312", "Scripts", "python.exe")
    script     = os.path.join(os.path.dirname(__file__), "run_pipeline.py")

    cmd = [python_exe, script, "--episode", episode_id]
    if len(source_paths) > 1:
        cmd += ["--sources"] + source_paths
    else:
        cmd += ["--source", source_paths[0]]
    if show_name:
        cmd += ["--show", show_name]
    cmd += ["--min-clip", str(min_clip), "--max-clip", str(max_clip)]
    cmd += ["--format", export_format]
    if not fill:
        cmd.append("--no-fill")
    if clips_only:
        cmd.append("--clips-only")
    if export_only:
        cmd.append("--export-only")
    if not filter_audio:
        cmd.append("--no-filter")

    # Launch as detached background process — does not block MCP request
    subprocess.Popen(
        cmd,
        cwd=os.path.dirname(__file__),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    # Determine expected status file path so caller can poll it
    from pipeline.episode import episode_dir, slugify
    from datetime import date
    import config as _cfg
    cfg = _cfg.load()
    sn  = show_name or cfg.get("active_profile", "podcast")
    ep  = episode_dir(sn, episode_id)
    status_path = os.path.join(ep, "pipeline_status.json")

    return (
        f"Pipeline started in background.\n"
        f"Episode : {episode_id}\n"
        f"Output  : {ep}\n"
        f"Status  : {status_path}\n\n"
        f"Use check_pipeline_status to monitor progress."
    )


@mcp.tool()
def check_pipeline_status(status_path: str) -> str:
    """
    Read the current status of a running or completed pipeline.
    Returns a human-readable summary of completed steps and any errors.

    Args:
        status_path: Path to pipeline_status.json returned by run_full_pipeline.
    """
    from pipeline.episode import read_status

    if not os.path.exists(status_path):
        return f"Status file not found yet — pipeline may still be starting up.\nExpected: {status_path}"

    status = read_status(status_path)
    state  = status.get("state", "unknown")
    lines  = [f"State: {state.upper()}",
              f"Episode: {status.get('episode_id', '?')}",
              f"Show: {status.get('show', '?')}",
              f"Started: {status.get('started', '?')}"]

    steps = ["compose", "clean", "transcribe", "suggest"]
    for step in steps:
        if step in status:
            lines.append(f"  ✓ {step}: {status[step].get('message', '')}")

    # Count exports
    exported = [k for k in status if k.startswith("export_")]
    if exported:
        lines.append(f"  ✓ exports: {len(exported)} clip(s) done")

    if state == "complete":
        clips = status.get("exported_clips", [])
        lines.append(f"\nComplete — {len(clips)} clip(s) exported:")
        for c in clips:
            lines.append(f"  • {c['title']}")
            for fmt in ("social", "youtube", "srt"):
                if fmt in c:
                    lines.append(f"      {fmt}: {c[fmt]}")
        lines.append(f"Finished: {status.get('finished', '?')}")

    elif state == "error":
        lines.append(f"\nERROR: {status.get('error', 'unknown error')}")

    return "\n".join(lines)


# ── Publishing tools ───────────────────────────────────────────────────────────

@mcp.tool()
def schedule_clip(
    clip_path: str,
    platforms: list[str],
    title: str,
    description: str = "",
    scheduled_time: str = "",
    tags: list[str] = [],
) -> str:
    """
    Add a clip to the publish queue for scheduled delivery to social platforms.

    The publisher daemon (publisher_daemon.py) runs every 15 minutes via Windows Task
    Scheduler and will upload the clip when the scheduled_time is reached.

    Args:
        clip_path:      Absolute path to the exported MP4 clip.
        platforms:      List of target platforms: "youtube", "tiktok", "instagram".
        title:          Post title / caption.
        description:    Longer description (used by YouTube; optional for others).
        scheduled_time: ISO 8601 UTC datetime string, e.g. "2026-05-16T15:00:00+00:00".
                        If empty, schedules for now (uploaded at next daemon run).
        tags:           Optional hashtag strings (without '#').

    Returns:
        Confirmation message with the assigned post_id.
    """
    from pipeline.publish_queue import enqueue
    from datetime import datetime, timezone

    if not scheduled_time:
        scheduled_time = datetime.now(tz=timezone.utc).isoformat()

    post_id = enqueue(
        clip_path=clip_path,
        platforms=platforms,
        title=title,
        description=description,
        scheduled_time_iso=scheduled_time,
        tags=tags or [],
    )

    return (
        f"Queued for publishing.\n"
        f"post_id   : {post_id}\n"
        f"Platforms : {', '.join(platforms)}\n"
        f"Scheduled : {scheduled_time}\n"
        f"Title     : {title}\n\n"
        f"The daemon will upload this at or after the scheduled time.\n"
        f"Use list_scheduled_clips to check status or cancel_scheduled_clip to remove it."
    )


@mcp.tool()
def publish_clip_now(
    clip_path: str,
    platforms: list[str],
    title: str,
    description: str = "",
    tags: list[str] = [],
) -> str:
    """
    Upload and publish a clip immediately to one or more social platforms.

    This runs the upload functions directly (not via the queue) and may take
    30-120 seconds depending on file size and platform. Requires PUBLISHING_ENABLED=true
    and valid credentials in .env.

    Args:
        clip_path:   Absolute path to the exported MP4 clip.
        platforms:   List of target platforms: "youtube", "tiktok", "instagram".
        title:       Post title / caption.
        description: Longer description (used by YouTube; optional for others).
        tags:        Optional hashtag strings (without '#').

    Returns:
        Per-platform results (video IDs, URLs, publish IDs).
    """
    from pipeline.publish import upload_youtube, upload_tiktok, upload_instagram
    import traceback as _tb

    uploaders = {
        "youtube":   lambda: upload_youtube(clip_path, title, description, tags or []),
        "tiktok":    lambda: upload_tiktok(clip_path, title, tags or []),
        "instagram": lambda: upload_instagram(clip_path, title),
    }

    lines = [f"Publishing '{title}' to {len(platforms)} platform(s)...\n"]
    for platform in platforms:
        if platform not in uploaders:
            lines.append(f"[{platform}] Unknown platform — skipped.")
            continue
        try:
            result = uploaders[platform]()
            lines.append(f"[{platform}] OK — {result}")
        except Exception as exc:
            lines.append(f"[{platform}] FAILED — {type(exc).__name__}: {exc}")

    return "\n".join(lines)


@mcp.tool()
def list_scheduled_clips() -> str:
    """
    List all entries in the publish queue (pending, partial, complete, failed, cancelled).

    Returns a human-readable summary of every queued post sorted by scheduled_time.
    """
    from pipeline.publish_queue import list_all

    entries = list_all()

    if not entries:
        return "Publish queue is empty."

    lines = [f"Publish queue — {len(entries)} entry(ies):\n"]
    for e in entries:
        status    = e.get("status", "?")
        post_id   = e.get("post_id", "?")
        title     = e.get("title", "(no title)")
        platforms = ", ".join(e.get("platforms", []))
        sched     = e.get("scheduled_time", "?")
        clip      = e.get("clip_path", "?")

        lines.append(
            f"  [{status.upper()}] {post_id} — {title}\n"
            f"    Platforms : {platforms}\n"
            f"    Scheduled : {sched}\n"
            f"    Clip      : {clip}"
        )

        results = e.get("results", {})
        if results:
            for platform, res in results.items():
                if res.get("status") == "ok":
                    url = res.get("url") or res.get("publish_id") or res.get("media_id", "")
                    lines.append(f"    {platform}: OK ({url})")
                else:
                    lines.append(f"    {platform}: ERROR — {res.get('error', '?')}")
        lines.append("")

    return "\n".join(lines).rstrip()


@mcp.tool()
def cancel_scheduled_clip(post_id: str) -> str:
    """
    Cancel a pending scheduled post by its post_id.

    Only posts with 'pending' status can be cancelled. Posts that are already
    uploading, complete, or failed cannot be cancelled.

    Args:
        post_id: The 8-character post identifier returned by schedule_clip.

    Returns:
        Confirmation or error message.
    """
    from pipeline.publish_queue import cancel

    success = cancel(post_id)

    if success:
        return f"Post {post_id} has been cancelled."
    else:
        return (
            f"Could not cancel post {post_id}. "
            "Either the post_id was not found, or the post is not in 'pending' status. "
            "Use list_scheduled_clips to check the current status."
        )


# ── Description generation tools ──────────────────────────────────────────────

@mcp.tool()
def generate_episode_descriptions(
    transcript_path: str,
    episode_title: str,
    episode_notes: str = "",
    hosts: str = "Neil and Shelly",
) -> str:
    """
    Generate a full episode description package using Claude — hook title trio,
    deep dive, must-hear moments, and platform signature block with all links.
    Follows the show's established template. Requires ANTHROPIC_API_KEY.

    Args:
        transcript_path: Path to transcript JSON (output of transcribe_audio or pipeline).
        episode_title:   Episode title or topic, e.g. "Men Taking Accountability Part 2".
        episode_notes:   Optional producer notes — themes, key moments, or context to guide Claude.
        hosts:           Host names for context. Default "Neil and Shelly".

    Returns:
        Full description package ready to copy into YouTube, plus extracted title options.
    """
    import config as _cfg
    from pipeline.describe import generate_episode_descriptions as _gen

    cfg   = _cfg.load()
    brand = _cfg.active_brand(cfg)

    with open(transcript_path, "r", encoding="utf-8") as f:
        import json as _json
        transcript = _json.load(f)

    result = _gen(
        transcript=transcript,
        episode_title=episode_title,
        episode_notes=episode_notes,
        brand=brand,
        hosts=hosts,
    )

    lines = ["=== EPISODE DESCRIPTION PACKAGE ===\n"]

    if result["title_options"]:
        lines.append("TITLE OPTIONS:")
        for i, t in enumerate(result["title_options"], 1):
            lines.append(f"  {i}. {t}")
        lines.append("")

    lines.append("FULL DESCRIPTION:")
    lines.append(result["youtube_full"])

    return "\n".join(lines)


@mcp.tool()
def generate_clip_descriptions(
    transcript_path: str,
    clip_title: str,
    clip_start: float,
    clip_end: float,
    episode_context: str = "",
) -> str:
    """
    Generate platform-specific short-form descriptions for a clip:
    YouTube Shorts description, TikTok caption, and Instagram Reels caption.
    Uses the show's brand links and handles from the active profile.

    Args:
        transcript_path: Path to full episode transcript JSON.
        clip_title:      Title of the clip (from suggest_clips or run_full_pipeline output).
        clip_start:      Clip start time in seconds.
        clip_end:        Clip end time in seconds.
        episode_context: Brief context e.g. "Is Love Blind S11 manosphere episode".
    """
    import json as _json
    import config as _cfg
    from pipeline.describe import generate_clip_descriptions as _gen

    cfg   = _cfg.load()
    brand = _cfg.active_brand(cfg)

    with open(transcript_path, "r", encoding="utf-8") as f:
        transcript = _json.load(f)

    clip_words = [
        w for w in transcript["words"]
        if clip_start <= w["start"] <= clip_end
    ]

    result = _gen(
        clip_words=clip_words,
        clip_title=clip_title,
        episode_context=episode_context,
        brand=brand,
    )

    return (
        f"=== CLIP DESCRIPTIONS: {clip_title} ===\n\n"
        f"── YOUTUBE SHORTS ──\n{result['youtube_short']}\n\n"
        f"── TIKTOK ──\n{result['tiktok']}\n\n"
        f"── INSTAGRAM REELS ──\n{result['instagram']}"
    )


if __name__ == "__main__":
    mcp.run()
