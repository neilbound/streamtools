"""
publisher_daemon.py — Processes due publish queue entries and uploads clips to social platforms.

Run via Windows Task Scheduler every 15 minutes:
    Program:   C:\\GitHub Repositories\\streamtools\\.venv312\\Scripts\\python.exe
    Arguments: publisher_daemon.py
    Start in:  C:\\GitHub Repositories\\streamtools

Requires PUBLISHING_ENABLED=true in .env to upload anything.
"""

import os
import sys
import traceback
from datetime import datetime, timezone

# Add project root to sys.path so pipeline/ is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

from pipeline.publish_queue import get_due, mark_complete, mark_failed
from pipeline.publish import upload_youtube, upload_tiktok, upload_instagram


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

    if platform == "youtube":
        return upload_youtube(
            clip_path=clip_path,
            title=title,
            description=description,
            tags=tags,
            scheduled_time=sched_time,
        )
    elif platform == "tiktok":
        return upload_tiktok(
            clip_path=clip_path,
            title=title,
            tags=tags,
        )
    elif platform == "instagram":
        return upload_instagram(
            clip_path=clip_path,
            title=title,
            scheduled_time=sched_time,
        )
    else:
        raise ValueError(f"Unknown platform: {platform!r}")


# ── Main ────────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(tz=timezone.utc)
    print(f"[publisher_daemon] Starting at {now.isoformat()}")

    publishing_enabled = os.environ.get("PUBLISHING_ENABLED", "").lower() == "true"
    if not publishing_enabled:
        print(
            "[publisher_daemon] PUBLISHING_ENABLED is not set to 'true'. "
            "No uploads will be attempted.\n"
            "To enable publishing: set PUBLISHING_ENABLED=true in .env and run "
            "python setup_credentials.py --platform <platform> for each platform."
        )
        return

    due_posts = get_due(now=now)

    if not due_posts:
        print("[publisher_daemon] No posts due. Exiting.")
        return

    print(f"[publisher_daemon] {len(due_posts)} post(s) due for publishing.")

    success_count = 0
    failure_count = 0

    for entry in due_posts:
        post_id   = entry["post_id"]
        platforms = entry.get("platforms", [])
        title     = entry.get("title", "(no title)")

        print(f"\n[publisher_daemon] Processing post_id={post_id} | {title!r}")
        print(f"  Platforms : {', '.join(platforms)}")
        print(f"  Clip      : {entry.get('clip_path', '?')}")
        print(f"  Scheduled : {entry.get('scheduled_time', '?')}")

        for platform in platforms:
            print(f"  [{platform}] Uploading...")
            try:
                result = _upload_platform(platform, entry)
                mark_complete(post_id, platform, result)
                print(f"  [{platform}] OK — {result}")
                success_count += 1
            except Exception as exc:
                error_msg = f"{type(exc).__name__}: {exc}"
                print(f"  [{platform}] FAILED — {error_msg}")
                traceback.print_exc()
                mark_failed(post_id, platform, error_msg)
                failure_count += 1
                # Continue to next platform — do not abort the whole run

    print(
        f"\n[publisher_daemon] Done. "
        f"Successes: {success_count}  Failures: {failure_count}"
    )


if __name__ == "__main__":
    main()
