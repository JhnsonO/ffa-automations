"""Offline tests for the shadow ready_to_view observer (no network)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ready_watch as rw  # noqa: E402
import ready_watch_report as rep  # noqa: E402

GB = 1_000_000_000


def item(status, size, mid="m1"):
    return {"id": mid, "filename": "GX01.MP4", "created_at": "2026-11-18T08:00:00Z",
            "file_size": size, "ready_to_view": status}


def test_records_transitions_and_first_ready():
    s = {}
    assert rw.observe(s, [item("uploading", 2 * GB)], "2026-11-18T09:00:00Z")
    assert rw.observe(s, [item("transcoding", 5 * GB)], "2026-11-18T09:10:00Z")
    assert rw.observe(s, [item("ready", 5 * GB)], "2026-11-18T09:20:00Z")
    r = s["media"]["m1"]
    assert [h["ready_to_view"] for h in r["history"]] == ["uploading", "transcoding", "ready"]
    assert r["first_ready_at"] == "2026-11-18T09:20:00Z"
    assert "ready_then_size_changed" not in r


def test_no_change_means_no_write():
    s = {}
    rw.observe(s, [item("ready", 5 * GB)], "2026-11-18T09:00:00Z")
    assert rw.observe(s, [item("ready", 5 * GB)], "2026-11-18T09:10:00Z") is False


def test_ready_then_growth_is_flagged_unsafe():
    s = {}
    rw.observe(s, [item("ready", 5 * GB)], "2026-11-18T09:00:00Z")
    rw.observe(s, [item("ready", 7 * GB)], "2026-11-18T09:10:00Z")
    assert s["media"]["m1"]["ready_then_size_changed"] == "2026-11-18T09:10:00Z"


def test_tiny_ready_files_do_not_count():
    s = {}
    rw.observe(s, [item("ready", 50_000_000)], "2026-11-18T09:00:00Z")
    assert "first_ready_at" not in s["media"]["m1"]


def test_missing_field_is_recorded_not_crashing():
    s = {}
    it = {"id": "m2", "filename": "x", "file_size": 3 * GB, "created_at": "2026-11-18T08:00:00Z"}
    rw.observe(s, [it], "2026-11-18T09:00:00Z")
    assert s["media"]["m2"]["history"][0]["ready_to_view"] is None
    assert "ready_to_view" not in s["item_keys"]


def test_old_records_pruned():
    s = {}
    rw.observe(s, [item("ready", 5 * GB)], "2026-10-01T09:00:00Z")
    rw.observe(s, [item("ready", 5 * GB, mid="m3")], "2026-11-18T09:00:00Z")
    assert "m1" not in s["media"] and "m3" in s["media"]


def test_report_lag_math():
    s = {}
    rw.observe(s, [item("uploading", 2 * GB)], "2026-11-18T09:00:00Z")
    rw.observe(s, [item("ready", 5 * GB)], "2026-11-18T09:30:00Z")
    rows, bad = rep.report(s, {"m1": "2026-11-18T11:30:00Z"})
    assert rows[0]["seen_to_ready_min"] == 30
    assert rows[0]["ready_to_uploaded_min"] == 120
    assert bad == []


def test_refresh_cooldown():
    now = "2026-11-18T10:30:00Z"
    assert rw.should_dispatch_refresh([], now)
    assert not rw.should_dispatch_refresh([{"status": "in_progress", "created_at": "2026-11-18T08:00:00Z"}], now)
    assert not rw.should_dispatch_refresh([{"status": "completed", "created_at": "2026-11-18T10:15:00Z"}], now)
    assert rw.should_dispatch_refresh([{"status": "completed", "created_at": "2026-11-18T09:00:00Z"}], now)

# ---- ready trigger -------------------------------------------------
import ready_trigger as rt  # noqa: E402

NOW = "2026-11-21T10:00:00Z"


def ready_item(**kw):
    base = {"id": "m1", "filename": "GX01.MP4", "file_size": 5 * GB, "ready_to_view": "ready"}
    base.update(kw)
    return base


def test_trigger_dispatches_ready_mp4():
    assert rt.decide(ready_item(), set(), {}, NOW) == (True, "ready")


def test_trigger_skips_not_ready_small_360_uploaded_and_recent():
    assert not rt.decide(ready_item(ready_to_view="uploading"), set(), {}, NOW)[0]
    assert not rt.decide(ready_item(ready_to_view=None), set(), {}, NOW)[0]
    assert not rt.decide(ready_item(file_size=100_000_000), set(), {}, NOW)[0]
    assert not rt.decide(ready_item(filename="GS01.360"), set(), {}, NOW)[0]
    assert not rt.decide(ready_item(), {"m1"}, {}, NOW)[0]
    assert not rt.decide(ready_item(), set(), {"m1": "2026-11-21T08:00:00Z"}, NOW)[0]
    assert rt.decide(ready_item(), set(), {"m1": "2026-11-21T02:00:00Z"}, NOW)[0]  # >6h ago: retry allowed


# ---- uploader: ready bypasses the 30-minute processing guess -------
import sqlite3  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402

import gopro_uploader as u  # noqa: E402


def _mem_db():
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE uploads (media_id TEXT PRIMARY KEY, filename TEXT, captured_at TEXT, youtube_id TEXT, uploaded_at TEXT)")
    con.execute("CREATE TABLE failed_uploads (media_id TEXT PRIMARY KEY, failed_at TEXT, reason TEXT, fail_count INTEGER DEFAULT 1)")
    return con


def _fresh_item(status):
    t = (datetime.now(timezone.utc) - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": "m9", "filename": "GX09.MP4", "file_size": 5 * GB, "captured_at": t,
            "created_at": t, "updated_at": t, "ready_to_view": status}


def test_uploader_still_waits_30min_when_not_ready():
    reasons, _ = u.describe_media_filter(_mem_db(), _fresh_item("transcoding"))
    assert any("still processing" in r for r in reasons)


def test_uploader_skips_30min_wait_when_ready():
    reasons, _ = u.describe_media_filter(_mem_db(), _fresh_item("ready"))
    assert reasons == []


def test_polls_fast_only_when_something_in_flight():
    assert rt.is_active([{"ready_to_view": "uploading"}])
    assert rt.is_active([{"ready_to_view": "ready"}, {"ready_to_view": "transcoding"}])
    assert not rt.is_active([{"ready_to_view": "ready"}, {"ready_to_view": None}])
    assert not rt.is_active([])
    assert rt.next_interval(True) < rt.next_interval(False)


def test_poll_once_idle_makes_no_github_calls_and_ready_dispatches_once(monkeypatch):
    calls = {"uploaded": 0, "dispatch": [], "save": 0}
    monkeypatch.setattr(rw, "load_state", lambda: ({"media": {}}, "sha0"))
    monkeypatch.setattr(rw, "save_state", lambda s, sha: calls.__setitem__("save", calls["save"] + 1) or "sha1")
    monkeypatch.setattr(rt, "load_uploaded_ids", lambda: calls.__setitem__("uploaded", calls["uploaded"] + 1) or set())
    monkeypatch.setattr(rt, "dispatch_upload", lambda mid: calls["dispatch"].append(mid) or 204)
    ctx = {"state": None, "sha": None}

    # 1) a video uploading -> fast mode, nothing dispatched, no uploaded.db read
    monkeypatch.setattr(rw, "fetch_recent", lambda s: [item("uploading", 2 * GB)])
    assert rt.poll_once(None, ctx) == ("ok", True)
    assert calls["uploaded"] == 0 and calls["dispatch"] == []

    # 2) same state again -> identical, still no GitHub calls beyond the first save
    saves = calls["save"]
    assert rt.poll_once(None, ctx) == ("ok", True)
    assert calls["save"] == saves and calls["uploaded"] == 0

    # 3) becomes ready -> exactly one dispatch, back to slow mode
    monkeypatch.setattr(rw, "fetch_recent", lambda s: [item("ready", 5 * GB)])
    assert rt.poll_once(None, ctx) == ("ok", False)
    assert calls["dispatch"] == ["m1"]

    # 4) still ready next poll -> NOT dispatched again
    assert rt.poll_once(None, ctx) == ("ok", False)
    assert calls["dispatch"] == ["m1"]
