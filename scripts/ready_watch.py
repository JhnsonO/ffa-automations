#!/usr/bin/env python3
"""Shadow observer for GoPro Cloud `ready_to_view`.

READ-ONLY. Never downloads or uploads video and never touches the scanner's
queue/DB/tracker files. Every run it lists recent Cloud videos, records any
change in (ready_to_view, file_size) with a timestamp, and stores that state on
the `ready-watch-data` branch so it can later be compared with when the
existing scanner actually queued/uploaded each file.

Question being answered: is `ready_to_view == "ready"` a reliable, EARLY
"fully uploaded" signal, replacing the 1h size-stability wait?
"""
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

GOPRO_API = "https://api.gopro.com"
GOPRO_HEADERS = {"Accept": "application/vnd.gopro.jk.media+json; version=2.0.0"}
COOKIE_PATH = Path(os.environ.get("GOPRO_COOKIE_PATH", "gopro_cookies.json"))
LOCAL_STATE = Path(os.environ.get("READY_WATCH_STATE", "ready_watch.json"))
STATE_BRANCH = "ready-watch-data"
STATE_FILE = "ready_watch.json"
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS") or 3)
MAX_PAGES = int(os.environ.get("MAX_PAGES") or 4)
KEEP_DAYS = 21
MIN_REAL_SIZE = 1_000_000_000  # same "definitely still recording" floor as the scanner
FIELDS = "id,captured_at,created_at,updated_at,filename,file_size,type,ready_to_view,ready_to_edit"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def observe(state, items, now):
    """Pure function: fold one poll of media items into state. Returns True if state changed."""
    changed = False
    media = state.setdefault("media", {})
    keys = set(state.get("item_keys", []))
    for item in items:
        keys.update(item.keys())
        mid = item.get("id")
        if not mid:
            continue
        status = item.get("ready_to_view")
        size = int(item.get("file_size") or 0)
        rec = media.get(mid)
        if rec is None:
            rec = media[mid] = {
                "filename": item.get("filename", ""),
                "created_at": item.get("created_at", ""),
                "first_seen": now,
                "history": [],
            }
            changed = True
        last = rec["history"][-1] if rec["history"] else None
        if last is None or last["ready_to_view"] != status or last["size"] != size:
            rec["history"].append({"t": now, "ready_to_view": status, "size": size})
            changed = True
        if status == "ready" and size >= MIN_REAL_SIZE and "first_ready_at" not in rec:
            rec["first_ready_at"] = now
            rec["first_ready_size"] = size
            changed = True
        # Safety signal: "ready" fired but the file kept growing => ready is NOT a safe trigger.
        if rec.get("first_ready_size") and size != rec["first_ready_size"] and not rec.get("ready_then_size_changed"):
            rec["ready_then_size_changed"] = now
            changed = True
    if sorted(keys) != state.get("item_keys"):
        state["item_keys"] = sorted(keys)
        changed = True
    cutoff = (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for mid in [m for m, r in media.items() if r["first_seen"] < cutoff]:
        del media[mid]
        changed = True
    return changed


def gopro_get(session, params):
    r = session.get(f"{GOPRO_API}/media/search", headers=GOPRO_HEADERS, params=params, timeout=(10, 60))
    r.raise_for_status()
    return r.json()


def fetch_recent(session):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    use_fields = True
    items = []
    for page in range(1, MAX_PAGES + 1):
        params = {"order_by": "created_at", "order": "desc", "per_page": 50, "page": page, "type": "Video"}
        if use_fields:
            params["fields"] = FIELDS
        try:
            data = gopro_get(session, params)
        except requests.exceptions.HTTPError as e:
            if use_fields and e.response is not None and e.response.status_code in (400, 422):
                print(f"WARN: fields={FIELDS!r} rejected (HTTP {e.response.status_code}); retrying without fields")
                use_fields = False
                data = gopro_get(session, {k: v for k, v in params.items() if k != "fields"})
            else:
                raise
        page_items = data.get("_embedded", {}).get("media", [])
        if not page_items:
            break
        stop = False
        for it in page_items:
            if (it.get("created_at") or "9999") < cutoff:
                stop = True
                break
            items.append(it)
        if stop or page >= data.get("_pages", {}).get("total_pages", 1):
            break
    return items


def _gh(method, path, **kw):
    token = os.environ["GITHUB_TOKEN"]
    repo = os.environ["GITHUB_REPOSITORY"]
    return requests.request(
        method, f"https://api.github.com/repos/{repo}/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        timeout=30, **kw,
    )


def load_state():
    """Remote (state branch) when running in Actions, else local file."""
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        r = _gh("GET", f"contents/{STATE_FILE}", params={"ref": STATE_BRANCH})
        if r.status_code == 404:
            return {"media": {}}, None
        r.raise_for_status()
        body = r.json()
        return json.loads(base64.b64decode(body["content"])), body["sha"]
    if LOCAL_STATE.exists():
        return json.loads(LOCAL_STATE.read_text()), None
    return {"media": {}}, None


def ensure_branch():
    if _gh("GET", f"git/ref/heads/{STATE_BRANCH}").status_code == 200:
        return
    # Create an orphan-style branch rooted at an empty tree so main's history isn't dragged along.
    tree = _gh("POST", "git/trees", json={"tree": [{"path": "README.md", "mode": "100644", "type": "blob",
                                                    "content": "Data branch for gopro-ready-watch. Do not merge.\n"}]})
    tree.raise_for_status()
    commit = _gh("POST", "git/commits", json={"message": "init ready-watch-data", "tree": tree.json()["sha"], "parents": []})
    commit.raise_for_status()
    ref = _gh("POST", "git/refs", json={"ref": f"refs/heads/{STATE_BRANCH}", "sha": commit.json()["sha"]})
    ref.raise_for_status()


def save_state(state, sha):
    if os.environ.get("GITHUB_TOKEN") and os.environ.get("GITHUB_REPOSITORY"):
        ensure_branch()
        payload = {
            "message": f"ready-watch {state.get('updated', '')}",
            "content": base64.b64encode(json.dumps(state, indent=1, sort_keys=True).encode()).decode(),
            "branch": STATE_BRANCH,
        }
        if sha:
            payload["sha"] = sha
        _gh("PUT", f"contents/{STATE_FILE}", json=payload).raise_for_status()
    else:
        LOCAL_STATE.write_text(json.dumps(state, indent=1, sort_keys=True))


def main():
    if not COOKIE_PATH.exists():
        print("No GoPro cookies file; nothing to do")
        return 0
    session = requests.Session()
    session.cookies.update(json.loads(COOKIE_PATH.read_text() or "{}"))
    try:
        items = fetch_recent(session)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        # Cookie refresh is owned by the existing scanner; the shadow watcher must not interfere.
        print(f"WARN: GoPro HTTP {code}; skipping this poll (scanner handles cookie refresh)")
        return 0
    except requests.exceptions.RequestException as e:
        print(f"WARN: network error {type(e).__name__}; skipping this poll")
        return 0
    print(f"Fetched {len(items)} video(s)")
    state, sha = load_state()
    now = now_iso()
    changed = observe(state, items, now)
    for it in items[:10]:
        print(f"  {it.get('filename')} | ready_to_view={it.get('ready_to_view')!r} | ready_to_edit={it.get('ready_to_edit')!r} | {int(it.get('file_size') or 0)/1e9:.2f} GB")
    print(f"Item keys seen: {state.get('item_keys')}")
    if changed:
        state["updated"] = now
        save_state(state, sha)
        print("State saved")
    else:
        print("No change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
