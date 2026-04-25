#!/usr/bin/env python3
"""FFA GoPro Cloud → YouTube uploader."""

import hashlib
import json
import logging
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "uploaded.db"
LOG_PATH = BASE_DIR / "logs" / "uploader.log"
CREDS_PATH = BASE_DIR / "youtube_credentials.json"
TOKEN_PATH = BASE_DIR / "youtube_token.json"
COOKIE_PATH = BASE_DIR / "gopro_cookies.json"
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
LOG_PATH.parent.mkdir(exist_ok=True)

GOPRO_API = "https://api.gopro.com"
GOPRO_HEADERS = {"Accept": "application/vnd.gopro.jk.media+json; version=2.0.0"}
YT_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
MIN_VIDEO_SIZE_BYTES = 100_000_000

LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS") or 0)
MANUAL_GOPRO_FILENAME = (os.environ.get("MANUAL_GOPRO_FILENAME") or "").strip()
DIRECT_UPLOAD_URL = (os.environ.get("DIRECT_UPLOAD_URL") or "").strip()
DIRECT_UPLOAD_FILENAME = (os.environ.get("DIRECT_UPLOAD_FILENAME") or "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOG_PATH, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
)
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
log = logging.getLogger(__name__)


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS uploads (
            media_id TEXT PRIMARY KEY,
            filename TEXT,
            captured_at TEXT,
            youtube_id TEXT,
            uploaded_at TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS failed_uploads (
            media_id TEXT PRIMARY KEY,
            failed_at TEXT,
            reason TEXT
        )
    """)
    con.commit()
    return con


def already_uploaded(con, media_id, filename=None, include_filename_fallback=True):
    row = con.execute("SELECT youtube_id FROM uploads WHERE media_id=?", (media_id,)).fetchone()
    if row:
        return True
    if filename and include_filename_fallback:
        row = con.execute("SELECT media_id, youtube_id FROM uploads WHERE filename=?", (filename,)).fetchone()
        if row:
            return True
        row = con.execute("SELECT media_id, youtube_id FROM uploads WHERE media_id=?", (f"filename:{filename}",)).fetchone()
        if row:
            return True
    return False


def db_upload_rows_for_filename(con, filename):
    if not filename:
        return []
    rows = con.execute(
        "SELECT media_id, filename, captured_at, youtube_id, uploaded_at FROM uploads WHERE filename=? OR media_id=? ORDER BY uploaded_at DESC",
        (filename, f"filename:{filename}"),
    ).fetchall()
    return rows


def recently_failed(con, media_id):
    row = con.execute("SELECT failed_at FROM failed_uploads WHERE media_id=?", (media_id,)).fetchone()
    if not row:
        return False
    failed_at = datetime.fromisoformat(row[0])
    return (datetime.now(timezone.utc) - failed_at) < timedelta(hours=24)


def mark_uploaded(con, media_id, filename, captured_at, youtube_id):
    con.execute("DELETE FROM failed_uploads WHERE media_id=?", (media_id,))
    con.execute(
        "INSERT OR REPLACE INTO uploads (media_id, filename, captured_at, youtube_id, uploaded_at) VALUES (?,?,?,?,?)",
        (media_id, filename, captured_at, youtube_id, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()


def mark_failed(con, media_id, reason):
    con.execute("INSERT OR REPLACE INTO failed_uploads VALUES (?,?,?)", (media_id, datetime.now(timezone.utc).isoformat(), reason))
    con.commit()


def load_cookies_from_file():
    if not COOKIE_PATH.exists():
        return {}
    return json.loads(COOKIE_PATH.read_text())


def refresh_cookies_via_playwright():
    email = os.environ.get("GOPRO_EMAIL")
    password = os.environ.get("GOPRO_PASSWORD")
    if not email or not password:
        log.error("GOPRO_EMAIL / GOPRO_PASSWORD env vars not set — cannot refresh cookies")
        return False
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        log.error("Playwright not installed")
        return False

    log.info("Refreshing GoPro cookies via Playwright...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = browser.new_context(user_agent="Mozilla/5.0 Chrome/120.0.0.0 Safari/537.36")
            page = context.new_page()
            page.goto("https://gopro.com/login", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("input#email", timeout=15000)
            page.fill("input#email", email)
            page.fill("input#password", password)
            page.click("button.Login_loginButton__iaFNb")
            try:
                page.wait_for_url(lambda url: "gopro.com/login" not in url, timeout=20000)
            except PlaywrightTimeout:
                log.error(f"Playwright login failed — still on: {page.url}")
                browser.close()
                return False
            if "plus.gopro.com" not in page.url:
                page.goto("https://plus.gopro.com", wait_until="domcontentloaded", timeout=20000)
            cookies = context.cookies()
            browser.close()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        COOKIE_PATH.write_text(json.dumps(cookie_dict, indent=2))
        log.info(f"Cookie refresh successful — {len(cookie_dict)} cookies saved")
        return True
    except Exception as e:
        log.error(f"Playwright cookie refresh failed: {e}")
        return False


def make_session(cookies):
    session = requests.Session()
    session.cookies.update(cookies)
    return session


def gopro_get(session, path, params=None):
    r = session.get(f"{GOPRO_API}{path}", headers=GOPRO_HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def get_authenticated_gopro_session():
    session = make_session(load_cookies_from_file())
    try:
        gopro_get(session, "/media/search", params={"fields": "id", "per_page": 1, "page": 1, "type": "Video"})
        return session
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            log.warning("GoPro cookies returned 401 — refreshing via Playwright")
            if not refresh_cookies_via_playwright():
                log.error("Cookie refresh failed — giving up")
                sys.exit(1)
            return make_session(load_cookies_from_file())
        raise


def fetch_recent_media(session, con=None):
    if LOOKBACK_DAYS > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log.info(f"GoPro scan cutoff: last {LOOKBACK_DAYS} day(s), after {cutoff}")
    elif con is not None:
        row = con.execute("SELECT MAX(captured_at) FROM uploads").fetchone()
        cutoff = row[0] if row and row[0] else (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log.info(f"GoPro scan cutoff: after {cutoff}")
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        log.info(f"GoPro scan cutoff: defaulting to after {cutoff}")

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
        log.info(f"GoPro page {page}: {len(items)} video item(s) returned")
        if not items:
            break
        for item in items:
            filename = item.get("filename", "")
            cap = item.get("captured_at", "")
            media_id = item.get("id", "")
            file_size = int(item.get("file_size", 0) or 0)
            log.info(f"GoPro candidate: {filename or '(no filename)'} | id={media_id or '(no id)'} | captured_at={cap or '(no date)'} | size={file_size / 1e9:.2f} GB")
            if cap and cap < cutoff:
                log.info(f"Stopping scan at {filename or media_id} ({cap}) because it is older than cutoff {cutoff}")
                return all_media
            all_media.append(item)
        pages = data.get("_pages", {})
        if page >= pages.get("total_pages", 1):
            break
        page += 1
    return all_media


def fetch_media_by_filename(session, filename, max_pages=10):
    wanted = filename.strip().lower()
    log.info(f"Manual GoPro filename mode: searching for exact filename {filename}")
    for page in range(1, max_pages + 1):
        data = gopro_get(session, "/media/search", params={
            "fields": "id,captured_at,filename,file_size,type",
            "order_by": "captured_at",
            "order": "desc",
            "per_page": 50,
            "page": page,
            "type": "Video",
        })
        items = data.get("_embedded", {}).get("media", [])
        log.info(f"Manual filename search page {page}: {len(items)} video item(s) returned")
        for item in items:
            if (item.get("filename") or "").lower() == wanted:
                log.info(f"Found manual GoPro file: {item.get('filename')} | id={item.get('id')} | captured_at={item.get('captured_at')} | size={int(item.get('file_size', 0) or 0)/1e9:.2f} GB")
                return item
        pages = data.get("_pages", {})
        if page >= pages.get("total_pages", 1):
            break
    log.error(f"Manual GoPro filename not found in recent Cloud pages: {filename}")
    return None


def describe_media_filter(con, item):
    media_id = item.get("id", "")
    filename = item.get("filename", "")
    captured_at = item.get("captured_at", "")
    file_size = int(item.get("file_size", 0) or 0)
    reasons = []
    if already_uploaded(con, media_id, filename, include_filename_fallback=False):
        rows = db_upload_rows_for_filename(con, filename)
        details = "; ".join([f"db_media_id={r[0]}, youtube_id={r[3]}, uploaded_at={r[4]}" for r in rows[:3]])
        reasons.append(f"already uploaded by exact GoPro media_id ({details or 'media_id matched'})")
    else:
        filename_rows = db_upload_rows_for_filename(con, filename)
        if filename_rows:
            details = "; ".join([f"db_media_id={r[0]}, youtube_id={r[3]}, uploaded_at={r[4]}" for r in filename_rows[:3]])
            log.warning(f"Filename reuse detected for {filename}: DB has older filename match, but media_id is different so this will NOT block upload. {details}")
    if recently_failed(con, media_id):
        reasons.append("recently failed within 24h cooldown")
    if file_size <= MIN_VIDEO_SIZE_BYTES:
        reasons.append(f"file below 100MB filter ({file_size / 1e6:.1f} MB)")
    summary = f"{filename or '(no filename)'} | id={media_id or '(no id)'} | captured_at={captured_at or '(no date)'} | size={file_size / 1e9:.2f} GB"
    return reasons, summary


def get_new_items(con):
    session = get_authenticated_gopro_session()
    media_items = fetch_recent_media(session, con=con)
    log.info(f"GoPro scan found {len(media_items)} video item(s) before local filtering")
    new_items = []
    for item in media_items:
        reasons, summary = describe_media_filter(con, item)
        if reasons:
            log.info(f"Filtered out: {summary} — {'; '.join(reasons)}")
        else:
            log.info(f"Accepted for upload: {summary}")
            new_items.append(item)
    log.info(f"GoPro scan accepted {len(new_items)} new video(s) after filtering")
    return session, new_items


def get_concat_download_url(session, media_id):
    quality_preference = ["2160p", "3000p", "1440p"]
    try:
        data = gopro_get(session, f"/media/{media_id}/download")
        variations = data.get("_embedded", {}).get("variations", [])
        for label in ["concat", "source"]:
            for quality in quality_preference:
                for v in variations:
                    if v.get("label") == label and v.get("quality") == quality and v.get("available"):
                        log.info(f"Using GoPro download variant: {label} {quality}")
                        return v["url"]
        log.warning(f"No preferred 4K variant found for {media_id}")
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
            dest_path.unlink(missing_ok=True)
        log.info(f"Downloading to {dest_path.name} (attempt {attempt})...")
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                start_time = time.time()
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        elapsed = time.time() - start_time
                        speed = downloaded / elapsed if elapsed > 0 else 0
                        if total and speed > 0:
                            remaining = (total - downloaded) / speed
                            mins, secs = divmod(int(remaining), 60)
                            pct = downloaded / total * 100
                            print(f"\r  {pct:.1f}% ({downloaded/1e9:.2f}/{total/1e9:.2f} GB) — {speed/1e6:.1f} MB/s — ETA: {mins}m {secs}s    ", end="", flush=True)
                print()
            log.info(f"Download complete: {dest_path.stat().st_size / 1e9:.2f} GB")
            return True
        except Exception as e:
            log.error(f"Download error (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                dest_path.unlink(missing_ok=True)
                return False
    return False


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
        TOKEN_PATH.write_text(creds.to_json())
    return build("youtube", "v3", credentials=creds)


def youtube_video_exists(service, media_id, filename, skip_filename_query=False):
    queries = [f"FFA_MEDIA_ID:{media_id}"]
    if not skip_filename_query:
        queries.extend([f"FFA_FILENAME:{filename}", f"\"{filename}\""])

    for q in queries:
        try:
            resp = service.search().list(part="snippet", forMine=True, type="video", maxResults=5, q=q).execute()
            if resp.get("items"):
                log.info(f"YouTube duplicate check matched query: {q}")
                return True
        except Exception as e:
            log.warning(f"YouTube duplicate check failed for query {q}: {e}")
    return False


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
    monday_section = "\n\nThis is part of the FFA Monday League — our weekly competitive kickabout with individual player standings.\nCheck the league table: https://www.officialffa.co.uk/mnl\n" if is_monday else ""
    return (
        f"FFA Leicester | {day_name} Session | {date_str}\n"
        f"{cam_line}\nCompetitive kickabouts in Leicester, running weekly. All levels welcome.\n"
        f"{monday_section}\nWant to play? Book your spot:\nhttps://www.officialffa.co.uk\n\n"
        f"Interested in tournaments or events? Get in touch via our website or socials.\n\n"
        f"Shop FFA merch: https://www.officialffa.co.uk/store\n\n"
        f"Follow us:\nInstagram: https://www.instagram.com/_official_ffa\nTikTok: https://www.tiktok.com/@official_ffa\n\n"
        f"#FFA #FFALeicester #FootballForAll #Leicester #GrassrootsFootball #5aside #MondayLeague"
    )


def filename_from_direct_url(url):
    name = Path(urlparse(url).path).name
    return name if name and "." in name else f"manual_upload_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.mp4"


def upload_item(con, yt, session, item, camera_label="", force=False):
    media_id = item["id"]
    filename = item["filename"]
    captured_at = item["captured_at"]
    file_size = int(item.get("file_size", 0) or 0)
    log.info(f"Processing: {filename} ({captured_at[:10]}) Camera {camera_label or '-'} — {file_size/1e9:.1f} GB")
    dl_url = get_concat_download_url(session, media_id)
    if not dl_url:
        log.warning(f"Skipping {filename} — no download URL")
        mark_failed(con, media_id, "no download URL")
        return
    dest = DOWNLOAD_DIR / filename
    if not download_video(dl_url, dest):
        mark_failed(con, media_id, "download failed")
        return
    if not force and youtube_video_exists(yt, media_id, filename, skip_filename_query=True):
        log.warning(f"Skipping {filename} — exact media ID already found on YouTube")
        mark_uploaded(con, media_id, filename, captured_at, "existing")
        dest.unlink(missing_ok=True)
        return
    if force:
        log.warning(f"Manual force upload enabled for {filename}; skipping DB/filename duplicate blockers")
    description = make_description(filename, captured_at, camera_label)
    description += f"\n\nFFA_MEDIA_ID:{media_id}\nFFA_FILENAME:{filename}"
    yt_id = upload_to_youtube(yt, dest, make_title(filename, captured_at, camera_label), description, gopro_filename=filename)
    if yt_id:
        mark_uploaded(con, media_id, filename, captured_at, yt_id)
        dest.unlink(missing_ok=True)
        log.info(f"Done: {filename} Camera {camera_label or '-'} -> https://youtu.be/{yt_id}")
    else:
        mark_failed(con, media_id, "youtube upload failed")
        log.error(f"Upload failed for {filename} — will retry after 24h")


def run_direct_upload(con):
    if not DIRECT_UPLOAD_URL:
        return False
    filename = DIRECT_UPLOAD_FILENAME or filename_from_direct_url(DIRECT_UPLOAD_URL)
    captured_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    url_hash = hashlib.sha256(DIRECT_UPLOAD_URL.encode("utf-8")).hexdigest()[:16]
    media_id = f"direct:{filename}:{url_hash}"
    log.info("=" * 60)
    log.info("Manual direct URL upload mode enabled")
    log.info(f"Direct URL filename: {filename}")
    if already_uploaded(con, media_id, filename):
        log.warning(f"Skipping manual upload for {filename} — already in uploaded.db")
        return True
    yt = get_youtube_service()
    if youtube_video_exists(yt, media_id, filename):
        log.warning(f"Skipping manual upload for {filename} — already found on YouTube")
        mark_uploaded(con, media_id, filename, captured_at, "existing")
        return True
    dest = DOWNLOAD_DIR / filename
    if not download_video(DIRECT_UPLOAD_URL, dest):
        mark_failed(con, media_id, "direct URL download failed")
        log.error(f"Manual upload failed for {filename} — could not download direct URL")
        return True
    description = make_description(filename, captured_at, "Manual")
    description += f"\n\nFFA_MEDIA_ID:{media_id}\nFFA_FILENAME:{filename}\nFFA_DIRECT_UPLOAD_URL:{DIRECT_UPLOAD_URL}"
    yt_id = upload_to_youtube(yt, dest, make_title(filename, captured_at, "Manual"), description, gopro_filename=filename)
    if yt_id:
        mark_uploaded(con, media_id, filename, captured_at, yt_id)
        dest.unlink(missing_ok=True)
        log.info(f"Manual upload done: {filename} -> https://youtu.be/{yt_id}")
    else:
        mark_failed(con, media_id, "direct URL youtube upload failed")
    return True


def run_manual_gopro_filename(con):
    if not MANUAL_GOPRO_FILENAME:
        return False
    session = get_authenticated_gopro_session()
    item = fetch_media_by_filename(session, MANUAL_GOPRO_FILENAME)
    if not item:
        return True
    reasons, summary = describe_media_filter(con, item)
    if reasons:
        log.warning(f"Manual filename matched but would normally be filtered: {summary} — {'; '.join(reasons)}")
        if any("file below 100MB" in r for r in reasons):
            log.error("Manual upload stopped because the matched file is below the 100MB safety filter")
            return True
        for row in db_upload_rows_for_filename(con, item.get("filename")):
            log.warning(f"Manual DB match: media_id={row[0]} filename={row[1]} captured_at={row[2]} youtube_id={row[3]} uploaded_at={row[4]}")
        log.info("Manual filename override will force upload unless it is below 100MB")
    yt = get_youtube_service()
    upload_item(con, yt, session, item, camera_label="Manual", force=True)
    return True


def run():
    con = init_db()
    if run_direct_upload(con):
        return
    if run_manual_gopro_filename(con):
        return

    session, new_items = get_new_items(con)
    if not new_items:
        log.info("No new GoPro videos accepted for upload after filtering. See filter logs above for details.")
        return
    log.info("=" * 60)
    log.info(f"FFA GoPro -> YouTube uploader starting — {len(new_items)} new video(s)")
    yt = get_youtube_service()
    camera_labels = "ABCDEFGH"
    day_counters = defaultdict(int)
    for item in new_items:
        date_key = item["captured_at"][:10]
        cam_index = day_counters[date_key]
        camera_label = camera_labels[cam_index] if cam_index < len(camera_labels) else str(cam_index + 1)
        day_counters[date_key] += 1
        upload_item(con, yt, session, item, camera_label=camera_label)
    log.info("All done.")


if __name__ == "__main__":
    run()
