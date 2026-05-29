"""
Episode naming conventions and directory structure for streamtools.

Directory layout for a processed shorts-season episode:
  output/{group}/{show_slug}_{episode_id}_{YYYY-MM-DD}/
    stitched.mp4                  ← horizontal 16:9 full-episode stitch
    vertical_stitched.mp4         ← vertical 9:16 shorts stitch (when vertical_paths provided)
    clean.wav / filtered.wav      ← DeepFilterNet3 enhanced audio (horizontal)
    vertical_clean.wav / vertical_filtered.wav   ← enhanced audio (vertical)
    transcript.json               ← Deepgram word-level transcript (horizontal timeline)
    vertical_transcript.json      ← word-level transcript (vertical timeline)
    pipeline_status.json          ← progress log written throughout the run
    segment_manifest.json         ← segment labels, offsets, durations
    episode/
      {slug}_youtube.mp4          ← full stitched episode (16:9) for YouTube
      {slug}.srt                  ← full episode captions
      {slug}.mp3                  ← podcast audio (optional)
      {slug}_description.txt      ← Claude YouTube description
      {slug}_shownotes.txt        ← Claude show notes for Spotify
    clips/
      {seg}__{clip_slug}_social.mp4    ← 9:16 with burned-in karaoke captions
      {seg}__{clip_slug}_youtube.mp4   ← 9:16 clean (vertical source)
      {seg}__{clip_slug}.srt
      {seg}__{clip_slug}_descriptions.json
    segments/
      {seg_slug}_youtube.mp4           ← per-segment 9:16 (vertical source)
      {seg_slug}.srt
      {seg_slug}_horizontal_youtube.mp4  ← per-segment 16:9 (horizontal source)
      {seg_slug}_horizontal.srt

Slug rules:
  - Lowercase
  - Spaces and non-alphanumeric characters → underscore
  - Consecutive underscores collapsed to one
  - Leading/trailing underscores stripped
"""

import json
import os
import re
from datetime import date


OUTPUT_ROOT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "output")


def slugify(text: str) -> str:
    """Convert arbitrary text to a safe filename slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "_", text)
    return text.strip("_")


def episode_dir(show_name: str, episode_id: str, run_date: str = "",
                group: str = "") -> str:
    """
    Return the canonical output directory path for an episode.

    Args:
        show_name:  Display name of the show (e.g. "Is Love Blind").
        episode_id: Short label provided by the user (e.g. "s11e01" or "men_accountability").
        run_date:   ISO date string (YYYY-MM-DD). Defaults to today.
        group:      Optional grouping folder (e.g. "age_of_attraction_s1").
                    When provided, the path becomes output/{group}/{show_slug}_{id}_{date}/
                    so all episodes in a season live under one parent directory.

    Returns:
        Absolute path to the episode directory (not yet created).
    """
    if not run_date:
        run_date = date.today().isoformat()
    folder = f"{slugify(show_name)}_{slugify(episode_id)}_{run_date}"
    if group:
        return os.path.join(OUTPUT_ROOT, slugify(group), folder)
    return os.path.join(OUTPUT_ROOT, folder)


def clip_slug(title: str) -> str:
    """Sanitise a clip title for use in filenames."""
    return slugify(title)[:60]  # cap at 60 chars to avoid filesystem limits


def ensure_episode_dirs(ep_dir: str) -> dict[str, str]:
    """
    Create the episode directory tree and return a dict of key paths.

    Returns:
        {
            "root":          ep_dir,
            "clips":         ep_dir/clips,
            "episode_dir":   ep_dir/episode,
            "segments_dir":  ep_dir/segments,
            "portrait":      ep_dir/portrait.mp4,
            "stitched":      ep_dir/stitched.mp4,
            "clean":         ep_dir/clean.wav,
            "filtered":      ep_dir/filtered.wav,
            "transcript":    ep_dir/transcript.json,
            "status":        ep_dir/pipeline_status.json,
            "segment_manifest": ep_dir/segment_manifest.json,
            # Vertical-source shorts pipeline
            "vertical_stitched":   ep_dir/vertical_stitched.mp4,
            "vertical_clean":      ep_dir/vertical_clean.wav,
            "vertical_filtered":   ep_dir/vertical_filtered.wav,
            "vertical_transcript": ep_dir/vertical_transcript.json,
            # Full-episode outputs (slug = basename of ep_dir)
            "episode_video": ep_dir/episode/{slug}_youtube.mp4,
            "episode_srt":   ep_dir/episode/{slug}.srt,
            "episode_mp3":   ep_dir/episode/{slug}.mp3,
            "episode_desc":  ep_dir/episode/{slug}_description.txt,
            "episode_notes": ep_dir/episode/{slug}_shownotes.txt,
        }
    """
    clips_dir    = os.path.join(ep_dir, "clips")
    episode_sub  = os.path.join(ep_dir, "episode")
    segments_sub = os.path.join(ep_dir, "segments")
    os.makedirs(clips_dir,    exist_ok=True)
    os.makedirs(episode_sub,  exist_ok=True)
    os.makedirs(segments_sub, exist_ok=True)

    slug = os.path.basename(ep_dir)

    return {
        "root":          ep_dir,
        "clips":         clips_dir,
        "episode_dir":   episode_sub,
        "segments_dir":  segments_sub,
        "portrait":      os.path.join(ep_dir, "portrait.mp4"),
        "stitched":      os.path.join(ep_dir, "stitched.mp4"),   # shorts-season stitch
        "clean":         os.path.join(ep_dir, "clean.wav"),
        "filtered":      os.path.join(ep_dir, "filtered.wav"),
        "transcript":    os.path.join(ep_dir, "transcript.json"),
        "status":        os.path.join(ep_dir, "pipeline_status.json"),
        "segment_manifest": os.path.join(ep_dir, "segment_manifest.json"),
        # Vertical-source shorts pipeline (separate from horizontal episode pipeline)
        "vertical_stitched":   os.path.join(ep_dir, "vertical_stitched.mp4"),
        "vertical_clean":      os.path.join(ep_dir, "vertical_clean.wav"),
        "vertical_filtered":   os.path.join(ep_dir, "vertical_filtered.wav"),
        "vertical_transcript": os.path.join(ep_dir, "vertical_transcript.json"),
        # Full-episode outputs (broadcast / shorts-season pipeline)
        "episode_video": os.path.join(episode_sub, f"{slug}_youtube.mp4"),
        "episode_srt":   os.path.join(episode_sub, f"{slug}.srt"),
        "episode_mp3":   os.path.join(episode_sub, f"{slug}.mp3"),
        "episode_desc":  os.path.join(episode_sub, f"{slug}_description.txt"),
        "episode_notes": os.path.join(episode_sub, f"{slug}_shownotes.txt"),
    }


# ── Status file helpers ────────────────────────────────────────────────────────

def _read_status(status_path: str) -> dict:
    if os.path.exists(status_path):
        with open(status_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def write_status(status_path: str, **kwargs) -> None:
    """Merge kwargs into the status JSON and write it atomically."""
    status = _read_status(status_path)
    status.update(kwargs)
    tmp = status_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    os.replace(tmp, status_path)


def read_status(status_path: str) -> dict:
    """Read and return the current pipeline status."""
    return _read_status(status_path)
