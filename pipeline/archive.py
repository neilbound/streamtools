"""
pipeline/archive.py — move successfully-posted episode deliverables to a cold-storage
archive folder (e.g. a Google Drive for Desktop synced folder), freeing local disk.

Design (per the operator's choices):
  - Deliverables only: the clips/, segments/, and episode/ subfolders (MP4s, SRTs,
    .ass, descriptions). Big regenerable intermediates (stitched.mp4,
    vertical_stitched.mp4, *.wav, transcripts) are NOT archived and stay local.
  - Per-episode sweep: an episode is archivable once every one of its queued clips
    has a successful YouTube upload (the durable home; TikTok/IG are ephemeral).
  - Move once verified: each file is copied to the archive, its size verified at the
    destination, and only after ALL files verify are the local originals deleted.
    So a failed copy never leaves a half-archived episode with deleted originals.

Archive root resolution: explicit arg > env STREAMTOOLS_ARCHIVE_ROOT. Point it at the
Drive for Desktop synced folder once installed; Drive uploads anything placed there.
"""

import json
import os
import shutil
from datetime import datetime, timezone

from pipeline.publish_queue import list_all

# Subdirectories that hold deliverables (everything else in the episode dir stays local)
_DELIVERABLE_SUBDIRS = ("clips", "segments", "episode")
_ARCHIVE_MARKER = "ARCHIVED.json"


def resolve_archive_root(archive_root: str = "") -> str:
    """Resolve the archive root from the arg or STREAMTOOLS_ARCHIVE_ROOT env var."""
    root = archive_root or os.environ.get("STREAMTOOLS_ARCHIVE_ROOT", "")
    return root.strip()


def _episode_dir_of(clip_path: str) -> str:
    """Episode dir = parent of the clips/segments/episode subdir a deliverable lives in."""
    # .../<episode_dir>/<subdir>/<file>  ->  <episode_dir>
    return os.path.dirname(os.path.dirname(clip_path))


def find_archivable_episodes() -> list[str]:
    """
    Return episode directories where EVERY queued clip has a successful YouTube upload
    and which are not already archived. These are safe to move to cold storage.
    """
    by_episode: dict[str, list[dict]] = {}
    for e in list_all():
        cp = e.get("clip_path", "")
        if not cp:
            continue
        ep = _episode_dir_of(cp)
        by_episode.setdefault(ep, []).append(e)

    ready = []
    for ep, entries in by_episode.items():
        if not os.path.isdir(ep):
            continue
        if os.path.exists(os.path.join(ep, _ARCHIVE_MARKER)):
            continue  # already archived
        # Every queued clip for this episode must have YouTube confirmed.
        if all(e.get("results", {}).get("youtube", {}).get("status") == "ok"
               for e in entries):
            ready.append(ep)
    return sorted(ready)


def episode_deliverables(episode_dir: str) -> list[str]:
    """Absolute paths of every deliverable file under the episode's clips/segments/episode dirs."""
    files = []
    for sub in _DELIVERABLE_SUBDIRS:
        d = os.path.join(episode_dir, sub)
        if not os.path.isdir(d):
            continue
        for root, _dirs, names in os.walk(d):
            for n in names:
                files.append(os.path.join(root, n))
    return sorted(files)


def archive_episode(episode_dir: str, archive_root: str = "", dry_run: bool = False) -> dict:
    """
    Copy an episode's deliverables to {archive_root}/{episode_name}/, verify each by
    size, then (only if all verified) delete the local originals. Writes an ARCHIVED.json
    marker into the episode dir. Returns a report dict.

    Raises ValueError if the archive root is unset or missing.
    """
    root = resolve_archive_root(archive_root)
    if not root:
        raise ValueError(
            "Archive root not set. Pass archive_root or set STREAMTOOLS_ARCHIVE_ROOT "
            "to your Google Drive for Desktop synced folder."
        )
    if not dry_run and not os.path.isdir(root):
        raise ValueError(f"Archive root does not exist: {root}")

    episode_name = os.path.basename(episode_dir.rstrip(os.sep))
    dest_base = os.path.join(root, episode_name)
    deliverables = episode_deliverables(episode_dir)

    report = {
        "episode": episode_name,
        "dest": dest_base,
        "files": len(deliverables),
        "bytes": sum(os.path.getsize(f) for f in deliverables),
        "copied": 0,
        "deleted": 0,
        "dry_run": dry_run,
    }
    if dry_run or not deliverables:
        return report

    # ── Phase 1: copy + verify every file (no deletions yet) ──────────────────
    copied = []  # (src, dst)
    for src in deliverables:
        rel = os.path.relpath(src, episode_dir)
        dst = os.path.join(dest_base, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
        if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
            raise IOError(
                f"Verification failed copying {src} -> {dst} "
                f"(size mismatch). No local files deleted."
            )
        copied.append((src, dst))
    report["copied"] = len(copied)

    # ── Phase 2: all verified — now delete local originals ────────────────────
    for src, _dst in copied:
        os.remove(src)
        report["deleted"] += 1
    # remove now-empty deliverable subdirs
    for sub in _DELIVERABLE_SUBDIRS:
        d = os.path.join(episode_dir, sub)
        if os.path.isdir(d) and not os.listdir(d):
            os.rmdir(d)

    # ── Marker so the sweep never re-archives this episode ────────────────────
    with open(os.path.join(episode_dir, _ARCHIVE_MARKER), "w", encoding="utf-8") as f:
        json.dump({
            "archived_at": datetime.now(timezone.utc).isoformat(),
            "archive_dest": dest_base,
            "files": report["copied"],
            "bytes": report["bytes"],
        }, f, indent=2)
    return report


def archive_ready_episodes(archive_root: str = "", dry_run: bool = False) -> list[dict]:
    """Sweep: archive every episode whose queued clips are all YouTube-confirmed."""
    reports = []
    for ep in find_archivable_episodes():
        reports.append(archive_episode(ep, archive_root=archive_root, dry_run=dry_run))
    return reports
