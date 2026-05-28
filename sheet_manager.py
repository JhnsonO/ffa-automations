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
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    sa_json = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive  = build("drive",  "v3", credentials=creds, cache_discovery=False)
    return sheets, drive


def get_youtube_service():
    """Load YouTube credentials the same way gopro_uploader does —
    write the secret to a temp file and use from_authorized_user_file."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_json = os.environ.get("YOUTUBE_TOKEN", "")
    if not token_json:
        raise RuntimeError("YOUTUBE_TOKEN env var is not set")

    token_path = Path("/tmp/youtube_token.json")
    token_path.write_text(token_json)

    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


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


# ── Job 1: sync-videos ────────────────────────────────────────────────────────

def sync_videos(lookback_days: int = 14):
    """
    Checks the YouTube channel for public videos published within the last
    lookback_days days that are not yet in the sheet. Creates a tab + index
    row for each new one.
    """
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    print(f"=== sync-videos (last {lookback_days} days, since {cutoff.date()}) ===")
    sheets_svc, drive_svc = get_sheets_service()
    yt_svc = get_youtube_service()
    spreadsheet_id = get_spreadsheet_id(sheets_svc)
    ensure_index_tab(sheets_svc, spreadsheet_id)

    channel_resp = yt_svc.channels().list(part="contentDetails", mine=True).execute()
    uploads_playlist = (
        channel_resp["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    )
    playlist_resp = yt_svc.playlistItems().list(
        part="snippet",
        playlistId=uploads_playlist,
        maxResults=50,
    ).execute()

    new_count = 0
    for item in playlist_resp.get("items", []):
        snippet   = item["snippet"]
        video_id  = snippet["resourceId"]["videoId"]
        title     = snippet["title"]
        published = snippet["publishedAt"]

        # Skip anything older than the lookback window
        pub_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        if pub_dt < cutoff:
            continue

        # Check privacy status
        vid_resp = yt_svc.videos().list(part="status,snippet", id=video_id).execute()
        if not vid_resp["items"]:
            continue
        status = vid_resp["items"][0]["status"]["privacyStatus"]
        if status != "public":
            continue

        yt_url = f"https://www.youtube.com/watch?v={video_id}"
        if youtube_url_in_index(sheets_svc, spreadsheet_id, yt_url):
            continue  # already tracked

        # Try to find source GoPro filename from video description
        description = vid_resp["items"][0]["snippet"].get("description", "")
        source_match = re.search(r"FFA_FILENAME:([\w.]+)", description)
        source_filename = source_match.group(1) if source_match else ""

        tab_name = safe_tab_name(title)
        # Deduplicate tab name if it clashes
        base, suffix = tab_name, 1
        while tab_exists(sheets_svc, spreadsheet_id, tab_name):
            tab_name = f"{base}_{suffix}"
            suffix += 1

        date_str = published[:10]
        _create_video_tab(sheets_svc, spreadsheet_id, tab_name, title, yt_url, source_filename)
        _add_index_row(sheets_svc, spreadsheet_id, title, yt_url, source_filename, date_str, tab_name)
        print(f"  + Created tab '{tab_name}' for: {title}")
        new_count += 1

    print(f"sync-videos complete. {new_count} new video(s) added.")


def _create_video_tab(sheets_svc, spreadsheet_id, tab_name, title, yt_url, source_filename):
    """Add a new tab with header block and clip table header."""
    # Create the sheet
    sheets_svc.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": [{"addSheet": {"properties": {"title": tab_name}}}]},
    ).execute()

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


def _add_index_row(sheets_svc, spreadsheet_id, title, yt_url, source_filename, date_str, tab_name):
    yt_formula = f'=HYPERLINK("{yt_url}","▶ Watch")'
    tab_formula = f'=HYPERLINK("#gid=0","{tab_name}")'  # placeholder; gid resolved separately
    sheets_svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=f"{INDEX_TAB}!A:F",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [[title, yt_formula, source_filename, date_str, tab_name, "Active"]]},
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
    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"'{tab_name}'!A1:F",
    ).execute()
    rows = result.get("values", [])
    if len(rows) < 5:
        return 0  # no clip table yet

    # Read header block
    def cell(r, c):
        try: return rows[r][c]
        except IndexError: return ""

    yt_url = _extract_url(cell(1, 1))  # row 2 col B
    if not yt_url:
        print(f"  [{tab_name}] No YouTube URL found — skipping")
        return 0

    # Clip rows start at row index 5 (row 6 in sheet)
    clip_rows = rows[5:]
    pending_indices = []
    for i, row in enumerate(clip_rows):
        status = row[4] if len(row) > 4 else ""
        if status.strip().lower() not in ("done", "pending"):
            # Treat blank/empty status as Pending if start/end are filled
            start = row[0] if len(row) > 0 else ""
            end   = row[1] if len(row) > 1 else ""
            if start and end:
                pending_indices.append(i)
        elif status.strip().lower() == "pending":
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
        print(f"  Downloading source: {yt_url}")
        try:
            source = _download_source(yt_url, tmp)
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



def _parse_ts(ts: str) -> float:
    ts = ts.strip()
    if not ts:
        raise ValueError("Empty timestamp")
    if ":" not in ts:
        return float(ts)
    parts = [float(p) for p in ts.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError(f"Unrecognised format: {ts}")


def _download_source(url: str, work_dir: Path) -> Path:
    fmt = (
        "bestvideo[height>=2160][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height>=2160]+bestaudio/"
        "bestvideo[height>=1080][ext=mp4]+bestaudio[ext=m4a]/"
        "bestvideo[height>=1080]+bestaudio/"
        "bestvideo+bestaudio/best"
    )
    subprocess.run([
        "yt-dlp", "-f", fmt,
        "--merge-output-format", "mp4",
        "-o", str(work_dir / "source.%(ext)s"),
        "--no-playlist", "--no-progress", url,
    ], check=True)
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


def _ensure_drive_folder(drive_svc, folder_name: str) -> str:
    """Get or create FFA/Clips/<folder_name> in Drive. Returns folder ID."""
    # Find or create root FFA folder
    ffa_id = _find_or_create_folder(drive_svc, "FFA", parent_id=None)
    clips_id = _find_or_create_folder(drive_svc, "Clips", parent_id=ffa_id)
    video_id = _find_or_create_folder(drive_svc, folder_name, parent_id=clips_id)
    return video_id


def _find_or_create_folder(drive_svc, name: str, parent_id) -> str:
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = drive_svc.files().list(q=q, fields="files(id,name)").execute()
    if res["files"]:
        return res["files"][0]["id"]
    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        meta["parents"] = [parent_id]
    folder = drive_svc.files().create(body=meta, fields="id").execute()
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
        body=file_meta, media_body=media, fields="id"
    ).execute()
    file_id = uploaded["id"]
    # Make readable by anyone with the link
    drive_svc.permissions().create(
        fileId=file_id,
        body={"type": "anyone", "role": "reader"},
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
