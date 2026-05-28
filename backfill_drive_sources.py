#!/usr/bin/env python3
"""
Backfill Drive sources: fetches GoPro source files for videos uploaded
in the last 14 days and saves them to Drive: FFA/Sources/.
Skips anything already in Drive.
"""
import json, os, sqlite3, sys, logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger(__name__)

BASE_DIR  = Path(__file__).parent
DB_PATH   = BASE_DIR / "uploaded.db"
DOWNLOADS = BASE_DIR / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

LOOKBACK_DAYS = 14

# ── reuse helpers from gopro_uploader ────────────────────────────────────────
sys.path.insert(0, str(BASE_DIR))
from gopro_uploader import (
    get_authenticated_gopro_session,
    get_concat_download_url,
    download_video,
    get_drive_service,
    upload_source_to_drive,
)

def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="Only process the N most recent videos")
    ap.add_argument("--days", type=int, default=LOOKBACK_DAYS, help="Lookback window in days")
    args = ap.parse_args()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
    log.info(f"Backfill: last {args.days} days (since {cutoff[:10]}){f', limit {args.limit}' if args.limit else ''}")

    con = sqlite3.connect(str(DB_PATH))
    rows = con.execute(
        """SELECT media_id, filename FROM uploads
           WHERE captured_at >= ?
           AND media_id NOT LIKE 'direct:%'
           AND media_id NOT LIKE 'filename:%'
           ORDER BY captured_at DESC""",
        (cutoff,)
    ).fetchall()
    con.close()

    if args.limit:
        rows = rows[:args.limit]

    if not rows:
        log.info("No GoPro videos found in the last 14 days.")
        return

    log.info(f"Found {len(rows)} GoPro video(s) to check")

    drive_svc = get_drive_service()
    if not drive_svc:
        log.error("GOOGLE_SERVICE_ACCOUNT_JSON not set — cannot upload to Drive")
        sys.exit(1)

    session = get_authenticated_gopro_session()

    for media_id, filename in rows:
        dest = DOWNLOADS / filename

        # Check if already in Drive
        existing = drive_svc.files().list(
            q=f"name='{filename}' and trashed=false",
            fields="files(id,name)"
        ).execute()
        if existing["files"]:
            log.info(f"  Already in Drive: {filename} — skipping")
            continue

        log.info(f"  Fetching {filename} from GoPro Cloud...")
        dl_url = get_concat_download_url(session, media_id)
        if not dl_url:
            log.warning(f"  No download URL for {filename} — may have expired in GoPro Cloud")
            continue

        if not download_video(dl_url, dest):
            log.warning(f"  Download failed for {filename}")
            continue

        upload_source_to_drive(drive_svc, dest)
        dest.unlink(missing_ok=True)
        log.info(f"  Done: {filename}")

    log.info("Backfill complete.")

if __name__ == "__main__":
    main()
