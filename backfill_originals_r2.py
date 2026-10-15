"""One-off: copy original GoPro files from Drive FFA/Sources to R2 for videos
already on YouTube, so ffa-media renders skip the YouTube relay.

Writes cache/youtube/<youtube_id>/master.mp4 plus an original.keep marker
(same layout as gopro_uploader.upload_source_to_r2). Idempotent: anything
already in R2 is skipped. DRY_RUN=true (default) copies nothing.
Env: DAYS (default 60), DRY_RUN, MAX_FILES (default 100), S3_* secrets.
"""
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from googleapiclient.http import MediaIoBaseDownload

import gopro_uploader as g

DAYS = int(os.environ.get("DAYS", "60"))
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"
MAX_FILES = int(os.environ.get("MAX_FILES", "100"))
BUDGET_S = 5 * 3600  # stop cleanly before the 6h job limit


def main() -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)
    con = sqlite3.connect(g.BASE_DIR / "uploaded.db")
    rows = []
    for fn, cap, yt, up in con.execute(
        "select filename, captured_at, youtube_id, uploaded_at from uploads "
        "where youtube_id is not null and youtube_id != '' order by coalesce(captured_at, uploaded_at) desc"
    ):
        stamp = (cap or up or "").replace("Z", "+00:00")
        try:
            when = datetime.fromisoformat(stamp)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when >= cutoff:
            rows.append((fn, yt, when))
    print(f"{len(rows)} video(s) in the last {DAYS} days; dry_run={DRY_RUN}")

    drive = g.get_drive_service()
    ffa_id = g.get_ffa_drive_folder_id()
    if not drive or not ffa_id:
        print("::error::Drive service or folder id unavailable")
        return 1
    res = drive.files().list(
        q=f"name='Sources' and mimeType='application/vnd.google-apps.folder' and trashed=false and '{ffa_id}' in parents",
        fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute()
    if not res["files"]:
        print("::error::FFA/Sources folder not found")
        return 1
    sources_id = res["files"][0]["id"]

    s3 = boto3.client(
        "s3", endpoint_url=os.environ["S3_ENDPOINT_URL"],
        aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
        region_name="auto", config=Config(signature_version="s3v4"),
    )
    bucket = os.environ["S3_BUCKET"]
    started = time.time()
    copied = missing = present = 0
    total_bytes = 0
    work = Path("/tmp/backfill")
    work.mkdir(exist_ok=True)
    for fn, yt, when in rows:
        if copied >= MAX_FILES or time.time() - started > BUDGET_S:
            print("limit reached, stopping; run again to continue")
            break
        key = f"cache/youtube/{re.sub(r'[^A-Za-z0-9._-]', '_', yt)}/master.mp4"
        try:
            s3.head_object(Bucket=bucket, Key=key)
            present += 1
            continue
        except Exception:
            pass
        found = drive.files().list(
            q=f"name='{fn}' and '{sources_id}' in parents and trashed=false",
            fields="files(id,size)", supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()["files"]
        if not found:
            missing += 1
            print(f"MISSING in Drive: {fn} ({yt}) - stays on the relay")
            continue
        size = int(found[0].get("size", 0) or 0)
        total_bytes += size
        if DRY_RUN:
            print(f"would copy {fn} ({size / 1e9:.1f} GB) -> {key}")
            copied += 1
            continue
        local = work / fn
        try:
            with open(local, "wb") as fh:
                dl = MediaIoBaseDownload(fh, drive.files().get_media(fileId=found[0]["id"], supportsAllDrives=True), chunksize=64 * 1024 * 1024)
                done = False
                while not done:
                    _, done = dl.next_chunk()
            s3.upload_file(str(local), bucket, key, ExtraArgs={"ContentType": "video/mp4"})
            s3.put_object(Bucket=bucket, Key=key.rsplit("/", 1)[0] + "/original.keep", Body=b"")
            copied += 1
            print(f"copied {fn} ({size / 1e9:.1f} GB) -> {key}")
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::failed {fn}: {exc}")
        finally:
            local.unlink(missing_ok=True)
    print(f"summary: {'would copy' if DRY_RUN else 'copied'}={copied} ({total_bytes / 1e9:.1f} GB) already_in_r2={present} missing_in_drive={missing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
