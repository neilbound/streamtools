"""
streamtools MCP server — exposes the podcast pipeline as tools for Claude Desktop Cowork.

Tools:
  get_video_info                → duration + file size
  compose_portrait              → stack 2-4 landscape recordings into 1080x1920
  clean_audio                   → DeepFilterNet3 speech enhancement
  transcribe_audio              → Deepgram Nova-3, saves transcript JSON, returns path
  suggest_clips                 → Claude Opus 4.6 clip suggestions from transcript
  export_clip_social            → burned-in karaoke captions, social MP4
  export_clip_youtube           → clean MP4 + SRT for YouTube
  run_full_pipeline             → run complete pipeline in background, returns immediately
  process_broadcast_episode     → run StreamYard dual-output pipeline in background
  upload_episode_to_youtube     → upload a completed full episode to YouTube
  check_pipeline_status         → read progress/results from a running or completed pipeline

Data flow: tools pass file paths. transcribe_audio saves transcript JSON to cache/;
subsequent tools accept transcript_path to avoid putting large transcripts in context.

Long-running tools (compose, clean, transcribe, export) exceed Claude Desktop's request
timeout. Use run_full_pipeline or process_broadcast_episode to launch in the background —
they return immediately with the status file path. Poll with check_pipeline_status.
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

    pipeline_type = status.get("pipeline_type", "standard")

    # Step keys vary by pipeline type
    if pipeline_type == "broadcast":
        steps = ["compose", "clean", "transcribe", "filter",
                 "episode_export", "episode_srt", "episode_mp3",
                 "describe", "suggest"]
    elif pipeline_type == "shorts_season":
        steps = ["stitch", "clean", "transcribe", "filter",
                 "v_stitch", "v_clean", "v_transcribe", "v_filter",
                 "episode_export", "episode_srt", "episode_mp3", "describe"]
    else:
        steps = ["compose", "clean", "transcribe", "filter", "suggest"]

    for step in steps:
        if step in status:
            lines.append(f"  ✓ {step}: {status[step].get('message', '')}")

    # Count clip exports
    exported_keys = [k for k in status if k.startswith("export_") and not k.startswith("export_only")]
    if exported_keys:
        lines.append(f"  ✓ exports: {len(exported_keys)} clip(s) done")

    # Count segment exports
    seg_v_keys = [k for k in status if k.startswith("seg_v_")]
    seg_h_keys = [k for k in status if k.startswith("seg_h_")]
    if seg_v_keys or seg_h_keys:
        lines.append(f"  ✓ segments: {len(seg_h_keys)} horizontal, {len(seg_v_keys)} vertical")

    if state == "complete":
        clips = status.get("exported_clips", [])
        lines.append(f"\nComplete — {len(clips)} clip(s) exported:")
        for c in clips:
            lines.append(f"  • {c['title']}")
            for fmt in ("social", "youtube", "srt", "descriptions"):
                if fmt in c:
                    lines.append(f"      {fmt}: {c[fmt]}")

        if pipeline_type in ("broadcast", "shorts_season"):
            ep_status  = status.get("episode_export", {})
            mp3_status = status.get("episode_mp3", {})
            if ep_status.get("output"):
                lines.append(f"\n  Full episode : {ep_status['output']}")
            if mp3_status.get("output"):
                lines.append(f"  Podcast MP3  : {mp3_status['output']}")
            if pipeline_type == "shorts_season":
                n_seg_h = len([k for k in status if k.startswith("seg_h_")])
                n_seg_v = len([k for k in status if k.startswith("seg_v_")])
                lines.append(f"  Segments     : {n_seg_h} horizontal, {n_seg_v} vertical")
            spotify_url = status.get("spotify_upload_url", "")
            if spotify_url:
                lines.append(f"\n  ► Upload to Spotify: {spotify_url}")

        lines.append(f"\nFinished: {status.get('finished', '?')}")

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
    channel: str = "neilbound",
    force: bool = False,
) -> str:
    """
    Add a clip to the publish queue for scheduled delivery to social platforms.

    The publisher daemon (publisher_daemon.py) runs every 15 minutes via Windows Task
    Scheduler and will upload the clip when the scheduled_time is reached.

    The clip is QA-checked (probe: streams, codec, aspect, duration) before
    queueing; QA issues block scheduling unless force=True.

    Args:
        clip_path:      Absolute path to the exported MP4 clip.
        platforms:      List of target platforms: "youtube", "tiktok", "instagram".
        title:          Post title / caption.
        description:    Longer description (used by YouTube; optional for others).
        scheduled_time: ISO 8601 UTC datetime string, e.g. "2026-05-16T15:00:00+00:00".
                        If empty, schedules for now (uploaded at next daemon run).
        tags:           Optional hashtag strings (without '#').
        channel:        Publishing channel: "neilbound" or "ilb". Default "neilbound".
        force:          Schedule even if QA checks find issues.

    Returns:
        Confirmation message with the assigned post_id, or a QA refusal.
    """
    from pipeline.publish_queue import enqueue, get_entry
    from pipeline.validate import validate_media
    from datetime import datetime, timezone

    # deep=True: arbitrary paths have no stored export-time QA, and truncation
    # (intact moov header, missing data) is only detectable by the decode pass.
    # Costs ~2s for a <62s clip.
    qa_issues, qa_warnings = validate_media(clip_path, profile="clip", deep=True)
    if qa_issues and not force:
        lines = [f"NOT scheduled — QA found {len(qa_issues)} issue(s) with {clip_path}:"]
        lines += [f"  [ERROR] {i}" for i in qa_issues]
        lines += [f"  [WARNING] {w}" for w in qa_warnings]
        lines.append("Fix and re-export, or re-run with force=True to schedule anyway.")
        return "\n".join(lines)

    if not scheduled_time:
        scheduled_time = datetime.now(tz=timezone.utc).isoformat()

    post_id = enqueue(
        clip_path=clip_path,
        platforms=platforms,
        title=title,
        description=description,
        scheduled_time_iso=scheduled_time,
        tags=tags or [],
        channel=channel,
        extra={"expected_orientation": "portrait"},
    )

    lines = [
        f"Queued for publishing.",
        f"post_id   : {post_id}",
        f"Platforms : {', '.join(platforms)}",
        f"Scheduled : {scheduled_time}",
        f"Title     : {title}",
    ]
    if qa_issues:
        lines.append(f"QA OVERRIDDEN (force=True) — {len(qa_issues)} issue(s): " + "; ".join(qa_issues))
    for w in qa_warnings:
        lines.append(f"QA warning: {w}")
    entry = get_entry(post_id)
    for w in (entry or {}).get("warnings", []):
        lines.append(f"Queue warning: {w}")
    lines.append("")
    lines.append("The daemon will upload this at or after the scheduled time.")
    lines.append("Use list_scheduled_clips to check status or cancel_scheduled_clip to remove it.")
    return "\n".join(lines)


@mcp.tool()
def publish_clip_now(
    clip_path: str,
    platforms: list[str],
    title: str,
    description: str = "",
    tags: list[str] = [],
    channel: str = "neilbound",
    force: bool = False,
) -> str:
    """
    Upload and publish a clip immediately to one or more social platforms.

    This runs the upload functions directly (not via the queue) and may take
    30-120 seconds depending on file size and platform. Requires PUBLISHING_ENABLED=true
    and valid credentials in .env.

    The clip is QA-checked (probe: streams, codec, aspect, duration) first;
    QA issues block the upload unless force=True.

    Args:
        clip_path:   Absolute path to the exported MP4 clip.
        platforms:   List of target platforms: "youtube", "tiktok", "instagram".
        title:       Post title / caption.
        description: Longer description (used by YouTube; optional for others).
        tags:        Optional hashtag strings (without '#').
        channel:     Publishing channel: "neilbound" or "ilb". Default "neilbound".
        force:       Publish even if QA checks find issues.

    Returns:
        Per-platform results (video IDs, URLs, publish IDs), or a QA refusal.
    """
    from pipeline.publish import upload_youtube, upload_tiktok, upload_instagram
    from pipeline.validate import validate_media
    import traceback as _tb

    # deep=True: catches truncated files that pass probe-only checks (see
    # schedule_clip). ~2s — negligible next to a 30-120s upload.
    qa_issues, qa_warnings = validate_media(clip_path, profile="clip", deep=True)
    if qa_issues and not force:
        lines = [f"NOT published — QA found {len(qa_issues)} issue(s) with {clip_path}:"]
        lines += [f"  [ERROR] {i}" for i in qa_issues]
        lines += [f"  [WARNING] {w}" for w in qa_warnings]
        lines.append("Fix and re-export, or re-run with force=True to publish anyway.")
        return "\n".join(lines)

    uploaders = {
        "youtube":   lambda: upload_youtube(clip_path, title, description, tags or [], channel=channel),
        "tiktok":    lambda: upload_tiktok(clip_path, title, tags or [], channel=channel),
        "instagram": lambda: upload_instagram(clip_path, title, channel=channel),
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

    def _is_unposted_tiktok_draft(res: dict) -> bool:
        return (
            res.get("status") == "ok"
            and res.get("requires_manual_post")
            and not res.get("manually_posted")
        )

    # ── NEEDS ATTENTION: failures, partials, unposted TikTok drafts, warnings ──
    attention: list[str] = []
    for e in entries:
        post_id = e.get("post_id", "?")
        title   = e.get("title", "(no title)")
        status  = e.get("status", "?")
        results = e.get("results", {})

        if status == "failed":
            first_err = next(
                (r.get("error", "?") for r in results.values()
                 if r.get("status") == "error"), "?")
            attention.append(f"  FAILED: {title} ({post_id}) — {first_err}")
        elif status == "partial":
            pending_pl = [p for p in e.get("platforms", [])
                          if results.get(p, {}).get("status") != "ok"]
            attention.append(
                f"  PARTIAL: {title} ({post_id}) — still pending/failed: "
                f"{', '.join(pending_pl)}")

        tiktok_res = results.get("tiktok", {})
        if _is_unposted_tiktok_draft(tiktok_res):
            attention.append(
                f"  TIKTOK DRAFT: {title} ({post_id}) — uploaded to drafts; "
                f"open the TikTok app and tap Post, then run "
                f"confirm_tiktok_posted('{post_id}')")

        for w in e.get("warnings", []):
            attention.append(f"  WARNING: {title} ({post_id}) — {w}")

    lines = ["=== NEEDS ATTENTION ==="]
    if attention:
        lines.extend(attention)
    else:
        lines.append("  Nothing needs attention.")
    lines.append("")

    lines.append(f"Publish queue — {len(entries)} entry(ies):\n")
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
                if platform == "tiktok" and _is_unposted_tiktok_draft(res):
                    lines.append(
                        f"    tiktok: UPLOADED TO DRAFTS — needs manual post in app "
                        f"({res.get('publish_id', '')})")
                elif res.get("status") == "ok":
                    url = res.get("url") or res.get("publish_id") or res.get("media_id", "")
                    posted_note = " (manually posted)" if res.get("manually_posted") else ""
                    lines.append(f"    {platform}: OK ({url}){posted_note}")
                else:
                    fatal_note = " [FATAL — no auto-retry; use retry_failed_clip]" if res.get("fatal") else ""
                    lines.append(f"    {platform}: ERROR{fatal_note} — {res.get('error', '?')}")
        for w in e.get("warnings", []):
            lines.append(f"    warning: {w}")
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


@mcp.tool()
def retry_failed_clip(post_id: str) -> str:
    """
    Re-arm a partial or failed post so the daemon retries ONLY the platforms that
    have not yet succeeded.

    Use this instead of manually resetting a post — it preserves the results of
    platforms that already uploaded, so they are never posted twice. The entry is
    set back to 'pending'; the next daemon run (or publish_clip_now) will attempt
    only the failed/missing platforms.

    Args:
        post_id: The 8-character post identifier (from list_scheduled_clips).

    Returns:
        Confirmation listing which platforms will be retried, or an explanation
        if there was nothing to retry.
    """
    from pipeline.publish_queue import retry_failed

    success, to_retry = retry_failed(post_id)

    if success:
        return (
            f"Post {post_id} re-armed for retry.\n"
            f"Platforms to retry : {', '.join(to_retry)}\n"
            f"Already-successful platforms are preserved and will NOT be re-uploaded.\n"
            f"The daemon will process this at its next run."
        )
    return (
        f"Nothing to retry for post {post_id}. "
        "Either the post_id was not found, or every platform already succeeded. "
        "Use list_scheduled_clips to check the current status."
    )


@mcp.tool()
def reconcile_uploads(channel: str = "ilb") -> str:
    """
    Audit every queue entry marked as a successful YouTube upload against the actual
    channel, catching problems that slipped through after the fact:
      - "missing":   the video was deleted or rejected (no longer on the channel)
      - "truncated": stuck with no duration (interrupted/corrupt upload)

    Videos still processing normally are not flagged. Run this periodically (or after
    an incident) to confirm what the queue thinks posted actually did, intact.

    Args:
        channel: Publishing channel to audit. Default "ilb".

    Returns:
        A report of any discrepancies, or confirmation that all are healthy.
    """
    from pipeline.publish import reconcile_youtube
    try:
        problems = reconcile_youtube(channel)
    except Exception as exc:
        return f"Reconciliation failed: {type(exc).__name__}: {exc}"

    if not problems:
        return f"All YouTube uploads for channel '{channel}' verified healthy on the channel."

    lines = [f"{len(problems)} discrepancy(ies) on channel '{channel}':", ""]
    for p in problems:
        lines.append(f"  [{p['issue'].upper()}] {p['video_id']}  (post {p['post_id']})")
        lines.append(f"      {p['title']}")
    lines.append("")
    lines.append("'missing' = deleted/rejected (a good copy may exist under a different ID); "
                 "'truncated' = corrupt upload. Use retry_failed_clip after clearing the bad "
                 "result, or re-arm the entry, to re-post.")
    return "\n".join(lines)


@mcp.tool()
def confirm_tiktok_posted(post_id: str) -> str:
    """
    Record that a TikTok inbox upload has been manually posted in the app.

    TikTok "inbox" mode uploads land in the account's drafts and the operator
    must open the TikTok app and tap Post. The queue marks the upload "ok" but
    keeps a reminder in list_scheduled_clips' NEEDS ATTENTION section until
    this tool confirms the post actually went live.

    Args:
        post_id: The 8-character post identifier (from list_scheduled_clips).

    Returns:
        Confirmation or an explanation if the post wasn't a TikTok draft.
    """
    from pipeline.publish_queue import confirm_manual_post

    if confirm_manual_post(post_id, "tiktok"):
        return (
            f"Recorded: TikTok draft for post {post_id} was manually posted. "
            f"The reminder is cleared from NEEDS ATTENTION."
        )
    return (
        f"Could not confirm post {post_id}. Either the post_id was not found, "
        f"or its TikTok result was not an inbox/draft upload. "
        f"Use list_scheduled_clips to check."
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


# ── Broadcast pipeline tools ──────────────────────────────────────────────────

@mcp.tool()
def process_broadcast_episode(
    episode_id: str,
    horizontal_path: str,
    vertical_path: str,
    episode_title: str = "",
    episode_notes: str = "",
    show_name: str = "",
    local_recordings: list[str] = [],
    min_clip: int = 45,
    max_clip: int = 50,
    export_format: str = "both",
    filter_audio: bool = True,
    cover_art_path: str = "",
    channel: str = "",   # empty → active profile's pipeline.default_channel
    generate_mp3: bool = False,
) -> str:
    """
    Run the StreamYard dual-output broadcast pipeline in the background.

    Takes the 16:9 horizontal episode and the 9:16 vertical shorts source from the
    same StreamYard session. Both carry the same mixed audio — cleaned once and
    reused for all outputs.

    Produces in output/{show}_{episode_id}_{date}/:
      episode/{slug}_youtube.mp4      — full 16:9 episode for YouTube
      episode/{slug}.srt              — full episode captions
      episode/{slug}.mp3              — podcast MP3 (ID3 tagged, upload to Spotify manually)
      episode/{slug}_description.txt  — Claude YouTube description
      episode/{slug}_shownotes.txt    — Claude show notes for Spotify
      clips/{slug}_social.mp4         — 9:16 with karaoke captions (from vertical source)
      clips/{slug}_youtube.mp4        — 9:16 clean (from vertical source)
      clips/{slug}_descriptions.json  — YouTube Short / TikTok / Instagram captions

    Args:
        episode_id:        Short episode label, e.g. "s11e01" or "manosphere_pt2".
        horizontal_path:   Absolute path to the 16:9 StreamYard horizontal download.
        vertical_path:     Absolute path to the 9:16 StreamYard vertical download.
        episode_title:     Full episode title for descriptions and ID3 tags.
        episode_notes:     Producer notes to guide Claude description generation.
        show_name:         Show name for directory naming. Defaults to active profile.
        local_recordings:  Optional list of per-participant local recording paths to compose
                           a higher-quality episode (replaces horizontal_path as video source).
        min_clip:          Minimum clip duration in seconds. Default 45.
        max_clip:          Maximum clip duration in seconds. Default 50.
        export_format:     "social", "youtube", or "both". Default "both".
        filter_audio:      Apply profanity filter to audio and captions. Default True.
        cover_art_path:    Optional absolute path to cover art image for podcast MP3.
        channel:           Publishing channel identifier, e.g. "neilbound" or "ilb".
                           Must match credentials set up via setup_credentials.py --channel.
                           Default "neilbound".
        generate_mp3:      Also export a podcast MP3 (ID3 tagged). Default False — upload
                           the video episode directly to Spotify instead.

    Returns:
        Status file path for polling with check_pipeline_status.
        Includes a reminder to upload the episode video to Spotify manually.
    """
    python_exe = os.path.join(os.path.dirname(__file__), ".venv312", "Scripts", "python.exe")
    script     = os.path.join(os.path.dirname(__file__), "run_pipeline.py")

    cmd = [python_exe, script,
           "--broadcast",
           "--episode", episode_id,
           "--horizontal", horizontal_path,
           "--vertical",   vertical_path]
    if episode_title:
        cmd += ["--episode-title", episode_title]
    if episode_notes:
        cmd += ["--episode-notes", episode_notes]
    if show_name:
        cmd += ["--show", show_name]
    if local_recordings:
        cmd += ["--local-recordings"] + list(local_recordings)
    cmd += ["--min-clip", str(min_clip), "--max-clip", str(max_clip)]
    cmd += ["--format", export_format]
    if not filter_audio:
        cmd.append("--no-filter")
    if cover_art_path:
        cmd += ["--cover-art", cover_art_path]
    if channel:
        cmd += ["--channel", channel]
    if generate_mp3:
        cmd.append("--generate-mp3")

    subprocess.Popen(
        cmd,
        cwd=os.path.dirname(__file__),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    from pipeline.episode import episode_dir as _ep_dir
    import config as _cfg
    cfg = _cfg.load()
    sn  = show_name or cfg.get("active_profile", "podcast")
    ep  = _ep_dir(sn, episode_id)
    status_path = os.path.join(ep, "pipeline_status.json")

    return (
        f"Broadcast pipeline started in background.\n"
        f"Episode  : {episode_id}\n"
        f"Title    : {episode_title or episode_id}\n"
        f"Output   : {ep}\n"
        f"Status   : {status_path}\n\n"
        f"Use check_pipeline_status to monitor progress.\n\n"
        f"NOTE: When complete, upload the episode video to Spotify manually:\n"
        f"  https://podcasters.spotify.com/pod/dashboard/episodes/new"
    )


@mcp.tool()
def process_shorts_season(
    episode_id: str,
    segments_dir: str,
    episode_title: str = "",
    episode_notes: str = "",
    show_name: str = "",
    group: str = "",
    intro_max_clips: int = 1,
    default_max_clips: int = 3,
    min_clip: int = 30,
    max_clip: int = 55,
    export_format: str = "both",
    filter_audio: bool = True,
    cover_art_path: str = "",
    channel: str = "",   # empty → active profile's pipeline.default_channel
    generate_mp3: bool = False,
    vertical_paths: list[str] | None = None,
    chyron_suffix: str = "",
) -> str:
    """
    Run the shorts-season pipeline in the background.

    A shorts season consists of multiple independently recorded segments
    (intro, overall impressions, per-couple or per-topic segments) that are:
      1. Stitched into a single full episode (YouTube + Spotify MP3)
      2. Individually mined for social shorts

    Expects `segments_dir` to contain StreamYard dual-output pairs:
      - Horizontal 16:9 (no 📱 emoji) — used for the full episode video
      - Vertical   9:16 (with 📱 emoji) — used as the shorts source

    Segments are auto-detected and sorted by recording timestamp.
    The first (intro) segment gets `intro_max_clips` shorts; all others
    get `default_max_clips` each. Shorts are never forced — if a segment
    doesn't have enough natural clip material, fewer are generated.

    Produces in output/{group}/{show}_{episode_id}_{date}/:
      episode/{slug}_youtube.mp4              — full 16:9 episode for YouTube
      episode/{slug}.srt / .mp3 / _description.txt / _shownotes.txt
      clips/{seg}__{clip}_social.mp4          — 9:16 with karaoke captions
      clips/{seg}__{clip}_youtube.mp4         — 9:16 clean (vertical source)
      clips/{seg}__{clip}.srt / _descriptions.json
      segments/{seg_slug}_youtube.mp4         — full segment, vertical 9:16
      segments/{seg_slug}.srt
      segments/{seg_slug}_horizontal_youtube.mp4  — full segment, horizontal 16:9
      segments/{seg_slug}_horizontal.srt
      segment_manifest.json

    Args:
        episode_id:           Short label, e.g. "aoa_s1" or "age_of_attraction_s1".
        segments_dir:         Absolute path to the directory containing segment pairs.
        episode_title:        Full episode title for descriptions and ID3 tags.
        episode_notes:        Producer notes to guide Claude descriptions.
        show_name:            Show name for directory naming. Defaults to active profile.
        group:                Output folder group (e.g. "age_of_attraction_s1").
                              Creates output/{group}/{show}_{id}_{date}/ instead of
                              the flat output/{show}_{id}_{date}/.
        intro_max_clips:      Max clips extracted from the intro segment. Default 1.
        default_max_clips:    Max clips extracted from each non-intro segment. Default 3.
        min_clip:             Min clip duration in seconds. Default 30.
        max_clip:             Max clip duration in seconds. Default 55.
        export_format:        "social", "youtube", or "both". Default "both".
        filter_audio:         Apply profanity filter. Default True.
        cover_art_path:       Optional absolute path to cover art image for MP3 tags.
        channel:              Publishing channel. Empty → active profile's
                              pipeline.default_channel.
        generate_mp3:         Also export a podcast MP3 (ID3 tagged). Default False — upload
                              the video episode directly to Spotify instead.
        vertical_paths:       Optional override for the 9:16 vertical sources. By default
                              each segment's vertical counterpart (the 📱 StreamYard file)
                              is auto-detected, so this is rarely needed. Shorts always run
                              as a separate vertical pipeline (stitch → clean → transcribe →
                              find clips → export) — no H/V drift, title cards preserved.

    Returns:
        Status file path for polling with check_pipeline_status.
    """
    python_exe = os.path.join(os.path.dirname(__file__), ".venv312", "Scripts", "python.exe")
    script     = os.path.join(os.path.dirname(__file__), "run_pipeline.py")

    cmd = [python_exe, script,
           "--shorts-season",
           "--episode",      episode_id,
           "--segments-dir", segments_dir]
    if episode_title:
        cmd += ["--episode-title", episode_title]
    if episode_notes:
        cmd += ["--episode-notes", episode_notes]
    if show_name:
        cmd += ["--show", show_name]
    if group:
        cmd += ["--group", group]
    cmd += ["--intro-max-clips",   str(intro_max_clips)]
    cmd += ["--default-max-clips", str(default_max_clips)]
    cmd += ["--min-clip", str(min_clip), "--max-clip", str(max_clip)]
    cmd += ["--format", export_format]
    if not filter_audio:
        cmd.append("--no-filter")
    if cover_art_path:
        cmd += ["--cover-art", cover_art_path]
    if channel:
        cmd += ["--channel", channel]
    if generate_mp3:
        cmd.append("--generate-mp3")
    if vertical_paths:
        cmd += ["--vertical-paths", json.dumps(vertical_paths)]
    if chyron_suffix:
        cmd += ["--chyron-suffix", chyron_suffix]

    subprocess.Popen(
        cmd,
        cwd=os.path.dirname(__file__),
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )

    from pipeline.episode import episode_dir as _ep_dir
    import config as _cfg
    cfg = _cfg.load()
    sn  = show_name or cfg.get("active_profile", "podcast")
    ep  = _ep_dir(sn, episode_id, group=group)
    status_path = os.path.join(ep, "pipeline_status.json")

    return (
        f"Shorts season pipeline started in background.\n"
        f"Episode  : {episode_id}\n"
        f"Title    : {episode_title or episode_id}\n"
        f"Group    : {group or '(none)'}\n"
        f"Segments : {segments_dir}\n"
        f"Output   : {ep}\n"
        f"Status   : {status_path}\n\n"
        f"Use check_pipeline_status to monitor progress.\n\n"
        f"NOTE: When complete, upload the episode video to Spotify manually:\n"
        f"  https://podcasters.spotify.com/pod/dashboard/episodes/new"
    )


@mcp.tool()
def upload_episode_to_youtube(
    episode_dir_path: str,
    title: str,
    scheduled_time: str = "",
    category_id: str = "22",
    thumbnail_path: str = "",
    channel: str = "neilbound",
) -> str:
    """
    Upload a completed full episode to YouTube from the episode output directory.

    Reads the pipeline_status.json to verify the pipeline is complete, then locates
    the *_youtube.mp4, *.srt, and *_description.txt files automatically.

    Requires PUBLISHING_ENABLED=true and YOUTUBE_* credentials in .env.
    Run: python setup_credentials.py --platform youtube --channel <channel>

    Args:
        episode_dir_path: Path to the episode root directory (returned by check_pipeline_status
                          or process_broadcast_episode), e.g.
                          "C:/path/output/is_love_blind_s11e01_2026-05-18".
        title:            YouTube video title (max 100 chars).
        scheduled_time:   ISO 8601 UTC string for scheduled publish, e.g.
                          "2026-05-20T15:00:00+00:00". If empty, publishes immediately.
        category_id:      YouTube category ID. Default "22" (People & Blogs).
        thumbnail_path:   Optional absolute path to a thumbnail image (JPEG/PNG).
        channel:          Publishing channel: "neilbound" or "ilb". Default "neilbound".

    Returns:
        YouTube video URL and upload details.
    """
    import glob as _glob

    status_path = os.path.join(episode_dir_path, "pipeline_status.json")
    if not os.path.exists(status_path):
        return f"pipeline_status.json not found in {episode_dir_path}. Has the pipeline run?"

    from pipeline.episode import read_status
    status = read_status(status_path)

    if status.get("state") != "complete":
        return (
            f"Pipeline state is '{status.get('state', 'unknown')}' — must be 'complete' before uploading.\n"
            f"Use check_pipeline_status to see current progress."
        )

    if status.get("pipeline_type") != "broadcast":
        return (
            "upload_episode_to_youtube is for broadcast pipeline episodes only.\n"
            "Use publish_clip_now or schedule_clip for short clips."
        )

    ep_subdir = os.path.join(episode_dir_path, "episode")

    # Locate video
    video_matches = _glob.glob(os.path.join(ep_subdir, "*_youtube.mp4"))
    if not video_matches:
        return f"No *_youtube.mp4 found in {ep_subdir}. Check that episode_export step completed."
    video_path = video_matches[0]

    # Locate SRT (non-fatal if missing)
    srt_matches = _glob.glob(os.path.join(ep_subdir, "*.srt"))
    srt_path = srt_matches[0] if srt_matches else None

    # Locate description (non-fatal if missing)
    desc_matches = _glob.glob(os.path.join(ep_subdir, "*_description.txt"))
    description = ""
    if desc_matches:
        with open(desc_matches[0], "r", encoding="utf-8") as f:
            description = f.read()

    from pipeline.publish import upload_youtube_episode

    try:
        result = upload_youtube_episode(
            video_path=video_path,
            title=title,
            description=description,
            scheduled_time=scheduled_time or None,
            category_id=category_id,
            srt_path=srt_path,
            thumbnail_path=thumbnail_path or None,
            channel=channel,
        )
        lines = [
            f"Episode uploaded to YouTube.",
            f"  URL       : {result['url']}",
            f"  Video ID  : {result['video_id']}",
            f"  Scheduled : {result['scheduled']}",
            f"  Captions  : {'uploaded' if result['captions_uploaded'] else 'not uploaded (check manually)'}",
        ]
        if srt_path:
            lines.append(f"  SRT file  : {srt_path}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Upload failed: {type(exc).__name__}: {exc}"


# ── Clip scheduling tools ─────────────────────────────────────────────────────

@mcp.tool()
def review_episode_clips(episode_dir_path: str) -> str:
    """
    Show all clip descriptions from a completed broadcast pipeline run for review
    before scheduling. Returns platform-specific captions for each clip so you
    can request edits before calling schedule_episode_clips.

    Args:
        episode_dir_path: Path to the episode root directory returned by
                          process_broadcast_episode or check_pipeline_status.

    Returns:
        Formatted review of every clip with YouTube Shorts, TikTok, and
        Instagram captions. Includes clip slugs needed for description_overrides
        in schedule_episode_clips.
    """
    import glob as _glob

    status_path = os.path.join(episode_dir_path, "pipeline_status.json")
    if not os.path.exists(status_path):
        return f"pipeline_status.json not found in {episode_dir_path}."

    from pipeline.episode import read_status
    status = read_status(status_path)

    if status.get("state") != "complete":
        return (
            f"Pipeline is not complete yet (state: {status.get('state', 'unknown')}).\n"
            "Run check_pipeline_status to see current progress."
        )

    exported = status.get("exported_clips", [])
    if not exported:
        return "No exported clips found in pipeline status."

    lines = [
        f"Episode: {status.get('episode_title') or status.get('episode_id', '?')}",
        f"Clips:   {len(exported)}\n",
        "Review each clip's descriptions below. Note the slug for any edits.",
        "─" * 60,
    ]

    for i, clip in enumerate(exported, 1):
        title = clip.get("title", f"clip_{i}")
        slug  = clip.get("slug", "")
        desc_path = clip.get("descriptions", "")

        lines.append(f"\n[{i}] {title}")
        lines.append(f"    slug: {slug}")

        # Show video paths
        if clip.get("social"):
            lines.append(f"    social : {clip['social']}")
        if clip.get("youtube"):
            lines.append(f"    youtube: {clip['youtube']}")

        # QA results from the export run — this is what the scheduling gate
        # will enforce, so surface it here where edits are decided.
        qa_issues   = clip.get("qa_issues") or []
        qa_warnings = clip.get("qa_warnings") or []
        if qa_issues:
            lines.append("    QA: FAIL — will be BLOCKED at scheduling (unless force=True)")
            for issue in qa_issues:
                lines.append(f"      [ERROR]   {issue}")
        elif qa_warnings:
            lines.append("    QA: WARN")
        else:
            lines.append("    QA: PASS")
        for warning in qa_warnings:
            lines.append(f"      [WARNING] {warning}")

        # Load descriptions
        if desc_path and os.path.exists(desc_path):
            with open(desc_path, "r", encoding="utf-8") as f:
                descs = json.load(f)

            for w in descs.get("_warnings", []):
                lines.append(f"    [DESCRIPTION WARNING] {w}")

            lines.append("\n    ── YouTube Shorts ──")
            lines.append(f"    {descs.get('youtube_short', '(none)')}")

            lines.append("\n    ── TikTok ──")
            lines.append(f"    {descs.get('tiktok', '(none)')}")

            lines.append("\n    ── Instagram Reels ──")
            lines.append(f"    {descs.get('instagram', '(none)')}")
        else:
            lines.append("    (no descriptions file found)")

        lines.append("─" * 60)

    lines.append(
        "\nTo schedule with edits, call schedule_episode_clips with description_overrides:\n"
        '  { "slug": { "youtube_short": "...", "tiktok": "...", "instagram": "..." } }'
    )

    return "\n".join(lines)


@mcp.tool()
def schedule_episode_clips(
    episode_dir_path: str,
    channel: str = "",   # empty → active profile's pipeline.default_channel
    platforms: list[str] = ["youtube", "tiktok", "instagram"],
    description_overrides: dict = {},
    start_date: str = "",
    day_interval: int = 1,
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """
    Schedule all clips from a completed broadcast episode at optimal posting times.
    Reads descriptions from the pipeline output and enqueues each clip to the publish queue.

    Call review_episode_clips first to see descriptions and request any edits,
    then pass those edits via description_overrides.

    QA GATE: clips whose pipeline QA found issues (qa_issues), or that fail a
    fresh probe check, are SKIPPED unless force=True. QA warnings never block.

    Optimal posting times rotate through 12pm, 6pm, and 9am EST to avoid
    looking automated.

    Args:
        episode_dir_path:      Path to the episode root directory.
        channel:               Publishing channel: "neilbound" or "ilb". Default "neilbound".
        platforms:             List of platforms to post to. Default all three.
        description_overrides: Optional per-clip description edits keyed by slug:
                               { "slug": { "youtube_short": "...", "tiktok": "...", "instagram": "..." } }
        start_date:            ISO date (YYYY-MM-DD) for the first post. Defaults to today.
        day_interval:          Days between each post. Default 1 (daily). Use 2 for every other day.
        force:                 Schedule clips even if QA found issues.
        dry_run:               Report what would be scheduled (slots, blocked clips,
                               warnings) WITHOUT enqueueing or writing the checklist.

    Returns:
        Summary of scheduled posts with dates, times, and platforms.
    """
    from pipeline.episode import read_status
    from pipeline.publish_queue import enqueue, get_entry
    from pipeline.validate import quick_probe_check
    from datetime import date, datetime, timedelta, timezone
    import config as _cfg
    _cfg_data = _cfg.load()
    _brand    = _cfg.active_brand(_cfg_data)
    _pipeline = _cfg.active_pipeline(_cfg_data)

    # Resolve channel from config when the caller didn't specify one
    if not channel:
        channel = _pipeline["default_channel"]

    # ── Posting time slots (hours in UTC), from the active profile ──
    # The scheduler cycles through these so consecutive posts don't all land at
    # the same time / look automated. Configure per-show via pipeline.posting_slots_utc.
    # Defaults: 12pm / 6pm / 9am EST (16 / 22 / 13 UTC at EDT = UTC-4).
    _POSTING_SLOTS_UTC = _pipeline["posting_slots_utc"] or [16, 22, 13]

    status_path = os.path.join(episode_dir_path, "pipeline_status.json")
    if not os.path.exists(status_path):
        return f"pipeline_status.json not found in {episode_dir_path}."

    status = read_status(status_path)
    if status.get("state") != "complete":
        return (
            f"Pipeline is not complete (state: {status.get('state', 'unknown')}).\n"
            "Use check_pipeline_status to monitor progress."
        )

    exported = status.get("exported_clips", [])
    if not exported:
        return "No exported clips found in pipeline status."

    # Determine start date
    if start_date:
        post_date = date.fromisoformat(start_date)
    else:
        post_date = date.today()

    mode_note = " (DRY RUN — nothing will be queued)" if dry_run else ""
    lines = [
        f"Scheduling {len(exported)} clip(s) for channel '{channel}'{mode_note}",
        f"Platforms : {', '.join(platforms)}",
        f"Starting  : {post_date.isoformat()}",
        "",
    ]

    scheduled_clips = []   # collected for checklist generation
    blocked_clips   = []   # QA-blocked (skipped) clips for the summary

    for i, clip in enumerate(exported):
        title = clip.get("title", f"clip_{i+1}")
        slug  = clip.get("slug", "")

        # Determine which clip file to use — social for TikTok/Instagram, youtube for YT
        social_path  = clip.get("social", "")
        youtube_path = clip.get("youtube", "")
        clip_path    = social_path or youtube_path
        if not clip_path or not os.path.exists(clip_path):
            lines.append(f"  [{i+1}] SKIPPED — clip file not found: {clip_path}")
            post_date += timedelta(days=day_interval)
            continue

        # ── QA gate ────────────────────────────────────────────────────────
        # Stored QA from export time, plus a fresh probe (the file may have
        # been corrupted or replaced since the pipeline ran).
        # Legacy translation: older pipeline runs stored ">62s" duration as a
        # blocking issue; current policy only blocks past the 180s platform
        # cap (62s is a performance warning). Don't block on the stale rule.
        import re as _qre
        from pipeline.validate import QA_PROFILES as _qa_profiles
        _platform_cap = _qa_profiles["clip"]["max_duration"]
        gate_issues = []
        for issue in (clip.get("qa_issues") or []):
            m = _qre.match(r"DURATION: (\d+(?:\.\d+)?)s exceeds", issue)
            if m and float(m.group(1)) <= _platform_cap:
                continue   # legacy 62s rule — a warning under current policy
            gate_issues.append(issue)
        probe_err = quick_probe_check(clip_path, "portrait")
        if probe_err:
            gate_issues.append(f"PROBE: {probe_err}")
        if gate_issues and not force:
            blocked_clips.append((title, gate_issues))
            lines.append(f"  [{i+1}] BLOCKED by QA — {title}")
            for issue in gate_issues:
                lines.append(f"       [ERROR] {issue}")
            post_date += timedelta(days=day_interval)
            continue
        if gate_issues and force:
            lines.append(f"  [{i+1}] QA OVERRIDDEN (force=True) — {title}: {'; '.join(gate_issues)}")

        # Load base descriptions
        desc_path = clip.get("descriptions", "")
        descs = {}
        if desc_path and os.path.exists(desc_path):
            with open(desc_path, "r", encoding="utf-8") as f:
                descs = json.load(f)

        # Apply overrides
        if slug in description_overrides:
            descs.update(description_overrides[slug])

        # Build per-platform titles/captions
        yt_title    = descs.get("youtube_short", title)[:100]   # YouTube 100-char limit
        tiktok_cap  = descs.get("tiktok", title)
        ig_cap      = descs.get("instagram", title)

        # Pick posting time for this day
        hour_utc = _POSTING_SLOTS_UTC[i % len(_POSTING_SLOTS_UTC)]
        post_dt  = datetime(
            post_date.year, post_date.month, post_date.day,
            hour_utc, 0, 0, tzinfo=timezone.utc
        )
        sched_iso = post_dt.isoformat()

        # Enqueue with per-platform caption as description
        # The publisher daemon uses title for TikTok/Instagram caption and
        # description for YouTube — we pass the platform-appropriate text.
        entry_warnings: list[str] = []
        if dry_run:
            post_id = "(dry-run)"
        else:
            post_id = enqueue(
                clip_path=social_path or youtube_path,
                platforms=platforms,
                title=yt_title,
                description=ig_cap,        # stored for Instagram
                scheduled_time_iso=sched_iso,
                tags=[],
                channel=channel,
                extra={
                    "tiktok_caption":       tiktok_cap,
                    "instagram_caption":    ig_cap,
                    "youtube_path":         youtube_path,
                    "playlist_id":          _brand.get("youtube_playlist_shorts", ""),
                    "expected_orientation": "portrait",
                },
            )
            entry_warnings = (get_entry(post_id) or {}).get("warnings", [])

        # Description-generation warnings (e.g. a platform section Claude
        # failed to produce) ride along in the descriptions JSON.
        desc_warnings = descs.get("_warnings", [])

        # Convert UTC hour → EDT (UTC-4) 12-hour label for any configured slot
        _est_hour = (hour_utc - 4) % 24
        _ampm     = "AM" if _est_hour < 12 else "PM"
        _disp     = _est_hour % 12 or 12
        local_time_label = f"{_disp}:00 {_ampm} EST"

        # Extract full episode URL from descriptions for checklist
        import re as _re
        ep_url_match = _re.search(r'https://youtu\.be/\S+', descs.get("youtube_short", ""))
        ep_url = ep_url_match.group(0).rstrip("|").strip() if ep_url_match else ""

        scheduled_clips.append({
            "title":      title,
            "post_id":    post_id,
            "date_label": f"{post_date.isoformat()} at {local_time_label}",
            "post_date":  post_date,
            "episode_url": ep_url,
            "youtube_short_url": "",   # filled in by daemon after upload
            "yt_title":   yt_title,
            "tiktok_cap": tiktok_cap,
            "ig_cap":     ig_cap,
        })

        lines.append(
            f"  [{i+1}] {title}\n"
            f"       post_id  : {post_id}\n"
            f"       date     : {post_date.isoformat()} at {local_time_label}\n"
            f"       clip     : {os.path.basename(clip_path)}"
        )
        for w in entry_warnings:
            lines.append(f"       warning  : {w}")
        for w in desc_warnings:
            lines.append(f"       warning  : {w}")

        post_date += timedelta(days=day_interval)

    if blocked_clips:
        lines.append("")
        lines.append(f"{len(blocked_clips)} clip(s) were BLOCKED by QA and NOT scheduled:")
        for title, issues in blocked_clips:
            lines.append(f"  - {title}: {'; '.join(issues)}")
        lines.append("Fix and re-export, or re-run with force=True to schedule anyway.")

    if dry_run:
        lines.append(
            "\nDRY RUN complete — nothing was queued and no checklist was written.\n"
            "Re-run with dry_run=False to schedule."
        )
        return "\n".join(lines)

    lines.append(
        "\nAll clips queued. The publisher daemon will upload each at its scheduled time.\n"
        "Use list_scheduled_clips to review or cancel_scheduled_clip to remove any."
    )

    # ── Write upload checklist ─────────────────────────────────────────────
    checklist_path = os.path.join(episode_dir_path, "UPLOAD_CHECKLIST.md")
    _write_upload_checklist(checklist_path, scheduled_clips, platforms, channel)
    lines.append(f"\nUpload checklist written to: {checklist_path}")

    return "\n".join(lines)


def _write_upload_checklist(path: str, scheduled_clips: list[dict], platforms: list[str], channel: str) -> None:
    """
    Write a per-clip, per-platform manual-action checklist to disk.

    scheduled_clips: list of dicts with keys title, post_id, date_label, post_date,
                     youtube_url, clip_path, yt_title, tiktok_cap, ig_cap
    """
    from datetime import datetime as _dt

    lines = [
        "# Upload Checklist",
        f"Generated: {_dt.now().strftime('%Y-%m-%d %H:%M')}  |  Channel: {channel}",
        "",
        "The publisher daemon handles the actual uploads automatically.",
        "These are the **manual steps** to complete in each platform's UI",
        "after each clip goes live.",
        "",
        "---",
        "",
    ]

    for i, clip in enumerate(scheduled_clips, 1):
        title      = clip.get("title", f"Clip {i}")
        post_id    = clip.get("post_id", "")
        date_label = clip.get("date_label", "")
        yt_url     = clip.get("youtube_short_url", "")
        ep_url     = clip.get("episode_url", "")

        lines += [
            f"## Clip {i}: {title}",
            f"Post ID: `{post_id}`  |  Scheduled: {date_label}",
            "",
        ]

        if "youtube" in platforms:
            lines += [
                "### YouTube Shorts",
                f"Direct URL (available ~5 min after upload): {yt_url or '(paste after upload)'}",
                "",
                "- [ ] **End screen** — YouTube Studio → Editor → End screen",
                f"      Add a Video card pointing to the full episode: {ep_url or '(add episode URL)'}",
                "      Place it in the last 5–20 seconds of the clip.",
                "- [ ] **Pin a comment** — paste this after the Short is live:",
                f"      Full episode: {ep_url or '(episode URL)'}",
                "- [ ] **Playlist** — add to 'Is Love Blind Shorts' (create if it doesn't exist)",
                "- [ ] **Category** — confirm set to 'Entertainment' or 'People & Blogs'",
                "- [ ] **Not made for kids** — verify this is toggled off",
                "- [ ] **Thumbnail** — review the auto-selected frame; swap if needed",
                "",
            ]

        if "instagram" in platforms:
            lines += [
                "### Instagram Reels",
                "- [ ] **Bio link** — update link in bio to the full episode URL before this posts",
                f"      Full episode: {ep_url or '(episode URL)'}",
                "- [ ] **Share to Story** — repost the Reel to your Story within the first hour",
                "      (drives initial views and signals the algorithm)",
                "- [ ] **Reply to early comments** — respond to the first 3–5 comments quickly",
                "- [ ] **Co-host tag** — if applicable, tag the co-host account in the post",
                "",
            ]

        if "tiktok" in platforms:
            lines += [
                "### TikTok",
                "- [ ] **Cover frame** — set to the most expressive moment in the clip",
                "- [ ] **Collection** — add to a TikTok Series/Collection for this show",
                "- [ ] **Duet/Stitch** — enable both (increases discoverability)",
                "- [ ] **Reply to early comments** — especially any that ask for more context",
                "",
            ]

        lines += ["---", ""]

    lines += [
        "## General Notes",
        "",
        "**What the pipeline already handles automatically:**",
        "- Video upload to all platforms",
        "- Title and description with full episode link",
        "- Hashtags and tags",
        "- Karaoke captions burned into the social clip",
        "- Posting time optimization (rotating 9am / 12pm / 6pm EST)",
        "",
        "**What always requires manual action:**",
        "- YouTube end screen / related video card (API limitation — no programmatic end screen for Shorts)",
        "- Pinned YouTube comment",
        "- Instagram bio link update",
        "- TikTok cover frame selection",
        "- Any boosting / paid promotion decisions",
        "",
        "**If a clip fails to upload:**",
        "- Check `list_scheduled_clips` for the error message",
        "- Fix the issue (token expired, file missing, etc.)",
        "- Use `publish_clip_now` with the post_id to retry immediately",
        "- Or update the scheduled time and let the daemon retry",
    ]

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    mcp.run()
