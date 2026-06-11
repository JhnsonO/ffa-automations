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
existing = [s["properties"]["title"] for s in meta["sheets"]]
if "Clips Tracker" not in existing:
    svc.spreadsheets().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={"requests": [{"addSheet": {"properties": {"title": "Clips Tracker", "index": 2}}}]},
    ).execute()
    svc.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range="'Clips Tracker'!A1:H1",
        valueInputOption="USER_ENTERED",
        body={"values": [["#", "Clip Name", "Session", "Tags", "Drive Link", "Instagram", "TikTok", "YouTube"]]},
    ).execute()
    print("Created Clips Tracker tab")
else:
    print("Already exists")
