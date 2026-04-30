#!/usr/bin/env python3
"""Generate a fresh YouTube OAuth token for GitHub Actions.

Normal laptop mode:
    python youtube_reauth.py

Manual/phone-friendly mode:
    python youtube_reauth.py --manual

Manual mode prints a Google auth URL. Open it in a browser, approve access,
then copy the final localhost redirect URL, or just the code= value, back into
this script. This is useful on Android/Termux or remote terminals.

After either mode, copy the printed JSON into the GitHub Actions secret named
YOUTUBE_TOKEN.
"""

import argparse
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import InstalledAppFlow

BASE_DIR = Path(__file__).parent
CREDS_PATH = BASE_DIR / "youtube_credentials.json"
TOKEN_PATH = BASE_DIR / "youtube_token.json"
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
REDIRECT_URI = "http://localhost:8080/"


def build_flow():
    if not CREDS_PATH.exists():
        raise SystemExit(
            "Missing youtube_credentials.json. Put your Google OAuth client credentials file "
            "in this folder first."
        )
    return InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), YT_SCOPES)


def extract_code(value: str) -> str:
    value = value.strip()
    if not value:
        raise SystemExit("No code/URL pasted.")

    if "code=" in value:
        parsed = urlparse(value)
        query = parse_qs(parsed.query)
        code = query.get("code", [""])[0]
        if code:
            return code

    return value


def save_and_print(creds):
    TOKEN_PATH.write_text(creds.to_json())

    print("\nFresh YouTube token saved to youtube_token.json")
    print("Copy everything below into GitHub -> Settings -> Secrets and variables -> Actions -> YOUTUBE_TOKEN")
    print("=" * 80)
    print(creds.to_json())
    print("=" * 80)


def run_local_server_mode():
    flow = build_flow()
    creds = flow.run_local_server(port=8080, prompt="consent")
    save_and_print(creds)


def run_manual_mode():
    flow = build_flow()
    flow.redirect_uri = REDIRECT_URI

    auth_url, _ = flow.authorization_url(
        access_type="offline",
        prompt="consent",
        include_granted_scopes="true",
    )

    print("\nOpen this Google auth URL in your browser:")
    print("=" * 80)
    print(auth_url)
    print("=" * 80)
    print("\nAfter approval, Google will redirect to localhost. The page may fail to load.")
    print("That is fine. Copy the full address bar URL, or just the code= value, and paste it below.\n")

    pasted = input("Paste redirect URL or code: ")
    code = extract_code(pasted)

    flow.fetch_token(code=code)
    save_and_print(flow.credentials)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manual", action="store_true", help="Use copy/paste OAuth flow instead of localhost browser callback")
    args = parser.parse_args()

    if args.manual:
        run_manual_mode()
    else:
        run_local_server_mode()


if __name__ == "__main__":
    main()
