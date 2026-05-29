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

# Add project root to sys.path so pipeline/ is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

from pipeline.publish_queue import (
    get_due,
    get_retryable,
    mark_complete,
    mark_failed,
    schedule_retry,
)
from pipeline.publish import upload_youtube, upload_tiktok, upload_instagram


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

def main():
    now = datetime.now(tz=timezone.utc)
    log.info("Starting at %s", now.isoformat())

    publishing_enabled = os.environ.get("PUBLISHING_ENABLED", "").lower() == "true"
    if not publishing_enabled:
        log.warning(
            "PUBLISHING_ENABLED is not 'true' — no uploads will be attempted. "
            "Set PUBLISHING_ENABLED=true in .env and run "
            "setup_credentials.py --platform <platform> for each platform."
        )
        return

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

        # ── Pre-flight: verify the clip file exists and is not suspiciously small ──
        clip_path = entry.get("clip_path", "")
        if not clip_path or not os.path.exists(clip_path):
            error_msg = f"FILE NOT FOUND: {clip_path!r}"
            log.error("  [pre-flight] FAILED — %s", error_msg)
            for platform in platforms:
                mark_failed(post_id, platform, error_msg)
            failure_count += len(platforms)
            continue
        size_mb = os.path.getsize(clip_path) / (1024 * 1024)
        if size_mb < 0.1:
            error_msg = f"FILE TOO SMALL ({size_mb:.2f} MB) — may be corrupt: {clip_path!r}"
            log.error("  [pre-flight] FAILED — %s", error_msg)
            for platform in platforms:
                mark_failed(post_id, platform, error_msg)
            failure_count += len(platforms)
            continue
        log.info("  [pre-flight] OK — %.1f MB", size_mb)

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
                log.exception("  [%s] FAILED — %s", platform, error_msg)
                mark_failed(post_id, platform, error_msg)
                failure_count += 1
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
