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
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from filelock import FileLock

# Absolute path to the queue file — output/ is created by mcp_server.py and run_pipeline.py
_QUEUE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),  # streamtools root
    "output",
    "publish_queue.json",
)

# Cross-process lock so the publisher daemon and MCP tools never interleave a
# read-modify-write on the queue file (which would silently drop entries).
_LOCK_PATH = _QUEUE_PATH + ".lock"
_LOCK_TIMEOUT = 30  # seconds — fail loudly rather than hang forever


@contextmanager
def _queue_lock():
    """Acquire the cross-process queue lock for the duration of the block."""
    os.makedirs(os.path.dirname(_LOCK_PATH), exist_ok=True)
    lock = FileLock(_LOCK_PATH, timeout=_LOCK_TIMEOUT)
    with lock:
        yield


# ── Internal helpers ────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    """Load the queue from disk. Returns an empty list if the file does not exist."""
    if not os.path.exists(_QUEUE_PATH):
        return []
    with open(_QUEUE_PATH, "r", encoding="utf-8") as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError:
            # Corrupt file — back it up before starting fresh so entries can be recovered
            import shutil, time as _t
            backup = _QUEUE_PATH + f".corrupt.{int(_t.time())}"
            try:
                shutil.copy2(_QUEUE_PATH, backup)
                print(
                    f"[publish_queue] CRITICAL: queue file is corrupt.\n"
                    f"  Backed up to: {backup}\n"
                    f"  Starting with empty queue. Inspect the backup to recover any pending entries."
                )
            except Exception:
                print(
                    "[publish_queue] CRITICAL: queue file is corrupt and backup failed.\n"
                    "  Starting with empty queue."
                )
            return []
    return data if isinstance(data, list) else []


def _strip_md(text: str) -> str:
    """Strip markdown bold/italic markers from caption text."""
    lines = [l for l in text.split("\n") if l.strip() not in ("**", "*", "***")]
    text = "\n".join(lines)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*(.+?)\*",     r"\1", text, flags=re.DOTALL)
    text = re.sub(r"^\*+\s*",       "",    text, flags=re.MULTILINE)
    text = re.sub(r"\s*\*+$",       "",    text, flags=re.MULTILINE)
    return text.strip()


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
    channel: str = "neilbound",
    extra: Optional[dict] = None,
) -> str:
    """
    Add a new post to the publish queue.

    Args:
        clip_path:          Absolute path to the exported MP4 clip.
        platforms:          List of platforms: any of "youtube", "tiktok", "instagram".
        title:              Post title / caption (used as YouTube title, max 100 chars).
        description:        Longer description (used by YouTube; optional for others).
        scheduled_time_iso: ISO 8601 UTC datetime string, e.g. "2026-05-16T15:00:00+00:00".
        tags:               Optional list of hashtag strings (without '#').
        channel:            Publishing channel identifier, e.g. "neilbound" or "ilb".
        extra:              Optional per-platform caption overrides:
                            { "tiktok_caption": "...", "instagram_caption": "..." }

    Returns:
        post_id (8-character UUID prefix) for use with mark_complete / mark_failed / cancel.
    """
    # Warnings are persisted into the queue entry (and surfaced by the MCP
    # tools) because stdout is discarded when this runs under Task Scheduler.
    entry_warnings: list[str] = []

    # ── Normalise scheduled_time ────────────────────────────────────────────────
    dt = datetime.fromisoformat(scheduled_time_iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    scheduled_time_str = dt.isoformat()

    # ── 2. Title truncation (YouTube max 100 chars) ─────────────────────────────
    if len(title) > 100:
        title = title[:97] + "..."
        entry_warnings.append("title truncated to 100 chars for YouTube")
        print("[publish_queue] WARNING: title truncated to 100 chars for YouTube")

    # ── 3. Strip markdown from all captions ────────────────────────────────────
    description = _strip_md(description)
    if extra:
        extra = dict(extra)   # don't mutate caller's dict
        for caption_key in ("tiktok_caption", "instagram_caption"):
            if extra.get(caption_key):
                extra[caption_key] = _strip_md(extra[caption_key])

    # ── 4. Platform-specific content checks ────────────────────────────────────
    tiktok_cap = (extra or {}).get("tiktok_caption", "")
    if tiktok_cap and len(tiktok_cap) > 150:
        entry_warnings.append(
            f"TikTok caption is {len(tiktok_cap)} chars (best practice <= 150)"
        )
        print(
            f"[publish_queue] WARNING: TikTok caption is {len(tiktok_cap)} chars "
            f"(best practice <= 150) — consider shortening"
        )

    ig_cap = (extra or {}).get("instagram_caption", description)
    if ig_cap:
        hashtag_count = ig_cap.count("#")
        if hashtag_count > 28:
            entry_warnings.append(
                f"Instagram caption has {hashtag_count} hashtags (max 30, best practice <= 28)"
            )
            print(
                f"[publish_queue] WARNING: Instagram caption has {hashtag_count} hashtags "
                f"(max 30, best practice <= 28)"
            )

    # All read-modify-write of the queue happens under the cross-process lock.
    with _queue_lock():
        queue = _load()

        # ── 1. Duplicate detection ──────────────────────────────────────────────
        # If the same clip_path is already pending within 1 hour of this scheduled
        # time, return the existing post_id rather than creating a duplicate.
        for existing in queue:
            if existing.get("status") != "pending":
                continue
            if existing.get("clip_path") != clip_path:
                continue
            try:
                existing_t = datetime.fromisoformat(existing["scheduled_time"])
                if existing_t.tzinfo is None:
                    existing_t = existing_t.replace(tzinfo=timezone.utc)
                if abs((existing_t - dt).total_seconds()) < 3600:
                    print(
                        f"[publish_queue] DUPLICATE: {os.path.basename(clip_path)} is already "
                        f"queued as {existing['post_id']} for {existing['scheduled_time']}. "
                        f"Skipping — returning existing post_id."
                    )
                    return existing["post_id"]
            except Exception:
                pass

        # ── 4b. Duplicate-risk warning (same channel, same clip, shared platform) ──
        # The 1-hour block above handles accidental double-enqueues; this catches
        # the same clip being re-queued to the same platform later (sometimes
        # deliberate, e.g. wrong-channel recovery — so warn, never block).
        for existing in queue:
            if existing.get("status") == "cancelled":
                continue
            if existing.get("clip_path") != clip_path:
                continue
            if existing.get("channel") != channel:
                continue
            shared = set(existing.get("platforms", [])) & set(platforms)
            if shared:
                entry_warnings.append(
                    f"DUPLICATE RISK: this clip is already queued/posted to "
                    f"{', '.join(sorted(shared))} on channel '{channel}' as post "
                    f"{existing.get('post_id')} (scheduled {existing.get('scheduled_time')})"
                )
                break

        # ── 5. Daily density check ───────────────────────────────────────────────
        sched_date = dt.date()
        posts_that_day = sum(
            1 for e in queue
            if e.get("status") == "pending"
            and e.get("channel") == channel
            and _entry_date(e) == sched_date
        )
        if posts_that_day >= 2:
            entry_warnings.append(
                f"{posts_that_day + 1} posts now scheduled for {sched_date} "
                f"on channel '{channel}'"
            )
            print(
                f"[publish_queue] WARNING: {posts_that_day + 1} posts now scheduled "
                f"for {sched_date} on channel '{channel}' — consider spreading them out"
            )

        # ── 6. Scheduled-in-the-past warning ─────────────────────────────────────
        lag = (_now_utc() - dt).total_seconds()
        if lag > 3600:
            hours_ago = int(lag // 3600)
            entry_warnings.append(
                f"scheduled_time is {hours_ago}h in the past — will upload at next daemon run"
            )
            print(
                f"[publish_queue] WARNING: scheduled_time is {hours_ago}h in the past — "
                f"will upload at next daemon run"
            )

        # ── Build and save entry ─────────────────────────────────────────────────
        post_id = uuid.uuid4().hex[:8]

        entry = {
            "post_id":        post_id,
            "clip_path":      clip_path,
            "platforms":      platforms,
            "title":          title,
            "description":    description,
            "tags":           tags or [],
            "scheduled_time": scheduled_time_str,
            "channel":        channel,
            "status":         "pending",
            "results":        {},
            "warnings":       entry_warnings,
        }
        if extra:
            entry.update(extra)

        queue.append(entry)
        _save(queue)

    return post_id


def get_entry(post_id: str) -> Optional[dict]:
    """Return the queue entry for post_id, or None if not found."""
    for entry in _load():
        if entry.get("post_id") == post_id:
            return entry
    return None


def confirm_manual_post(post_id: str, platform: str = "tiktok") -> bool:
    """
    Record that the operator completed a platform's manual posting step
    (e.g. tapped Post on a TikTok inbox upload). Clears the entry from the
    NEEDS ATTENTION draft reminder. Returns False if the post/platform result
    wasn't found or didn't require a manual post.
    """
    with _queue_lock():
        queue = _load()
        for entry in queue:
            if entry.get("post_id") != post_id:
                continue
            res = entry.get("results", {}).get(platform)
            if not res or not res.get("requires_manual_post"):
                return False
            res["manually_posted"] = True
            _save(queue)
            return True
    return False


def _entry_date(entry: dict):
    """Return the date portion of a queue entry's scheduled_time, or None."""
    try:
        dt = datetime.fromisoformat(entry["scheduled_time"])
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.date()
    except Exception:
        return None


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


# Automatic-retry policy for failed/partial uploads.
DEFAULT_MAX_ATTEMPTS = 4      # give up after this many daemon rounds
RETRY_BASE_MINUTES   = 30     # backoff = base * 2**(attempts-1): 30, 60, 120 min


# Substrings that indicate a credential/config failure that will not heal on a
# backoff timer (the operator has to fix something). Matched case-insensitively
# against the exception text by is_fatal_error().
FATAL_ERROR_MARKERS = (
    "invalid_grant", "invalid_client", "401", "403",
    "unauthorized", "forbidden", "credentials", "not set for channel",
    "access_token", "refresh token",
)


def is_fatal_error(exc: Exception) -> bool:
    """
    True for errors that auto-retry cannot fix (missing/expired credentials,
    bad config) — these skip the retry budget and wait for manual retry_failed.

    Network errors are checked first and always retryable: TimeoutError and
    ConnectionError are OSError subclasses (and requests' exceptions inherit
    IOError), so a naive isinstance(EnvironmentError) test would misclassify
    every network blip as fatal. publish.py's missing-credential errors are
    caught by the "not set for channel" marker; ValueError covers config
    mistakes like a bad post_mode.
    """
    type_names = {c.__name__ for c in type(exc).__mro__}
    if any("Timeout" in n or "Connection" in n for n in type_names):
        return False
    if isinstance(exc, ValueError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in FATAL_ERROR_MARKERS)


def _entry_has_unfinished_platform(entry: dict, ignore_fatal: bool = False) -> bool:
    """
    True if any of the entry's target platforms is not yet uploaded ('ok').
    With ignore_fatal=True, platforms whose failure was marked fatal don't
    count — used by get_retryable so fatal failures never re-enter the pool.
    """
    results = entry.get("results", {})
    for p in entry.get("platforms", []):
        r = results.get(p, {})
        if r.get("status") == "ok":
            continue
        if ignore_fatal and r.get("fatal"):
            continue
        return True
    return False


def get_retryable(now: Optional[datetime] = None,
                  max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> list[dict]:
    """
    Return failed/partial entries eligible for an automatic retry.

    An entry is retryable when it still has a platform that hasn't succeeded,
    has not exhausted its attempt budget, and whose next_retry_at (if set) is
    at or before `now`. The daemon processes these alongside freshly-due posts;
    the per-platform idempotency guard ensures only the failed platforms retry.
    """
    now = now or _now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    queue = _load()
    out = []
    for entry in queue:
        if entry.get("status") not in ("failed", "partial"):
            continue
        if not _entry_has_unfinished_platform(entry, ignore_fatal=True):
            continue
        if entry.get("attempts", 0) >= max_attempts:
            continue  # attempt budget exhausted — left for manual retry_failed
        nra = entry.get("next_retry_at")
        if nra:
            try:
                t = datetime.fromisoformat(nra)
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t > now:
                    continue  # still in backoff window
            except (TypeError, ValueError):
                pass
        out.append(entry)
    return out


def schedule_retry(post_id: str,
                   max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                   base_minutes: int = RETRY_BASE_MINUTES) -> tuple[bool, int]:
    """
    Record a failed/partial processing round and schedule the next retry.

    Increments the entry's attempt counter and sets next_retry_at using
    exponential backoff. Returns (will_retry_again, attempts). When the attempt
    budget is exhausted, next_retry_at is cleared and will_retry_again is False
    (the entry stays failed/partial for manual handling).

    Call this once per entry after a daemon round that left platforms unfinished.
    """
    with _queue_lock():
        queue = _load()
        for entry in queue:
            if entry["post_id"] != post_id:
                continue
            attempts = entry.get("attempts", 0) + 1
            entry["attempts"] = attempts
            if attempts >= max_attempts:
                entry["next_retry_at"] = None
                _save(queue)
                return False, attempts
            delay = base_minutes * (2 ** (attempts - 1))
            entry["next_retry_at"] = (_now_utc() + timedelta(minutes=delay)).isoformat()
            _save(queue)
            return True, attempts
    return False, 0


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
    with _queue_lock():
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


def mark_failed(post_id: str, platform: str, error: str, fatal: bool = False) -> None:
    """
    Record a failed upload for a specific platform.

    The overall entry status is set to 'failed' unless other platforms succeeded
    (in which case it becomes 'partial').

    Args:
        post_id:  The 8-character post identifier.
        platform: Platform key, e.g. "tiktok".
        error:    Human-readable error message or exception string.
        fatal:    True for credential/config errors that auto-retry cannot fix —
                  the platform is excluded from get_retryable until the operator
                  runs retry_failed (which clears fatal results).
    """
    with _queue_lock():
        queue = _load()
        for entry in queue:
            if entry["post_id"] != post_id:
                continue
            result = {"status": "error", "error": error}
            if fatal:
                result["fatal"] = True
            entry["results"][platform] = result
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


def find_schedule_gaps(now: Optional[datetime] = None,
                       min_per_day: int = 1) -> list[tuple[str, int]]:
    """
    Find calendar days in the upcoming pending window that fall below the expected
    posting cadence — used to warn about gaps after a reschedule.

    Scans every day from today through the last pending post. Returns a list of
    (iso_date, post_count) for each day with fewer than `min_per_day` pending
    posts. An empty list means no gaps.

    Args:
        now:         Reference time (UTC). Defaults to now.
        min_per_day: Minimum posts per day before a day counts as a gap. Default 1
                     (i.e. only fully-empty days are flagged). Pass 2 to flag any
                     day below a 2/day cadence.
    """
    now = now or _now_utc()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    from collections import Counter
    dates = [d for d in (_entry_date(e) for e in _load()
                         if e.get("status") == "pending")
             if d and d >= now.date()]
    if not dates:
        return []

    counts = Counter(dates)
    gaps: list[tuple[str, int]] = []
    d = min(dates)
    last = max(dates)
    while d <= last:
        n = counts.get(d, 0)
        if n < min_per_day:
            gaps.append((d.isoformat(), n))
        d += timedelta(days=1)
    return gaps


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
    with _queue_lock():
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


def retry_failed(post_id: str) -> tuple[bool, list[str]]:
    """
    Re-arm only the failed platforms of a partial/failed entry for another attempt.

    Unlike manually resetting status='pending' and results={}, this preserves the
    results of platforms that already succeeded — so the daemon will NOT re-upload
    them (preventing duplicate posts). Only platforms whose result is missing or
    'error' are cleared and the entry is set back to 'pending'.

    Args:
        post_id: The 8-character post identifier.

    Returns:
        (success, platforms_to_retry). success is False if the post wasn't found
        or had no failed/missing platforms to retry.
    """
    with _queue_lock():
        queue = _load()
        for entry in queue:
            if entry["post_id"] != post_id:
                continue

            all_platforms = entry.get("platforms", [])
            results = entry.get("results", {})
            # A platform needs retry if it has no result or an error result.
            to_retry = [
                p for p in all_platforms
                if results.get(p, {}).get("status") != "ok"
            ]
            if not to_retry:
                return False, []  # Everything already succeeded — nothing to do

            # Clear only the failed/missing platform results; keep the 'ok' ones.
            entry["results"] = {
                p: r for p, r in results.items() if r.get("status") == "ok"
            }
            entry["status"] = "pending"
            _save(queue)
            return True, to_retry

    return False, []  # Not found
