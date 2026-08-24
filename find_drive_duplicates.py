#!/usr/bin/env python3
"""
FFA Drive Duplicate Scanner
============================
Read-only script. Walks the FFA/Clips folder tree in Google Drive and
reports duplicate files — either same filename within the same folder,
or identical md5Checksum anywhere in the tree (true content duplicates,
e.g. from the clip-extractor re-upload bug where a Drive link write
fails after upload and the row gets reprocessed next run).

Does NOT delete, move, or modify anything. Writes duplicates_report.json
and prints a human-readable summary.

Environment variables required
-------------------------------
  YOUTUBE_TOKEN  — existing YouTube/Drive OAuth token secret (same one
                   sheet_manager.py uses for Drive access; service
                   accounts can't list/read personal Drive content).
"""

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

FFA_DRIVE_FOLDER_ID_FILE = Path(__file__).parent / ".ffa_drive_folder_id"


def _get_ffa_drive_folder_id() -> str:
    if FFA_DRIVE_FOLDER_ID_FILE.exists():
        return FFA_DRIVE_FOLDER_ID_FILE.read_text().strip()
    return ""


def get_drive_service():
    """Same OAuth pattern as sheet_manager.py's get_sheets_service() — Drive
    needs the user OAuth token, not the service account (which can't see
    personal Drive content without domain-wide delegation)."""
    from google.oauth2.credentials import Credentials as OAuthCreds
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    token_file = Path(__file__).parent / "youtube_token.json"
    token_json = os.environ.get("YOUTUBE_TOKEN", "")
    if token_file.exists():
        token_path = token_file
    elif token_json:
        token_path = Path("/tmp/youtube_token.json")
        token_path.write_text(token_json)
    else:
        print("ERROR: no YOUTUBE_TOKEN env var and no youtube_token.json file found.")
        sys.exit(1)

    scopes = ["https://www.googleapis.com/auth/drive"]
    creds = OAuthCreds.from_authorized_user_file(str(token_path), scopes)
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_folder(drive_svc, name: str, parent_id: str):
    q = (f"name='{name}' and mimeType='application/vnd.google-apps.folder' "
         f"and trashed=false and '{parent_id}' in parents")
    res = drive_svc.files().list(
        q=q, fields="files(id,name)", supportsAllDrives=True, includeItemsFromAllDrives=True
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def walk_folder(drive_svc, folder_id: str, path: str, all_files: list):
    """Recursively list every file under folder_id, recording (path, file dict)."""
    page_token = None
    while True:
        res = drive_svc.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType,size,md5Checksum,createdTime,parents)",
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageSize=1000,
        ).execute()
        for f in res.get("files", []):
            if f["mimeType"] == "application/vnd.google-apps.folder":
                walk_folder(drive_svc, f["id"], f"{path}/{f['name']}", all_files)
            else:
                all_files.append((path, f))
        page_token = res.get("nextPageToken")
        if not page_token:
            break


def main():
    ffa_id = _get_ffa_drive_folder_id()
    if not ffa_id:
        print("ERROR: .ffa_drive_folder_id not set — can't locate FFA root folder.")
        sys.exit(1)

    drive_svc = get_drive_service()

    clips_id = _find_folder(drive_svc, "Clips", ffa_id)
    if not clips_id:
        print("ERROR: could not find 'Clips' folder under FFA root.")
        sys.exit(1)

    print("Scanning FFA/Clips folder tree (this may take a few minutes)...")
    all_files = []
    walk_folder(drive_svc, clips_id, "Clips", all_files)
    print(f"Found {len(all_files)} files total.\n")

    # Group 1: same filename within the same folder
    by_folder_name = defaultdict(list)
    for path, f in all_files:
        by_folder_name[(path, f["name"])].append(f)

    # Group 2: identical content (md5Checksum) anywhere in the tree
    by_checksum = defaultdict(list)
    for path, f in all_files:
        md5 = f.get("md5Checksum")
        if md5:
            by_checksum[md5].append((path, f))

    name_dupes = {k: v for k, v in by_folder_name.items() if len(v) > 1}
    checksum_dupes = {k: v for k, v in by_checksum.items() if len(v) > 1}

    report = {"filename_duplicates": [], "content_duplicates": []}
    reclaimable_bytes = 0

    print("=" * 70)
    print(f"DUPLICATE FILENAMES (same folder): {len(name_dupes)} group(s)")
    print("=" * 70)
    for (path, name), files in name_dupes.items():
        sizes = [int(f.get("size", 0)) for f in files]
        reclaimable_bytes += sum(sorted(sizes)[:-1])  # keep the largest, rest reclaimable
        print(f"\n{path}/{name}  ({len(files)} copies)")
        for f in files:
            print(f"  - id={f['id']}  size={f.get('size','?')}  created={f['createdTime']}")
        report["filename_duplicates"].append({
            "path": path, "name": name,
            "files": [{"id": f["id"], "size": f.get("size"), "created": f["createdTime"]} for f in files],
        })

    print("\n" + "=" * 70)
    print(f"DUPLICATE CONTENT (identical md5, any name/folder): {len(checksum_dupes)} group(s)")
    print("=" * 70)
    for md5, entries in checksum_dupes.items():
        print(f"\nmd5={md5}  ({len(entries)} copies)")
        for path, f in entries:
            print(f"  - {path}/{f['name']}  id={f['id']}  size={f.get('size','?')}  created={f['createdTime']}")
        report["content_duplicates"].append({
            "md5": md5,
            "files": [{"path": path, "name": f["name"], "id": f["id"],
                       "size": f.get("size"), "created": f["createdTime"]} for path, f in entries],
        })

    report["reclaimable_bytes_estimate"] = reclaimable_bytes
    with open("duplicates_report.json", "w") as fh:
        json.dump(report, fh, indent=2)

    print("\n" + "=" * 70)
    print(f"Estimated reclaimable space (filename dupes, keeping largest copy): "
          f"{reclaimable_bytes / 1e9:.2f} GB")
    print("Full report written to duplicates_report.json")
    print("=" * 70)


if __name__ == "__main__":
    main()
