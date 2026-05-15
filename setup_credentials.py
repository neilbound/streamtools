"""
setup_credentials.py — One-time OAuth setup for social media publishing platforms.

Usage:
    python setup_credentials.py --platform youtube
    python setup_credentials.py --platform tiktok
    python setup_credentials.py --platform instagram

This script is interactive and prints the environment variable lines you need to
add to your .env file. It does NOT write to .env directly.
"""

import argparse
import json
import os
import sys

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)


# ── YouTube OAuth setup ────────────────────────────────────────────────────────

def setup_youtube():
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

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]

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

    print()
    print("=" * 60)
    print("SUCCESS — Add these lines to your .env file:")
    print("=" * 60)
    print(f"YOUTUBE_CLIENT_ID={creds.client_id}")
    print(f"YOUTUBE_CLIENT_SECRET={creds.client_secret}")
    print(f"YOUTUBE_REFRESH_TOKEN={creds.refresh_token}")
    print("PUBLISHING_ENABLED=true")
    print()


# ── TikTok OAuth setup ─────────────────────────────────────────────────────────

def setup_tiktok():
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

    client_key    = input("Client Key: ").strip()
    client_secret = input("Client Secret: ").strip()

    if not client_key or not client_secret:
        print("Client Key and Client Secret are required.")
        sys.exit(1)

    # Build authorization URL
    redirect_uri = "https://www.tiktok.com/auth/tiktok/callback"  # TikTok's own callback
    scope        = "video.publish"
    csrf_state   = "streamtools_setup"

    auth_url = (
        f"https://www.tiktok.com/v2/auth/authorize/"
        f"?client_key={client_key}"
        f"&scope={scope}"
        f"&response_type=code"
        f"&redirect_uri={redirect_uri}"
        f"&state={csrf_state}"
    )

    print()
    print("Visit this URL in your browser to authorize the app:")
    print()
    print(auth_url)
    print()
    print(
        "After authorizing, you'll be redirected to a URL like:\n"
        "  https://www.tiktok.com/auth/tiktok/callback?code=XXXX&state=streamtools_setup\n"
        "Copy the 'code' parameter value from that URL."
    )
    print()

    code = input("Paste the authorization code here: ").strip()
    if not code:
        print("Authorization code is required.")
        sys.exit(1)

    # Exchange code for tokens
    try:
        import requests
    except ImportError:
        print("requests is not installed. Run: pip install requests")
        sys.exit(1)

    print("Exchanging authorization code for tokens...")
    token_resp = requests.post(
        "https://open.tiktokapis.com/v2/oauth/token/",
        data={
            "client_key":     client_key,
            "client_secret":  client_secret,
            "code":           code,
            "grant_type":     "authorization_code",
            "redirect_uri":   redirect_uri,
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

    print()
    print("=" * 60)
    print("SUCCESS — Add these lines to your .env file:")
    print("=" * 60)
    print(f"TIKTOK_CLIENT_KEY={client_key}")
    print(f"TIKTOK_CLIENT_SECRET={client_secret}")
    print(f"TIKTOK_ACCESS_TOKEN={access_token}")
    print(f"TIKTOK_REFRESH_TOKEN={refresh_token}")
    print("PUBLISHING_ENABLED=true")
    print()


# ── Instagram (Meta Graph API) setup ───────────────────────────────────────────

def setup_instagram():
    """
    Interactive setup for Instagram Graph API using a Meta long-lived access token.
    Guides the user through the Meta Graph API Explorer to get a short-lived token,
    then exchanges it for a long-lived token and finds the Instagram Business account ID.
    """
    print("=" * 60)
    print("Instagram (Meta Graph API) Setup")
    print("=" * 60)
    print()
    print("Step-by-step instructions:")
    print()
    print("1. Go to https://developers.facebook.com/apps/ and create or open your app.")
    print("   Your app type must be 'Business' and have Instagram Graph API added.")
    print()
    print("2. Open https://developers.facebook.com/tools/explorer/")
    print()
    print("3. In the top right, select your app from the 'Meta App' dropdown.")
    print()
    print("4. Click 'Generate Access Token' and grant these permissions:")
    print("   - instagram_basic")
    print("   - instagram_content_publish")
    print("   - pages_read_engagement")
    print("   - pages_show_list")
    print()
    print("5. Copy the short-lived token from the 'Access Token' field.")
    print()

    app_id     = input("Your Meta App ID: ").strip()
    app_secret = input("Your Meta App Secret: ").strip()
    short_token = input("Paste the short-lived access token: ").strip()

    if not app_id or not app_secret or not short_token:
        print("App ID, App Secret, and short-lived token are all required.")
        sys.exit(1)

    try:
        import requests
    except ImportError:
        print("requests is not installed. Run: pip install requests")
        sys.exit(1)

    # Exchange short-lived token for long-lived token (valid ~60 days)
    print("\nExchanging short-lived token for long-lived token...")
    exchange_resp = requests.get(
        "https://graph.facebook.com/v19.0/oauth/access_token",
        params={
            "grant_type":        "fb_exchange_token",
            "client_id":         app_id,
            "client_secret":     app_secret,
            "fb_exchange_token": short_token,
        },
        timeout=30,
    )

    if exchange_resp.status_code != 200:
        print(f"Token exchange failed (HTTP {exchange_resp.status_code}): {exchange_resp.text}")
        sys.exit(1)

    exchange_data = exchange_resp.json()
    if "access_token" not in exchange_data:
        print(f"Token exchange failed: {exchange_data}")
        sys.exit(1)

    long_lived_token = exchange_data["access_token"]
    print("Long-lived token obtained.")

    # Fetch Facebook pages to locate the Instagram business account
    print("Looking up connected Instagram Business account...")
    pages_resp = requests.get(
        "https://graph.facebook.com/v19.0/me/accounts",
        params={"access_token": long_lived_token, "fields": "id,name,instagram_business_account"},
        timeout=30,
    )

    if pages_resp.status_code != 200:
        print(f"Could not fetch pages (HTTP {pages_resp.status_code}): {pages_resp.text}")
        sys.exit(1)

    pages_data = pages_resp.json()
    pages      = pages_data.get("data", [])

    instagram_user_id = None
    for page in pages:
        ig_account = page.get("instagram_business_account")
        if ig_account:
            instagram_user_id = ig_account["id"]
            print(f"Found Instagram Business account: {instagram_user_id} (on page: {page['name']})")
            break

    if not instagram_user_id:
        print(
            "\nNo Instagram Business account found linked to your pages.\n"
            "Make sure your Instagram account is:\n"
            "  1. A Business or Creator account (not Personal)\n"
            "  2. Linked to your Facebook Page in Instagram Settings > Linked Accounts"
        )
        print(
            "\nIf you already know your Instagram User ID, you can set it manually.\n"
        )
        instagram_user_id = input("Enter Instagram User ID manually (or press Enter to abort): ").strip()
        if not instagram_user_id:
            sys.exit(1)

    print()
    print("=" * 60)
    print("SUCCESS — Add these lines to your .env file:")
    print("=" * 60)
    print(f"INSTAGRAM_ACCESS_TOKEN={long_lived_token}")
    print(f"INSTAGRAM_USER_ID={instagram_user_id}")
    print("PUBLISHING_ENABLED=true")
    print()
    print("NOTE: Long-lived tokens expire after ~60 days. Re-run this script to renew.")
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
    args = parser.parse_args()

    if args.platform == "youtube":
        setup_youtube()
    elif args.platform == "tiktok":
        setup_tiktok()
    elif args.platform == "instagram":
        setup_instagram()


if __name__ == "__main__":
    main()
