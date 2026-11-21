#!/usr/bin/env python3
"""Upload-on-ready trigger.

Polls GoPro Cloud every INTERVAL seconds for LOOP_MINUTES and, the moment a video is
`ready_to_view == "ready"`, dispatches the EXISTING gopro-upload.yml for that media_id.
No upload logic lives here. Safety nets (all pre-existing):
  * gopro-upload.yml serialises per media_id (concurrency group) and re-checks uploaded.db,
    so the old scanner and this trigger can never double-upload the same video.
  * this script also skips anything already in uploaded.db or dispatched in the last 6h.
Scope: MP4 only; .360 files stay on the old scanner path.
Kill switch: set repo variable READY_TRIGGER_DISABLED=true (log-only mode).
Also records every ready_to_view transition (same state as ready_watch.py).
"""
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import requests

import ready_watch as rw

INTERVAL = int(os.environ.get("POLL_SECONDS") or 120)
LOOP_MINUTES = int(os.environ.get("LOOP_MINUTES") or 340)
REDISPATCH_HOURS = 6
DISABLED = (os.environ.get("TRIGGER_DISABLED") or "").strip().lower() == "true"


def decide(item, uploaded_ids, dispatched, now):
    """Pure: (should_dispatch, reason)."""
    mid = item.get("id")
    name = (item.get("filename") or "")
    size = int(item.get("file_size") or 0)
    if not mid:
        return False, "no id"
    if item.get("ready_to_view") != "ready":
        return False, f"not ready ({item.get('ready_to_view')!r})"
    if name.lower().endswith(".360"):
        return False, ".360 stays on old path"
    if size < rw.MIN_REAL_SIZE:
        return False, f"below size floor ({size / 1e6:.0f} MB)"
    if mid in uploaded_ids:
        return False, "already uploaded"
    last = dispatched.get(mid)
    if last:
        age = datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(last.replace("Z", "+00:00"))
        if age < timedelta(hours=REDISPATCH_HOURS):
            return False, "dispatched recently"
    return True, "ready"


def load_uploaded_ids():
    """media_ids in uploaded.db on main. Returns None if unreadable (then we do NOT dispatch)."""
    try:
        r = rw._gh("GET", "contents/uploaded.db", params={"ref": "main"},
                   headers_extra={"Accept": "application/vnd.github.raw+json"})
        r.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            tf.write(r.content)
            path = tf.name
        con = sqlite3.connect(path)
        ids = {row[0] for row in con.execute("SELECT media_id FROM uploads")}
        con.close()
        os.unlink(path)
        return ids
    except Exception as e:
        print(f"WARN: cannot read uploaded.db ({e}); not dispatching this poll")
        return None


def dispatch_upload(media_id):
    d = rw._gh("POST", "actions/workflows/gopro-upload.yml/dispatches",
               json={"ref": "main", "inputs": {"media_id": media_id}})
    return d.status_code


def restart_self():
    d = rw._gh("POST", "actions/workflows/gopro-ready-trigger.yml/dispatches", json={"ref": "main"})
    print(f"Self-restart dispatched (HTTP {d.status_code})")


def poll_once(session):
    """Returns 'ok' | 'auth'."""
    try:
        items = rw.fetch_recent(session)
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else "?"
        print(f"WARN: GoPro HTTP {code}")
        return "auth" if code == 401 else "ok"
    except requests.exceptions.RequestException as e:
        print(f"WARN: network error {type(e).__name__}")
        return "ok"
    state, sha = rw.load_state()
    now = rw.now_iso()
    changed = rw.observe(state, items, now)
    dispatched = state.setdefault("dispatched", {})
    uploaded = load_uploaded_ids()
    if uploaded is not None:
        for item in items:
            go, why = decide(item, uploaded, dispatched, now)
            if not go:
                continue
            name = item.get("filename")
            if DISABLED:
                print(f"[disabled] would dispatch upload: {name} ({item['id']})")
                continue
            code = dispatch_upload(item["id"])
            print(f"DISPATCHED upload for {name} ({item['id']}) HTTP {code}")
            if code in (200, 201, 204):
                dispatched[item["id"]] = now
                changed = True
    if changed:
        state["updated"] = now
        rw.save_state(state, sha)
    return "ok"


def main():
    if not rw.COOKIE_PATH.exists():
        print("No GoPro cookies file")
        return 0
    session = requests.Session()
    session.cookies.update(__import__("json").loads(rw.COOKIE_PATH.read_text() or "{}"))
    deadline = time.time() + LOOP_MINUTES * 60
    print(f"Ready trigger started: every {INTERVAL}s for {LOOP_MINUTES} min; disabled={DISABLED}")
    while time.time() < deadline:
        if poll_once(session) == "auth":
            r = rw._gh("GET", "actions/workflows/cookie-refresh.yml/runs", params={"per_page": 5})
            runs = r.json().get("workflow_runs", []) if r.ok else []
            if rw.should_dispatch_refresh(runs, rw.now_iso()):
                rw.request_cookie_refresh()
                time.sleep(240)      # let the refresh finish and sync the secret
                restart_self()       # new run starts with the fresh cookies once this one exits
            else:
                print("Refresh already running/recent; exiting, hourly schedule will restart")
            return 0
        time.sleep(INTERVAL)
    print("Loop window finished; next scheduled/pending run continues")
    return 0


if __name__ == "__main__":
    sys.exit(main())
