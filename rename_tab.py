import json, os
from pathlib import Path
from google.oauth2 import service_account
from googleapiclient.discovery import build

SPREADSHEET_ID = Path(".ffa_sheet_id").read_text().strip()
creds = service_account.Credentials.from_service_account_info(
    json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
    scopes=["https://www.googleapis.com/auth/spreadsheets"],
)
svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
reqs = []
for s in meta["sheets"]:
    title = s["properties"]["title"]
    sid = s["properties"]["sheetId"]
    hidden = s["properties"].get("hidden", False)
    if title == "Wednesday Session | 20 May 2026":
        reqs.append({"deleteSheet": {"sheetId": sid}})
        print(f"Deleting: {title}")
    if title == "Add Video" and hidden:
        reqs.append({"updateSheetProperties": {"properties": {"sheetId": sid, "hidden": False}, "fields": "hidden"}})
        print(f"Showing: Add Video")
if reqs:
    svc.spreadsheets().batchUpdate(spreadsheetId=SPREADSHEET_ID, body={"requests": reqs}).execute()
    print("Done")
else:
    print("Nothing to do")
