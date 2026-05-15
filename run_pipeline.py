"""
run_pipeline.py — end-to-end podcast episode pipeline.

Usage:
  python run_pipeline.py --episode "s11e01" --source "D:\\path\\to\\episode.mp4"
  python run_pipeline.py --episode "s11e01" --sources "D:\\shelly.mp4" "D:\\neil.mp4"
  python run_pipeline.py --episode "s11e01" --source "D:\\episode.mp4" --min-clip 15 --max-clip 45
  python run_pipeline.py --episode "s11e01" --source "D:\\episode.mp4" --clips-only
  python run_pipeline.py --episode "s11e01" --source "D:\\episode.mp4" --export-only

Steps:
  1. compose    — stack local recordings into portrait (skipped for single source)
  2. clean      — DeepFilterNet3 audio enhancement
  3. transcribe — Deepgram Nova-3 word-level transcript
  4. suggest    — Claude Opus 4.6 clip suggestions
  5. export     — FFmpeg clip export (social + YouTube)

Progress is written to output/{episode_dir}/pipeline_status.json throughout.
All intermediate and final files follow the episode naming convention.
"""

import argparse
import json
import os
import sys
import traceback
from datetime import datetime

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import config as _config_module
from pipeline.episode import (
    clip_slug,
    ensure_episode_dirs,
    episode_dir,
    read_status,
    write_status,
)
from pipeline.export import compose_portrait, export_clip, export_clip_clean, get_video_duration
from pipeline.audio_clean import clean_audio
from pipeline.transcribe import transcribe
from pipeline.clip_finder import find_clips
from pipeline.captions import build_karaoke_ass, build_srt


def _log(status_path: str, step: str, message: str, **extra) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {step}: {message}")
    write_status(status_path, **{step: {"message": message, "ts": ts, **extra}})


def run(
    episode_id: str,
    source_paths: list[str],
    show_name: str = "",
    fill: bool = True,
    min_clip: int = 45,
    max_clip: int = 90,
    export_format: str = "both",   # "social", "youtube", "both"
    skip_to_clips: bool = False,   # re-run clips + export only (transcript already exists)
    export_only: bool = False,     # re-run export only (transcript + status clips already exist)
    producer_context: str = "",
) -> str:
    """
    Run the full pipeline for one episode. Returns the episode directory path.
    """
    # ── Setup ──────────────────────────────────────────────────────────────────
    cfg = _config_module.load()
    if not show_name:
        show_name = cfg.get("active_profile", "podcast")
    if not producer_context:
        producer_context = _config_module.active_context(cfg)
    style = _config_module.active_style(cfg)

    ep_dir = episode_dir(show_name, episode_id)
    paths  = ensure_episode_dirs(ep_dir)
    status_path = paths["status"]

    write_status(status_path,
        episode_id=episode_id,
        show=show_name,
        started=datetime.now().isoformat(),
        state="running",
        source_files=source_paths,
        min_clip=min_clip,
        max_clip=max_clip,
    )
    print(f"\n{'='*60}")
    print(f"  streamtools pipeline")
    print(f"  Episode : {episode_id}")
    print(f"  Show    : {show_name}")
    print(f"  Output  : {ep_dir}")
    print(f"{'='*60}\n")

    try:
        # ── Step 1: Compose portrait ───────────────────────────────────────────
        if not skip_to_clips and not export_only:
            if len(source_paths) > 1:
                _log(status_path, "compose", f"Stacking {len(source_paths)} recordings...")
                compose_portrait(source_paths, paths["portrait"], fill=fill)
                video_path = paths["portrait"]
                _log(status_path, "compose", "Done", output=video_path)
            else:
                video_path = source_paths[0]
                write_status(status_path, compose={"message": "Single source, skipped", "output": video_path})
                print(f"[compose] Single source file — skipping compose")
        else:
            # Resume: find existing portrait or use source
            if os.path.exists(paths["portrait"]):
                video_path = paths["portrait"]
            else:
                video_path = source_paths[0]
            print(f"[compose] Skipped (resuming) — using {video_path}")

        # ── Step 2: Clean audio ────────────────────────────────────────────────
        if not skip_to_clips and not export_only:
            _log(status_path, "clean", "Enhancing speech with DeepFilterNet3...")
            clean_audio(video_path, paths["clean"])
            duration = get_video_duration(video_path)
            write_status(status_path, video_duration=duration)
            _log(status_path, "clean", "Done", output=paths["clean"])
        else:
            duration = get_video_duration(video_path)
            write_status(status_path, video_duration=duration)
            print(f"[clean] Skipped (resuming) — using {paths['clean']}")

        # ── Step 3: Transcribe ─────────────────────────────────────────────────
        if not skip_to_clips and not export_only:
            _log(status_path, "transcribe", "Transcribing with Deepgram Nova-3...")
            transcript = transcribe(paths["clean"])
            with open(paths["transcript"], "w", encoding="utf-8") as f:
                json.dump(transcript, f)
            _log(status_path, "transcribe", f"Done — {len(transcript['words']):,} words",
                 output=paths["transcript"], word_count=len(transcript["words"]))
        else:
            with open(paths["transcript"], "r", encoding="utf-8") as f:
                transcript = json.load(f)
            print(f"[transcribe] Skipped (resuming) — loaded {len(transcript['words']):,} words")

        # ── Step 4: Suggest clips ──────────────────────────────────────────────
        if not export_only:
            _log(status_path, "suggest", f"Asking Claude for {min_clip}–{max_clip}s clips...")
            clips = find_clips(transcript, duration,
                               producer_context=producer_context,
                               min_clip_secs=min_clip,
                               max_clip_secs=max_clip)
            write_status(status_path, clips=clips)
            _log(status_path, "suggest", f"Done — {len(clips)} clips suggested")
            for i, c in enumerate(clips, 1):
                dur = c["end_time"] - c["start_time"]
                print(f"  {i}. [{c['start_time']:.0f}s–{c['end_time']:.0f}s] ({dur:.0f}s) {c['title']}")
        else:
            clips = read_status(status_path).get("clips", [])
            print(f"[suggest] Skipped (resuming) — loaded {len(clips)} clips from status")

        # ── Step 5: Export clips ───────────────────────────────────────────────
        audio_path = paths["clean"]
        exported = []

        for i, clip in enumerate(clips):
            start       = float(clip["start_time"])
            end         = float(clip["end_time"])
            title       = clip.get("title", f"clip_{i+1}")
            description = clip.get("description", "")
            slug        = clip_slug(title)

            _log(status_path, f"export_{i+1}", f"Exporting: {title} ({end-start:.0f}s)")

            clip_words = [
                w for w in transcript["words"]
                if start <= w["start"] <= end
            ]

            clip_files = {}

            if export_format in ("social", "both"):
                social_path = os.path.join(paths["clips"], f"{slug}_social.mp4")
                ass_path    = os.path.join(paths["clips"], f"{slug}.ass")
                build_karaoke_ass(clip_words, style, ass_path, start_offset=start)
                export_clip(video_path, audio_path, ass_path, start, end, social_path, description)
                clip_files["social"] = social_path

            if export_format in ("youtube", "both"):
                yt_path  = os.path.join(paths["clips"], f"{slug}_youtube.mp4")
                srt_path = os.path.join(paths["clips"], f"{slug}.srt")
                export_clip_clean(video_path, audio_path, start, end, yt_path)
                build_srt(clip_words, srt_path, start_offset=start)
                clip_files["youtube"] = yt_path
                clip_files["srt"]     = srt_path

            exported.append({"title": title, "slug": slug, **clip_files})
            _log(status_path, f"export_{i+1}", "Done", files=clip_files)

        # ── Done ───────────────────────────────────────────────────────────────
        write_status(status_path,
            state="complete",
            finished=datetime.now().isoformat(),
            exported_clips=exported,
        )
        print(f"\n{'='*60}")
        print(f"  Pipeline complete!")
        print(f"  {len(exported)} clip(s) exported to: {paths['clips']}")
        print(f"{'='*60}\n")
        return ep_dir

    except Exception as e:
        tb = traceback.format_exc()
        write_status(status_path, state="error", error=str(e), traceback=tb)
        print(f"\n[ERROR] {e}\n{tb}")
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="streamtools episode pipeline")
    parser.add_argument("--episode",    required=True, help="Short episode label, e.g. s11e01")
    parser.add_argument("--source",     help="Single source video path")
    parser.add_argument("--sources",    nargs="+", help="Multiple source paths to compose")
    parser.add_argument("--show",       default="", help="Show name (defaults to active profile)")
    parser.add_argument("--no-fill",    action="store_true", help="Letterbox instead of crop-fill")
    parser.add_argument("--min-clip",   type=int, default=45, help="Min clip duration in seconds")
    parser.add_argument("--max-clip",   type=int, default=50, help="Max clip duration in seconds")
    parser.add_argument("--format",     default="both", choices=["social", "youtube", "both"],
                        help="Export format")
    parser.add_argument("--clips-only", action="store_true",
                        help="Re-run suggest + export only (skip compose/clean/transcribe)")
    parser.add_argument("--export-only", action="store_true",
                        help="Re-run export only using clips from status JSON")
    args = parser.parse_args()

    sources = args.sources or ([args.source] if args.source else None)
    if not sources:
        parser.error("Provide --source or --sources")

    run(
        episode_id=args.episode,
        source_paths=sources,
        show_name=args.show,
        fill=not args.no_fill,
        min_clip=args.min_clip,
        max_clip=args.max_clip,
        export_format=args.format,
        skip_to_clips=args.clips_only,
        export_only=args.export_only,
    )
