#!/usr/bin/env python3
"""
MAX2 Chapter Concat + Upload
- Verifies ALL expected chapters are present in Drive FFA360/Inbox/ before proceeding
  (real check against TOTAL_CHAPTERS — unlike XbotGo's unused 'force' flag, this one
  actually blocks a partial concat)
- Downloads all chapter clips for a session_prefix from Google Drive FFA360/Inbox/
- Concatenates with FFmpeg (stream copy, no re-encode)
- Re-injects 360 XMP spherical metadata on the concatenated output (concat's new
  container does NOT inherit each chapter's per-file XMP tags — this step is required
  for YouTube to treat the result as a 360 video)
- Uploads to YouTube using the same title/description conventions as the single-host
  MAX2 pipeline (vastai_stitch_max2.sh)
- Logs to the REAL production uploaded.db (same schema single-host runs use)
- Cleans up chapter clips from Drive Inbox on success
"""

import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import base64
import json
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

BASE_DIR   = Path(__file__).parent.parent
CREDS_PATH = BASE_DIR / "youtube_credentials.json"
TOKEN_PATH = BASE_DIR / "youtube_token.json"
WORK_DIR   = BASE_DIR / "max2_chapter_work"
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


# ── uploaded.db (real production db, via GitHub API — matches vastai_stitch_max2.sh) ──

def gh_get(repo, token, path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/{path}",
        headers={"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def already_uploaded(repo, token, media_id):
    try:
        data = gh_get(repo, token, "contents/uploaded.db")
    except Exception as e:
        log.warning(f"Could not fetch uploaded.db to check dedup: {e}")
        return None
    db_content = base64.b64decode(data["content"])
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(db_content)
        tmp_path = f.name
    try:
        con = sqlite3.connect(tmp_path)
        row = con.execute(
            "SELECT youtube_id FROM uploads WHERE media_id=?", (media_id,)
        ).fetchone()
        con.close()
        return row[0] if row else None
    except Exception:
        return None
    finally:
        os.unlink(tmp_path)


def mark_uploaded(repo, token, media_id, filename, captured_at, youtube_id):
    data = gh_get(repo, token, "contents/uploaded.db")
    db_content = base64.b64decode(data["content"])
    sha = data["sha"]

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        f.write(db_content)
        tmp_path = f.name

    con = sqlite3.connect(tmp_path)
    con.execute("""CREATE TABLE IF NOT EXISTS uploads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_id TEXT UNIQUE,
        filename TEXT,
        captured_at TEXT,
        youtube_id TEXT,
        uploaded_at TEXT
    )""")
    con.execute(
        "INSERT OR REPLACE INTO uploads (media_id, filename, captured_at, youtube_id, uploaded_at) VALUES (?,?,?,?,?)",
        (media_id, filename, captured_at, youtube_id, datetime.now(timezone.utc).isoformat())
    )
    con.commit()
    con.close()

    encoded = base64.b64encode(Path(tmp_path).read_bytes()).decode()
    os.unlink(tmp_path)

    payload = json.dumps({
        "message": f"chore: mark {filename} uploaded (360, parallel-chapter) [skip ci]",
        "content": encoded,
        "sha": sha,
        "branch": "main"
    }).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/contents/uploaded.db",
        data=payload, method="PUT",
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req) as r:
        result = json.loads(r.read())
    log.info(f"uploaded.db committed: {result['commit']['sha']}")


# ── Drive ─────────────────────────────────────────────────────────────────────

def find_or_create_folder(drive, name, parent_id=None):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = drive.files().list(q=q, fields="files(id,name)", pageSize=10).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    folder = drive.files().create(body=meta, fields="id").execute()
    return folder["id"]


def list_all_inbox_clips(drive, inbox_id):
    q = f"'{inbox_id}' in parents and trashed=false"
    results = []
    page_token = None
    while True:
        params = dict(q=q, fields="nextPageToken,files(id,name,mimeType)", pageSize=100,
                      supportsAllDrives=True, includeItemsFromAllDrives=True)
        if page_token:
            params["pageToken"] = page_token
        res = drive.files().list(**params).execute()
        results.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token:
            break
    mp4s = [f for f in results if f["name"].lower().endswith(".mp4")]
    return sorted(mp4s, key=lambda f: f["name"])


def filter_clips_by_session(all_clips, session_prefix):
    """Match files named exactly {session_prefix}_chNN.mp4 — stricter than substring
    matching so e.g. session '0419' never accidentally matches '0419x'."""
    pattern = re.compile(rf'^{re.escape(session_prefix)}_ch\d+\.mp4$', re.IGNORECASE)
    matched = [c for c in all_clips if pattern.match(c["name"])]
    return sorted(matched, key=lambda f: f["name"])  # zero-padded chNN sorts correctly


def download_clip(drive, file_id, dest_path):
    from googleapiclient.http import MediaIoBaseDownload

    request = drive.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                log.info(f"  {dest_path.name}: {int(status.progress() * 100)}%")
    log.info(f"  Downloaded: {dest_path.name} ({dest_path.stat().st_size / 1e9:.2f} GB)")


def delete_drive_file(drive, file_id, name):
    try:
        drive.files().delete(fileId=file_id).execute()
        log.info(f"Deleted from Drive Inbox: {name}")
    except Exception as e:
        log.warning(f"Could not delete {name} from Drive: {e}")


# ── FFmpeg concat + metadata ────────────────────────────────────────────────────

def concatenate_clips(clip_paths, output_path):
    concat_list = WORK_DIR / "concat_list.txt"
    with open(concat_list, "w") as f:
        for p in clip_paths:
            f.write(f"file '{p}'\n")

    log.info(f"Concatenating {len(clip_paths)} chapter(s) -> {output_path.name}")
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
        log.error(f"FFmpeg concat failed:\n{result.stderr[-2000:]}")
        raise RuntimeError(f"FFmpeg concat failed with exit code {result.returncode}")

    size_gb = output_path.stat().st_size / 1e9
    log.info(f"Concat complete: {output_path.name} ({size_gb:.2f} GB)")
    return output_path


def inject_360_metadata(video_path):
    """concat's new container does not inherit per-chapter XMP tags — must
    re-inject on the final file, exactly as the single-host pipeline does."""
    log.info("Re-injecting 360 XMP spherical metadata on concatenated output...")
    cmd = [
        "exiftool",
        "-api", "LargeFileSupport=1",
        "-overwrite_original",
        "-XMP-GSpherical:Spherical=true",
        "-XMP-GSpherical:Stitched=true",
        "-XMP-GSpherical:StitchingSoftware=FFmpeg",
        "-XMP-GSpherical:ProjectionType=equirectangular",
        str(video_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"exiftool metadata injection failed: {result.stderr[-1000:]}")
    log.info("Metadata injected")


# ── Title / description (matches vastai_stitch_max2.sh conventions) ───────────

def make_title_and_description(captured_at, media_id, filename):
    try:
        dt = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        day_name = dt.strftime("%A")
        day = dt.day
        suffix = "th" if 11 <= day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
        date_str = dt.strftime(f"%-d{suffix} %B %Y")
    except Exception:
        day_name = "Session"
        date_str = (captured_at or "")[:10]

    title = f"{day_name} Session | {date_str} | FFA Leicester | 360°"
    description = (
        f"FFA Leicester — {day_name} session footage captured {date_str}.\n"
        f"360° video — use a VR headset or drag to look around.\n\n"
        f"FFA_MEDIA_ID:{media_id}\n"
        f"FFA_FILENAME:{filename}\n"
        f"FFA_CAPTURED_AT:{captured_at}\n"
        f"FFA_360:true"
    )
    return title, description


# ── YouTube ───────────────────────────────────────────────────────────────────

def upload_to_youtube(yt, video_path, title, description, max_retries=3):
    log.info(f"Uploading to YouTube: {title}")
    body = {
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["FFA", "Football For All", "Leicester", "360", "football"],
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
            media = MediaFileUpload(str(video_path), mimetype="video/mp4",
                                     chunksize=50 * 1024 * 1024, resumable=True)
            request = yt.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            start = time.time()
            while response is None:
                status, response = request.next_chunk()
                if status:
                    log.info(f"  YouTube upload: {int(status.progress() * 100)}% ({time.time()-start:.0f}s)")
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
    try:
        api_key    = os.environ.get("ALERT_EMAIL_KEY", "")
        from_email = os.environ.get("ALERT_EMAIL_FROM", "")
        to_email   = os.environ.get("ALERT_EMAIL_TO", "")
        if not all([api_key, from_email, to_email]):
            log.warning("Alert email secrets not set — skipping alert")
            return
        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": from_email},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}],
        }
        req = urllib.request.Request(
            "https://api.sendgrid.com/v3/mail/send",
            data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=15)
        log.info("Alert email sent")
    except Exception as e:
        log.warning(f"Alert email failed: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    session_prefix = os.environ.get("SESSION_PREFIX", "").strip()
    total_chapters = os.environ.get("TOTAL_CHAPTERS", "").strip()
    media_id       = os.environ.get("MEDIA_ID", "").strip()
    filename       = os.environ.get("FILENAME", "").strip()
    captured_at    = os.environ.get("CAPTURED_AT", "").strip()
    gh_pat         = os.environ.get("GH_PAT", "").strip()
    repo           = os.environ.get("REPO", "").strip()
    force          = os.environ.get("FORCE", "false").strip().lower() == "true"
    force_reupload = os.environ.get("FORCE_REUPLOAD", "false").strip().lower() == "true"

    if not all([session_prefix, total_chapters, media_id, gh_pat, repo]):
        log.error("SESSION_PREFIX, TOTAL_CHAPTERS, MEDIA_ID, GH_PAT, REPO are all required")
        sys.exit(1)

    try:
        total_chapters_n = int(total_chapters)
    except ValueError:
        log.error(f"TOTAL_CHAPTERS must be an integer, got: {total_chapters}")
        sys.exit(1)

    log.info(f"MAX2 chapter concat starting: session={session_prefix} "
             f"expected_chapters={total_chapters_n} media_id={media_id} force={force}")

    # Dedup check against the REAL production uploaded.db
    existing_yt_id = already_uploaded(repo, gh_pat, media_id)
    if existing_yt_id and not force_reupload:
        log.info(f"Already uploaded: media_id={media_id} -> https://youtu.be/{existing_yt_id}")
        log.info("Set force_reupload=true to override and upload anyway (will create a DUPLICATE video on YouTube).")
        return
    if existing_yt_id and force_reupload:
        log.warning(f"OVERRIDE: media_id={media_id} already uploaded as https://youtu.be/{existing_yt_id} "
                    f"but force_reupload=true — proceeding anyway. This WILL create a duplicate video.")

    creds = get_credentials()
    drive = get_drive_service(creds)
    yt    = get_youtube_service(creds)

    root_id  = find_or_create_folder(drive, "FFA360")
    inbox_id = find_or_create_folder(drive, "Inbox", root_id)

    all_clips = list_all_inbox_clips(drive, inbox_id)
    clips = filter_clips_by_session(all_clips, session_prefix)

    log.info(f"Found {len(clips)}/{total_chapters_n} expected chapter(s) in Inbox for session {session_prefix}:")
    for c in clips:
        log.info(f"  {c['name']}")

    # ── Real completion check — this is the teeth XbotGo's 'force' flag never had ──
    if len(clips) < total_chapters_n and not force:
        msg = (f"Only {len(clips)}/{total_chapters_n} chapters present for session "
               f"{session_prefix} — refusing to concat a partial session. "
               f"Re-run with force=true only if you're intentionally accepting a partial result.")
        log.error(msg)
        send_alert(f"[FFA MAX2] Incomplete session {session_prefix}", msg)
        sys.exit(1)

    if len(clips) > total_chapters_n:
        log.warning(f"Found MORE clips ({len(clips)}) than expected ({total_chapters_n}) — "
                    f"proceeding with all matched clips, but this is unexpected, check for duplicates.")

    if not clips:
        log.error(f"No clips found in Inbox matching session '{session_prefix}'")
        sys.exit(1)

    # Download all chapters
    local_clips = []
    for clip in clips:
        dest = WORK_DIR / clip["name"]
        log.info(f"Downloading {clip['name']}...")
        download_clip(drive, clip["id"], dest)
        local_clips.append(dest)

    # Concatenate
    output_name = f"{session_prefix}_concat.mp4"
    output_path = WORK_DIR / output_name
    try:
        concatenate_clips(local_clips, output_path)
        inject_360_metadata(output_path)
    except Exception as e:
        log.error(f"Concat/metadata step failed: {e}")
        send_alert(f"[FFA MAX2] Concat failed for session {session_prefix}", f"Error: {e}")
        sys.exit(1)

    # Upload to YouTube
    title, description = make_title_and_description(captured_at, media_id, filename)
    yt_id = upload_to_youtube(yt, output_path, title, description)

    if not yt_id:
        log.error("YouTube upload failed")
        send_alert(
            f"[FFA MAX2] YouTube upload failed for session {session_prefix}",
            f"Concat succeeded but YouTube upload failed. Local file was {output_name}."
        )
        sys.exit(1)

    # Log to the REAL production uploaded.db
    mark_uploaded(repo, gh_pat, media_id, filename, captured_at, yt_id)

    # Clean up chapter clips from Drive Inbox
    log.info("Cleaning up chapter clips from Drive Inbox...")
    for clip in clips:
        delete_drive_file(drive, clip["id"], clip["name"])

    # Clean up local work files
    for p in local_clips:
        p.unlink(missing_ok=True)
    output_path.unlink(missing_ok=True)

    log.info(f"All done: session {session_prefix} -> https://youtu.be/{yt_id}")


if __name__ == "__main__":
    run()
