#!/usr/bin/env python3
"""
FFA Clip Sheet Manager
======================
Manages the FFA Clips Google Sheet. Handles two jobs:

  1. sync-videos  — checks the YouTube channel for newly-public videos and
                    creates a tab + index row for each one. Run from the
                    scheduled gopro-upload workflow.

  2. process-clips — reads all video tabs, cuts any Pending clips using
                     yt-dlp --download-sections (fetches only the clip
                     timestamps, not the full video), uploads to Google Drive
                     and writes the Drive link back to the sheet.
                     Run from the clip-extractor workflow.

Sheet structure
---------------
  "Index" tab  — one row per video:
    A: Title | B: YouTube Link | C: Source Filename | D: Date | E: Tab Name | F: Status

  Per-video tab — header block (rows 1-4) then clip table from row 6:
    Row 1: Title      | <video title>
    Row 2: YouTube    | <hyperlink>
    Row 3: Source     | <GoPro filename or blank>
    Row 4: (blank)
    Row 5: Start | End | Name | Tags | Status | Link
    Row 6+: clip rows

Environment variables required
-------------------------------
  GOOGLE_SERVICE_ACCOUNT_JSON  — service account JSON key (GitHub secret)
  YOUTUBE_TOKEN                — existing YouTube OAuth token secret
  YOUTUBE_CREDENTIALS          — existing YouTube credentials secret
"""

import argparse
import json
import os
import sys
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

# ── Google API clients ────────────────────────────────────────────────────────

def get_sheets_service():
    """Sheets uses service account. Drive uses user OAuth token."""
    from google.oauth2 import service_account
    from google.oauth2.credentials import Credentials as OAuthCreds
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    # Sheets — service account
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    sa_creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    sheets = build("sheets", "v4", credentials=sa_creds, cache_discovery=False)

    # Drive — user OAuth token (service accounts can't upload to personal Drive)
    token_file = Path(__file__).parent / "youtube_token.json"
    token_json = os.environ.get("YOUTUBE_TOKEN", "")
    drive = None
    if token_file.exists():
        token_path = token_file
    elif token_json:
        token_path = Path("/tmp/youtube_token.json")
        token_path.write_text(token_json)
    else:
        token_path = None

    if token_path:
        scopes = [
            "https://www.googleapis.com/auth/youtube.upload",
            "https://www.googleapis.com/auth/youtube.readonly",
            "https://www.googleapis.com/auth/drive",
        ]
        try:
            drive_creds = OAuthCreds.from_authorized_user_file(str(token_path), scopes)
            if drive_creds and drive_creds.expired and drive_creds.refresh_token:
                drive_creds.refresh(Request())
            drive = build("drive", "v3", credentials=drive_creds, cache_discovery=False)
        except Exception as e:
            print(f"Warning: could not init Drive with OAuth token: {e}")

    return sheets, drive


FFA_CHANNEL_ID = "UCSj-hQdqQ9La4FMM3HFqvXw"
FFA_RSS_URL = f"https://www.youtube.com/feeds/videos.xml?channel_id={FFA_CHANNEL_ID}"


def get_recent_public_videos(lookback_days: int = 14):
    """
    Fetch recent public videos from the FFA YouTube RSS feed.
    Returns list of dicts: {video_id, title, published, yt_url}
    No OAuth required — RSS feed only shows public videos.
    """
    import urllib.request
    import xml.etree.ElementTree as ET
    from datetime import timedelta

    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    xml_data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(FFA_RSS_URL, timeout=15) as resp:
                xml_data = resp.read()
            break
        except Exception as e:
            if attempt < 2:
                import time; time.sleep(5)
            else:
                raise

    ns = {
        "atom":  "http://www.w3.org/2005/Atom",
        "yt":    "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    root = ET.fromstring(xml_data)
    videos = []
    for entry in root.findall("atom:entry", ns):
        video_id = entry.findtext("yt:videoId", namespaces=ns)
        title    = entry.findtext("atom:title", namespaces=ns)
        pub_str  = entry.findtext("atom:published", namespaces=ns)
        if not (video_id and title and pub_str):
            continue
        pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
        if pub_dt < cutoff:
            continue
        videos.append({
            "video_id":  video_id,
            "title":     title,
            "published": pub_str,
            "yt_url":    f"https://www.youtube.com/watch?v={video_id}",
        })
    return videos


# ── Sheet helpers ─────────────────────────────────────────────────────────────

SPREADSHEET_ID_FILE = Path(__file__).parent / ".ffa_sheet_id"
INDEX_TAB = "Index"
CLIP_HEADER = ["Start", "End", "Name", "Tags", "Status", "Link"]
PENDING = "Pending"
DONE    = "Done"
ADD_VIDEO_TAB = "Add Video"


def get_spreadsheet_id(sheets_svc):
    """Read the sheet ID from a local file (committed to repo)."""
    if SPREADSHEET_ID_FILE.exists():
        sid = SPREADSHEET_ID_FILE.read_text().strip()
        if sid:
            return sid
    raise RuntimeError(
        "No spreadsheet ID found. Create the sheet first and write its ID to "
        f"{SPREADSHEET_ID_FILE}"
    )


def ensure_index_tab(sheets_svc, spreadsheet_id):
    """Create the Index tab if it doesn't exist."""
    meta = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = [s["properties"]["title"] for s in meta["sheets"]]
    if INDEX_TAB not in existing:
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": INDEX_TAB, "index": 0}}}]},
        ).execute()
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{INDEX_TAB}!A1:F1",
            valueInputOption="USER_ENTERED",
            body={"values": [["Title", "YouTube", "Source Filename", "Date", "Tab Name", "Status"]]},
        ).execute()
        print(f"Created '{INDEX_TAB}' tab")


def tab_exists(sheets_svc, spreadsheet_id, title):
    meta = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    return any(s["properties"]["title"] == title for s in meta["sheets"])


def safe_tab_name(title: str) -> str:
    """Shorten and sanitise a video title for use as a sheet tab name (max 100 chars)."""
    safe = re.sub(r"[\\/*?\[\]:]", "", title)
    return safe[:100].strip()


def index_rows(sheets_svc, spreadsheet_id):
    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{INDEX_TAB}!A2:F",
    ).execute()
    return result.get("values", [])


def youtube_url_in_index(sheets_svc, spreadsheet_id, yt_url):
    for row in index_rows(sheets_svc, spreadsheet_id):
        if len(row) >= 2 and yt_url in row[1]:
            return True
    return False


def is_short_or_non_session(title: str) -> bool:
    """
    Filter out non-session videos (Shorts, highlights reels, etc).
    FFA session videos always contain the word "Session".
    """
    return "session" not in title.lower()


def _lookup_gopro_filename(youtube_id: str) -> str:
    """Check uploaded.db for a GoPro filename matching this YouTube video ID."""
    db_path = Path(__file__).parent / "uploaded.db"
    if not db_path.exists():
        return ""
    try:
        import sqlite3
        con = sqlite3.connect(str(db_path))
        row = con.execute(
            "SELECT filename FROM uploads WHERE youtube_id=?", (youtube_id,)
        ).fetchone()
        con.close()
        return row[0] if row else ""
    except Exception:
        return ""


# ── Job 1: sync-videos ────────────────────────────────────────────────────────

def sync_videos(lookback_days: int = 14):
    print(f"=== sync-videos (last {lookback_days} days) ===")
    sheets_svc, drive_svc = get_sheets_service()
    spreadsheet_id = get_spreadsheet_id(sheets_svc)
    ensure_index_tab(sheets_svc, spreadsheet_id)

    videos = get_recent_public_videos(lookback_days=lookback_days)
    print(f"  {len(videos)} public video(s) found in RSS feed")

    new_count = 0
    for v in videos:
        video_id  = v["video_id"]
        title     = v["title"]
        published = v["published"]
        yt_url    = v["yt_url"]

        if youtube_url_in_index(sheets_svc, spreadsheet_id, yt_url):
            continue

        if is_short_or_non_session(title):
            print(f"  Skipping non-session: {title}")
            continue

        candidate_tab = safe_tab_name(title)
        if tab_exists(sheets_svc, spreadsheet_id, candidate_tab):
            continue

        source_filename = _lookup_gopro_filename(video_id)

        tab_name = safe_tab_name(title)
        base, suffix = tab_name, 1
        while tab_exists(sheets_svc, spreadsheet_id, tab_name):
            tab_name = f"{base}_{suffix}"
            suffix += 1

        date_str = published[:10]
        sheet_gid = _create_video_tab(sheets_svc, spreadsheet_id, tab_name, title, yt_url, source_filename)
        _add_index_row(sheets_svc, spreadsheet_id, title, yt_url, source_filename, date_str, tab_name, sheet_gid)
        print(f"  + Created tab '{tab_name}' for: {title}")
        new_count += 1

    print(f"sync-videos complete. {new_count} new video(s) added.")


def _create_video_tab(sheets_svc, spreadsheet_id, tab_name, title, yt_url, source_filename) -> int:
    """Add a new tab with header block and clip table header. Returns the new sheet GID."""
    resp = sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()
    sheet_gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]

    yt_formula   = f'=HYPERLINK("{yt_url}","▶ Watch on YouTube")'
    source_value = source_filename if source_filename else "—"

    values = [
        ["Title",   title],
        ["YouTube", yt_formula],
        ["Source",  source_value],
        [],
        CLIP_HEADER,
    ]
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1:F5",
        valueInputOption="USER_ENTERED",
        body={"values": values},
    ).execute()
    return sheet_gid


def _add_index_row(sheets_svc, spreadsheet_id, title, yt_url, source_filename, date_str, tab_name, sheet_gid: int):
    yt_formula  = f'=HYPERLINK("{yt_url}","▶ Watch")'
    tab_formula = f'=HYPERLINK("#gid={sheet_gid}","{tab_name}")'
    sheets_svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{INDEX_TAB}!A:F",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [[title, yt_formula, source_filename, date_str, tab_formula, "Active"]]},
    ).execute()


# ── Job 2: process-clips ──────────────────────────────────────────────────────

def process_clips():
    """
    Reads all video tabs, processes Pending clips using yt-dlp --download-sections
    so only the exact clip timestamps are downloaded (not the full source video).
    Uploads clips to Drive and writes links back to sheet.
    """
    print("=== process-clips ===")
    sheets_svc, drive_svc = get_sheets_service()
    spreadsheet_id = get_spreadsheet_id(sheets_svc)

    meta = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    tab_names = [
        s["properties"]["title"]
        for s in meta["sheets"]
        if s["properties"]["title"] != INDEX_TAB
    ]

    total_processed = 0
    for tab in tab_names:
        processed = _process_tab(sheets_svc, drive_svc, spreadsheet_id, tab)
        total_processed += processed

    print(f"\nprocess-clips complete. {total_processed} clip(s) processed across {len(tab_names)} tab(s).")


def _process_tab(sheets_svc, drive_svc, spreadsheet_id, tab_name):
    """Process all Pending rows in one video tab."""
    header_result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1:F6",
        valueRenderOption="FORMULA",
    ).execute()
    data_result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1:F",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    header_rows = header_result.get("values", [])
    rows = data_result.get("values", [])
    if len(rows) < 5:
        return 0

    def cell(r, c):
        try: return header_rows[r][c]
        except IndexError: return ""

    yt_url         = _extract_url(cell(1, 1))
    gopro_filename = str(cell(2, 1))
    if gopro_filename.startswith("="):
        gopro_filename = ""
    if not yt_url:
        print(f"  [{tab_name}] No YouTube URL found — skipping")
        return 0

    clip_rows = rows[5:]
    pending_indices = []
    for i, row in enumerate(clip_rows):
        start = row[0] if len(row) > 0 else ""
        end   = row[1] if len(row) > 1 else ""
        link  = row[5] if len(row) > 5 else ""
        if start and end and not str(link).strip():
            pending_indices.append(i)

    if not pending_indices:
        return 0

    print(f"\n  [{tab_name}] {len(pending_indices)} pending clip(s)")

    # Mark all as Processing to avoid double-runs
    for i in pending_indices:
        sheet_row = i + 6
        _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, "Processing...")

    # Ensure Drive folder exists for this video
    drive_folder_id = _ensure_drive_folder(drive_svc, tab_name)

    # Get cookie args once (shared across clips in this tab)
    cookie_args = _get_cookie_args()

    processed = 0
    for i in pending_indices:
        row = clip_rows[i]
        sheet_row = i + 6
        start_str = row[0] if len(row) > 0 else ""
        end_str   = row[1] if len(row) > 1 else ""
        name      = row[2] if len(row) > 2 else f"clip_{i+1:02d}"
        tags      = row[3] if len(row) > 3 else ""

        safe_name = re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_") or f"clip_{i+1:02d}"

        try:
            start_s = _parse_ts(start_str)
            end_s   = _parse_ts(end_str)
        except ValueError as e:
            print(f"  ⚠️  Row {sheet_row}: bad timestamp — {e}")
            _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, f"Error: {e}")
            continue

        MAX_CLIP_SECONDS = 90  # 90 seconds max
        duration = end_s - start_s
        if duration > MAX_CLIP_SECONDS:
            msg = f"Skipped: clip too long ({duration/60:.1f} mins) — check timestamps"
            print(f"  ⚠️  Row {sheet_row}: {msg}")
            _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, msg)
            continue

        print(f"  ✂️  Fetching clip '{safe_name}' ({start_s:.1f}s → {end_s:.1f}s) from YouTube...")

        # Each clip gets its own tmpdir — cleaned up independently
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            out_path = tmp / f"{safe_name}.mp4"
            raw_path = tmp / "raw.mp4"

            try:
                _fetch_clip_section(yt_url, start_s, end_s, raw_path, cookie_args)
            except subprocess.CalledProcessError as e:
                print(f"  ❌ yt-dlp failed: {e}")
                _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, "Error: download failed")
                continue

            # Re-encode for iOS/CapCut compatibility
            try:
                _reencode_clip(raw_path, out_path)
            except subprocess.CalledProcessError as e:
                print(f"  ❌ ffmpeg re-encode failed: {e}")
                _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, "Error: encode failed")
                continue

            # Upload to Drive
            print(f"  📤 Uploading {out_path.name} to Drive...")
            try:
                file_id = _upload_to_drive(drive_svc, out_path, drive_folder_id, tags=tags)
                drive_link = f"https://drive.google.com/file/d/{file_id}/view"
            except Exception as e:
                print(f"  ❌ Drive upload failed: {e}")
                _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, "Error: upload failed")
                continue

        # Write Done + link back to sheet
        _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, DONE)
        link_formula = f'=HYPERLINK("{drive_link}","▶ View Clip")'
        _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 6, link_formula, raw=True)
        print(f"  ✅ Done: {safe_name}")
        processed += 1

    return processed


# ── Clip fetching ─────────────────────────────────────────────────────────────

def _secs_to_hhmmss(secs: float) -> str:
    """Convert seconds to HH:MM:SS.mmm for yt-dlp --download-sections."""
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    s = secs % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _get_cookie_args() -> list:
    """Return yt-dlp cookie args if available, else empty list.
    Priority: live Chrome profile (freshest) > cookies file > YOUTUBE_COOKIES env var
    """
    # 1. Live Chrome profile on Vultr self-hosted runner (freshest possible)
    chrome_profile = os.environ.get("CHROME_PROFILE_PATH", "/root/.config/chrome-ffa").strip()
    try:
        if Path(chrome_profile).exists():
            print(f"  Using live Chrome profile: {chrome_profile}")
            return ["--cookies-from-browser", f"chrome:{chrome_profile}"]
    except PermissionError:
        print(f"  Chrome profile not accessible at {chrome_profile} — falling back")

    # 2. Cookies file path from env var
    cookies_file = os.environ.get("YOUTUBE_COOKIES_FILE", "").strip()
    if cookies_file and Path(cookies_file).exists() and Path(cookies_file).stat().st_size > 0:
        print(f"  Using cookies file: {cookies_file}")
        return ["--cookies", cookies_file]

    # 3. Raw cookie content from env var
    cookies = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if cookies:
        cp = Path("/tmp/yt_cookies.txt")
        cp.write_text(cookies)
        print("  Using cookies from YOUTUBE_COOKIES env var")
        return ["--cookies", str(cp)]

    print("  No cookies found — attempting without (public videos only)")
    return []


def _fetch_clip_section(yt_url: str, start_s: float, end_s: float, out_path: Path, cookie_args: list):
    """
    Use yt-dlp --download-sections to fetch only the clip timestamp range.
    Tries without cookies first (works for public videos on clean IPs),
    falls back to cookies if that fails.
    """
    section = f"*{_secs_to_hhmmss(start_s)}-{_secs_to_hhmmss(end_s)}"
    fmt = (
        "bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height>=1080]+bestaudio/"
        "bestvideo+bestaudio/best"
    )
    base_cmd = [
        "yt-dlp",
        "--download-sections", section,
        "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", str(out_path),
        "--no-playlist", "--no-progress",
        "--force-keyframes-at-cuts",
    ]

    # Try without cookies first
    print(f"  Fetching section {section} (no cookies)...")
    result = subprocess.run(base_cmd + [yt_url])
    if result.returncode == 0 and out_path.exists():
        print("  ✅ Section download succeeded")
        return

    # Fall back to cookies
    if cookie_args:
        print("  Retrying with cookies...")
        subprocess.run(base_cmd + cookie_args + [yt_url], check=True)
    else:
        raise subprocess.CalledProcessError(result.returncode, base_cmd)


def _reencode_clip(raw_path: Path, out_path: Path):
    """Re-encode clip for iOS/CapCut compatibility."""
    subprocess.run([
        "ffmpeg", "-y",
        "-i", str(raw_path),
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-loglevel", "warning", "-stats",
        str(out_path),
    ], check=True)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _extract_url(cell_value: str) -> str:
    """Extract a URL from a plain string or HYPERLINK formula."""
    m = re.search(r'HYPERLINK\("([^"]+)"', cell_value)
    if m:
        return m.group(1)
    m = re.search(r"https?://\S+", cell_value)
    return m.group(0) if m else ""


def _parse_ts(ts) -> float:
    if isinstance(ts, (int, float)):
        if 0 < ts < 1:
            return float(ts) * 86400
        return float(ts)
    ts = str(ts).strip()
    if not ts:
        raise ValueError("Empty timestamp")
    if ":" not in ts:
        return float(ts)
    parts = [float(p) for p in ts.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        if parts[2] == 0:
            return parts[0] * 60 + parts[1]
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Unrecognised format: {ts}")


FFA_DRIVE_FOLDER_ID_FILE = Path(__file__).parent / ".ffa_drive_folder_id"


def _get_ffa_drive_folder_id() -> str:
    if FFA_DRIVE_FOLDER_ID_FILE.exists():
        return FFA_DRIVE_FOLDER_ID_FILE.read_text().strip()
    return ""


def _ensure_drive_folder(drive_svc, folder_name: str) -> str:
    """Get or create FFA/Clips/<folder_name> in Drive. Returns folder ID."""
    ffa_id = _get_ffa_drive_folder_id()
    if not ffa_id:
        raise RuntimeError(".ffa_drive_folder_id not set")
    clips_id = _find_or_create_folder(drive_svc, "Clips", parent_id=ffa_id)
    video_id = _find_or_create_folder(drive_svc, folder_name, parent_id=clips_id)
    return video_id


def _find_or_create_folder(drive_svc, name: str, parent_id: str) -> str:
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false and '{parent_id}' in parents"
    res = drive_svc.files().list(
        q=q, fields="files(id,name)", supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    if res["files"]:
        return res["files"][0]["id"]
    folder = drive_svc.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id",
        supportsAllDrives=True,
    ).execute()
    return folder["id"]


def _upload_to_drive(drive_svc, file_path: Path, folder_id: str, tags: str = "") -> str:
    """Upload a file to Drive folder. Returns file ID."""
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=False)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    description = "Tags: " + ", ".join(tag_list) if tag_list else ""

    file_meta = {
        "name": file_path.name,
        "parents": [folder_id],
        "description": description,
        "properties": {"ffa_tags": ",".join(tag_list)} if tag_list else {},
    }
    last_err = None
    for attempt in range(3):
        try:
            uploaded = drive_svc.files().create(
                body=file_meta, media_body=media, fields="id", supportsAllDrives=True
            ).execute()
            last_err = None
            break
        except Exception as e:
            last_err = e
            print(f"  Drive upload attempt {attempt+1} failed: {e} — retrying...")
            import time; time.sleep(10)
            media = MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=False)
    if last_err:
        raise last_err
    file_id = uploaded["id"]
    drive_svc.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
        supportsAllDrives=True,
    ).execute()
    return file_id


def _write_cell(sheets_svc, spreadsheet_id, tab, row_1indexed, col_1indexed, value, raw=False):
    col_letter = chr(ord("A") + col_1indexed - 1)
    range_str  = f"'{tab}'!{col_letter}{row_1indexed}"
    sheets_svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_str,
        valueInputOption="USER_ENTERED" if raw else "RAW",
        body={"values": [[value]]},
    ).execute()


# ── Entry point ───────────────────────────────────────────────────────────────



# ── Job 3: process-add-video ──────────────────────────────────────────────────

def _fetch_video_info(video_id: str) -> dict:
    """Fetch title and published date for a YouTube video.
    Priority: YouTube page (public upload date) > RSS feed > oEmbed (title only)
    """
    import urllib.request
    import xml.etree.ElementTree as ET
    import json as _json

    title = None
    published = ""

    # 1. Scrape YouTube page for upload date and title (works for all public videos)
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/watch?v={video_id}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="ignore")
        import re as _re
        # Upload date
        m = _re.search(r'"uploadDate":"([^"]+)"', html)
        if not m:
            m = _re.search(r'"datePublished":"([^"]+)"', html)
        if m:
            published = m.group(1)[:10]  # YYYY-MM-DD
        # Title
        m2 = _re.search(r'"title":"([^"]+)"', html)
        if m2:
            title = m2.group(1)
    except Exception as e:
        print(f"  Warning: could not scrape YouTube page for {video_id}: {e}")

    # 2. RSS feed fallback (only covers last ~15 videos)
    if not published or not title:
        try:
            rss_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={FFA_CHANNEL_ID}"
            with urllib.request.urlopen(rss_url, timeout=15) as resp:
                xml_data = resp.read()
            ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
            root = ET.fromstring(xml_data)
            for entry in root.findall("atom:entry", ns):
                if entry.findtext("yt:videoId", namespaces=ns) == video_id:
                    if not published:
                        published = (entry.findtext("atom:published", namespaces=ns) or "")[:10]
                    if not title:
                        title = entry.findtext("atom:title", namespaces=ns) or ""
                    break
        except Exception:
            pass

    # 3. oEmbed fallback for title only
    if not title:
        try:
            oembed_url = f"https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v={video_id}&format=json"
            with urllib.request.urlopen(oembed_url, timeout=10) as resp:
                title = _json.loads(resp.read()).get("title", "")
        except Exception:
            pass

    return {"title": title or f"Video {video_id}", "published": published}


def ensure_add_video_tab(sheets_svc, spreadsheet_id):
    """Create the Add Video tab if it doesn't exist."""
    meta = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    existing = [s["properties"]["title"] for s in meta["sheets"]]
    if ADD_VIDEO_TAB not in existing:
        sheets_svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": [{"addSheet": {"properties": {"title": ADD_VIDEO_TAB, "index": 1}}}]},
        ).execute()
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"'{ADD_VIDEO_TAB}'!A1:B1",
            valueInputOption="USER_ENTERED",
            body={"values": [["YouTube URL", "Status"]]},
        ).execute()
        print(f"Created '{ADD_VIDEO_TAB}' tab")


def process_add_video(sheets_svc, drive_svc, spreadsheet_id):
    """
    Reads the 'Add Video' tab and creates a clip tab for any unprocessed URLs.
    Kris pastes YouTube URLs in column A; this writes status back to column B.
    """
    ensure_add_video_tab(sheets_svc, spreadsheet_id)

    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{ADD_VIDEO_TAB}'!A2:B",
        valueRenderOption="FORMATTED_VALUE",
    ).execute()
    rows = result.get("values", [])
    if not rows:
        print("No URLs in Add Video tab")
        return

    # Get existing video IDs from Index to detect duplicates
    existing_rows = index_rows(sheets_svc, spreadsheet_id)
    existing_ids = set()
    for row in existing_rows:
        yt_url = row[1] if len(row) > 1 else ""
        vid_id = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", yt_url)
        if vid_id:
            existing_ids.add(vid_id.group(1))

    processed = 0
    for i, row in enumerate(rows):
        sheet_row = i + 2
        url_raw = row[0].strip() if len(row) > 0 else ""
        status  = row[1].strip() if len(row) > 1 else ""

        if not url_raw or status in ("Done", "Already exists", "Error: invalid URL"):
            continue

        # Extract video ID
        m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", url_raw)
        if not m:
            # Try short URL format youtu.be/VIDEO_ID
            m = re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", url_raw)
        if not m:
            _write_cell(sheets_svc, spreadsheet_id, ADD_VIDEO_TAB, sheet_row, 2, "Error: invalid URL")
            continue

        video_id = m.group(1)
        yt_url   = f"https://www.youtube.com/watch?v={video_id}"

        # Check for duplicate
        if video_id in existing_ids:
            print(f"  Skipping {video_id} — already in sheet")
            _write_cell(sheets_svc, spreadsheet_id, ADD_VIDEO_TAB, sheet_row, 2, "Already exists")
            continue

        # Mark as processing
        _write_cell(sheets_svc, spreadsheet_id, ADD_VIDEO_TAB, sheet_row, 2, "Processing...")

        # Fetch video info
        info = _fetch_video_info(video_id)
        title     = info["title"]
        published = info["published"]
        date_str  = published[:10] if published else ""

        # Generate unique tab name — use full title, append date suffix if collision
        candidate = safe_tab_name(title)
        tab_name  = candidate
        if tab_exists(sheets_svc, spreadsheet_id, tab_name):
            # Use date as suffix if available, otherwise fall back to video ID
            if date_str:
                try:
                    from datetime import datetime as _dt
                    d = _dt.strptime(date_str, "%Y-%m-%d")
                    date_suffix = d.strftime("%-d %B %Y")
                except Exception:
                    date_suffix = date_str
                tab_name = safe_tab_name(f"{title} | {date_suffix}")
            else:
                tab_name = safe_tab_name(f"{title} | {video_id[:8]}")
            # Final safety check
            suffix = 1
            base = tab_name
            while tab_exists(sheets_svc, spreadsheet_id, tab_name):
                tab_name = f"{base}_{suffix}"
                suffix += 1

        source_filename = _lookup_gopro_filename(video_id)

        try:
            sheet_gid = _create_video_tab(sheets_svc, spreadsheet_id, tab_name, title, yt_url, source_filename)
            _add_index_row(sheets_svc, spreadsheet_id, title, yt_url, source_filename, date_str, tab_name, sheet_gid)
            _write_cell(sheets_svc, spreadsheet_id, ADD_VIDEO_TAB, sheet_row, 2, "Done")
            existing_ids.add(video_id)
            print(f"  + Created tab '{tab_name}' for: {title}")
            processed += 1
        except Exception as e:
            _write_cell(sheets_svc, spreadsheet_id, ADD_VIDEO_TAB, sheet_row, 2, f"Error: {e}")
            print(f"  ❌ Failed for {video_id}: {e}")

    print(f"process-add-video complete. {processed} tab(s) created.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["sync-videos", "process-clips", "process-add-video"],
                    help="sync-videos: create tabs for newly-public videos. "
                         "process-clips: cut pending clips and upload to Drive.")
    ap.add_argument("--lookback-days", type=int, default=14,
                    help="How many days back to scan for new public videos (default: 14)")
    args = ap.parse_args()

    if args.job == "sync-videos":
        sync_videos(lookback_days=args.lookback_days)
    elif args.job == "process-clips":
        process_clips()
    elif args.job == "process-add-video":
        sheets_svc, drive_svc = get_sheets_service()
        spreadsheet_id = get_spreadsheet_id(sheets_svc)
        process_add_video(sheets_svc, drive_svc, spreadsheet_id)
