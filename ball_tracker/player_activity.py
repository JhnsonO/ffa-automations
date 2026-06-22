#!/usr/bin/env python3
"""
FFA Player Activity Mapper — Phase 2 v2
=========================================
Uses four independent Ultralytics ByteTrack instances (one per perspective
crop at yaw 0/90/180/270) to detect and track players across frames.

Key differences from v1:
- model.track(persist=True, tracker="bytetrack.yaml") per crop, not model()
- track velocity derived from box.id continuity, not centroid-shift heuristic
- convergence score from track velocity vectors pointing toward cluster centre
- YOLO_PERSON_WEIGHTS env var controls person model (default: yolov8n.pt)
- IDs are crop-local; no cross-crop identity handoff

Preserved from v1:
- extract_crop_frame geometry (identical to render_segment.py v6)
- crop_pixel_to_yaw_pitch back-projection (identical to run_tracker.py)
- spatial deduplication (8° angular radius)
- hotspot suppression (bootstrap discovery, mirrors run_tracker.py)
- DBSCAN-lite clustering (25° radius, min 2 players)
- activity.json schema (fully backward-compatible; adds track_velocity,
  convergence_score fields per player where available)
- confidence blend (cluster ratio 40%, mean YOLO conf 40%, motion 20%)

Output: activity.json
  {
    "fps": <float>,
    "sample_interval": <int>,
    "hotspot_zones": [{yaw, pitch, radius, coverage}],
    "frames": [
      {
        "frame": <int>,
        "players": [{yaw, pitch, conf, crop_yaw, track_id,
                     vel_yaw, vel_pitch, vel_mag}],
        "cluster_centre": {yaw, pitch} | null,
        "cluster_size": <int>,
        "confidence": <float 0-1>,
        "motion_score": <float>,      # replaces dynamic_score; from track velocities
        "convergence_score": <float>  # fraction of tracks moving toward centre
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
  python player_activity.py --input equirect_trim.mp4 --output activity.json \
      --sample-interval 15 --start-frame 700 --end-frame 1300

Env:
  YOLO_PERSON_WEIGHTS  — person detector weights (default: yolov8n.pt)
  YOLO_MODEL           — alias for YOLO_PERSON_WEIGHTS (legacy compat)
  FFMPEG_BIN           — path to ffmpeg (default: /usr/bin/ffmpeg)
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FFMPEG = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")

# Person weights: YOLO_PERSON_WEIGHTS takes precedence; fall back to YOLO_MODEL
# for backward compat with the existing workflow, then to yolov8n.pt.
YOLO_PERSON_WEIGHTS = (
    os.environ.get("YOLO_PERSON_WEIGHTS")
    or os.environ.get("YOLO_MODEL")
    or "yolov8n.pt"
)

CROP_YAWS_DEG = [0, 90, 180, 270]
CROP_FOV_DEG  = 110
CROP_W        = 1280
CROP_H        = 720

PERSON_CLASS_ID = 0     # COCO class 0
YOLO_CONF       = 0.25
YOLO_IMGSZ      = 1280

# Pitch filter — exclude crowd in stands and ground clutter
PITCH_MIN_DEG = -25.0
PITCH_MAX_DEG = 55.0

# Deduplication across crop-overlap zones (~20° per seam at 110° FOV / 90° spacing)
DEDUP_RADIUS_DEG = 8.0

# Clustering
CLUSTER_RADIUS_DEG  = 25.0
CLUSTER_MIN_PLAYERS = 2

# Hotspot suppression (mirrors run_tracker.py bootstrap)
HOTSPOT_SAMPLE_COUNT    = 40
HOTSPOT_CLUSTER_RADIUS  = 5.0
HOTSPOT_MIN_COVERAGE    = 0.45
HOTSPOT_SUPPRESS_RADIUS = 6.0

# Motion/convergence: velocity is measured in degrees/frame in spherical space
# Tracks with velocity magnitude below this are considered stationary
VEL_MIN_DEG_PER_FRAME = 0.3
# Convergence: dot-product threshold for "moving toward centre"
CONVERGENCE_DOT_THRESH = 0.3

DEFAULT_SAMPLE_INTERVAL = 15   # process 1 in N frames (~2/sec at 30fps)


# ---------------------------------------------------------------------------
# Geometry helpers — identical to run_tracker.py and render_segment.py v6
# ---------------------------------------------------------------------------

def extract_crop_frame(equirect_frame, yaw_deg, fov_deg=CROP_FOV_DEG,
                       out_w=CROP_W, out_h=CROP_H):
    """Pure yaw-only perspective crop. Matches tracker crop geometry exactly."""
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


def crop_pixel_to_yaw_pitch(px, py, crop_yaw_deg,
                              fov_deg=CROP_FOV_DEG, w=CROP_W, h=CROP_H):
    """Back-project crop pixel → global spherical (yaw°, pitch°)."""
    nx = (px - w / 2.0) / (w / 2.0)
    ny = (py - h / 2.0) / (h / 2.0)
    f  = 1.0 / math.tan(math.radians(fov_deg / 2.0))
    rx = nx / f
    ry = -ny / f * (w / h)
    rz = 1.0
    norm = math.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    cy = math.radians(crop_yaw_deg)
    wx =  math.cos(cy) * rx + math.sin(cy) * rz
    wy =  ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    return math.degrees(math.atan2(wx, wz)), math.degrees(math.asin(max(-1.0, min(1.0, wy))))


def angular_distance(y1, p1, y2, p2):
    """Great-circle distance in degrees."""
    dy = math.radians(y1 - y2)
    dp = math.radians(p1 - p2)
    a  = (math.sin(dp / 2) ** 2
          + math.cos(math.radians(p1)) * math.cos(math.radians(p2))
          * math.sin(dy / 2) ** 2)
    return math.degrees(2 * math.asin(math.sqrt(min(1.0, a))))


def yaw_mean(yaws):
    """Circular mean of yaw angles."""
    sx = sum(math.cos(math.radians(y)) for y in yaws)
    sy = sum(math.sin(math.radians(y)) for y in yaws)
    return math.degrees(math.atan2(sy, sx))


def spherical_centroid(points):
    """Mean (yaw, pitch) of a list of (yaw, pitch) pairs."""
    return yaw_mean([p[0] for p in points]), sum(p[1] for p in points) / len(points)


# ---------------------------------------------------------------------------
# Deduplication (preserved from v1)
# ---------------------------------------------------------------------------

def dedup_players(players, radius_deg=DEDUP_RADIUS_DEG):
    """
    Merge players from overlapping crops that map to the same physical person.
    Input:  list of player dicts with yaw/pitch/conf
    Output: deduplicated list, keeping highest-conf entry per group.
    """
    if not players:
        return []
    order = sorted(range(len(players)), key=lambda i: -players[i]["conf"])
    used  = [False] * len(players)
    kept  = []
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


# ---------------------------------------------------------------------------
# DBSCAN-lite clustering (preserved from v1)
# ---------------------------------------------------------------------------

def cluster_players(players, radius_deg=CLUSTER_RADIUS_DEG,
                    min_pts=CLUSTER_MIN_PLAYERS):
    if not players:
        return []
    n       = len(players)
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
    if not clusters:
        return None
    return max(clusters,
               key=lambda c: (len(c), sum(p["conf"] for p in c) / len(c)))


# ---------------------------------------------------------------------------
# Track-derived motion and convergence  (replaces dynamic_score from v1)
# ---------------------------------------------------------------------------

def motion_score_from_tracks(players):
    """
    Mean velocity magnitude of all players with a measured velocity,
    normalised to [0, 1]. Players without a prior position have vel_mag=0.
    Scale: 3°/frame ≈ full score (fast attacking run across the pitch view).
    """
    mags = [p.get("vel_mag", 0.0) for p in players]
    if not mags:
        return 0.0
    return min(1.0, sum(mags) / len(mags) / 3.0)


def convergence_score_from_tracks(players, centre):
    """
    Fraction of moving tracks whose velocity vector points toward the
    cluster centre (dot product of normalised velocity and direction-to-centre
    exceeds CONVERGENCE_DOT_THRESH).

    A high convergence score means players are closing in on the ball area —
    a strong indicator that this is where active play is.
    """
    if centre is None:
        return 0.0
    moving = [p for p in players
              if p.get("vel_mag", 0.0) >= VEL_MIN_DEG_PER_FRAME]
    if not moving:
        return 0.0
    converging = 0
    for p in moving:
        # Vector from player to cluster centre in spherical approx (small angles ok)
        to_cx = math.cos(math.radians(centre["pitch"])) * math.sin(
            math.radians(centre["yaw"] - p["yaw"]))
        to_cy = math.sin(math.radians(centre["pitch"] - p["pitch"]))
        to_c  = math.sqrt(to_cx**2 + to_cy**2)
        if to_c < 1e-6:
            continue
        to_cx /= to_c
        to_cy /= to_c
        # Velocity unit vector
        vy = p.get("vel_yaw", 0.0)
        vp = p.get("vel_pitch", 0.0)
        vm = p.get("vel_mag", 1.0)
        if vm < 1e-6:
            continue
        dot = (vy / vm) * to_cx + (vp / vm) * to_cy
        if dot >= CONVERGENCE_DOT_THRESH:
            converging += 1
    return round(converging / len(moving), 3)


# ---------------------------------------------------------------------------
# Confidence blend (same weights as v1, motion source changed)
# ---------------------------------------------------------------------------

def frame_confidence(cluster, total_players, mot_score, conv_score):
    if not cluster:
        return 0.0
    cluster_ratio = min(1.0, len(cluster) / max(1, total_players))
    mean_conf     = sum(p["conf"] for p in cluster) / len(cluster)
    # motion signal: blend velocity magnitude (60%) and convergence (40%)
    motion_signal = 0.60 * mot_score + 0.40 * conv_score
    return round(0.40 * cluster_ratio + 0.40 * mean_conf + 0.20 * motion_signal, 3)


# ---------------------------------------------------------------------------
# Hotspot discovery (preserved from v1)
# ---------------------------------------------------------------------------

def discover_hotspots(equirect_path, model, start_frame, end_frame):
    total = max(1, end_frame - start_frame)
    step  = max(1, total // HOTSPOT_SAMPLE_COUNT)
    sample_frames = list(range(start_frame, end_frame, step))[:HOTSPOT_SAMPLE_COUNT]

    all_detections = []
    cap = cv2.VideoCapture(equirect_path)
    for fidx in sample_frames:
        cap.set(cv2.CAP_PROP_POS_FRAMES, fidx)
        ret, frame = cap.read()
        if not ret:
            continue
        frame_dets = []
        for crop_yaw in CROP_YAWS_DEG:
            crop    = extract_crop_frame(frame, crop_yaw)
            results = model(crop, conf=YOLO_CONF, imgsz=YOLO_IMGSZ,
                            classes=[PERSON_CLASS_ID], verbose=False)
            for r in results:
                if r.boxes is None:
                    continue
                for box in r.boxes:
                    if int(box.cls[0]) != PERSON_CLASS_ID:
                        continue
                    x1, y1, x2, y2 = box.xyxy[0].tolist()
                    yaw, pitch = crop_pixel_to_yaw_pitch(
                        (x1 + x2) / 2, (y1 + y2) / 2, crop_yaw)
                    if PITCH_MIN_DEG <= pitch <= PITCH_MAX_DEG:
                        frame_dets.append((yaw, pitch))
        all_detections.append(frame_dets)
    cap.release()

    if not any(all_detections):
        return []

    candidate_centres = [(y, p) for dets in all_detections for y, p in dets]
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
        cy_m, cp_m = spherical_centroid(group)
        hits = sum(
            1 for dets in all_detections
            if any(angular_distance(cy_m, cp_m, dy, dp) < HOTSPOT_SUPPRESS_RADIUS
                   for dy, dp in dets)
        )
        coverage = hits / n_samples
        if coverage >= HOTSPOT_MIN_COVERAGE:
            merged.append({"yaw": round(cy_m, 2), "pitch": round(cp_m, 2),
                           "radius": HOTSPOT_SUPPRESS_RADIUS,
                           "coverage": round(coverage, 3)})

    print(f"[hotspot] {len(merged)} zone(s) from {n_samples} sampled frames")
    for z in merged:
        print(f"  yaw={z['yaw']:.1f}° pitch={z['pitch']:.1f}° "
              f"coverage={z['coverage']*100:.0f}%")
    return merged


def is_hotspot(yaw, pitch, zones):
    return any(angular_distance(yaw, pitch, z["yaw"], z["pitch"]) < z["radius"]
               for z in zones)


# ---------------------------------------------------------------------------
# Four ByteTrack instances — one per crop yaw
# ---------------------------------------------------------------------------

class CropTracker:
    """
    Wraps a YOLO model in track mode for a single fixed-yaw perspective crop.
    Maintains per-track position history to derive spherical velocity.
    IDs are crop-local (integers from Ultralytics ByteTrack).
    """

    def __init__(self, model, crop_yaw_deg, tracker_cfg="bytetrack.yaml"):
        self.model       = model
        self.crop_yaw    = crop_yaw_deg
        self.tracker_cfg = tracker_cfg
        # {track_id: (yaw, pitch)} from the previous frame this crop was seen
        self.prev_positions: dict[int, tuple[float, float]] = {}

    def update(self, crop_frame, hotspot_zones):
        """
        Run tracking on one crop frame.
        Returns list of player dicts with yaw/pitch/conf/track_id/vel_*.
        Filters pitch, hotspots, then computes per-track velocity.
        """
        results = self.model.track(
            crop_frame,
            persist=True,
            tracker=self.tracker_cfg,
            conf=YOLO_CONF,
            imgsz=YOLO_IMGSZ,
            classes=[PERSON_CLASS_ID],
            verbose=False,
        )

        players = []
        new_positions = {}

        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                if int(box.cls[0]) != PERSON_CLASS_ID:
                    continue
                if box.id is None:
                    continue   # track not yet assigned an ID
                track_id = int(box.id[0])
                conf_val = float(box.conf[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                cx, cy_box = (x1 + x2) / 2, (y1 + y2) / 2

                yaw, pitch = crop_pixel_to_yaw_pitch(cx, cy_box, self.crop_yaw)

                # Pitch filter
                if not (PITCH_MIN_DEG <= pitch <= PITCH_MAX_DEG):
                    continue
                # Hotspot suppression
                if is_hotspot(yaw, pitch, hotspot_zones):
                    continue

                new_positions[track_id] = (yaw, pitch)

                # Velocity from previous position of this track
                vel_yaw = vel_pitch = vel_mag = 0.0
                if track_id in self.prev_positions:
                    py_prev, pp_prev = self.prev_positions[track_id]
                    vel_yaw  = yaw   - py_prev
                    vel_pitch = pitch - pp_prev
                    # Wrap yaw delta to [-180, 180]
                    vel_yaw = (vel_yaw + 180) % 360 - 180
                    vel_mag = math.sqrt(vel_yaw**2 + vel_pitch**2)

                players.append({
                    "yaw":       round(yaw, 2),
                    "pitch":     round(pitch, 2),
                    "conf":      round(conf_val, 3),
                    "crop_yaw":  self.crop_yaw,
                    "track_id":  f"{self.crop_yaw}_{track_id}",  # namespaced: crop-local
                    "vel_yaw":   round(vel_yaw, 3),
                    "vel_pitch": round(vel_pitch, 3),
                    "vel_mag":   round(vel_mag, 3),
                })

        self.prev_positions = new_positions
        return players


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(args):
    from ultralytics import YOLO

    print(f"[activity] Person weights : {args.model}")
    print(f"[activity] Tracker        : bytetrack.yaml (built-in Ultralytics)")

    # One shared YOLO model object; four CropTracker instances each call
    # model.track(persist=True) which keeps separate internal track state per call
    # sequence — ByteTrack state is keyed to the model instance, so we need four
    # separate model instances to keep the four track spaces independent.
    models = {yaw: YOLO(args.model) for yaw in CROP_YAWS_DEG}
    trackers = {yaw: CropTracker(models[yaw], yaw) for yaw in CROP_YAWS_DEG}

    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        sys.exit(f"[error] Cannot open: {args.input}")
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps          = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.release()

    start_frame = max(0, args.start_frame)
    end_frame   = min(total_frames, args.end_frame) if args.end_frame > 0 else total_frames
    interval    = max(1, args.sample_interval)

    print(f"[activity] Input    : {args.input}")
    print(f"[activity] Frames   : {start_frame}–{end_frame}  "
          f"({end_frame - start_frame} frames)")
    print(f"[activity] FPS      : {fps:.2f}  Interval: every {interval} frames")

    # Phase A — hotspot discovery (detection only, no tracking needed here)
    print("[activity] === Phase A: hotspot discovery ===")
    # Use one of the model instances for detection-only pass
    hotspot_zones = discover_hotspots(args.input, models[0], start_frame, end_frame)

    # Phase B — sequential read with ByteTrack per crop
    # We must read frames in order (not random-seek) so ByteTrack Kalman state
    # advances correctly.  Skip non-sampled frames by reading but not tracking.
    print("[activity] === Phase B: ByteTrack per crop ===")
    sample_set   = set(range(start_frame, end_frame, interval))
    frame_records = []

    cap = cv2.VideoCapture(args.input)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    processed = 0
    total_samples = len(sample_set)

    for fidx in range(start_frame, end_frame):
        ret, frame = cap.read()
        if not ret:
            break

        if fidx not in sample_set:
            # Feed frame to trackers anyway so Kalman state stays in sync,
            # but discard the output — we only record sampled frames.
            for crop_yaw, tracker in trackers.items():
                crop = extract_crop_frame(frame, crop_yaw)
                tracker.update(crop, hotspot_zones)
            continue

        # --- Tracked detections from all four crops ---
        all_players = []
        for crop_yaw, tracker in trackers.items():
            crop = extract_crop_frame(frame, crop_yaw)
            all_players.extend(tracker.update(crop, hotspot_zones))

        # --- Spatial dedup across crop-overlap zones ---
        players = dedup_players(all_players)

        # --- Cluster ---
        clusters    = cluster_players(players)
        top_cluster = best_cluster(clusters)
        cluster_centre = None
        cluster_size   = 0
        if top_cluster:
            pts = [(p["yaw"], p["pitch"]) for p in top_cluster]
            cy_m, cp_m = spherical_centroid(pts)
            cluster_centre = {"yaw": round(cy_m, 2), "pitch": round(cp_m, 2)}
            cluster_size   = len(top_cluster)

        # --- Track-derived motion and convergence ---
        mot  = motion_score_from_tracks(players)
        conv = convergence_score_from_tracks(players, cluster_centre)

        # --- Confidence ---
        conf = frame_confidence(top_cluster, len(players), mot, conv)

        processed += 1
        frame_records.append({
            "frame":              fidx,
            "players":            players,
            "cluster_centre":     cluster_centre,
            "cluster_size":       cluster_size,
            "confidence":         conf,
            "motion_score":       round(mot, 3),
            "convergence_score":  round(conv, 3),
        })

        if processed % 20 == 0 or processed == total_samples:
            cc_str = (f"yaw={cluster_centre['yaw']:.1f}° "
                      f"pitch={cluster_centre['pitch']:.1f}°"
                      if cluster_centre else "no cluster")
            print(f"  [{processed:3d}/{total_samples}] frame {fidx:5d} "
                  f"| players={len(players):2d} cluster={cluster_size:2d} "
                  f"| {cc_str} conf={conf:.2f} "
                  f"mot={mot:.2f} conv={conv:.2f}")

    cap.release()

    # --- Summary ---
    frames_with_cluster = [r for r in frame_records if r["cluster_centre"] is not None]
    if frames_with_cluster:
        mean_yaw   = yaw_mean([r["cluster_centre"]["yaw"]   for r in frames_with_cluster])
        mean_pitch = (sum(r["cluster_centre"]["pitch"] for r in frames_with_cluster)
                      / len(frames_with_cluster))
    else:
        mean_yaw = mean_pitch = 0.0

    summary = {
        "mean_active_yaw":      round(mean_yaw,   2),
        "mean_active_pitch":    round(mean_pitch,  2),
        "frames_with_cluster":  len(frames_with_cluster),
        "total_sampled":        len(frame_records),
    }

    output = {
        "fps":            fps,
        "sample_interval": interval,
        "start_frame":    start_frame,
        "end_frame":      end_frame,
        "hotspot_zones":  hotspot_zones,
        "frames":         frame_records,
        "summary":        summary,
    }

    with open(args.output, "w") as f:
        json.dump(output, f, indent=2)

    print(f"\n[activity] Done. {len(frame_records)} samples → {args.output}")
    print(f"[activity] Frames with cluster : "
          f"{summary['frames_with_cluster']} / {summary['total_sampled']}")
    print(f"[activity] Mean active centre  : "
          f"yaw={summary['mean_active_yaw']:.1f}° "
          f"pitch={summary['mean_active_pitch']:.1f}°")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="FFA Phase 2 v2 — Player Activity Mapper (ByteTrack)")
    parser.add_argument("--input",           default="equirect_trim.mp4")
    parser.add_argument("--output",          default="activity.json")
    parser.add_argument("--model",           default=YOLO_PERSON_WEIGHTS,
                        help="Person detector weights "
                             "(env: YOLO_PERSON_WEIGHTS, default: yolov8n.pt)")
    parser.add_argument("--sample-interval", type=int, default=DEFAULT_SAMPLE_INTERVAL)
    parser.add_argument("--start-frame",     type=int, default=0)
    parser.add_argument("--end-frame",       type=int, default=-1,
                        help="-1 = full clip")
    args = parser.parse_args()
    run(args)
