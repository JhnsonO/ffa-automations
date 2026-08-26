#!/usr/bin/env python3
"""Generate a causal, bounded optical-flow continuation for OEV ball gaps.

EXPERIMENT ONLY. The script consumes a control run's events plus the original
left/right clips. It starts only from a control WorldState Tracking ball that
can be matched back to a real YOLO ball detection. During subsequent frames
where the control selected ball is not Tracking, pyramidal Lucas-Kanade flow
tracks local features around that last real ball box. Successful flow emits a
synthetic raw Detection for the existing Reco mapping/tracker chain.

No future detector observation is used to advance flow. A later real Tracking
observation only terminates/reseeds the bridge and is used after the fact for
endpoint-error telemetry.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np

BALL_CLASS_ID = 32
MAX_BRIDGE_SECONDS = 0.85
MIN_FEATURES = 5
FB_ERROR_PX = 1.75
LK_ERROR_MAX = 35.0
MAX_STEP_PX = 80.0
MAX_DISPERSION_PX = 12.0
MIN_SEED_CONF = 0.10
SYNTHETIC_CONF = 0.05


def load_events(path: Path) -> tuple[dict[int, dict[str, Any]], dict[int, dict[str, Any]]]:
    detections: dict[int, dict[str, Any]] = {}
    worlds: dict[int, dict[str, Any]] = {}
    with path.open() as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            ev = json.loads(raw)
            fi = ev.get("frame_index")
            if not isinstance(fi, int):
                continue
            if ev.get("kind") == "detections_raw":
                detections[fi] = ev
            elif ev.get("kind") == "world_state":
                worlds[fi] = ev
    return detections, worlds


def ball_detections(ev: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not ev:
        return []
    return [d for d in ev.get("detections", []) if d.get("class_id") == BALL_CLASS_ID]


def match_selected_detection(world: dict[str, Any] | None, det_ev: dict[str, Any] | None) -> dict[str, Any] | None:
    if not world:
        return None
    ball = world.get("ball")
    if not ball or ball.get("state") != "Tracking" or float(ball.get("confidence", 0.0)) < MIN_SEED_CONF:
        return None
    by, bp = float(ball["yaw"]), float(ball["pitch"])
    origin = ball.get("origin")
    best = None
    best_dist = float("inf")
    for d in ball_detections(det_ev):
        pos = d.get("position") or {}
        if "yaw" not in pos or "pitch" not in pos:
            continue
        # Prefer the selected track's reporting camera, but allow a fallback
        # if the event's diagnostic origin is absent/inconsistent.
        camera_penalty = 0.05 if origin and d.get("camera") != origin else 0.0
        dist = math.hypot(float(pos["yaw"]) - by, float(pos["pitch"]) - bp) + camera_penalty
        if dist < best_dist:
            best_dist = dist
            best = d
    if best is None or best_dist > 0.08:
        return None
    return best


def open_capture(path: Path, start_frame: int) -> tuple[cv2.VideoCapture, float, int, int]:
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"could not open {path}")
    fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if fps <= 0 or width <= 0 or height <= 0:
        raise RuntimeError(f"invalid video metadata for {path}: fps={fps} size={width}x{height}")
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    return cap, fps, width, height


def roi_bounds(center: tuple[float, float], size: tuple[float, float], width: int, height: int) -> tuple[int, int, int, int]:
    cx, cy = center
    bw, bh = size
    # Ball boxes are tiny. Expand enough to get trackable image structure,
    # while keeping the aperture local enough not to become a player/grass tracker.
    half_w = max(24.0, bw * width * 2.5)
    half_h = max(24.0, bh * height * 2.5)
    px, py = cx * width, cy * height
    x0 = max(0, int(px - half_w))
    x1 = min(width, int(px + half_w + 1))
    y0 = max(0, int(py - half_h))
    y1 = min(height, int(py + half_h + 1))
    return x0, y0, x1, y1


def seed_features(gray: np.ndarray, center: tuple[float, float], size: tuple[float, float]) -> np.ndarray | None:
    h, w = gray.shape[:2]
    x0, y0, x1, y1 = roi_bounds(center, size, w, h)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return None
    roi = gray[y0:y1, x0:x1]
    pts = cv2.goodFeaturesToTrack(
        roi,
        maxCorners=30,
        qualityLevel=0.01,
        minDistance=4,
        blockSize=5,
        useHarrisDetector=False,
    )
    if pts is None:
        return None
    pts[:, 0, 0] += x0
    pts[:, 0, 1] += y0
    return pts.astype(np.float32)


def read_frame(cap: cv2.VideoCapture) -> np.ndarray:
    ok, frame = cap.read()
    if not ok or frame is None:
        raise EOFError
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True, type=Path)
    ap.add_argument("--left", required=True, type=Path)
    ap.add_argument("--right", required=True, type=Path)
    ap.add_argument("--start-seconds", required=True, type=float)
    ap.add_argument("--duration-seconds", required=True, type=float)
    ap.add_argument("--output", required=True, type=Path)
    ap.add_argument("--report", required=True, type=Path)
    args = ap.parse_args()

    det_events, worlds = load_events(args.events)
    if not worlds:
        raise SystemExit("no world_state events in control trace")

    left_probe = cv2.VideoCapture(str(args.left))
    fps = float(left_probe.get(cv2.CAP_PROP_FPS) or 0.0)
    left_probe.release()
    if fps <= 0:
        raise SystemExit("could not determine source fps")
    start_frame = int(round(args.start_seconds * fps))
    target_frames = int(round(args.duration_seconds * fps))
    max_bridge_frames = max(1, int(round(MAX_BRIDGE_SECONDS * fps)))

    lcap, lfps, lw, lh = open_capture(args.left, start_frame)
    rcap, rfps, rw, rh = open_capture(args.right, start_frame)
    if abs(lfps - rfps) > 0.05 or (lw, lh) != (rw, rh):
        raise SystemExit(f"left/right mismatch: left={lfps},{lw}x{lh} right={rfps},{rw}x{rh}")

    prev_gray: dict[str, np.ndarray | None] = {"Left": None, "Right": None}
    current_gray: dict[str, np.ndarray | None] = {"Left": None, "Right": None}
    active: dict[str, Any] | None = None
    pending_span: dict[str, Any] | None = None
    spans: list[dict[str, Any]] = []
    emitted: list[dict[str, Any]] = []
    termination_counts: dict[str, int] = {}

    def terminate(reason: str, fi: int, reacq: dict[str, Any] | None = None) -> None:
        nonlocal active, pending_span
        if pending_span is not None:
            pending_span["end_frame"] = fi - 1
            pending_span["duration_frames"] = max(0, fi - pending_span["start_frame"])
            pending_span["duration_seconds"] = pending_span["duration_frames"] / fps
            pending_span["termination"] = reason
            if reacq is not None and active is not None and reacq.get("camera") == active.get("camera"):
                rc = reacq.get("camera_center") or [None, None]
                if rc[0] is not None:
                    dx = (float(rc[0]) - active["center"][0]) * lw
                    dy = (float(rc[1]) - active["center"][1]) * lh
                    pending_span["reacquisition_error_px"] = math.hypot(dx, dy)
            spans.append(pending_span)
        termination_counts[reason] = termination_counts.get(reason, 0) + 1
        active = None
        pending_span = None

    for fi in range(target_frames):
        try:
            current_gray["Left"] = read_frame(lcap)
            current_gray["Right"] = read_frame(rcap)
        except EOFError:
            terminate("video_eof", fi)
            break

        world = worlds.get(fi)
        selected = match_selected_detection(world, det_events.get(fi))

        # A genuine accepted YOLO observation always wins and immediately
        # re-anchors optical flow. This is the only normal way to start a bridge.
        if selected is not None:
            if active is not None:
                terminate("yolo_reacquired", fi, selected)
            camera = selected["camera"]
            center = tuple(map(float, selected["camera_center"]))
            size = tuple(map(float, selected["camera_size"]))
            pts = seed_features(current_gray[camera], center, size)
            if pts is not None and len(pts) >= MIN_FEATURES:
                active = {
                    "camera": camera,
                    "center": center,
                    "size": size,
                    "points": pts,
                    "gap_frames": 0,
                    "seed_frame": fi,
                    "seed_confidence": float(selected.get("confidence", 0.0)),
                }
            else:
                active = None

        else:
            # Never invent a track from nothing: no recent selected YOLO seed means no flow.
            if active is not None:
                camera = active["camera"]
                prev = prev_gray[camera]
                curr = current_gray[camera]
                if prev is None:
                    terminate("missing_previous_frame", fi)
                elif active["gap_frames"] >= max_bridge_frames:
                    terminate("max_duration", fi)
                else:
                    p0 = active["points"]
                    p1, st1, err1 = cv2.calcOpticalFlowPyrLK(
                        prev,
                        curr,
                        p0,
                        None,
                        winSize=(21, 21),
                        maxLevel=3,
                        criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
                    )
                    if p1 is None or st1 is None:
                        terminate("lk_forward_failed", fi)
                    else:
                        p0r, st2, _ = cv2.calcOpticalFlowPyrLK(
                            curr,
                            prev,
                            p1,
                            None,
                            winSize=(21, 21),
                            maxLevel=3,
                            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
                        )
                        if p0r is None or st2 is None:
                            terminate("lk_backward_failed", fi)
                        else:
                            fb = np.linalg.norm(p0r[:, 0, :] - p0[:, 0, :], axis=1)
                            err = err1.reshape(-1) if err1 is not None else np.zeros(len(p0))
                            good = (st1.reshape(-1) == 1) & (st2.reshape(-1) == 1) & (fb <= FB_ERROR_PX) & (err <= LK_ERROR_MAX)
                            old = p0[good, 0, :]
                            new = p1[good, 0, :]
                            if len(new) < MIN_FEATURES:
                                terminate("too_few_features", fi)
                            else:
                                disp = new - old
                                med = np.median(disp, axis=0)
                                residual = np.linalg.norm(disp - med, axis=1)
                                mad = float(np.median(residual))
                                step = float(np.linalg.norm(med))
                                if not np.isfinite(med).all() or step > MAX_STEP_PX or mad > MAX_DISPERSION_PX:
                                    terminate("unstable_flow", fi)
                                else:
                                    cx = active["center"][0] + float(med[0]) / lw
                                    cy = active["center"][1] + float(med[1]) / lh
                                    if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0):
                                        terminate("out_of_frame", fi)
                                    else:
                                        active["center"] = (cx, cy)
                                        active["gap_frames"] += 1
                                        # Keep only features near the translated local ball aperture.
                                        x0, y0, x1, y1 = roi_bounds(active["center"], active["size"], lw, lh)
                                        local = new[(new[:, 0] >= x0) & (new[:, 0] < x1) & (new[:, 1] >= y0) & (new[:, 1] < y1)]
                                        if len(local) < MIN_FEATURES:
                                            refreshed = seed_features(curr, active["center"], active["size"])
                                            if refreshed is None or len(refreshed) < MIN_FEATURES:
                                                terminate("feature_aperture_empty", fi)
                                                local = None
                                            else:
                                                active["points"] = refreshed
                                        else:
                                            active["points"] = local[:, None, :].astype(np.float32)

                                        if active is not None:
                                            if pending_span is None:
                                                pending_span = {
                                                    "start_frame": fi,
                                                    "seed_frame": active["seed_frame"],
                                                    "camera": camera,
                                                    "seed_confidence": active["seed_confidence"],
                                                }
                                            detection = {
                                                "camera": camera,
                                                "class_id": BALL_CLASS_ID,
                                                "confidence": SYNTHETIC_CONF,
                                                "center_x": cx,
                                                "center_y": cy,
                                                "width": active["size"][0],
                                                "height": active["size"][1],
                                            }
                                            entry = {
                                                "frame_index": fi,
                                                "detection": detection,
                                                "quality": {
                                                    "features": int(len(active["points"])),
                                                    "fb_median_px": float(np.median(fb[good])),
                                                    "lk_error_median": float(np.median(err[good])),
                                                    "step_px": step,
                                                    "dispersion_px": mad,
                                                    "bridge_age_frames": active["gap_frames"],
                                                },
                                            }
                                            emitted.append(entry)

        prev_gray["Left"] = current_gray["Left"]
        prev_gray["Right"] = current_gray["Right"]

    if active is not None:
        terminate("window_end", target_frames)

    lcap.release()
    rcap.release()

    with args.output.open("w") as f:
        for entry in emitted:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

    report = {
        "fps": fps,
        "start_seconds": args.start_seconds,
        "duration_seconds": args.duration_seconds,
        "source_start_frame": start_frame,
        "max_bridge_frames": max_bridge_frames,
        "max_bridge_seconds": MAX_BRIDGE_SECONDS,
        "synthetic_frames": len(emitted),
        "bridge_spans": spans,
        "termination_counts": termination_counts,
        "parameters": {
            "min_features": MIN_FEATURES,
            "fb_error_px": FB_ERROR_PX,
            "lk_error_max": LK_ERROR_MAX,
            "max_step_px": MAX_STEP_PX,
            "max_dispersion_px": MAX_DISPERSION_PX,
            "min_seed_conf": MIN_SEED_CONF,
            "synthetic_conf": SYNTHETIC_CONF,
        },
    }
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
