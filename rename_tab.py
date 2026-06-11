import json, os, re
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = Path(".ffa_sheet_id").read_text().strip()
creds = service_account.Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)
svc = build("sheets", "v4", credentials=creds, cache_discovery=False)

TRACKER = "Clips Tracker"
INDEX = "Index"
ALWAYS_SKIP = {INDEX, TRACKER, "Add Video"}

meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
session_tabs = [s["properties"]["title"] for s in meta["sheets"] if s["properties"]["title"] not in ALWAYS_SKIP]

all_clips = []

for tab in session_tabs:
    try:
        result = svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{tab}'!A6:F",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        rows = result.get("values", [])
        for row in rows:
            name = row[2] if len(row) > 2 else ""
            tags = row[3] if len(row) > 3 else ""
            link = row[5] if len(row) > 5 else ""
            if name and link and str(link).strip():
                # Extract URL from HYPERLINK formula or plain URL
                m = re.search(r"https://drive\.google\.com/\S+", str(link))
                drive_url = m.group(0).rstrip('")\'') if m else str(link)
                all_clips.append([name, tab, tags, drive_url])
    except Exception as e:
        print(f"  Error reading {tab}: {e}")

print(f"Found {len(all_clips)} existing processed clips")

if not all_clips:
    print("Nothing to backfill")
else:
    # Clear existing tracker data (keep header)
    svc.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{TRACKER}'!A2:H"
    ).execute()

    # Write all clips with auto-incrementing #
    rows = []
    for i, (name, session, tags, drive_url) in enumerate(all_clips, start=1):
        drive_formula = f'=HYPERLINK("{drive_url}","▶ View Clip")'
        rows.append([i, name, session, tags, drive_formula, "", "", ""])

    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"'{TRACKER}'!A2",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()
    print(f"Backfilled {len(rows)} clips into Clips Tracker")
