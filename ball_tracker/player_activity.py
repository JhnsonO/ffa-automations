#!/usr/bin/env python3
"""
FFA Player Activity Mapper — Phase 2
=====================================
Runs YOLO person detection across four 90°-spaced equirectangular crops,
back-projects every detected person centroid to spherical (yaw, pitch),
clusters players spatially per frame, tracks cluster dynamics over time,
and emits activity.json with a per-sample active-play centre and confidence.

Fully automatic — no manual frame selection, no per-clip labelling,
no operator calibration required.

Output: activity.json
  {
    "fps": <float>,
    "sample_interval": <int frames>,
    "hotspot_zones": [ {yaw, pitch, radius} ],   # suppressed background
    "frames": [
      {
        "frame":       <int>,
        "players":     [ {yaw, pitch, conf, crop_yaw} ],
        "cluster_centre": {yaw, pitch} | null,
        "cluster_size":   <int>,
        "confidence":  <float 0-1>,
        "dynamic_score": <float>   # motion contribution this sample
      }
    ],
    "summary": {
      "mean_active_yaw": <float>,
      "mean_active_pitch": <float>,
      "frames_with_cluster": <int>,
      "total_sampled": <int>
    }
  }

Usage:
  python player_activity.py --input equirect_trim.mp4 --output activity.json
  python player_activity.py --input equirect_trim.mp4 --output activity.json \\
      --sample-interval 15 --start-frame 700 --end-frame 1300

Env:
  YOLO_MODEL   — path to YOLO weights (default: yolov8n.pt)
  FFMPEG_BIN   — path to ffmpeg (default: /usr/bin/ffmpeg)
"""

import argparse
import json
import math
import os
import subprocess
import sys
from collections import defaultdict

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FFMPEG            = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
YOLO_MODEL_PATH   = os.environ.get("YOLO_MODEL", "yolov8n.pt")

CROP_YAWS_DEG     = [0, 90, 180, 270]
CROP_FOV_DEG      = 110
CROP_W            = 1280
CROP_H            = 720

PERSON_CLASS_ID   = 0          # COCO person class
YOLO_CONF         = 0.25       # higher than ball tracker — persons are easier
YOLO_IMGSZ        = 1280

# Pitch filter — ignore crowd in stands (high pitch) and ground clutter
PITCH_MIN_DEG     = -25.0
PITCH_MAX_DEG     =  20.0

# Deduplication: merge person detections from overlapping crops
DEDUP_RADIUS_DEG  = 8.0        # persons within this angular distance are the same person

# Clustering: DBSCAN-lite — link players within this radius into a cluster
CLUSTER_RADIUS_DEG      = 25.0
CLUSTER_MIN_PLAYERS     = 2    # minimum players to form a valid cluster

# Hotspot suppression (mirrors run_tracker.py logic)
HOTSPOT_SAMPLE_COUNT    = 40
HOTSPOT_CLUSTER_RADIUS  = 5.0
HOTSPOT_MIN_COVERAGE    = 0.45
HOTSPOT_SUPPRESS_RADIUS = 6.0

# Dynamic scoring: weight recent frames by motion relative to prior frame
MOTION_WINDOW           = 3    # look back N samples for centroid shift
MOTION_SCALE_DEG        = 15.0 # expected max centroid shift per window

# Default sampling: process 1 in every N frames
DEFAULT_SAMPLE_INTERVAL = 15   # ~2 samples/sec at 30 fps → fast enough, cheap enough


# ---------------------------------------------------------------------------
# Geometry helpers (matches run_tracker.py exactly)
# ---------------------------------------------------------------------------

def crop_pixel_to_yaw_pitch(px, py, crop_yaw_deg, fov_deg=CROP_FOV_DEG,
                             w=CROP_W, h=CROP_H):
    """Back-project a crop-space pixel to global spherical (yaw, pitch) degrees."""
    nx = (px - w / 2.0) / (w / 2.0)
    ny = (py - h / 2.0) / (h / 2.0)
    f  = 1.0 / math.tan(math.radians(fov_deg / 2.0))
    # Normalised camera ray
    rx = nx / f
    ry = -ny / f * (w / h)
    rz = 1.0
    norm = math.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    # Rotate by crop yaw (yaw-only, no pitch offset in crop)
    cy = math.radians(crop_yaw_deg)
    wx =  math.cos(cy) * rx + math.sin(cy) * rz
    wy =  ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    yaw_rad   = math.atan2(wx, wz)
    pitch_rad = math.asin(max(-1.0, min(1.0, wy)))
    return math.degrees(yaw_rad), math.degrees(pitch_rad)


def angular_distance(y1, p1, y2, p2):
    """Great-circle distance in degrees between two spherical points."""
    dy = math.radians(y1 - y2)
    dp = math.radians(p1 - p2)
    a  = (math.sin(dp / 2) ** 2
          + math.cos(math.radians(p1)) * math.cos(math.radians(p2))
          * math.sin(dy / 2) ** 2)
    return math.degrees(2 * math.asin(math.sqrt(min(1.0, a))))


def yaw_mean(yaws):
    """Circular mean of yaw angles (degrees)."""
    sx = sum(math.cos(math.radians(y)) for y in yaws)
    sy = sum(math.sin(math.radians(y)) for y in yaws)
    return math.degrees(math.atan2(sy, sx))


def spherical_centroid(points):
    """Mean yaw/pitch of a list of (yaw, pitch) pairs."""
    yaws   = [p[0] for p in points]
    pitches = [p[1] for p in points]
    return yaw_mean(yaws), sum(pitches) / len(pitches)


# ---------------------------------------------------------------------------
# Equirectangular → perspective crop  (matches render_segment.py v6)
# ---------------------------------------------------------------------------

def extract_crop_frame(equirect_frame, yaw_deg, fov_deg=CROP_FOV_DEG,
                       out_w=CROP_W, out_h=CROP_H):
    """Pure perspective crop — no pitch, no roll — matches tracker's crop geometry."""
    h_eq, w_eq = equirect_frame.shape[:2]
    f  = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_w / 2.0) / f
    ry = -(yv - out_h / 2.0) / f
    rz = np.ones_like(rx)
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    cy = math.radians(yaw_deg)
    wx =  math.cos(cy) * rx + math.sin(cy) * rz
    wy =  ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    yaw_map   = np.arctan2(wx, wz)
    pitch_map = np.arcsin(np.clip(wy, -1, 1))
    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    return cv2.remap(equirect_frame,
                     map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def dedup_detections(detections, radius_deg=DEDUP_RADIUS_DEG):
    """
    Merge detections that map to the same physical player.
    detections: list of (yaw, pitch, conf, crop_yaw)
    Returns deduplicated list, keeping highest-conf detection in each group.
    """
    kept   = []
    used   = [False] * len(detections)
    # Sort by conf descending so we keep the best
    order  = sorted(range(len(detections)), key=lambda i: -detections[i][2])
    for i in order:
        if used[i]:
            continue
        yaw_i, pitch_i, conf_i, cy_i = detections[i]
        kept.append(detections[i])
        used[i] = True
        for j in range(len(detections)):
            if not used[j]:
                yaw_j, pitch_j, conf_j, cy_j = detections[j]
                if angular_distance(yaw_i, pitch_i, yaw_j, pitch_j) < radius_deg:
                    used[j] = True
    return kept


# ---------------------------------------------------------------------------
# DBSCAN-lite clustering
# ---------------------------------------------------------------------------

def cluster_players(players, radius_deg=CLUSTER_RADIUS_DEG, min_pts=CLUSTER_MIN_PLAYERS):
    """
    Single-linkage grouping.  Returns list of clusters, each a list of player dicts.
    players: list of {yaw, pitch, conf, crop_yaw}
    """
    if not players:
        return []

    n       = len(players)
    cluster = [-1] * n   # -1 = unassigned

    def dist(i, j):
        return angular_distance(players[i]["yaw"], players[i]["pitch"],
                                players[j]["yaw"], players[j]["pitch"])

    cluster_id = 0
    for i in range(n):
        if cluster[i] != -1:
            continue
        neighbours = [j for j in range(n) if i != j and dist(i, j) <= radius_deg]
        if len(neighbours) + 1 < min_pts:
            continue
        cluster[i] = cluster_id
        stack = list(neighbours)
        while stack:
            k = stack.pop()
            if cluster[k] == -1:
                cluster[k] = cluster_id
                nk = [j for j in range(n) if j != k and dist(k, j) <= radius_deg]
                if len(nk) + 1 >= min_pts:
                    stack.extend([j for j in nk if cluster[j] == -1])
        cluster_id += 1

    groups = defaultdict(list)
    for i, cid in enumerate(cluster):
        if cid != -1:
            groups[cid].append(players[i])
    return list(groups.values())


def best_cluster(clusters):
    """Select the most active cluster: largest first, break ties by mean confidence."""
    if not clusters:
        return None
    return max(clusters,
               key=lambda c: (len(c), sum(p["conf"] for p in c) / len(c)))


# ---------------------------------------------------------------------------
# Background hotspot discovery (mirrors run_tracker.py bootstrap logic)
# ---------------------------------------------------------------------------

def discover_hotspots(equirect_path, model, start_frame, end_frame, fps):
    """
    Sample HOTSPOT_SAMPLE_COUNT evenly-spaced frames across [start, end].
    Run person detection on each.  Cluster recurring positions.
    Return list of {yaw, pitch, radius} suppression zones.
    """
    total = max(1, end_frame - start_frame)
    step  = max(1, total // HOTSPOT_SAMPLE_COUNT)
    sample_frames = list(range(start_frame, end_frame, step))[:HOTSPOT_SAMPLE_COUNT]

    all_detections = []  # list of (yaw, pitch) per sampled frame

    cap = cv2.VideoCapture(equirect_path)
    for fidx in sample_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fidx)
        ret, frame = cap.read()
        if not ret:
            continue
        frame_dets = []
        for crop_yaw in CROP_YAWS_DEG:
            crop = extract_crop_frame(frame, crop_yaw)
            results = model(crop, conf=YOLO_CONF, imgsz=YOLO_IMGSZ,
                            classes=[PERSON_CLASS_ID], verbose=False)
            for r in results:
                if r.boxes is None:
                    continue
                for box in r.boxes:
                    cls = int(box.cls[0])
                    if cls != PERSON_CLASS_ID:
                        continue
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    cx, cy_box = (x1 + x2) / 2, (y1 + y2) / 2
                    yaw, pitch = crop_pixel_to_yaw_pitch(cx, cy_box, crop_yaw)
                    if PITCH_MIN_DEG <= pitch <= PITCH_MAX_DEG:
                        frame_dets.append((yaw, pitch))
        all_detections.append(frame_dets)
    cap.release()

    if not all_detections:
        return []

    # Cluster across all sampled frames
    # Count how many sample frames each candidate cluster appears in
    candidate_centres = []
    for dets in all_detections:
        for yaw, pitch in dets:
            candidate_centres.append((yaw, pitch))

    if not candidate_centres:
        return []

    # Simple greedy clustering
    n_samples = len(all_detections)
    merged    = []
    used      = [False] * len(candidate_centres)
    for i, (yi, pi) in enumerate(candidate_centres):
        if used[i]:
            continue
        group = [(yi, pi)]
        used[i] = True
        for j, (yj, pj) in enumerate(candidate_centres):
            if not used[j] and angular_distance(yi, pi, yj, pj) < HOTSPOT_CLUSTER_RADIUS:
                group.append((yj, pj))
                used[j] = True
        cy_mean, cp_mean = spherical_centroid(group)
        # Count how many sample frames this cluster appeared in
        frame_hits = 0
        for dets in all_detections:
            if any(angular_distance(cy_mean, cp_mean, dy, dp) < HOTSPOT_SUPPRESS_RADIUS
                   for dy, dp in dets):
                frame_hits += 1
        coverage = frame_hits / n_samples
        if coverage >= HOTSPOT_MIN_COVERAGE:
            merged.append({"yaw": round(cy_mean, 2), "pitch": round(cp_mean, 2),
                           "radius": HOTSPOT_SUPPRESS_RADIUS, "coverage": round(coverage, 3)})

    print(f"[hotspot] {len(merged)} background zone(s) discovered "
          f"from {n_samples} sampled frames")
    for z in merged:
        print(f"  yaw={z['yaw']:.1f}° pitch={z['pitch']:.1f}° "
              f"coverage={z['coverage']*100:.0f}%")
    return merged


def is_hotspot(yaw, pitch, zones):
    return any(angular_distance(yaw, pitch, z["yaw"], z["pitch"]) < z["radius"]
               for z in zones)


# ---------------------------------------------------------------------------
# Dynamic scoring: how much did the active-play centre shift vs recent frames?
# ---------------------------------------------------------------------------

def dynamic_score(current_centre, history, window=MOTION_WINDOW,
                  scale=MOTION_SCALE_DEG):
    """
    Score 0–1: 0 = no motion, 1 = heavy motion.
    history: list of (yaw, pitch) from recent samples (oldest first).
    """
    if not history or current_centre is None:
        return 0.0
    recent = history[-window:]
    shifts = []
    prev   = recent[0]
    for pt in recent[1:] + [current_centre]:
        d = angular_distance(prev[0], prev[1], pt[0], pt[1])
        shifts.append(d)
        prev = pt
    if not shifts:
        return 0.0
    mean_shift = sum(shifts) / len(shifts)
    return min(1.0, mean_shift / scale)


# ---------------------------------------------------------------------------
# Confidence: blend cluster size, mean detection conf, and motion
# ---------------------------------------------------------------------------

def frame_confidence(players, cluster, cluster_centre, dyn_score, total_players):
    """
    Blend three signals:
      - cluster_ratio: fraction of detected players in the dominant cluster
      - mean_conf:     mean YOLO confidence in the cluster
      - dyn_score:     how much the centre is moving (active play ↑ motion)
    Returns 0–1 float.
    """
    if cluster is None or cluster_centre is None:
        return 0.0
    n_cluster   = len(cluster)
    n_total     = max(1, total_players)
    cluster_ratio = min(1.0, n_cluster / max(1, n_total))
    mean_conf     = sum(p["conf"] for p in cluster) / n_cluster
    # Weighted blend: cluster composition 40%, detection quality 40%, motion 20%
    return round(0.40 * cluster_ratio + 0.40 * mean_conf + 0.20 * dyn_score, 3)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    from ultralytics import YOLO

    print(f"[activity] Loading YOLO model: {args.model}")
    model = YOLO(args.model)

    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        sys.exit(f"[error] Cannot open: {args.input}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps          = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()

    start_frame = max(0, args.start_frame)
    end_frame   = min(total_frames, args.end_frame) if args.end_frame > 0 else total_frames
    interval    = max(1, args.sample_interval)

    print(f"[activity] Input:   {args.input}")
    print(f"[activity] Frames:  {start_frame}–{end_frame} ({end_frame - start_frame} frames)")
    print(f"[activity] FPS:     {fps:.2f}   Interval: every {interval} frames")

    # -----------------------------------------------------------------------
    # Phase A: hotspot discovery
    # -----------------------------------------------------------------------
    print("[activity] === Phase A: background hotspot discovery ===")
    hotspot_zones = discover_hotspots(args.input, model, start_frame, end_frame, fps)

    # -----------------------------------------------------------------------
    # Phase B: per-sample detection + clustering
    # -----------------------------------------------------------------------
    print("[activity] === Phase B: per-sample player detection ===")
    sample_frames = list(range(start_frame, end_frame, interval))
    frame_records = []
    centre_history = []   # list of (yaw, pitch) for dynamic scoring

    cap = cv2.VideoCapture(args.input)
    processed = 0

    for fidx in sample_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fidx)
        ret, frame = cap.read()
        if not ret:
            continue

        # --- Detect persons in all 4 crops ---
        raw_dets = []   # (yaw, pitch, conf, crop_yaw)
        for crop_yaw in CROP_YAWS_DEG:
            crop    = extract_crop_frame(frame, crop_yaw)
            results = model(crop, conf=YOLO_CONF, imgsz=YOLO_IMGSZ,
                            classes=[PERSON_CLASS_ID], verbose=False)
            for r in results:
                if r.boxes is None:
                    continue
                for box in r.boxes:
                    cls = int(box.cls[0])
                    if cls != PERSON_CLASS_ID:
                        continue
                    conf_val = float(box.conf[0])
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    cx = (x1 + x2) / 2
                    cy_box = (y1 + y2) / 2
                    yaw, pitch = crop_pixel_to_yaw_pitch(cx, cy_box, crop_yaw)
                    # Pitch filter
                    if not (PITCH_MIN_DEG <= pitch <= PITCH_MAX_DEG):
                        continue
                    # Hotspot suppression
                    if is_hotspot(yaw, pitch, hotspot_zones):
                        continue
                    raw_dets.append((yaw, pitch, conf_val, crop_yaw))

        # --- Deduplicate ---
        deduped = dedup_detections(raw_dets)
        players = [{"yaw": round(y, 2), "pitch": round(p, 2),
                    "conf": round(c, 3), "crop_yaw": cy}
                   for y, p, c, cy in deduped]

        # --- Cluster ---
        clusters  = cluster_players(players)
        top_cluster = best_cluster(clusters)
        cluster_centre = None
        cluster_size   = 0
        if top_cluster:
            pts = [(pl["yaw"], pl["pitch"]) for pl in top_cluster]
            cy_mean, cp_mean = spherical_centroid(pts)
            cluster_centre = {"yaw": round(cy_mean, 2), "pitch": round(cp_mean, 2)}
            cluster_size   = len(top_cluster)

        # --- Dynamic score ---
        dyn = dynamic_score(
            (cluster_centre["yaw"], cluster_centre["pitch"]) if cluster_centre else None,
            centre_history
        )

        # --- Confidence ---
        conf = frame_confidence(players, top_cluster, cluster_centre, dyn, len(players))

        if cluster_centre:
            centre_history.append((cluster_centre["yaw"], cluster_centre["pitch"]))
            if len(centre_history) > 30:
                centre_history.pop(0)

        frame_records.append({
            "frame":          fidx,
            "players":        players,
            "cluster_centre": cluster_centre,
            "cluster_size":   cluster_size,
            "confidence":     conf,
            "dynamic_score":  round(dyn, 3),
        })

        processed += 1
        if processed % 20 == 0 or processed == len(sample_frames):
            cc_str = (f"yaw={cluster_centre['yaw']:.1f}° pitch={cluster_centre['pitch']:.1f}°"
                      if cluster_centre else "no cluster")
            print(f"  [{processed}/{len(sample_frames)}] frame {fidx:5d} "
                  f"| players={len(players):2d} cluster={cluster_size} | {cc_str} "
                  f"conf={conf:.2f}")

    cap.release()

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    frames_with_cluster = [r for r in frame_records if r["cluster_centre"] is not None]
    if frames_with_cluster:
        mean_yaw   = yaw_mean([r["cluster_centre"]["yaw"]   for r in frames_with_cluster])
        mean_pitch = sum(r["cluster_centre"]["pitch"] for r in frames_with_cluster) / len(frames_with_cluster)
    else:
        mean_yaw   = 0.0
        mean_pitch = 0.0

    summary = {
        "mean_active_yaw":   round(mean_yaw, 2),
        "mean_active_pitch": round(mean_pitch, 2),
        "frames_with_cluster": len(frames_with_cluster),
        "total_sampled":       len(frame_records),
    }

    output = {
        "fps":             fps,
        "sample_interval": interval,
        "start_frame":     start_frame,
        "end_frame":       end_frame,
        "hotspot_zones":   hotspot_zones,
        "frames":          frame_records,
        "summary":         summary,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n[activity] Done. {len(frame_records)} samples written → {args.output}")
    print(f"[activity] Frames with active cluster: {summary['frames_with_cluster']} "
          f"/ {summary['total_sampled']}")
    print(f"[activity] Mean active-play centre: "
          f"yaw={summary['mean_active_yaw']:.1f}° "
          f"pitch={summary['mean_active_pitch']:.1f}°")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FFA Phase 2 — Player Activity Mapper")
    parser.add_argument("--input",           default="equirect_trim.mp4",
                        help="Equirectangular input video")
    parser.add_argument("--output",          default="activity.json",
                        help="Output JSON path")
    parser.add_argument("--model",           default=YOLO_MODEL_PATH,
                        help="YOLO weights path")
    parser.add_argument("--sample-interval", type=int, default=DEFAULT_SAMPLE_INTERVAL,
                        help="Process 1 in every N frames (default: 15)")
    parser.add_argument("--start-frame",     type=int, default=0,
                        help="First frame to process (default: 0)")
    parser.add_argument("--end-frame",       type=int, default=-1,
                        help="Last frame exclusive (-1 = full clip)")
    args = parser.parse_args()
    run(args)
