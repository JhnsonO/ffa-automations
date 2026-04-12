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
    QUALITY_PREFERENCE = ["2160p", "3000p", "1440p"]

    try:
        data = gopro_get(session, f"/media/{media_id}/download")
        variations = data.get("_embedded", {}).get("variations", [])

        # Prefer concat (pre-joined) over source, highest quality first
        for quality in QUALITY_PREFERENCE:
            for v in variations:
                if v.get("label") == "concat" and v.get("quality") == quality and v.get("available"):
                    return v["url"]

        # Fallback to first available source at any high quality
        for quality in QUALITY_PREFERENCE:
            for v in variations:
                if v.get("label") == "source" and v.get("quality") == quality and v.get("available"):
                    return v["url"]

        log.warning(f"No 4K variant found for {media_id}")
        return None

    except Exception as e:
        log.error(f"Failed to get download URL for {media_id}: {e}")
        return None


def download_video(url: str, dest_path: Path, max_retries: int = 3) -> bool:
    """Stream download a video file with retry logic and ETA."""
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            wait = 30 * attempt
            log.info(f"Download retry {attempt}/{max_retries} in {wait}s...")
            time.sleep(wait)
            if dest_path.exists():
                dest_path.unlink()

        log.info(f"Downloading to {dest_path.name} (attempt {attempt})...")
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                start_time = time.time()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        f.write(chunk)
                        downloaded += len(chunk)
                        elapsed = time.time() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        if total and speed > 0:
                            remaining = (total - downloaded) / speed
                            mins, secs = divmod(int(remaining), 60)
                            eta = f"{mins}m {secs}s remaining"
                            pct = downloaded / total * 100
                            print(f"\r  {pct:.1f}% ({downloaded/1e9:.2f}/{total/1e9:.2f} GB) — {speed/1e6:.1f} MB/s — ETA: {eta}    ", end="", flush=True)
                print()
            log.info(f"Download complete: {dest_path.stat().st_size / 1e9:.2f} GB")
            return True
        except Exception as e:
            log.error(f"Download error (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                if dest_path.exists():
                    dest_path.unlink()
                return False

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


def upload_to_youtube(service, video_path: Path, title: str, description: str, gopro_filename: str = "", max_retries: int = 3) -> str | None:
    """Upload a video file to YouTube with retry logic, return video ID."""
    gopro_label = f" ({gopro_filename})" if gopro_filename else ""
    log.info(f"Uploading to YouTube: {title}{gopro_label}")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["FFA", "Football For All", "Leicester", "grassroots football"],
            "categoryId": "17",  # Sports
        },
        "status": {
            "privacyStatus": "unlisted",
            "selfDeclaredMadeForKids": False,
        },
    }

    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            wait = 30 * attempt
            log.info(f"Retry {attempt}/{max_retries} in {wait}s...")
            time.sleep(wait)

        try:
            media = MediaFileUpload(str(video_path), chunksize=50 * 1024 * 1024, resumable=True)
            request = service.videos().insert(part="snippet,status", body=body, media_body=media)

            response = None
            upload_start = time.time()
            last_progress = 0.0
            while response is None:
                status, response = request.next_chunk()
                if status:
                    progress = status.progress()
                    elapsed = time.time() - upload_start
                    speed = progress / elapsed if elapsed > 0 else 0
                    if speed > 0:
                        remaining = (1 - progress) / speed
                        mins, secs = divmod(int(remaining), 60)
                        eta = f"{mins}m {secs}s remaining"
                    else:
                        eta = "calculating..."
                    pct = int(progress * 100)
                    label = f"{title} ({gopro_filename})" if gopro_filename else title
                    print(f"\r  [{label}] {pct}% — ETA: {eta}    ", end="", flush=True)
            print()

            vid_id = response.get("id")
            log.info(f"YouTube upload complete: https://youtu.be/{vid_id}")
            return vid_id

        except Exception as e:
            log.error(f"YouTube upload error (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                log.error("All retry attempts exhausted.")
                return None

    return None


# ── Main pipeline ─────────────────────────────────────────────────────────────

def make_title(filename: str, captured_at: str, camera_label: str = "") -> str:
    try:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        day_name = dt.strftime("%A")
        day = int(dt.strftime("%d"))
        suffix = "th" if 11 <= day <= 13 else {1:"st", 2:"nd", 3:"rd"}.get(day % 10, "th")
        date_str = f"{day}{suffix} {dt.strftime('%B %Y')}"
    except Exception:
        day_name = "Session"
        date_str = captured_at[:10]
    cam = f" | Camera {camera_label}" if camera_label else ""
    return f"{day_name} Session | {date_str} | FFA Leicester{cam}"


def make_description(filename: str, captured_at: str, camera_label: str = "") -> str:
    try:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        day_name = dt.strftime("%A")
        day = int(dt.strftime("%d"))
        suffix = "th" if 11 <= day <= 13 else {1:"st", 2:"nd", 3:"rd"}.get(day % 10, "th")
        date_str = f"{day}{suffix} {dt.strftime('%B %Y')}"
        is_monday = dt.weekday() == 0
    except Exception:
        day_name = "Session"
        date_str = captured_at[:10]
        is_monday = False

    cam_line = f"Camera {camera_label} footage.\n" if camera_label else ""

    monday_section = ""
    if is_monday:
        monday_section = (
            "\n\nThis is part of the FFA Monday League — our weekly competitive kickabout with individual player standings.\n"
            "Check the league table: https://www.officialffa.co.uk/mnl\n"
        )

    return (
        f"FFA Leicester | {day_name} Session | {date_str}\n"
        f"{cam_line}"
        f"\nCompetitive kickabouts in Leicester, running weekly. All levels welcome.\n"
        f"{monday_section}\n"
        f"Want to play? Book your spot:\n"
        f"https://www.officialffa.co.uk\n\n"
        f"Interested in tournaments or events? Get in touch via our website or socials.\n\n"
        f"Shop FFA merch: https://www.officialffa.co.uk/store\n\n"
        f"Follow us:\n"
        f"Instagram: https://www.instagram.com/_official_ffa\n"
        f"TikTok: https://www.tiktok.com/@official_ffa\n\n"
        f"#FFA #FFALeicester #FootballForAll #Leicester #GrassrootsFootball #5aside #MondayLeague"
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
    new_items = [m for m in new_items if int(m.get("file_size", 0)) > 100_000_000]
    log.info(f"{len(new_items)} new video(s) to upload")

    if not new_items:
        log.info("Nothing to do. Exiting.")
        return

    yt = get_youtube_service()

    camera_labels = "ABCDEFGH"

    for item in new_items:
        media_id    = item["id"]
        filename    = item["filename"]
        captured_at = item["captured_at"]
        date_key    = captured_at[:10]

        # Count how many videos already uploaded for this date (including previous runs)
        existing = con.execute(
            "SELECT COUNT(*) FROM uploads WHERE captured_at LIKE ?", (f"{date_key}%",)
        ).fetchone()[0]
        camera_label = camera_labels[existing] if existing < len(camera_labels) else str(existing + 1)

        log.info(f"Processing: {filename} ({date_key}) Camera {camera_label} — {item.get('file_size', 0)/1e9:.1f} GB — est. {int(item.get('file_size', 0)/1e9 * 2)} mins")

        dl_url = get_concat_download_url(session, media_id)
        if not dl_url:
            log.warning(f"Skipping {filename} — no download URL")
            continue

        dest = DOWNLOAD_DIR / filename

        if not download_video(dl_url, dest):
            continue

        title       = make_title(filename, captured_at, camera_label)
        description = make_description(filename, captured_at, camera_label)
        yt_id       = upload_to_youtube(yt, dest, title, description, gopro_filename=filename)

        if yt_id:
            mark_uploaded(con, media_id, filename, captured_at, yt_id)
            dest.unlink()
            log.info(f"Done: {filename} Camera {camera_label} -> https://youtu.be/{yt_id}")
        else:
            log.error(f"Upload failed for {filename}, keeping local file")

    log.info("All done.")


if __name__ == "__main__":
    run()
