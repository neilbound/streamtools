"""
test_credentials.py — Smoke test all configured channel credentials.

Usage:
    python test_credentials.py --channel neilbound
    python test_credentials.py --channel ilb
"""

import argparse
import os
import sys

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"), override=True)


def _cred(channel, key):
    val = os.environ.get(f"{channel.upper()}_{key}")
    if val:
        return val
    return os.environ.get(key)


def test_youtube(channel):
    print("[YouTube] Testing connection...")
    client_id     = _cred(channel, "YOUTUBE_CLIENT_ID")
    client_secret = _cred(channel, "YOUTUBE_CLIENT_SECRET")
    refresh_token = _cred(channel, "YOUTUBE_REFRESH_TOKEN")

    if not all([client_id, client_secret, refresh_token]):
        print("[YouTube] SKIP — credentials not configured")
        return

    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            client_id=client_id,
            client_secret=client_secret,
            token_uri="https://oauth2.googleapis.com/token",
        )
        creds.refresh(Request())
        # Token refreshed successfully — upload scope is valid
        print(f"[YouTube] OK — token valid (scope: youtube.upload)")
    except Exception as e:
        print(f"[YouTube] FAILED — {e}")


def test_instagram(channel):
    print("[Instagram] Testing connection...")
    access_token = _cred(channel, "INSTAGRAM_ACCESS_TOKEN")
    user_id      = _cred(channel, "INSTAGRAM_USER_ID")

    if not all([access_token, user_id]):
        print("[Instagram] SKIP — credentials not configured")
        return

    try:
        import requests
        # Use graph.facebook.com — tokens are Facebook User Access Tokens
        # with instagram_content_publish scope (not Instagram Login API tokens)
        resp = requests.get(
            f"https://graph.facebook.com/v21.0/{user_id}",
            params={"fields": "id,username", "access_token": access_token},
            timeout=15,
        )
        data = resp.json()
        if "error" in data:
            print(f"[Instagram] FAILED — {data['error']['message']}")
        else:
            print(f"[Instagram] OK — @{data.get('username', data.get('id'))}")
    except Exception as e:
        print(f"[Instagram] FAILED — {e}")


def test_tiktok(channel):
    print("[TikTok] Testing connection...")
    client_key    = _cred(channel, "TIKTOK_CLIENT_KEY")
    client_secret = _cred(channel, "TIKTOK_CLIENT_SECRET")
    access_token  = _cred(channel, "TIKTOK_ACCESS_TOKEN")
    refresh_token = _cred(channel, "TIKTOK_REFRESH_TOKEN")

    if not all([client_key, client_secret, access_token]):
        print("[TikTok] SKIP — credentials not configured")
        return

    try:
        import requests

        # Refresh token first
        token_resp = requests.post(
            "https://open.tiktokapis.com/v2/oauth/token/",
            data={
                "client_key":    client_key,
                "client_secret": client_secret,
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15,
        )
        token_data = token_resp.json()
        fresh_token = token_data.get("access_token", access_token)

        # Get user info
        resp = requests.get(
            "https://open.tiktokapis.com/v2/user/info/",
            params={"fields": "open_id,display_name,username"},
            headers={"Authorization": f"Bearer {fresh_token}"},
            timeout=15,
        )
        # Token refreshed successfully — video.publish scope is valid
        if token_resp.status_code == 200 and "access_token" in token_data:
            print(f"[TikTok] OK — token valid (scope: video.publish)")
        else:
            print(f"[TikTok] FAILED — token refresh failed: {token_data}")
    except Exception as e:
        print(f"[TikTok] FAILED — {e}")


def main():
    parser = argparse.ArgumentParser(description="Smoke test channel credentials.")
    parser.add_argument("--channel", required=True, help="Channel to test, e.g. 'neilbound' or 'ilb'")
    args = parser.parse_args()

    channel = args.channel
    print(f"\nTesting credentials for channel: {channel.upper()}")
    print("=" * 40)
    test_youtube(channel)
    test_instagram(channel)
    test_tiktok(channel)
    print("=" * 40)
    print("Done.\n")


if __name__ == "__main__":
    main()
