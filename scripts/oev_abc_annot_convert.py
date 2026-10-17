#!/usr/bin/env python3
"""Convert an OEV Match Annotator file (camera-frame clicks) to the scorer's
panorama yaw/pitch annotation (oev/abc/annotation.schema.json).

The annotator stores clicks as fractions of each camera frame, the same space
as a detection's `camera_center`. Every detection in the window's
`events.jsonl` pairs `camera_center` with its panorama `position` (yaw,
pitch), so this script fits a per-camera degree-4 polynomial mapping from
those pairs (held-out error on sample_02 run 34236809938: median ~0.1 mrad,
p99 < 0.6 mrad) and applies it to every click and outline vertex.

Clicks outside the area the detections cover (plus a small margin) are
extrapolations: they are converted but listed under `conversion.warnings`.

Usage:
  python3 scripts/oev_abc_annot_convert.py --annotation window.json \
      --events events.jsonl --out window_scorer.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

DEG = 4
MARGIN = 0.03
CAMS = {"left": "Left", "right": "Right"}


def features(x, y, deg=DEG):
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    return np.stack([x**i * y**j for i in range(deg + 1) for j in range(deg + 1 - i)], axis=-1)


class CameraMap:
    def __init__(self, pairs: np.ndarray):
        if len(pairs) < 50:
            raise ValueError(f"only {len(pairs)} detections with positions; need >= 50 to fit")
        a = pairs
        A = features(a[:, 0], a[:, 1])
        self.cy = np.linalg.lstsq(A, a[:, 2], rcond=None)[0]
        self.cp = np.linalg.lstsq(A, a[:, 3], rcond=None)[0]
        resid = np.hypot(A @ self.cy - a[:, 2], A @ self.cp - a[:, 3])
        self.fit_median_mrad = float(np.median(resid) * 1000)
        self.fit_p99_mrad = float(np.percentile(resid, 99) * 1000)
        self.n = int(len(a))
        self.xr = (float(a[:, 0].min()), float(a[:, 0].max()))
        self.yr = (float(a[:, 1].min()), float(a[:, 1].max()))

    def __call__(self, x, y):
        f = features([x], [y])[0]
        return float(f @ self.cy), float(f @ self.cp)

    def covered(self, x, y):
        return (self.xr[0] - MARGIN <= x <= self.xr[1] + MARGIN) and (self.yr[0] - MARGIN <= y <= self.yr[1] + MARGIN)


def load_pairs(events: Path) -> dict:
    out = {"Left": [], "Right": []}
    with events.open() as f:
        for line in f:
            if '"detections_raw"' not in line:
                continue
            o = json.loads(line)
            for d in o.get("detections", []):
                pos = d.get("position")
                if pos and d.get("camera") in out:
                    x, y = d["camera_center"]
                    out[d["camera"]].append((x, y, pos["yaw"], pos["pitch"]))
    return {k: np.array(v, dtype=float) for k, v in out.items()}


def convert(ann: dict, maps: dict) -> dict:
    warnings = []

    def m(cam, x, y, where):
        cm = maps[CAMS[cam]]
        if not cm.covered(x, y):
            warnings.append(f"{where}: {cam} ({x:.3f}, {y:.3f}) is outside the detection coverage; angle extrapolated")
        return cm(x, y)

    points = []
    for p in ann.get("points", []):
        if p.get("x") is None or p.get("cam") is None:
            points.append({"t": p["t"], "yaw": None, "pitch": None, "match_ball_visible": p.get("ball", "UNSURE"), "unsure": True})
            continue
        yaw, pitch = m(p["cam"], p["x"], p["y"], f"aim point t={p['t']}")
        points.append({"t": p["t"], "yaw": round(yaw, 6), "pitch": round(pitch, 6),
                       "match_ball_visible": p.get("ball", True), "unsure": bool(p.get("unsure", False))})

    events = []
    for e in ann.get("events", []):
        out = {"type": e["type"], "t_start": e["t_start"], "t_end": e.get("t_end") if e.get("t_end") is not None else e.get("key_t") or e["t_start"],
               "key_t": e.get("key_t") if e.get("key_t") is not None else e["t_start"], "unsure": bool(e.get("unsure", False))}
        if e.get("x") is None or e.get("cam") is None:
            out.update(target=[0.0, 0.0], unsure=True)
        else:
            yaw, pitch = m(e["cam"], e["x"], e["y"], f"{e['type']} t={e.get('key_t')}")
            out["target"] = [round(yaw, 6), round(pitch, 6)]
        if e.get("note"):
            out["note"] = e["note"]
        events.append(out)

    def poly(cam, pts, what):
        return [[round(v, 6) for v in m(cam, x, y, f"{what} vertex")] for x, y in pts]

    target_polys = [poly(c, ann["polygons"]["target"][c], "our-pitch") for c in ("left", "right") if len(ann["polygons"]["target"].get(c, [])) >= 3]
    neigh_polys = [poly(c, pts, "neighbour-pitch") for c in ("left", "right") for pts in ann["polygons"]["neighbour"].get(c, []) if len(pts) >= 3]

    return {
        "schema_version": 1,
        "window_id": ann["window_id"],
        "session": ann.get("session", ""),
        "role": ann.get("role"),
        "source": {"left": ann["source"].get("left", ""), "right": ann["source"].get("right", ""),
                   "sync_offset_frames": ann["source"].get("right_offset_frames", 0)},
        "window_start_s": ann.get("window_start_s") if ann.get("window_start_s") is not None else 0,
        "duration_s": ann.get("duration_s") or 0,
        "target_pitch_polygon": target_polys[0] if target_polys else [[-3.2, -1.6], [3.2, -1.6], [3.2, 1.6], [-3.2, 1.6]],
        "target_pitch_polygons": target_polys,
        "neighbour_pitch_polygons": neigh_polys,
        "action_points": points,
        "events": events,
        "prelabel": ann.get("prelabel", ""),
        "human_corrected": True,
        "conversion": {
            "method": f"per-camera degree-{DEG} polynomial fit camera_center -> (yaw, pitch) from window detections",
            "fit": {c: {"detections": maps[c].n, "median_mrad": round(maps[c].fit_median_mrad, 3), "p99_mrad": round(maps[c].fit_p99_mrad, 3)} for c in maps},
            "warnings": warnings,
            "target_outline_drawn": bool(target_polys),
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--annotation", required=True, type=Path)
    ap.add_argument("--events", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    ann = json.loads(a.annotation.read_text())
    if ann.get("schema") != "oev-abc-annot-camera/1":
        print("not an OEV Match Annotator file (schema oev-abc-annot-camera/1)", file=sys.stderr)
        return 2
    pairs = load_pairs(a.events)
    maps = {c: CameraMap(pairs[c]) for c in ("Left", "Right")}
    out = convert(ann, maps)
    a.out.write_text(json.dumps(out, indent=1))
    c = out["conversion"]
    print(json.dumps({"window_id": out["window_id"], "points": len(out["action_points"]), "events": len(out["events"]),
                      "fit": c["fit"], "warnings": len(c["warnings"]), "target_outline_drawn": c["target_outline_drawn"]}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
