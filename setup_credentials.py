"""
setup_credentials.py — One-time OAuth setup for social media publishing platforms.

Usage:
    python setup_credentials.py --platform youtube
    python setup_credentials.py --platform tiktok
    python setup_credentials.py --platform instagram

This script writes credentials directly to your .env file.
"""

import argparse
import json
import os
import re
import sys
import urllib.parse

from dotenv import load_dotenv

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
load_dotenv(_ENV_PATH, override=True)


def _write_env(values: dict[str, str]) -> None:
    """Write or update key=value pairs in the .env file."""
    # Read existing content
    if os.path.exists(_ENV_PATH):
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Ensure the last line ends with a newline
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
    else:
        lines = []

    # Update existing keys or append new ones
    updated = set()
    new_lines = []
    for line in lines:
        match = re.match(r"^([A-Z_]+)\s*=", line)
        if match and match.group(1) in values:
            key = match.group(1)
            new_lines.append(f"{key}={values[key]}\n")
            updated.add(key)
        else:
            new_lines.append(line)

    # Append any keys that weren't already in the file
    for key, val in values.items():
        if key not in updated:
            new_lines.append(f"{key}={val}\n")

    with open(_ENV_PATH, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

    print()
    print("=" * 60)
    print("SUCCESS — Written to .env:")
    print("=" * 60)
    for key, val in values.items():
        preview = val[:12] + "..." if len(val) > 12 else val
        print(f"  {key}={preview}")
    print()


# ── YouTube OAuth setup ────────────────────────────────────────────────────────

def setup_youtube(channel: str = "NEILBOUND"):
    """
    Interactive OAuth 2.0 setup for YouTube Data API v3.
    Opens a local browser flow on port 8080 to obtain a refresh token.
    """
    print("=" * 60)
    print("YouTube OAuth2 Setup")
    print("=" * 60)
    print()
    print("You need a Google Cloud project with the YouTube Data API v3 enabled.")
    print("Create OAuth 2.0 credentials (Desktop app type) and note your")
    print("Client ID and Client Secret.")
    print()

    # Accept either a path to client_secrets.json or direct values
    secrets_path = input(
        "Path to client_secrets.json (or press Enter to enter values manually): "
    ).strip()

    if secrets_path:
        if not os.path.exists(secrets_path):
            print(f"File not found: {secrets_path}")
            sys.exit(1)
        with open(secrets_path, "r", encoding="utf-8") as f:
            secrets = json.load(f)
        # Supports both "installed" and "web" credential types
        cred_block = secrets.get("installed") or secrets.get("web", {})
        client_id     = cred_block.get("client_id", "")
        client_secret = cred_block.get("client_secret", "")
        client_config = secrets
    else:
        client_id     = input("Client ID: ").strip()
        client_secret = input("Client Secret: ").strip()
        if not client_id or not client_secret:
            print("Client ID and Client Secret are required.")
            sys.exit(1)
        client_config = {
            "installed": {
                "client_id":                  client_id,
                "client_secret":              client_secret,
                "redirect_uris":              ["http://localhost:8080"],
                "auth_uri":                   "https://accounts.google.com/o/oauth2/auth",
                "token_uri":                  "https://oauth2.googleapis.com/token",
                "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            }
        }

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        print(
            "google-auth-oauthlib is not installed.\n"
            "Run: pip install google-auth-oauthlib"
        )
        sys.exit(1)

    scopes = [
        "https://www.googleapis.com/auth/youtube.upload",  # upload videos
        "https://www.googleapis.com/auth/youtube",         # manage playlists (add Shorts to playlist)
    ]

    print()
    print("Opening browser for Google authorization (port 8080)...")
    print("If the browser does not open automatically, copy the URL printed below.")
    print()

    flow = InstalledAppFlow.from_client_config(client_config, scopes=scopes)
    creds = flow.run_local_server(port=8080)

    if not creds.refresh_token:
        print(
            "\nERROR: No refresh token was returned. This usually means you have already "
            "authorized this app. Revoke access at https://myaccount.google.com/permissions "
            "and run this script again."
        )
        sys.exit(1)

    _write_env({
        f"{channel}_YOUTUBE_CLIENT_ID":     creds.client_id,
        f"{channel}_YOUTUBE_CLIENT_SECRET": creds.client_secret,
        f"{channel}_YOUTUBE_REFRESH_TOKEN": creds.refresh_token,
        "PUBLISHING_ENABLED":               "true",
    })


# ── TikTok OAuth setup ─────────────────────────────────────────────────────────

def _find_existing_tiktok_app(exclude_channel: str = "") -> tuple[str, str, str] | None:
    """
    Find an already-configured TikTok app's Client Key/Secret in the environment so a
    new channel can authorize the SAME dev app without re-entering credentials.

    One TikTok dev app can be authorized by many accounts; only the OAuth (access /
    refresh tokens) differs per account. Returns (channel_prefix, key, secret) or None.
    """
    exclude = (exclude_channel or "").upper()
    for env_key, val in os.environ.items():
        if env_key.endswith("_TIKTOK_CLIENT_KEY") and val:
            prefix = env_key[: -len("_TIKTOK_CLIENT_KEY")]
            if prefix == exclude:
                continue
            secret = os.environ.get(f"{prefix}_TIKTOK_CLIENT_SECRET", "")
            if secret:
                return prefix, val, secret
    return None


def setup_tiktok(channel: str = "NEILBOUND"):
    """
    Interactive OAuth 2.0 setup for TikTok Content Posting API v2.
    Guides the user through the authorization code flow manually.
    """
    print("=" * 60)
    print("TikTok OAuth2 Setup")
    print("=" * 60)
    print()
    print("You need a TikTok for Developers app with the")
    print("'video.publish' scope enabled.")
    print("Find your Client Key and Client Secret in the app dashboard.")
    print()

    existing = _find_existing_tiktok_app(exclude_channel=channel)
    if existing:
        ref_prefix, ref_key, _ = existing
        print(f"Found an existing TikTok app under {ref_prefix}_TIKTOK_* "
              f"(Client Key {ref_key[:6]}...).")
        print("Press Enter at both prompts to reuse it for this channel.")
        print("(One dev app can be authorized by multiple TikTok accounts.)")
        print()

    client_key    = input("Client Key: ").strip()
    client_secret = input("Client Secret: ").strip()

    if existing:
        ref_prefix, ref_key, ref_secret = existing
        client_key    = client_key or ref_key
        client_secret = client_secret or ref_secret
        if client_key == ref_key:
            print(f"Reusing app credentials from {ref_prefix}_TIKTOK_*.")

    if not client_key or not client_secret:
        print("Client Key and Client Secret are required.")
        sys.exit(1)

    # Redirect URI — must be registered in the TikTok app dashboard and sit under a
    # verified URL property. TikTok does not support localhost; the page need not exist
    # (the auth code appears in the URL bar after TikTok redirects). Override with the
    # TIKTOK_REDIRECT_URI env var if your verified domain differs.
    redirect_uri = os.environ.get("TIKTOK_REDIRECT_URI", "https://neilbound.com/tiktok-auth")
    # Inbox upload (upload to TikTok drafts) needs video.upload. Direct Post needs
    # video.publish, which requires Direct Post + domain verification on the app.
    scope        = "video.upload"
    csrf_state   = "streamtools_setup"

    import base64
    import hashlib
    import os as _os
    import urllib.parse

    # PKCE — TikTok requires code_challenge since 2024
    code_verifier  = base64.urlsafe_b64encode(_os.urandom(40)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    auth_params = {
        "client_key":            client_key,
        "scope":                 scope,
        "response_type":         "code",
        "redirect_uri":          redirect_uri,
        "state":                 csrf_state,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    }
    auth_url = (
        "https://www.tiktok.com/v2/auth/authorize/?"
        + urllib.parse.urlencode(auth_params)
    )

    print()
    print("Opening browser for TikTok authorization...")
    print("If it doesn't open, visit this URL manually:")
    print()
    print(auth_url)
    print()

    import webbrowser
    webbrowser.open(auth_url)

    print()
    print("After authorizing, TikTok will redirect to a page that may not load.")
    print("That's fine — copy the full URL from your browser's address bar.")
    print("It will look like:")
    print(f"  {redirect_uri}?code=XXXX&state=streamtools_setup")
    print()

    callback_url = input("Paste the full redirect URL here: ").strip()
    if not callback_url:
        print("No URL provided.")
        sys.exit(1)

    parsed_cb = urllib.parse.urlparse(callback_url)
    params_cb  = urllib.parse.parse_qs(parsed_cb.query)

    if "error" in params_cb:
        print(f"Authorization failed: {params_cb['error'][0]}")
        sys.exit(1)

    if "code" not in params_cb:
        print(f"No code found in URL: {callback_url}")
        sys.exit(1)

    code = params_cb["code"][0]
    print("Authorization code received. Exchanging for tokens...")

    # Exchange code for tokens
    try:
        import requests
    except ImportError:
        print("requests is not installed. Run: pip install requests")
        sys.exit(1)

    token_resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key":     client_key,
            "client_secret":  client_secret,
            "code":           code,
            "grant_type":     "authorization_code",
            "redirect_uri":   redirect_uri,
            "code_verifier":  code_verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )

    if token_resp.status_code != 200:
        print(f"Token exchange failed (HTTP {token_resp.status_code}): {token_resp.text}")
        sys.exit(1)

    token_data = token_resp.json()
    if "access_token" not in token_data:
        print(f"Token exchange failed: {token_data}")
        sys.exit(1)

    access_token  = token_data["access_token"]
    refresh_token = token_data.get("refresh_token", "")

    _write_env({
        f"{channel}_TIKTOK_CLIENT_KEY":     client_key,
        f"{channel}_TIKTOK_CLIENT_SECRET":  client_secret,
        f"{channel}_TIKTOK_ACCESS_TOKEN":   access_token,
        f"{channel}_TIKTOK_REFRESH_TOKEN":  refresh_token,
        "PUBLISHING_ENABLED":               "true",
    })


# ── Instagram Login API setup ──────────────────────────────────────────────────

def setup_instagram(channel: str = "NEILBOUND"):
    """
    Interactive setup for Instagram using the Instagram Login API.

    Does NOT require a Facebook Page — works directly with Business and Creator
    Instagram accounts via Instagram OAuth (api.instagram.com).

    Pre-requisites in your Meta app:
      1. Add the 'Instagram' product (Instagram Login)
      2. Add https://neilbound.me/instagram-auth as a redirect URI in
         Instagram Login → Settings → Valid OAuth Redirect URIs
    """
    print("=" * 60)
    print("Instagram Login API Setup")
    print("=" * 60)
    print()
    print("Pre-requisites:")
    print("  1. Your Meta app has the 'Instagram' (Instagram Login) product added")
    print("  2. https://neilbound.me/instagram-auth is in the app's")
    print("     Instagram Login > Settings > Valid OAuth Redirect URIs")
    print()

    app_id     = input("Your Meta App ID: ").strip()
    app_secret = input("Your Meta App Secret: ").strip()

    if not app_id or not app_secret:
        print("App ID and App Secret are required.")
        sys.exit(1)

    try:
        import requests
    except ImportError:
        print("requests is not installed. Run: pip install requests")
        sys.exit(1)

    redirect_uri = "https://neilbound.me/instagram-auth"
    scopes       = "instagram_business_basic,instagram_business_content_publish"

    auth_params = {
        "client_id":     app_id,
        "redirect_uri":  redirect_uri,
        "scope":         scopes,
        "response_type": "code",
    }
    auth_url = "https://api.instagram.com/oauth/authorize?" + urllib.parse.urlencode(auth_params)

    print()
    print("Opening browser for Instagram authorization...")
    print("If it doesn't open, visit this URL manually:")
    print()
    print(auth_url)
    print()

    import webbrowser
    webbrowser.open(auth_url)

    print()
    print("After authorizing, Instagram will redirect to a page that may not load.")
    print("That's fine — copy the full URL from your browser's address bar.")
    print("It will look like:")
    print("  https://neilbound.me/instagram-auth?code=XXXX#_")
    print()

    callback_url = input("Paste the full redirect URL here: ").strip()
    if not callback_url:
        print("No URL provided.")
        sys.exit(1)

    parsed_cb = urllib.parse.urlparse(callback_url)
    params_cb  = urllib.parse.parse_qs(parsed_cb.query)

    if "error" in params_cb:
        print(f"Authorization failed: {params_cb.get('error_description', params_cb['error'])[0]}")
        sys.exit(1)

    if "code" not in params_cb:
        print(f"No code found in URL: {callback_url}")
        sys.exit(1)

    code = params_cb["code"][0]
    # Strip #_ suffix Instagram appends
    code = code.split("#")[0]

    print("Authorization code received. Exchanging for short-lived token...")

    # Step 1: Exchange code for short-lived token
    token_resp = requests.post(
        "https://api.instagram.com/oauth/access_token",
        data={
            "client_id":     app_id,
            "client_secret": app_secret,
            "grant_type":    "authorization_code",
            "redirect_uri":  redirect_uri,
            "code":          code,
        },
        timeout=30,
    )

    if token_resp.status_code != 200:
        print(f"Token exchange failed (HTTP {token_resp.status_code}): {token_resp.text}")
        sys.exit(1)

    token_data = token_resp.json()
    if "access_token" not in token_data:
        print(f"Token exchange failed: {token_data}")
        sys.exit(1)

    short_token   = token_data["access_token"]
    instagram_uid = str(token_data.get("user_id", ""))

    # Step 2: Exchange for long-lived token (~60 days)
    print("Exchanging for long-lived token...")
    ll_resp = requests.get(
        "https://graph.instagram.com/access_token",
        params={
            "grant_type":        "ig_exchange_token",
            "client_secret":     app_secret,
            "access_token":      short_token,
        },
        timeout=30,
    )

    if ll_resp.status_code != 200:
        print(f"Long-lived token exchange failed (HTTP {ll_resp.status_code}): {ll_resp.text}")
        sys.exit(1)

    ll_data = ll_resp.json()
    if "access_token" not in ll_data:
        print(f"Long-lived token exchange failed: {ll_data}")
        sys.exit(1)

    long_lived_token = ll_data["access_token"]

    # Step 3: Get Instagram user ID and username if not in token response
    if not instagram_uid:
        me_resp = requests.get(
            "https://graph.instagram.com/me",
            params={"fields": "id,username", "access_token": long_lived_token},
            timeout=30,
        )
        me_data = me_resp.json()
        instagram_uid = str(me_data.get("id", ""))
        username      = me_data.get("username", "")
        print(f"Instagram account: @{username} (id: {instagram_uid})")
    else:
        print(f"Instagram user ID: {instagram_uid}")

    _write_env({
        f"{channel}_INSTAGRAM_APP_ID":       app_id,
        f"{channel}_INSTAGRAM_APP_SECRET":   app_secret,
        f"{channel}_INSTAGRAM_ACCESS_TOKEN": long_lived_token,
        f"{channel}_INSTAGRAM_USER_ID":      instagram_uid,
        "PUBLISHING_ENABLED":                "true",
    })
    print()
    print("NOTE: Long-lived tokens expire after ~60 days.")
    print("      To refresh: python setup_credentials.py --platform instagram --channel", channel.lower())
    print()


# ── CLI entry point ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="One-time OAuth credential setup for social media publishing."
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=["youtube", "tiktok", "instagram"],
        help="Platform to configure.",
    )
    parser.add_argument(
        "--channel",
        required=True,
        help="Channel identifier, e.g. 'neilbound' or 'ilb'. "
             "Credentials are stored as CHANNEL_PLATFORM_KEY in .env.",
    )
    args = parser.parse_args()

    channel = args.channel.upper()

    if args.platform == "youtube":
        setup_youtube(channel)
    elif args.platform == "tiktok":
        setup_tiktok(channel)
    elif args.platform == "instagram":
        setup_instagram(channel)


if __name__ == "__main__":
    main()
