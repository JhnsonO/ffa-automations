"""One-off: dump the full FFA Clips spreadsheet (with grid data, including
tab titles/gids) to a JSON file, for offline inspection and for feeding
ffa-media's import-sheets --payload-file. Read-only -- never writes to the
live sheet. Reuses the same service-account auth pattern as sheet_manager.py.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID_FILE = Path(__file__).parent / ".ffa_sheet_id"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]


def main() -> None:
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    creds = service_account.Credentials.from_service_account_info(sa_info, scopes=SCOPES)
    sheets_svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

    spreadsheet_id = SPREADSHEET_ID_FILE.read_text().strip()
    payload = sheets_svc.spreadsheets().get(
        spreadsheetId=spreadsheet_id, includeGridData=True
    ).execute()

    out_path = Path("clips_sheet_export.json")
    out_path.write_text(json.dumps(payload))
    print(f"Wrote {out_path} ({out_path.stat().st_size} bytes)")

    # Quick structural summary for the workflow log -- tab titles + row counts,
    # so we don't need to open the artifact just to see what's in it.
    for sheet in payload.get("sheets", []):
        props = sheet.get("properties", {})
        rows = sheet.get("data", [{}])[0].get("rowData", [])
        print(f"  gid={props.get('sheetId')!s:>10}  rows={len(rows):>5}  title={props.get('title')!r}")


if __name__ == "__main__":
    main()
