from pathlib import Path
import re

p = Path('gopro_uploader.py')
s = p.read_text()

# Config defaults
s = s.replace(
    'GOPRO_FALLBACK_SCAN_PAGES = int(os.environ.get("GOPRO_FALLBACK_SCAN_PAGES") or 10)\n',
    'GOPRO_FALLBACK_SCAN_PAGES = int(os.environ.get("GOPRO_FALLBACK_SCAN_PAGES") or 10)\n'
    'PROGRESS_LOG_INTERVAL_SECONDS = int(os.environ.get("PROGRESS_LOG_INTERVAL_SECONDS") or 20)\n'
    'PROGRESS_PERCENT_STEP = float(os.environ.get("PROGRESS_PERCENT_STEP") or 5)\n'
    'YOUTUBE_UPLOAD_CHUNK_MB = int(os.environ.get("YOUTUBE_UPLOAD_CHUNK_MB") or 16)\n'
) if 'PROGRESS_LOG_INTERVAL_SECONDS' not in s else s

# Helper functions after effective_media_date_string
anchor = '''def effective_media_date_string(item):
    dt, _ = effective_media_datetime(item)
    return datetime_to_gopro_z(dt)
'''
helpers = anchor + '''

def format_duration(seconds):
    if seconds is None or seconds < 0:
        return "calculating..."
    seconds = int(seconds)
    hours, rem = divmod(seconds, 3600)
    mins, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {mins}m {secs}s"
    if mins:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def format_gb(num_bytes):
    return f"{num_bytes / 1e9:.2f} GB"


def should_log_progress(now, pct, last_log_time, last_log_pct):
    if last_log_time is None:
        return True
    if pct is not None and pct >= 100:
        return True
    if now - last_log_time >= PROGRESS_LOG_INTERVAL_SECONDS:
        return True
    if pct is not None and pct - last_log_pct >= PROGRESS_PERCENT_STEP:
        return True
    return False


def log_transfer_progress(label, filename, transferred, total, start_time, pct=None):
    elapsed = max(time.time() - start_time, 0.001)
    speed = transferred / elapsed
    eta = None
    if total and speed > 0:
        eta = (total - transferred) / speed
    if pct is None and total:
        pct = transferred / total * 100
    pct_text = f"{pct:.1f}%" if pct is not None else "progress unknown"
    total_text = format_gb(total) if total else "unknown size"
    log.info(
        "%s %s: %s | %s/%s | %.1f MB/s | ETA %s",
        label,
        filename,
        pct_text,
        format_gb(transferred),
        total_text,
        speed / 1e6,
        format_duration(eta),
    )
'''
if 'def log_transfer_progress(' not in s:
    s = s.replace(anchor, helpers)

# Add camera label storage column in init_db
if 'camera_label TEXT' not in s:
    s = s.replace(
        '''    con.execute("""
        CREATE TABLE IF NOT EXISTS failed_uploads (
            media_id TEXT PRIMARY KEY,
            failed_at TEXT,
            reason TEXT
        )
    """)
    con.commit()
    return con
''',
        '''    con.execute("""
        CREATE TABLE IF NOT EXISTS failed_uploads (
            media_id TEXT PRIMARY KEY,
            failed_at TEXT,
            reason TEXT
        )
    """)
    try:
        con.execute("ALTER TABLE uploads ADD COLUMN camera_label TEXT")
    except sqlite3.OperationalError:
        pass
    con.commit()
    return con
'''
    )

# Replace mark_uploaded signature/body
s = re.sub(
    r'''def mark_uploaded\(con, media_id, filename, captured_at, youtube_id\):\n    con\.execute\("DELETE FROM failed_uploads WHERE media_id=\?", \(media_id,\)\)\n    con\.execute\(\n        "INSERT OR REPLACE INTO uploads \(media_id, filename, captured_at, youtube_id, uploaded_at\) VALUES \(\?,\?,\?,\?,\?\)",\n        \(media_id, filename, captured_at, youtube_id, datetime\.now\(timezone\.utc\)\.isoformat\(\)\),\n    \)\n    con\.commit\(\)\n''',
    '''def mark_uploaded(con, media_id, filename, captured_at, youtube_id, camera_label=""):
    con.execute("DELETE FROM failed_uploads WHERE media_id=?", (media_id,))
    con.execute(
        "INSERT OR REPLACE INTO uploads (media_id, filename, captured_at, youtube_id, uploaded_at, camera_label) VALUES (?,?,?,?,?,?)",
        (media_id, filename, captured_at, youtube_id, datetime.now(timezone.utc).isoformat(), camera_label),
    )
    con.commit()
''',
    s
)

# Add DB-backed camera label helpers before get_concat_download_url
label_helpers = '''

def date_key_from_gopro_date(value):
    return (value or "")[:10]


def camera_label_from_index(index):
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    if index < len(letters):
        return letters[index]
    return str(index + 1)


def used_camera_labels_for_date(con, date_key):
    if not date_key:
        return set()
    rows = con.execute(
        "SELECT camera_label, captured_at FROM uploads WHERE substr(captured_at, 1, 10)=?",
        (date_key,),
    ).fetchall()
    used = set()
    legacy_count = 0
    for label, captured_at in rows:
        if label:
            used.add(label)
        elif captured_at:
            legacy_count += 1
    for i in range(legacy_count):
        used.add(camera_label_from_index(i))
    return used


def next_camera_label_for_date(con, date_key, reserved=None):
    reserved = reserved or set()
    used = used_camera_labels_for_date(con, date_key) | reserved
    i = 0
    while True:
        label = camera_label_from_index(i)
        if label not in used:
            return label
        i += 1
'''
if 'def next_camera_label_for_date(' not in s:
    s = s.replace('\n\ndef get_concat_download_url(session, media_id):', label_helpers + '\n\ndef get_concat_download_url(session, media_id):')

# Replace download_video
new_download = '''def download_video(url, dest_path, max_retries=3):
    filename = dest_path.name
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            wait = 30 * attempt
            log.info(f"Download retry {attempt}/{max_retries} in {wait}s...")
            time.sleep(wait)
            dest_path.unlink(missing_ok=True)
        log.info(f"Downloading to {filename} (attempt {attempt})...")
        try:
            with requests.get(url, stream=True, timeout=60) as r:
                r.raise_for_status()
                total = int(r.headers.get("content-length", 0))
                downloaded = 0
                start_time = time.time()
                last_log_time = None
                last_log_pct = 0.0
                with open(dest_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        f.write(chunk)
                        downloaded += len(chunk)
                        pct = downloaded / total * 100 if total else None
                        now = time.time()
                        if should_log_progress(now, pct, last_log_time, last_log_pct):
                            log_transfer_progress("Download", filename, downloaded, total, start_time, pct)
                            last_log_time = now
                            if pct is not None:
                                last_log_pct = pct
                log_transfer_progress("Download", filename, downloaded, total, start_time, 100.0 if total else None)
            log.info(f"Download complete: {dest_path.stat().st_size / 1e9:.2f} GB")
            return True
        except Exception as e:
            log.error(f"Download error (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                dest_path.unlink(missing_ok=True)
                return False
    return False


'''
s = re.sub(r'def download_video\(url, dest_path, max_retries=3\):.*?\n\ndef get_youtube_service\(\):', new_download + 'def get_youtube_service():', s, flags=re.S)

# Replace upload_to_youtube
new_upload = '''def upload_to_youtube(service, video_path, title, description, gopro_filename="", max_retries=3):
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
    file_size = video_path.stat().st_size
    chunk_size = max(1, YOUTUBE_UPLOAD_CHUNK_MB) * 1024 * 1024
    for attempt in range(1, max_retries + 1):
        if attempt > 1:
            wait = 30 * attempt
            log.info(f"Retry {attempt}/{max_retries} in {wait}s...")
            time.sleep(wait)
        try:
            media = MediaFileUpload(str(video_path), chunksize=chunk_size, resumable=True)
            request = service.videos().insert(part="snippet,status", body=body, media_body=media)
            response = None
            upload_start = time.time()
            last_log_time = None
            last_log_pct = 0.0
            while response is None:
                status, response = request.next_chunk()
                if status:
                    pct = status.progress() * 100
                    uploaded = int(file_size * status.progress())
                    now = time.time()
                    if should_log_progress(now, pct, last_log_time, last_log_pct):
                        log_transfer_progress("Upload", gopro_filename or video_path.name, uploaded, file_size, upload_start, pct)
                        last_log_time = now
                        last_log_pct = pct
            log_transfer_progress("Upload", gopro_filename or video_path.name, file_size, file_size, upload_start, 100.0)
            vid_id = response.get("id")
            log.info(f"YouTube upload complete: https://youtu.be/{vid_id}")
            return vid_id
        except Exception as e:
            log.error(f"YouTube upload error (attempt {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                return None
    return None


'''
s = re.sub(r'def upload_to_youtube\(service, video_path, title, description, gopro_filename="", max_retries=3\):.*?\n\ndef make_title\(', new_upload + 'def make_title(', s, flags=re.S)

# Update upload_item signature and mark_uploaded calls/descriptions
s = s.replace('def upload_item(con, yt, session, item, camera_label="", force=False):', 'def upload_item(con, yt, session, item, camera_label="", force=False):')
s = s.replace('description += f"\\n\\nFFA_MEDIA_ID:{media_id}\\nFFA_FILENAME:{filename}\\nFFA_CAPTURED_AT:{original_captured_at}\\nFFA_CREATED_AT:{created_at}\\nFFA_EFFECTIVE_AT:{upload_date}"', 'description += f"\\n\\nFFA_MEDIA_ID:{media_id}\\nFFA_FILENAME:{filename}"')
s = s.replace('description += f"\\n\\nFFA_MEDIA_ID:{media_id}\\nFFA_FILENAME:{filename}\\nFFA_DIRECT_UPLOAD_URL:{DIRECT_UPLOAD_URL}"', 'description += f"\\n\\nFFA_MEDIA_ID:{media_id}\\nFFA_FILENAME:{filename}"')
s = s.replace('mark_uploaded(con, media_id, filename, upload_date, "existing")', 'mark_uploaded(con, media_id, filename, upload_date, "existing", camera_label)')
s = s.replace('mark_uploaded(con, media_id, filename, upload_date, yt_id)', 'mark_uploaded(con, media_id, filename, upload_date, yt_id, camera_label)')
s = s.replace('mark_uploaded(con, media_id, filename, captured_at, "existing")', 'mark_uploaded(con, media_id, filename, captured_at, "existing", camera_label)')
s = s.replace('mark_uploaded(con, media_id, filename, captured_at, yt_id)', 'mark_uploaded(con, media_id, filename, captured_at, yt_id, camera_label)')

# Direct upload: normal camera label rather than Manual
s = s.replace('description = make_description(filename, captured_at, "Manual")', 'camera_label = next_camera_label_for_date(con, captured_at[:10])\n    description = make_description(filename, captured_at, camera_label)')
s = s.replace('make_title(filename, captured_at, "Manual")', 'make_title(filename, captured_at, camera_label)')

# Manual GoPro upload: assign next normal camera label
s = s.replace('upload_item(con, yt, session, item, camera_label="Manual", force=True)', 'date_key = effective_media_date_string(item)[:10]\n    camera_label = next_camera_label_for_date(con, date_key)\n    upload_item(con, yt, session, item, camera_label=camera_label, force=True)')

# Main run: DB-backed labels and per-run reservation
old_run_block = '''    camera_labels = "ABCDEFGH"
    day_counters = defaultdict(int)
    for item in new_items:
        date_key = effective_media_date_string(item)[:10]
        cam_index = day_counters[date_key]
        camera_label = camera_labels[cam_index] if cam_index < len(camera_labels) else str(cam_index + 1)
        day_counters[date_key] += 1
        upload_item(con, yt, session, item, camera_label=camera_label)
'''
new_run_block = '''    reserved_labels_by_date = defaultdict(set)
    for item in new_items:
        date_key = effective_media_date_string(item)[:10]
        camera_label = next_camera_label_for_date(con, date_key, reserved_labels_by_date[date_key])
        reserved_labels_by_date[date_key].add(camera_label)
        upload_item(con, yt, session, item, camera_label=camera_label)
'''
s = s.replace(old_run_block, new_run_block)

p.write_text(s)
