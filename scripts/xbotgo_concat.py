#!/usr/bin/env python3
"""
XbotGo Concat + Upload
- Downloads all clips for a group_prefix from Google Drive XbotGo/Inbox/
- Concatenates with FFmpeg (stream copy, no re-encode)
- Uploads concatenated file to Google Drive XbotGo/Done/
- Uploads to YouTube
- Logs to xbotgo.db
- Cleans up source clips from Drive Inbox
"""

import logging
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_DIR   = Path(__file__).parent.parent
DB_PATH    = BASE_DIR / "xbotgo.db"
CREDS_PATH = BASE_DIR / "youtube_credentials.json"
TOKEN_PATH = BASE_DIR / "youtube_token.json"
WORK_DIR   = BASE_DIR / "xbotgo_work"
WORK_DIR.mkdir(exist_ok=True)

YT_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/drive",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_credentials():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), YT_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            log.info("Refreshing credentials...")
            creds.refresh(Request())
            TOKEN_PATH.write_text(creds.to_json())
        else:
            log.error("Credentials invalid and cannot be refreshed non-interactively")
            sys.exit(1)
    return creds


def get_drive_service(creds):
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_youtube_service(creds):
    return build("youtube", "v3", credentials=creds)


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


def mark_uploaded(con, group_prefix, filename, youtube_id, file_count):
    con.execute(
        "INSERT OR REPLACE INTO xbotgo_uploads (group_prefix, filename, youtube_id, uploaded_at, file_count) VALUES (?,?,?,?,?)",
        (group_prefix, filename, youtube_id, datetime.now(timezone.utc).isoformat(), file_count)
    )
    con.commit()


# ── Drive helpers ─────────────────────────────────────────────────────────────

def find_or_create_folder(drive, name, parent_id):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false and '{parent_id}' in parents"
    res = drive.files().list(q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
    if res["files"]:
        return res["files"][0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return drive.files().create(body=meta, fields="id", supportsAllDrives=True).execute()["id"]


def get_xbotgo_root(drive):
    q = "name='XbotGo' and mimeType='application/vnd.google-apps.folder' and trashed=false and 'root' in parents"
    res = drive.files().list(q=q, fields="files(id)").execute()
    if res["files"]:
        return res["files"][0]["id"]
    meta = {"name": "XbotGo", "mimeType": "application/vnd.google-apps.folder"}
    return drive.files().create(body=meta, fields="id").execute()["id"]


def list_inbox_clips(drive, inbox_id, group_prefix):
    """Return MP4s in Inbox matching the group_prefix, sorted by name."""
    q = f"'{inbox_id}' in parents and mimeType='video/mp4' and trashed=false and name contains '{group_prefix}'"
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


def download_clip(drive, file_id, dest_path):
    """Download a Drive file to disk."""
    import io
    from googleapiclient.http import MediaIoBaseDownload

    request = drive.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                log.info(f"  {dest_path.name}: {int(status.progress() * 100)}%")
    log.info(f"  Downloaded: {dest_path.name} ({dest_path.stat().st_size / 1e6:.1f} MB)")


def upload_to_drive(drive, local_path, parent_id, filename):
    """Upload a file to a Drive folder."""
    log.info(f"Uploading {filename} to Drive...")
    media = MediaFileUpload(str(local_path), mimetype="video/mp4", resumable=True)
    file_meta = {"name": filename, "parents": [parent_id]}
    uploaded = drive.files().create(body=file_meta, media_body=media, fields="id").execute()
    log.info(f"Drive upload complete: {filename} -> {uploaded['id']}")
    return uploaded["id"]


def delete_drive_file(drive, file_id, name):
    try:
        drive.files().delete(fileId=file_id).execute()
        log.info(f"Deleted from Drive Inbox: {name}")
    except Exception as e:
        log.warning(f"Could not delete {name} from Drive: {e}")


# ── FFmpeg concat ─────────────────────────────────────────────────────────────

def concatenate_clips(clip_paths, output_path):
    """Concat MP4s using FFmpeg concat demuxer — no re-encode, stream copy."""
    concat_list = WORK_DIR / "concat_list.txt"
    with open(concat_list, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")

    log.info(f"Concatenating {len(clip_paths)} clips -> {output_path.name}")
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(output_path)
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log.error(f"FFmpeg concat failed:\n{result.stderr}")
        raise RuntimeError(f"FFmpeg concat failed with exit code {result.returncode}")

    size_mb = output_path.stat().st_size / 1e6
    log.info(f"Concat complete: {output_path.name} ({size_mb:.1f} MB)")
    return output_path


# ── YouTube ───────────────────────────────────────────────────────────────────

def make_title(group_prefix):
    """
    group_prefix e.g. 2026-06-19-18
    -> 'Friday Session | 19th June 2026 | FFA Leicester | XbotGo'
    """
    try:
        dt = datetime.strptime(group_prefix, "%Y-%m-%d-%H")
        day_name = dt.strftime("%A")
        day = dt.day
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        date_str = f"{day}{suffix} {dt.strftime('%B %Y')}"
        return f"{day_name} Session | {date_str} | FFA Leicester | XbotGo"
    except Exception:
        return f"Session {group_prefix} | FFA Leicester | XbotGo"


def make_description(group_prefix):
    try:
        dt = datetime.strptime(group_prefix, "%Y-%m-%d-%H")
        day_name = dt.strftime("%A")
        day = dt.day
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        date_str = f"{day}{suffix} {dt.strftime('%B %Y')}"
        is_monday = dt.weekday() == 0
    except Exception:
        day_name, date_str, is_monday = "Session", group_prefix, False

    monday_section = (
        "\n\nThis is part of the FFA Monday League — our weekly competitive kickabout with individual player standings.\n"
        "Check the league table: https://www.officialffa.co.uk/mnl\n"
    ) if is_monday else ""

    return (
        f"FFA Leicester | {day_name} Session | {date_str}\n"
        f"Filmed with XbotGo Chameleon ball-tracking camera.\n"
        f"\nCompetitive kickabouts in Leicester, running weekly. All levels welcome.\n"
        f"{monday_section}"
        f"\nWant to play? Book your spot:\nhttps://www.officialffa.co.uk\n\n"
        f"Shop FFA merch: https://www.officialffa.co.uk/store\n\n"
        f"Follow us:\nInstagram: https://www.instagram.com/_official_ffa\nTikTok: https://www.tiktok.com/@official_ffa\n\n"
        f"#FFA #FFALeicester #FootballForAll #Leicester #GrassrootsFootball #5aside #XbotGo"
    )


def upload_to_youtube(yt, video_path, title, description, max_retries=3):
    log.info(f"Uploading to YouTube: {title}")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["FFA", "Football For All", "Leicester", "grassroots football", "XbotGo"],
            "categoryId": "17",
        },
        "status": {"privacyStatus": "unlisted", "selfDeclaredMadeForKids": False},
    }
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            wait = 30 * attempt
            log.info(f"YouTube retry {attempt}/{max_retries} in {wait}s...")
            time.sleep(wait)
        try:
            media = MediaFileUpload(str(video_path), chunksize=50 * 1024 * 1024, resumable=True)
            request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            start = time.time()
            while response is None:
                status, response = request.next_chunk()
                if status:
                    pct = int(status.progress() * 100)
                    elapsed = time.time() - start
                    log.info(f"  YouTube upload: {pct}% ({elapsed:.0f}s)")
            vid_id = response.get("id")
            log.info(f"YouTube upload complete: https://youtu.be/{vid_id}")
            return vid_id
        except Exception as e:
            log.error(f"YouTube upload error (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                return None
    return None


# ── Alert ─────────────────────────────────────────────────────────────────────

def send_alert(subject, body):
    """Send email alert via SendGrid (same pattern as existing pipeline)."""
    try:
        import urllib.request as ureq
        api_key = os.environ.get("ALERT_EMAIL_KEY", "")
        from_email = os.environ.get("ALERT_EMAIL_FROM", "")
        to_email = os.environ.get("ALERT_EMAIL_TO", "")
        if not all([api_key, from_email, to_email]):
            log.warning("Alert email secrets not set — skipping alert")
            return
        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": from_email},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        }
        import json
        req = ureq.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=json.dumps(payload).encode(),
            method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        ureq.urlopen(req, timeout=15)
        log.info("Alert email sent")
    except Exception as e:
        log.warning(f"Alert email failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    group_prefix = os.environ.get("GROUP_PREFIX", "").strip()
    force        = os.environ.get("FORCE", "false").strip().lower() == "true"

    if not group_prefix:
        log.error("GROUP_PREFIX not set")
        sys.exit(1)

    log.info(f"XbotGo concat starting: group_prefix={group_prefix} force={force}")

    con   = init_db()
    creds = get_credentials()
    drive = get_drive_service(creds)
    yt    = get_youtube_service(creds)

    # Check not already done
    row = con.execute("SELECT youtube_id FROM xbotgo_uploads WHERE group_prefix=?", (group_prefix,)).fetchone()
    if row:
        log.info(f"Already uploaded: {group_prefix} -> https://youtu.be/{row[0]}")
        return

    # Locate folders
    root_id  = get_xbotgo_root(drive)
    inbox_id = find_or_create_folder(drive, "Inbox", root_id)
    done_id  = find_or_create_folder(drive, "Done", root_id)

    # Get clips
    clips = list_inbox_clips(drive, inbox_id, group_prefix)
    if not clips:
        log.error(f"No clips found in Inbox for prefix {group_prefix}")
        send_alert(
            f"[FFA XbotGo] No clips found for {group_prefix}",
            f"xbotgo-concat.yml ran for {group_prefix} but found no matching clips in Drive Inbox."
        )
        sys.exit(1)

    log.info(f"Found {len(clips)} clip(s) for {group_prefix}:")
    for c in clips:
        log.info(f"  {c['name']}")

    # Download all clips
    local_clips = []
    for clip in clips:
        dest = WORK_DIR / clip["name"]
        log.info(f"Downloading {clip['name']}...")
        download_clip(drive, clip["id"], dest)
        local_clips.append(dest)

    # Concatenate
    output_name = f"{group_prefix}_concat.mp4"
    output_path = WORK_DIR / output_name
    try:
        concatenate_clips(local_clips, output_path)
    except Exception as e:
        log.error(f"Concat failed: {e}")
        send_alert(
            f"[FFA XbotGo] Concat failed for {group_prefix}",
            f"FFmpeg concat failed for {group_prefix}.\n\nError: {e}"
        )
        sys.exit(1)

    # Upload to Drive Done/
    try:
        upload_to_drive(drive, output_path, done_id, output_name)
    except Exception as e:
        log.warning(f"Drive upload to Done/ failed: {e} — continuing to YouTube anyway")

    # Upload to YouTube
    title       = make_title(group_prefix)
    description = make_description(group_prefix)
    yt_id = upload_to_youtube(yt, output_path, title, description)

    if not yt_id:
        log.error("YouTube upload failed")
        send_alert(
            f"[FFA XbotGo] YouTube upload failed for {group_prefix}",
            f"Concat succeeded but YouTube upload failed for {group_prefix}.\n"
            f"Concatenated file is saved in Drive XbotGo/Done/{output_name}"
        )
        sys.exit(1)

    # Log to DB
    mark_uploaded(con, group_prefix, output_name, yt_id, len(clips))
    log.info(f"Logged to xbotgo.db: {group_prefix}")

    # Clean up source clips from Drive Inbox
    log.info("Cleaning up source clips from Drive Inbox...")
    for clip in clips:
        delete_drive_file(drive, clip["id"], clip["name"])

    # Clean up local work files
    for p in local_clips:
        p.unlink(missing_ok=True)
    output_path.unlink(missing_ok=True)

    log.info(f"All done: {group_prefix} -> https://youtu.be/{yt_id}")
    con.close()


if __name__ == "__main__":
    run()
