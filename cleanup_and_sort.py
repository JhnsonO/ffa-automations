#!/usr/bin/env python3
"""
One-off: removes Short tabs from the sheet and sorts the Index by session date (oldest first).
"""
import json, os, re
from pathlib import Path

SPREADSHEET_ID = Path(__file__).parent.joinpath(".ffa_sheet_id").read_text().strip()
INDEX_TAB = "Index"

def get_sheets_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    return build("sheets", "v4", credentials=creds, cache_discovery=False)

def extract_url(val):
    m = re.search(r'HYPERLINK\("([^"]+)"', str(val or ""))
    if m: return m.group(1)
    m = re.search(r"https?://\S+", str(val or ""))
    return m.group(0) if m else ""

def extract_video_id(yt_url):
    m = re.search(r"[?&]v=([A-Za-z0-9_-]{11})", yt_url)
    return m.group(1) if m else ""

def is_short(video_id):
    if not video_id:
        return False
    import urllib.request
    try:
        req = urllib.request.Request(
            f"https://www.youtube.com/shorts/{video_id}",
            headers={"User-Agent": "Mozilla/5.0"},
            method="HEAD"
        )
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler())
        resp = opener.open(req, timeout=5)
        return resp.status == 200
    except Exception:
        return False

def main():
    svc = get_sheets_service()

    # Get all sheet metadata
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    all_sheets = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    video_tabs = {k: v for k, v in all_sheets.items() if k != INDEX_TAB}

    # Read Index
    result = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{INDEX_TAB}!A2:F",
        valueRenderOption="FORMULA",
    ).execute()
    index_rows = result.get("values", [])
    print(f"Index has {len(index_rows)} rows, {len(video_tabs)} video tabs")

    # Find Short tabs by checking YouTube URL
    shorts_to_delete = []
    for row in index_rows:
        yt_url = extract_url(row[1]) if len(row) > 1 else ""
        video_id = extract_video_id(yt_url)
        if video_id and is_short(video_id):
            tab_name = row[4] if len(row) > 4 else ""
            if isinstance(tab_name, str) and tab_name.startswith("="):
                # Extract tab name from hyperlink formula
                m = re.search(r'"([^"]+)"\s*\)$', tab_name)
                tab_name = m.group(1) if m else ""
            print(f"  Short found: {row[0]} ({video_id})")
            shorts_to_delete.append((video_id, tab_name))

    # Delete Short tabs
    deleted_tabs = 0
    for video_id, tab_name in shorts_to_delete:
        if tab_name and tab_name in all_sheets:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"requests": [{"deleteSheet": {"sheetId": all_sheets[tab_name]}}]},
            ).execute()
            print(f"  Deleted tab: {tab_name}")
            deleted_tabs += 1

    # Filter Shorts from index rows and sort remaining by date (col D), oldest first
    short_ids = {vid_id for vid_id, _ in shorts_to_delete}
    clean_rows = []
    for row in index_rows:
        yt_url = extract_url(row[1]) if len(row) > 1 else ""
        vid_id = extract_video_id(yt_url)
        if vid_id not in short_ids:
            clean_rows.append(row)

    # Sort by date column (index 3) ascending — oldest first
    def sort_key(row):
        date = row[3] if len(row) > 3 else ""
        return str(date)

    clean_rows.sort(key=sort_key)
    print(f"\nSorted {len(clean_rows)} rows by date (oldest first)")

    # Rewrite Index
    svc.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{INDEX_TAB}!A2:F",
    ).execute()

    if clean_rows:
        svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{INDEX_TAB}!A2",
            valueInputOption="USER_ENTERED",
            body={"values": clean_rows},
        ).execute()

    print(f"\nDone. Removed {len(shorts_to_delete)} Short(s), sorted {len(clean_rows)} remaining videos oldest-first.")

if __name__ == "__main__":
    main()
