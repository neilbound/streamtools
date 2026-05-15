"""
pipeline/publish_queue.py — JSON-backed publish queue for scheduled social media posts.

Queue file: output/publish_queue.json (created automatically on first use).

Each entry tracks a clip's publish state across one or more platforms. The daemon
(publisher_daemon.py) processes due entries every 15 minutes via Windows Task Scheduler.

Queue entry shape:
{
    "post_id":        "a1b2c3d4",
    "clip_path":      "C:\\...\\output\\clip_social.mp4",
    "platforms":      ["youtube", "tiktok", "instagram"],
    "title":          "Episode title",
    "description":    "Episode description",
    "tags":           ["podcast", "shorts"],
    "scheduled_time": "2026-05-16T15:00:00+00:00",
    "status":         "pending",   # pending | partial | complete | failed | cancelled
    "results":        {}           # keyed by platform once processed
}
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

# Absolute path to the queue file — output/ is created by mcp_server.py and run_pipeline.py
_QUEUE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),  # streamtools root
    "output",
    "publish_queue.json",
)


# ── Internal helpers ────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    """Load the queue from disk. Returns an empty list if the file does not exist."""
    if not os.path.exists(_QUEUE_PATH):
        return []
    with open(_QUEUE_PATH, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            # Corrupt file — start fresh (existing items are lost; operator should investigate)
            return []
    return data if isinstance(data, list) else []


def _save(queue: list[dict]) -> None:
    """Atomically save the queue to disk."""
    os.makedirs(os.path.dirname(_QUEUE_PATH), exist_ok=True)
    tmp_path = _QUEUE_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(queue, f, indent=2)
    # Replace is atomic on Windows (moves the tmp file over the target)
    os.replace(tmp_path, _QUEUE_PATH)


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


# ── Public API ──────────────────────────────────────────────────────────────────

def enqueue(
    clip_path: str,
    platforms: list[str],
    title: str,
    description: str,
    scheduled_time_iso: str,
    tags: Optional[list[str]] = None,
) -> str:
    """
    Add a new post to the publish queue.

    Args:
        clip_path:          Absolute path to the exported MP4 clip.
        platforms:          List of platforms: any of "youtube", "tiktok", "instagram".
        title:              Post title / caption.
        description:        Longer description (used by YouTube; optional for others).
        scheduled_time_iso: ISO 8601 UTC datetime string, e.g. "2026-05-16T15:00:00+00:00".
        tags:               Optional list of hashtag strings (without '#').

    Returns:
        post_id (8-character UUID prefix) for use with mark_complete / mark_failed / cancel.
    """
    post_id = uuid.uuid4().hex[:8]

    # Normalise scheduled_time to include UTC offset
    dt = datetime.fromisoformat(scheduled_time_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    scheduled_time_str = dt.isoformat()

    entry = {
        "post_id":        post_id,
        "clip_path":      clip_path,
        "platforms":      platforms,
        "title":          title,
        "description":    description,
        "tags":           tags or [],
        "scheduled_time": scheduled_time_str,
        "status":         "pending",
        "results":        {},
    }

    queue = _load()
    queue.append(entry)
    _save(queue)

    return post_id


def get_due(now: Optional[datetime] = None) -> list[dict]:
    """
    Return all pending queue entries whose scheduled_time is at or before `now`.

    Args:
        now: Comparison datetime (UTC). Defaults to the current UTC time.

    Returns:
        List of queue entry dicts (not copies — mutate then call mark_complete/mark_failed).
    """
    now = now or _now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    queue = _load()
    due = []
    for entry in queue:
        if entry.get("status") != "pending":
            continue
        try:
            sched = datetime.fromisoformat(entry["scheduled_time"])
            if sched.tzinfo is None:
                sched = sched.replace(tzinfo=timezone.utc)
        except (KeyError, ValueError):
            continue  # Skip malformed entries
        if sched <= now:
            due.append(entry)
    return due


def mark_complete(post_id: str, platform: str, result: dict) -> None:
    """
    Record a successful upload result for a specific platform.

    Marks the overall entry 'complete' once all platforms have a result.
    Marks it 'partial' if some platforms are still pending.

    Args:
        post_id:  The 8-character post identifier.
        platform: Platform key, e.g. "youtube".
        result:   The dict returned by the upload function.
    """
    queue = _load()
    for entry in queue:
        if entry["post_id"] != post_id:
            continue
        entry["results"][platform] = {"status": "ok", **result}
        # Determine overall status
        completed = {p for p, r in entry["results"].items() if r.get("status") == "ok"}
        all_platforms = set(entry.get("platforms", []))
        if all_platforms and completed >= all_platforms:
            entry["status"] = "complete"
        else:
            entry["status"] = "partial"
        break
    _save(queue)


def mark_failed(post_id: str, platform: str, error: str) -> None:
    """
    Record a failed upload for a specific platform.

    The overall entry status is set to 'failed' unless other platforms succeeded
    (in which case it becomes 'partial').

    Args:
        post_id:  The 8-character post identifier.
        platform: Platform key, e.g. "tiktok".
        error:    Human-readable error message or exception string.
    """
    queue = _load()
    for entry in queue:
        if entry["post_id"] != post_id:
            continue
        entry["results"][platform] = {"status": "error", "error": error}
        # At least one platform failed — but others may have succeeded
        any_ok    = any(r.get("status") == "ok"    for r in entry["results"].values())
        any_error = any(r.get("status") == "error" for r in entry["results"].values())
        if any_ok and any_error:
            entry["status"] = "partial"
        elif any_error and not any_ok:
            entry["status"] = "failed"
        break
    _save(queue)


def list_all() -> list[dict]:
    """
    Return all entries in the queue, sorted by scheduled_time ascending.
    """
    queue = _load()
    try:
        queue.sort(key=lambda e: e.get("scheduled_time", ""))
    except Exception:
        pass
    return queue


def cancel(post_id: str) -> bool:
    """
    Cancel a pending queue entry by post_id.

    Only pending entries can be cancelled. Returns True if cancelled, False otherwise
    (e.g. already complete, failed, or not found).

    Args:
        post_id: The 8-character post identifier.

    Returns:
        True if the entry was successfully cancelled.
    """
    queue = _load()
    for entry in queue:
        if entry["post_id"] == post_id:
            if entry.get("status") == "pending":
                entry["status"] = "cancelled"
                _save(queue)
                return True
            else:
                return False  # Cannot cancel a non-pending entry
    return False  # Not found
