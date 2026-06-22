#!/usr/bin/env python3
"""
FFA 360 Ball Tracker — Detector Audit
======================================
Samples AUDIT_SAMPLE_COUNT frames evenly from equirect_trim.mp4.
Runs YOLO ball detector (all 4 crops) on each sampled frame.
Cross-references v11 tracking.json confirmed positions.
Classifies each sampled frame as one of:
  DETECTED_CONFIRMED  — candidate near tracker confirmed position, accepted by scorer
  DETECTED_REJECTED   — candidate near tracker position but filtered out (log why)
  DETECTED_NO_MATCH   — candidates exist but none near any plausible ball position
  NOT_DETECTED        — zero raw candidates in frame

Output: audit.json  (uploaded to Drive)
"""

import argparse
import json
import math
import os
import subprocess

import cv2
import numpy as np
from ultralytics import YOLO

# ---------------------------------------------------------------------------
# Config — mirrors run_tracker.py
# ---------------------------------------------------------------------------
FFMPEG           = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
CROP_YAWS_DEG    = [0, 90, 180, 270]
CROP_FOV_DEG     = 110
CROP_W           = 1280
CROP_H           = 720
DEDUP_THRESH_DEG = 15
YOLO_CONF        = 0.12
YOLO_IMGSZ       = 1280
BALL_CLASS_ID    = 0

PITCH_HARD_MAX   = 18.0

# Hotspot suppression (v11 values)
HOTSPOT_SUPPRESS_RADIUS = 5.0

# Audit config
AUDIT_SAMPLE_COUNT  = 75
CONFIRMED_MATCH_DEG = 10.0   # candidate within this of smoothed position = "near tracker"

# ---------------------------------------------------------------------------
# Geometry (copied from run_tracker.py)
# ---------------------------------------------------------------------------
def crop_pixel_to_yaw_pitch(px, py, crop_yaw_deg, fov_deg, w, h):
    nx = (px - w / 2.0) / (w / 2.0)
    ny = (py - h / 2.0) / (h / 2.0)
    f  = 1.0 / math.tan(math.radians(fov_deg / 2.0))
    ray = np.array([nx / f, -ny / f * (w / h), 1.0])
    ray = ray / np.linalg.norm(ray)
    cy = math.radians(crop_yaw_deg)
    Ry = np.array([[ math.cos(cy), 0, math.sin(cy)],
                   [            0, 1,            0],
                   [-math.sin(cy), 0, math.cos(cy)]])
    world = Ry @ ray
    yaw   = math.degrees(math.atan2(world[0], world[2]))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, world[1]))))
    return yaw, pitch


def angular_distance(y1, p1, y2, p2):
    dy = math.radians(y1 - y2)
    return math.degrees(math.acos(max(-1.0, min(1.0,
        math.sin(math.radians(p1)) * math.sin(math.radians(p2)) +
        math.cos(math.radians(p1)) * math.cos(math.radians(p2)) * math.cos(dy)
    ))))


def dedupe_detections(detections, thresh_deg=DEDUP_THRESH_DEG):
    kept = []
    for det in sorted(detections, key=lambda d: -d[2]):
        yaw, pitch, conf = det[:3]
        if not any(angular_distance(yaw, pitch, k[0], k[1]) < thresh_deg for k in kept):
            kept.append(det)
    return kept


def extract_crop_frame(equirect_frame, crop_yaw_deg, fov_deg, out_w, out_h):
    h_eq, w_eq = equirect_frame.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_w / 2.0) / f
    ry = -(yv - out_h / 2.0) / f
    rz = np.ones_like(rx)
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    cy = math.radians(crop_yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    yaw_map   = np.arctan2(wx, wz)
    pitch_map = np.arcsin(np.clip(wy, -1, 1))
    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    return cv2.remap(equirect_frame,
                     map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------
def get_device():
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            vram = torch.cuda.get_device_properties(0).total_memory // (1024**2)
            print(f"[gpu] CUDA: {name} ({vram}MB) — using GPU")
            return "cuda"
    except ImportError:
        pass
    print("[gpu] No CUDA — using CPU")
    return "cpu"


# ---------------------------------------------------------------------------
# Hotspot suppression check
# ---------------------------------------------------------------------------
def is_hotspot_suppressed(yaw, pitch, hotspots):
    for hs in hotspots:
        if angular_distance(yaw, pitch, hs["yaw"], hs["pitch"]) < HOTSPOT_SUPPRESS_RADIUS:
            return True
    return False


# ---------------------------------------------------------------------------
# Main audit
# ---------------------------------------------------------------------------
def run_audit(equirect_path, tracking_json_path, output_path,
              ball_model_path="models/football-ball-detection.pt"):

    device = get_device()
    print(f"[audit] Loading YOLO model: {ball_model_path}")
    ball_model = YOLO(ball_model_path)
    ball_model.to(device)

    # Load tracking.json
    print(f"[audit] Loading tracking.json from: {tracking_json_path}")
    with open(tracking_json_path) as f:
        tracking_data = json.load(f)

    frames_list = tracking_data.get("frames", [])
    metadata    = tracking_data.get("metadata", {})
    fps         = tracking_data.get("fps", 30.0)

    # Extract hotspots from v11 metadata for suppression
    hotspots = []
    v11_meta = metadata.get("v11_bootstrap_metrics", {})
    for hs in v11_meta.get("bootstrap_hotspots", []):
        hotspots.append({"yaw": hs["yaw"], "pitch": hs["pitch"]})
    print(f"[audit] Loaded {len(hotspots)} hotspot(s) from tracking.json for suppression")

    # Build frame index: frame_idx → {smoothed_yaw, smoothed_pitch, tracker_state, best_score}
    frame_index = {}
    for rec in frames_list:
        fi = rec.get("frame")
        if fi is None:
            continue
        sm = rec.get("smoothed") or {}
        frame_index[fi] = {
            "smoothed_yaw":   sm.get("yaw"),
            "smoothed_pitch": sm.get("pitch"),
            "tracker_state":  rec.get("tracker_state"),
            "best_score":     rec.get("best_score"),
            "loss_state":     rec.get("loss_state"),
        }

    total_tracked_frames = len(frames_list)
    print(f"[audit] tracking.json covers {total_tracked_frames} frames")

    # Open video and sample frame indices
    os.environ["OPENCV_FFMPEG_READ_ATTEMPTS"] = "65536"
    cap = cv2.VideoCapture(equirect_path, cv2.CAP_FFMPEG)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[audit] Video: {total_frames} frames @ {fps:.2f} fps")

    # Evenly spaced sample indices (avoid first/last 10 frames)
    margin = 10
    sample_indices = sorted(set(
        int(margin + i * (total_frames - 2 * margin) / (AUDIT_SAMPLE_COUNT - 1))
        for i in range(AUDIT_SAMPLE_COUNT)
    ))
    print(f"[audit] Sampling {len(sample_indices)} frames: {sample_indices[0]}…{sample_indices[-1]}")

    # ---------------------------------------------------------------------------
    # Per-frame audit
    # ---------------------------------------------------------------------------
    audit_frames = []
    counts = {
        "DETECTED_CONFIRMED":  0,
        "DETECTED_REJECTED":   0,
        "DETECTED_NO_MATCH":   0,
        "NOT_DETECTED":        0,
    }
    rejection_reasons = {
        "pitch_hard_max": 0,
        "hotspot":        0,
        "low_score":      0,
        "no_match":       0,
    }

    prev_frame = -1
    for sample_i, fi in enumerate(sample_indices):
        # Seek to frame
        if fi != prev_frame + 1:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ret, frame = cap.read()
        if not ret:
            print(f"  [frame {fi}] SKIP — read failed")
            continue
        prev_frame = fi

        # Run YOLO on all 4 crops
        raw_detections = []
        for crop_yaw in CROP_YAWS_DEG:
            crop = extract_crop_frame(frame, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
            res  = ball_model(crop, imgsz=YOLO_IMGSZ, conf=YOLO_CONF,
                              verbose=False, classes=[BALL_CLASS_ID], device=device)
            for box in res[0].boxes:
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                px, py = (x1 + x2) / 2, (y1 + y2) / 2
                conf   = float(box.conf[0])
                area   = (x2 - x1) * (y2 - y1)
                yaw_d, pitch_d = crop_pixel_to_yaw_pitch(
                    px, py, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                raw_detections.append({
                    "yaw": round(yaw_d, 2), "pitch": round(pitch_d, 2),
                    "conf": round(conf, 3), "area": round(area, 1),
                    "crop_yaw": crop_yaw
                })

        deduped = dedupe_detections([(d["yaw"], d["pitch"], d["conf"]) for d in raw_detections])

        # Tracker ground truth for this frame
        tracker_rec = frame_index.get(fi, {})
        tr_yaw      = tracker_rec.get("smoothed_yaw")
        tr_pitch    = tracker_rec.get("smoothed_pitch")
        tr_state    = tracker_rec.get("tracker_state", "UNKNOWN")
        tr_score    = tracker_rec.get("best_score")
        tr_loss     = tracker_rec.get("loss_state", "")

        # Was this frame "confirmed" by the tracker?
        tracker_confirmed = (tr_score is not None and tr_score > 0)

        # Classify
        if not raw_detections:
            classification = "NOT_DETECTED"
            counts["NOT_DETECTED"] += 1
            frame_result = {
                "frame": fi,
                "classification": classification,
                "raw_count": 0,
                "deduped_count": 0,
                "tracker_state": tr_state,
                "tracker_confirmed": tracker_confirmed,
                "tracker_best_score": tr_score,
                "tracker_loss_state": tr_loss,
                "candidates": [],
                "rejection_detail": None,
            }
        else:
            # Check each deduped candidate against tracker position and filters
            classified = False
            frame_candidates = []
            closest_to_tracker = None
            closest_dist = 999.0

            for yaw_d, pitch_d, conf_d in deduped:
                cand_info = {
                    "yaw": round(yaw_d, 2),
                    "pitch": round(pitch_d, 2),
                    "conf": round(conf_d, 3),
                }

                # Distance to tracker confirmed position
                dist_to_tracker = None
                if tr_yaw is not None and tr_pitch is not None:
                    dist_to_tracker = round(angular_distance(yaw_d, pitch_d, tr_yaw, tr_pitch), 2)
                    if dist_to_tracker < closest_dist:
                        closest_dist = dist_to_tracker
                        closest_to_tracker = (yaw_d, pitch_d, conf_d, dist_to_tracker)

                # Check filters
                pitch_rejected = pitch_d > PITCH_HARD_MAX
                hotspot_rejected = is_hotspot_suppressed(yaw_d, pitch_d, hotspots)

                cand_info["dist_to_tracker"] = dist_to_tracker
                cand_info["pitch_rejected"]   = pitch_rejected
                cand_info["hotspot_rejected"] = hotspot_rejected
                frame_candidates.append(cand_info)

            # Now classify the frame
            rejection_detail = None

            if tr_yaw is not None and tr_pitch is not None and closest_to_tracker is not None and closest_dist <= CONFIRMED_MATCH_DEG:
                yaw_d, pitch_d, conf_d, dist = closest_to_tracker
                # Candidate is near tracker position — was it accepted or rejected?
                pitch_rej   = pitch_d > PITCH_HARD_MAX
                hotspot_rej = is_hotspot_suppressed(yaw_d, pitch_d, hotspots)

                if pitch_rej:
                    classification = "DETECTED_REJECTED"
                    rejection_detail = "pitch_hard_max"
                    rejection_reasons["pitch_hard_max"] += 1
                    counts["DETECTED_REJECTED"] += 1
                elif hotspot_rej:
                    classification = "DETECTED_REJECTED"
                    rejection_detail = "hotspot"
                    rejection_reasons["hotspot"] += 1
                    counts["DETECTED_REJECTED"] += 1
                elif not tracker_confirmed:
                    # Candidate near tracker but tracker didn't confirm — scorer rejected
                    classification = "DETECTED_REJECTED"
                    rejection_detail = "low_score_or_hysteresis"
                    rejection_reasons["low_score"] += 1
                    counts["DETECTED_REJECTED"] += 1
                else:
                    classification = "DETECTED_CONFIRMED"
                    counts["DETECTED_CONFIRMED"] += 1
            else:
                # Candidates exist but none near tracker
                classification = "DETECTED_NO_MATCH"
                rejection_detail = "no_candidate_near_tracker"
                rejection_reasons["no_match"] += 1
                counts["DETECTED_NO_MATCH"] += 1

            frame_result = {
                "frame": fi,
                "classification": classification,
                "raw_count": len(raw_detections),
                "deduped_count": len(deduped),
                "tracker_state": tr_state,
                "tracker_confirmed": tracker_confirmed,
                "tracker_best_score": tr_score,
                "tracker_loss_state": tr_loss,
                "tracker_smoothed_yaw":   round(tr_yaw, 2) if tr_yaw is not None else None,
                "tracker_smoothed_pitch": round(tr_pitch, 2) if tr_pitch is not None else None,
                "closest_to_tracker_deg": round(closest_dist, 2) if closest_to_tracker else None,
                "candidates": frame_candidates,
                "rejection_detail": rejection_detail,
            }

        audit_frames.append(frame_result)

        if sample_i % 10 == 0 or sample_i == len(sample_indices) - 1:
            print(f"  [{sample_i+1}/{len(sample_indices)}] frame={fi} "
                  f"state={tr_state} class={frame_result['classification']} "
                  f"raw={frame_result['raw_count']} deduped={frame_result.get('deduped_count',0)}")

    cap.release()

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    total_sampled = len(audit_frames)
    summary = {
        "total_sampled": total_sampled,
        "counts": counts,
        "pct": {
            k: round(100.0 * v / max(total_sampled, 1), 1)
            for k, v in counts.items()
        },
        "rejection_reasons": rejection_reasons,
        "video_total_frames": total_frames,
        "tracking_json_frames": total_tracked_frames,
        "sample_count": len(sample_indices),
        "confirmed_match_threshold_deg": CONFIRMED_MATCH_DEG,
        "hotspot_count": len(hotspots),
        "hotspots": hotspots,
    }

    print("=" * 70)
    print("[DETECTOR AUDIT SUMMARY]")
    for k, v in counts.items():
        pct = round(100.0 * v / max(total_sampled, 1), 1)
        print(f"  {k:30s}: {v:4d}  ({pct}%)")
    print(f"  Rejection reasons: {rejection_reasons}")
    print("=" * 70)

    result = {
        "audit_version": "v1",
        "summary": summary,
        "frames": audit_frames,
    }

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"[done] audit.json → {output_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",        default="equirect_trim.mp4")
    parser.add_argument("--tracking-json", default="tracking.json")
    parser.add_argument("--output",       default="audit.json")
    parser.add_argument("--ball-model",   default="models/football-ball-detection.pt")
    args = parser.parse_args()

    run_audit(
        equirect_path=args.input,
        tracking_json_path=args.tracking_json,
        output_path=args.output,
        ball_model_path=args.ball_model,
    )
