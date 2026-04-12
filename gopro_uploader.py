#!/usr/bin/env python3
"""
FFA GoPro Cloud → YouTube Uploader
-----------------------------------
Fetches new videos from GoPro Cloud and uploads them to YouTube.
Uses the 'concat' variation which is already a single joined 4K file.
"""

import os
import sys
import json
import time
import logging
import sqlite3
import requests
import subprocess
from datetime import datetime, timezone
from pathlib import Path

# ── YouTube API ──────────────────────────────────────────────────────────────
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# ─────────────────────────────────────────────────────────────────────────────
BASE_DIR      = Path(__file__).parent
DB_PATH       = BASE_DIR / "uploaded.db"
LOG_PATH      = BASE_DIR / "logs" / "uploader.log"
CREDS_PATH    = BASE_DIR / "youtube_credentials.json"   # OAuth client secret
TOKEN_PATH    = BASE_DIR / "youtube_token.json"         # saved access/refresh token
COOKIE_PATH   = BASE_DIR / "gopro_cookies.json"        # GoPro session cookies
DOWNLOAD_DIR  = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
LOG_PATH.parent.mkdir(exist_ok=True)

GOPRO_API     = "https://api.gopro.com"
GOPRO_HEADERS = {"Accept": "application/vnd.gopro.jk.media+json; version=2.0.0"}
YT_SCOPES     = ["https://www.googleapis.com/auth/youtube.upload"]

# How many days back to look for new videos (change to 0 to only do today)
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", 2))

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
# Fix Windows console encoding
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
log = logging.getLogger(__name__)


# ── Database ──────────────────────────────────────────────────────────────────
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            media_id    TEXT PRIMARY KEY,
            filename    TEXT,
            captured_at TEXT,
            youtube_id  TEXT,
            uploaded_at TEXT
        )
    """)
    con.commit()
    return con


def already_uploaded(con, media_id: str) -> bool:
    row = con.execute("SELECT 1 FROM uploads WHERE media_id=?", (media_id,)).fetchone()
    return row is not None


def mark_uploaded(con, media_id: str, filename: str, captured_at: str, youtube_id: str):
    con.execute(
        "INSERT OR REPLACE INTO uploads VALUES (?,?,?,?,?)",
        (media_id, filename, captured_at, youtube_id, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()


# ── GoPro Cloud ───────────────────────────────────────────────────────────────
def load_gopro_cookies() -> dict:
    """Load saved GoPro session cookies from file."""
    if not COOKIE_PATH.exists():
        log.error(
            "gopro_cookies.json not found. Run: python3 extract_cookies.py first."
        )
        sys.exit(1)
    with open(COOKIE_PATH) as f:
        return json.load(f)


def gopro_get(session: requests.Session, path: str, params: dict = None):
    url = f"{GOPRO_API}{path}"
    r = session.get(url, headers=GOPRO_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_recent_media(session: requests.Session, days: int = LOOKBACK_DAYS) -> list:
    """Return list of video media items captured in the last N days."""
    from datetime import timedelta
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    all_media = []
    page = 1
    while True:
        data = gopro_get(
            session,
            "/media/search",
            params={
                "fields": "id,captured_at,filename,file_size,type",
                "order_by": "captured_at",
                "order": "desc",
                "per_page": 50,
                "page": page,
                "type": "Video",
            },
        )
        items = data.get("_embedded", {}).get("media", [])
        if not items:
            break

        for item in items:
            # Stop if we've gone past our lookback window
            cap = item.get("captured_at", "")
            if cap and cap < cutoff:
                return all_media
            all_media.append(item)

        pages = data.get("_pages", {})
        if page >= pages.get("total_pages", 1):
            break
        page += 1

    return all_media


def get_concat_download_url(session: requests.Session, media_id: str) -> str | None:
    """
    Fetch the pre-signed download URL for the pre-joined 4K 'concat' variation.
    Falls back to first 'source' variation if concat isn't available.
    """
    try:
        data = gopro_get(session, f"/media/{media_id}/download")
        variations = data.get("_embedded", {}).get("variations", [])

        # Prefer the pre-joined concat file (already one file, 4K)
        for v in variations:
            if v.get("label") == "concat" and v.get("quality") == "2160p" and v.get("available"):
                return v["url"]

        # Fallback: first source file at 4K
        for v in variations:
            if v.get("label") == "source" and v.get("quality") == "2160p" and v.get("available"):
                return v["url"]

        log.warning(f"No 4K variant found for {media_id}")
        return None

    except Exception as e:
        log.error(f"Failed to get download URL for {media_id}: {e}")
        return None


def download_video(url: str, dest_path: Path) -> bool:
    """Stream download a video file, showing progress."""
    log.info(f"Downloading to {dest_path.name} ...")
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(dest_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):  # 1MB chunks
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total * 100
                        print(f"\r  {pct:.1f}% ({downloaded/1e9:.2f} GB)", end="", flush=True)
            print()
        log.info(f"Download complete: {dest_path.stat().st_size / 1e9:.2f} GB")
        return True
    except Exception as e:
        log.error(f"Download failed: {e}")
        if dest_path.exists():
            dest_path.unlink()
        return False


# ── YouTube ───────────────────────────────────────────────────────────────────
def get_youtube_service():
    """OAuth2 flow — runs browser once, saves token for future runs."""
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), YT_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing YouTube token...")
            creds.refresh(Request())
        else:
            log.info("Starting YouTube OAuth flow (opens browser)...")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), YT_SCOPES)
            creds = flow.run_local_server(port=8080)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def upload_to_youtube(service, video_path: Path, title: str, description: str) -> str | None:
    """Upload a video file to YouTube, return video ID."""
    log.info(f"Uploading to YouTube: {title}")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["FFA", "Football For All", "Leicester", "grassroots football"],
            "categoryId": "17",  # Sports
        },
        "status": {
            "privacyStatus": "public",   # change to "private" if you want manual review first
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(video_path), chunksize=50 * 1024 * 1024, resumable=True)
    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        try:
            status, response = request.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                print(f"\r  YouTube upload: {pct}%", end="", flush=True)
        except Exception as e:
            log.error(f"YouTube upload error: {e}")
            return None
    print()

    vid_id = response.get("id")
    log.info(f"YouTube upload complete: https://youtu.be/{vid_id}")
    return vid_id


# ── Main pipeline ─────────────────────────────────────────────────────────────
def make_title(filename: str, captured_at: str) -> str:
    try:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        date_str = dt.strftime("%d %b %Y")
    except Exception:
        date_str = captured_at[:10]
    return f"FFA Session – {date_str}"


def make_description(filename: str, captured_at: str) -> str:
    try:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        date_str = dt.strftime("%A %d %B %Y")
    except Exception:
        date_str = captured_at[:10]
    return (
        f"Football For All – Leicester\n"
        f"Session recorded: {date_str}\n\n"
        f"Join us at footballforall.co.uk\n"
        f"#FFA #FootballForAll #Leicester #GrassrootsFootball"
    )


def run():
    log.info("=" * 60)
    log.info("FFA GoPro -> YouTube uploader starting")

    con = init_db()
    cookies = load_gopro_cookies()

    session = requests.Session()
    session.cookies.update(cookies)

    log.info(f"Fetching media from last {LOOKBACK_DAYS} day(s)...")
    media_items = fetch_recent_media(session)
    log.info(f"Found {len(media_items)} video(s) in window")

    new_items = [m for m in media_items if not already_uploaded(con, m["id"])]
    log.info(f"{len(new_items)} new video(s) to upload")

    if not new_items:
        log.info("Nothing to do. Exiting.")
        return

    yt = get_youtube_service()

    for item in new_items:
        media_id   = item["id"]
        filename   = item["filename"]
        captured_at = item["captured_at"]

        log.info(f"Processing: {filename} ({captured_at[:10]})")

        # Get fresh pre-signed download URL
        dl_url = get_concat_download_url(session, media_id)
        if not dl_url:
            log.warning(f"Skipping {filename} — no download URL")
            continue

        dest = DOWNLOAD_DIR / filename

        # Download
        if not download_video(dl_url, dest):
            continue

        # Upload to YouTube
        title       = make_title(filename, captured_at)
        description = make_description(filename, captured_at)
        yt_id       = upload_to_youtube(yt, dest, title, description)

        if yt_id:
            mark_uploaded(con, media_id, filename, captured_at, yt_id)
            # Delete local file after successful upload to save disk space
            dest.unlink()
            log.info(f"Done: {filename} -> https://youtu.be/{yt_id}")
        else:
            log.error(f"Upload failed for {filename}, keeping local file")

    log.info("All done.")


if __name__ == "__main__":
    run()
