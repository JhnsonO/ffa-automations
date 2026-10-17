"""Tests for scripts/oev_abc_score.py (OEV-006 scorer). Synthetic data only."""

import csv
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import oev_abc_score as s  # noqa: E402

ARMS = json.loads((Path(__file__).resolve().parents[1] / "oev" / "abc" / "arms.json").read_text())
FPS = 60000 / 1001


def trace(seconds=60.0, yaw_fn=lambda t: 0.0, fov_fn=lambda t: 44.0):
    rows = []
    n = int(seconds * FPS)
    for i in range(n):
        t = i / FPS
        rows.append({"t": t, "yaw": yaw_fn(t), "pitch": 0.0, "fov": fov_fn(t)})
    return rows


def ann(**kw):
    base = {
        "schema_version": 1,
        "window_id": "synthetic",
        "session": "test",
        "source": {"left": "l.mp4", "right": "r.mp4"},
        "window_start_s": 0,
        "duration_s": 60,
        "target_pitch_polygon": [[-0.8, -0.3], [0.8, -0.3], [0.8, 0.3], [-0.8, 0.3]],
        "action_points": [],
        "events": [],
    }
    base.update(kw)
    return base


def test_static_camera_action_points_inside_and_outside_frame():
    rows = trace()
    half = math.radians(44) / 2 * 0.9
    a = ann(action_points=[
        {"t": 1, "yaw": 0.0, "pitch": 0.0},
        {"t": 2, "yaw": half * 0.9, "pitch": 0.0},
        {"t": 3, "yaw": half * 1.2, "pitch": 0.0},
        {"t": 4, "yaw": 0.0, "pitch": 0.0, "unsure": True},
    ])
    r = s.score(rows, a, ARMS, "X", None)
    assert r["counts"]["action_points"] == 3
    assert abs(r["metrics"]["action_in_frame_frac"] - 2 / 3) < 1e-9


def test_counterattack_window_and_goal_gate():
    # Camera pans from 0 to +0.6 rad between t=10 and t=11.
    rows = trace(yaw_fn=lambda t: 0.0 if t < 10 else (0.6 if t > 11 else 0.6 * (t - 10)))
    a = ann(events=[
        {"type": "counterattack", "t_start": 9.5, "t_end": 13, "key_t": 9.5, "target": [0.6, 0.0]},
        {"type": "counterattack", "t_start": 5, "t_end": 7, "key_t": 5.0, "target": [0.6, 0.0]},
        {"type": "goal", "t_start": 20, "t_end": 21, "key_t": 20.5, "target": [0.6, 0.0]},
        {"type": "goal", "t_start": 2, "t_end": 3, "key_t": 2.5, "target": [0.6, 0.0]},
    ])
    r = s.score(rows, a, ARMS, "X", None)
    # first counter caught within 2 s of key_t, second never in frame
    assert r["metrics"]["counterattacks_captured_frac"] == 0.5
    assert r["metrics"]["goals_missed"] == 1
    assert r["gates"]["goals_missed"] is False
    assert r["hard_gates_pass"] is False


def test_neighbour_switch_needs_polygons_and_dwell():
    rows = trace(yaw_fn=lambda t: 1.5 if 20 <= t < 22.5 else (1.5 if 40 <= t < 40.5 else 0.0))
    no_poly = s.score(rows, ann(), ARMS, "X", None)
    assert no_poly["metrics"]["neighbour_switches"] is None
    assert "neighbour_switches_per_10min" in no_poly["not_measurable"]
    a = ann(neighbour_pitch_polygons=[[[1.0, -0.3], [2.0, -0.3], [2.0, 0.3], [1.0, 0.3]]])
    r = s.score(rows, a, ARMS, "X", None)
    assert r["metrics"]["neighbour_switches"] == 1  # 0.5 s visit is below the 1 s dwell
    assert abs(r["metrics"]["neighbour_switches_per_10min"] - 10 * 1 / (rows[-1]["t"] / 60)) < 1e-6
    assert r["gates"]["neighbour_switches_per_10min"] is False  # ~10 per 10 min > 1


def test_comfort_metrics_and_reference_gate():
    smooth = trace(yaw_fn=lambda t: 0.2 * math.sin(t / 5))
    jerky = trace(yaw_fn=lambda t: 0.2 * (1 if int(t) % 2 else -1), fov_fn=lambda t: 40 + 4 * (int(t) % 2))
    rs = s.score(smooth, ann(), ARMS, "A0", None)
    rj = s.score(jerky, ann(), ARMS, "B0", rs)
    assert rs["metrics"]["pan_accel_spikes_per_min"] == 0
    assert rj["metrics"]["pan_accel_spikes_per_min"] > 0
    assert rj["metrics"]["zoom_reversals_per_min"] > 0
    assert rj["gates"]["pan_accel_spikes_per_min"] is False
    assert s.score(smooth, ann(), ARMS, "A0", rs)["gates"]["pan_accel_spikes_per_min"] is True


def test_cli_roundtrip(tmp_path):
    p = tmp_path / "trace.csv"
    with p.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["frame", "t_s", "yaw", "pitch", "fov"])
        for i, r in enumerate(trace(seconds=5)):
            w.writerow([i, r["t"], r["yaw"], r["pitch"], r["fov"]])
    a = tmp_path / "ann.json"
    a.write_text(json.dumps(ann(action_points=[{"t": 1, "yaw": 0.0, "pitch": 0.0}])))
    out = tmp_path / "out.json"
    arms = Path(__file__).resolve().parents[1] / "oev" / "abc" / "arms.json"
    assert s.main(["--trace", str(p), "--annotation", str(a), "--arms", str(arms), "--arm", "A0", "--out", str(out)]) == 0
    res = json.loads(out.read_text())
    assert res["metrics"]["action_in_frame_frac"] == 1.0
