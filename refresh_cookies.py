#!/usr/bin/env python3
"""
refresh_cookies.py
------------------
Run this when GoPro cookies expire (~every few weeks).
Close Chrome first, then run:

    python3 refresh_cookies.py

It will:
1. Extract fresh GoPro cookies from Chrome
2. Verify they work against the GoPro API
3. Automatically update the GOPRO_COOKIES secret in GitHub
"""

import json
import sys
import urllib.request
import base64
from pathlib import Path

GITHUB_TOKEN = "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"
GITHUB_REPO  = "JhnsonO/ffa-automations"


def extract_cookies() -> dict:
    try:
        import browser_cookie3
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "browser-cookie3"], check=True)
        import browser_cookie3

    for name, loader in [("Chrome", browser_cookie3.chrome), ("Firefox", browser_cookie3.firefox)]:
        try:
            jar = loader(domain_name="gopro.com")
            cookies = {c.name: c.value for c in jar}
            if cookies:
                print(f"Found {len(cookies)} GoPro cookies in {name}")
                return cookies
        except Exception as e:
            print(f"{name}: {e}")

    print("\nCould not auto-extract cookies. Make sure Chrome is fully closed and try again.")
    sys.exit(1)


def verify_cookies(cookies: dict) -> bool:
    import requests
    session = requests.Session()
    session.cookies.update(cookies)
    r = session.get(
        "https://api.gopro.com/media/search",
        headers={"Accept": "application/vnd.gopro.jk.media+json; version=2.0.0"},
        params={"fields": "id,filename", "per_page": 1, "page": 1},
        timeout=15,
    )
    print(f"GoPro API check: HTTP {r.status_code}")
    return r.status_code == 200


def update_github_secret(cookies: dict):
    try:
        from nacl import encoding, public
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "PyNaCl"], check=True)
        from nacl import encoding, public

    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json"
    }

    # Get public key
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/public-key",
        headers=headers
    )
    with urllib.request.urlopen(req) as r:
        key_data = json.load(r)

    key_id  = key_data["key_id"]
    pub_key = key_data["key"]

    def encrypt(pub_key_b64, value):
        pk = public.PublicKey(pub_key_b64.encode(), encoding.Base64Encoder())
        encrypted = public.SealedBox(pk).encrypt(value.encode())
        return base64.b64encode(encrypted).decode()

    encrypted = encrypt(pub_key, json.dumps(cookies))
    payload   = json.dumps({"encrypted_value": encrypted, "key_id": key_id}).encode()

    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/GOPRO_COOKIES",
        data=payload, method="PUT", headers=headers
    )
    with urllib.request.urlopen(req) as r:
        print(f"GitHub secret updated: HTTP {r.status}")


def main():
    print("=== FFA GoPro Cookie Refresher ===\n")
    print("Step 1: Extracting cookies from browser...")
    cookies = extract_cookies()

    print("\nStep 2: Verifying cookies work...")
    if not verify_cookies(cookies):
        print("Cookies invalid — try logging into GoPro Cloud in Chrome first, then close Chrome and run again.")
        sys.exit(1)

    print("\nStep 3: Updating GitHub secret...")
    update_github_secret(cookies)

    # Also save locally
    Path("gopro_cookies.json").write_text(json.dumps(cookies, indent=2))
    print("\nDone! Cookies refreshed and GitHub secret updated.")
    print("The next GitHub Actions run will use the new cookies automatically.")


if __name__ == "__main__":
    main()
