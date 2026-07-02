#!/usr/bin/env python3
"""
playcam/play_location.py

Phase 1 — play-location measurement layer.

Samples an equirectangular 360 clip at a low frame rate, detects people
across four fixed-yaw rectilinear crops (not on the raw distorted 360
frame), back-projects detections to spherical (yaw, pitch) coordinates,
clusters them, and writes a timeline of the dominant active-play area.

This is a MEASUREMENT layer only:
  - no Kalman smoothing
  - no camera rendering
  - no homography
  - no ball detection or ball_tracker/ imports
  - no coupling to ball_tracker/ — geometry helpers below are a standalone
    reimplementation of the same proven math used elsewhere in this repo
    (crop/back-projection identical in form to ball_tracker/player_activity.py),
    kept separate so playcam/ has zero import dependency on ball_tracker/.

Output: playcam/output/play_location.jsonl (one JSON object per sampled frame)
Debug:  playcam/output/debug_frames/frame_<idx>.png (equirect-space plot of
        detections, chosen cluster, and target yaw/pitch)

Usage:
  python3 play_location.py --input clip.mp4
  python3 play_location.py --input clip.mp4 --fps 3 --start 30 --duration 20

Env:
  YOLO_PERSON_WEIGHTS  — person detector weights (default: yolov8n.pt)
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

YOLO_PERSON_WEIGHTS = os.environ.get("YOLO_PERSON_WEIGHTS", "yolov8n.pt")

CROP_YAWS_DEG = [0, 90, 180, 270]
CROP_FOV_DEG = 110
CROP_W = 1280
CROP_H = 720

PERSON_CLASS_ID = 0  # COCO class 0
YOLO_CONF = 0.25
YOLO_IMGSZ = 1280

# Exclude crowd in stands / sky / ground clutter outside plausible play height
PITCH_MIN_DEG = -25.0
PITCH_MAX_DEG = 55.0

# Merge duplicate detections of the same person across overlapping crop seams
DEDUP_RADIUS_DEG = 8.0

# Clustering — dominant area of play
CLUSTER_RADIUS_DEG = 25.0
CLUSTER_MIN_PLAYERS = 2

DEFAULT_SAMPLE_FPS = 3.0
DEFAULT_MAX_FRAMES = 200  # safety cap so a bad --duration doesn't run forever


# ---------------------------------------------------------------------------
# Geometry — equirect crop extraction and spherical back-projection
# (standalone reimplementation; same math as elsewhere in this repo)
# ---------------------------------------------------------------------------

def extract_crop_frame(equirect_frame, yaw_deg, fov_deg=CROP_FOV_DEG,
                        out_w=CROP_W, out_h=CROP_H):
    """Pure yaw-only perspective crop from an equirectangular frame."""
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
    cy = math.radians(yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    yaw_map = np.arctan2(wx, wz)
    pitch_map = np.arcsin(np.clip(wy, -1, 1))
    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    return cv2.remap(equirect_frame,
                      map_x.astype(np.float32), map_y.astype(np.float32),
                      interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def crop_pixel_to_yaw_pitch(px, py, crop_yaw_deg,
                             fov_deg=CROP_FOV_DEG, w=CROP_W, h=CROP_H):
    """Back-project a crop pixel to global spherical (yaw deg, pitch deg)."""
    nx = (px - w / 2.0) / (w / 2.0)
    ny = (py - h / 2.0) / (h / 2.0)
    f = 1.0 / math.tan(math.radians(fov_deg / 2.0))
    rx = nx / f
    ry = -ny / f * (w / h)
    rz = 1.0
    norm = math.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    cy = math.radians(crop_yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    return (math.degrees(math.atan2(wx, wz)),
            math.degrees(math.asin(max(-1.0, min(1.0, wy)))))


def angular_distance(y1, p1, y2, p2):
    """Great-circle distance in degrees between two (yaw, pitch) points."""
    dy = math.radians(y1 - y2)
    dp = math.radians(p1 - p2)
    a = (math.sin(dp / 2) ** 2
         + math.cos(math.radians(p1)) * math.cos(math.radians(p2))
         * math.sin(dy / 2) ** 2)
    return math.degrees(2 * math.asin(math.sqrt(min(1.0, a))))


def yaw_mean(yaws):
    """Circular mean of yaw angles (handles the -180/180 wraparound)."""
    sx = sum(math.cos(math.radians(y)) for y in yaws)
    sy = sum(math.sin(math.radians(y)) for y in yaws)
    return math.degrees(math.atan2(sy, sx))


def spherical_centroid(points):
    return yaw_mean([p[0] for p in points]), sum(p[1] for p in points) / len(points)


# ---------------------------------------------------------------------------
# Dedup + clustering
# ---------------------------------------------------------------------------

def dedup_players(players, radius_deg=DEDUP_RADIUS_DEG):
    """Merge detections of the same physical person seen in overlapping crops."""
    if not players:
        return []
    order = sorted(range(len(players)), key=lambda i: -players[i]["conf"])
    used = [False] * len(players)
    kept = []
    for i in order:
        if used[i]:
            continue
        kept.append(players[i])
        used[i] = True
        for j in range(len(players)):
            if not used[j]:
                if angular_distance(players[i]["yaw"], players[i]["pitch"],
                                     players[j]["yaw"], players[j]["pitch"]) < radius_deg:
                    used[j] = True
    return kept


def cluster_players(players, radius_deg=CLUSTER_RADIUS_DEG, min_pts=CLUSTER_MIN_PLAYERS):
    """DBSCAN-lite clustering in spherical space."""
    if not players:
        return []
    n = len(players)
    cluster = [-1] * n

    def dist(i, j):
        return angular_distance(players[i]["yaw"], players[i]["pitch"],
                                 players[j]["yaw"], players[j]["pitch"])

    cid = 0
    for i in range(n):
        if cluster[i] != -1:
            continue
        nbrs = [j for j in range(n) if i != j and dist(i, j) <= radius_deg]
        if len(nbrs) + 1 < min_pts:
            continue
        cluster[i] = cid
        stack = list(nbrs)
        while stack:
            k = stack.pop()
            if cluster[k] == -1:
                cluster[k] = cid
                nk = [j for j in range(n) if j != k and dist(k, j) <= radius_deg]
                if len(nk) + 1 >= min_pts:
                    stack.extend(j for j in nk if cluster[j] == -1)
        cid += 1

    groups = defaultdict(list)
    for i, c in enumerate(cluster):
        if c != -1:
            groups[c].append(players[i])
    return list(groups.values())


def best_cluster(clusters):
    """Dominant area of play = largest cluster, tie-broken by mean confidence."""
    if not clusters:
        return None
    return max(clusters, key=lambda c: (len(c), sum(p["conf"] for p in c) / len(c)))


# ---------------------------------------------------------------------------
# Debug preview
# ---------------------------------------------------------------------------

def render_debug_frame(equirect_frame, players, cluster, target, out_path,
                        thumb_w=960):
    """
    Draw a downscaled equirect thumbnail with:
      - a dot per detected player (yaw/pitch -> equirect x/y)
      - cluster members highlighted
      - crosshair at the chosen target yaw/pitch
    """
    h_eq, w_eq = equirect_frame.shape[:2]
    thumb_h = int(thumb_w * h_eq / w_eq)
    thumb = cv2.resize(equirect_frame, (thumb_w, thumb_h))

    def yaw_pitch_to_xy(yaw, pitch):
        x = int(((yaw / 360.0) + 0.5) * thumb_w) % thumb_w
        y = int((0.5 - pitch / 180.0) * thumb_h)
        y = max(0, min(thumb_h - 1, y))
        return x, y

    cluster_set = {id(p) for p in cluster} if cluster else set()
    for p in players:
        x, y = yaw_pitch_to_xy(p["yaw"], p["pitch"])
        color = (0, 255, 0) if id(p) in cluster_set else (0, 165, 255)
        cv2.circle(thumb, (x, y), 5, color, -1)

    if target is not None:
        tx, ty = yaw_pitch_to_xy(target["yaw"], target["pitch"])
        cv2.drawMarker(thumb, (tx, ty), (0, 0, 255),
                        markerType=cv2.MARKER_CROSS, markerSize=24, thickness=2)
        cv2.putText(thumb, f"target yaw={target['yaw']:.1f} pitch={target['pitch']:.1f}",
                    (10, thumb_h - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    cv2.putText(thumb, f"players={len(players)} cluster={len(cluster) if cluster else 0}",
                (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

    cv2.imwrite(str(out_path), thumb)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Phase 1 — play-location measurement layer")
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--output", type=Path, default=Path("playcam/output/play_location.jsonl"))
    p.add_argument("--debug-dir", type=Path, default=Path("playcam/output/debug_frames"))
    p.add_argument("--fps", type=float, default=DEFAULT_SAMPLE_FPS,
                    help=f"Sample rate in frames/sec (default {DEFAULT_SAMPLE_FPS})")
    p.add_argument("--start", type=float, default=0.0, help="Start time in seconds")
    p.add_argument("--duration", type=float, default=None, help="Duration in seconds (default: full clip)")
    p.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES,
                    help=f"Safety cap on sampled frames (default {DEFAULT_MAX_FRAMES})")
    p.add_argument("--model", default=YOLO_PERSON_WEIGHTS)
    p.add_argument("--no-debug", action="store_true", help="Skip debug frame PNGs")
    return p.parse_args()


def main():
    args = parse_args()

    if not args.input.exists():
        print(f"ERROR: input file does not exist: {args.input}", file=sys.stderr)
        sys.exit(1)
    if args.fps <= 0:
        print(f"ERROR: --fps must be > 0, got {args.fps}", file=sys.stderr)
        sys.exit(1)

    from ultralytics import YOLO
    model = YOLO(args.model)

    cap = cv2.VideoCapture(str(args.input))
    if not cap.isOpened():
        print(f"ERROR: cannot open input: {args.input}", file=sys.stderr)
        sys.exit(1)

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    start_frame = int(args.start * src_fps)
    if args.duration is not None:
        end_frame = min(total_frames, start_frame + int(args.duration * src_fps))
    else:
        end_frame = total_frames

    step = max(1, round(src_fps / args.fps))
    sample_frames = list(range(start_frame, end_frame, step))[:args.max_frames]

    print(f"[play_location] input={args.input} src_fps={src_fps:.2f} "
          f"total_frames={total_frames}")
    print(f"[play_location] sampling {len(sample_frames)} frames "
          f"(every {step} frames, ~{args.fps} fps effective)")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not args.no_debug:
        args.debug_dir.mkdir(parents=True, exist_ok=True)

    records = []
    if start_frame > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)  # one seek, not per-sample
    with open(args.output, "w") as out_f:
        sample_set = set(sample_frames)
        i = 0
        for fidx in range(start_frame, end_frame):
            ret, frame = cap.read()
            if not ret:
                print(f"  [warn] could not read frame {fidx}, stopping early")
                break
            if fidx not in sample_set:
                continue
            i += 1

            all_players = []
            for crop_yaw in CROP_YAWS_DEG:
                crop = extract_crop_frame(frame, crop_yaw)
                results = model(crop, conf=YOLO_CONF, imgsz=YOLO_IMGSZ,
                                 classes=[PERSON_CLASS_ID], verbose=False)
                for r in results:
                    if r.boxes is None:
                        continue
                    for box in r.boxes:
                        x1, y1, x2, y2 = box.xyxy[0].tolist()
                        conf_val = float(box.conf[0])
                        yaw, pitch = crop_pixel_to_yaw_pitch((x1 + x2) / 2, (y1 + y2) / 2, crop_yaw)
                        if PITCH_MIN_DEG <= pitch <= PITCH_MAX_DEG:
                            all_players.append({
                                "yaw": round(yaw, 2),
                                "pitch": round(pitch, 2),
                                "conf": round(conf_val, 3),
                            })

            players = dedup_players(all_players)
            clusters = cluster_players(players)
            top_cluster = best_cluster(clusters)

            target = None
            if top_cluster:
                cy_m, cp_m = spherical_centroid([(p["yaw"], p["pitch"]) for p in top_cluster])
                target = {"yaw": round(cy_m, 2), "pitch": round(cp_m, 2)}

            record = {
                "timestamp": round(fidx / src_fps, 3),
                "frame": fidx,
                "players": players,
                "cluster_size": len(top_cluster) if top_cluster else 0,
                "target_yaw": target["yaw"] if target else None,
                "target_pitch": target["pitch"] if target else None,
            }
            records.append(record)
            out_f.write(json.dumps(record) + "\n")

            if not args.no_debug:
                debug_path = args.debug_dir / f"frame_{fidx:06d}.png"
                render_debug_frame(frame, players, top_cluster, target, debug_path)

            if i % 10 == 0 or i == len(sample_frames):
                t_str = (f"target yaw={target['yaw']:.1f} pitch={target['pitch']:.1f}"
                          if target else "no cluster")
                print(f"  [{i:3d}/{len(sample_frames)}] frame {fidx:5d} "
                      f"players={len(players):2d} cluster={record['cluster_size']:2d} | {t_str}")

    cap.release()

    with_cluster = [r for r in records if r["target_yaw"] is not None]
    print(f"\n[play_location] Done. {len(records)} samples -> {args.output}")
    print(f"[play_location] Frames with a dominant cluster: "
          f"{len(with_cluster)} / {len(records)}")
    if with_cluster:
        mean_yaw = yaw_mean([r["target_yaw"] for r in with_cluster])
        mean_pitch = sum(r["target_pitch"] for r in with_cluster) / len(with_cluster)
        print(f"[play_location] Mean dominant-area centre: "
              f"yaw={mean_yaw:.1f} pitch={mean_pitch:.1f}")
    if not args.no_debug:
        print(f"[play_location] Debug frames written to: {args.debug_dir}")


if __name__ == "__main__":
    main()
