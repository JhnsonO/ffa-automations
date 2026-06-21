#!/usr/bin/env python3
"""
FFA 360 Ball Tracker — v9 (Pitch Plausibility Scoring)
=============================================
Input:  equirect_trim.mp4
Output: tracking.json  (always)
        tracked.mp4    (only if --metrics-only is NOT set)

v7 diagnosis: hard angular cap (MAX_FRAME_DELTA_DEG) is the wrong discriminator.
  - Raw candidate delta p50=76°, p90=139° — real ball detections are genuinely
    far from the Kalman prediction because the Kalman was poisoned at frame 1.
  - Tracker initialises from the first accepted candidate. If that's junk, every
    subsequent real detection looks geometrically distant → cap rejects them →
    tracker locks onto junk indefinitely.

v8 fixes:
  1. REMOVE MAX_FRAME_DELTA_DEG entirely. Mahalanobis handles temporal gating.
  2. TRACKLET WARM-UP: collect KALMAN_INIT_FRAMES=5 frames before committing
     Kalman state. Build candidate tracklets across those frames using spherical
     distance + size similarity + confidence. Initialise from the strongest chain.
  3. STATE MACHINE: UNINITIALIZED → WARMING_UP → TRACKING → UNCERTAIN → LOST
  4. NumPy deprecation fixes: kf.x[N] → kf.x[N, 0] throughout.
  5. Extended diagnostics in tracking.json.

--metrics-only flag:
  Skips render. Produces tracking.json only. ~5x faster. Use for all experiments.
"""

import argparse
import json
import math
import os
import subprocess

import cv2
import numpy as np
from filterpy.kalman import KalmanFilter
from ultralytics import YOLO

# ---------------------------------------------------------------------------
# Config — detection (unchanged from v5)
# ---------------------------------------------------------------------------
FFMPEG           = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
CROP_YAWS_DEG    = [0, 90, 180, 270]
CROP_FOV_DEG     = 110
CROP_W           = 1280
CROP_H           = 720
DEDUP_THRESH_DEG = 15
YOLO_CONF        = 0.12
YOLO_IMGSZ       = 1280
BALL_CLASS_ID    = 0   # football model: class 0 == ball
PERSON_CLASS_ID  = 0   # yolov8s.pt: class 0 == person

OUTPUT_W         = 1920
OUTPUT_H         = 1080
OUTPUT_FOV_DEG   = 90
LEAD_DEG         = 3.0

# ---------------------------------------------------------------------------
# Config — candidate scoring weights (unchanged from v6)
# ---------------------------------------------------------------------------
W_CONF   = 0.20
W_POS    = 0.25
W_PITCH  = 0.15   # v9: pitch plausibility
W_SIZE   = 0.20
W_MOTION = 0.10
W_EDGE   = 0.10

MIN_CANDIDATE_SCORE   = 0.35
# MAX_FRAME_DELTA_DEG removed in v8 — Mahalanobis handles temporal gating
HYSTERESIS_MARGIN     = 0.08
MAHAL_SOFT_THRESH     = 4.0
MAHAL_FAST_THRESH     = 8.0
FAST_MOVEMENT_DEG     = 5.0

# ---------------------------------------------------------------------------
# Config — v8 warm-up / state machine
# ---------------------------------------------------------------------------
KALMAN_INIT_FRAMES       = 5     # collect this many frames before committing Kalman
WARMUP_MIN_CHAIN_LEN     = 3     # minimum tracklet length to consider valid
WARMUP_SIZE_RATIO_THRESH = 0.3   # max log-ratio difference for size consistency
WARMUP_DIST_THRESH_DEG   = 18.0  # max spherical distance between consecutive frames in chain
UNCERTAIN_STREAK         = 3     # frames with score < UNCERTAIN_SCORE_THRESH → UNCERTAIN
UNCERTAIN_SCORE_THRESH   = 0.50

# ---------------------------------------------------------------------------
# Config — loss handling (unchanged from v4/v5)
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

# ---------------------------------------------------------------------------
# Config — v9 pitch plausibility
# ---------------------------------------------------------------------------
PITCH_SOFT_MIN = -30.0   # degrees — real ball is almost never above this pitch
PITCH_SOFT_MAX =  10.0   # degrees — real ball rarely goes above this on a football pitch
PITCH_PLAUSIBILITY_DECAY = 8.0  # degrees outside range for score to reach 0.0
PITCH_HARD_MAX           = 18.0 # v10b: hard ceiling — candidates above this are rejected before scoring

# ---------------------------------------------------------------------------
# Config — v10c static-lock breaker
# ---------------------------------------------------------------------------
STATIC_LOCK_WINDOW      = 90    # frames: rolling window to measure position spread
STATIC_LOCK_STD_MAX     = 0.4   # degrees: max std-dev of yaw OR pitch to flag static lock
STATIC_LOCK_GRACE       = 45    # frames: confirmed-ball frames before lock can trigger (real ball can pause briefly)
STATIC_BLACKLIST_RADIUS = 2.0   # degrees: exclusion zone around a detected static lock
# v10d: blacklist entries are permanent for the clip duration (permanent=True in each entry)


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
    world_ray = Ry @ ray
    yaw   = math.degrees(math.atan2(world_ray[0], world_ray[2]))
    pitch = math.degrees(math.asin(np.clip(world_ray[1], -1, 1)))
    return yaw, pitch


def angular_distance(y1, p1, y2, p2):
    y1, p1, y2, p2 = map(math.radians, [y1, p1, y2, p2])
    dot = (math.cos(p1)*math.cos(y1)*math.cos(p2)*math.cos(y2) +
           math.cos(p1)*math.sin(y1)*math.cos(p2)*math.sin(y2) +
           math.sin(p1)*math.sin(p2))
    return math.degrees(math.acos(np.clip(dot, -1, 1)))


def dedupe_detections(detections, thresh_deg=DEDUP_THRESH_DEG):
    if not detections:
        return []
    detections = sorted(detections, key=lambda d: -d[2])
    kept = []
    for d in detections:
        if not any(angular_distance(d[0], d[1], k[0], k[1]) < thresh_deg for k in kept):
            kept.append(d)
    return kept


def player_centroid_from_detections(person_detections):
    if not person_detections:
        return None
    total_conf = sum(d[2] for d in person_detections)
    if total_conf == 0:
        return None
    return (sum(d[0] * d[2] for d in person_detections) / total_conf,
            sum(d[1] * d[2] for d in person_detections) / total_conf)


# ---------------------------------------------------------------------------
# Frame extraction
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
    return cv2.remap(equirect_frame, map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


# ---------------------------------------------------------------------------
# Ball size tracker
# ---------------------------------------------------------------------------
class BallSizeTracker:
    def __init__(self, history=BALL_SIZE_HISTORY):
        self._sizes = []
        self._history = history

    def update(self, bbox_area):
        self._sizes.append(bbox_area)
        if len(self._sizes) > self._history:
            self._sizes.pop(0)

    def expected_size(self):
        return float(np.median(self._sizes)) if self._sizes else None

    def size_score(self, bbox_area):
        exp = self.expected_size()
        if exp is None or exp == 0:
            return 1.0
        ratio = bbox_area / exp
        return math.exp(-abs(math.log(ratio)) * 2)


# ---------------------------------------------------------------------------
# Velocity tracker
# ---------------------------------------------------------------------------
class VelocityTracker:
    def __init__(self, history=8):
        self._history = history
        self._dyaws   = []
        self._dpitches = []

    def update(self, dyaw, dpitch):
        self._dyaws.append(dyaw)
        self._dpitches.append(dpitch)
        if len(self._dyaws) > self._history:
            self._dyaws.pop(0)
            self._dpitches.pop(0)

    def expected_velocity(self):
        if not self._dyaws:
            return 0.0, 0.0
        return float(np.mean(self._dyaws)), float(np.mean(self._dpitches))

    def motion_score(self, dyaw, dpitch):
        edy, edp = self.expected_velocity()
        diff = math.sqrt((dyaw - edy)**2 + (dpitch - edp)**2)
        return math.exp(-diff * 0.3)


# ---------------------------------------------------------------------------
# v8: Tracklet warm-up builder
# ---------------------------------------------------------------------------
def build_warmup_tracklets(warmup_frames):
    """
    warmup_frames: list of lists, each inner list is the deduped candidates
    for that frame: [(yaw, pitch, conf, area), ...]

    Returns (best_chain, consistency_score) where best_chain is a list of
    (frame_idx, yaw, pitch, conf, area) tuples from the winning tracklet,
    or (None, 0.0) if no valid chain found.
    """
    n = len(warmup_frames)
    if n == 0:
        return None, 0.0

    # Each node: (frame_idx, candidate_idx, yaw, pitch, conf, area)
    nodes = []
    for fi, candidates in enumerate(warmup_frames):
        for ci, (yaw, pitch, conf, area) in enumerate(candidates):
            nodes.append((fi, ci, yaw, pitch, conf, area))

    # Build chains greedily: for each node as chain start, extend forward
    best_chain = None
    best_score = -1.0

    for start_node in nodes:
        chain = [start_node]
        for fi in range(start_node[0] + 1, n):
            last = chain[-1]
            best_next = None
            best_link_score = -1.0
            for candidate in warmup_frames[fi]:
                yaw, pitch, conf, area = candidate
                dist = angular_distance(last[2], last[3], yaw, pitch)
                if dist > WARMUP_DIST_THRESH_DEG:
                    continue
                # Size consistency
                if last[5] > 0 and area > 0:
                    size_ratio = abs(math.log(area / last[5]))
                    if size_ratio > WARMUP_SIZE_RATIO_THRESH * 3:
                        continue
                    size_sim = math.exp(-size_ratio * 2)
                else:
                    size_sim = 0.5
                # Link score: confidence + proximity + size similarity + pitch plausibility
                prox_score   = max(0.0, 1.0 - dist / WARMUP_DIST_THRESH_DEG)
                pitch_score  = pitch_plausibility(pitch)
                if pitch_score == 0.0:
                    # Hard-exclude impossible pitch in warm-up chain
                    print(f"[v9 warmup] excluding candidate pitch={pitch:.1f}° (plausibility=0)")
                    continue
                link_score = 0.35 * conf + 0.35 * prox_score + 0.15 * size_sim + 0.15 * pitch_score
                if link_score > best_link_score:
                    best_link_score = link_score
                    best_next = (fi, 0, yaw, pitch, conf, area)

            if best_next is not None:
                chain.append(best_next)

        if len(chain) < WARMUP_MIN_CHAIN_LEN:
            continue

        # Score the chain
        frame_coverage = len(chain) / n
        mean_conf = sum(c[4] for c in chain) / len(chain)
        # Size consistency across chain
        areas = [c[5] for c in chain if c[5] > 0]
        if len(areas) >= 2:
            log_areas = [math.log(a) for a in areas]
            size_consistency = math.exp(-float(np.std(log_areas)))
        else:
            size_consistency = 0.5
        # Motion smoothness: variance of frame-to-frame angular deltas
        if len(chain) >= 2:
            deltas = [angular_distance(chain[i][2], chain[i][3],
                                       chain[i+1][2], chain[i+1][3])
                      for i in range(len(chain)-1)]
            smoothness = math.exp(-float(np.std(deltas)) * 0.1)
        else:
            smoothness = 0.5

        chain_score = (0.35 * mean_conf +
                       0.25 * frame_coverage +
                       0.20 * size_consistency +
                       0.20 * smoothness)

        if chain_score > best_score:
            best_score = chain_score
            best_chain = chain

    return best_chain, best_score


# ---------------------------------------------------------------------------
# v9: Pitch plausibility scorer
# ---------------------------------------------------------------------------
def pitch_plausibility(pitch_deg):
    """Soft pitch penalty. Returns 1.0 inside [PITCH_SOFT_MIN, PITCH_SOFT_MAX],
    decays linearly to 0.0 at PITCH_PLAUSIBILITY_DECAY degrees outside the range.
    Candidates at pitch=48.8° → 0.0 (the v8 failure case)."""
    if PITCH_SOFT_MIN <= pitch_deg <= PITCH_SOFT_MAX:
        return 1.0
    distance = max(PITCH_SOFT_MIN - pitch_deg, pitch_deg - PITCH_SOFT_MAX)
    return max(0.0, 1.0 - distance / PITCH_PLAUSIBILITY_DECAY)


# ---------------------------------------------------------------------------
# Candidate scorer — v9: pitch plausibility added
# ---------------------------------------------------------------------------
def score_candidate(candidate, kf, kf_initialised, ball_size_tracker, vel_tracker,
                    last_yaw, last_pitch, crop_yaw_deg, tracker_state):
    """
    Returns (total_score, component_dict).
    No hard rejects in v8 — Mahalanobis handles temporal discrimination.
    """
    yaw, pitch, conf, area, px, py = candidate

    components = {}
    components["conf"] = float(conf)

    # Position consistency vs Kalman prediction
    if kf_initialised and tracker_state in (TrackerState.TRACKING, TrackerState.UNCERTAIN):
        pred_yaw   = float(kf.x[0, 0])
        pred_pitch = float(kf.x[1, 0])
        vel_mag    = math.sqrt(float(kf.x[2, 0])**2 + float(kf.x[3, 0])**2)

        mahal_thresh = MAHAL_FAST_THRESH if vel_mag > FAST_MOVEMENT_DEG else MAHAL_SOFT_THRESH
        z     = np.array([[yaw], [pitch]])
        innov = z - kf.H @ kf.x
        S     = kf.H @ kf.P @ kf.H.T + kf.R
        try:
            mahal = float(np.sqrt(innov.T @ np.linalg.inv(S) @ innov))
        except np.linalg.LinAlgError:
            mahal = 0.0
        components["pos"]    = max(0.0, 1.0 - mahal / (mahal_thresh * 2))
        components["_mahal"] = mahal

        dyaw   = yaw   - last_yaw
        dpitch = pitch - last_pitch
        components["motion"] = vel_tracker.motion_score(dyaw, dpitch)
    else:
        # Warming up or uninitialised: position and motion scores are neutral
        components["pos"]    = 1.0
        components["motion"] = 1.0
        components["_mahal"] = 0.0

    components["size"] = ball_size_tracker.size_score(area) if area else 1.0

    edge_dist = min(px, CROP_W - px, py, CROP_H - py)
    edge_score = min(1.0, edge_dist / EDGE_PENALTY_ZONE)
    components["edge"] = edge_score

    # v9: pitch plausibility
    p_score = pitch_plausibility(pitch)
    components["pitch_plausibility"] = p_score
    if p_score < 1.0:
        # Log any candidate outside the plausible pitch range
        print(f"[v9] pitch_plausibility warning: pitch={pitch:.1f}° score={p_score:.3f} "
              f"conf={conf:.3f} total_pre_pitch={(W_CONF*float(conf) + W_POS*components['pos'] + W_SIZE*components['size'] + W_MOTION*components['motion'] + W_EDGE*edge_score):.3f}")

    total = (W_CONF   * components["conf"] +
             W_POS    * components["pos"] +
             W_PITCH  * components["pitch_plausibility"] +
             W_SIZE   * components["size"] +
             W_MOTION * components["motion"] +
             W_EDGE   * components["edge"])

    return total, components


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
    print("[v9 PITCH PLAUSIBILITY — soft pitch scorer, warmup pitch gate, per-candidate logging]")
    print(f"  ball_model    : {ball_model_path}  names={ball_model.names}")
    print(f"  device        : {device}")
    print(f"  crop res      : {CROP_W}x{CROP_H}  fov={CROP_FOV_DEG}  yaws={CROP_YAWS_DEG}")
    print(f"  imgsz         : {YOLO_IMGSZ}   conf={YOLO_CONF}")
    print(f"  metrics_only  : {metrics_only}")
    print(f"  kalman_init   : {KALMAN_INIT_FRAMES} frames, min chain {WARMUP_MIN_CHAIN_LEN}")
    print(f"  scoring       : conf={W_CONF} pos={W_POS} pitch={W_PITCH} size={W_SIZE} motion={W_MOTION} edge={W_EDGE}")
    print(f"  pitch_range   : [{PITCH_SOFT_MIN}, {PITCH_SOFT_MAX}] decay={PITCH_PLAUSIBILITY_DECAY}°")
    print(f"  min_score     : {MIN_CANDIDATE_SCORE}   hysteresis={HYSTERESIS_MARGIN}")
    print(f"  hard_cap      : REMOVED (v8)")
    print("=" * 70)

    # Raise OpenCV's packet-read attempt limit — equirect MP4s may have
    # multiple streams (audio/metadata) that exhaust the default 4096 limit.
    os.environ["OPENCV_FFMPEG_READ_ATTEMPTS"] = "65536"
    cap = cv2.VideoCapture(equirect_path, cv2.CAP_FFMPEG)
    fps          = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[video] {total_frames} frames @ {fps:.2f} fps")

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

    # Warm-up buffer: list of per-frame candidate lists
    warmup_buffer  = []   # each entry: [(yaw, pitch, conf, area), ...]
    # v8 diagnostics
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

    # v10c: static-lock breaker state
    static_lock_yaw_history   = []   # rolling window of accepted yaw values
    static_lock_pitch_history = []   # rolling window of accepted pitch values
    static_lock_events        = []   # list of {start_frame, end_frame, yaw, pitch}
    static_lock_active        = False
    static_lock_start_frame   = None
    static_blacklist          = []   # list of {yaw, pitch, permanent, first_lock_frame, hit_count}
    confirmed_ball_count      = 0    # tracks frames with confirmed ball (for grace period)

    # ---- Instrumentation ----
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
            "low_score":  0,
            "hysteresis": 0,
        },
        "mahalanobis_accepted":        [],
        "mahalanobis_rejected":        [],
        "large_yaw_jump_count":        0,   # yaw delta > 30° (informational only, no reject)
        "accepted_pitches":            [],  # v9: pitch of every confirmed-ball frame
        "pitch_hard_rejections":       0,   # v10b: candidates above PITCH_HARD_MAX
        "static_blacklist_hits":        0,   # v10c: candidates rejected by blacklist
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

        # Helper: match deduped coords back to raw for area/px/py
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
        # STATE: UNINITIALIZED / WARMING_UP
        # ================================================================
        if tracker_state in (TrackerState.UNINITIALIZED, TrackerState.WARMING_UP):

            if tracker_state == TrackerState.UNINITIALIZED and deduped_balls:
                tracker_state = TrackerState.WARMING_UP
                state_transition_counts[TrackerState.WARMING_UP] += 1

            if tracker_state == TrackerState.WARMING_UP:
                # Collect this frame's candidates (yaw, pitch, conf, area)
                frame_candidates = []
                for yaw_d, pitch_d, conf_d in deduped_balls:
                    # v10b/v10c: hard pitch ceiling and blacklist — exclude before warm-up chain building
                    if pitch_d > PITCH_HARD_MAX:
                        instr["pitch_hard_rejections"] += 1
                        continue
                    raw = find_raw(yaw_d, pitch_d)
                    if raw:
                        frame_candidates.append((yaw_d, pitch_d, conf_d, raw[3]))
                warmup_buffer.append(frame_candidates)
                warmup_candidate_count += len(frame_candidates)

                if len(warmup_buffer) >= KALMAN_INIT_FRAMES:
                    # Build tracklets and pick winner
                    best_chain, chain_score = build_warmup_tracklets(warmup_buffer)

                    if best_chain is not None:
                        # Initialise Kalman from chain mean position + estimated velocity
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

                        # Seed size tracker from chain
                        for c in best_chain:
                            if c[5] > 0:
                                ball_size_tracker.update(c[5])

                        tracker_state = TrackerState.TRACKING
                        state_transition_counts[TrackerState.TRACKING] += 1
                        frames_since_detection = 0
                        print(f"[v8] Kalman initialised at frame {frame_idx} "
                              f"from chain len={len(best_chain)} score={chain_score:.3f} "
                              f"init_pos=({init_yaw:.1f}°, {init_pitch:.1f}°)")
                    else:
                        # No valid chain — reset buffer and try again
                        print(f"[v8] Warm-up failed at frame {frame_idx} "
                              f"(no chain ≥{WARMUP_MIN_CHAIN_LEN} frames) — resetting buffer")
                        warmup_buffer = []
                        reinitialisation_count += 1

            ball_seen_this_frame = False
            best_candidate = None
            best_score = 0.0

        # ================================================================
        # STATE: TRACKING / UNCERTAIN
        # ================================================================
        else:
            if kf_initialised:
                kf.predict()

            best_candidate = None
            best_score     = -1.0
            ball_seen_this_frame = False

            for yaw_d, pitch_d, conf_d in deduped_balls:
                # v10b: hard pitch ceiling — reject before scoring or Kalman update
                if pitch_d > PITCH_HARD_MAX:
                    instr["pitch_hard_rejections"] += 1
                    instr["rejection_reasons"]["pitch_hard_max"] =                         instr["rejection_reasons"].get("pitch_hard_max", 0) + 1
                    continue
                # v10d: permanent blacklist check — reject candidates near confirmed static-lock locations
                blacklisted = False
                for bl in static_blacklist:
                    if angular_distance(yaw_d, pitch_d, bl["yaw"], bl["pitch"]) < STATIC_BLACKLIST_RADIUS:
                        blacklisted = True
                        bl["hit_count"] += 1
                        instr["static_blacklist_hits"] += 1
                        instr["rejection_reasons"]["static_blacklist"] = \
                            instr["rejection_reasons"].get("static_blacklist", 0) + 1
                        print(f"[v10d] blacklist hit #{bl['hit_count']} at frame {frame_idx}: "
                              f"candidate yaw={yaw_d:.2f} pitch={pitch_d:.2f} "
                              f"(zone yaw={bl['yaw']:.2f} pitch={bl['pitch']:.2f}, "
                              f"first locked frame {bl['first_lock_frame']})")
                        break
                if blacklisted:
                    continue
                raw = find_raw(yaw_d, pitch_d)
                if raw is None:
                    continue
                area, px, py = raw[3], raw[4], raw[5]
                candidate = (yaw_d, pitch_d, conf_d, area, px, py)

                score, components = score_candidate(
                    candidate, kf, kf_initialised, ball_size_tracker, vel_tracker,
                    last_yaw, last_pitch, raw[6], tracker_state)

                mahal = components.get("_mahal", 0.0)

                # Large yaw jump counter (informational — no reject)
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
                instr["accepted_pitches"].append(pitch_meas)  # v9

                vel_tracker.update(yaw_meas - float(kf.x[0, 0]),
                                   pitch_meas - float(kf.x[1, 0]))
                kf.update(np.array([[yaw_meas], [pitch_meas]]))

                ball_size_tracker.update(area_meas)
                confirmed_yaw, confirmed_pitch = float(kf.x[0, 0]), float(kf.x[1, 0])
                incumbent_yaw, incumbent_pitch = yaw_meas, pitch_meas
                frames_since_detection = 0
                prev_loss_state_was_hold = False

                # v10c: update static-lock rolling window
                confirmed_ball_count += 1
                static_lock_yaw_history.append(yaw_meas)
                static_lock_pitch_history.append(pitch_meas)
                if len(static_lock_yaw_history) > STATIC_LOCK_WINDOW:
                    static_lock_yaw_history.pop(0)
                    static_lock_pitch_history.pop(0)

                # Check for static lock (only after grace period and full window)
                if (confirmed_ball_count >= STATIC_LOCK_GRACE and
                        len(static_lock_yaw_history) == STATIC_LOCK_WINDOW):
                    yaw_mean = sum(static_lock_yaw_history) / STATIC_LOCK_WINDOW
                    pitch_mean = sum(static_lock_pitch_history) / STATIC_LOCK_WINDOW
                    yaw_std = (sum((y - yaw_mean)**2 for y in static_lock_yaw_history) / STATIC_LOCK_WINDOW) ** 0.5
                    pitch_std = (sum((p - pitch_mean)**2 for p in static_lock_pitch_history) / STATIC_LOCK_WINDOW) ** 0.5
                    if yaw_std < STATIC_LOCK_STD_MAX and pitch_std < STATIC_LOCK_STD_MAX:
                        if not static_lock_active:
                            static_lock_active = True
                            static_lock_start_frame = frame_idx - STATIC_LOCK_WINDOW + 1
                            lock_yaw = round(yaw_mean, 2)
                            lock_pitch = round(pitch_mean, 2)
                            print(f"[v10c] STATIC LOCK detected at frame {frame_idx}: "
                                  f"yaw={lock_yaw:.2f} pitch={lock_pitch:.2f} "
                                  f"yaw_std={yaw_std:.3f} pitch_std={pitch_std:.3f}")
                            # Log event
                            static_lock_events.append({
                                "start_frame": static_lock_start_frame,
                                "end_frame": frame_idx,
                                "yaw": lock_yaw,
                                "pitch": lock_pitch,
                                "yaw_std": round(yaw_std, 4),
                                "pitch_std": round(pitch_std, 4),
                            })
                            # v10d: permanent blacklist — persists for entire clip
                            already_listed = any(
                                angular_distance(lock_yaw, lock_pitch, bl["yaw"], bl["pitch"]) < STATIC_BLACKLIST_RADIUS
                                for bl in static_blacklist
                            )
                            if not already_listed:
                                static_blacklist.append({
                                    "yaw":              lock_yaw,
                                    "pitch":            lock_pitch,
                                    "permanent":        True,
                                    "first_lock_frame": frame_idx,
                                    "hit_count":        0,
                                })
                                print(f"[v10d] PERMANENT BLACKLIST added: "
                                      f"yaw={lock_yaw:.2f} pitch={lock_pitch:.2f} "
                                      f"radius={STATIC_BLACKLIST_RADIUS}° at frame {frame_idx} "
                                      f"(total zones: {len(static_blacklist) + 1})")
                            # Invalidate Kalman — transition to UNCERTAIN
                            kf_initialised = False
                            kf = build_kalman()
                            incumbent_yaw, incumbent_pitch = None, None
                            tracker_state = TrackerState.UNCERTAIN
                            state_transition_counts[TrackerState.UNCERTAIN] += 1
                            static_lock_yaw_history.clear()
                            static_lock_pitch_history.clear()
                            confirmed_ball_count = 0
                    else:
                        static_lock_active = False

                # State transitions
                uncertain_streak = 0
                if tracker_state == TrackerState.UNCERTAIN:
                    tracker_state = TrackerState.TRACKING
                    state_transition_counts[TrackerState.TRACKING] += 1
                elif tracker_state == TrackerState.LOST:
                    tracker_state = TrackerState.TRACKING
                    state_transition_counts[TrackerState.TRACKING] += 1
                    reinitialisation_count += 1

                frame_record["best_score"] = round(best_score, 3)

            else:
                frames_since_detection += 1
                if best_score > 0 and best_score < UNCERTAIN_SCORE_THRESH:
                    uncertain_streak += 1
                else:
                    uncertain_streak = 0

                # State transitions
                if frames_since_detection > LOSS_EXTRAPOLATE_FRAMES + LOSS_HOLD_FRAMES:
                    if tracker_state != TrackerState.LOST:
                        tracker_state = TrackerState.LOST
                        state_transition_counts[TrackerState.LOST] += 1
                elif uncertain_streak >= UNCERTAIN_STREAK:
                    if tracker_state == TrackerState.TRACKING:
                        tracker_state = TrackerState.UNCERTAIN
                        state_transition_counts[TrackerState.UNCERTAIN] += 1

        # ================================================================
        # Camera target — unchanged loss logic
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
                  f"confirmed={cg_pct:.1f}% gate_rej={instr['continuous_gate_rejected']}")

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
        "swap_event_count":                 len(swap_events),
        # v9: accepted pitch distribution
        "accepted_pitch_median":            _median(instr["accepted_pitches"]),
        "accepted_pitch_p90":               _pct(instr["accepted_pitches"], 90),
        "accepted_pitch_p95":               _pct(instr["accepted_pitches"], 95),
        "accepted_pitch_above_10":          sum(1 for p in instr["accepted_pitches"] if p > 10.0),
        "accepted_pitch_above_20":          sum(1 for p in instr["accepted_pitches"] if p > 20.0),
        "accepted_pitch_above_30":          sum(1 for p in instr["accepted_pitches"] if p > 30.0),
        "pitch_hard_rejection_count":       instr["pitch_hard_rejections"],  # v10b
        # v10c: static lock breaker
        "static_lock_event_count":          len(static_lock_events),
        "static_lock_events":               static_lock_events,
        "static_blacklist_hit_count":       instr["static_blacklist_hits"],
        "permanent_blacklist_zones":        [
            {
                "yaw":              bl["yaw"],
                "pitch":            bl["pitch"],
                "first_lock_frame": bl["first_lock_frame"],
                "hit_count":        bl["hit_count"],
            }
            for bl in static_blacklist
        ],
    }
    v8_init_metrics = {
        "kalman_init_frame":         kalman_init_frame,
        "warmup_candidate_count":    warmup_candidate_count,
        "warmup_consistency_score":  round(warmup_consistency_score, 4),
        "reinitialisation_count":    reinitialisation_count,
        "state_transition_counts":   state_transition_counts,
        "frames_in_tracking":        state_transition_counts.get(TrackerState.TRACKING, 0),
        "frames_in_uncertain":       state_transition_counts.get(TrackerState.UNCERTAIN, 0),
        "frames_in_lost":            state_transition_counts.get(TrackerState.LOST, 0),
    }
    metadata = {
        "version": "v10d",
        "metrics_only": metrics_only,
        "config": {
            "ball_model":           ball_model_path,
            "ball_names":           ball_model.names,
            "device":               device,
            "crop_w": CROP_W, "crop_h": CROP_H, "crop_fov_deg": CROP_FOV_DEG,
            "imgsz": YOLO_IMGSZ, "conf": YOLO_CONF,
            "scoring_weights": {"conf": W_CONF, "pos": W_POS, "pitch": W_PITCH,
                                "size": W_SIZE, "motion": W_MOTION, "edge": W_EDGE},
            "min_candidate_score":   MIN_CANDIDATE_SCORE,
            "hysteresis_margin":     HYSTERESIS_MARGIN,
            "pitch_hard_max":        PITCH_HARD_MAX,  # v10b
            "static_lock_window":    STATIC_LOCK_WINDOW,   # v10c
            "static_lock_std_max":   STATIC_LOCK_STD_MAX,  # v10c
            "static_lock_grace":     STATIC_LOCK_GRACE,    # v10c
            "static_blacklist_radius": STATIC_BLACKLIST_RADIUS,  # v10c
            "static_blacklist_permanent": True,  # v10d: entries never expire within a clip
            "max_frame_delta_deg":   "REMOVED_v8",
            "kalman_init_frames":    KALMAN_INIT_FRAMES,
            "warmup_min_chain_len":  WARMUP_MIN_CHAIN_LEN,
        },
        "detection_metrics":   detection_metrics,
        "association_metrics": association_metrics,
        "v8_init_metrics":     v8_init_metrics,
    }

    print("=" * 70)
    print("[v8 DETECTION METRICS]")
    for k, v in detection_metrics.items():
        print(f"  {k:44s}: {v}")
    print("[v8 ASSOCIATION METRICS]")
    for k, v in association_metrics.items():
        print(f"  {k:44s}: {v}")
    print("[v8 INIT METRICS]")
    for k, v in v8_init_metrics.items():
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
    parser.add_argument("--input",          default="equirect_trim.mp4")
    parser.add_argument("--output",         default="tracked.mp4")
    parser.add_argument("--json",           default="tracking.json")
    parser.add_argument("--ball-model",     default="models/football-ball-detection.pt")
    parser.add_argument("--person-model",   default="yolov8s.pt")
    parser.add_argument("--metrics-only",   action="store_true",
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
