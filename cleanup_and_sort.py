#!/usr/bin/env python3
"""
Cleans up the FFA Clips sheet:
  1. Removes non-session tabs (no "Session" in title)
  2. Sorts Index oldest-first by date
  3. Hides fully-processed tabs — keeps visible:
       - Any tab with at least one pending clip (no Drive link yet)
       - The 3 most recent tabs by date (so Kris can always see recent sessions)
     Everything else gets hidden (still accessible, just tidied away)
"""
import json, os, re
from pathlib import Path

SPREADSHEET_ID = Path(__file__).parent.joinpath(".ffa_sheet_id").read_text().strip()
INDEX_TAB = "Index"
KEEP_RECENT = 3  # always keep this many recent tabs visible


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


def is_non_session(title: str) -> bool:
    return "session" not in title.lower()


def tab_has_pending_clips(svc, spreadsheet_id, tab_name):
    """Return True if any clip row has timestamps but no Drive link."""
    try:
        result = svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id,
            range=f"'{tab_name}'!A6:F",
            valueRenderOption="FORMATTED_VALUE",
        ).execute()
        rows = result.get("values", [])
        for row in rows:
            start = row[0] if len(row) > 0 else ""
            end   = row[1] if len(row) > 1 else ""
            link  = row[5] if len(row) > 5 else ""
            if start and end and not str(link).strip():
                return True
        return False
    except Exception:
        return False


def main():
    svc = get_sheets_service()

    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    all_sheets = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}
    video_tabs = {k: v for k, v in all_sheets.items() if k != INDEX_TAB}

    result = svc.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{INDEX_TAB}!A2:F",
        valueRenderOption="FORMULA",
    ).execute()
    index_rows = result.get("values", [])
    print(f"Index has {len(index_rows)} rows, {len(video_tabs)} video tabs")

    # ── Step 1: Find and delete non-session tabs ───────────────────────────
    shorts_to_delete = []
    for row in index_rows:
        yt_url   = extract_url(row[1]) if len(row) > 1 else ""
        video_id = extract_video_id(yt_url)
        title    = row[0] if len(row) > 0 else ""
        if is_non_session(title):
            tab_name = row[4] if len(row) > 4 else ""
            if isinstance(tab_name, str) and tab_name.startswith("="):
                m = re.search(r'"([^"]+)"\s*\)$', tab_name)
                tab_name = m.group(1) if m else ""
            print(f"  Non-session: {title}")
            shorts_to_delete.append((video_id, tab_name))

    deleted_tabs = 0
    for video_id, tab_name in shorts_to_delete:
        if tab_name and tab_name in all_sheets:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SPREADSHEET_ID,
                body={"requests": [{"deleteSheet": {"sheetId": all_sheets[tab_name]["sheetId"]}}]},
            ).execute()
            print(f"  Deleted tab: {tab_name}")
            deleted_tabs += 1

    # ── Step 2: Filter and sort Index ──────────────────────────────────────
    short_ids = {vid_id for vid_id, _ in shorts_to_delete}
    clean_rows = [row for row in index_rows
                  if extract_video_id(extract_url(row[1]) if len(row) > 1 else "") not in short_ids]
    clean_rows.sort(key=lambda r: str(r[3]) if len(r) > 3 else "")
    print(f"\nSorted {len(clean_rows)} rows oldest-first")

    svc.spreadsheets().values().clear(
        spreadsheetId=SPREADSHEET_ID, range=f"{INDEX_TAB}!A2:F"
    ).execute()
    if clean_rows:
        svc.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{INDEX_TAB}!A2",
            valueInputOption="USER_ENTERED",
            body={"values": clean_rows},
        ).execute()

    # ── Step 3: Hide/show tabs ─────────────────────────────────────────────
    # Refresh metadata after deletions
    meta = svc.spreadsheets().get(spreadsheetId=SPREADSHEET_ID).execute()
    all_sheets = {s["properties"]["title"]: s["properties"] for s in meta["sheets"]}

    # Build ordered list of session tabs by date (newest last in clean_rows = index -1 is newest)
    session_tab_names = []
    for row in clean_rows:
        tab_name = row[4] if len(row) > 4 else ""
        if isinstance(tab_name, str) and tab_name.startswith("="):
            m = re.search(r'"([^"]+)"\s*\)$', tab_name)
            tab_name = m.group(1) if m else ""
        if tab_name and tab_name in all_sheets:
            session_tab_names.append(tab_name)

    # Most recent N tabs (last N in sorted list)
    recent_tabs = set(session_tab_names[-KEEP_RECENT:])

    # Tabs with pending clips
    print("\nChecking for pending clips...")
    pending_tabs = set()
    for tab_name in session_tab_names:
        if tab_has_pending_clips(svc, SPREADSHEET_ID, tab_name):
            pending_tabs.add(tab_name)
            print(f"  Pending: {tab_name}")

    # Decide visibility
    hide_requests = []
    show_requests = []
    ALWAYS_VISIBLE = {INDEX_TAB, "Add Video"}
    for tab_name, props in all_sheets.items():
        if tab_name in ALWAYS_VISIBLE:
            continue
        sheet_id       = props["sheetId"]
        currently_hidden = props.get("hidden", False)
        should_show    = tab_name in recent_tabs or tab_name in pending_tabs

        if should_show and currently_hidden:
            show_requests.append({"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "hidden": False},
                "fields": "hidden"
            }})
            print(f"  Show: {tab_name}")
        elif not should_show and not currently_hidden:
            hide_requests.append({"updateSheetProperties": {
                "properties": {"sheetId": sheet_id, "hidden": True},
                "fields": "hidden"
            }})
            print(f"  Hide: {tab_name}")

    all_requests = show_requests + hide_requests
    if all_requests:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=SPREADSHEET_ID,
            body={"requests": all_requests}
        ).execute()

    print(f"\nDone.")
    print(f"  Deleted {deleted_tabs} non-session tab(s)")
    print(f"  Hidden {len(hide_requests)} tab(s), showed {len(show_requests)} tab(s)")
    print(f"  Visible: {len(pending_tabs)} with pending clips + {len(recent_tabs)} most recent")


if __name__ == "__main__":
    main()
