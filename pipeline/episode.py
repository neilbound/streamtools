"""
Episode naming conventions and directory structure for streamtools.

Directory layout for a processed episode:
  output/{show_slug}_{episode_id}_{YYYY-MM-DD}/
    portrait.mp4                  ← composed portrait (if built from local recordings)
    clean.wav                     ← DeepFilterNet3 enhanced audio
    transcript.json               ← Deepgram word-level transcript
    pipeline_status.json          ← progress log written throughout the run
    clips/
      {clip_slug}_social.mp4      ← burned-in captions
      {clip_slug}_youtube.mp4     ← clean video for YouTube upload
      {clip_slug}.srt             ← captions file for YouTube

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


def episode_dir(show_name: str, episode_id: str, run_date: str = "") -> str:
    """
    Return the canonical output directory path for an episode.

    Args:
        show_name:  Display name of the show (e.g. "Is Love Blind").
        episode_id: Short label provided by the user (e.g. "s11e01" or "men_accountability").
        run_date:   ISO date string (YYYY-MM-DD). Defaults to today.

    Returns:
        Absolute path to the episode directory (not yet created).
    """
    if not run_date:
        run_date = date.today().isoformat()
    folder = f"{slugify(show_name)}_{slugify(episode_id)}_{run_date}"
    return os.path.join(OUTPUT_ROOT, folder)


def clip_slug(title: str) -> str:
    """Sanitise a clip title for use in filenames."""
    return slugify(title)[:60]  # cap at 60 chars to avoid filesystem limits


def ensure_episode_dirs(ep_dir: str) -> dict[str, str]:
    """
    Create the episode directory tree and return a dict of key paths.

    Returns:
        {
            "root":      ep_dir,
            "clips":     ep_dir/clips,
            "portrait":  ep_dir/portrait.mp4,
            "clean":     ep_dir/clean.wav,
            "transcript": ep_dir/transcript.json,
            "status":    ep_dir/pipeline_status.json,
        }
    """
    clips_dir = os.path.join(ep_dir, "clips")
    os.makedirs(clips_dir, exist_ok=True)

    return {
        "root":       ep_dir,
        "clips":      clips_dir,
        "portrait":   os.path.join(ep_dir, "portrait.mp4"),
        "clean":      os.path.join(ep_dir, "clean.wav"),
        "filtered":   os.path.join(ep_dir, "filtered.wav"),
        "transcript": os.path.join(ep_dir, "transcript.json"),
        "status":     os.path.join(ep_dir, "pipeline_status.json"),
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
