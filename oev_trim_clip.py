#!/usr/bin/env python3
"""Trim a clip that's already in the OEV Drive folder and upload the
trimmed result back into a Trimmed/ subfolder. Runs on vultr-ffa, using
/mnt/oevdata as scratch space.

Reuses Drive auth from gopro_uploader.py (same OAuth token, drive scope).
"""

import io
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gopro_uploader as gu  # noqa: E402
from googleapiclient.http import MediaIoBaseDownload  # noqa: E402

log = logging.getLogger("oev_trim_clip")

OEV_DRIVE_FOLDER_ID = "18Y8hI_S29BMeg5FEoxlaqoGy1DsQ7GKJ"
SCRATCH_DIR = Path("/mnt/oevdata") if Path("/mnt/oevdata").is_dir() else gu.DOWNLOAD_DIR

SOURCE_FILENAME = (os.environ.get("SOURCE_FILENAME") or "").strip()
OFFSET = (os.environ.get("TRIM_OFFSET") or "00:10:00").strip()
DURATION = (os.environ.get("TRIM_DURATION") or "00:00:45").strip()


def find_file_in_folder(drive_svc, filename, parent_id):
    res = drive_svc.files().list(
        q=f"name='{filename}' and '{parent_id}' in parents and trashed=false",
        fields="files(id,name)", supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files", [])
    return files[0] if files else None


def get_or_create_trimmed_folder(drive_svc):
    res = drive_svc.files().list(
        q=f"name='Trimmed' and '{OEV_DRIVE_FOLDER_ID}' in parents and trashed=false and mimeType='application/vnd.google-apps.folder'",
        fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    folder = drive_svc.files().create(
        body={"name": "Trimmed", "mimeType": "application/vnd.google-apps.folder", "parents": [OEV_DRIVE_FOLDER_ID]},
        fields="id",
    ).execute()
    return folder["id"]


def download_from_drive(drive_svc, file_id, dest: Path):
    request = drive_svc.files().get_media(fileId=file_id)
    with open(dest, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=64 * 1024 * 1024)
        done = False
        while not done:
            status, done = downloader.next_chunk()
            if status:
                log.info(f"Download {int(status.progress() * 100)}%")


def run():
    if not SOURCE_FILENAME:
        log.error("SOURCE_FILENAME not provided")
        raise SystemExit(1)

    drive_svc = gu.get_drive_service()
    if not drive_svc:
        log.error("Could not initialise Drive service")
        raise SystemExit(1)

    src_meta = find_file_in_folder(drive_svc, SOURCE_FILENAME, OEV_DRIVE_FOLDER_ID)
    if not src_meta:
        log.error(f"{SOURCE_FILENAME} not found in OEV Drive folder")
        raise SystemExit(1)

    src_path = SCRATCH_DIR / SOURCE_FILENAME
    log.info(f"Downloading {SOURCE_FILENAME} from Drive to {src_path}")
    download_from_drive(drive_svc, src_meta["id"], src_path)

    trimmed_name = f"trimmed_{SOURCE_FILENAME}"
    trimmed_path = SCRATCH_DIR / trimmed_name
    log.info(f"Trimming {SOURCE_FILENAME}: offset={OFFSET} duration={DURATION}")
    result = subprocess.run(
        ["ffmpeg", "-y", "-ss", OFFSET, "-i", str(src_path), "-t", DURATION, "-c", "copy", str(trimmed_path)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        log.error(f"ffmpeg failed:\n{result.stderr[-2000:]}")
        raise SystemExit(1)

    trimmed_folder_id = get_or_create_trimmed_folder(drive_svc)
    log.info(f"Uploading {trimmed_name} to OEV Drive Trimmed/ folder")
    media = gu.MediaFileUpload(str(trimmed_path), mimetype="video/mp4", resumable=True)
    uploaded = drive_svc.files().create(
        body={"name": trimmed_name, "parents": [trimmed_folder_id]},
        media_body=media, fields="id", supportsAllDrives=True,
    ).execute()
    log.info(f"Done: {trimmed_name} -> https://drive.google.com/file/d/{uploaded['id']}")

    src_path.unlink(missing_ok=True)
    trimmed_path.unlink(missing_ok=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    run()
