"""
publisher_daemon.py — Processes due publish queue entries and uploads clips to social platforms.

Run via Windows Task Scheduler every 15 minutes:
    Program:   C:\\GitHub Repositories\\streamtools\\.venv312\\Scripts\\python.exe
    Arguments: publisher_daemon.py
    Start in:  C:\\GitHub Repositories\\streamtools

Requires PUBLISHING_ENABLED=true in .env to upload anything.
"""

import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from filelock import FileLock, Timeout

# Windows consoles default to cp1252 — a single emoji or box-drawing char in
# printed output has crashed real runs. Force UTF-8 with replacement so console
# encoding can never kill the daemon. (The rotating file log is already utf-8.)
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

# Add project root to sys.path so pipeline/ is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

from pipeline.publish_queue import (
    get_due,
    get_retryable,
    is_fatal_error,
    list_all,
    mark_complete,
    mark_failed,
    schedule_retry,
)
from pipeline.publish import (
    reconcile_youtube,
    upload_youtube,
    upload_tiktok,
    upload_instagram,
)
from pipeline.analytics import snapshot as _analytics_snapshot
from pipeline.validate import quick_probe_check


# ── Logging ───────────────────────────────────────────────────────────────────
# The daemon runs unattended under Task Scheduler, where stdout is discarded.
# Persist every run to a rotating log file so failures have an audit trail, while
# still echoing to the console for manual/interactive runs.

_LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", "publisher_daemon.log"
)


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("publisher_daemon")
    if logger.handlers:  # already configured (avoid duplicate handlers on re-import)
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
    fh = RotatingFileHandler(
        _LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


log = _setup_logging()


# ── Platform dispatch ───────────────────────────────────────────────────────────

_UPLOADERS = {
    "youtube":   upload_youtube,
    "tiktok":    upload_tiktok,
    "instagram": upload_instagram,
}


def _upload_platform(platform: str, entry: dict) -> dict:
    """
    Dispatch to the correct upload function for a given platform.
    Returns the result dict from the upload function.
    """
    clip_path   = entry["clip_path"]
    title       = entry["title"]
    description = entry.get("description", "")
    tags        = entry.get("tags") or []
    sched_time  = entry.get("scheduled_time")
    channel     = entry.get("channel", "neilbound")

    if platform == "youtube":
        return upload_youtube(
            clip_path=clip_path,
            title=title,
            description=description,
            tags=tags,
            scheduled_time=sched_time,
            channel=channel,
            playlist_id=entry.get("playlist_id", ""),
        )
    elif platform == "tiktok":
        # Use per-platform caption if provided, otherwise fall back to title
        tiktok_title = entry.get("tiktok_caption") or title
        return upload_tiktok(
            clip_path=clip_path,
            title=tiktok_title,
            tags=tags,
            channel=channel,
            privacy_level=entry.get("tiktok_privacy", ""),
            post_mode=entry.get("tiktok_mode", ""),
        )
    elif platform == "instagram":
        # Use per-platform caption if provided, otherwise fall back to description or title
        ig_title = entry.get("instagram_caption") or description or title
        return upload_instagram(
            clip_path=clip_path,
            title=ig_title,
            scheduled_time=sched_time,
            channel=channel,
        )
    else:
        raise ValueError(f"Unknown platform: {platform!r}")


# ── Main ────────────────────────────────────────────────────────────────────────

# Whole-run exclusive lock. Prevents two daemon invocations (e.g. a manual run
# overlapping the Task Scheduler run) from both uploading the same due post before
# either marks it complete — the race that caused a duplicate upload.
_RUN_LOCK = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", "publisher_daemon.run.lock"
)

# Heartbeat: proof-of-life written at the start of EVERY run (even no-op runs).
# The Task Scheduler task was once found silently Disabled, causing a multi-day
# posting gap — a stale heartbeat is how that failure mode gets surfaced
# (list_scheduled_clips checks it in its NEEDS ATTENTION section).
HEARTBEAT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", ".last_daemon_heartbeat"
)
# Daemon runs every 15 min; >60 min stale means at least 3 missed runs.
HEARTBEAT_STALE_SECS = 3600


def write_heartbeat(now=None):
    """Record that the daemon executed. Never raises — a heartbeat failure must
    not break a publishing run."""
    try:
        ts = (now or datetime.now(tz=timezone.utc)).timestamp()
        os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
        with open(HEARTBEAT_PATH, "w") as f:
            f.write(str(ts))
    except Exception:
        pass


def heartbeat_age_seconds(now=None) -> float | None:
    """Seconds since the daemon last ran, or None if it has never written one."""
    try:
        with open(HEARTBEAT_PATH) as f:
            last = float(f.read().strip())
    except (OSError, ValueError):
        return None
    ref = (now or datetime.now(tz=timezone.utc)).timestamp()
    return max(0.0, ref - last)


def main():
    os.makedirs(os.path.dirname(_RUN_LOCK), exist_ok=True)
    run_lock = FileLock(_RUN_LOCK, timeout=0)   # non-blocking
    try:
        run_lock.acquire()
    except Timeout:
        log.warning(
            "Another daemon instance is already running — exiting to avoid overlap."
        )
        return
    try:
        _run()
    finally:
        run_lock.release()


_RECONCILE_MARKER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", ".last_reconcile"
)
_RECONCILE_INTERVAL = 86400  # once per 24h


def _maybe_reconcile():
    """Once per 24h, audit ok'd YouTube uploads against the actual channel and log any
    drift (videos deleted/rejected/truncated after the fact). Cheap no-op between runs."""
    import time
    try:
        last = float(open(_RECONCILE_MARKER).read().strip())
    except Exception:
        last = 0.0
    if time.time() - last < _RECONCILE_INTERVAL:
        return
    channels = {
        e.get("channel", "neilbound") for e in list_all()
        if e.get("results", {}).get("youtube", {}).get("status") == "ok"
    }
    for ch in sorted(channels):
        try:
            problems = reconcile_youtube(ch)
            if problems:
                log.warning("Reconciliation [%s]: %d discrepancy(ies):", ch, len(problems))
                for p in problems:
                    log.warning("  [%s] %s  %s", p["issue"], p["video_id"], p["title"])
            else:
                log.info("Reconciliation [%s]: all uploads healthy.", ch)
        except Exception as exc:
            log.warning("Reconciliation [%s] skipped: %s", ch, exc)
    try:
        os.makedirs(os.path.dirname(_RECONCILE_MARKER), exist_ok=True)
        with open(_RECONCILE_MARKER, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


_SNAPSHOT_MARKER = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "output", ".last_snapshot"
)
_SNAPSHOT_INTERVAL = 86400  # once per 24h


def _maybe_snapshot():
    """Once per 24h, record a performance snapshot of every posted video so we build a
    growth time series for content optimization. Cheap no-op between runs."""
    import time
    try:
        last = float(open(_SNAPSHOT_MARKER).read().strip())
    except Exception:
        last = 0.0
    if time.time() - last < _SNAPSHOT_INTERVAL:
        return
    try:
        result = _analytics_snapshot(channel="ilb")
        log.info("Analytics snapshot: %s row(s) recorded (tier2=%s)",
                 result.get("snapshotted"), result.get("tier2"))
    except Exception as exc:
        log.warning("Analytics snapshot skipped: %s", exc)
    try:
        os.makedirs(os.path.dirname(_SNAPSHOT_MARKER), exist_ok=True)
        with open(_SNAPSHOT_MARKER, "w") as f:
            f.write(str(time.time()))
    except Exception:
        pass


def _run():
    now = datetime.now(tz=timezone.utc)
    write_heartbeat(now)
    log.info("Starting at %s", now.isoformat())

    publishing_enabled = os.environ.get("PUBLISHING_ENABLED", "").lower() == "true"
    if not publishing_enabled:
        log.warning(
            "PUBLISHING_ENABLED is not 'true' — no uploads will be attempted. "
            "Set PUBLISHING_ENABLED=true in .env and run "
            "setup_credentials.py --platform <platform> for each platform."
        )
        return

    # Daily audit of already-posted uploads + performance snapshot (gated to 24h each).
    _maybe_reconcile()
    _maybe_snapshot()

    # Freshly-due (pending) posts + failed/partial posts eligible for auto-retry.
    # The two lists are disjoint by status, so concatenation needs no dedupe.
    due_posts   = get_due(now=now)
    retry_posts = get_retryable(now=now)

    if not due_posts and not retry_posts:
        log.info("No posts due. Exiting.")
        return

    log.info("%d due, %d retryable post(s) for publishing.",
             len(due_posts), len(retry_posts))

    success_count = 0
    failure_count = 0

    for entry in due_posts + retry_posts:
        post_id   = entry["post_id"]
        platforms = entry.get("platforms", [])
        title     = entry.get("title", "(no title)")

        log.info("Processing post_id=%s | %r", post_id, title)
        log.info("  Platforms : %s", ", ".join(platforms))
        log.info("  Clip      : %s", entry.get("clip_path", "?"))
        log.info("  Scheduled : %s", entry.get("scheduled_time", "?"))

        # ── Pre-flight: single ffprobe sanity check (streams, duration, aspect).
        # No decode — full QA already ran at export and the scheduling gate.
        # Failures are fatal (no auto-retry): a corrupt or missing file will
        # not heal between daemon rounds.
        clip_path = entry.get("clip_path", "")
        probe_err = quick_probe_check(clip_path, entry.get("expected_orientation", ""))
        if probe_err:
            error_msg = f"PRE-FLIGHT: {probe_err}"
            log.error("  [pre-flight] FAILED — %s", error_msg)
            for platform in platforms:
                mark_failed(post_id, platform, error_msg, fatal=True)
            failure_count += len(platforms)
            continue
        log.info("  [pre-flight] OK — %.1f MB",
                 os.path.getsize(clip_path) / (1024 * 1024))

        existing_results = entry.get("results", {})
        round_finished = True   # all target platforms ended 'ok' this round

        for platform in platforms:
            # Idempotency guard: never re-upload a platform that already succeeded.
            # Protects against duplicate posts if an entry re-enters the queue as
            # 'partial' or is manually re-armed without clearing its 'ok' results.
            if existing_results.get(platform, {}).get("status") == "ok":
                log.info("  [%s] Skipped — already uploaded successfully", platform)
                continue

            log.info("  [%s] Uploading...", platform)
            try:
                result = _upload_platform(platform, entry)
                mark_complete(post_id, platform, result)
                log.info("  [%s] OK — %s", platform, result)
                success_count += 1
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                fatal = is_fatal_error(exc)
                log.exception("  [%s] FAILED — %s", platform, error_msg)
                mark_failed(post_id, platform, error_msg, fatal=fatal)
                failure_count += 1
                if fatal:
                    # Credential/config errors don't heal on a backoff timer —
                    # don't burn the retry budget; wait for manual retry_failed.
                    log.error("  [%s] FATAL — credential/config error, will NOT "
                              "auto-retry: %s", platform, error_msg)
                else:
                    round_finished = False
                # Continue to next platform — do not abort the whole run

        # If the entry still has unfinished platforms, schedule an automatic
        # retry with exponential backoff (until the attempt budget is spent).
        if not round_finished:
            will_retry, attempts = schedule_retry(post_id)
            if will_retry:
                log.info("  [retry] attempt %d failed — will auto-retry with backoff", attempts)
            else:
                log.warning("  [retry] attempt budget exhausted after %d tries — "
                            "left for manual retry_failed", attempts)

    log.info("Done. Successes: %d  Failures: %d", success_count, failure_count)


if __name__ == "__main__":
    main()
