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
ARCHIVE_RETENTION_DAYS = 5  # how long archived source clips are kept before permanent deletion

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
    con.execute("""
        CREATE TABLE IF NOT EXISTS xbotgo_archive (
            file_id      TEXT PRIMARY KEY,
            filename     TEXT,
            group_prefix TEXT,
            archived_at  TEXT
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


def delete_drive_file(drive, file_id, name):
    try:
        drive.files().delete(fileId=file_id, supportsAllDrives=True).execute()
        log.info(f"  Permanently deleted from Archive: {name}")
    except Exception as e:
        log.warning(f"  Could not delete {name} from Archive: {e}")


def cleanup_old_archives(con, drive):
    """Permanently delete clips that have sat in XbotGo/Archive/ longer than
    ARCHIVE_RETENTION_DAYS."""
    now = datetime.now(timezone.utc)
    rows = con.execute("SELECT file_id, filename, archived_at FROM xbotgo_archive").fetchall()
    if not rows:
        return
    log.info(f"Checking {len(rows)} archived clip(s) for cleanup...")
    for file_id, filename, archived_at_str in rows:
        archived_at = datetime.fromisoformat(archived_at_str)
        age_days = (now - archived_at).total_seconds() / 86400
        if age_days >= ARCHIVE_RETENTION_DAYS:
            delete_drive_file(drive, file_id, filename)
            con.execute("DELETE FROM xbotgo_archive WHERE file_id=?", (file_id,))
            con.commit()
        else:
            log.info(f"  Keeping {filename} — {age_days:.1f}/{ARCHIVE_RETENTION_DAYS} days")


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


SESSION_GAP_SECONDS = 1800  # 30 min gap between clips starts a new session


def parse_clip_datetime(filename):
    """
    Parse the full YYYY-MM-DD-HH-MM-SS timestamp from an XbotGo filename.
    e.g. 2026-06-19-18-13-01_1781953815548.mp4 -> datetime(2026,6,19,18,13,1)
    Handles Drive's 'Copy of ' prefix.
    """
    name = filename
    if name.lower().startswith("copy of "):
        name = name[len("copy of "):]
    name = name[:-4] if name.lower().endswith(".mp4") else name
    parts = name.split("-")
    if len(parts) < 6:
        return None
    try:
        year, month, day, hour, minute = parts[0], parts[1], parts[2], parts[3], parts[4]
        second = parts[5].split("_")[0]
        return datetime.strptime(
            f"{year}-{month}-{day} {hour}:{minute}:{second}", "%Y-%m-%d %H:%M:%S"
        ).replace(tzinfo=timezone.utc)
    except Exception:
        return None


def session_key(dt):
    """Stable, second-precision session identifier derived from a clip's start time."""
    return dt.strftime("%Y-%m-%d-%H%M%S")


def cluster_into_sessions(clips):
    """
    Group clips by recording continuity, not clock hour. A new session starts
    whenever the gap to the previous clip's start time exceeds
    SESSION_GAP_SECONDS. This keeps a session that runs past the top of the
    hour in a single group instead of splitting it.
    Returns: dict[session_key] -> list of clips, in start-time order.
    """
    dated = []
    for clip in clips:
        dt = parse_clip_datetime(clip["name"])
        if dt is None:
            log.warning(f"Could not parse timestamp from filename: {clip['name']}")
            continue
        dated.append((dt, clip))
    dated.sort(key=lambda x: x[0])

    groups: dict[str, list] = {}
    current_key = None
    prev_dt = None
    for dt, clip in dated:
        if current_key is None or (dt - prev_dt).total_seconds() > SESSION_GAP_SECONDS:
            current_key = session_key(dt)
        groups.setdefault(current_key, []).append(clip)
        prev_dt = dt
    return groups


# ── GitHub dispatch ───────────────────────────────────────────────────────────

def dispatch_concat(group_prefix, clip_names):
    token = os.environ.get("GH_PAT", "")
    repo  = os.environ.get("REPO", "")
    payload = json.dumps({
        "ref": "main",
        "inputs": {
            "group_prefix": group_prefix,
            "force": "false",
            "clip_names": ",".join(clip_names),
        }
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

    # Group by actual session (gap-based clustering), not clock hour —
    # keeps sessions that run past the top of the hour in one group.
    groups = cluster_into_sessions(clips)
    log.info(f"Session groups found: {list(groups.keys())}")

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
            dispatch_concat(prefix, [c["name"] for c in group_clips])
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
    cleanup_old_archives(con, drive)
    con.close()


if __name__ == "__main__":
    run()
