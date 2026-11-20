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
