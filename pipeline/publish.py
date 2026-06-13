"""
pipeline/publish.py — Social media upload functions for YouTube Shorts, TikTok, and Instagram Reels.

All credentials are loaded from environment variables. Use setup_credentials.py to generate them.
Set PUBLISHING_ENABLED=true in .env to activate uploads.

Functions:
    upload_youtube(clip_path, title, description, tags, scheduled_time) -> dict
    upload_tiktok(clip_path, title, tags) -> dict
    upload_instagram(clip_path, title, scheduled_time) -> dict
"""

import os
import math
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional


# ── Guard + credential helpers ─────────────────────────────────────────────────

def _require_publishing_enabled():
    """Raise a clear error if PUBLISHING_ENABLED is not set to 'true'."""
    if os.environ.get("PUBLISHING_ENABLED", "").lower() != "true":
        raise EnvironmentError(
            "Publishing is disabled. Set PUBLISHING_ENABLED=true in .env, "
            "then run: python setup_credentials.py --platform <platform> --channel <channel>"
        )


def _cred(channel: str, key: str) -> str | None:
    """
    Look up a credential env var with optional channel prefix.

    With channel='neilbound' and key='YOUTUBE_CLIENT_ID':
      → checks NEILBOUND_YOUTUBE_CLIENT_ID first, falls back to YOUTUBE_CLIENT_ID.
    With channel='':
      → checks YOUTUBE_CLIENT_ID only.
    """
    if channel:
        val = os.environ.get(f"{channel.upper()}_{key}")
        if val:
            return val
    return os.environ.get(key)


# ── YouTube self-healing helpers ───────────────────────────────────────────────
# Two failure modes have bitten in production, both from trusting the upload result:
#   1. Read timeout AFTER the bytes landed -> false failure (would double-post on retry)
#   2. Truncated upload -> video stuck "processing" with duration P0D (never recovers)
# These helpers verify a video actually landed and is intact, and recover automatically.

def classify_youtube_health(item: Optional[dict]) -> str:
    """
    Classify a YouTube videos().list item (part=status,contentDetails,processingDetails).

    Returns:
      "ok"        — has a real (non-zero) duration; the file is intact
      "truncated" — uploaded but duration is P0D/empty and processing failed/terminated
      "pending"   — uploaded, no duration yet, still processing (re-check shortly)
      "missing"   — no item (video not found)
    """
    if not item:
        return "missing"
    duration = (item.get("contentDetails") or {}).get("duration", "")
    proc = (item.get("processingDetails") or {}).get("processingStatus", "")
    has_real_duration = bool(duration) and duration not in ("P0D", "PT0S")
    if has_real_duration:
        return "ok"
    if proc in ("failed", "terminated"):
        return "truncated"
    return "pending"


def _yt_video_item(youtube, video_id: str) -> Optional[dict]:
    r = youtube.videos().list(
        part="status,contentDetails,processingDetails", id=video_id
    ).execute()
    items = r.get("items", [])
    return items[0] if items else None


def _yt_wait_health(youtube, video_id: str, polls: int = 4, gap: float = 5.0) -> str:
    """
    Poll a freshly-uploaded video's health. Healthy clips populate their duration
    within seconds; a truncated upload stays P0D. Returns 'ok' or 'truncated'.
    """
    import time as _t
    for i in range(polls):
        health = classify_youtube_health(_yt_video_item(youtube, video_id))
        if health == "ok":
            return "ok"
        if health == "truncated":
            return "truncated"
        if i < polls - 1:
            _t.sleep(gap)
    # Persistent P0D after polling — for short clips this means a truncated file.
    return "truncated"


def _yt_recent_uploads(youtube, limit: int = 15) -> list[dict]:
    """Return recent uploads [{video_id, title, publishedAt}] via the uploads playlist
    (more immediately consistent for just-uploaded videos than search())."""
    ch = youtube.channels().list(part="contentDetails", mine=True).execute()
    items = ch.get("items", [])
    if not items:
        return []
    uploads_pl = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
    r = youtube.playlistItems().list(
        part="snippet,contentDetails", playlistId=uploads_pl, maxResults=limit
    ).execute()
    out = []
    for it in r.get("items", []):
        sn = it.get("snippet", {})
        out.append({
            "video_id": it.get("contentDetails", {}).get("videoId", ""),
            "title": sn.get("title", ""),
            "publishedAt": sn.get("publishedAt", ""),
        })
    return out


def _yt_find_recent_by_title(youtube, title: str, polls: int = 6, gap: float = 6.0) -> Optional[str]:
    """After an upload error, poll the uploads playlist for a video matching `title`
    to recover its id (the bytes may have landed despite the error)."""
    import time as _t
    for i in range(polls):
        for u in _yt_recent_uploads(youtube):
            if u["title"].strip() == title.strip() and u["video_id"]:
                return u["video_id"]
        if i < polls - 1:
            _t.sleep(gap)
    return None


def _yt_delete(youtube, video_id: str) -> None:
    try:
        youtube.videos().delete(id=video_id).execute()
        print(f"[YouTube] Deleted broken video {video_id}")
    except Exception as exc:
        print(f"[YouTube] Warning: could not delete {video_id}: {exc}")


def _yt_add_to_playlist(youtube, playlist_id: str, video_id: str, retries: int = 3) -> bool:
    """Add a video to a playlist, retrying transient errors (429/5xx/409 SERVICE_UNAVAILABLE)."""
    import time as _t
    for attempt in range(1, retries + 1):
        try:
            youtube.playlistItems().insert(
                part="snippet",
                body={"snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }},
            ).execute()
            print(f"[YouTube] Added to playlist: {playlist_id}")
            return True
        except Exception as exc:
            transient = any(s in str(exc) for s in ("SERVICE_UNAVAILABLE", "backendError",
                                                    "quotaExceeded", "500", "503", "409"))
            if transient and attempt < retries:
                _t.sleep(2 ** attempt)
                continue
            print(f"[YouTube] Warning: could not add to playlist ({exc})")
            return False
    return False


def _youtube_service(channel: str):
    """Build an authenticated YouTube API client for a channel (raises if creds missing)."""
    client_id     = _cred(channel, "YOUTUBE_CLIENT_ID")
    client_secret = _cred(channel, "YOUTUBE_CLIENT_SECRET")
    refresh_token = _cred(channel, "YOUTUBE_REFRESH_TOKEN")
    if not (client_id and client_secret and refresh_token):
        raise EnvironmentError(
            f"YouTube credentials incomplete for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform youtube --channel {channel}"
        )
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    creds = Credentials(token=None, refresh_token=refresh_token,
                        client_id=client_id, client_secret=client_secret,
                        token_uri="https://oauth2.googleapis.com/token")
    creds.refresh(Request())
    return build("youtube", "v3", credentials=creds)


def reconcile_youtube(channel: str = "ilb") -> list[dict]:
    """
    Audit every queue entry whose YouTube upload was marked 'ok' against the actual
    channel state, catching anything that slipped through after the fact:
      - "missing":   the video was deleted/rejected (no longer on the channel)
      - "truncated": stuck with no duration (P0D) and processing failed/terminated
    Videos still processing normally (no duration yet) are NOT flagged.

    Returns a list of {post_id, video_id, title, issue}. Empty = all healthy.
    """
    from pipeline.publish_queue import list_all
    entries = [
        e for e in list_all()
        if (e.get("results", {}).get("youtube", {}).get("status") == "ok"
            and e["results"]["youtube"].get("video_id"))
    ]
    if not entries:
        return []
    yt = _youtube_service(channel)
    health: dict[str, str] = {}
    ids = [e["results"]["youtube"]["video_id"] for e in entries]
    for i in range(0, len(ids), 50):   # videos.list accepts up to 50 ids/call
        batch = ids[i:i + 50]
        r = yt.videos().list(
            part="status,contentDetails,processingDetails", id=",".join(batch)
        ).execute()
        found = {}
        for it in r.get("items", []):
            found[it["id"]] = classify_youtube_health(it)
        for vid in batch:
            health[vid] = found.get(vid, "missing")
    problems = []
    for e in entries:
        vid = e["results"]["youtube"]["video_id"]
        h = health.get(vid, "missing")
        if h in ("missing", "truncated"):
            problems.append({
                "post_id": e["post_id"],
                "video_id": vid,
                "title": e.get("title", "")[:50],
                "issue": h,
            })
    return problems


# ── YouTube Shorts ─────────────────────────────────────────────────────────────

def upload_youtube(
    clip_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    scheduled_time: str | None = None,
    category_id: str = "22",
    contains_synthetic_media: bool = False,
    made_for_kids: bool = False,
    embeddable: bool = True,
    channel: str = "neilbound",
    playlist_id: str = "",
) -> dict:
    """
    Upload a short MP4 clip to YouTube Shorts.

    Refreshes credentials using the stored refresh token (no interactive OAuth flow).
    The clip is published immediately (public) or scheduled (private + publishAt).

    Args:
        clip_path:                Absolute path to the MP4 file.
        title:                    Video title (max 100 chars).
        description:              Video description. '#Shorts' is appended automatically.
        tags:                     Optional list of tag strings.
        scheduled_time:           ISO 8601 UTC string e.g. '2026-05-16T15:00:00+00:00'.
                                  If None, publishes immediately as public.
        category_id:              YouTube category ID. Default "22" (People & Blogs).
                                  Common: "22"=People&Blogs, "24"=Entertainment, "25"=News.
        contains_synthetic_media: Set True if the video contains AI-generated content.
                                  YouTube requires disclosure for AI voices/faces/scenes.
        made_for_kids:            Set True if content is directed at children (COPPA).
        embeddable:               Whether the video can be embedded on other sites.

    Returns:
        {"platform": "youtube", "video_id": str, "url": str, "scheduled": bool}
    """
    _require_publishing_enabled()

    client_id     = _cred(channel, "YOUTUBE_CLIENT_ID")
    client_secret = _cred(channel, "YOUTUBE_CLIENT_SECRET")
    refresh_token = _cred(channel, "YOUTUBE_REFRESH_TOKEN")

    if not client_id:
        raise EnvironmentError(
            f"YOUTUBE_CLIENT_ID not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform youtube --channel {channel}"
        )
    if not client_secret:
        raise EnvironmentError(
            f"YOUTUBE_CLIENT_SECRET not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform youtube --channel {channel}"
        )
    if not refresh_token:
        raise EnvironmentError(
            f"YOUTUBE_REFRESH_TOKEN not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform youtube --channel {channel}"
        )

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)

    # Ensure #Shorts is in description
    body_description = description
    if "#Shorts" not in body_description and "#shorts" not in body_description:
        body_description = body_description.rstrip() + "\n\n#Shorts"

    # Build status block
    if scheduled_time:
        # Parse and normalise to UTC ISO 8601
        dt = datetime.fromisoformat(scheduled_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # YouTube only honors a publishAt that is in the future. When the daemon
        # processes a *due* post, the scheduled time has just passed, so a past
        # publishAt would be rejected / leave the video stuck private. In that
        # case publish immediately as public instead of scheduling.
        if dt > datetime.now(tz=timezone.utc) + timedelta(seconds=60):
            publish_at_str = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            privacy = "private"
            is_scheduled = True
        else:
            publish_at_str = None
            privacy = "public"
            is_scheduled = False
    else:
        publish_at_str = None
        privacy = "public"
        is_scheduled = False

    status_body = {
        "privacyStatus": privacy,
    }
    if publish_at_str:
        status_body["publishAt"] = publish_at_str

    body = {
        "snippet": {
            "title": title,
            "description": body_description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {
            **status_body,
            "selfDeclaredMadeForKids": made_for_kids,
            "embeddable": embeddable,
            "containsSyntheticMedia": contains_synthetic_media,
        },
    }

    # ── Resilient upload: recover from timeout-after-success and truncation ────
    # Each attempt: upload -> (on error) try to recover the id from the uploads
    # playlist -> verify the file is intact (real duration) -> delete+retry if not.
    MAX_ATTEMPTS = 3
    from googleapiclient.http import MediaFileUpload  # re-import safe; used per attempt
    video_id = None
    last_exc = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        clean = False
        try:
            media = MediaFileUpload(clip_path, mimetype="video/mp4",
                                    resumable=True, chunksize=1024 * 1024 * 5)
            print(f"[YouTube] Uploading (attempt {attempt}/{MAX_ATTEMPTS}): {clip_path}")
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                status_obj, response = request.next_chunk()
                if status_obj:
                    print(f"[YouTube] Upload progress: {int(status_obj.progress() * 100)}%")
            video_id = response["id"]
            clean = True   # a completed resumable upload is intact by definition
            print(f"[YouTube] Upload complete: {video_id}")
        except Exception as exc:
            last_exc = exc
            print(f"[YouTube] Upload errored on attempt {attempt}: {exc}")
            # The bytes may have landed despite the error — try to recover the id
            print("[YouTube] Checking whether the video landed anyway...")
            video_id = _yt_find_recent_by_title(youtube, title)
            if video_id:
                print(f"[YouTube] Recovered uploaded video despite error: {video_id}")
            else:
                print("[YouTube] No matching video found — it did not land.")

        if not video_id:
            if attempt < MAX_ATTEMPTS:
                continue
            raise RuntimeError(
                f"YouTube upload failed after {MAX_ATTEMPTS} attempts. Last error: {last_exc}"
            )

        # Clean uploads are trusted. Only a video recovered after an interrupted
        # upload might be truncated — verify those with a generous window (a healthy
        # fresh upload can read P0D for up to ~a minute while YouTube ingests it).
        if clean:
            break
        health = _yt_wait_health(youtube, video_id, polls=6, gap=20.0)
        if health == "ok":
            break
        print(f"[YouTube] Recovered video {video_id} is truncated — deleting and retrying")
        _yt_delete(youtube, video_id)
        video_id = None
        if attempt < MAX_ATTEMPTS:
            continue
        raise RuntimeError(
            f"YouTube upload was truncated on every one of {MAX_ATTEMPTS} attempts "
            f"(recovered video stuck with no duration). Check the source file and network."
        )

    url = f"https://www.youtube.com/shorts/{video_id}"
    print(f"[YouTube] Upload complete and verified: {url}")

    if playlist_id:
        _yt_add_to_playlist(youtube, playlist_id, video_id)

    return {
        "platform":    "youtube",
        "video_id":    video_id,
        "url":         url,
        "scheduled":   is_scheduled,
        "playlist_id": playlist_id or None,
    }


# ── YouTube Full Episode ───────────────────────────────────────────────────────

def upload_youtube_episode(
    video_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    scheduled_time: str | None = None,
    category_id: str = "22",
    srt_path: str | None = None,
    thumbnail_path: str | None = None,
    contains_synthetic_media: bool = False,
    made_for_kids: bool = False,
    embeddable: bool = True,
    channel: str = "neilbound",
    playlist_id: str = "",
) -> dict:
    """
    Upload a full 16:9 episode to YouTube (long-form, not Shorts).

    Refreshes credentials using the stored refresh token. After upload, optionally
    sets a custom thumbnail and uploads an SRT caption file (both non-fatal on failure).

    Args:
        video_path:               Absolute path to the episode MP4.
        title:                    Video title (max 100 chars).
        description:              Full episode description. '#Shorts' is NOT appended.
        tags:                     Optional list of tag strings.
        scheduled_time:           ISO 8601 UTC string for scheduled publish.
                                  If None, publishes immediately as public.
        category_id:              YouTube category ID. Default "22" (People & Blogs).
        srt_path:                 Optional path to .srt captions file for upload.
        thumbnail_path:           Optional path to thumbnail image (JPEG/PNG).
        contains_synthetic_media: Set True if the video contains AI-generated content.
        made_for_kids:            Set True if content is directed at children (COPPA).
        embeddable:               Whether the video can be embedded on other sites.

    Returns:
        {"platform": "youtube", "video_id": str, "url": str, "scheduled": bool,
         "captions_uploaded": bool}
    """
    _require_publishing_enabled()

    client_id     = _cred(channel, "YOUTUBE_CLIENT_ID")
    client_secret = _cred(channel, "YOUTUBE_CLIENT_SECRET")
    refresh_token = _cred(channel, "YOUTUBE_REFRESH_TOKEN")

    if not client_id:
        raise EnvironmentError(
            f"YOUTUBE_CLIENT_ID not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform youtube --channel {channel}"
        )
    if not client_secret:
        raise EnvironmentError(
            f"YOUTUBE_CLIENT_SECRET not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform youtube --channel {channel}"
        )
    if not refresh_token:
        raise EnvironmentError(
            f"YOUTUBE_REFRESH_TOKEN not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform youtube --channel {channel}"
        )

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    creds.refresh(Request())

    youtube = build("youtube", "v3", credentials=creds)

    # Build status block
    if scheduled_time:
        dt = datetime.fromisoformat(scheduled_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Past/now publishAt is invalid for YouTube — publish immediately instead.
        if dt > datetime.now(tz=timezone.utc) + timedelta(seconds=60):
            publish_at_str = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
            privacy = "private"
            is_scheduled = True
        else:
            publish_at_str = None
            privacy = "public"
            is_scheduled = False
    else:
        publish_at_str = None
        privacy = "public"
        is_scheduled = False

    status_body: dict = {"privacyStatus": privacy}
    if publish_at_str:
        status_body["publishAt"] = publish_at_str

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": {
            **status_body,
            "selfDeclaredMadeForKids": made_for_kids,
            "embeddable": embeddable,
            "containsSyntheticMedia": contains_synthetic_media,
        },
    }

    # ── Resilient upload (same self-healing as Shorts; episodes are large and
    # more timeout-prone). Clean uploads are trusted; a video recovered after an
    # interrupted upload is health-checked and deleted+retried if truncated. ────
    MAX_ATTEMPTS = 3
    video_id = None
    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        clean = False
        try:
            media = MediaFileUpload(video_path, mimetype="video/mp4",
                                    resumable=True, chunksize=1024 * 1024 * 5)
            print(f"[YouTube Episode] Uploading (attempt {attempt}/{MAX_ATTEMPTS}): {video_path}")
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            while response is None:
                status_obj, response = request.next_chunk()
                if status_obj:
                    print(f"[YouTube Episode] Upload progress: {int(status_obj.progress() * 100)}%")
            video_id = response["id"]
            clean = True
            print(f"[YouTube Episode] Upload complete: {video_id}")
        except Exception as exc:
            last_exc = exc
            print(f"[YouTube Episode] Upload errored on attempt {attempt}: {exc}")
            print("[YouTube Episode] Checking whether the video landed anyway...")
            video_id = _yt_find_recent_by_title(youtube, title)
            if video_id:
                print(f"[YouTube Episode] Recovered uploaded video despite error: {video_id}")

        if not video_id:
            if attempt < MAX_ATTEMPTS:
                continue
            raise RuntimeError(
                f"YouTube episode upload failed after {MAX_ATTEMPTS} attempts. Last error: {last_exc}"
            )
        if clean:
            break
        health = _yt_wait_health(youtube, video_id, polls=6, gap=20.0)
        if health == "ok":
            break
        print(f"[YouTube Episode] Recovered video {video_id} truncated — deleting and retrying")
        _yt_delete(youtube, video_id)
        video_id = None
        if attempt < MAX_ATTEMPTS:
            continue
        raise RuntimeError(
            f"YouTube episode upload was truncated on every one of {MAX_ATTEMPTS} attempts."
        )

    url = f"https://www.youtube.com/watch?v={video_id}"
    print(f"[YouTube Episode] Upload complete and verified: {url}")

    # Optional: set thumbnail (non-fatal)
    if thumbnail_path and os.path.exists(thumbnail_path):
        try:
            ext = os.path.splitext(thumbnail_path)[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path, mimetype=mime),
            ).execute()
            print(f"[YouTube Episode] Thumbnail uploaded.")
        except Exception as e:
            print(f"[YouTube Episode] Thumbnail upload failed (non-fatal): {e}")

    # Optional: upload SRT captions (non-fatal)
    captions_uploaded = False
    if srt_path and os.path.exists(srt_path):
        try:
            youtube.captions().insert(
                part="snippet",
                body={
                    "snippet": {
                        "videoId": video_id,
                        "language": "en",
                        "name": "English",
                        "isDraft": False,
                    }
                },
                media_body=MediaFileUpload(srt_path, mimetype="application/octet-stream"),
                sync=False,
            ).execute()
            captions_uploaded = True
            print(f"[YouTube Episode] Captions uploaded.")
        except Exception as e:
            print(f"[YouTube Episode] Caption upload failed (non-fatal): {e}")

    # Add to playlist if one is configured (non-fatal, retries transient errors)
    if playlist_id:
        _yt_add_to_playlist(youtube, playlist_id, video_id)

    return {
        "platform":          "youtube",
        "video_id":          video_id,
        "url":               url,
        "scheduled":         is_scheduled,
        "captions_uploaded": captions_uploaded,
        "playlist_id":       playlist_id or None,
    }


# ── TikTok ─────────────────────────────────────────────────────────────────────

def _refresh_tiktok_token(client_key: str, client_secret: str, refresh_token: str) -> str:
    """
    Refresh the TikTok access token and return the new access token.
    TikTok Content Posting API v2 refresh endpoint.
    """
    import requests

    resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key": client_key,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "access_token" not in data:
        raise ValueError(f"TikTok token refresh failed: {data}")
    return data["access_token"]


def upload_tiktok(
    clip_path: str,
    title: str,
    tags: list[str] | None = None,
    privacy_level: str = "",
    disable_duet: bool = False,
    disable_comment: bool = False,
    disable_stitch: bool = False,
    brand_content: bool = False,
    brand_organic: bool = False,
    channel: str = "neilbound",
    post_mode: str = "",
) -> dict:
    """
    Upload a short MP4 clip to TikTok using the Content Posting API v2.

    TikTok does not support native scheduling — use the publish queue to time delivery.
    Refreshes the access token automatically before upload.

    Two posting modes (resolved from post_mode arg, then {CHANNEL}_TIKTOK_POST_MODE /
    TIKTOK_POST_MODE env, defaulting to "inbox"):
      - "inbox":  upload to the user's TikTok drafts (scope video.upload). The clip
                  lands in the TikTok app inbox and the user taps Post to publish.
                  No audit, no domain verification, no privacy level. Caption is added
                  in-app, so title/tags/privacy are ignored here.
      - "direct": Direct Post (scope video.publish). Posts immediately at privacy_level.
                  Unaudited apps may only use SELF_ONLY; needs Direct Post + domain
                  verification configured on the TikTok app.

    Args:
        clip_path:       Absolute path to the MP4 file.
        title:           Post caption (max 2200 chars). Hashtags appended from tags. (direct only)
        tags:            Optional hashtag strings (without '#'). Appended to caption. (direct only)
        privacy_level:   "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", or "SELF_ONLY". (direct only)
                         If empty, resolves to {CHANNEL}_TIKTOK_PRIVACY_LEVEL / the
                         TIKTOK_PRIVACY_LEVEL env var, defaulting to "SELF_ONLY".
        disable_duet:    Prevent other users from duetting this video. (direct only)
        disable_comment: Disable comments on this video. (direct only)
        disable_stitch:  Prevent other users from stitching this video. (direct only)
        brand_content:   True if this is paid/sponsored branded content. (direct only)
        brand_organic:   True if this organically promotes a brand you are affiliated with. (direct only)
        post_mode:       "inbox" or "direct" (see above).

    Returns:
        {"platform": "tiktok", "publish_id": str, "scheduled": False,
         "mode": str, "requires_manual_post": bool}
    """
    _require_publishing_enabled()

    import requests

    client_key    = _cred(channel, "TIKTOK_CLIENT_KEY")
    client_secret = _cred(channel, "TIKTOK_CLIENT_SECRET")
    access_token  = _cred(channel, "TIKTOK_ACCESS_TOKEN")
    refresh_token = _cred(channel, "TIKTOK_REFRESH_TOKEN")

    if not client_key:
        raise EnvironmentError(
            f"TIKTOK_CLIENT_KEY not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform tiktok --channel {channel}"
        )
    if not client_secret:
        raise EnvironmentError(
            f"TIKTOK_CLIENT_SECRET not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform tiktok --channel {channel}"
        )
    if not access_token:
        raise EnvironmentError(
            f"TIKTOK_ACCESS_TOKEN not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform tiktok --channel {channel}"
        )
    if not refresh_token:
        raise EnvironmentError(
            f"TIKTOK_REFRESH_TOKEN not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform tiktok --channel {channel}"
        )

    # Resolve posting mode. Default "inbox" (upload to drafts) — works on an unaudited
    # app with no domain verification. Override via arg or {CHANNEL}_TIKTOK_POST_MODE.
    if not post_mode:
        post_mode = _cred(channel, "TIKTOK_POST_MODE") or "inbox"
    post_mode = post_mode.lower()
    if post_mode not in ("inbox", "direct"):
        raise ValueError(f"Unknown TikTok post_mode '{post_mode}' (use 'inbox' or 'direct').")

    # Refresh token before upload to avoid mid-upload expiry
    print("[TikTok] Refreshing access token...")
    try:
        access_token = _refresh_tiktok_token(client_key, client_secret, refresh_token)
    except Exception as e:
        print(f"[TikTok] Token refresh failed, using existing token: {e}")

    video_size = os.path.getsize(clip_path)
    auth_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }
    source_info = {
        "source": "FILE_UPLOAD",
        "video_size": video_size,
        "chunk_size": video_size,   # single chunk for files ≤ 128 MB
        "total_chunk_count": 1,
    }

    if post_mode == "inbox":
        # Upload to the user's TikTok drafts/inbox. No post_info, no privacy, no audit.
        # The user opens TikTok and taps Post to finish (adds caption there).
        print("[TikTok] Initialising inbox upload (lands in TikTok drafts)...")
        init_resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/",
            json={"source_info": source_info},
            headers=auth_headers,
            timeout=30,
        )
    else:
        # Direct Post. Resolve privacy (default SELF_ONLY — the only level an unaudited
        # app may post) and preflight creator_info to fail fast on a bad privacy level.
        if not privacy_level:
            privacy_level = _cred(channel, "TIKTOK_PRIVACY_LEVEL") or "SELF_ONLY"

        print("[TikTok] Checking creator info...")
        info_resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/creator_info/query/",
            headers=auth_headers,
            timeout=30,
        )
        info_resp.raise_for_status()
        info_data = info_resp.json()
        if info_data.get("error", {}).get("code", "ok") != "ok":
            raise ValueError(f"TikTok creator_info query failed: {info_data}")
        available = info_data.get("data", {}).get("privacy_level_options", [])
        if available and privacy_level not in available:
            raise ValueError(
                f"TikTok rejects privacy_level '{privacy_level}' for channel '{channel}'. "
                f"Available options: {available}. Unaudited apps may only post SELF_ONLY "
                f"until the app passes TikTok's content-posting audit."
            )

        caption = title
        if tags:
            hashtags = " ".join(f"#{t.lstrip('#')}" for t in tags)
            caption = f"{caption} {hashtags}"

        print("[TikTok] Initialising direct post...")
        init_resp = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            json={
                "post_info": {
                    "title": caption,
                    "privacy_level": privacy_level,
                    "disable_duet": disable_duet,
                    "disable_comment": disable_comment,
                    "disable_stitch": disable_stitch,
                    "brand_content_toggle": brand_content,
                    "brand_organic_toggle": brand_organic,
                },
                "source_info": source_info,
            },
            headers=auth_headers,
            timeout=30,
        )

    init_resp.raise_for_status()
    init_data = init_resp.json()

    if init_data.get("error", {}).get("code", "ok") != "ok":
        raise ValueError(f"TikTok init failed: {init_data}")

    publish_id = init_data["data"]["publish_id"]
    upload_url = init_data["data"]["upload_url"]

    # Step 2: Upload file bytes (single chunk)
    print(f"[TikTok] Uploading {video_size / 1024 / 1024:.1f} MB...")
    with open(clip_path, "rb") as f:
        video_bytes = f.read()

    upload_resp = requests.put(
        upload_url,
        data=video_bytes,
        headers={
            "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
            "Content-Type": "video/mp4",
        },
        timeout=300,  # allow up to 5 min for upload
    )
    upload_resp.raise_for_status()
    if post_mode == "inbox":
        print(f"[TikTok] Uploaded to drafts. Open TikTok and tap Post to publish. "
              f"publish_id={publish_id}")
    else:
        print(f"[TikTok] Direct post complete. publish_id={publish_id}")

    return {
        "platform": "tiktok",
        "publish_id": publish_id,
        "scheduled": False,
        "mode": post_mode,
        "requires_manual_post": post_mode == "inbox",
    }


# ── Instagram Reels (Instagram Login API) ─────────────────────────────────────

def _refresh_instagram_token(access_token: str) -> str:
    """
    Refresh a long-lived Instagram token (resets the 60-day expiry).
    Facebook User Access Tokens (instagram_content_publish scope) are refreshed
    via graph.facebook.com; Instagram Login API tokens via graph.instagram.com.
    This function handles the Facebook token case.
    Returns the existing token unchanged — Facebook long-lived tokens last 60 days
    and are not refreshable via API; obtain a new one via setup_credentials.py.
    """
    # Facebook User Access Tokens cannot be programmatically refreshed.
    # The token is valid for ~60 days from issue. Return as-is.
    return access_token


def upload_instagram(
    clip_path: str,
    title: str,
    tags: list[str] | None = None,
    scheduled_time: str | None = None,
    share_to_feed: bool = True,
    channel: str = "neilbound",
) -> dict:
    """
    Upload a short MP4 clip to Instagram Reels via the Instagram Login API.

    Uses graph.instagram.com — no Facebook Page required. Works directly with
    Business and Creator accounts authorized via Instagram OAuth.

    Three-step process: create media container → upload bytes → publish (or schedule).

    Args:
        clip_path:      Absolute path to the MP4 file.
        title:          Caption for the Reel. Hashtags from tags are appended automatically.
        tags:           Optional hashtag strings (without '#'). Appended to caption.
        scheduled_time: ISO 8601 UTC string for scheduled publish.
                        If None, publishes immediately.
        share_to_feed:  Whether the Reel appears in the profile grid/feed (default True).
        channel:        Publishing channel: "neilbound" or "ilb". Default "neilbound".

    Returns:
        {"platform": "instagram", "media_id": str, "scheduled": bool}
    """
    _require_publishing_enabled()

    import requests

    access_token = _cred(channel, "INSTAGRAM_ACCESS_TOKEN")
    user_id      = _cred(channel, "INSTAGRAM_USER_ID")

    if not access_token:
        raise EnvironmentError(
            f"INSTAGRAM_ACCESS_TOKEN not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform instagram --channel {channel}"
        )
    if not user_id:
        raise EnvironmentError(
            f"INSTAGRAM_USER_ID not set for channel '{channel}'. "
            f"Run: python setup_credentials.py --platform instagram --channel {channel}"
        )

    import time

    # Facebook User Access Tokens use graph.facebook.com.
    # Instagram Login API tokens use graph.instagram.com.
    # The token in .env has instagram_content_publish scope (Facebook token).
    graph_base = "https://graph.facebook.com/v21.0"
    file_size  = os.path.getsize(clip_path)

    # Build caption
    caption = title
    if tags:
        hashtags = " ".join(f"#{t.lstrip('#')}" for t in tags)
        caption = f"{caption}\n\n{hashtags}"

    # Step 1: Create media container (resumable upload)
    print("[Instagram] Creating media container...")
    container_params: dict = {
        "media_type":    "REELS",
        "upload_type":   "resumable",
        "caption":       caption,
        "share_to_feed": "true" if share_to_feed else "false",
        "access_token":  access_token,
    }

    is_scheduled = False
    if scheduled_time:
        dt = datetime.fromisoformat(scheduled_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now_utc = datetime.now(tz=timezone.utc)
        # Instagram requires scheduled_publish_time to be at least 1 minute in the future.
        # If the scheduled time has already passed, publish immediately instead.
        if dt > now_utc:
            unix_ts = int(dt.timestamp())
            container_params["published"] = "false"
            container_params["scheduled_publish_time"] = str(unix_ts)
            is_scheduled = True
        # else: fall through and publish immediately (published param defaults to true)

    container_resp = requests.post(
        f"{graph_base}/{user_id}/media",
        params=container_params,
        timeout=30,
    )
    container_resp.raise_for_status()
    container_data = container_resp.json()

    if "error" in container_data:
        raise ValueError(f"Instagram container creation failed: {container_data['error']}")

    container_id = container_data["id"]
    upload_uri   = container_data.get("uri")

    if not upload_uri:
        raise ValueError(
            f"Instagram did not return an upload URI. Response: {container_data}"
        )

    # Step 2: Upload file bytes
    print(f"[Instagram] Uploading {file_size / 1024 / 1024:.1f} MB...")
    with open(clip_path, "rb") as f:
        video_bytes = f.read()

    upload_resp = requests.post(
        upload_uri,
        data=video_bytes,
        headers={
            "Authorization": f"OAuth {access_token}",
            "offset":        "0",
            "file_size":     str(file_size),
            "Content-Type":  "application/octet-stream",
        },
        timeout=300,
    )
    upload_resp.raise_for_status()
    print("[Instagram] File uploaded. Waiting for processing...")

    # Step 3: Poll container status until FINISHED (Meta processes video server-side)
    for attempt in range(24):   # up to ~2 minutes
        time.sleep(5)
        status_resp = requests.get(
            f"{graph_base}/{container_id}",
            params={"fields": "status_code", "access_token": access_token},
            timeout=15,
        )
        status_resp.raise_for_status()
        status_data = status_resp.json()
        status_code = status_data.get("status_code", "")
        print(f"[Instagram] Container status: {status_code} (attempt {attempt + 1})")
        if status_code == "FINISHED":
            break
        if status_code in ("ERROR", "EXPIRED"):
            raise ValueError(f"Instagram container processing failed: {status_data}")
    else:
        raise TimeoutError("Instagram container did not finish processing within 2 minutes.")

    # Step 4: Publish (or leave scheduled)
    if is_scheduled:
        print(f"[Instagram] Reel scheduled. container_id={container_id}")
        media_id = container_id
    else:
        print("[Instagram] Publishing Reel...")
        publish_resp = requests.post(
            f"{graph_base}/{user_id}/media_publish",
            params={
                "creation_id":  container_id,
                "access_token": access_token,
            },
            timeout=60,
        )
        publish_resp.raise_for_status()
        publish_data = publish_resp.json()

        if "error" in publish_data:
            raise ValueError(f"Instagram publish failed: {publish_data['error']}")

        media_id = publish_data.get("id", container_id)
        print(f"[Instagram] Published. media_id={media_id}")

    return {
        "platform": "instagram",
        "media_id":  media_id,
        "scheduled": is_scheduled,
    }
