#!/usr/bin/env python3
"""
FFA 360 Ball Tracker — v6 (Data Association)
=============================================
Input:  equirect_trim.mp4
Output: tracking.json  (always)
        tracked.mp4    (only if --metrics-only is NOT set)

v5 result: 99.86% raw detection, 99.83% confirmed — detector is solved.
Problem:   mean 3.58 candidates/frame, gate fired only once (runs after loss only).
           Camera snapped chaotically between ~3 equally-confident "ball" candidates.

v6 fix: DATA ASSOCIATION via per-frame candidate scoring.
  Every frame with candidates:
    1. Predict next ball position from Kalman state.
    2. Score every deduped candidate:
         score = w_conf    * conf_score
               + w_pos     * position_score   (Mahalanobis vs Kalman prediction)
               + w_size    * size_score        (vs rolling median box area)
               + w_motion  * motion_score      (vs recent velocity)
               + w_edge    * edge_penalty      (penalise near crop boundary)
    3. Accept only the best candidate IF it clears MIN_CANDIDATE_SCORE.
    4. Apply hard cap: reject if yaw/pitch delta > MAX_FRAME_DELTA_DEG.
    5. Apply hysteresis: only switch candidate if new score is materially
       better than incumbent (HYSTERESIS_MARGIN).
    6. Otherwise: treat as miss, fall through to extrapolate/hold logic.

Instrumentation additions vs v5:
  continuous_gate_rejected_count  — candidates scored but below threshold
  candidate_switch_count          — times best candidate diverged from incumbent
  mean_best_candidate_score       — average score of accepted candidates
  rejection_reason_counts         — breakdown: low_score / hard_cap / hysteresis
  All written to tracking.json["metadata"]["association_metrics"]

--metrics-only flag:
  Skips render entirely. Produces tracking.json only. ~5x faster.
  Use for all association logic experiments. Render only when association looks stable.
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
# Config — candidate scoring weights
# ---------------------------------------------------------------------------
W_CONF   = 0.25   # raw detection confidence
W_POS    = 0.35   # predicted-position consistency (Mahalanobis-based)
W_SIZE   = 0.20   # size consistency vs rolling median
W_MOTION = 0.10   # short-term motion consistency
W_EDGE   = 0.10   # crop-edge penalty (inverted — close to edge = lower score)

MIN_CANDIDATE_SCORE   = 0.35   # below this → treat as miss
MAX_FRAME_DELTA_DEG   = 35.0   # hard cap: reject if yaw/pitch jumps > this
HYSTERESIS_MARGIN     = 0.08   # only switch incumbent if new score is this much better
MAHAL_SOFT_THRESH     = 4.0    # normal Mahalanobis accept
MAHAL_FAST_THRESH     = 8.0    # looser thresh when ball is moving fast
FAST_MOVEMENT_DEG     = 5.0    # velocity above which we use the looser Mahal thresh

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

# Crop-edge penalty zone (pixels from edge)
EDGE_PENALTY_ZONE = 80


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
# Kalman filter (unchanged)
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
# Geometry (unchanged)
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
# Frame extraction (unchanged)
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
# Ball size tracker (unchanged)
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
# Velocity tracker (short-term motion consistency)
# ---------------------------------------------------------------------------
class VelocityTracker:
    """Rolling mean of recent yaw/pitch deltas — used to score motion consistency."""
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
        # similarity between candidate velocity and recent mean velocity
        diff = math.sqrt((dyaw - edy)**2 + (dpitch - edp)**2)
        return math.exp(-diff * 0.3)


# ---------------------------------------------------------------------------
# Candidate scorer
# ---------------------------------------------------------------------------
def score_candidate(candidate, kf, kf_initialised, ball_size_tracker, vel_tracker,
                    prev_yaw, prev_pitch, crop_yaw_deg):
    """
    Returns (total_score, component_dict, rejection_reason_or_None).
    rejection_reason is set only for hard rejects (hard_cap).
    Score below MIN_CANDIDATE_SCORE is a soft reject — caller decides.
    """
    yaw, pitch, conf, area, px, py = candidate  # px/py = pixel coords in crop

    components = {}

    # 1. Confidence score (normalised — model outputs 0.12–1.0 range)
    components["conf"] = float(conf)

    # 2. Position consistency vs Kalman prediction
    if kf_initialised:
        pred_yaw   = float(kf.x[0])
        pred_pitch = float(kf.x[1])
        vel_mag    = math.sqrt(float(kf.x[2])**2 + float(kf.x[3])**2)

        # Hard cap: reject outright if delta exceeds MAX_FRAME_DELTA_DEG
        delta = angular_distance(yaw, pitch, pred_yaw, pred_pitch)
        # delta is returned so caller can log it before rejection
        components["_raw_delta_deg"] = delta
        if delta > MAX_FRAME_DELTA_DEG:
            return 0.0, components, "hard_cap"

        # Mahalanobis — looser when ball is moving fast
        mahal_thresh = MAHAL_FAST_THRESH if vel_mag > FAST_MOVEMENT_DEG else MAHAL_SOFT_THRESH
        z     = np.array([[yaw], [pitch]])
        innov = z - kf.H @ kf.x
        S     = kf.H @ kf.P @ kf.H.T + kf.R
        try:
            mahal = float(np.sqrt(innov.T @ np.linalg.inv(S) @ innov))
        except np.linalg.LinAlgError:
            mahal = 0.0
        # Convert to 0-1 score (lower Mahal = better)
        components["pos"] = max(0.0, 1.0 - mahal / (mahal_thresh * 2))

        # Motion consistency
        dyaw   = yaw   - prev_yaw
        dpitch = pitch - prev_pitch
        components["motion"] = vel_tracker.motion_score(dyaw, dpitch)
    else:
        components["pos"]    = 1.0
        components["motion"] = 1.0

    # 3. Size consistency
    components["size"] = ball_size_tracker.size_score(area) if area else 1.0

    # 4. Crop-edge penalty (candidates near crop edges are often seam artefacts)
    edge_dist = min(px, CROP_W - px, py, CROP_H - py)
    edge_score = min(1.0, edge_dist / EDGE_PENALTY_ZONE)
    components["edge"] = edge_score

    total = (W_CONF   * components["conf"] +
             W_POS    * components["pos"] +
             W_SIZE   * components["size"] +
             W_MOTION * components["motion"] +
             W_EDGE   * components["edge"])

    return total, components, None


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
    print("[v6 DATA ASSOCIATION]")
    print(f"  ball_model    : {ball_model_path}  names={ball_model.names}")
    print(f"  person_model  : {person_model_path}")
    print(f"  device        : {device}")
    print(f"  crop res      : {CROP_W}x{CROP_H}  fov={CROP_FOV_DEG}  yaws={CROP_YAWS_DEG}")
    print(f"  imgsz         : {YOLO_IMGSZ}   conf={YOLO_CONF}")
    print(f"  metrics_only  : {metrics_only}")
    print(f"  scoring       : conf={W_CONF} pos={W_POS} size={W_SIZE} motion={W_MOTION} edge={W_EDGE}")
    print(f"  min_score     : {MIN_CANDIDATE_SCORE}   hysteresis={HYSTERESIS_MARGIN}")
    print(f"  hard_cap_deg  : {MAX_FRAME_DELTA_DEG}")
    print("=" * 70)

    cap = cv2.VideoCapture(equirect_path)
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

    frames_since_detection     = 0
    confirmed_yaw, confirmed_pitch = 0.0, 0.0
    prev_loss_state_was_hold   = False
    incumbent_yaw, incumbent_pitch = None, None   # v6: hysteresis tracking

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
            "low_score": 0,
            "hard_cap":  0,
            "hysteresis": 0,
        },
        "raw_frame_deltas_deg": [],   # v7: all candidate deltas before hard-cap
    }

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # ---- Detection ----
        raw_ball_detections  = []   # (yaw, pitch, conf, area, px, py, crop_yaw)
        person_detections    = []

        for crop_yaw in CROP_YAWS_DEG:
            crop = extract_crop_frame(frame, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)

            ball_res = ball_model(crop, imgsz=YOLO_IMGSZ, conf=YOLO_CONF,
                                  verbose=False, classes=[BALL_CLASS_ID], device=device)
            for box in ball_res[0].boxes:
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                px, py   = (x1 + x2) / 2, (y1 + y2) / 2
                conf     = float(box.conf[0])
                area     = (x2 - x1) * (y2 - y1)
                yaw_d, pitch_d = crop_pixel_to_yaw_pitch(
                    px, py, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                raw_ball_detections.append((yaw_d, pitch_d, conf, area, px, py, crop_yaw))
                instr["all_candidate_areas"].append(area)
                instr["all_candidate_confs"].append(conf)

            person_res = person_model(crop, imgsz=YOLO_IMGSZ, conf=0.40,
                                      verbose=False, classes=[PERSON_CLASS_ID], device=device)
            for box in person_res[0].boxes:
                x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
                px, py   = (x1 + x2) / 2, (y1 + y2) / 2
                conf     = float(box.conf[0])
                yaw_d, pitch_d = crop_pixel_to_yaw_pitch(
                    px, py, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                person_detections.append((yaw_d, pitch_d, conf))

        # Dedupe on sphere coords (use first 3 fields)
        deduped_balls   = dedupe_detections(
            [(d[0], d[1], d[2]) for d in raw_ball_detections])
        deduped_persons = dedupe_detections(person_detections)

        instr["frames_total"]          += 1
        instr["raw_ball_candidates"].append(len(raw_ball_detections))
        instr["deduped_ball_candidates"].append(len(deduped_balls))
        if raw_ball_detections:
            instr["frames_with_raw_ball"] += 1

        # ---- v6: Candidate scoring (runs EVERY frame with detections) ----
        # Build full candidate list with pixel coords for edge penalty
        # Match deduped spherical coords back to raw detections for area/px/py
        def find_raw(yaw, pitch):
            best, best_d = None, 999
            for r in raw_ball_detections:
                d = angular_distance(yaw, pitch, r[0], r[1])
                if d < best_d:
                    best_d, best = d, r
            return best  # (yaw, pitch, conf, area, px, py, crop_yaw)

        best_candidate   = None
        best_score       = -1.0
        frame_record     = {"frame": frame_idx, "detections": [],
                            "smoothed": None, "loss_state": None,
                            "best_score": None, "rejection_reason": None}

        if kf_initialised:
            kf.predict()

        for yaw_d, pitch_d, conf_d in deduped_balls:
            raw = find_raw(yaw_d, pitch_d)
            if raw is None:
                continue
            area, px, py, crop_yaw = raw[3], raw[4], raw[5], raw[6]
            candidate = (yaw_d, pitch_d, conf_d, area, px, py)

            score, components, hard_reject = score_candidate(
                candidate, kf, kf_initialised, ball_size_tracker, vel_tracker,
                last_yaw, last_pitch, crop_yaw)

            if "_raw_delta_deg" in components:
                instr["raw_frame_deltas_deg"].append(components["_raw_delta_deg"])
            if hard_reject == "hard_cap":
                instr["rejection_reasons"]["hard_cap"] += 1
                instr["continuous_gate_rejected"] += 1
                continue

            if score < MIN_CANDIDATE_SCORE:
                instr["rejection_reasons"]["low_score"] += 1
                instr["continuous_gate_rejected"] += 1
                continue

            if score > best_score:
                best_score, best_candidate = score, candidate

        ball_seen_this_frame = False
        if best_candidate is not None:
            yaw_meas, pitch_meas, conf_meas, area_meas = best_candidate[:4]

            # Hysteresis: only switch if materially better than incumbent
            if (incumbent_yaw is not None and
                    angular_distance(yaw_meas, pitch_meas, incumbent_yaw, incumbent_pitch) > DEDUP_THRESH_DEG):
                # This is a candidate switch
                if best_score < (instr["best_candidate_scores"][-1]
                                 if instr["best_candidate_scores"] else 0) + HYSTERESIS_MARGIN:
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

            if not kf_initialised:
                kf.x = np.array([[yaw_meas], [pitch_meas], [0.], [0.]])
                kf_initialised = True
                cam_yaw, cam_pitch = yaw_meas, pitch_meas
                incumbent_yaw, incumbent_pitch = yaw_meas, pitch_meas
            else:
                # Update velocity tracker before Kalman update
                vel_tracker.update(yaw_meas - float(kf.x[0]),
                                   pitch_meas - float(kf.x[1]))
                kf.update(np.array([[yaw_meas], [pitch_meas]]))

            ball_size_tracker.update(area_meas)
            confirmed_yaw, confirmed_pitch = float(kf.x[0]), float(kf.x[1])
            incumbent_yaw, incumbent_pitch = yaw_meas, pitch_meas
            frames_since_detection       = 0
            prev_loss_state_was_hold     = False

            frame_record["best_score"] = round(best_score, 3)
        else:
            # No acceptable candidate — miss
            frames_since_detection += 1
            # kf.predict() already called above

        # ---- Camera target (unchanged loss logic from v4) ----
        if not kf_initialised:
            target_yaw, target_pitch = last_yaw, last_pitch
            loss_state = "uninitialised"
            ema_alpha  = EMA_ALPHA_LOSS

        elif frames_since_detection == 0:
            target_yaw   = confirmed_yaw
            target_pitch = confirmed_pitch
            loss_state   = "tracking"
            ema_alpha    = EMA_ALPHA_TRACKING

        elif frames_since_detection <= LOSS_EXTRAPOLATE_FRAMES:
            if prev_loss_state_was_hold:
                kf.x[2] = 0.0
                kf.x[3] = 0.0
                prev_loss_state_was_hold = False
            raw_yaw   = float(kf.x[0])
            raw_pitch = float(kf.x[1])
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

        frame_record["detections"] = [
            {"yaw": d[0], "pitch": d[1], "conf": d[2]} for d in deduped_balls]
        frame_record["smoothed"]   = {"yaw": round(ema_yaw, 2), "pitch": round(ema_pitch, 2)}
        frame_record["loss_state"] = loss_state
        tracking_data.append(frame_record)

        if not metrics_only and ffmpeg_writer:
            render_yaw = ema_yaw + LEAD_DEG
            out_frame  = extract_crop_frame(frame, render_yaw, OUTPUT_FOV_DEG, OUTPUT_W, OUTPUT_H)
            ffmpeg_writer.stdin.write(out_frame.tobytes())

        frame_idx += 1
        if frame_idx % 200 == 0:
            cg_pct = 100 * instr["frames_confirmed_ball"] / max(instr["frames_total"], 1)
            print(f"[tracker] frame {frame_idx}/{total_frames} | "
                  f"yaw={ema_yaw:.1f}° | {loss_state} | "
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
    deltas = instr["raw_frame_deltas_deg"]
    delta_percentiles = {
        "p50": round(float(np.percentile(deltas, 50)), 2) if deltas else None,
        "p90": round(float(np.percentile(deltas, 90)), 2) if deltas else None,
        "p95": round(float(np.percentile(deltas, 95)), 2) if deltas else None,
        "p99": round(float(np.percentile(deltas, 99)), 2) if deltas else None,
    }
    association_metrics = {
        "raw_frame_delta_deg_percentiles":  delta_percentiles,
        "continuous_gate_rejected_count":   instr["continuous_gate_rejected"],
        "candidate_switch_count":           instr["candidate_switch_count"],
        "mean_best_candidate_score":        _mean(instr["best_candidate_scores"]),
        "rejection_reason_counts":          instr["rejection_reasons"],
        "swap_event_count":                 len(swap_events),
    }
    metadata = {
        "version": "v7-delta35",
        "metrics_only": metrics_only,
        "config": {
            "ball_model":   ball_model_path, "ball_names": ball_model.names,
            "person_model": person_model_path, "device": device,
            "crop_w": CROP_W, "crop_h": CROP_H, "crop_fov_deg": CROP_FOV_DEG,
            "imgsz": YOLO_IMGSZ, "conf": YOLO_CONF,
            "scoring_weights": {"conf": W_CONF, "pos": W_POS, "size": W_SIZE,
                                "motion": W_MOTION, "edge": W_EDGE},
            "min_candidate_score": MIN_CANDIDATE_SCORE,
            "hysteresis_margin":   HYSTERESIS_MARGIN,
            "max_frame_delta_deg": MAX_FRAME_DELTA_DEG,
        },
        "detection_metrics":   detection_metrics,
        "association_metrics": association_metrics,
    }

    print("=" * 70)
    print("[v6 DETECTION METRICS]")
    for k, v in detection_metrics.items():
        print(f"  {k:40s}: {v}")
    print("[v6 ASSOCIATION METRICS]")
    for k, v in association_metrics.items():
        print(f"  {k:40s}: {v}")
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
