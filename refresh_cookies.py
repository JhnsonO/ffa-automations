#!/usr/bin/env python3
"""
refresh_cookies.py
------------------
Run this when GoPro cookies expire (~every few weeks).

OPTION A — Your own laptop (Chrome must be closed first):
    python3 refresh_cookies.py

OPTION B — Someone else's laptop / any computer:
    python3 refresh_cookies.py --manual

It will verify the cookies and automatically update the GitHub secret.
"""

import json, sys, urllib.request, base64, argparse
from pathlib import Path

GITHUB_TOKEN = "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"
GITHUB_REPO  = "JhnsonO/ffa-automations"


def extract_from_browser() -> dict:
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

    print("\nCould not auto-extract. Make sure Chrome is fully closed and try again.")
    print("Or run: python3 refresh_cookies.py --manual")
    sys.exit(1)


def extract_manual() -> dict:
    print("""
=== Manual Cookie Extraction ===

1. Open Chrome and go to: https://gopro.com/media-library/
2. Make sure you're logged in
3. Press F12 to open DevTools
4. Click the Console tab
5. Paste this and press Enter:

   copy(JSON.stringify(Object.fromEntries(document.cookie.split(';').map(c=>[c.trim().split('=')[0],c.trim().split('=').slice(1).join('=')]))))

   (This copies cookies to your clipboard)

6. If that gives an empty object {}, use this instead:
   - Click Application tab in DevTools
   - Click Cookies → https://gopro.com in the left panel
   - You'll see a table of cookies
   - Press F12 on the page below to open a fresh console and run:
   
   const cookies = {}; document.cookie.split(';').forEach(c => { const [k,...v] = c.trim().split('='); cookies[k]=v.join('='); }); copy(JSON.stringify(cookies));

7. Once copied, paste it below and press Enter twice:
""")
    lines = []
    while True:
        line = input()
        if line == "" and lines:
            break
        lines.append(line)
    
    raw = "\n".join(lines).strip()
    try:
        cookies = json.loads(raw)
        print(f"Parsed {len(cookies)} cookies.")
        return cookies
    except Exception as e:
        print(f"Could not parse cookies: {e}")
        print("Make sure you pasted valid JSON like: {\"cookie_name\": \"value\", ...}")
        sys.exit(1)


def verify_cookies(cookies: dict) -> bool:
    try:
        import requests
    except ImportError:
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "requests"], check=True)
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
    if r.status_code == 200:
        total = r.json().get("_pages", {}).get("total_items", 0)
        print(f"Cookies valid — {total} videos accessible")
        return True
    print("Cookies invalid or expired.")
    return False


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
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/public-key",
        headers=headers
    )
    with urllib.request.urlopen(req) as r:
        key_data = json.load(r)

    pk = public.PublicKey(key_data["key"].encode(), encoding.Base64Encoder())
    encrypted = base64.b64encode(public.SealedBox(pk).encrypt(json.dumps(cookies).encode())).decode()
    payload = json.dumps({"encrypted_value": encrypted, "key_id": key_data["key_id"]}).encode()

    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/secrets/GOPRO_COOKIES",
        data=payload, method="PUT", headers=headers
    )
    with urllib.request.urlopen(req) as r:
        print(f"GitHub secret updated: HTTP {r.status}")

    # Also close the expired cookies issue if open
    req = urllib.request.Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/issues?labels=cookies-expired&state=open",
        headers=headers
    )
    with urllib.request.urlopen(req) as r:
        issues = json.load(r)
    for issue in issues:
        close_req = urllib.request.Request(
            f"https://api.github.com/repos/{GITHUB_REPO}/issues/{issue['number']}",
            data=json.dumps({"state": "closed"}).encode(),
            method="PATCH", headers=headers
        )
        with urllib.request.urlopen(close_req) as r:
            print(f"Closed GitHub issue #{issue['number']}: {issue['title']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manual", action="store_true", help="Manual cookie extraction (any computer)")
    args = parser.parse_args()

    print("=== FFA GoPro Cookie Refresher ===\n")

    if args.manual:
        cookies = extract_manual()
    else:
        print("Extracting cookies from browser (Chrome must be closed)...")
        cookies = extract_from_browser()

    print("\nVerifying cookies...")
    if not verify_cookies(cookies):
        print("\nTry logging into GoPro Cloud in Chrome first, then run again.")
        sys.exit(1)

    print("\nUpdating GitHub secret...")
    update_github_secret(cookies)

    Path("gopro_cookies.json").write_text(json.dumps(cookies, indent=2))
    print("\nAll done! Uploads will resume on the next GitHub Actions run.")


if __name__ == "__main__":
    main()
