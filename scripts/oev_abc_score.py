#!/usr/bin/env python3
"""OEV-006 A/B/C scorer: camera trace + human annotation -> metrics + gates.

Inputs
  --trace       per-frame CSV from video-stitcher `director_replay`
                (columns: frame, t_s, yaw, pitch, fov, ...). t_s = 0 is the
                window start (the replayed events cover exactly the window).
  --annotation  window annotation (oev/abc/annotation.schema.json)
  --arms        oev/abc/arms.json (frozen thresholds + comfort params)
  --arm         arm id being scored (A0, B0, ...), recorded in the output
  --reference   optional score JSON of A0 on the same window, for the
                "no worse than A0" comfort gates
  --out         output JSON

In-frame test: a point (yaw, pitch) is in frame when its angular offset from
the camera centre is within `in_frame_margin` of the half field of view on
each axis. Horizontal FOV is the pose `fov` (degrees); vertical FOV follows
from the 16:9 output aspect. This is an angular approximation of the
perspective crop (exact at the centre, conservative-to-neutral at the edges
for the narrow FOVs OEV uses); it is the same for every arm, so rankings are
unaffected.

Metrics with no annotated cases are reported as null ("not measurable"),
never as a pass.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import sys
from pathlib import Path

EVENT_TYPES = ("long_pass", "counterattack", "shot", "goal", "near_side_ball", "restart")


def wrap(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def load_trace(path: Path) -> list[dict]:
    rows = []
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            fov = r.get("fov") or ""
            rows.append(
                {
                    "t": float(r["t_s"]),
                    "yaw": float(r["yaw"]),
                    "pitch": float(r["pitch"]),
                    "fov": float(fov) if fov else None,
                }
            )
    if not rows:
        raise ValueError(f"{path}: empty trace")
    rows.sort(key=lambda r: r["t"])
    return rows


class Camera:
    def __init__(self, rows: list[dict], aspect: tuple[int, int], margin: float, default_fov: float = 44.0):
        self.rows = rows
        self.ts = [r["t"] for r in rows]
        self.aspect = aspect[1] / aspect[0]
        self.margin = margin
        self.default_fov = default_fov

    def at(self, t: float) -> dict:
        i = bisect.bisect_left(self.ts, t)
        if i <= 0:
            return self.rows[0]
        if i >= len(self.rows):
            return self.rows[-1]
        a, b = self.rows[i - 1], self.rows[i]
        return a if (t - a["t"]) <= (b["t"] - t) else b

    def contains(self, pose: dict, yaw: float, pitch: float) -> bool:
        hfov = math.radians(pose["fov"] if pose["fov"] is not None else self.default_fov)
        vfov = 2 * math.atan(math.tan(hfov / 2) * self.aspect)
        return (
            abs(wrap(yaw - pose["yaw"])) <= self.margin * hfov / 2
            and abs(pitch - pose["pitch"]) <= self.margin * vfov / 2
        )

    def in_frame_at(self, t: float, yaw: float, pitch: float) -> bool:
        return self.contains(self.at(t), yaw, pitch)

    def in_frame_during(self, t0: float, t1: float, yaw: float, pitch: float) -> bool:
        lo = bisect.bisect_left(self.ts, t0)
        hi = bisect.bisect_right(self.ts, t1)
        return any(self.contains(r, yaw, pitch) for r in self.rows[lo:hi]) or self.in_frame_at(t0, yaw, pitch)


def point_in_polygon(x: float, y: float, poly: list[list[float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def frac(hits: int, n: int):
    return None if n == 0 else hits / n


def neighbour_switches(cam: Camera, ann: dict, min_dwell: float):
    polys = ann.get("neighbour_pitch_polygons") or []
    if not polys:
        return None, None
    # One outline per camera from the annotator, or the single schema polygon.
    targets = ann.get("target_pitch_polygons") or [ann["target_pitch_polygon"]]
    switches = 0
    seconds = 0.0
    run_start = None
    rows = cam.rows
    for k, r in enumerate(rows):
        on_neighbour = any(point_in_polygon(r["yaw"], r["pitch"], p) for p in polys) and not any(
            point_in_polygon(r["yaw"], r["pitch"], t) for t in targets
        )
        if on_neighbour and run_start is None:
            run_start = r["t"]
        end_of_run = run_start is not None and (not on_neighbour or k == len(rows) - 1)
        if end_of_run:
            dur = r["t"] - run_start
            if dur >= min_dwell:
                switches += 1
                seconds += dur
            run_start = None
    return switches, seconds


def comfort(cam: Camera, spike_thr: float, deadband: float):
    rows = cam.rows
    dur_min = max((rows[-1]["t"] - rows[0]["t"]) / 60.0, 1e-9)
    rates = []
    for a, b in zip(rows, rows[1:]):
        dt = b["t"] - a["t"]
        if dt > 0:
            rates.append(((a["t"] + b["t"]) / 2, wrap(b["yaw"] - a["yaw"]) / dt))
    spikes = 0
    in_spike = False
    for (t0, v0), (t1, v1) in zip(rates, rates[1:]):
        dt = t1 - t0
        if dt <= 0:
            continue
        hot = abs((v1 - v0) / dt) > spike_thr
        if hot and not in_spike:
            spikes += 1
        in_spike = hot
    reversals = 0
    direction = 0
    extreme = None
    for r in rows:
        f = r["fov"]
        if f is None:
            continue
        if extreme is None:
            extreme = f
            continue
        delta = f - extreme
        if direction >= 0 and delta <= -deadband:
            reversals += direction == 1
            direction, extreme = -1, f
        elif direction <= 0 and delta >= deadband:
            reversals += direction == -1
            direction, extreme = 1, f
        elif (direction == 1 and f > extreme) or (direction == -1 and f < extreme):
            extreme = f
    return spikes / dur_min, reversals / dur_min


def score(trace_rows: list[dict], ann: dict, arms: dict, arm: str, reference: dict | None) -> dict:
    cp = arms["comfort_params"]
    th = arms["thresholds"]
    cam = Camera(trace_rows, tuple(cp["output_aspect"]), cp["in_frame_margin"])

    pts = [p for p in ann["action_points"] if not p.get("unsure") and p.get("yaw") is not None]
    action_hits = sum(cam.in_frame_at(p["t"], p["yaw"], p["pitch"]) for p in pts)

    by_type: dict[str, list[dict]] = {t: [] for t in EVENT_TYPES}
    for e in ann["events"]:
        if not e.get("unsure") and e["type"] in by_type:
            by_type[e["type"]].append(e)

    def hit(e, within=0.0):
        y, p = e["target"]
        if within > 0:
            return cam.in_frame_during(e["key_t"], e["key_t"] + within, y, p)
        return cam.in_frame_at(e["key_t"], y, p)

    counter_within = th["ranking"]["counterattacks_captured_frac"]["within_s"]
    per_event = {}
    for t, evs in by_type.items():
        w = counter_within if t == "counterattack" else 0.0
        per_event[t] = [{"key_t": e["key_t"], "captured": hit(e, w)} for e in evs]

    shots_goals = per_event["shot"] + per_event["goal"]
    goals_missed = sum(not e["captured"] for e in per_event["goal"])
    switches, switch_s = neighbour_switches(cam, ann, th["hard_gates"]["neighbour_switches_per_10min"]["min_dwell_s"])
    minutes = (trace_rows[-1]["t"] - trace_rows[0]["t"]) / 60.0
    per10 = None if switches is None else switches / max(minutes, 1e-9) * 10.0
    spikes_pm, reversals_pm = comfort(cam, cp["pan_accel_spike_rad_s2"], cp["zoom_reversal_deadband_deg"])

    m = {
        "action_in_frame_frac": frac(action_hits, len(pts)),
        "shots_goals_in_frame_frac": frac(sum(e["captured"] for e in shots_goals), len(shots_goals)),
        "goals_missed": goals_missed if per_event["goal"] else None,
        "counterattacks_captured_frac": frac(sum(e["captured"] for e in per_event["counterattack"]), len(per_event["counterattack"])),
        "long_passes_captured_frac": frac(sum(e["captured"] for e in per_event["long_pass"]), len(per_event["long_pass"])),
        "near_side_balls_in_frame_frac": frac(sum(e["captured"] for e in per_event["near_side_ball"]), len(per_event["near_side_ball"])),
        "restarts_in_frame_frac": frac(sum(e["captured"] for e in per_event["restart"]), len(per_event["restart"])),
        "neighbour_switches": switches,
        "neighbour_switch_seconds": switch_s,
        "neighbour_switches_per_10min": per10,
        "pan_accel_spikes_per_min": spikes_pm,
        "zoom_reversals_per_min": reversals_pm,
    }
    counts = {
        "action_points": len(pts),
        **{t: len(v) for t, v in per_event.items()},
        "minutes": minutes,
    }

    gates = {}
    hg = th["hard_gates"]
    gates["goals_missed"] = None if m["goals_missed"] is None else m["goals_missed"] <= hg["goals_missed"]["max"]
    gates["neighbour_switches_per_10min"] = None if per10 is None else per10 <= hg["neighbour_switches_per_10min"]["max"]
    for key, rule in th["ranking"].items():
        if "min" in rule and key in m:
            gates[key] = None if m[key] is None else m[key] >= rule["min"]
        elif rule.get("max_vs_A0") == "no worse":
            if reference is None:
                gates[key] = None
            else:
                gates[key] = m[key] <= reference["metrics"][key] + 1e-9
    hard = [gates["goals_missed"], gates["neighbour_switches_per_10min"]]
    return {
        "arm": arm,
        "window_id": ann["window_id"],
        "role": ann.get("role"),
        "metrics": m,
        "counts": counts,
        "events": per_event,
        "gates": gates,
        # A known failure fails the arm even when the other gate is unmeasurable.
        "hard_gates_pass": False if any(g is False for g in hard) else (None if any(g is None for g in hard) else True),
        "not_measurable": sorted(k for k, v in gates.items() if v is None),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--trace", required=True, type=Path)
    ap.add_argument("--annotation", required=True, type=Path)
    ap.add_argument("--arms", required=True, type=Path)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--reference", type=Path)
    ap.add_argument("--out", required=True, type=Path)
    a = ap.parse_args(argv)
    ann = json.loads(a.annotation.read_text())
    if ann.get("schema_version") != 1:
        print("annotation schema_version must be 1", file=sys.stderr)
        return 2
    arms = json.loads(a.arms.read_text())
    ref = json.loads(a.reference.read_text()) if a.reference else None
    result = score(load_trace(a.trace), ann, arms, a.arm, ref)
    a.out.write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result[k] for k in ("arm", "window_id", "metrics", "hard_gates_pass", "not_measurable")}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
