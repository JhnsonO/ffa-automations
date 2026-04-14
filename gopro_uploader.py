#!/usr/bin/env python3
"""
FFA GoPro Cloud → YouTube Uploader
-----------------------------------
Runs every 30 minutes via GitHub Actions.

Flow:
1. Try existing cookies → quick check for new videos
2. If cookies expired (401) → refresh via Playwright → retry check
3. If no new videos → exit silently (no logs, no noise)
4. If new videos → download and upload to YouTube
"""

import os
import sys
import json
import time
import logging
import sqlite3
import requests
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_DIR     = Path(__file__).parent
DB_PATH      = BASE_DIR / "uploaded.db"
LOG_PATH     = BASE_DIR / "logs" / "uploader.log"
CREDS_PATH   = BASE_DIR / "youtube_credentials.json"
TOKEN_PATH   = BASE_DIR / "youtube_token.json"
COOKIE_PATH  = BASE_DIR / "gopro_cookies.json"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
LOG_PATH.parent.mkdir(exist_ok=True)

GOPRO_API     = "https://api.gopro.com"
GOPRO_HEADERS = {"Accept": "application/vnd.gopro.jk.media+json; version=2.0.0"}
YT_SCOPES     = ["https://www.googleapis.com/auth/youtube.upload"]

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS") or 2)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
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
    con.execute("""
        CREATE TABLE IF NOT EXISTS failed_uploads (
            media_id  TEXT PRIMARY KEY,
            failed_at TEXT,
            reason    TEXT
        )
    """)
    con.commit()
    return con

def already_uploaded(con, media_id):
    return con.execute(
        "SELECT 1 FROM uploads WHERE media_id=?", (media_id,)
    ).fetchone() is not None

def recently_failed(con, media_id):
    row = con.execute(
        "SELECT failed_at FROM failed_uploads WHERE media_id=?", (media_id,)
    ).fetchone()
    if not row:
        return False
    failed_at = datetime.fromisoformat(row[0])
    return (datetime.now(timezone.utc) - failed_at) < timedelta(hours=24)

def mark_uploaded(con, media_id, filename, captured_at, youtube_id):
    con.execute("DELETE FROM failed_uploads WHERE media_id=?", (media_id,))
    con.execute(
        "INSERT OR REPLACE INTO uploads VALUES (?,?,?,?,?)",
        (media_id, filename, captured_at, youtube_id, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()

def mark_failed(con, media_id, reason):
    con.execute(
        "INSERT OR REPLACE INTO failed_uploads VALUES (?,?,?)",
        (media_id, datetime.now(timezone.utc).isoformat(), reason),
    )
    con.commit()


# ── GoPro Cookies ─────────────────────────────────────────────────────────────
def load_cookies_from_file():
    if not COOKIE_PATH.exists():
        return {}
    with open(COOKIE_PATH) as f:
        return json.load(f)

def refresh_cookies_via_playwright():
    email    = os.environ.get("GOPRO_EMAIL")
    password = os.environ.get("GOPRO_PASSWORD")

    if not email or not password:
        log.error("GOPRO_EMAIL / GOPRO_PASSWORD env vars not set — cannot refresh cookies")
        return False

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        log.error("Playwright not installed — run: pip install playwright && playwright install chromium")
        return False

    log.info("Refreshing GoPro cookies via Playwright...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()

            page.goto("https://gopro.com/login", wait_until="networkidle", timeout=30000)
            page.fill('input[type="email"], input[name="email"], input[id*="email"]', email)

            try:
                next_btn = page.locator('button:has-text("Next"), button:has-text("Continue")')
                if next_btn.count() > 0:
                    next_btn.first.click()
                    page.wait_for_timeout(1500)
            except Exception:
                pass

            page.fill('input[type="password"]', password)
            page.click('button[type="submit"], button:has-text("Sign In"), button:has-text("Log In")')

            try:
                page.wait_for_url("**/plus.gopro.com/**", timeout=15000)
            except PlaywrightTimeout:
                try:
                    page.wait_for_url(lambda url: "gopro.com/login" not in url, timeout=10000)
                except PlaywrightTimeout:
                    log.error(f"Playwright login failed — still on: {page.url}")
                    browser.close()
                    return False

            if "plus.gopro.com" not in page.url:
                page.goto("https://plus.gopro.com", wait_until="networkidle", timeout=20000)

            cookies = context.cookies()
            browser.close()

        if not cookies:
            log.error("No cookies extracted after Playwright login")
            return False

        cookie_dict = {c["name"]: c["value"] for c in cookies}
        with open(COOKIE_PATH, "w") as f:
            json.dump(cookie_dict, f, indent=2)

        log.info(f"Cookie refresh successful — {len(cookie_dict)} cookies saved")
        return True

    except Exception as e:
        log.error(f"Playwright cookie refresh failed: {e}")
        return False


# ── GoPro API ─────────────────────────────────────────────────────────────────
def make_session(cookies):
    session = requests.Session()
    session.cookies.update(cookies)
    return session

def gopro_get(session, path, params=None):
    r = session.get(f"{GOPRO_API}{path}", headers=GOPRO_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()

def fetch_recent_media(session):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z"
    )
    all_media = []
    page = 1
    while True:
        data = gopro_get(session, "/media/search", params={
            "fields": "id,captured_at,filename,file_size,type",
            "order_by": "captured_at",
            "order": "desc",
            "per_page": 50,
            "page": page,
            "type": "Video",
        })
        items = data.get("_embedded", {}).get("media", [])
        if not items:
            break
        for item in items:
            cap = item.get("captured_at", "")
            if cap and cap < cutoff:
                return all_media
            all_media.append(item)
        pages = data.get("_pages", {})
        if page >= pages.get("total_pages", 1):
            break
        page += 1
    return all_media

def get_new_items(con):
    """
    Try existing cookies first. On 401, refresh via Playwright and retry once.
    Returns (session, new_items).
    """
    cookies = load_cookies_from_file()
    session = make_session(cookies)

    try:
        media_items = fetch_recent_media(session)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            if not refresh_cookies_via_playwright():
                log.error("Cookie refresh failed — giving up")
                sys.exit(1)
            cookies = load_cookies_from_file()
            session = make_session(cookies)
            media_items = fetch_recent_media(session)
        else:
            raise

    new_items = [
        m for m in media_items
        if not already_uploaded(con, m["id"])
        and not recently_failed(con, m["id"])
        and int(m.get("file_size", 0)) > 100_000_000
    ]

    return session, new_items

def get_concat_download_url(session, media_id):
    QUALITY_PREFERENCE = ["2160p", "3000p", "1440p"]
    try:
        data = gopro_get(session, f"/media/{media_id}/download")
        variations = data.get("_embedded", {}).get("variations", [])
        for quality in QUALITY_PREFERENCE:
            for v in variations:
                if v.get("label") == "concat" and v.get("quality") == quality and v.get("available"):
                    return v["url"]
        for quality in QUALITY_PREFERENCE:
            for v in variations:
                if v.get("label") == "source" and v.get("quality") == quality and v.get("available"):
                    return v["url"]
        log.warning(f"No 4K variant found for {media_id}")
        return None
    except Exception as e:
        log.error(f"Failed to get download URL for {media_id}: {e}")
        return None

def download_video(url, dest_path, max_retries=3):
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
                            pct = downloaded / total * 100
                            print(
                                f"\r  {pct:.1f}% ({downloaded/1e9:.2f}/{total/1e9:.2f} GB)"
                                f" — {speed/1e6:.1f} MB/s — ETA: {mins}m {secs}s    ",
                                end="", flush=True,
                            )
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
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), YT_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing YouTube token...")
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_PATH), YT_SCOPES)
            creds = flow.run_local_server(port=8080)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())
    return build("youtube", "v3", credentials=creds)

def upload_to_youtube(service, video_path, title, description, gopro_filename="", max_retries=3):
    log.info(f"Uploading to YouTube: {title} ({gopro_filename})")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["FFA", "Football For All", "Leicester", "grassroots football"],
            "categoryId": "17",
        },
        "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False},
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
            while response is None:
                status, response = request.next_chunk()
                if status:
                    progress = status.progress()
                    elapsed = time.time() - upload_start
                    speed = progress / elapsed if elapsed > 0 else 0
                    eta = f"{int((1-progress)/speed//60)}m {int((1-progress)/speed%60)}s" if speed > 0 else "calculating..."
                    print(f"\r  [{gopro_filename}] {int(progress*100)}% — ETA: {eta}    ", end="", flush=True)
            print()
            vid_id = response.get("id")
            log.info(f"YouTube upload complete: https://youtu.be/{vid_id}")
            return vid_id
        except Exception as e:
            log.error(f"YouTube upload error (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                return None
    return None


# ── Title / Description ───────────────────────────────────────────────────────
def make_title(filename, captured_at, camera_label=""):
    try:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        day_name = dt.strftime("%A")
        day = int(dt.strftime("%d"))
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        date_str = f"{day}{suffix} {dt.strftime('%B %Y')}"
    except Exception:
        day_name, date_str = "Session", captured_at[:10]
    cam = f" | Camera {camera_label}" if camera_label else ""
    return f"{day_name} Session | {date_str} | FFA Leicester{cam}"

def make_description(filename, captured_at, camera_label=""):
    try:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        day_name = dt.strftime("%A")
        day = int(dt.strftime("%d"))
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        date_str = f"{day}{suffix} {dt.strftime('%B %Y')}"
        is_monday = dt.weekday() == 0
    except Exception:
        day_name, date_str, is_monday = "Session", captured_at[:10], False

    cam_line = f"Camera {camera_label} footage.\n" if camera_label else ""
    monday_section = (
        "\n\nThis is part of the FFA Monday League — our weekly competitive kickabout with individual player standings.\n"
        "Check the league table: https://www.officialffa.co.uk/mnl\n"
    ) if is_monday else ""

    return (
        f"FFA Leicester | {day_name} Session | {date_str}\n"
        f"{cam_line}"
        f"\nCompetitive kickabouts in Leicester, running weekly. All levels welcome.\n"
        f"{monday_section}\n"
        f"Want to play? Book your spot:\nhttps://www.officialffa.co.uk\n\n"
        f"Interested in tournaments or events? Get in touch via our website or socials.\n\n"
        f"Shop FFA merch: https://www.officialffa.co.uk/store\n\n"
        f"Follow us:\n"
        f"Instagram: https://www.instagram.com/_official_ffa\n"
        f"TikTok: https://www.tiktok.com/@official_ffa\n\n"
        f"#FFA #FFALeicester #FootballForAll #Leicester #GrassrootsFootball #5aside #MondayLeague"
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    con = init_db()

    # Step 1: Check GoPro for new videos (Playwright only fires if cookies are stale)
    session, new_items = get_new_items(con)

    # Step 2: Nothing new — exit completely silently
    if not new_items:
        return

    # Step 3: Work to do — initialise logging and YouTube
    log.info("=" * 60)
    log.info(f"FFA GoPro -> YouTube uploader starting — {len(new_items)} new video(s)")

    yt = get_youtube_service()
    camera_labels = "ABCDEFGH"
    day_counters = defaultdict(int)

    for item in new_items:
        media_id    = item["id"]
        filename    = item["filename"]
        captured_at = item["captured_at"]
        date_key    = captured_at[:10]

        cam_index    = day_counters[date_key]
        camera_label = camera_labels[cam_index] if cam_index < len(camera_labels) else str(cam_index + 1)
        day_counters[date_key] += 1

        log.info(f"Processing: {filename} ({date_key}) Camera {camera_label} — {item.get('file_size', 0)/1e9:.1f} GB")

        dl_url = get_concat_download_url(session, media_id)
        if not dl_url:
            log.warning(f"Skipping {filename} — no download URL")
            mark_failed(con, media_id, "no download URL")
            continue

        dest = DOWNLOAD_DIR / filename
        if not download_video(dl_url, dest):
            mark_failed(con, media_id, "download failed")
            continue

        title       = make_title(filename, captured_at, camera_label)
        description = make_description(filename, captured_at, camera_label)
        yt_id       = upload_to_youtube(yt, dest, title, description, gopro_filename=filename)

        if yt_id:
            mark_uploaded(con, media_id, filename, captured_at, yt_id)
            dest.unlink()
            log.info(f"Done: {filename} Camera {camera_label} -> https://youtu.be/{yt_id}")
        else:
            mark_failed(con, media_id, "youtube upload failed")
            log.error(f"Upload failed for {filename} — will retry after 24h")

    log.info("All done.")

if __name__ == "__main__":
    run()


