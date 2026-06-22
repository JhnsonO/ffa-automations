#!/usr/bin/env python3
"""
FFA Track B — Detector Audit
==============================
Runs the YOLO ball detector on each frame in track_b_manifest.json.
Classifies each frame from observable evidence only — no tracking.json required.

Classification labels (produced by this audit, not inferred from tracker output):
  BALL_CONFIDENT    — at least one candidate with weighted_conf >= HIGH_CONF_THRESH,
                       not hotspot-suppressed, within pitch bounds
  BALL_WEAK         — at least one candidate passes filters but weighted_conf < HIGH_CONF_THRESH
  HOTSPOT_ONLY      — all candidates are hotspot-suppressed (none pass)
  PITCH_FILTERED    — all candidates rejected by pitch hard bounds (none hotspot)
  NOT_DETECTED      — YOLO produced zero raw candidates
  AMBIGUOUS         — multiple candidates, none clearly dominant (max conf spread < 0.15)

Per-candidate annotations:
  pitch_rejected    — outside [PITCH_MIN, PITCH_MAX]
  hotspot_rejected  — inside a hotspot region (penalty < HOTSPOT_HARD_THRESH)
  weighted_conf     — raw_conf * hotspot_penalty_from_manifest
  dist_to_top_s1    — angular distance to the top Stage 1 candidate for this frame

Outputs: track_b_audit.json
  {
    "audit_version": "track_b_v1",
    "summary": { counts, pcts, difficulty_breakdown },
    "frames": [ per-frame records ]
  }
"""

import argparse
import json
import math
import os
import time

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Config
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

PITCH_MIN_DEG    = -30.0
PITCH_MAX_DEG    =  18.0

# Hotspot penalty below this → candidate is suppressed for classification purposes
HOTSPOT_HARD_THRESH = 0.5

# Weighted conf threshold for BALL_CONFIDENT vs BALL_WEAK
HIGH_CONF_THRESH = 0.35

# Ambiguity: if top two candidates within this of each other, flag AMBIGUOUS
AMBIGUITY_SPREAD = 0.15


# ---------------------------------------------------------------------------
# Geometry (verbatim from stage1_candidate_gen.py)
# ---------------------------------------------------------------------------
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
    """detections: list of (yaw, pitch, conf, crop_yaw, area)"""
    kept = []
    for det in sorted(detections, key=lambda d: -d[2]):
        yaw, pitch = det[0], det[1]
        if all(angular_distance(yaw, pitch, k[0], k[1]) > thresh_deg for k in kept):
            kept.append(det)
    return kept


# ---------------------------------------------------------------------------
# Hotspot penalty lookup (re-implements Stage 1 region-distance penalty)
# ---------------------------------------------------------------------------
def load_hotspot_map(path):
    if not path or not os.path.isfile(path):
        return None, {}
    with open(path) as f:
        hm = json.load(f)
    bin_lookup = {}
    for b in hm.get("bins", []):
        key = (b["yaw_bin"], b["pitch_bin"])
        bin_lookup[key] = b["penalty_weight"]
    return hm, bin_lookup


def bin_id_for(yaw, pitch, bin_deg):
    yaw_w = ((yaw + 180.0) % 360.0) - 180.0
    yb = int(math.floor((yaw_w + 180.0) / bin_deg))
    pb = int(math.floor((pitch + 90.0) / bin_deg))
    return (yb, pb)


def penalty_weight(yaw, pitch, hm, bin_lookup):
    if hm is None:
        return 1.0
    bin_deg   = hm["sphere_bin_deg"]
    low_floor = hm["low_duty_floor"]
    threshold = hm["duty_cycle_threshold"]
    pmin      = hm["penalty_min"]

    b     = bin_id_for(yaw, pitch, bin_deg)
    bin_w = bin_lookup.get(b, 1.0)

    region_w = 1.0
    for r in hm.get("hotspot_regions", []):
        d    = angular_distance(yaw, pitch, r["centre_yaw"], r["centre_pitch"])
        core = r["radius_deg"]
        band = core
        if d <= core:
            eff_duty = r["peak_duty"]
        elif d <= core + band:
            frac     = 1.0 - (d - core) / band
            eff_duty = r["peak_duty"] * frac
        else:
            continue
        if eff_duty < low_floor:
            w = 1.0
        elif eff_duty >= threshold:
            w = pmin
        else:
            frac2 = (eff_duty - low_floor) / (threshold - low_floor)
            w = pmin + (1.0 - pmin) * 0.5 * (1.0 + math.cos(math.pi * frac2))
        if w < region_w:
            region_w = w

    return max(pmin, min(bin_w, region_w))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify_frame(audit_cands):
    """
    audit_cands: list of dicts with pitch_rejected, hotspot_rejected, weighted_conf
    Returns (classification_label, detail_str)
    """
    if not audit_cands:
        return "NOT_DETECTED", None

    passing = [c for c in audit_cands
               if not c["pitch_rejected"] and not c["hotspot_rejected"]]

    if not passing:
        # All rejected — why?
        all_hotspot = all(c["hotspot_rejected"] and not c["pitch_rejected"]
                          for c in audit_cands)
        all_pitch   = all(c["pitch_rejected"] and not c["hotspot_rejected"]
                          for c in audit_cands)
        if all_hotspot:
            return "HOTSPOT_ONLY", None
        if all_pitch:
            return "PITCH_FILTERED", None
        return "HOTSPOT_ONLY", "mixed_rejection"   # some hotspot, some pitch

    # Passing candidates exist
    passing_sorted = sorted(passing, key=lambda c: -c["weighted_conf"])
    top_wc = passing_sorted[0]["weighted_conf"]

    # Ambiguity check
    if len(passing_sorted) >= 2:
        second_wc = passing_sorted[1]["weighted_conf"]
        if (top_wc - second_wc) < AMBIGUITY_SPREAD and top_wc < HIGH_CONF_THRESH:
            return "AMBIGUOUS", f"top={top_wc:.3f} second={second_wc:.3f}"

    if top_wc >= HIGH_CONF_THRESH:
        return "BALL_CONFIDENT", f"wc={top_wc:.3f}"
    return "BALL_WEAK", f"wc={top_wc:.3f}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def run_audit(equirect_path, manifest_path, hotspot_map_path,
              ball_model_path, output_path):
    t0 = time.time()

    print(f"[audit] Loading manifest: {manifest_path}")
    with open(manifest_path) as f:
        manifest = json.load(f)

    sample_frames = manifest["frames"]
    fps           = manifest["fps"]
    print(f"[audit] {len(sample_frames)} frames to audit  fps={fps:.2f}")

    print(f"[audit] Loading hotspot map: {hotspot_map_path or 'none'}")
    hm, bin_lookup = load_hotspot_map(hotspot_map_path)

    print(f"[audit] Loading YOLO model: {ball_model_path}")
    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"[audit] Device: {device}")
    except ImportError:
        device = "cpu"

    from ultralytics import YOLO
    model = YOLO(ball_model_path)
    model.to(device)

    os.environ["OPENCV_FFMPEG_READ_ATTEMPTS"] = "65536"
    cap = cv2.VideoCapture(equirect_path, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open {equirect_path}")
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[audit] Video: {total_frames} frames")

    # ---------------------------------------------------------------------------
    audit_records = []
    counts = {
        "BALL_CONFIDENT": 0,
        "BALL_WEAK":      0,
        "AMBIGUOUS":      0,
        "HOTSPOT_ONLY":   0,
        "PITCH_FILTERED": 0,
        "NOT_DETECTED":   0,
    }

    prev_fi = -1
    for rec_i, mrec in enumerate(sample_frames):
        fi = mrec["frame_idx"]

        if fi != prev_fi + 1:
            cap.set(cv2.CAP_PROP_POS_FRAMES, fi)
        ret, frame = cap.read()
        if not ret:
            print(f"  [frame {fi}] SKIP — read failed")
            prev_fi = fi
            continue
        prev_fi = fi

        # Stage 1 reference (from manifest)
        s1_top_yaw   = mrec.get("top_yaw")
        s1_top_pitch = mrec.get("top_pitch")
        s1_top_wc    = mrec.get("top_weighted_conf")
        s1_n         = mrec.get("stage1_candidate_count", 0)

        # Run YOLO on all 4 crops
        raw_dets = []
        for crop_yaw in CROP_YAWS_DEG:
            crop = extract_crop_frame(frame, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
            res  = model(crop, imgsz=YOLO_IMGSZ, conf=YOLO_CONF,
                         verbose=False, classes=[BALL_CLASS_ID], device=device)
            for box in res[0].boxes:
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                px, py = (x1 + x2) / 2, (y1 + y2) / 2
                conf   = float(box.conf[0])
                area   = (x2 - x1) * (y2 - y1)
                yaw_d, pitch_d = crop_pixel_to_yaw_pitch(
                    px, py, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                raw_dets.append((yaw_d, pitch_d, conf, crop_yaw, area))

        deduped = dedupe_detections(raw_dets)

        # Annotate each deduplicated candidate
        audit_cands = []
        for (yaw_d, pitch_d, conf_d, crop_yaw, area) in deduped:
            pen            = penalty_weight(yaw_d, pitch_d, hm, bin_lookup)
            wc             = conf_d * pen
            pitch_rej      = pitch_d < PITCH_MIN_DEG or pitch_d > PITCH_MAX_DEG
            hotspot_rej    = pen < HOTSPOT_HARD_THRESH

            dist_to_s1 = None
            if s1_top_yaw is not None and s1_top_pitch is not None:
                dist_to_s1 = round(angular_distance(yaw_d, pitch_d,
                                                     s1_top_yaw, s1_top_pitch), 2)

            audit_cands.append({
                "yaw":             round(yaw_d, 2),
                "pitch":           round(pitch_d, 2),
                "raw_conf":        round(conf_d, 3),
                "penalty":         round(pen, 4),
                "weighted_conf":   round(wc, 4),
                "area":            round(area, 1),
                "crop_yaw":        crop_yaw,
                "pitch_rejected":  pitch_rej,
                "hotspot_rejected": hotspot_rej,
                "dist_to_s1_top":  dist_to_s1,
            })

        classification, detail = classify_frame(audit_cands)
        counts[classification] = counts.get(classification, 0) + 1

        audit_records.append({
            "frame_idx":          fi,
            "timestamp_s":        round(fi / fps, 3) if fps else None,
            "strata":             mrec.get("strata", []),
            "classification":     classification,
            "classification_detail": detail,
            "raw_det_count":      len(raw_dets),
            "deduped_count":      len(deduped),
            "audit_candidates":   audit_cands,
            "s1_candidate_count": s1_n,
            "s1_top_weighted_conf": s1_top_wc,
            "s1_top_yaw":         s1_top_yaw,
            "s1_top_pitch":       s1_top_pitch,
        })

        if rec_i % 10 == 0 or rec_i == len(sample_frames) - 1:
            elapsed = time.time() - t0
            print(f"  [{rec_i+1}/{len(sample_frames)}] frame={fi} "
                  f"raw={len(raw_dets)} deduped={len(deduped)} "
                  f"class={classification}  {elapsed:.1f}s")

    cap.release()

    # ---------------------------------------------------------------------------
    # Summary
    # ---------------------------------------------------------------------------
    total_audited = len(audit_records)

    # Difficulty breakdown by stratum
    stratum_difficulty = {}
    for rec in audit_records:
        cls = rec["classification"]
        for lbl in rec.get("strata", []):
            if lbl not in stratum_difficulty:
                stratum_difficulty[lbl] = {}
            stratum_difficulty[lbl][cls] = stratum_difficulty[lbl].get(cls, 0) + 1

    summary = {
        "total_audited": total_audited,
        "counts":        counts,
        "pct":           {k: round(100.0 * v / max(total_audited, 1), 1)
                          for k, v in counts.items()},
        "stratum_difficulty": stratum_difficulty,
        "hotspot_map_used": hm is not None,
        "elapsed_s":     round(time.time() - t0, 1),
    }

    print("=" * 70)
    print("[TRACK B AUDIT SUMMARY]")
    for k, v in counts.items():
        pct = round(100.0 * v / max(total_audited, 1), 1)
        print(f"  {k:20s}: {v:4d}  ({pct}%)")
    print("=" * 70)

    result = {
        "audit_version": "track_b_v1",
        "summary":       summary,
        "frames":        audit_records,
    }

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)
    print(f"[done] {output_path}  ({os.path.getsize(output_path)//1024}KB)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--equirect",     required=True, help="equirect_trim.mp4")
    ap.add_argument("--manifest",     required=True, help="track_b_manifest.json")
    ap.add_argument("--hotspot-map",  default="",    help="hotspot_map.json (optional)")
    ap.add_argument("--ball-model",   required=True)
    ap.add_argument("--output",       default="track_b_audit.json")
    args = ap.parse_args()

    run_audit(
        equirect_path=args.equirect,
        manifest_path=args.manifest,
        hotspot_map_path=args.hotspot_map or None,
        ball_model_path=args.ball_model,
        output_path=args.output,
    )
