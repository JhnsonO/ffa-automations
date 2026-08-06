#!/usr/bin/env python3
"""OEV GoPro Cloud -> Drive uploader.

Downloads a single GoPro Cloud video (by media_id or exact filename) and
uploads it straight into the OEV Drive folder. No YouTube step, no
uploaded.db bookkeeping — dedupe is by filename already present in the
target Drive folder.

Reuses auth/download/Drive-service helpers from gopro_uploader.py rather
than duplicating them.
"""

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gopro_uploader as gu  # noqa: E402

log = logging.getLogger("oev_drive_uploader")

OEV_DRIVE_FOLDER_ID = "18Y8hI_S29BMeg5FEoxlaqoGy1DsQ7GKJ"

# Prefer the mounted Block Storage volume on vultr-ffa (plenty of room for
# full 4K sessions). Falls back to the repo's default downloads/ dir on
# GitHub-hosted runners where that mount doesn't exist.
OEV_DOWNLOAD_DIR = Path("/mnt/oevdata") if Path("/mnt/oevdata").is_dir() else gu.DOWNLOAD_DIR

MEDIA_ID_OVERRIDE = (os.environ.get("MEDIA_ID_OVERRIDE") or "").strip()
MANUAL_GOPRO_FILENAME = (os.environ.get("MANUAL_GOPRO_FILENAME") or "").strip()


def fetch_media_by_id(session, media_id, max_pages=10):
    """Scan recent GoPro Cloud pages for an exact media id match."""
    log.info(f"Searching GoPro Cloud for media_id={media_id}")
    for page in range(1, max_pages + 1):
        data = gu.gopro_get(session, "/media/search", params={
            "fields": "id,captured_at,created_at,updated_at,filename,file_size,type",
            "order_by": "created_at",
            "order": "desc",
            "per_page": 50,
            "page": page,
            "type": "Video",
        })
        items = data.get("_embedded", {}).get("media", [])
        for item in items:
            if item.get("id") == media_id:
                log.info(f"Found: {item.get('filename')} ({int(item.get('file_size', 0) or 0)/1e9:.2f} GB)")
                return item
        pages = data.get("_pages", {})
        if page >= pages.get("total_pages", 1):
            break
    log.error(f"media_id not found in recent GoPro Cloud pages: {media_id}")
    return None


def upload_to_oev_folder(drive_svc, file_path: Path) -> str:
    """Upload directly into the OEV Drive folder. Skips if same-named file already exists there."""
    existing = drive_svc.files().list(
        q=f"name='{file_path.name}' and '{OEV_DRIVE_FOLDER_ID}' in parents and trashed=false",
        fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    if existing["files"]:
        log.info(f"{file_path.name} already in OEV Drive folder — skipping upload")
        return existing["files"][0]["id"]

    log.info(f"Uploading {file_path.name} to OEV Drive folder ({file_path.stat().st_size/1e9:.2f} GB)")
    media = gu.MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=True)
    uploaded = drive_svc.files().create(
        body={"name": file_path.name, "parents": [OEV_DRIVE_FOLDER_ID]},
        media_body=media, fields="id", supportsAllDrives=True,
    ).execute()
    log.info(f"Done: {file_path.name} -> https://drive.google.com/file/d/{uploaded['id']}")
    return uploaded["id"]


def process_item(session, drive_svc, item) -> bool:
    filename = item["filename"]
    media_id = item["id"]
    dl_url = gu.get_concat_download_url(session, media_id)
    if not dl_url:
        log.error(f"No download URL for {filename}")
        return False
    dest = OEV_DOWNLOAD_DIR / filename
    if not gu.download_video(dl_url, dest):
        log.error(f"Download failed for {filename}")
        return False
    try:
        file_id = upload_to_oev_folder(drive_svc, dest)
    finally:
        dest.unlink(missing_ok=True)
    return bool(file_id)


def run():
    if not MEDIA_ID_OVERRIDE and not MANUAL_GOPRO_FILENAME:
        log.error("Provide media_id or gopro_filename")
        raise SystemExit(1)

    session = gu.get_authenticated_gopro_session()
    drive_svc = gu.get_drive_service()
    if not drive_svc:
        log.error("Could not initialise Drive service (check YOUTUBE_CREDENTIALS/YOUTUBE_TOKEN secrets — same token used for OEV Drive uploads)")
        raise SystemExit(1)

    if MANUAL_GOPRO_FILENAME:
        item = gu.fetch_media_by_filename(session, MANUAL_GOPRO_FILENAME)
    else:
        item = fetch_media_by_id(session, MEDIA_ID_OVERRIDE)

    if not item:
        raise SystemExit(1)

    ok = process_item(session, drive_svc, item)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    run()
