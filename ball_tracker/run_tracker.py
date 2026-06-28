#!/usr/bin/env python3
"""
FFA 360 Ball Tracker — v12 (Multi-Timepoint Hotspot Discovery)
=====================================================================
Input:  equirect_trim.mp4
Output: tracking.json  (always)
        tracked.mp4    (only if --metrics-only is NOT set)

v11 replaces the reactive post-lock blacklist (v10c/d/e) with a proactive
bootstrap suppressor:

  1. BOOTSTRAP PASS (pre warm-up):
     Sample HOTSPOT_SAMPLE_COUNT timestamps evenly across the full clip.
     Cluster recurring detections by spherical proximity.
     Any cluster present in >= HOTSPOT_MIN_COVERAGE fraction of bootstrap
     frames is marked as a persistent background hotspot.
     These zones are suppressed from frame 0 — before warm-up, before
     Kalman initialisation, before any candidate is scored.

  2. MAIN PASS:
     Hotspot suppression applied to every candidate in every state:
     WARMING_UP, TRACKING, UNCERTAIN, LOST.
     No reactive blacklist. No grace period. No post-lock patching.

Retained from v10b (baseline):
  - PITCH_HARD_MAX = 18° pre-filter
  - State machine: UNINITIALIZED → WARMING_UP → TRACKING → UNCERTAIN → LOST
  - Tracklet warm-up (KALMAN_INIT_FRAMES=5, WARMUP_MIN_CHAIN_LEN=3)
  - Mahalanobis gating, scoring weights, loss handling, player centroid drift

Removed:
  - v10c STATIC_LOCK_WINDOW / STD-based reactive detection
  - v10d permanent blacklist added post-lock
  - v10e seed_blacklist_zones / seed_blacklist.json

Logging (new):
  - bootstrap_hotspots: [{yaw, pitch, radius, frame_count, coverage_pct}]
  - hotspot_suppression_count: total candidates suppressed by hotspot map
  - hotspot_suppression_by_zone: hit count per hotspot
  - confirmed_fence_hotspot: True if known -77.4°/-3.9° zone found pre-warmup
"""

import argparse
import json
import math
import os
import subprocess

import cv2
import numpy as np
from filterpy.kalman import KalmanFilter
from pitch_geometry import PitchGeometry
from ultralytics import YOLO

# ---------------------------------------------------------------------------
# Config — detection
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
PERSON_CLASS_ID  = 0

OUTPUT_W         = 1920
OUTPUT_H         = 1080
OUTPUT_FOV_DEG   = 90
LEAD_DEG         = 3.0

# ---------------------------------------------------------------------------
# Config — candidate scoring weights
# ---------------------------------------------------------------------------
W_CONF   = 0.20
W_POS    = 0.25
W_PITCH  = 0.15
W_SIZE   = 0.20
W_MOTION = 0.10
W_EDGE   = 0.10

MIN_CANDIDATE_SCORE   = 0.35
HYSTERESIS_MARGIN     = 0.08
MAHAL_SOFT_THRESH     = 4.0
MAHAL_FAST_THRESH     = 8.0
FAST_MOVEMENT_DEG     = 5.0

# ---------------------------------------------------------------------------
# Config — warm-up / state machine
# ---------------------------------------------------------------------------
KALMAN_INIT_FRAMES       = 5
WARMUP_MIN_CHAIN_LEN     = 3

# ---------------------------------------------------------------------------
# Config — loss handling
# ---------------------------------------------------------------------------
LOSS_EXTRAPOLATE_FRAMES = 45
LOSS_HOLD_FRAMES        = 90
PLAYER_DRIFT_SPEED_DEG  = 0.5
REACQ_LERP_FRAMES       = 15
BALL_SIZE_HISTORY        = 30
EMA_ALPHA_TRACKING      = 0.18
EMA_ALPHA_LOSS          = 0.08
MAX_EXTRAP_VELOCITY_DEG = 3.0
EDGE_PENALTY_ZONE       = 80

UNCERTAIN_SCORE_THRESH  = 0.25
UNCERTAIN_STREAK        = 5

# ---------------------------------------------------------------------------
# Config — v9 pitch plausibility
# ---------------------------------------------------------------------------
PITCH_SOFT_MIN = -30.0
PITCH_SOFT_MAX =  10.0
PITCH_PLAUSIBILITY_DECAY = 8.0
PITCH_HARD_MAX           = 18.0  # v10b: hard ceiling

# ---------------------------------------------------------------------------
# Config — v12 multi-timepoint hotspot discovery
# ---------------------------------------------------------------------------
HOTSPOT_SAMPLE_COUNT    = 50    # evenly distributed timestamps to sample across full clip
HOTSPOT_CLUSTER_RADIUS  = 5.0   # degrees: merge candidates within this radius
HOTSPOT_MIN_COVERAGE    = 0.40  # fraction of sampled timestamps a cluster must appear in
HOTSPOT_SUPPRESS_RADIUS = 5.0   # degrees: suppression zone around each hotspot centre

# Known fence zone for pre-warmup confirmation logging
KNOWN_FENCE_YAW   = -77.4
KNOWN_FENCE_PITCH = -3.9
KNOWN_FENCE_CONFIRM_RADIUS = 10.0  # degrees


# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
class TrackerState:
    UNINITIALIZED = "UNINITIALIZED"
    WARMING_UP    = "WARMING_UP"
    TRACKING      = "TRACKING"
    UNCERTAIN     = "UNCERTAIN"
    LOST          = "LOST"


# ---------------------------------------------------------------------------
# GPU detection
# ---------------------------------------------------------------------------
def get_device():
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            vram = torch.cuda.get_device_properties(0).total_memory // (1024**2)
            print(f"[gpu] CUDA available: {name} ({vram}MB VRAM) — using GPU")
            return "cuda"
    except ImportError:
        pass
    print("[gpu] CUDA not available — using CPU")
    return "cpu"


# ---------------------------------------------------------------------------
# Kalman filter
# ---------------------------------------------------------------------------
def build_kalman():
    kf = KalmanFilter(dim_x=4, dim_z=2)
    dt = 1.0
    kf.F = np.array([[1, 0, dt, 0],
                     [0, 1,  0, dt],
                     [0, 0,  1,  0],
                     [0, 0,  0,  1]], dtype=float)
    kf.H = np.array([[1, 0, 0, 0],
                     [0, 1, 0, 0]], dtype=float)
    kf.R *= 5.0
    kf.Q *= 0.01
    kf.P *= 10.0
    return kf


# ---------------------------------------------------------------------------
# Geometry
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
    dp = math.radians(p1 - p2)
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


def player_centroid_from_detections(person_detections):
    if not person_detections:
        return None
    yaws   = [d[0] for d in person_detections]
    pitches = [d[1] for d in person_detections]
    return sum(yaws) / len(yaws), sum(pitches) / len(pitches)


# ---------------------------------------------------------------------------
# Crop extraction
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


# ---------------------------------------------------------------------------
# Size / velocity trackers
# ---------------------------------------------------------------------------
class BallSizeTracker:
    def __init__(self, history=BALL_SIZE_HISTORY):
        self._history = history
        self._sizes   = []

    def update(self, bbox_area):
        self._sizes.append(bbox_area)
        if len(self._sizes) > self._history:
            self._sizes.pop(0)

    def expected_size(self):
        return sum(self._sizes) / len(self._sizes) if self._sizes else None

    def size_score(self, bbox_area):
        exp = self.expected_size()
        if exp is None or exp <= 0:
            return 0.5
        ratio = bbox_area / exp
        return max(0.0, 1.0 - abs(math.log(max(ratio, 1e-6))) / 2.0)


class VelocityTracker:
    def __init__(self, history=8):
        self._history = history
        self._vels    = []

    def update(self, dyaw, dpitch):
        self._vels.append((dyaw, dpitch))
        if len(self._vels) > self._history:
            self._vels.pop(0)

    def expected_velocity(self):
        if not self._vels:
            return 0.0, 0.0
        return (sum(v[0] for v in self._vels) / len(self._vels),
                sum(v[1] for v in self._vels) / len(self._vels))

    def motion_score(self, dyaw, dpitch):
        ey, ep = self.expected_velocity()
        pred_dist = math.sqrt((dyaw - ey)**2 + (dpitch - ep)**2)
        return max(0.0, 1.0 - pred_dist / 10.0)


# ---------------------------------------------------------------------------
# v11: Bootstrap static background map builder
# ---------------------------------------------------------------------------
def build_static_hotspot_map(sampled_candidates):
    """
    sampled_candidates: list of per-timestamp lists, each [(yaw, pitch, conf, crop_yaw), ...]
    Returns list of hotspot dicts with full per-hotspot metadata.
    Uses greedy spherical clustering. A cluster must appear in
    >= HOTSPOT_MIN_COVERAGE fraction of sampled timestamps to be flagged.
    v12: candidates carry crop_yaw for source_crop_distribution logging.
    """
    n_frames = len(sampled_candidates)
    if n_frames == 0:
        return []

    # Build clusters: list of {yaw, pitch, timestamps: set, confs: list, crops: dict}
    clusters = []

    for fi, frame_cands in enumerate(sampled_candidates):
        for item in frame_cands:
            yaw, pitch, conf = item[0], item[1], item[2]
            crop_yaw = item[3] if len(item) > 3 else None
            matched = False
            for cl in clusters:
                if angular_distance(yaw, pitch, cl["yaw"], cl["pitch"]) < HOTSPOT_CLUSTER_RADIUS:
                    n = len(cl["timestamps"])
                    cl["yaw"]   = (cl["yaw"] * n + yaw)   / (n + 1)
                    cl["pitch"] = (cl["pitch"] * n + pitch) / (n + 1)
                    cl["timestamps"].add(fi)
                    cl["confs"].append(conf)
                    if crop_yaw is not None:
                        cl["crops"][str(crop_yaw)] = cl["crops"].get(str(crop_yaw), 0) + 1
                    matched = True
                    break
            if not matched:
                clusters.append({
                    "yaw": yaw, "pitch": pitch,
                    "timestamps": {fi},
                    "confs": [conf],
                    "crops": {str(crop_yaw): 1} if crop_yaw is not None else {},
                })

    hotspots = []
    for cl in clusters:
        coverage = len(cl["timestamps"]) / n_frames
        if coverage >= HOTSPOT_MIN_COVERAGE:
            mean_conf = round(sum(cl["confs"]) / len(cl["confs"]), 3) if cl["confs"] else 0.0
            hotspots.append({
                "yaw":                      round(cl["yaw"], 2),
                "pitch":                    round(cl["pitch"], 2),
                "radius":                   HOTSPOT_SUPPRESS_RADIUS,
                "timestamp_count":          len(cl["timestamps"]),
                "coverage_pct":             round(100.0 * coverage, 1),
                "mean_conf":                mean_conf,
                "source_crop_distribution": cl["crops"],
                "hit_count":                0,
            })

    return hotspots


def is_hotspot_suppressed(yaw, pitch, hotspots):
    for hs in hotspots:
        if angular_distance(yaw, pitch, hs["yaw"], hs["pitch"]) < hs["radius"]:
            hs["hit_count"] += 1
            return True
    return False


# ---------------------------------------------------------------------------
# Warm-up tracklet builder (unchanged from v8/v10b)
# ---------------------------------------------------------------------------
def build_warmup_tracklets(warmup_frames):
    best_chain, best_score = None, -1.0
    nodes = []
    for fi, candidates in enumerate(warmup_frames):
        for ci, (yaw, pitch, conf, area) in enumerate(candidates):
            nodes.append((fi, ci, yaw, pitch, conf, area))

    if not nodes:
        return None, 0.0

    chains = {i: {"path": [i], "score": nodes[i][4]} for i in range(len(nodes))}

    for i, (fi, ci, yaw, pitch, conf, area) in enumerate(nodes):
        for j, (fj, cj, yw2, pt2, cf2, ar2) in enumerate(nodes):
            if fj != fi + 1:
                continue
            dist = angular_distance(yaw, pitch, yw2, pt2)
            if dist > 30.0:
                continue
            link_score = (conf + cf2) / 2.0 - dist / 60.0
            for seed, chain in list(chains.items()):
                if chain["path"][-1] == i:
                    new_score = chain["score"] + link_score
                    if j not in chains or chains[j]["score"] < new_score:
                        chains[j] = {"path": chain["path"] + [j], "score": new_score}

    for chain in chains.values():
        path = chain["path"]
        if len(path) < WARMUP_MIN_CHAIN_LEN:
            continue
        # Hard-exclude impossible pitch in warm-up chain
        if any(nodes[k][3] > PITCH_HARD_MAX for k in path):
            continue
        if chain["score"] > best_score:
            best_score = chain["score"]
            best_chain = [(nodes[k][0], nodes[k][1], nodes[k][2], nodes[k][3],
                           chain["score"], nodes[k][5]) for k in path]

    return best_chain, best_score


# ---------------------------------------------------------------------------
# Pitch plausibility (v9, unchanged)
# ---------------------------------------------------------------------------
def pitch_plausibility(pitch_deg):
    if pitch_deg > PITCH_HARD_MAX:
        return 0.0
    if PITCH_SOFT_MIN <= pitch_deg <= PITCH_SOFT_MAX:
        return 1.0
    dist = max(0.0, pitch_deg - PITCH_SOFT_MAX) if pitch_deg > PITCH_SOFT_MAX \
           else max(0.0, PITCH_SOFT_MIN - pitch_deg)
    return max(0.0, 1.0 - dist / PITCH_PLAUSIBILITY_DECAY)


# ---------------------------------------------------------------------------
# Candidate scorer (unchanged from v10b)
# ---------------------------------------------------------------------------
def score_candidate(candidate, kf, kf_initialised, ball_size_tracker, vel_tracker,
                    last_yaw, last_pitch, crop_yaw, tracker_state):
    yaw, pitch, conf, area, px, py = candidate

    plaus = pitch_plausibility(pitch)
    if plaus == 0.0:
        return 0.0, {"_rejected": "pitch_hard_max"}

    conf_score   = min(1.0, conf / 0.5)
    size_score   = ball_size_tracker.size_score(area)

    if kf_initialised:
        pred_yaw   = float(kf.x[0, 0])
        pred_pitch = float(kf.x[1, 0])
        dy  = yaw   - pred_yaw
        dp  = pitch - pred_pitch
        S   = kf.S if hasattr(kf, "S") else np.eye(2) * 25.0
        try:
            z   = np.array([dy, dp])
            Si  = np.linalg.inv(S)
            mahal = float(np.sqrt(z @ Si @ z))
        except Exception:
            mahal = 0.0
        if mahal > MAHAL_FAST_THRESH and angular_distance(yaw, pitch, last_yaw, last_pitch) > FAST_MOVEMENT_DEG:
            mahal = min(mahal, MAHAL_SOFT_THRESH)
        pos_score = max(0.0, 1.0 - mahal / 10.0)
        dyaw_v  = yaw   - last_yaw
        dpitch_v = pitch - last_pitch
        motion_score = vel_tracker.motion_score(dyaw_v, dpitch_v)
    else:
        pos_score    = 0.5
        mahal        = 0.0
        motion_score = 0.5

    edge_dist   = min(abs(px), abs(CROP_W - px))
    edge_score  = min(1.0, edge_dist / EDGE_PENALTY_ZONE)

    score = (W_CONF * conf_score + W_POS * pos_score + W_PITCH * plaus +
             W_SIZE * size_score + W_MOTION * motion_score + W_EDGE * edge_score)

    return score, {
        "conf_score": round(conf_score, 3), "pos_score": round(pos_score, 3),
        "plaus": round(plaus, 3), "size_score": round(size_score, 3),
        "motion_score": round(motion_score, 3), "edge_score": round(edge_score, 3),
        "_mahal": round(mahal, 3),
    }


# ---------------------------------------------------------------------------
# Main tracking loop
# ---------------------------------------------------------------------------
def run_tracker(equirect_path, output_path, json_path,
                ball_model_path="models/football-ball-detection.pt",
                person_model_path="yolov8s.pt",
                metrics_only=False):

    device = get_device()

    print(f"[yolo] loading BALL model  {ball_model_path} on {device}")
    ball_model = YOLO(ball_model_path)
    ball_model.to(device)

    print(f"[yolo] loading PERSON model {person_model_path} on {device}")
    person_model = YOLO(person_model_path)
    person_model.to(device)

    print("=" * 70)
    print("[v12 MULTI-TIMEPOINT HOTSPOT DISCOVERY]")
    print(f"  hotspot_sample_count : {HOTSPOT_SAMPLE_COUNT}")
    print(f"  cluster_radius     : {HOTSPOT_CLUSTER_RADIUS}°")
    print(f"  min_coverage       : {HOTSPOT_MIN_COVERAGE*100:.0f}%")
    print(f"  suppress_radius    : {HOTSPOT_SUPPRESS_RADIUS}°")
    print(f"  pitch_hard_max     : {PITCH_HARD_MAX}°")
    print(f"  kalman_init_frames : {KALMAN_INIT_FRAMES}  min_chain={WARMUP_MIN_CHAIN_LEN}")
    print("=" * 70)

    os.environ["OPENCV_FFMPEG_READ_ATTEMPTS"] = "65536"

    # ====================================================================
    # PASS 1: Multi-timepoint hotspot discovery (v12)
    # Sample HOTSPOT_SAMPLE_COUNT timestamps evenly across full clip.
    # ====================================================================
    print(f"\n[v12] === MULTI-TIMEPOINT HOTSPOT DISCOVERY PASS ===")
    cap = cv2.VideoCapture(equirect_path, cv2.CAP_FFMPEG)
    fps          = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[video] {total_frames} frames @ {fps:.2f} fps")
    print(f"  hotspot_sample_count : {HOTSPOT_SAMPLE_COUNT}")

    # Build evenly spaced sample frame indices (avoid first/last 5 frames)
    _margin = 5
    _sample_indices = sorted(set(
        int(_margin + i * (total_frames - 2 * _margin) / max(HOTSPOT_SAMPLE_COUNT - 1, 1))
        for i in range(HOTSPOT_SAMPLE_COUNT)
    ))
    _sample_indices = [fi for fi in _sample_indices if 0 <= fi < total_frames]
    print(f"  sampling frames      : {_sample_indices[0]}…{_sample_indices[-1]} ({len(_sample_indices)} points)")

    sampled_candidates = []   # per-timestamp lists of (yaw, pitch, conf, crop_yaw)
    for _si, _fi in enumerate(_sample_indices):
        cap.set(cv2.CAP_PROP_POS_FRAMES, _fi)
        ret, frame = cap.read()
        if not ret:
            sampled_candidates.append([])
            continue
        _raw = []
        for crop_yaw in CROP_YAWS_DEG:
            crop = extract_crop_frame(frame, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
            res  = ball_model(crop, imgsz=YOLO_IMGSZ, conf=YOLO_CONF,
                              verbose=False, classes=[BALL_CLASS_ID], device=device)
            for box in res[0].boxes:
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                px, py = (x1 + x2) / 2, (y1 + y2) / 2
                conf   = float(box.conf[0])
                yaw_d, pitch_d = crop_pixel_to_yaw_pitch(
                    px, py, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                _raw.append((yaw_d, pitch_d, conf, crop_yaw))
        # Dedupe on yaw/pitch/conf, then re-attach crop_yaw and pitch-cap
        _deduped_ypc = dedupe_detections([(_r[0], _r[1], _r[2]) for _r in _raw])
        _deduped = []
        for _yaw, _pitch, _conf in _deduped_ypc:
            if _pitch > PITCH_HARD_MAX:
                continue
            _crop_src = next((_r[3] for _r in _raw if abs(_r[0]-_yaw)<0.01 and abs(_r[1]-_pitch)<0.01), None)
            _deduped.append((_yaw, _pitch, _conf, _crop_src))
        sampled_candidates.append(_deduped)
        if _si % 10 == 0 or _si == len(_sample_indices) - 1:
            print(f"  [hotspot scan] {_si+1}/{len(_sample_indices)}  frame={_fi}  candidates={len(_deduped)}")
    cap.release()

    # Build hotspot map from sampled timestamps
    hotspots = build_static_hotspot_map(sampled_candidates)
    _geo_config = os.path.join(os.path.dirname(__file__), "configs", "geometry_st_margarets.json")
    pitch_geo = PitchGeometry(_geo_config) if os.path.exists(_geo_config) else None

    print(f"\n[v12] Hotspot discovery complete. Found {len(hotspots)} hotspot(s):")
    confirmed_fence_hotspot = False
    for i, hs in enumerate(hotspots):
        print(f"  hotspot {i}: yaw={hs['yaw']:.2f}° pitch={hs['pitch']:.2f}° "
              f"coverage={hs['coverage_pct']:.1f}% timestamps={hs['timestamp_count']} "
              f"mean_conf={hs['mean_conf']:.3f} crops={hs['source_crop_distribution']}")
        if angular_distance(hs["yaw"], hs["pitch"],
                            KNOWN_FENCE_YAW, KNOWN_FENCE_PITCH) < KNOWN_FENCE_CONFIRM_RADIUS:
            confirmed_fence_hotspot = True
            print(f"  *** FENCE HOTSPOT CONFIRMED (matches known {KNOWN_FENCE_YAW}°/{KNOWN_FENCE_PITCH}°)")

    if not confirmed_fence_hotspot:
        print(f"  [v12] NOTE: known fence zone ({KNOWN_FENCE_YAW}°, {KNOWN_FENCE_PITCH}°) "
              f"not found as hotspot on this clip")

    # ====================================================================
    # PASS 2: Main tracking
    # ====================================================================
    print(f"\n[v11] === MAIN TRACKING PASS ===")
    cap = cv2.VideoCapture(equirect_path, cv2.CAP_FFMPEG)

    ffmpeg_writer = None
    if not metrics_only:
        ffmpeg_writer = subprocess.Popen([
            FFMPEG, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{OUTPUT_W}x{OUTPUT_H}",
            "-pix_fmt", "bgr24",
            "-r", str(fps),
            "-i", "pipe:0",
            "-vcodec", "libx264",
            "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            output_path
        ], stdin=subprocess.PIPE)

    kf             = build_kalman()
    kf_initialised = False
    tracker_state  = TrackerState.UNINITIALIZED

    warmup_buffer  = []
    kalman_init_frame       = None
    warmup_candidate_count  = 0
    warmup_consistency_score = 0.0
    reinitialisation_count  = 0
    state_transition_counts = {s: 0 for s in [
        TrackerState.WARMING_UP, TrackerState.TRACKING,
        TrackerState.UNCERTAIN, TrackerState.LOST]}

    frames_since_detection     = 0
    confirmed_yaw, confirmed_pitch = 0.0, 0.0
    prev_loss_state_was_hold   = False
    incumbent_yaw, incumbent_pitch = None, None
    uncertain_streak           = 0
    reacq_lerp_remaining = 0
    reacq_target_yaw     = 0.0
    reacq_target_pitch   = 0.0

    ema_yaw, ema_pitch = None, None
    cam_yaw, cam_pitch = 0.0, 0.0
    last_yaw, last_pitch = 0.0, 0.0

    ball_size_tracker = BallSizeTracker()
    vel_tracker       = VelocityTracker()

    tracking_data = []
    swap_events   = []

    instr = {
        "frames_total":                0,
        "frames_with_raw_ball":        0,
        "frames_confirmed_ball":       0,
        "raw_ball_candidates":         [],
        "deduped_ball_candidates":     [],
        "all_candidate_areas":         [],
        "all_candidate_confs":         [],
        "best_candidate_scores":       [],
        "accepted_size_scores":        [],
        "continuous_gate_rejected":    0,
        "candidate_switch_count":      0,
        "rejection_reasons": {
            "low_score":    0,
            "hysteresis":   0,
            "pitch_hard_max": 0,
            "hotspot":      0,
        },
        "mahalanobis_accepted":        [],
        "mahalanobis_rejected":        [],
        "large_yaw_jump_count":        0,
        "accepted_pitches":            [],
        "pitch_hard_rejections":       0,
        "hotspot_suppression_count":   0,
        "pitch_geometry_suppression_count": 0,
    }

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # ---- Detection ----
        raw_ball_detections = []
        person_detections   = []

        for crop_yaw in CROP_YAWS_DEG:
            crop = extract_crop_frame(frame, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)

            ball_res = ball_model(crop, imgsz=YOLO_IMGSZ, conf=YOLO_CONF,
                                  verbose=False, classes=[BALL_CLASS_ID], device=device)
            for box in ball_res[0].boxes:
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                px, py = (x1 + x2) / 2, (y1 + y2) / 2
                conf   = float(box.conf[0])
                area   = (x2 - x1) * (y2 - y1)
                yaw_d, pitch_d = crop_pixel_to_yaw_pitch(
                    px, py, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                raw_ball_detections.append((yaw_d, pitch_d, conf, area, px, py, crop_yaw))
                instr["all_candidate_areas"].append(area)
                instr["all_candidate_confs"].append(conf)

            person_res = person_model(crop, imgsz=YOLO_IMGSZ, conf=0.40,
                                      verbose=False, classes=[PERSON_CLASS_ID], device=device)
            for box in person_res[0].boxes:
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                px, py = (x1 + x2) / 2, (y1 + y2) / 2
                conf   = float(box.conf[0])
                yaw_d, pitch_d = crop_pixel_to_yaw_pitch(
                    px, py, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                person_detections.append((yaw_d, pitch_d, conf))

        deduped_balls   = dedupe_detections(
            [(d[0], d[1], d[2]) for d in raw_ball_detections])
        deduped_persons = dedupe_detections(person_detections)

        instr["frames_total"]          += 1
        instr["raw_ball_candidates"].append(len(raw_ball_detections))
        instr["deduped_ball_candidates"].append(len(deduped_balls))
        if raw_ball_detections:
            instr["frames_with_raw_ball"] += 1

        def find_raw(yaw, pitch):
            best, best_d = None, 999
            for r in raw_ball_detections:
                d = angular_distance(yaw, pitch, r[0], r[1])
                if d < best_d:
                    best_d, best = d, r
            return best

        frame_record = {
            "frame": frame_idx, "detections": [],
            "smoothed": None, "loss_state": None,
            "tracker_state": None, "best_score": None,
        }

        # ================================================================
        # Helper: filter candidates through pitch cap + hotspot suppression
        # ================================================================
        def filter_candidates(candidates):
            """Apply pitch hard cap and hotspot suppression. Returns filtered list."""
            filtered = []
            for yaw_d, pitch_d, conf_d in candidates:
                if pitch_d > PITCH_HARD_MAX:
                    instr["pitch_hard_rejections"] += 1
                    instr["rejection_reasons"]["pitch_hard_max"] += 1
                    continue
                if is_hotspot_suppressed(yaw_d, pitch_d, hotspots):
                    instr["hotspot_suppression_count"] += 1
                    instr["rejection_reasons"]["hotspot"] += 1
                    continue
                if pitch_geo and pitch_geo.is_suppressed(yaw_d, pitch_d):
                    instr["pitch_geometry_suppression_count"] += 1
                    instr["hotspot_suppression_count"] += 1
                    instr["rejection_reasons"]["hotspot"] += 1
                    continue
                filtered.append((yaw_d, pitch_d, conf_d))
            return filtered

        # ================================================================
        # STATE: UNINITIALIZED / WARMING_UP
        # ================================================================
        if tracker_state in (TrackerState.UNINITIALIZED, TrackerState.WARMING_UP):

            filtered = filter_candidates(deduped_balls)

            if tracker_state == TrackerState.UNINITIALIZED and filtered:
                tracker_state = TrackerState.WARMING_UP
                state_transition_counts[TrackerState.WARMING_UP] += 1

            if tracker_state == TrackerState.WARMING_UP:
                frame_candidates = []
                for yaw_d, pitch_d, conf_d in filtered:
                    raw = find_raw(yaw_d, pitch_d)
                    if raw:
                        frame_candidates.append((yaw_d, pitch_d, conf_d, raw[3]))
                warmup_buffer.append(frame_candidates)
                warmup_candidate_count += len(frame_candidates)

                if len(warmup_buffer) >= KALMAN_INIT_FRAMES:
                    best_chain, chain_score = build_warmup_tracklets(warmup_buffer)

                    if best_chain is not None:
                        init_yaw   = sum(c[2] for c in best_chain) / len(best_chain)
                        init_pitch = sum(c[3] for c in best_chain) / len(best_chain)
                        if len(best_chain) >= 2:
                            total_dyaw   = best_chain[-1][2] - best_chain[0][2]
                            total_dpitch = best_chain[-1][3] - best_chain[0][3]
                            n_steps = best_chain[-1][0] - best_chain[0][0]
                            if n_steps > 0:
                                init_dyaw   = total_dyaw   / n_steps
                                init_dpitch = total_dpitch / n_steps
                            else:
                                init_dyaw, init_dpitch = 0.0, 0.0
                        else:
                            init_dyaw, init_dpitch = 0.0, 0.0

                        kf.x = np.array([[init_yaw], [init_pitch],
                                          [init_dyaw], [init_dpitch]])
                        kf_initialised = True
                        kalman_init_frame = frame_idx
                        warmup_consistency_score = chain_score
                        confirmed_yaw, confirmed_pitch = init_yaw, init_pitch
                        incumbent_yaw, incumbent_pitch = init_yaw, init_pitch
                        cam_yaw, cam_pitch = init_yaw, init_pitch

                        for c in best_chain:
                            if c[5] > 0:
                                ball_size_tracker.update(c[5])

                        tracker_state = TrackerState.TRACKING
                        state_transition_counts[TrackerState.TRACKING] += 1
                        frames_since_detection = 0
                        print(f"[v11] Kalman initialised at frame {frame_idx} "
                              f"from chain len={len(best_chain)} score={chain_score:.3f} "
                              f"init_pos=({init_yaw:.1f}°, {init_pitch:.1f}°)")
                    else:
                        print(f"[v11] Warm-up failed at frame {frame_idx} "
                              f"(no chain ≥{WARMUP_MIN_CHAIN_LEN}) — resetting buffer")
                        warmup_buffer = []
                        reinitialisation_count += 1

            ball_seen_this_frame = False
            best_candidate = None
            best_score = 0.0

        # ================================================================
        # STATE: TRACKING / UNCERTAIN / LOST
        # ================================================================
        else:
            if kf_initialised:
                kf.predict()

            best_candidate = None
            best_score     = -1.0
            ball_seen_this_frame = False

            filtered = filter_candidates(deduped_balls)

            for yaw_d, pitch_d, conf_d in filtered:
                raw = find_raw(yaw_d, pitch_d)
                if raw is None:
                    continue
                area, px, py = raw[3], raw[4], raw[5]
                candidate = (yaw_d, pitch_d, conf_d, area, px, py)

                score, components = score_candidate(
                    candidate, kf, kf_initialised, ball_size_tracker, vel_tracker,
                    last_yaw, last_pitch, raw[6], tracker_state)

                mahal = components.get("_mahal", 0.0)

                if abs(yaw_d - last_yaw) > 30.0:
                    instr["large_yaw_jump_count"] += 1

                if score < MIN_CANDIDATE_SCORE:
                    instr["rejection_reasons"]["low_score"] += 1
                    instr["continuous_gate_rejected"] += 1
                    instr["mahalanobis_rejected"].append(mahal)
                    continue

                instr["mahalanobis_accepted"].append(mahal)
                if score > best_score:
                    best_score, best_candidate = score, candidate

            # Hysteresis
            if best_candidate is not None:
                yaw_meas, pitch_meas = best_candidate[0], best_candidate[1]
                if (incumbent_yaw is not None and
                        angular_distance(yaw_meas, pitch_meas,
                                         incumbent_yaw, incumbent_pitch) > DEDUP_THRESH_DEG):
                    prev_best = instr["best_candidate_scores"][-1] if instr["best_candidate_scores"] else 0
                    if best_score < prev_best + HYSTERESIS_MARGIN:
                        instr["rejection_reasons"]["hysteresis"] += 1
                        instr["continuous_gate_rejected"] += 1
                        best_candidate = None
                    else:
                        instr["candidate_switch_count"] += 1

            if best_candidate is not None:
                yaw_meas, pitch_meas, conf_meas, area_meas = best_candidate[:4]
                ball_seen_this_frame = True
                instr["best_candidate_scores"].append(best_score)
                instr["frames_confirmed_ball"] += 1
                instr["accepted_size_scores"].append(ball_size_tracker.size_score(area_meas))
                instr["accepted_pitches"].append(pitch_meas)

                vel_tracker.update(yaw_meas - float(kf.x[0, 0]),
                                   pitch_meas - float(kf.x[1, 0]))
                kf.update(np.array([[yaw_meas], [pitch_meas]]))
                ball_size_tracker.update(area_meas)
                confirmed_yaw, confirmed_pitch = float(kf.x[0, 0]), float(kf.x[1, 0])
                incumbent_yaw, incumbent_pitch = yaw_meas, pitch_meas
                frames_since_detection = 0
                prev_loss_state_was_hold = False

                uncertain_streak = 0
                if tracker_state in (TrackerState.UNCERTAIN, TrackerState.LOST):
                    tracker_state = TrackerState.TRACKING
                    state_transition_counts[TrackerState.TRACKING] += 1
                    if tracker_state == TrackerState.LOST:
                        reinitialisation_count += 1

                frame_record["best_score"] = round(best_score, 3)

            else:
                frames_since_detection += 1
                if best_score > 0 and best_score < UNCERTAIN_SCORE_THRESH:
                    uncertain_streak += 1
                else:
                    uncertain_streak = 0

                if frames_since_detection > LOSS_EXTRAPOLATE_FRAMES + LOSS_HOLD_FRAMES:
                    if tracker_state != TrackerState.LOST:
                        tracker_state = TrackerState.LOST
                        state_transition_counts[TrackerState.LOST] += 1
                elif uncertain_streak >= UNCERTAIN_STREAK:
                    if tracker_state == TrackerState.TRACKING:
                        tracker_state = TrackerState.UNCERTAIN
                        state_transition_counts[TrackerState.UNCERTAIN] += 1

        # ================================================================
        # Camera target — loss handling (unchanged from v10b)
        # ================================================================
        if not kf_initialised or tracker_state == TrackerState.UNINITIALIZED:
            target_yaw, target_pitch = last_yaw, last_pitch
            loss_state = "uninitialised"
            ema_alpha  = EMA_ALPHA_LOSS

        elif tracker_state == TrackerState.WARMING_UP:
            target_yaw, target_pitch = last_yaw, last_pitch
            loss_state = f"warming_up ({len(warmup_buffer)}/{KALMAN_INIT_FRAMES})"
            ema_alpha  = EMA_ALPHA_LOSS

        elif frames_since_detection == 0:
            target_yaw   = confirmed_yaw
            target_pitch = confirmed_pitch
            loss_state   = "tracking"
            ema_alpha    = EMA_ALPHA_TRACKING

        elif frames_since_detection <= LOSS_EXTRAPOLATE_FRAMES:
            if prev_loss_state_was_hold:
                kf.x[2, 0] = 0.0
                kf.x[3, 0] = 0.0
                prev_loss_state_was_hold = False
            raw_yaw   = float(kf.x[0, 0])
            raw_pitch = float(kf.x[1, 0])
            if cam_yaw is not None:
                delta_yaw   = raw_yaw   - cam_yaw
                delta_pitch = raw_pitch - cam_pitch
                speed = math.sqrt(delta_yaw**2 + delta_pitch**2)
                if speed > MAX_EXTRAP_VELOCITY_DEG:
                    scale = MAX_EXTRAP_VELOCITY_DEG / speed
                    raw_yaw   = cam_yaw   + delta_yaw   * scale
                    raw_pitch = cam_pitch + delta_pitch * scale
            target_yaw, target_pitch = raw_yaw, raw_pitch
            loss_state = f"extrapolating ({frames_since_detection})"
            ema_alpha  = EMA_ALPHA_LOSS

        elif frames_since_detection <= LOSS_EXTRAPOLATE_FRAMES + LOSS_HOLD_FRAMES:
            target_yaw, target_pitch = confirmed_yaw, confirmed_pitch
            loss_state = f"holding ({frames_since_detection})"
            ema_alpha  = EMA_ALPHA_LOSS
            prev_loss_state_was_hold = True

        else:
            centroid = player_centroid_from_detections(deduped_persons)
            if centroid:
                dy   = centroid[0] - cam_yaw
                dp   = centroid[1] - cam_pitch
                dist = math.sqrt(dy**2 + dp**2)
                if dist > PLAYER_DRIFT_SPEED_DEG:
                    target_yaw   = cam_yaw   + PLAYER_DRIFT_SPEED_DEG * dy / dist
                    target_pitch = cam_pitch + PLAYER_DRIFT_SPEED_DEG * dp / dist
                else:
                    target_yaw, target_pitch = centroid
            else:
                target_yaw, target_pitch = cam_yaw, cam_pitch
            loss_state = f"player_drift ({frames_since_detection})"
            ema_alpha  = EMA_ALPHA_LOSS

        if reacq_lerp_remaining > 0:
            t = 1.0 - (reacq_lerp_remaining / REACQ_LERP_FRAMES)
            target_yaw   = cam_yaw + t * (reacq_target_yaw   - cam_yaw)
            target_pitch = cam_pitch + t * (reacq_target_pitch - cam_pitch)
            reacq_lerp_remaining -= 1

        cam_yaw, cam_pitch = target_yaw, target_pitch
        last_yaw, last_pitch = cam_yaw, cam_pitch

        if ema_yaw is None:
            ema_yaw, ema_pitch = cam_yaw, cam_pitch
        else:
            ema_yaw   = ema_alpha * cam_yaw   + (1 - ema_alpha) * ema_yaw
            ema_pitch = ema_alpha * cam_pitch + (1 - ema_alpha) * ema_pitch

        frame_record["detections"]    = [{"yaw": d[0], "pitch": d[1], "conf": d[2]}
                                          for d in deduped_balls]
        frame_record["smoothed"]      = {"yaw": round(ema_yaw, 2), "pitch": round(ema_pitch, 2)}
        frame_record["loss_state"]    = loss_state
        frame_record["tracker_state"] = tracker_state
        tracking_data.append(frame_record)

        if not metrics_only and ffmpeg_writer:
            render_yaw = ema_yaw + LEAD_DEG
            out_frame  = extract_crop_frame(frame, render_yaw, OUTPUT_FOV_DEG, OUTPUT_W, OUTPUT_H)
            ffmpeg_writer.stdin.write(out_frame.tobytes())

        frame_idx += 1
        if frame_idx % 200 == 0:
            cg_pct = 100 * instr["frames_confirmed_ball"] / max(instr["frames_total"], 1)
            print(f"[tracker] frame {frame_idx}/{total_frames} | "
                  f"state={tracker_state} yaw={ema_yaw:.1f}° | {loss_state} | "
                  f"confirmed={cg_pct:.1f}% hotspot_hits={instr['hotspot_suppression_count']}")

    cap.release()
    if ffmpeg_writer:
        ffmpeg_writer.stdin.close()
        ffmpeg_writer.wait()
        print(f"[done] tracked.mp4 → {output_path}")

    # ---- Metrics summary ----
    ft = max(instr["frames_total"], 1)
    def _median(x): return round(float(np.median(x)), 4) if x else None
    def _mean(x):   return round(float(np.mean(x)), 4)   if x else None
    def _pct(x, p): return round(float(np.percentile(x, p)), 2) if x else None

    detection_metrics = {
        "frames_total":                     instr["frames_total"],
        "frames_with_raw_ball":             instr["frames_with_raw_ball"],
        "frames_with_raw_ball_pct":         round(100.0 * instr["frames_with_raw_ball"] / ft, 2),
        "frames_confirmed_ball":            instr["frames_confirmed_ball"],
        "confirmed_ball_pct":               round(100.0 * instr["frames_confirmed_ball"] / ft, 2),
        "mean_raw_candidates_per_frame":    _mean(instr["raw_ball_candidates"]),
        "mean_deduped_candidates_per_frame":_mean(instr["deduped_ball_candidates"]),
        "median_candidate_box_area":        _median(instr["all_candidate_areas"]),
        "median_candidate_conf":            _median(instr["all_candidate_confs"]),
        "median_accepted_size_score":       _median(instr["accepted_size_scores"]),
    }
    association_metrics = {
        "continuous_gate_rejected_count":   instr["continuous_gate_rejected"],
        "candidate_switch_count":           instr["candidate_switch_count"],
        "mean_best_candidate_score":        _mean(instr["best_candidate_scores"]),
        "rejection_reason_counts":          instr["rejection_reasons"],
        "mean_mahalanobis_accepted":        _mean(instr["mahalanobis_accepted"]),
        "mean_mahalanobis_rejected":        _mean(instr["mahalanobis_rejected"]),
        "large_yaw_jump_count":             instr["large_yaw_jump_count"],
        "accepted_pitch_median":            _median(instr["accepted_pitches"]),
        "accepted_pitch_p90":               _pct(instr["accepted_pitches"], 90),
        "accepted_pitch_p95":               _pct(instr["accepted_pitches"], 95),
        "accepted_pitch_above_10":          sum(1 for p in instr["accepted_pitches"] if p > 10.0),
        "accepted_pitch_above_20":          sum(1 for p in instr["accepted_pitches"] if p > 20.0),
        "accepted_pitch_above_30":          sum(1 for p in instr["accepted_pitches"] if p > 30.0),
        "pitch_hard_rejection_count":       instr["pitch_hard_rejections"],
        "hotspot_suppression_count":        instr["hotspot_suppression_count"],
        "pitch_geometry_suppression_count": instr["pitch_geometry_suppression_count"],
        "hotspot_suppression_by_zone": [
            {"yaw": hs["yaw"], "pitch": hs["pitch"],
             "coverage_pct": hs["coverage_pct"], "hit_count": hs["hit_count"]}
            for hs in hotspots
        ],
    }
    v11_bootstrap_metrics = {
        "hotspot_sample_count":         len(sampled_candidates),
        "hotspots_discovered":          len(hotspots),
        "confirmed_fence_hotspot":      confirmed_fence_hotspot,
        "bootstrap_hotspots": [
            {
                "yaw":                      hs["yaw"],
                "pitch":                    hs["pitch"],
                "radius":                   hs["radius"],
                "timestamp_count":          hs["timestamp_count"],
                "coverage_pct":             hs["coverage_pct"],
                "mean_conf":                hs["mean_conf"],
                "source_crop_distribution": hs["source_crop_distribution"],
            }
            for hs in hotspots
        ],
    }
    v8_init_metrics = {
        "kalman_init_frame":         kalman_init_frame,
        "warmup_candidate_count":    warmup_candidate_count,
        "warmup_consistency_score":  round(warmup_consistency_score, 4),
        "reinitialisation_count":    reinitialisation_count,
        "state_transition_counts":   state_transition_counts,
    }
    metadata = {
        "version": "v12",
        "metrics_only": metrics_only,
        "config": {
            "ball_model":           ball_model_path,
            "device":               device,
            "crop_w": CROP_W, "crop_h": CROP_H, "crop_fov_deg": CROP_FOV_DEG,
            "imgsz": YOLO_IMGSZ, "conf": YOLO_CONF,
            "scoring_weights": {"conf": W_CONF, "pos": W_POS, "pitch": W_PITCH,
                                "size": W_SIZE, "motion": W_MOTION, "edge": W_EDGE},
            "min_candidate_score":   MIN_CANDIDATE_SCORE,
            "pitch_hard_max":        PITCH_HARD_MAX,
            "hotspot_sample_count":  HOTSPOT_SAMPLE_COUNT,
            "hotspot_cluster_radius": HOTSPOT_CLUSTER_RADIUS,
            "hotspot_min_coverage":  HOTSPOT_MIN_COVERAGE,
            "hotspot_suppress_radius": HOTSPOT_SUPPRESS_RADIUS,
            "kalman_init_frames":    KALMAN_INIT_FRAMES,
            "warmup_min_chain_len":  WARMUP_MIN_CHAIN_LEN,
        },
        "detection_metrics":   detection_metrics,
        "association_metrics": association_metrics,
        "v11_bootstrap_metrics": v11_bootstrap_metrics,
        "v8_init_metrics":     v8_init_metrics,
    }

    print("=" * 70)
    print("[v12 DETECTION METRICS]")
    for k, v in detection_metrics.items():
        print(f"  {k:44s}: {v}")
    print("[v12 ASSOCIATION METRICS]")
    for k, v in association_metrics.items():
        print(f"  {k:44s}: {v}")
    print("[v12 HOTSPOT DISCOVERY METRICS]")
    for k, v in v11_bootstrap_metrics.items():
        print(f"  {k:44s}: {v}")
    print("=" * 70)

    with open(json_path, "w") as f:
        json.dump({"fps": fps, "metadata": metadata,
                   "frames": tracking_data, "swap_events": swap_events}, f, indent=2)
    print(f"[done] tracking.json → {json_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",        default="equirect_trim.mp4")
    parser.add_argument("--output",       default="tracked.mp4")
    parser.add_argument("--json",         default="tracking.json")
    parser.add_argument("--ball-model",   default="models/football-ball-detection.pt")
    parser.add_argument("--person-model", default="yolov8s.pt")
    parser.add_argument("--metrics-only", action="store_true",
                        help="Skip render, produce tracking.json only (~5x faster)")
    args = parser.parse_args()

    run_tracker(
        equirect_path=args.input,
        output_path=args.output,
        json_path=args.json,
        ball_model_path=args.ball_model,
        person_model_path=args.person_model,
        metrics_only=args.metrics_only,
    )
