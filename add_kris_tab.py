#!/usr/bin/env python3
"""
One-off: creates the tab for Monday Session | 6th of January (tEJUsAYDacY)
and pre-populates Kris's timestamps. Skips if the tab already exists.
"""
import json, os
from pathlib import Path

SPREADSHEET_ID = Path(__file__).parent.joinpath(".ffa_sheet_id").read_text().strip()
YT_URL   = "https://www.youtube.com/watch?v=tEJUsAYDacY"
TAB_NAME = "Monday Session | 6th of January"
TITLE    = "Monday Session | 6th of January"

CLIPS = [
    ("4:20",  "4:30",  "clean goal"),
    ("7:50",  "8:05",  "defence 1"),
    ("8:45",  "8:55",  "defence 2"),
    ("9:10",  "9:20",  "nice goal"),
    ("19:15", "19:25", "open goal miss"),
    ("21:10", "21:25", "goal 3"),
    ("29:25", "29:35", "GK block antics"),
    ("29:39", "29:50", "GK save"),
    ("33:35", "33:50", "CR7 celebration"),
    ("37:06", "37:16", "team work miss"),
    ("38:55", "39:07", "Turn leads to goal"),
    ("39:16", "39:30", "GK overdoing it"),
    ("39:45", "40:05", "painful goal"),
    ("41:10", "41:15", "volley goal"),
    ("47:45", "47:55", "obstruct goal"),
    ("49:45", "49:55", "proper defence"),
    ("51:15", "51:35", "mysterious goal or nah"),
]

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

def main():
    svc = get_sheets_service()

    # Check if tab already exists
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    existing = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

    if TAB_NAME in existing:
        print(f"Tab '{TAB_NAME}' already exists — just adding clips to it")
        sheet_gid = existing[TAB_NAME]
    else:
        # Create the tab
        resp = svc.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"addSheet": {"properties": {"title": TAB_NAME}}}]},
        ).execute()
        sheet_gid = resp["replies"][0]["addSheet"]["properties"]["sheetId"]
        print(f"Created tab '{TAB_NAME}' (gid={sheet_gid})")

        # Write header block
        yt_formula = f'=HYPERLINK("{YT_URL}","▶ Watch on YouTube")'
        header = [
            ["Title",   TITLE],
            ["YouTube", yt_formula],
            ["Source",  "—"],
            [],
            ["Start", "End", "Name", "Tags", "Status", "Link"],
        ]
        svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{TAB_NAME}'!A1:F5",
            valueInputOption="USER_ENTERED",
            body={"values": header},
        ).execute()

        # Add to Index
        tab_formula = f'=HYPERLINK("#gid={sheet_gid}","{TAB_NAME}")'
        yt_idx_formula = f'=HYPERLINK("{YT_URL}","▶ Watch")'
        svc.spreadsheets().values().append(
            spreadsheetId=SPREADSHEET_ID,
            range="Index!A:F",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [[TITLE, yt_idx_formula, "", "2025-01-06", tab_formula, "Active"]]},
        ).execute()
        print("Added to Index tab")

    # Append clips (check how many rows already exist first)
    existing_rows = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{TAB_NAME}'!A6:F",
    ).execute().get("values", [])

    if existing_rows:
        print(f"{len(existing_rows)} clip row(s) already exist — skipping clip insert")
    else:
        rows = [[start, end, name, "", "", ""] for start, end, name in CLIPS]
        svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{TAB_NAME}'!A6:F{5+len(rows)}",
            valueInputOption="USER_ENTERED",
            body={"values": rows},
        ).execute()
        print(f"Added {len(rows)} clip rows")

    print("Done.")

if __name__ == "__main__":
    main()
