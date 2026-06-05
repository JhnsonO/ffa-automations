
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
for s in meta["sheets"]:
    if s["properties"]["title"] == "Wednesday Session_JYTTPq":
        sheet_id = s["properties"]["sheetId"]
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": [{"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "title": "Wednesday Session | 20 May 2026"},
                "fields": "title"
            }}]}
        ).execute()
        print("Renamed to: Wednesday Session | 20 May 2026")
        break
else:
    print("Tab not found")
