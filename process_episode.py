"""
process_episode.py — Automated pipeline for Is Love Blind episodes.

Phases:
  1. Clean audio (DeepFilterNet3) → transcribe (WhisperX) → profanity filter
     → export full video (clean audio) + SRT
  2. Claude clip suggestions (15–40s, sensitive topics avoided)
     → export clips with burned-in captions

Usage:
  python process_episode.py "Breaking News.mp4" --name "Ep12_BrookeTopic"
  python process_episode.py "Breaking News.mp4" --name "Ep12_BrookeTopic" --full-only
  python process_episode.py "Breaking News.mp4" --name "Ep12_BrookeTopic" --clips-only
  python process_episode.py --list
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(override=True)

from pipeline.audio_clean import clean_audio
from pipeline.captions import build_karaoke_ass, build_srt
from pipeline.clip_finder import find_clips
from pipeline.export import export_clip, export_clip_clean, get_video_duration
from pipeline.filter import censor_transcript, filter_profanity
from pipeline.transcribe import transcribe
import config as _config

# ── Paths ─────────────────────────────────────────────────────────────────────
RAW_DIR       = Path(r"D:\Podcasts\Is Love Blind\Raw Video")
OUT_DIR       = Path(r"D:\Podcasts\Is Love Blind\Auto Edited")
CACHE_DIR     = Path(os.path.dirname(__file__)) / "cache"

# ── Show context for Claude ───────────────────────────────────────────────────
PRODUCER_CONTEXT = """You are a social media producer for the reality TV show Love is Blind.
Your job is to find the most engaging, shareable moments from raw episode footage for short-form social clips.

Focus on:
- Genuine emotional reactions and relationship moments
- Surprising reveals, confessions, or first impressions
- Lighthearted, funny exchanges between cast members
- Clear story beats that make sense without prior episode context
- Moments with strong, punchy dialogue

Avoid when possible:
- Clips touching on mental health crises or emotional breakdowns
- Sensitive personal trauma or family issues disclosed in confidence
- Medical information or health struggles
- Heated arguments that could be hurtful or embarrassing out of context
- Anything that requires extensive prior context to understand

Chyron format: Cast Name (& Cast Name) | Love is Blind S#
"""


def _safe(s: str) -> str:
    return "".join(c if c.isalnum() or c in " -_" else "_" for c in s).strip().replace(" ", "_")


def _log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Phase 1 ───────────────────────────────────────────────────────────────────

def run_full_episode(video_path: Path, episode_name: str) -> tuple[dict, float, str]:
    """
    Clean audio → transcribe → filter → export full video + SRT.
    Returns (transcript, duration, audio_path_for_export).
    """
    safe      = _safe(episode_name)
    full_dir  = OUT_DIR / f"Episode_{safe}" / "full"
    full_dir.mkdir(parents=True, exist_ok=True)

    cache_audio    = CACHE_DIR / f"{safe}_clean.wav"
    cache_filtered = CACHE_DIR / f"{safe}_filtered.wav"
    cache_meta     = CACHE_DIR / f"{safe}.json"

    # Step 1 — Clean audio
    if cache_audio.exists():
        _log(f"Cache hit — skipping DeepFilterNet3: {cache_audio.name}")
    else:
        _log("Cleaning audio with DeepFilterNet3 (chunked, may take several minutes)…")
        clean_audio(str(video_path), str(cache_audio))
        _log("Audio cleaned.")

    duration = get_video_duration(str(video_path))

    # Step 2 — Transcribe
    if cache_meta.exists():
        _log(f"Cache hit — skipping WhisperX: {cache_meta.name}")
        with open(cache_meta, encoding="utf-8") as f:
            cached = json.load(f)
        transcript = cached["transcript"]
    else:
        _log("Transcribing with WhisperX large-v3 (may take several minutes)…")
        transcript = transcribe(str(cache_audio))
        with open(cache_meta, "w", encoding="utf-8") as f:
            json.dump({"transcript": transcript, "video_duration": duration}, f)
        _log(f"Transcription complete — {len(transcript['words']):,} words, {int(duration//60)}m {int(duration%60)}s.")

    # Step 3 — Profanity filter
    _log("Applying profanity filter…")
    censored_transcript, censored = censor_transcript(transcript)
    filter_profanity(str(cache_audio), transcript["words"], str(cache_filtered))
    if censored:
        _log(f"Censored {len(censored)} word(s): {', '.join(set(censored))}")
    else:
        _log("No profanity detected.")
    audio_for_export = str(cache_filtered)

    # Step 4 — Export full episode video
    full_video = full_dir / f"{safe}_full.mp4"
    _log(f"Exporting full episode video → {full_video.name}")
    export_clip_clean(
        video_path=str(video_path),
        clean_audio_path=audio_for_export,
        start=0.0,
        end=duration,
        output_path=str(full_video),
    )

    # Step 5 — SRT
    srt_path = full_dir / f"{safe}.srt"
    _log(f"Writing SRT → {srt_path.name}")
    build_srt(censored_transcript["words"], str(srt_path), start_offset=0.0)

    _log(f"Phase 1 complete → {full_dir}")
    return censored_transcript, duration, audio_for_export


# ── Phase 2 ───────────────────────────────────────────────────────────────────

def run_clips(video_path: Path, episode_name: str, transcript: dict, duration: float, audio_path: str) -> list[dict]:
    """
    Ask Claude for 15–40s clip suggestions → export each with burned-in captions.
    Returns list of exported clip metadata dicts.
    """
    safe      = _safe(episode_name)
    clips_dir = OUT_DIR / f"Episode_{safe}" / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)

    _log("Asking Claude for clip suggestions (15–40s, sensitive topics avoided)…")
    suggestions = find_clips(
        transcript,
        duration,
        producer_context=PRODUCER_CONTEXT,
        min_clip_secs=15,
        max_clip_secs=40,
    )
    _log(f"{len(suggestions)} clip(s) suggested.")

    # Load caption style from active show profile
    cfg   = _config.load()
    style = _config.active_style(cfg)

    exported = []
    for i, clip in enumerate(suggestions, 1):
        start       = float(clip["start_time"])
        end         = float(clip["end_time"])
        title       = _safe(clip.get("title", f"clip_{i:02d}"))
        description = clip.get("description", "")
        clip_words  = [w for w in transcript["words"] if start <= w["start"] <= end]

        out_stem  = f"{safe}_clip_{i:02d}_{title}"
        ass_path  = str(clips_dir / f"{out_stem}.ass")
        clip_path = str(clips_dir / f"{out_stem}.mp4")

        _log(f"  [{i}/{len(suggestions)}] {clip.get('title', '')} ({end - start:.0f}s)")
        build_karaoke_ass(clip_words, style, ass_path, start_offset=start)
        export_clip(
            video_path=str(video_path),
            clean_audio_path=audio_path,
            ass_path=ass_path,
            start=start,
            end=end,
            output_path=clip_path,
            description=description,
        )
        exported.append({
            "index":       i,
            "title":       clip.get("title", ""),
            "file":        out_stem + ".mp4",
            "start":       round(start, 2),
            "end":         round(end, 2),
            "duration":    round(end - start, 1),
            "description": description,
            "reason":      clip.get("reason", ""),
        })

    # Write metadata.json
    meta = {
        "episode":      episode_name,
        "processed_at": datetime.now().isoformat(),
        "source":       video_path.name,
        "clips":        exported,
    }
    meta_path = OUT_DIR / f"Episode_{safe}" / "metadata.json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    _log(f"Phase 2 complete → {clips_dir}")
    _log(f"Metadata → {meta_path}")
    return exported


# ── Archive ───────────────────────────────────────────────────────────────────

def archive_raw(video_path: Path) -> None:
    dest_dir = video_path.parent / "_processed"
    dest_dir.mkdir(exist_ok=True)
    dest = dest_dir / video_path.name
    shutil.move(str(video_path), str(dest))
    _log(f"Raw file archived → {dest}")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process a Love is Blind episode through the full pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("file", nargs="?", help="Raw video filename or full path")
    parser.add_argument("--name", help="Episode name, e.g. Ep12_BrookeTopic (defaults to filename)")
    parser.add_argument("--full-only",  action="store_true", help="Phase 1 only — no clip suggestions")
    parser.add_argument("--clips-only", action="store_true", help="Phase 2 only — requires cached transcript from Phase 1")
    parser.add_argument("--no-archive", action="store_true", help="Skip archiving the raw file after processing")
    parser.add_argument("--list",       action="store_true", help="List unprocessed files in the raw folder")
    args = parser.parse_args()

    # ── List mode ─────────────────────────────────────────────────────────────
    if args.list:
        processed = {f.name for f in (RAW_DIR / "_processed").iterdir()} if (RAW_DIR / "_processed").exists() else set()
        files = sorted(
            f for f in RAW_DIR.iterdir()
            if f.suffix.lower() in {".mp4", ".mov", ".mkv"} and f.name not in processed
        )
        if not files:
            print("No unprocessed files found.")
        else:
            print(f"\nUnprocessed files in {RAW_DIR}:")
            for f in files:
                print(f"  {f.name}  ({f.stat().st_size / 1e9:.1f} GB)")
        return

    if not args.file:
        parser.print_help()
        sys.exit(1)

    # ── Resolve file ──────────────────────────────────────────────────────────
    video_path = Path(args.file)
    if not video_path.exists():
        video_path = RAW_DIR / args.file
    if not video_path.exists():
        print(f"Error: file not found — {args.file}")
        sys.exit(1)

    episode_name = args.name or video_path.stem
    safe         = _safe(episode_name)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    _log(f"Episode : {episode_name}")
    _log(f"Source  : {video_path}")
    _log(f"Output  : {OUT_DIR / f'Episode_{safe}'}")
    print()

    transcript: dict | None = None
    duration:   float | None = None
    audio_path: str | None   = None

    # ── Phase 1 ───────────────────────────────────────────────────────────────
    if not args.clips_only:
        transcript, duration, audio_path = run_full_episode(video_path, episode_name)
        print()

    # ── Phase 2 ───────────────────────────────────────────────────────────────
    if not args.full_only:
        if transcript is None:
            # Load from cache for --clips-only runs
            cache_meta     = CACHE_DIR / f"{safe}.json"
            cache_filtered = CACHE_DIR / f"{safe}_filtered.wav"
            cache_clean    = CACHE_DIR / f"{safe}_clean.wav"
            if not cache_meta.exists():
                print(f"Error: no cached transcript for '{episode_name}'. Run Phase 1 first (without --clips-only).")
                sys.exit(1)
            with open(cache_meta, encoding="utf-8") as f:
                cached = json.load(f)
            transcript = cached["transcript"]
            duration   = cached["video_duration"]
            audio_path = str(cache_filtered) if cache_filtered.exists() else str(cache_clean)

        run_clips(video_path, episode_name, transcript, duration, audio_path)
        print()

    # ── Archive ───────────────────────────────────────────────────────────────
    if not args.no_archive and not args.clips_only:
        answer = input(f"Archive raw file '{video_path.name}' to _processed/? [y/N] ").strip().lower()
        if answer == "y":
            archive_raw(video_path)
        else:
            _log("Raw file left in place.")

    print()
    _log("All done.")


if __name__ == "__main__":
    main()
