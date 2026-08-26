#!/usr/bin/env python3
"""Compare targeted OEV control vs optical-flow experiment telemetry."""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

BALL_CLASS_ID = 32
SYNTHETIC_CONF = 0.05


def load(path: Path) -> dict[str, dict[int, dict[str, Any]]]:
    out: dict[str, dict[int, dict[str, Any]]] = {"detections_raw": {}, "world_state": {}, "pan_decision": {}}
    with path.open() as f:
        for line in f:
            if not line.strip():
                continue
            ev = json.loads(line)
            k = ev.get("kind")
            fi = ev.get("frame_index")
            if k in out and isinstance(fi, int):
                out[k][fi] = ev
    return out


def missing_spans(present: set[int], lo: int, hi: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    start = None
    for fi in range(lo, hi + 1):
        if fi not in present and start is None:
            start = fi
        if fi in present and start is not None:
            spans.append((start, fi - 1))
            start = None
    if start is not None:
        spans.append((start, hi))
    return spans


def longest(spans: list[tuple[int, int]], fps: float) -> dict[str, Any] | None:
    if not spans:
        return None
    s, e = max(spans, key=lambda x: x[1] - x[0])
    n = e - s + 1
    return {"start_frame": s, "end_frame": e, "frames": n, "seconds": n / fps}


def actual_ball_detection_frames(data: dict[str, dict[int, dict[str, Any]]], synthetic_frames: set[int]) -> set[int]:
    result = set()
    for fi, ev in data["detections_raw"].items():
        for d in ev.get("detections", []):
            if d.get("class_id") != BALL_CLASS_ID:
                continue
            # The flow hook uses a distinctive confidence. Only suppress that
            # exact synthetic observation on frames listed by the flow generator.
            if fi in synthetic_frames and abs(float(d.get("confidence", -1)) - SYNTHETIC_CONF) < 1e-6:
                continue
            result.add(fi)
            break
    return result


def accepted_ball_frames(data: dict[str, dict[int, dict[str, Any]]]) -> set[int]:
    out = set()
    for fi, ev in data["world_state"].items():
        b = ev.get("ball")
        if b and b.get("state") != "Lost":
            out.add(fi)
    return out


def state_counts(data: dict[str, dict[int, dict[str, Any]]], lo: int, hi: int) -> dict[str, int]:
    c: Counter[str] = Counter()
    for fi in range(lo, hi + 1):
        b = data["world_state"].get(fi, {}).get("ball")
        c["None" if b is None else str(b.get("state"))] += 1
    return dict(c)


def containment(data: dict[str, dict[int, dict[str, Any]]], lo: int, hi: int) -> dict[str, Any]:
    total = contained = 0
    max_over = 0.0
    for fi in range(lo, hi + 1):
        b = data["world_state"].get(fi, {}).get("ball")
        pose = data["pan_decision"].get(fi, {}).get("pose")
        if not b or b.get("state") == "Lost" or not pose:
            continue
        fov = pose.get("fov_degrees")
        if fov is None:
            continue
        total += 1
        half = math.radians(float(fov)) / 2.0
        off = abs(float(b["yaw"]) - float(pose["yaw"]))
        if off <= half:
            contained += 1
        else:
            max_over = max(max_over, math.degrees(off - half))
    return {
        "frames_evaluable": total,
        "frames_contained_horizontal": contained,
        "fraction_contained_horizontal": contained / total if total else None,
        "max_horizontal_overflow_deg": max_over,
    }


def pan_difference(control: dict[str, dict[int, dict[str, Any]]], experiment: dict[str, dict[int, dict[str, Any]]], lo: int, hi: int) -> dict[str, Any]:
    yaw = []
    fov = []
    for fi in range(lo, hi + 1):
        cp = control["pan_decision"].get(fi, {}).get("pose")
        ep = experiment["pan_decision"].get(fi, {}).get("pose")
        if not cp or not ep:
            continue
        yaw.append(abs(math.degrees(float(ep["yaw"]) - float(cp["yaw"]))))
        if cp.get("fov_degrees") is not None and ep.get("fov_degrees") is not None:
            fov.append(abs(float(ep["fov_degrees"]) - float(cp["fov_degrees"])))
    return {
        "mean_abs_yaw_delta_deg": sum(yaw) / len(yaw) if yaw else None,
        "max_abs_yaw_delta_deg": max(yaw) if yaw else None,
        "mean_abs_fov_delta_deg": sum(fov) / len(fov) if fov else None,
        "max_abs_fov_delta_deg": max(fov) if fov else None,
    }


def summarize_variant(data: dict[str, dict[int, dict[str, Any]]], fps: float, lo: int, hi: int, flo: int, fhi: int, synthetic_frames: set[int]) -> dict[str, Any]:
    raw = actual_ball_detection_frames(data, synthetic_frames)
    accepted = accepted_ball_frames(data)
    raw_window = raw & set(range(lo, hi + 1))
    accepted_window = accepted & set(range(lo, hi + 1))
    raw_failure = raw & set(range(flo, fhi + 1))
    accepted_failure = accepted & set(range(flo, fhi + 1))
    return {
        "detector_ball_frames": len(raw_window),
        "detector_ball_coverage": len(raw_window) / (hi - lo + 1),
        "accepted_ball_frames": len(accepted_window),
        "accepted_ball_coverage": len(accepted_window) / (hi - lo + 1),
        "longest_detector_gap": longest(missing_spans(raw, lo, hi), fps),
        "longest_accepted_ball_loss": longest(missing_spans(accepted, lo, hi), fps),
        "failure_132_141": {
            "detector_ball_frames": len(raw_failure),
            "accepted_ball_frames": len(accepted_failure),
            "frames_total": fhi - flo + 1,
            "longest_detector_gap": longest(missing_spans(raw, flo, fhi), fps),
            "longest_accepted_ball_loss": longest(missing_spans(accepted, flo, fhi), fps),
            "state_counts": state_counts(data, flo, fhi),
            "containment": containment(data, flo, fhi),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--control", required=True, type=Path)
    ap.add_argument("--experiment", required=True, type=Path)
    ap.add_argument("--flow", required=True, type=Path)
    ap.add_argument("--fps", required=True, type=float)
    ap.add_argument("--duration-seconds", required=True, type=float)
    ap.add_argument("--failure-start", default=7.0, type=float, help="seconds into targeted clip")
    ap.add_argument("--failure-end", default=16.0, type=float, help="seconds into targeted clip")
    ap.add_argument("--output", required=True, type=Path)
    args = ap.parse_args()

    control = load(args.control)
    experiment = load(args.experiment)
    flow_entries = [json.loads(x) for x in args.flow.read_text().splitlines() if x.strip()]
    synthetic_frames = {int(x["frame_index"]) for x in flow_entries}
    lo = 0
    hi = max(0, int(round(args.duration_seconds * args.fps)) - 1)
    flo = int(round(args.failure_start * args.fps))
    fhi = min(hi, int(round(args.failure_end * args.fps)) - 1)

    result = {
        "fps": args.fps,
        "target_window_seconds": [0.0, args.duration_seconds],
        "source_window_seconds": [125.0, 125.0 + args.duration_seconds],
        "failure_source_seconds": [125.0 + args.failure_start, 125.0 + args.failure_end],
        "synthetic_flow_frames_generated": len(synthetic_frames),
        "control": summarize_variant(control, args.fps, lo, hi, flo, fhi, set()),
        "experiment": summarize_variant(experiment, args.fps, lo, hi, flo, fhi, synthetic_frames),
        "pan_difference": pan_difference(control, experiment, lo, hi),
        "failure_pan_difference": pan_difference(control, experiment, flo, fhi),
    }

    # How often did an injected flow observation actually become the selected ball?
    accepted_synthetic = 0
    rejected_synthetic = 0
    synthetic_jump_violations = 0
    prev_ball = None
    for fi in sorted(synthetic_frames):
        b = experiment["world_state"].get(fi, {}).get("ball")
        if b and b.get("state") == "Tracking" and abs(float(b.get("confidence", -1)) - SYNTHETIC_CONF) < 1e-6:
            accepted_synthetic += 1
            if prev_ball is not None:
                jump = math.hypot(float(b["yaw"]) - prev_ball[0], float(b["pitch"]) - prev_ball[1])
                if jump > 0.35:
                    synthetic_jump_violations += 1
            prev_ball = (float(b["yaw"]), float(b["pitch"]))
        else:
            rejected_synthetic += 1
    result["flow_acceptance"] = {
        "accepted_synthetic_frames": accepted_synthetic,
        "rejected_synthetic_frames": rejected_synthetic,
        "synthetic_jump_violations_over_0_35rad": synthetic_jump_violations,
    }

    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
