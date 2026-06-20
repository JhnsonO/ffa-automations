#!/usr/bin/env python3
"""
XbotGo Drive Scanner — groups clips by session prefix, checks stability,
dispatches xbotgo-concat.yml when a group is complete.
"""

import json
import logging
import os
import sqlite3
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

BASE_DIR   = Path(__file__).parent.parent
DB_PATH    = BASE_DIR / "xbotgo.db"
CREDS_PATH = BASE_DIR / "youtube_credentials.json"
TOKEN_PATH = BASE_DIR / "youtube_token.json"

YT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/drive",
]

STABILITY_SECONDS = 3600  # 1 hour — file count must be stable this long before processing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── DB ────────────────────────────────────────────────────────────────────────

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS scan_state (
            group_prefix TEXT PRIMARY KEY,
            file_count   INTEGER,
            last_seen    TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS xbotgo_uploads (
            group_prefix TEXT PRIMARY KEY,
            filename     TEXT,
            youtube_id   TEXT,
            uploaded_at  TEXT,
            file_count   INTEGER
        )
    """)
    con.commit()
    return con


def already_processed(con, group_prefix):
    row = con.execute(
        "SELECT youtube_id FROM xbotgo_uploads WHERE group_prefix=?", (group_prefix,)
    ).fetchone()
    return row is not None


def get_scan_state(con, group_prefix):
    return con.execute(
        "SELECT file_count, last_seen FROM scan_state WHERE group_prefix=?", (group_prefix,)
    ).fetchone()


def upsert_scan_state(con, group_prefix, file_count):
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        "INSERT OR REPLACE INTO scan_state (group_prefix, file_count, last_seen) VALUES (?,?,?)",
        (group_prefix, file_count, now),
    )
    con.commit()


def delete_scan_state(con, group_prefix):
    con.execute("DELETE FROM scan_state WHERE group_prefix=?", (group_prefix,))
    con.commit()


# ── Drive ─────────────────────────────────────────────────────────────────────

def get_drive_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), YT_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
        else:
            log.error("YouTube/Drive token invalid and cannot be refreshed non-interactively")
            sys.exit(1)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def find_or_create_folder(drive, name, parent_id):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false and '{parent_id}' in parents"
    res = drive.files().list(q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    if res["files"]:
        return res["files"][0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return drive.files().create(body=meta, fields="id", supportsAllDrives=True).execute()["id"]


def get_xbotgo_root(drive):
    """Find or create XbotGo root folder in My Drive."""
    q = "name='XbotGo' and mimeType='application/vnd.google-apps.folder' and trashed=false and 'root' in parents"
    res = drive.files().list(q=q, fields="files(id)").execute()
    if res["files"]:
        return res["files"][0]["id"]
    meta = {"name": "XbotGo", "mimeType": "application/vnd.google-apps.folder"}
    return drive.files().create(body=meta, fields="id").execute()["id"]


def list_inbox_clips(drive, inbox_id):
    """Return list of MP4 files in XbotGo/Inbox/, sorted by name."""
    q = f"'{inbox_id}' in parents and mimeType='video/mp4' and trashed=false"
    results = []
    page_token = None
    while True:
        params = dict(q=q, fields="nextPageToken,files(id,name)", pageSize=100)
        if page_token:
            params["pageToken"] = page_token
        res = drive.files().list(**params).execute()
        results.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    return sorted(results, key=lambda f: f["name"])


def extract_prefix(filename):
    """
    Extract YYYY-MM-DD-HH prefix from XbotGo filename.
    e.g. 2026-06-19-18-13-01_1781953815548.mp4 -> 2026-06-19-18
    """
    try:
        parts = filename.split("-")
        if len(parts) >= 4:
            return "-".join(parts[:4])
    except Exception:
        pass
    return None


# ── GitHub dispatch ───────────────────────────────────────────────────────────

def dispatch_concat(group_prefix):
    token = os.environ.get("GH_PAT", "")
    repo  = os.environ.get("REPO", "")
    payload = json.dumps({
        "ref": "main",
        "inputs": {"group_prefix": group_prefix, "force": "false"}
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/xbotgo-concat.yml/dispatches",
        data=payload, method="POST",
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"}
    )
    urllib.request.urlopen(req, timeout=15)
    log.info(f"Dispatched xbotgo-concat.yml for group_prefix={group_prefix}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    con  = init_db()
    drive = get_drive_service()

    root_id  = get_xbotgo_root(drive)
    inbox_id = find_or_create_folder(drive, "Inbox", root_id)
    log.info(f"XbotGo/Inbox/ folder ID: {inbox_id}")

    clips = list_inbox_clips(drive, inbox_id)
    log.info(f"Found {len(clips)} MP4(s) in Inbox")

    # Group by session prefix
    groups: dict[str, list] = {}
    for clip in clips:
        prefix = extract_prefix(clip["name"])
        if prefix:
            groups.setdefault(prefix, []).append(clip)
        else:
            log.warning(f"Could not extract prefix from filename: {clip['name']}")

    log.info(f"Groups found: {list(groups.keys())}")

    now = datetime.now(timezone.utc)

    for prefix, group_clips in groups.items():
        count = len(group_clips)
        log.info(f"Group {prefix}: {count} clip(s)")

        # Skip if already processed/uploaded
        if already_processed(con, prefix):
            log.info(f"  SKIP — already uploaded")
            delete_scan_state(con, prefix)
            continue

        prev = get_scan_state(con, prefix)

        if prev is None:
            # First time we've seen this group
            log.info(f"  NEW — tracking, file count={count}")
            upsert_scan_state(con, prefix, count)
            continue

        prev_count, last_seen_str = prev

        if prev_count != count:
            # Count changed — still uploading, reset timer
            log.info(f"  CHANGED — {prev_count} -> {count} clips, resetting timer")
            upsert_scan_state(con, prefix, count)
            continue

        # Count unchanged — check how long it's been stable
        last_seen = datetime.fromisoformat(last_seen_str)
        elapsed = (now - last_seen).total_seconds()
        remaining = max(0, STABILITY_SECONDS - elapsed)

        if elapsed >= STABILITY_SECONDS:
            log.info(f"  READY — stable for {elapsed/60:.0f} min, dispatching concat")
            dispatch_concat(prefix)
            delete_scan_state(con, prefix)
        else:
            log.info(f"  STABLE — waiting {remaining/60:.0f} more min before dispatch")

    # Clean up scan_state entries for prefixes no longer in Inbox (all clips removed)
    all_prefixes_in_inbox = set(groups.keys())
    tracked = con.execute("SELECT group_prefix FROM scan_state").fetchall()
    for (tracked_prefix,) in tracked:
        if tracked_prefix not in all_prefixes_in_inbox:
            log.info(f"Removing stale scan_state entry: {tracked_prefix}")
            delete_scan_state(con, tracked_prefix)

    log.info("Scanner run complete")
    con.close()


if __name__ == "__main__":
    run()
