#!/usr/bin/env python3
"""
Cleans up duplicate tabs in the FFA Clips sheet.
Keeps the tab with the most data, deletes the rest.
Also deduplicates the Index tab.
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
    m = re.search(r'HYPERLINK\("([^"]+)"', val or "")
    if m: return m.group(1)
    m = re.search(r"https?://\S+", val or "")
    return m.group(0) if m else ""

def main():
    svc = get_sheets_service()

    # Get all sheets
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    all_sheets = {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}
    video_tabs = {k: v for k, v in all_sheets.items() if k != INDEX_TAB}

    print(f"Found {len(video_tabs)} video tabs + Index")

    # Read all tabs and group by YouTube URL
    url_to_tabs = {}
    for tab_name, gid in video_tabs.items():
        result = svc.spreadsheets().values().get(
            spreadsheetId=SPREADSHEET_ID,
            range=f"'{tab_name}'!A1:F",
            valueRenderOption="FORMULA",
        ).execute()
        rows = result.get("values", [])
        # Try to find YouTube URL in first 3 rows col B
        yt_url = ""
        for i in range(min(3, len(rows))):
            val = rows[i][1] if len(rows[i]) > 1 else ""
            yt_url = extract_url(val)
            if yt_url: break
        # Count data rows (clip rows)
        data_rows = sum(1 for r in rows[5:] if any(str(c).strip() for c in r))
        if yt_url not in url_to_tabs:
            url_to_tabs[yt_url] = []
        url_to_tabs[yt_url].append({
            "tab_name": tab_name,
            "gid": gid,
            "data_rows": data_rows,
            "rows": rows,
        })

    # Find duplicates
    to_delete = []
    for yt_url, tabs in url_to_tabs.items():
        if len(tabs) <= 1:
            continue
        # Keep the one with most data, or the first one if tied
        tabs.sort(key=lambda t: t["data_rows"], reverse=True)
        keep = tabs[0]
        dupes = tabs[1:]
        print(f"\nDuplicate: {yt_url or '(no url)'}")
        print(f"  Keeping:  '{keep['tab_name']}' ({keep['data_rows']} data rows)")
        for d in dupes:
            print(f"  Deleting: '{d['tab_name']}' ({d['data_rows']} data rows)")
            to_delete.append(d["gid"])

    if not to_delete:
        print("\nNo duplicate tabs found.")
    else:
        # Delete duplicate sheets
        requests = [{"deleteSheet": {"sheetId": gid}} for gid in to_delete]
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": requests}
        ).execute()
        print(f"\nDeleted {len(to_delete)} duplicate tab(s)")

    # Deduplicate Index rows
    result = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{INDEX_TAB}!A2:F",
        valueRenderOption="FORMULA",
    ).execute()
    index_rows = result.get("values", [])
    seen_urls = set()
    rows_to_keep = []
    for row in index_rows:
        url = extract_url(row[1]) if len(row) > 1 else ""
        if url and url in seen_urls:
            continue
        seen_urls.add(url)
        rows_to_keep.append(row)

    removed = len(index_rows) - len(rows_to_keep)
    if removed > 0:
        # Clear and rewrite index
        svc.spreadsheets().values().clear(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{INDEX_TAB}!A2:F",
        ).execute()
        if rows_to_keep:
            svc.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=f"{INDEX_TAB}!A2:F",
                valueInputOption="USER_ENTERED",
                body={"values": rows_to_keep},
            ).execute()
        print(f"Removed {removed} duplicate Index row(s)")
    else:
        print("No duplicate Index rows found.")

    print("\nCleanup complete.")

if __name__ == "__main__":
    main()
