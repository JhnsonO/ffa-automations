#!/usr/bin/env python3
"""
One-off script: backfill hyperlinks in the Tab Name column of the Index sheet.
Reads each row, finds the matching sheet GID, and rewrites the Tab Name cell
as a HYPERLINK formula pointing to that tab.
"""

import json
import os
from pathlib import Path

def get_sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

INDEX_TAB = "Index"
SPREADSHEET_ID_FILE = Path(__file__).parent / ".ffa_sheet_id"

def main():
    sheets_svc = get_sheets_service()
    spreadsheet_id = SPREADSHEET_ID_FILE.read_text().strip()

    # Build a map of tab name -> GID from the spreadsheet metadata
    meta = sheets_svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    gid_map = {
        s["properties"]["title"]: s["properties"]["sheetId"]
        for s in meta["sheets"]
    }
    print(f"Found {len(gid_map)} tabs")

    # Read the Index tab
    result = sheets_svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id,
        range=f"{INDEX_TAB}!A2:E",
    ).execute()
    rows = result.get("values", [])
    print(f"Found {len(rows)} index row(s) to process")

    updates = 0
    for i, row in enumerate(rows):
        tab_name = row[4] if len(row) > 4 else ""
        if not tab_name:
            continue
        # Skip if already a HYPERLINK formula (starts with =)
        if tab_name.startswith("="):
            print(f"  Row {i+2}: already a hyperlink, skipping")
            continue
        gid = gid_map.get(tab_name)
        if gid is None:
            print(f"  Row {i+2}: no tab found for '{tab_name}', skipping")
            continue
        formula = f'=HYPERLINK("#gid={gid}","{tab_name}")'
        sheets_svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=f"{INDEX_TAB}!E{i+2}",
            valueInputOption="USER_ENTERED",
            body={"values": [[formula]]},
        ).execute()
        print(f"  Row {i+2}: linked '{tab_name}' → gid {gid}")
        updates += 1

    print(f"\nDone. {updates} row(s) updated.")

if __name__ == "__main__":
    main()
