#!/usr/bin/env python3
"""
FFA Clip Sheet Manager
======================
Manages the FFA Clips Google Sheet. Handles two jobs:

  1. sync-videos  — checks the YouTube channel for newly-public videos and
                    creates a tab + index row for each one. Run from the
                    scheduled gopro-upload workflow.

  2. process-clips — reads all video tabs, cuts any Pending clips, uploads
                     to Google Drive and writes the Drive link back to the sheet.
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
    # Prefer file written by workflow step; fall back to env var
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
    # Retry up to 3 times on transient errors (RSS occasionally 404s)
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
        # Write header
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


def is_short(video_id: str) -> bool:
    """Check if a YouTube video is a Short by requesting the /shorts/ URL."""
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/shorts/{video_id}",
            headers={"User-Agent": "Mozilla/5.0"},
            method="HEAD"
        )
        # Don't follow redirects — Shorts return 200, regular videos redirect away
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        resp = opener.open(req, timeout=5)
        return resp.status == 200
    except Exception:
        return False  # If in doubt, include it


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
    """
    Checks the YouTube channel for public videos published within the last
    lookback_days days that are not yet in the sheet. Creates a tab + index
    row for each new one.
    """
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
            continue  # already in index

        # Skip YouTube Shorts
        if is_short(video_id):
            print(f"  Skipping Short: {title}")
            continue

        # Also check if a tab with this name already exists (race condition guard)
        candidate_tab = safe_tab_name(title)
        if tab_exists(sheets_svc, spreadsheet_id, candidate_tab):
            continue  # tab already exists

        # Try to match to a GoPro filename via uploaded.db in repo
        source_filename = _lookup_gopro_filename(video_id)

        tab_name = safe_tab_name(title)
        # Deduplicate tab name if it clashes
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
    Reads all video tabs, processes Pending clips, uploads to Drive, writes links back.
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
    # Fetch header with FORMULA (for URL extraction) and data with FORMATTED_VALUE (for timestamps as strings)
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
        return 0  # no clip table yet

    # Read header block from FORMULA-rendered data (need URLs intact)
    def cell(r, c):
        try: return header_rows[r][c]
        except IndexError: return ""

    yt_url         = _extract_url(cell(1, 1))  # row 2 col B
    gopro_filename = str(cell(2, 1))                # row 3 col B (Source)
    if gopro_filename.startswith("="):
        gopro_filename = ""
    if not yt_url:
        print(f"  [{tab_name}] No YouTube URL found — skipping")
        return 0

    # Clip rows start at row index 5 (row 6 in sheet)
    clip_rows = rows[5:]
    pending_indices = []
    for i, row in enumerate(clip_rows):
        start   = row[0] if len(row) > 0 else ""
        end     = row[1] if len(row) > 1 else ""
        link    = row[5] if len(row) > 5 else ""
        # Process any row with timestamps but no Drive link yet
        if start and end and not str(link).strip():
            pending_indices.append(i)

    if not pending_indices:
        return 0

    print(f"\n  [{tab_name}] {len(pending_indices)} pending clip(s)")

    # Mark all as Processing to avoid double-runs
    for i in pending_indices:
        sheet_row = i + 6  # 1-indexed, offset by 5 header rows + 1
        _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, "Processing...")

    # Ensure Drive folder exists for this video
    drive_folder_id = _ensure_drive_folder(drive_svc, tab_name)

    # Download source video once
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        print(f"  Downloading source (gopro={gopro_filename or 'n/a'}): {yt_url}")
        try:
            source = _download_source(yt_url, tmp, drive_svc=drive_svc, gopro_filename=gopro_filename)
        except subprocess.CalledProcessError as e:
            print(f"  ❌ Download failed: {e}")
            for i in pending_indices:
                sheet_row = i + 6
                _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, "Error: download failed")
            return 0

        clips_dir = tmp / "clips"
        clips_dir.mkdir()
        processed = 0

        for i in pending_indices:
            row = clip_rows[i]
            sheet_row = i + 6
            start_str = row[0] if len(row) > 0 else ""
            end_str   = row[1] if len(row) > 1 else ""
            name      = row[2] if len(row) > 2 else f"clip_{i+1:02d}"
            tags      = row[3] if len(row) > 3 else ""

            # Sanitise name — tags go to Drive metadata, not the filename
            safe_name = re.sub(r"[^A-Za-z0-9_\-]+", "_", name).strip("_") or f"clip_{i+1:02d}"
            filename  = safe_name

            try:
                start_s = _parse_ts(start_str)
                end_s   = _parse_ts(end_str)
            except ValueError as e:
                print(f"  ⚠️  Row {sheet_row}: bad timestamp — {e}")
                _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, f"Error: {e}")
                continue

            out_path = clips_dir / f"{filename}.mp4"
            print(f"  ✂️  Cutting {filename} ({start_s:.1f}s → {end_s:.1f}s)")
            try:
                _cut_clip(source, start_s, end_s, out_path)
            except subprocess.CalledProcessError as e:
                print(f"  ❌ ffmpeg failed: {e}")
                _write_cell(sheets_svc, spreadsheet_id, tab_name, sheet_row, 5, "Error: cut failed")
                continue

            # Upload to Drive
            print(f"  📤 Uploading {out_path.name} to Drive")
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
            print(f"  ✅ Done: {filename}")
            processed += 1

    return processed


# ── Utilities ─────────────────────────────────────────────────────────────────

def _extract_url(cell_value: str) -> str:
    """Extract a URL from a plain string or HYPERLINK formula."""
    m = re.search(r'HYPERLINK\("([^"]+)"', cell_value)
    if m:
        return m.group(1)
    m = re.search(r"https?://\S+", cell_value)
    return m.group(0) if m else ""



def _parse_ts(ts) -> float:
    # Sheets sometimes returns numeric values for timestamp-like cells.
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
        # If trailing :00 — Sheets added it to user input like "37:06" → "37:06:00"
        if parts[2] == 0:
            return parts[0] * 60 + parts[1]
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Unrecognised format: {ts}")


def _find_drive_source(drive_svc, gopro_filename: str) -> str:
    """Look for a GoPro source file in Drive: FFA/Sources/. Returns file ID or empty string."""
    if not drive_svc or not gopro_filename or gopro_filename == "—":
        return ""
    try:
        res = drive_svc.files().list(
            q=f"name='{gopro_filename}' and trashed=false",
            fields="files(id,name)",
            spaces="drive",
        ).execute()
        if res["files"]:
            print(f"Found Drive source for {gopro_filename}: {res['files'][0]['id']}")
            return res["files"][0]["id"]
    except Exception as e:
        print(f"Drive source lookup failed: {e}")
    return ""


def _download_from_drive(drive_svc, file_id: str, work_dir: Path) -> Path:
    """Download a file from Drive by file ID."""
    import io
    from googleapiclient.http import MediaIoBaseDownload
    out_path = work_dir / "source.mp4"
    request = drive_svc.files().get_media(fileId=file_id)
    with open(out_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                print(f"  Drive download: {int(status.progress() * 100)}%")
    return out_path


def _download_source(url: str, work_dir: Path, drive_svc=None, gopro_filename: str = "") -> Path:
    # Try Drive first for GoPro sources — no bot detection, works forever
    if drive_svc and gopro_filename:
        file_id = _find_drive_source(drive_svc, gopro_filename)
        if file_id:
            print(f"Downloading source from Drive: {gopro_filename}")
            return _download_from_drive(drive_svc, file_id, work_dir)

    # Fall back to yt-dlp for XbotGo or when Drive source not available
    print(f"Downloading source from YouTube: {url}")
    fmt = (
        "bestvideo[height>=2160][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height>=2160]+bestaudio/"
        "bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height>=1080]+bestaudio/"
        "bestvideo+bestaudio/best"
    )
    # Find cookies file — written by workflow or env var fallback
    cookie_args = []
    cookie_candidates = [
        Path(__file__).parent / "yt_cookies.txt",
        Path("/tmp/yt_cookies.txt"),
    ]
    for p in cookie_candidates:
        if p.exists() and p.stat().st_size > 0:
            print(f"Using cookies from {p}")
            cookie_args = ["--cookies", str(p)]
            break
    else:
        cookies = os.environ.get("YOUTUBE_COOKIES", "").strip()
        if cookies:
            cp = Path("/tmp/yt_cookies.txt")
            cp.write_text(cookies)
            cookie_args = ["--cookies", str(cp)]
            print("Using cookies from env var")
        else:
            print("Warning: no YouTube cookies — bot detection may block download")

    subprocess.run([
        "yt-dlp", "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", str(work_dir / "source.%(ext)s"),
        "--no-playlist", "--no-progress",
    ] + cookie_args + [url], check=True)
    matches = list(work_dir.glob("source.*"))
    if not matches:
        raise RuntimeError("Download produced no file")
    return matches[0]


def _cut_clip(source: Path, start: float, end: float, out: Path):
    subprocess.run([
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", str(source),
        "-t", f"{(end - start):.3f}",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-loglevel", "warning", "-stats",
        str(out),
    ], check=True)


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
    """Upload a file to Drive folder. Returns file ID.
    Tags are stored as Drive file description and as a custom property
    so they're searchable/filterable without affecting the filename.
    """
    from googleapiclient.http import MediaFileUpload
    media = MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=True)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    description = "Tags: " + ", ".join(tag_list) if tag_list else ""

    file_meta = {
        "name": file_path.name,
        "parents": [folder_id],
        "description": description,
        # Drive custom properties — filterable via API and visible in file details
        "properties": {"ffa_tags": ",".join(tag_list)} if tag_list else {},
    }
    uploaded = drive_svc.files().create(
        body=file_meta, media_body=media, fields="id", supportsAllDrives=True
    ).execute()
    file_id = uploaded["id"]
    # Make readable by anyone with the link
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

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("job", choices=["sync-videos", "process-clips"],
                    help="sync-videos: create tabs for newly-public videos. "
                         "process-clips: cut pending clips and upload to Drive.")
    ap.add_argument("--lookback-days", type=int, default=14,
                    help="How many days back to scan for new public videos (default: 14)")
    args = ap.parse_args()

    if args.job == "sync-videos":
        sync_videos(lookback_days=args.lookback_days)
    elif args.job == "process-clips":
        process_clips()
