#!/usr/bin/env python3
"""Generate a fresh YouTube OAuth token for GitHub Actions.

Usage:
1. Put youtube_credentials.json in this repo folder.
2. Run: python youtube_reauth.py
3. Sign in/approve the YouTube upload permission in the browser.
4. Copy the printed JSON into the GitHub Actions secret named YOUTUBE_TOKEN.
"""

from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = Path(__file__).parent
CREDS_PATH = BASE_DIR / "youtube_credentials.json"
TOKEN_PATH = BASE_DIR / "youtube_token.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]


def main():
    if not CREDS_PATH.exists():
        raise SystemExit(
            "Missing youtube_credentials.json. Put your Google OAuth client credentials file "
            "in this folder first."
        )

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), YT_SCOPES)
    creds = flow.run_local_server(port=8080, prompt="consent")
    TOKEN_PATH.write_text(creds.to_json())

    print("\nFresh YouTube token saved to youtube_token.json")
    print("Copy everything below into GitHub -> Settings -> Secrets and variables -> Actions -> YOUTUBE_TOKEN")
    print("=" * 80)
    print(creds.to_json())
    print("=" * 80)


if __name__ == "__main__":
    main()
