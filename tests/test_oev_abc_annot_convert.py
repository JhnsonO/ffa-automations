"""Tests for scripts/oev_abc_annot_convert.py (annotator -> scorer annotation)."""

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import oev_abc_annot_convert as c  # noqa: E402
import oev_abc_score as s  # noqa: E402


def true_map(cam, x, y):
    # Smooth, lens-like synthetic mapping per camera.
    off = -0.9 if cam == "Left" else 0.9
    yaw = off + 1.6 * (x - 0.5) + 0.12 * (x - 0.5) ** 3 - 0.05 * (x - 0.5) * (y - 0.6)
    pitch = -1.0 * (y - 0.55) + 0.04 * (x - 0.5) ** 2
    return yaw, pitch


def write_events(path, n=4000, seed=1):
    rng = np.random.default_rng(seed)
    with path.open("w") as f:
        for i in range(n // 20):
            dets = []
            for _ in range(20):
                cam = "Left" if rng.random() < 0.5 else "Right"
                x, y = rng.uniform(0.05, 0.95), rng.uniform(0.3, 0.98)
                yaw, pitch = true_map(cam, x, y)
                dets.append({"camera": cam, "class_id": 0, "confidence": 0.9, "camera_center": [x, y],
                             "camera_size": [0.02, 0.06], "position": {"yaw": yaw, "pitch": pitch, "fov_degrees": None}})
            f.write(json.dumps({"kind": "detections_raw", "frame_index": i, "detections": dets}) + "\n")
            f.write(json.dumps({"kind": "world_state", "frame_index": i, "timestamp_ms": 0, "players": [], "ball": None}) + "\n")


def annotation():
    return {
        "schema": "oev-abc-annot-camera/1", "window_id": "synthetic", "session": "test", "role": "development",
        "window_start_s": 1000, "source": {"left": "l.mp4", "right": "r.mp4", "right_offset_frames": 7},
        "fps": 59.94, "duration_s": 300,
        "points": [
            {"t": 1.0, "cam": "left", "x": 0.4, "y": 0.6, "ball": True},
            {"t": 2.0, "cam": "right", "x": 0.7, "y": 0.5, "ball": True},
            {"t": 3.0, "cam": None, "x": None, "y": None, "ball": False, "unsure": True},
            {"t": 4.0, "cam": "left", "x": 0.5, "y": 0.05, "ball": True},  # sky: outside coverage
        ],
        "events": [
            {"id": "e1", "type": "long_pass", "t_start": 5, "key_t": 6.2, "t_end": 7, "cam": "right", "x": 0.6, "y": 0.55},
            {"id": "e2", "type": "goal", "t_start": 8, "key_t": None, "t_end": 9, "cam": None, "x": None, "y": None, "unsure": True},
        ],
        "polygons": {"target": {"left": [[0.1, 0.5], [0.9, 0.45], [0.9, 0.95], [0.1, 0.95]], "right": []},
                     "neighbour": {"left": [], "right": [[[0.2, 0.32], [0.6, 0.32], [0.6, 0.4], [0.2, 0.4]]]}},
    }


def test_conversion_accuracy_and_flags(tmp_path):
    ev = tmp_path / "events.jsonl"
    write_events(ev)
    maps = {k: c.CameraMap(v) for k, v in c.load_pairs(ev).items()}
    out = c.convert(annotation(), maps)
    p0, p1 = out["action_points"][0], out["action_points"][1]
    for p, cam, x, y in ((p0, "Left", 0.4, 0.6), (p1, "Right", 0.7, 0.5)):
        ty, tp = true_map(cam, x, y)
        assert abs(p["yaw"] - ty) < 1e-3 and abs(p["pitch"] - tp) < 1e-3
    assert out["action_points"][2]["yaw"] is None and out["action_points"][2]["unsure"] is True
    assert any("aim point t=4.0" in w for w in out["conversion"]["warnings"]), out["conversion"]["warnings"]
    lp = out["events"][0]
    ty, tp = true_map("Right", 0.6, 0.55)
    assert abs(lp["target"][0] - ty) < 1e-3 and lp["key_t"] == 6.2
    assert out["events"][1]["unsure"] is True
    assert len(out["target_pitch_polygons"]) == 1 and len(out["neighbour_pitch_polygons"]) == 1
    assert out["source"]["sync_offset_frames"] == 7


def test_cli_output_scores(tmp_path):
    ev = tmp_path / "events.jsonl"
    write_events(ev)
    ann = tmp_path / "ann.json"
    ann.write_text(json.dumps(annotation()))
    out = tmp_path / "scorer.json"
    assert c.main(["--annotation", str(ann), "--events", str(ev), "--out", str(out)]) == 0
    conv = json.loads(out.read_text())
    # Camera that stays on the converted long-pass target: the scorer accepts the file.
    target = conv["events"][0]["target"]
    rows = [{"t": i / 59.94, "yaw": target[0], "pitch": target[1], "fov": 44.0} for i in range(int(12 * 59.94))]
    arms = json.loads((ROOT / "oev" / "abc" / "arms.json").read_text())
    r = s.score(rows, conv, arms, "X", None)
    assert r["metrics"]["long_passes_captured_frac"] == 1.0
    assert r["metrics"]["neighbour_switches"] == 0


def test_rejects_wrong_schema(tmp_path):
    ann = tmp_path / "a.json"
    ann.write_text(json.dumps({"schema": "other"}))
    ev = tmp_path / "e.jsonl"
    write_events(ev, n=200)
    assert c.main(["--annotation", str(ann), "--events", str(ev), "--out", str(tmp_path / "o.json")]) == 2
