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
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)

import config as _config_module
from pipeline.episode import (
    clip_slug,
    ensure_episode_dirs,
    episode_dir,
    read_status,
    write_status,
)
from pipeline.export import compose_portrait, export_clip, export_clip_clean, get_video_duration, stitch_segments
from pipeline.audio_clean import clean_audio
from pipeline.transcribe import transcribe
from pipeline.filter import censor_transcript, filter_profanity
from pipeline.clip_finder import find_clips
from pipeline.captions import build_karaoke_ass, build_srt
from pipeline.export import export_episode_youtube
from pipeline.podcast import export_podcast_mp3
from pipeline.describe import generate_episode_descriptions, generate_clip_descriptions
from pipeline.validate import validate_clip, format_validation


def _snap_to_sentence_end(end: float, words: list[dict], window: float = 12.0,
                          min_pause: float = 0.6) -> float:
    """
    Snap `end` to the nearest natural speech boundary (inter-word pause >= min_pause)
    within `window` seconds either side of `end`.

    Deepgram word-level transcripts rarely include sentence-ending punctuation, so
    this function relies on pauses rather than punctuation markers.

    Strategy:
    1. Collect all pauses >= min_pause within `end ± window`.
    2. From those, prefer pauses within ±5s of end (tight window first).
    3. Within the tight window, pick the one closest to `end`.
    4. If none in tight window, use the closest across the full ±window.
    5. Fallback: return original `end` unchanged.

    Returns the *end* time of the word immediately before the pause so the clip
    includes the last spoken syllable before the natural break.
    """
    # Build a list of (cut_time, distance_from_end, gap) for every qualifying pause.
    # Extend the left boundary by 2s so words that START just before the window
    # but END inside it are included for gap calculation.
    candidates = []
    w_all = [w for w in words if end - window - 2 <= w.get("start", 0) <= end + window + 1]
    for i in range(len(w_all) - 1):
        w_cur  = w_all[i]
        w_next = w_all[i + 1]
        gap = w_next.get("start", 0) - w_cur.get("end", 0)
        if gap >= min_pause:
            cut = w_cur.get("end", end)
            dist = abs(cut - end)
            candidates.append((cut, dist, gap))

    if not candidates:
        return end

    # Prefer candidates within ±5s (tight window).
    # Fall back to full window only if the nearest candidate is within 8s —
    # beyond that the clip would be significantly shortened/extended with no
    # guarantee of a cleaner ending, so leave it at the original end.
    tight = [c for c in candidates if c[1] <= 5.0]
    if tight:
        pool = tight
    else:
        wide_best = min(candidates, key=lambda c: (c[1], -c[2]))
        if wide_best[1] > 8.0:
            return end  # No useful boundary nearby — leave unchanged
        pool = candidates

    # Pick closest to end; break ties by largest gap
    best = min(pool, key=lambda c: (c[1], -c[2]))
    return best[0]


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
    max_clip: int = 50,
    export_format: str = "both",   # "social", "youtube", "both"
    skip_to_clips: bool = False,   # re-run clips + export only (transcript already exists)
    export_only: bool = False,     # re-run export only (transcript + status clips already exist)
    producer_context: str = "",
    run_date: str = "",            # override date for resuming a previous run (YYYY-MM-DD)
    filter_audio: bool = True,     # replace profanity with beep tone in audio + captions
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

    ep_dir = episode_dir(show_name, episode_id, run_date=run_date)
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

        # ── Step 4: Profanity filter ───────────────────────────────────────────
        if filter_audio and not export_only:
            if not skip_to_clips and os.path.exists(paths["filtered"]):
                # Resume: filtered file already exists, just re-apply censored transcript
                _log(status_path, "filter", "Using cached filtered audio...")
                transcript, censored = censor_transcript(transcript)
                _log(status_path, "filter", f"Done — {len(set(censored))} unique word(s) censored" if censored else "Done — no profanity detected")
            else:
                _log(status_path, "filter", "Applying profanity filter...")
                original_words = list(transcript["words"])  # must use originals — censor_transcript replaces with **** which filter_profanity can't detect
                transcript, censored = censor_transcript(transcript)
                filter_profanity(paths["clean"], original_words, paths["filtered"])
                if censored:
                    _log(status_path, "filter", f"Done — censored {len(censored)} instance(s): {', '.join(set(censored))}")
                else:
                    _log(status_path, "filter", "Done — no profanity detected")
        elif filter_audio and export_only:
            if os.path.exists(paths["filtered"]):
                original_words = list(transcript["words"])
                transcript, censored = censor_transcript(transcript)
                print(f"[filter] Skipped (resuming) — using {paths['filtered']}")
            else:
                # filtered.wav missing (e.g. deleted) — rebuild it now
                _log(status_path, "filter", "Rebuilding filtered audio (filtered.wav missing)...")
                original_words = list(transcript["words"])
                transcript, censored = censor_transcript(transcript)
                filter_profanity(paths["clean"], original_words, paths["filtered"])
                if censored:
                    _log(status_path, "filter", f"Done — censored {len(censored)} instance(s): {', '.join(set(censored))}")
                else:
                    _log(status_path, "filter", "Done — no profanity detected")

        # ── Step 5: Suggest clips ──────────────────────────────────────────────
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

        # ── Step 6: Export clips ───────────────────────────────────────────────
        # Use filtered audio if it exists, otherwise fall back to clean audio
        audio_path = paths["filtered"] if filter_audio and os.path.exists(paths["filtered"]) else paths["clean"]
        exported = []

        for i, clip in enumerate(clips):
            start       = float(clip["start_time"])
            end         = _snap_to_sentence_end(float(clip["end_time"]), transcript["words"])
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

            # ── QA validation ──────────────────────────────────────────────
            qa_issues, qa_warnings = validate_clip(
                clip_path        = clip_files.get("social") or clip_files.get("youtube", ""),
                start            = start,
                end              = end,
                title            = title,
                transcript_words = transcript["words"],
            )
            print(format_validation(qa_issues, qa_warnings, title))
            exported.append({
                "title": title, "slug": slug,
                **clip_files,
                "qa_issues":   qa_issues,
                "qa_warnings": qa_warnings,
            })
            _log(status_path, f"export_{i+1}", "Done", files=clip_files,
                 qa_issues=qa_issues, qa_warnings=qa_warnings)

        # ── Done ───────────────────────────────────────────────────────────────
        qa_failures = sum(1 for c in exported if c.get("qa_issues"))
        write_status(status_path,
            state="complete",
            finished=datetime.now().isoformat(),
            exported_clips=exported,
            qa_summary={
                "total": len(exported),
                "passed": len(exported) - qa_failures,
                "failed": qa_failures,
            },
        )
        print(f"\n{'='*60}")
        print(f"  Pipeline complete!")
        print(f"  {len(exported)} clip(s) exported to: {paths['clips']}")
        if qa_failures:
            print(f"  QA: {qa_failures} clip(s) have issues — review before scheduling!")
        else:
            print(f"  QA: all clips passed")
        print(f"{'='*60}\n")
        return ep_dir

    except Exception as e:
        tb = traceback.format_exc()
        write_status(status_path, state="error", error=str(e), traceback=tb)
        print(f"\n[ERROR] {e}\n{tb}")
        raise


def run_broadcast(
    episode_id: str,
    horizontal_path: str,
    vertical_path: str,
    episode_title: str = "",
    episode_notes: str = "",
    show_name: str = "",
    local_recordings: list[str] | None = None,
    min_clip: int = 45,
    max_clip: int = 50,
    export_format: str = "both",   # "social", "youtube", "both"
    filter_audio: bool = True,
    run_date: str = "",
    skip_to_clips: bool = False,
    export_only: bool = False,
    cover_art_path: str | None = None,
    channel: str = "",   # empty → resolved from active profile's pipeline.default_channel
    generate_mp3: bool = False,
) -> str:
    """
    Run the StreamYard dual-output broadcast pipeline.

    Takes a 16:9 horizontal (for full episode) and a 9:16 vertical (for shorts)
    from the same StreamYard session. Both carry the same mixed audio — audio is
    cleaned once from the horizontal and reused for all outputs.

    Produces:
      episode/{slug}_youtube.mp4   — full 16:9 episode for YouTube
      episode/{slug}.srt           — full episode captions
      episode/{slug}.mp3           — podcast audio (ID3 tagged, upload to Spotify manually)
      episode/{slug}_description.txt — Claude YouTube description
      episode/{slug}_shownotes.txt   — Claude show notes for Spotify
      clips/{slug}_social.mp4      — 9:16 with karaoke captions (from vertical source)
      clips/{slug}_youtube.mp4     — 9:16 clean (from vertical source)
      clips/{slug}.srt
      clips/{slug}_descriptions.json

    Returns the episode directory path.
    """
    # ── Setup ──────────────────────────────────────────────────────────────────
    cfg = _config_module.load()
    if not show_name:
        show_name = cfg.get("active_profile", "podcast")
    if not episode_title:
        episode_title = episode_id
    producer_context = _config_module.active_context(cfg)
    brand  = _config_module.active_brand(cfg)
    style  = _config_module.active_style(cfg)

    # Resolve channel from config when the caller didn't specify one
    if not channel:
        channel = _config_module.active_pipeline(cfg)["default_channel"]

    ep_dir = episode_dir(show_name, episode_id, run_date=run_date)
    paths  = ensure_episode_dirs(ep_dir)
    status_path = paths["status"]

    write_status(status_path,
        pipeline_type="broadcast",
        episode_id=episode_id,
        episode_title=episode_title,
        show=show_name,
        channel=channel,
        started=datetime.now().isoformat(),
        state="running",
        horizontal_path=horizontal_path,
        vertical_path=vertical_path,
        min_clip=min_clip,
        max_clip=max_clip,
    )
    print(f"\n{'='*60}")
    print(f"  streamtools broadcast pipeline")
    print(f"  Episode : {episode_id}")
    print(f"  Title   : {episode_title}")
    print(f"  Show    : {show_name}")
    print(f"  Output  : {ep_dir}")
    print(f"{'='*60}\n")

    try:
        # ── Step 1: Compose (optional local recordings) ────────────────────────
        if not skip_to_clips and not export_only:
            if local_recordings and len(local_recordings) >= 2:
                _log(status_path, "compose",
                     f"Stacking {len(local_recordings)} local recordings...")
                compose_portrait(local_recordings, paths["portrait"], fill=True)
                _log(status_path, "compose", "Done", output=paths["portrait"])
                episode_video_src = paths["portrait"]
            else:
                episode_video_src = horizontal_path
                write_status(status_path, compose={"message": "No local recordings — using horizontal", "output": horizontal_path})
                print(f"[compose] No local recordings — using horizontal StreamYard download")
        else:
            episode_video_src = paths["portrait"] if os.path.exists(paths["portrait"]) else horizontal_path
            print(f"[compose] Skipped (resuming) — using {episode_video_src}")

        # ── Step 2: Clean audio (from horizontal source) ───────────────────────
        if not skip_to_clips and not export_only:
            _log(status_path, "clean", "Enhancing speech with DeepFilterNet3...")
            clean_audio(horizontal_path, paths["clean"])
            duration = get_video_duration(horizontal_path)
            write_status(status_path, video_duration=duration)
            _log(status_path, "clean", "Done", output=paths["clean"])
        else:
            duration = get_video_duration(horizontal_path)
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

        # ── Step 4: Profanity filter ───────────────────────────────────────────
        if filter_audio and not export_only:
            if not skip_to_clips and os.path.exists(paths["filtered"]):
                _log(status_path, "filter", "Using cached filtered audio...")
                transcript, censored = censor_transcript(transcript)
                _log(status_path, "filter",
                     f"Done — {len(set(censored))} unique word(s) censored" if censored else "Done — no profanity detected")
            else:
                _log(status_path, "filter", "Applying profanity filter...")
                original_words = list(transcript["words"])
                transcript, censored = censor_transcript(transcript)
                filter_profanity(paths["clean"], original_words, paths["filtered"])
                if censored:
                    _log(status_path, "filter", f"Done — censored {len(censored)} instance(s): {', '.join(set(censored))}")
                else:
                    _log(status_path, "filter", "Done — no profanity detected")
        elif filter_audio and export_only:
            if os.path.exists(paths["filtered"]):
                original_words = list(transcript["words"])
                transcript, censored = censor_transcript(transcript)
                print(f"[filter] Skipped (resuming) — using {paths['filtered']}")
            else:
                _log(status_path, "filter", "Rebuilding filtered audio (filtered.wav missing)...")
                original_words = list(transcript["words"])
                transcript, censored = censor_transcript(transcript)
                filter_profanity(paths["clean"], original_words, paths["filtered"])
                if censored:
                    _log(status_path, "filter", f"Done — censored {len(censored)} instance(s): {', '.join(set(censored))}")
                else:
                    _log(status_path, "filter", "Done — no profanity detected")

        # Determine which audio to use for exports
        audio_path = paths["filtered"] if filter_audio and os.path.exists(paths["filtered"]) else paths["clean"]

        # ── Step 5: Full episode video export ─────────────────────────────────
        if not export_only or not os.path.exists(paths["episode_video"]):
            _log(status_path, "episode_export", "Exporting full episode for YouTube...")
            export_episode_youtube(episode_video_src, audio_path, paths["episode_video"])
            _log(status_path, "episode_export", "Done", output=paths["episode_video"])
        else:
            print(f"[episode_export] Skipped (resuming) — {paths['episode_video']}")

        # ── Step 6: Full episode SRT ───────────────────────────────────────────
        if not export_only or not os.path.exists(paths["episode_srt"]):
            _log(status_path, "episode_srt", "Building full episode SRT...")
            from pipeline.captions import build_srt as _build_srt
            _build_srt(transcript["words"], paths["episode_srt"], start_offset=0.0)
            _log(status_path, "episode_srt", "Done", output=paths["episode_srt"])
        else:
            print(f"[episode_srt] Skipped (resuming) — {paths['episode_srt']}")

        # ── Step 7: Podcast MP3 (optional) ────────────────────────────────────
        if generate_mp3:
            if not export_only or not os.path.exists(paths["episode_mp3"]):
                _log(status_path, "episode_mp3", "Encoding podcast MP3...")
                mp3_result = export_podcast_mp3(
                    audio_path=audio_path,
                    output_path=paths["episode_mp3"],
                    title=episode_title,
                    description=episode_notes,
                    show_name=brand.get("show_name") or show_name,
                    cover_art_path=cover_art_path,
                )
                _log(status_path, "episode_mp3", f"Done — {mp3_result['size_mb']:.1f} MB",
                     output=paths["episode_mp3"],
                     spotify_upload_url=mp3_result["spotify_upload_url"])
            else:
                print(f"[episode_mp3] Skipped (resuming) — {paths['episode_mp3']}")
        else:
            print(f"[episode_mp3] Skipped — upload video episode to Spotify directly")

        # ── Step 8: Episode descriptions ──────────────────────────────────────
        if not export_only or not os.path.exists(paths["episode_desc"]):
            _log(status_path, "describe", "Generating episode descriptions with Claude...")
            desc_result = generate_episode_descriptions(
                transcript=transcript,
                episode_title=episode_title,
                episode_notes=episode_notes,
                brand=brand,
            )
            # Write YouTube description
            with open(paths["episode_desc"], "w", encoding="utf-8") as f:
                f.write(desc_result["youtube_full"])
            # Write show notes (title options + description — paste into Spotify form)
            shownotes_lines = []
            if desc_result["title_options"]:
                shownotes_lines.append("TITLE OPTIONS:")
                for i, t in enumerate(desc_result["title_options"], 1):
                    shownotes_lines.append(f"  {i}. {t}")
                shownotes_lines.append("")
            shownotes_lines.append(desc_result["youtube_full"])
            with open(paths["episode_notes"], "w", encoding="utf-8") as f:
                f.write("\n".join(shownotes_lines))
            _log(status_path, "describe", "Done",
                 episode_desc=paths["episode_desc"],
                 episode_notes=paths["episode_notes"])
        else:
            print(f"[describe] Skipped (resuming) — {paths['episode_desc']}")

        # ── Step 9: Suggest clips ──────────────────────────────────────────────
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

        # ── Step 10–11: Export clips from vertical source ──────────────────────
        exported = []

        for i, clip in enumerate(clips):
            start       = float(clip["start_time"])
            end         = _snap_to_sentence_end(float(clip["end_time"]), transcript["words"])
            title_str   = clip.get("title", f"clip_{i+1}")
            description = clip.get("description", "")
            slug        = clip_slug(title_str)

            _log(status_path, f"export_{i+1}", f"Exporting: {title_str} ({end-start:.0f}s)")

            clip_words = [
                w for w in transcript["words"]
                if start <= w["start"] <= end
            ]

            clip_files = {}

            if export_format in ("social", "both"):
                social_path = os.path.join(paths["clips"], f"{slug}_social.mp4")
                ass_path    = os.path.join(paths["clips"], f"{slug}.ass")
                build_karaoke_ass(clip_words, style, ass_path, start_offset=start)
                # Use vertical_path directly — StreamYard 9:16 output is already portrait
                export_clip(vertical_path, audio_path, ass_path, start, end, social_path, description)
                clip_files["social"] = social_path

            if export_format in ("youtube", "both"):
                yt_path  = os.path.join(paths["clips"], f"{slug}_youtube.mp4")
                srt_path = os.path.join(paths["clips"], f"{slug}.srt")
                export_clip_clean(vertical_path, audio_path, start, end, yt_path)
                build_srt(clip_words, srt_path, start_offset=start)
                clip_files["youtube"] = yt_path
                clip_files["srt"]     = srt_path

            # Generate platform descriptions for this clip
            try:
                _log(status_path, f"clip_desc_{i+1}", f"Generating descriptions for: {title_str}")
                episode_context = f"{brand.get('show_name') or show_name} — {episode_title}"
                clip_desc = generate_clip_descriptions(
                    clip_words=clip_words,
                    clip_title=title_str,
                    episode_context=episode_context,
                    brand=brand,
                )
                desc_path = os.path.join(paths["clips"], f"{slug}_descriptions.json")
                with open(desc_path, "w", encoding="utf-8") as f:
                    json.dump(clip_desc, f, indent=2)
                clip_files["descriptions"] = desc_path
                _log(status_path, f"clip_desc_{i+1}", "Done", output=desc_path)
            except Exception as e:
                print(f"[clip_desc_{i+1}] Warning: description generation failed: {e}")

            # ── QA validation ──────────────────────────────────────────────
            qa_issues, qa_warnings = validate_clip(
                clip_path        = clip_files.get("social") or clip_files.get("youtube", ""),
                start            = start,
                end              = end,
                title            = title_str,
                transcript_words = transcript["words"],
            )
            print(format_validation(qa_issues, qa_warnings, title_str))
            exported.append({
                "title": title_str, "slug": slug,
                **clip_files,
                "qa_issues":   qa_issues,
                "qa_warnings": qa_warnings,
            })
            _log(status_path, f"export_{i+1}", "Done", files=clip_files,
                 qa_issues=qa_issues, qa_warnings=qa_warnings)

        # ── Done ───────────────────────────────────────────────────────────────
        qa_failures = sum(1 for c in exported if c.get("qa_issues"))
        spotify_url = "https://podcasters.spotify.com/pod/dashboard/episodes/new"
        write_status(status_path,
            state="complete",
            finished=datetime.now().isoformat(),
            exported_clips=exported,
            qa_summary={
                "total": len(exported),
                "passed": len(exported) - qa_failures,
                "failed": qa_failures,
            },
            spotify_upload_url=spotify_url,
        )
        print(f"\n{'='*60}")
        print(f"  Broadcast pipeline complete!")
        print(f"  Full episode : {paths['episode_video']}")
        if generate_mp3:
            print(f"  Podcast MP3  : {paths['episode_mp3']}")
        print(f"  {len(exported)} clip(s) in: {paths['clips']}")
        print(f"\n  >> Upload episode video to Spotify: {spotify_url}")
        print(f"{'='*60}\n")
        return ep_dir

    except Exception as e:
        tb = traceback.format_exc()
        write_status(status_path, state="error", error=str(e), traceback=tb)
        print(f"\n[ERROR] {e}\n{tb}")
        raise


def _detect_segments(
    segments_dir: str,
    intro_max_clips: int = 1,
    default_max_clips: int = 3,
    label_prefixes: list[str] | None = None,
) -> list[dict]:
    """
    Scan a directory for StreamYard dual-output segment pairs.

    Naming convention (set by StreamYard):
      - Horizontal 16:9: "Show - Title.mp4"
      - Vertical   9:16: "Show - Title 📱.mp4"

    Pairs are matched by stripping ' 📱' from the vertical filename.
    Returned list is sorted by the MP4's embedded creation_time tag so
    segments come out in recording order regardless of filesystem sort.
    The first segment in recording order is treated as the intro and
    gets intro_max_clips; all others get default_max_clips.

    label_prefixes: ordered list of filename prefixes to strip when building the
                    clean segment label (e.g. "Age Of Attraction - Season 1 - ").
                    Configured per-show via the profile's pipeline settings. The
                    list is checked longest-first so the most specific match wins.

    Raises ValueError if any horizontal file has no matching vertical.
    """
    # Strip the most specific (longest) prefix first to avoid a short prefix
    # shadowing a longer one (e.g. "Show - " vs "Show - Season 1 - ").
    prefixes = sorted(label_prefixes or [], key=len, reverse=True)
    import subprocess as _sp

    FFPROBE = os.path.join(
        r"C:\Users\ntmas\AppData\Local\Microsoft\WinGet\Packages"
        r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
        r"\ffmpeg-8.1-full_build\bin",
        "ffprobe.exe",
    )

    files = os.listdir(segments_dir)
    verticals   = {f for f in files if "\U0001f4f1" in f and f.endswith(".mp4")}
    horizontals = [f for f in files if "\U0001f4f1" not in f and f.endswith(".mp4")]

    # Build vertical lookup: strip ' 📱' suffix from stem → horizontal filename
    vert_map: dict[str, str] = {}
    for vf in verticals:
        stem = vf.replace(" \U0001f4f1.mp4", ".mp4").replace("\U0001f4f1.mp4", ".mp4")
        vert_map[stem] = vf

    pairs = []
    for hf in horizontals:
        vf = vert_map.get(hf)
        if not vf:
            expected = hf.replace(".mp4", " \U0001f4f1.mp4")
            raise ValueError(
                f"No vertical counterpart found for '{hf}'. "
                f"Expected a file named '{expected}'"
            )
        h_path = os.path.join(segments_dir, hf)
        v_path = os.path.join(segments_dir, vf)

        # Read embedded creation_time for sort order
        result = _sp.run(
            [FFPROBE, "-v", "quiet", "-print_format", "json",
             "-show_format", h_path],
            capture_output=True, text=True, encoding="utf-8",
        )
        try:
            data  = json.loads(result.stdout)
            ct    = data.get("format", {}).get("tags", {}).get("creation_time", "")
        except Exception:
            ct = ""

        label = hf.replace(".mp4", "")
        # Strip configured show prefixes for a cleaner label (longest match first)
        for prefix in prefixes:
            if label.startswith(prefix):
                label = label[len(prefix):]
                break

        pairs.append({
            "label":      label,
            "horizontal": h_path,
            "vertical":   v_path,
            "creation_time": ct,
        })

    # Sort by recording time; fall back to filesystem order
    pairs.sort(key=lambda p: p["creation_time"])

    # Assign clip caps — first segment = intro
    for i, seg in enumerate(pairs):
        seg["max_clips"] = intro_max_clips if i == 0 else default_max_clips
        del seg["creation_time"]  # not needed downstream

    return pairs


def run_shorts_season(
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
    run_date: str = "",
    export_only: bool = False,
    cover_art_path: str | None = None,
    channel: str = "",   # empty → resolved from active profile's pipeline.default_channel
    generate_mp3: bool = False,
    vertical_paths: list[str] | None = None,
    chyron_suffix: str = "",
) -> str:
    """
    Pipeline for a "shorts season" — multiple short recorded segments that are
    stitched into one full episode AND individually mined for social shorts.

    Expects a directory of StreamYard dual-output pairs:
      - Horizontal 16:9 file (no 📱 emoji) — full episode video source
      - Vertical   9:16 file (with 📱 emoji) — shorts source (auto-detected)

    vertical_paths (ordered list matching the detected segments) is REQUIRED.
    Shorts run as a completely separate pipeline from the episode:
      vertical stitch → clean → transcribe → find clips → caption → export
    Video and audio both come from the vertical source — zero H/V drift.

    Segments are auto-detected and sorted by recording timestamp.
    The first segment (intro) gets at most `intro_max_clips` shorts (default 1).
    All other segments get at most `default_max_clips` shorts each (default 3).

    Produces:
      episode/{slug}_youtube.mp4              — full stitched episode (16:9, all segments)
      episode/{slug}.srt                      — full episode captions
      episode/{slug}.mp3                      — podcast MP3 (ID3 tagged, optional)
      episode/{slug}_description.txt
      episode/{slug}_shownotes.txt
      clips/{seg_label}__{clip_slug}_social.mp4        — 9:16 with karaoke captions
      clips/{seg_label}__{clip_slug}_youtube.mp4       — 9:16 clean (vertical source)
      clips/{seg_label}__{clip_slug}.srt
      clips/{seg_label}__{clip_slug}_descriptions.json
      segments/{seg_slug}_youtube.mp4                  — per-segment 9:16 (vertical source)
      segments/{seg_slug}.srt
      segments/{seg_slug}_horizontal_youtube.mp4       — per-segment 16:9 (horizontal source)
      segments/{seg_slug}_horizontal.srt
      segment_manifest.json  — segment labels, offsets, durations, clip counts

    Returns the episode directory path.
    """
    # ── Setup ──────────────────────────────────────────────────────────────────
    cfg = _config_module.load()
    if not show_name:
        show_name = cfg.get("active_profile", "podcast")
    if not episode_title:
        episode_title = episode_id
    producer_context = _config_module.active_context(cfg)
    brand    = _config_module.active_brand(cfg)
    style    = _config_module.active_style(cfg)
    pipeline_cfg = _config_module.active_pipeline(cfg)

    # Resolve channel from config when the caller didn't specify one
    if not channel:
        channel = pipeline_cfg["default_channel"]

    # Shorts are sourced directly from the 9:16 vertical StreamYard files. The
    # old horizontal-only fallback (stack_h split-crop) is gone, so vertical
    # paths are required.
    if not vertical_paths:
        raise ValueError(
            "run_shorts_season requires vertical_paths — the ordered list of 9:16 "
            "vertical StreamYard files matching the detected segments. Shorts are "
            "sourced directly from the vertical files (zero H/V drift, title cards "
            "preserved); the horizontal stack_h fallback has been removed."
        )

    ep_dir = episode_dir(show_name, episode_id, run_date=run_date, group=group)
    paths  = ensure_episode_dirs(ep_dir)
    status_path = paths["status"]

    # Auto-detect segment pairs (label prefixes are per-show, from config)
    segments = _detect_segments(
        segments_dir, intro_max_clips, default_max_clips,
        label_prefixes=pipeline_cfg["segment_label_prefixes"],
    )

    write_status(status_path,
        pipeline_type="shorts_season",
        episode_id=episode_id,
        episode_title=episode_title,
        show=show_name,
        group=group,
        channel=channel,
        started=datetime.now().isoformat(),
        state="running",
        segments_dir=segments_dir,
        segment_count=len(segments),
        min_clip=min_clip,
        max_clip=max_clip,
    )
    print(f"\n{'='*60}")
    print(f"  streamtools shorts season pipeline")
    print(f"  Episode  : {episode_id}")
    print(f"  Title    : {episode_title}")
    print(f"  Show     : {show_name}")
    print(f"  Segments : {len(segments)}")
    print(f"  Output   : {ep_dir}")
    print(f"{'='*60}\n")
    for i, seg in enumerate(segments):
        label = seg["label"]
        cap   = seg["max_clips"]
        marker = " [intro — 1 clip max]" if i == 0 else f" (max {cap} clips)"
        print(f"  {i+1}. {label}{marker}")
    print()

    try:
        # ── Step 1: Stitch horizontals → full episode source ───────────────────
        h_paths = [seg["horizontal"] for seg in segments]
        if not export_only and not os.path.exists(paths["stitched"]):
            _log(status_path, "stitch",
                 f"Stitching {len(segments)} segments into full episode...")
            _, offsets = stitch_segments(h_paths, paths["stitched"])
            _log(status_path, "stitch", "Done", output=paths["stitched"])
        else:
            # Recalculate offsets from individual durations (no re-stitch needed)
            offsets = []
            t = 0.0
            for hp in h_paths:
                offsets.append(t)
                t += get_video_duration(hp)
            if export_only:
                print(f"[stitch] Skipped (resuming) — using {paths['stitched']}")
            else:
                print(f"[stitch] Already exists — using {paths['stitched']}")

        # Attach offsets and durations to segment dicts for downstream use
        for i, seg in enumerate(segments):
            seg["offset"]   = offsets[i]
            seg["duration"] = get_video_duration(seg["horizontal"])

        # Write segment manifest
        manifest_data = [
            {
                "label":      s["label"],
                "horizontal": s["horizontal"],
                "vertical":   s["vertical"],
                "offset":     s["offset"],
                "duration":   s["duration"],
                "max_clips":  s["max_clips"],
            }
            for s in segments
        ]
        with open(paths["segment_manifest"], "w", encoding="utf-8") as f:
            json.dump(manifest_data, f, indent=2)

        # ── Step 2: Clean audio from stitched file ─────────────────────────────
        full_duration = sum(s["duration"] for s in segments)
        if not export_only and not os.path.exists(paths["clean"]):
            _log(status_path, "clean", "Enhancing speech with DeepFilterNet3...")
            clean_audio(paths["stitched"], paths["clean"])
            write_status(status_path, video_duration=full_duration)
            _log(status_path, "clean", "Done", output=paths["clean"])
        else:
            write_status(status_path, video_duration=full_duration)
            print(f"[clean] Skipped (resuming) — using {paths['clean']}")

        # ── Step 3: Transcribe ─────────────────────────────────────────────────
        if not export_only and not os.path.exists(paths["transcript"]):
            _log(status_path, "transcribe", "Transcribing with Deepgram Nova-3...")
            transcript = transcribe(paths["clean"])
            with open(paths["transcript"], "w", encoding="utf-8") as f:
                json.dump(transcript, f)
            _log(status_path, "transcribe",
                 f"Done — {len(transcript['words']):,} words",
                 output=paths["transcript"],
                 word_count=len(transcript["words"]))
        else:
            with open(paths["transcript"], "r", encoding="utf-8") as f:
                transcript = json.load(f)
            print(f"[transcribe] Skipped (resuming) — loaded {len(transcript['words']):,} words")

        # ── Step 4: Profanity filter ───────────────────────────────────────────
        if filter_audio and not export_only:
            if os.path.exists(paths["filtered"]):
                _log(status_path, "filter", "Using cached filtered audio...")
                transcript, censored = censor_transcript(transcript)
                _log(status_path, "filter",
                     f"Done — {len(set(censored))} unique word(s) censored"
                     if censored else "Done — no profanity detected")
            else:
                _log(status_path, "filter", "Applying profanity filter...")
                original_words = list(transcript["words"])
                transcript, censored = censor_transcript(transcript)
                filter_profanity(paths["clean"], original_words, paths["filtered"])
                if censored:
                    _log(status_path, "filter",
                         f"Done — censored {len(censored)} instance(s): {', '.join(set(censored))}")
                else:
                    _log(status_path, "filter", "Done — no profanity detected")
        elif filter_audio and export_only:
            if os.path.exists(paths["filtered"]):
                transcript, _ = censor_transcript(transcript)
                print(f"[filter] Skipped (resuming) — using {paths['filtered']}")
            else:
                _log(status_path, "filter", "Rebuilding filtered audio...")
                original_words = list(transcript["words"])
                transcript, censored = censor_transcript(transcript)
                filter_profanity(paths["clean"], original_words, paths["filtered"])
                _log(status_path, "filter",
                     f"Done — {len(censored)} instance(s) censored" if censored
                     else "Done — no profanity detected")

        audio_path = (
            paths["filtered"]
            if filter_audio and os.path.exists(paths["filtered"])
            else paths["clean"]
        )

        # ── Vertical shorts pipeline (independent of episode pipeline) ─────────
        # When vertical_paths provided, shorts are sourced entirely from the
        # vertical files — same timeline for audio and video, zero H/V drift.
        use_vertical    = False
        clip_transcript = transcript   # overridden below if vertical pipeline runs
        clip_video_src  = paths["stitched"]
        clip_audio_path = audio_path
        v_seg_meta: list[dict] = []    # per-segment {"v_offset", "v_duration"}

        if vertical_paths:
            if len(vertical_paths) != len(segments):
                raise ValueError(
                    f"vertical_paths has {len(vertical_paths)} entries but "
                    f"{len(segments)} segments were detected — must match."
                )

            # V-Step 1: Stitch vertical segments
            if not export_only and not os.path.exists(paths["vertical_stitched"]):
                _log(status_path, "v_stitch",
                     f"Stitching {len(vertical_paths)} vertical segments...")
                _, v_offsets = stitch_segments(vertical_paths, paths["vertical_stitched"])
                _log(status_path, "v_stitch", "Done",
                     output=paths["vertical_stitched"])
            else:
                v_offsets = []
                t = 0.0
                for vp in vertical_paths:
                    v_offsets.append(t)
                    t += get_video_duration(vp)
                print(
                    f"[v_stitch] {'Skipped (resuming)' if export_only else 'Already exists'}"
                    f" — using {paths['vertical_stitched']}"
                )

            for i, seg in enumerate(segments):
                seg["v_offset"]   = v_offsets[i]
                seg["v_duration"] = get_video_duration(vertical_paths[i])

            # V-Step 2: Clean vertical audio
            if not export_only and not os.path.exists(paths["vertical_clean"]):
                _log(status_path, "v_clean",
                     "Enhancing vertical audio with DeepFilterNet3...")
                clean_audio(paths["vertical_stitched"], paths["vertical_clean"])
                _log(status_path, "v_clean", "Done",
                     output=paths["vertical_clean"])
            else:
                print(
                    f"[v_clean] {'Skipped (resuming)' if export_only else 'Already exists'}"
                    f" — using {paths['vertical_clean']}"
                )

            # V-Step 3: Transcribe vertical audio
            if not export_only and not os.path.exists(paths["vertical_transcript"]):
                _log(status_path, "v_transcribe",
                     "Transcribing vertical audio with Deepgram Nova-3...")
                v_transcript = transcribe(paths["vertical_clean"])
                with open(paths["vertical_transcript"], "w", encoding="utf-8") as f:
                    json.dump(v_transcript, f)
                _log(status_path, "v_transcribe",
                     f"Done — {len(v_transcript['words']):,} words",
                     output=paths["vertical_transcript"],
                     word_count=len(v_transcript["words"]))
            else:
                with open(paths["vertical_transcript"], "r", encoding="utf-8") as f:
                    v_transcript = json.load(f)
                print(
                    f"[v_transcribe] {'Skipped (resuming)' if export_only else 'Loaded'}"
                    f" — {len(v_transcript['words']):,} words"
                )

            # V-Step 4: Profanity filter on vertical audio
            if filter_audio and not export_only:
                if os.path.exists(paths["vertical_filtered"]):
                    v_transcript, _ = censor_transcript(v_transcript)
                    print("[v_filter] Using cached vertical filtered audio")
                else:
                    _log(status_path, "v_filter",
                         "Applying profanity filter to vertical audio...")
                    v_original_words = list(v_transcript["words"])
                    v_transcript, v_censored = censor_transcript(v_transcript)
                    filter_profanity(
                        paths["vertical_clean"], v_original_words,
                        paths["vertical_filtered"]
                    )
                    _log(status_path, "v_filter",
                         f"Done — censored {len(v_censored)} instance(s): "
                         f"{', '.join(set(v_censored))}"
                         if v_censored else "Done — no profanity detected")
            elif filter_audio and export_only:
                if os.path.exists(paths["vertical_filtered"]):
                    v_transcript, _ = censor_transcript(v_transcript)
                    print(f"[v_filter] Skipped (resuming) — using {paths['vertical_filtered']}")
                else:
                    _log(status_path, "v_filter",
                         "Rebuilding vertical filtered audio...")
                    v_original_words = list(v_transcript["words"])
                    v_transcript, v_censored = censor_transcript(v_transcript)
                    filter_profanity(
                        paths["vertical_clean"], v_original_words,
                        paths["vertical_filtered"]
                    )
                    _log(status_path, "v_filter",
                         f"Done — {len(v_censored)} instance(s) censored"
                         if v_censored else "Done — no profanity detected")

            v_audio_path = (
                paths["vertical_filtered"]
                if filter_audio and os.path.exists(paths["vertical_filtered"])
                else paths["vertical_clean"]
            )

            use_vertical    = True
            clip_transcript = v_transcript
            clip_video_src  = paths["vertical_stitched"]
            clip_audio_path = v_audio_path

        # ── Step 5: Full episode video export ─────────────────────────────────
        # Skip if file already exists — re-encoding an 82-min episode is slow;
        # delete the file manually if you need to regenerate it.
        if not os.path.exists(paths["episode_video"]):
            _log(status_path, "episode_export",
                 "Exporting full episode for YouTube...")
            from pipeline.export import export_episode_youtube
            export_episode_youtube(paths["stitched"], audio_path,
                                   paths["episode_video"])
            _log(status_path, "episode_export", "Done",
                 output=paths["episode_video"])
        else:
            print(f"[episode_export] Skipped — already exists")

        # ── Step 6: Full episode SRT ───────────────────────────────────────────
        if not os.path.exists(paths["episode_srt"]):
            _log(status_path, "episode_srt", "Building full episode SRT...")
            build_srt(transcript["words"], paths["episode_srt"], start_offset=0.0)
            _log(status_path, "episode_srt", "Done", output=paths["episode_srt"])
        else:
            print(f"[episode_srt] Skipped — already exists")

        # ── Step 7: Podcast MP3 (optional) ────────────────────────────────────
        if generate_mp3:
            if not os.path.exists(paths["episode_mp3"]):
                _log(status_path, "episode_mp3", "Encoding podcast MP3...")
                mp3_result = export_podcast_mp3(
                    audio_path=audio_path,
                    output_path=paths["episode_mp3"],
                    title=episode_title,
                    description=episode_notes,
                    show_name=brand.get("show_name") or show_name,
                    cover_art_path=cover_art_path,
                )
                _log(status_path, "episode_mp3",
                     f"Done — {mp3_result['size_mb']:.1f} MB",
                     output=paths["episode_mp3"],
                     spotify_upload_url=mp3_result["spotify_upload_url"])
            else:
                print(f"[episode_mp3] Skipped — already exists")
        else:
            print(f"[episode_mp3] Skipped — upload video episode to Spotify directly")

        # ── Step 8: Episode descriptions ──────────────────────────────────────
        if not os.path.exists(paths["episode_desc"]):
            _log(status_path, "describe",
                 "Generating episode descriptions with Claude...")
            desc_result = generate_episode_descriptions(
                transcript=transcript,
                episode_title=episode_title,
                episode_notes=episode_notes,
                brand=brand,
            )
            with open(paths["episode_desc"], "w", encoding="utf-8") as f:
                f.write(desc_result["youtube_full"])
            shownotes_lines = []
            if desc_result["title_options"]:
                shownotes_lines.append("TITLE OPTIONS:")
                for i, t in enumerate(desc_result["title_options"], 1):
                    shownotes_lines.append(f"  {i}. {t}")
                shownotes_lines.append("")
            shownotes_lines.append(desc_result["youtube_full"])
            with open(paths["episode_notes"], "w", encoding="utf-8") as f:
                f.write("\n".join(shownotes_lines))
            _log(status_path, "describe", "Done",
                 episode_desc=paths["episode_desc"],
                 episode_notes=paths["episode_notes"])
        else:
            print(f"[describe] Skipped — already exists")

        # ── Steps 9–11: Per-segment clip finding and export ────────────────────
        all_exported: list[dict] = []
        clip_counter = 0

        for seg in segments:
            seg_label  = seg["label"]
            seg_slug   = clip_slug(seg_label)
            max_clips  = seg["max_clips"]

            # Use vertical offsets/durations when running the vertical pipeline
            if use_vertical:
                seg_offset = seg["v_offset"]
                seg_dur    = seg["v_duration"]
            else:
                seg_offset = seg["offset"]
                seg_dur    = seg["duration"]
            seg_end = seg_offset + seg_dur

            print(f"\n[{seg_label}] Finding clips (max {max_clips})...")

            # Words within this segment's time window (in clip_transcript timeline)
            seg_words = [
                w for w in clip_transcript["words"]
                if seg_offset <= w.get("start", 0) <= seg_end
            ]
            if not seg_words:
                print(f"  [SKIP] No transcript words found in [{seg_offset:.0f}s–{seg_end:.0f}s]")
                continue

            # Normalize word timestamps to segment-relative.
            # find_clips() clamps to seg_dur, so absolute timestamps would be dropped.
            norm_words = [
                {**w, "start": w["start"] - seg_offset,
                       "end":   w["end"]   - seg_offset}
                for w in seg_words
            ]
            seg_transcript = {
                "text":  " ".join(w["word"] for w in norm_words),
                "words": norm_words,
            }

            if not export_only:
                clips_relative = find_clips(
                    seg_transcript,
                    seg_dur,
                    producer_context=producer_context,
                    min_clip_secs=min_clip,
                    max_clip_secs=max_clip,
                )
                # Translate back to absolute clip_transcript timeline
                clips = [
                    {**c,
                     "start_time": c["start_time"] + seg_offset,
                     "end_time":   c["end_time"]   + seg_offset}
                    for c in clips_relative
                ]
                clips = clips[:max_clips]
                _log(status_path, f"suggest_{seg_slug}",
                     f"Done — {len(clips)} clip(s)",
                     clips=clips)
            else:
                saved = read_status(status_path).get(f"suggest_{seg_slug}", {})
                clips = saved.get("clips", [])
                print(f"  Skipped (resuming) — loaded {len(clips)} clip(s)")

            for clip in clips:
                clip_counter += 1
                i = clip_counter

                start       = float(clip["start_time"])
                end         = _snap_to_sentence_end(
                    float(clip["end_time"]), clip_transcript["words"]
                )
                title_str   = clip.get("title", f"clip_{i}")
                description = (
                    f"{seg_label} | {chyron_suffix}"
                    if chyron_suffix
                    else clip.get("description", "")
                )
                slug        = f"{seg_slug}__{clip_slug(title_str)}"

                _log(status_path, f"export_{i}",
                     f"Exporting [{seg_label}]: {title_str} ({end-start:.0f}s)")

                clip_words = [
                    w for w in clip_transcript["words"]
                    if start <= w.get("start", 0) <= end
                ]

                clip_files: dict[str, str] = {}

                if export_format in ("social", "both"):
                    social_path = os.path.join(paths["clips"], f"{slug}_social.mp4")
                    ass_path    = os.path.join(paths["clips"], f"{slug}.ass")
                    build_karaoke_ass(clip_words, style, ass_path,
                                      start_offset=start)
                    # Vertical source is already portrait; audio and video come
                    # from the same file — zero drift, no layout transform.
                    export_clip(
                        clip_video_src, clip_audio_path, ass_path,
                        start, end, social_path, description,
                    )
                    clip_files["social"] = social_path

                if export_format in ("youtube", "both"):
                    yt_path  = os.path.join(paths["clips"], f"{slug}_youtube.mp4")
                    srt_path = os.path.join(paths["clips"], f"{slug}.srt")
                    export_clip_clean(
                        clip_video_src, clip_audio_path,
                        start, end, yt_path,
                    )
                    build_srt(clip_words, srt_path, start_offset=start)
                    clip_files["youtube"] = yt_path
                    clip_files["srt"]     = srt_path

                # Generate platform descriptions
                try:
                    episode_context = (
                        f"{brand.get('show_name') or show_name} — "
                        f"{episode_title} — {seg_label}"
                    )
                    clip_desc = generate_clip_descriptions(
                        clip_words=clip_words,
                        clip_title=title_str,
                        episode_context=episode_context,
                        brand=brand,
                    )
                    desc_path = os.path.join(paths["clips"], f"{slug}_descriptions.json")
                    with open(desc_path, "w", encoding="utf-8") as f:
                        json.dump(clip_desc, f, indent=2)
                    clip_files["descriptions"] = desc_path
                except Exception as e:
                    print(f"  [clip_desc] Warning: {e}")

                # QA
                qa_issues, qa_warnings = validate_clip(
                    clip_path        = clip_files.get("social") or clip_files.get("youtube", ""),
                    start            = start,
                    end              = end,
                    title            = title_str,
                    transcript_words = clip_transcript["words"],
                )
                print(format_validation(qa_issues, qa_warnings, title_str))

                all_exported.append({
                    "title":       title_str,
                    "slug":        slug,
                    "segment":     seg_label,
                    **clip_files,
                    "qa_issues":   qa_issues,
                    "qa_warnings": qa_warnings,
                })
                _log(status_path, f"export_{i}", "Done",
                     files=clip_files,
                     segment=seg_label,
                     qa_issues=qa_issues,
                     qa_warnings=qa_warnings)

        # ── Step 12: Per-segment standalone YouTube exports ────────────────────
        # Each segment is exported as its own clean video + SRT for individual
        # YouTube upload alongside the full stitched episode.
        #
        # When vertical source is available, both orientations are produced:
        #   {seg_slug}_youtube.mp4            — vertical 9:16 (from vertical_stitched)
        #   {seg_slug}.srt
        #   {seg_slug}_horizontal_youtube.mp4 — horizontal 16:9 (from stitched)
        #   {seg_slug}_horizontal.srt
        #
        # When no vertical source, only horizontal is produced as {seg_slug}_youtube.mp4.
        # Files are skipped individually if they already exist, so re-runs are safe.
        print(f"\n[segments] Exporting {len(segments)} standalone segment videos...")

        for seg in segments:
            seg_label    = seg["label"]
            seg_slug_str = clip_slug(seg_label)

            # ── Vertical version (only when vertical pipeline ran) ─────────
            if use_vertical:
                v_start = seg["v_offset"]
                v_end   = seg["v_offset"] + seg["v_duration"]
                v_yt    = os.path.join(paths["segments_dir"], f"{seg_slug_str}_youtube.mp4")
                v_srt   = os.path.join(paths["segments_dir"], f"{seg_slug_str}.srt")

                if os.path.exists(v_yt):
                    print(f"  [{seg_label}] Vertical — skipped (exists)")
                else:
                    _log(status_path, f"seg_v_{seg_slug_str}",
                         f"Exporting vertical segment: {seg_label} ({v_end - v_start:.0f}s)")
                    export_clip_clean(clip_video_src, clip_audio_path, v_start, v_end, v_yt)
                    v_words = [
                        w for w in clip_transcript["words"]
                        if v_start <= w.get("start", 0) <= v_end
                    ]
                    build_srt(v_words, v_srt, start_offset=v_start)
                    _log(status_path, f"seg_v_{seg_slug_str}", "Done", video=v_yt, srt=v_srt)

            # ── Horizontal version (always produced) ──────────────────────
            h_start  = seg["offset"]
            h_end    = seg["offset"] + seg["duration"]
            h_suffix = "_horizontal" if use_vertical else ""
            h_yt     = os.path.join(paths["segments_dir"], f"{seg_slug_str}{h_suffix}_youtube.mp4")
            h_srt    = os.path.join(paths["segments_dir"], f"{seg_slug_str}{h_suffix}.srt")

            if os.path.exists(h_yt):
                print(f"  [{seg_label}] Horizontal — skipped (exists)")
            else:
                _log(status_path, f"seg_h_{seg_slug_str}",
                     f"Exporting horizontal segment: {seg_label} ({h_end - h_start:.0f}s)")
                export_clip_clean(paths["stitched"], audio_path, h_start, h_end, h_yt)
                h_words = [
                    w for w in transcript["words"]
                    if h_start <= w.get("start", 0) <= h_end
                ]
                build_srt(h_words, h_srt, start_offset=h_start)
                _log(status_path, f"seg_h_{seg_slug_str}", "Done", video=h_yt, srt=h_srt)

        # ── Done ───────────────────────────────────────────────────────────────
        qa_failures = sum(1 for c in all_exported if c.get("qa_issues"))
        spotify_url = "https://podcasters.spotify.com/pod/dashboard/episodes/new"
        write_status(status_path,
            state="complete",
            finished=datetime.now().isoformat(),
            exported_clips=all_exported,
            qa_summary={
                "total":  len(all_exported),
                "passed": len(all_exported) - qa_failures,
                "failed": qa_failures,
            },
            spotify_upload_url=spotify_url,
        )
        print(f"\n{'='*60}")
        print(f"  Shorts season pipeline complete!")
        print(f"  Full episode : {paths['episode_video']}")
        orientations = "vertical + horizontal" if use_vertical else "horizontal"
        print(f"  Segments     : {paths['segments_dir']} ({orientations})")
        if generate_mp3:
            print(f"  Podcast MP3  : {paths['episode_mp3']}")
        print(f"  {len(all_exported)} clip(s) across {len(segments)} segments")
        if qa_failures:
            print(f"  QA: {qa_failures} clip(s) have issues — review before scheduling")
        else:
            print(f"  QA: all clips passed")
        print(f"\n  >> Upload episode video to Spotify: {spotify_url}")
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
    parser.add_argument("--date",        default="",
                        help="Override run date (YYYY-MM-DD) to resume a previous episode folder")
    parser.add_argument("--no-filter",   action="store_true",
                        help="Skip profanity filter (keep raw audio and transcript)")

    # ── Broadcast pipeline (StreamYard dual-output) ────────────────────────────
    parser.add_argument("--broadcast",      action="store_true",
                        help="Run the StreamYard dual-output broadcast pipeline")
    parser.add_argument("--horizontal",     help="16:9 StreamYard horizontal MP4 path")
    parser.add_argument("--vertical",       help="9:16 StreamYard vertical MP4 path")
    parser.add_argument("--local-recordings", nargs="+",
                        help="Optional per-participant local recordings to compose")
    parser.add_argument("--episode-title",  default="",
                        help="Episode title for descriptions and metadata")
    parser.add_argument("--episode-notes",  default="",
                        help="Producer notes / themes to guide Claude descriptions")
    parser.add_argument("--cover-art",      default="",
                        help="Path to cover art image for podcast MP3 ID3 APIC tag")
    parser.add_argument("--channel",        default="",
                        help="Publishing channel, e.g. 'neilbound' or 'ilb'. "
                             "Empty → use the active profile's pipeline.default_channel.")
    parser.add_argument("--generate-mp3",   action="store_true",
                        help="Generate a podcast MP3 in addition to the video episode (off by default — upload video to Spotify directly)")

    # ── Shorts season pipeline ─────────────────────────────────────────────────
    parser.add_argument("--shorts-season",  action="store_true",
                        help="Run the shorts-season pipeline (multiple segments → full episode + clips)")
    parser.add_argument("--segments-dir",   default="",
                        help="Directory containing StreamYard dual-output segment pairs")
    parser.add_argument("--group",          default="",
                        help="Output folder grouping, e.g. 'age_of_attraction_s1'")
    parser.add_argument("--intro-max-clips",    type=int, default=1,
                        help="Max clips from the first (intro) segment (default: 1)")
    parser.add_argument("--default-max-clips",  type=int, default=3,
                        help="Max clips from each non-intro segment (default: 3)")
    parser.add_argument("--vertical-paths",     default="",
                        help="JSON array of 9:16 vertical MP4 paths (ordered to match detected segments)")
    parser.add_argument("--chyron-suffix",      default="",
                        help="Fixed suffix for clip chyron bar, e.g. 'Age of Attraction S1'. Chyron becomes 'Segment | suffix'.")

    args = parser.parse_args()

    if args.broadcast:
        if not args.horizontal:
            parser.error("--broadcast requires --horizontal")
        if not args.vertical:
            parser.error("--broadcast requires --vertical")
        run_broadcast(
            episode_id=args.episode,
            horizontal_path=args.horizontal,
            vertical_path=args.vertical,
            episode_title=args.episode_title,
            episode_notes=args.episode_notes,
            show_name=args.show,
            local_recordings=args.local_recordings,
            min_clip=args.min_clip,
            max_clip=args.max_clip,
            export_format=args.format,
            filter_audio=not args.no_filter,
            run_date=args.date,
            skip_to_clips=args.clips_only,
            export_only=args.export_only,
            cover_art_path=args.cover_art or None,
            channel=args.channel,
            generate_mp3=args.generate_mp3,
        )
    elif args.shorts_season:
        if not args.segments_dir:
            parser.error("--shorts-season requires --segments-dir")
        v_paths = json.loads(args.vertical_paths) if args.vertical_paths else None
        run_shorts_season(
            episode_id=args.episode,
            segments_dir=args.segments_dir,
            episode_title=args.episode_title,
            episode_notes=args.episode_notes,
            show_name=args.show,
            group=args.group,
            intro_max_clips=args.intro_max_clips,
            default_max_clips=args.default_max_clips,
            min_clip=args.min_clip,
            max_clip=args.max_clip,
            export_format=args.format,
            filter_audio=not args.no_filter,
            run_date=args.date,
            export_only=args.export_only,
            cover_art_path=args.cover_art or None,
            channel=args.channel,
            generate_mp3=args.generate_mp3,
            vertical_paths=v_paths,
            chyron_suffix=args.chyron_suffix,
        )
    else:
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
            run_date=args.date,
            filter_audio=not args.no_filter,
        )
