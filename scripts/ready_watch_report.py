#!/usr/bin/env python3
"""Compare ready_to_view observations with when the existing scanner actually uploaded.

usage: ready_watch_report.py ready_watch.json [uploaded.db]
"""
import json
import sqlite3
import sys
from datetime import datetime


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def report(state, uploaded):
    rows, early_bad = [], []
    for mid, r in state.get("media", {}).items():
        first_seen, ready = ts(r["first_seen"]), ts(r.get("first_ready_at"))
        up = ts(uploaded.get(mid))
        rows.append({
            "file": r["filename"], "first_seen": r["first_seen"], "first_ready_at": r.get("first_ready_at"),
            "seen_to_ready_min": round((ready - first_seen).total_seconds() / 60) if ready else None,
            "ready_to_uploaded_min": round((up - ready).total_seconds() / 60) if ready and up else None,
            "ready_size_changed": r.get("ready_then_size_changed"),
            "statuses": sorted({h["ready_to_view"] for h in r["history"] if h["ready_to_view"] is not None}),
        })
        if r.get("ready_then_size_changed"):
            early_bad.append(r["filename"])
    return rows, early_bad


def main(argv):
    state = json.load(open(argv[1]))
    uploaded = {}
    if len(argv) > 2:
        con = sqlite3.connect(argv[2])
        uploaded = {m: u for m, u in con.execute("SELECT media_id, uploaded_at FROM uploads")}
    rows, bad = report(state, uploaded)
    for r in sorted(rows, key=lambda x: x["first_seen"]):
        print(r)
    print(f"\nVideos: {len(rows)} | ever 'ready': {sum(1 for r in rows if r['first_ready_at'])} | "
          f"READY-THEN-GREW (unsafe signal): {len(bad)} {bad}")
    print(f"Fields the API returned: {state.get('item_keys')}")


if __name__ == "__main__":
    main(sys.argv)
