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
from datetime import datetime, timezone


# ── Guard helper ───────────────────────────────────────────────────────────────

def _require_publishing_enabled():
    """Raise a clear error if PUBLISHING_ENABLED is not set to 'true'."""
    if os.environ.get("PUBLISHING_ENABLED", "").lower() != "true":
        raise EnvironmentError(
            "Publishing is disabled. Set PUBLISHING_ENABLED=true in .env, "
            "then run: python setup_credentials.py --platform <platform>"
        )


# ── YouTube Shorts ─────────────────────────────────────────────────────────────

def upload_youtube(
    clip_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    scheduled_time: str | None = None,
) -> dict:
    """
    Upload a short MP4 clip to YouTube Shorts.

    Refreshes credentials using the stored refresh token (no interactive OAuth flow).
    The clip is published immediately (public) or scheduled (private + publishAt).

    Args:
        clip_path:      Absolute path to the MP4 file.
        title:          Video title (max 100 chars).
        description:    Video description. '#Shorts' is appended automatically if absent.
        tags:           Optional list of tag strings.
        scheduled_time: ISO 8601 UTC string e.g. '2026-05-16T15:00:00+00:00'.
                        If None, publishes immediately as public.

    Returns:
        {"platform": "youtube", "video_id": str, "url": str, "scheduled": bool}
    """
    _require_publishing_enabled()

    # Validate credentials
    client_id     = os.environ.get("YOUTUBE_CLIENT_ID")
    client_secret = os.environ.get("YOUTUBE_CLIENT_SECRET")
    refresh_token = os.environ.get("YOUTUBE_REFRESH_TOKEN")

    if not client_id:
        raise EnvironmentError(
            "YOUTUBE_CLIENT_ID not set. Run: python setup_credentials.py --platform youtube"
        )
    if not client_secret:
        raise EnvironmentError(
            "YOUTUBE_CLIENT_SECRET not set. Run: python setup_credentials.py --platform youtube"
        )
    if not refresh_token:
        raise EnvironmentError(
            "YOUTUBE_REFRESH_TOKEN not set. Run: python setup_credentials.py --platform youtube"
        )

    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    # Build credentials from refresh token — refresh immediately to get a fresh access token
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
        publish_at_str = dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        privacy = "private"
        is_scheduled = True
    else:
        publish_at_str = None
        privacy = "public"
        is_scheduled = False

    status_body = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": False,
    }
    if publish_at_str:
        status_body["publishAt"] = publish_at_str

    body = {
        "snippet": {
            "title": title,
            "description": body_description,
            "tags": tags or [],
            "categoryId": "22",  # People & Blogs — sensible default for podcasts
        },
        "status": status_body,
    }

    media = MediaFileUpload(
        clip_path,
        mimetype="video/mp4",
        resumable=True,
        chunksize=1024 * 1024 * 5,  # 5 MB chunks
    )

    print(f"[YouTube] Uploading: {clip_path}")
    request = youtube.videos().insert(
        part="snippet,status",
        body=body,
        media_body=media,
    )

    response = None
    while response is None:
        status_obj, response = request.next_chunk()
        if status_obj:
            pct = int(status_obj.progress() * 100)
            print(f"[YouTube] Upload progress: {pct}%")

    video_id = response["id"]
    url = f"https://www.youtube.com/shorts/{video_id}"
    print(f"[YouTube] Upload complete: {url}")

    return {
        "platform": "youtube",
        "video_id": video_id,
        "url": url,
        "scheduled": is_scheduled,
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
) -> dict:
    """
    Upload a short MP4 clip to TikTok using the Content Posting API v2.

    TikTok does not support native scheduling — use the publish queue to time delivery.
    Refreshes the access token automatically before upload.

    Args:
        clip_path: Absolute path to the MP4 file.
        title:     Post caption / title (max 2200 chars for TikTok).
        tags:      Optional hashtag strings (without '#'). Appended to title.

    Returns:
        {"platform": "tiktok", "publish_id": str, "scheduled": False}
    """
    _require_publishing_enabled()

    import requests

    client_key    = os.environ.get("TIKTOK_CLIENT_KEY")
    client_secret = os.environ.get("TIKTOK_CLIENT_SECRET")
    access_token  = os.environ.get("TIKTOK_ACCESS_TOKEN")
    refresh_token = os.environ.get("TIKTOK_REFRESH_TOKEN")

    if not client_key:
        raise EnvironmentError(
            "TIKTOK_CLIENT_KEY not set. Run: python setup_credentials.py --platform tiktok"
        )
    if not client_secret:
        raise EnvironmentError(
            "TIKTOK_CLIENT_SECRET not set. Run: python setup_credentials.py --platform tiktok"
        )
    if not access_token:
        raise EnvironmentError(
            "TIKTOK_ACCESS_TOKEN not set. Run: python setup_credentials.py --platform tiktok"
        )
    if not refresh_token:
        raise EnvironmentError(
            "TIKTOK_REFRESH_TOKEN not set. Run: python setup_credentials.py --platform tiktok"
        )

    # Refresh token before upload to avoid mid-upload expiry
    print("[TikTok] Refreshing access token...")
    try:
        access_token = _refresh_tiktok_token(client_key, client_secret, refresh_token)
    except Exception as e:
        print(f"[TikTok] Token refresh failed, using existing token: {e}")

    # Build caption with hashtags
    caption = title
    if tags:
        hashtags = " ".join(f"#{t.lstrip('#')}" for t in tags)
        caption = f"{caption} {hashtags}"

    video_size = os.path.getsize(clip_path)

    # Step 1: Init upload
    print("[TikTok] Initialising upload...")
    init_resp = requests.post(
        "https://open.tiktokapis.com/v2/post/publish/video/init/",
        json={
            "post_info": {
                "title": caption,
                "privacy_level": "PUBLIC_TO_EVERYONE",
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": video_size,
                "chunk_size": video_size,   # single chunk for files ≤ 128 MB
                "total_chunk_count": 1,
            },
        },
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
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
    print(f"[TikTok] Upload complete. publish_id={publish_id}")

    return {
        "platform": "tiktok",
        "publish_id": publish_id,
        "scheduled": False,
    }


# ── Instagram Reels ────────────────────────────────────────────────────────────

def upload_instagram(
    clip_path: str,
    title: str,
    scheduled_time: str | None = None,
) -> dict:
    """
    Upload a short MP4 clip to Instagram Reels via the Meta Graph API (v19.0).

    Uses a three-step process: create media container → upload bytes → publish (or schedule).

    Args:
        clip_path:      Absolute path to the MP4 file.
        title:          Caption for the Reel.
        scheduled_time: ISO 8601 UTC string for scheduled publish.
                        If None, publishes immediately.

    Returns:
        {"platform": "instagram", "media_id": str, "scheduled": bool}
    """
    _require_publishing_enabled()

    import requests

    access_token = os.environ.get("INSTAGRAM_ACCESS_TOKEN")
    user_id      = os.environ.get("INSTAGRAM_USER_ID")

    if not access_token:
        raise EnvironmentError(
            "INSTAGRAM_ACCESS_TOKEN not set. Run: python setup_credentials.py --platform instagram"
        )
    if not user_id:
        raise EnvironmentError(
            "INSTAGRAM_USER_ID not set. Run: python setup_credentials.py --platform instagram"
        )

    graph_base = "https://graph.facebook.com/v19.0"
    file_size  = os.path.getsize(clip_path)

    # Step 1: Create media container
    print("[Instagram] Creating media container...")
    container_params: dict = {
        "media_type": "REELS",
        "upload_type": "resumable",
        "caption": title,
        "access_token": access_token,
    }

    is_scheduled = False
    if scheduled_time:
        dt = datetime.fromisoformat(scheduled_time)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        unix_ts = int(dt.timestamp())
        container_params["published"] = "false"
        container_params["scheduled_publish_time"] = str(unix_ts)
        is_scheduled = True

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

    # Step 2: Upload file bytes to upload_uri
    print(f"[Instagram] Uploading {file_size / 1024 / 1024:.1f} MB to resumable URI...")
    with open(clip_path, "rb") as f:
        video_bytes = f.read()

    upload_resp = requests.post(
        upload_uri,
        data=video_bytes,
        headers={
            "offset": "0",
            "file_size": str(file_size),
            "Content-Type": "application/octet-stream",
        },
        timeout=300,
    )
    upload_resp.raise_for_status()
    print("[Instagram] File uploaded.")

    # Step 3: Publish (or leave scheduled)
    if is_scheduled:
        # Scheduling is set on the container; no separate publish call needed
        print(f"[Instagram] Reel scheduled. container_id={container_id}")
        media_id = container_id
    else:
        print("[Instagram] Publishing Reel...")
        publish_resp = requests.post(
            f"{graph_base}/{user_id}/media_publish",
            params={
                "creation_id": container_id,
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
        "media_id": media_id,
        "scheduled": is_scheduled,
    }
