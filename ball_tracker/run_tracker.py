#!/usr/bin/env python3
"""
FFA 360 Ball Tracker — v5 (Detection Baseline)
==============================================
Input:  equirect_trim.mp4
Output: tracked.mp4 + tracking.json

v5 is a DETECTION BASELINE TEST, not a visual tweak. Three changes vs v4,
made together because they constitute the correct detector setup:

  1. Ball model: stock yolov8s.pt (COCO class 32) -> Roboflow
     football-ball-detection.pt (single-class {0: 'ball'}).
  2. Confidence: 0.40 -> 0.12. The detector is now biased toward RECALL;
     the downstream motion/size gate does false-positive rejection.
  3. Inference resolution: imgsz=1280 (was default 640). The 1280x720 crops
     were being downscaled to 640 before inference, roughly halving ball pixels.

IMPORTANT model architecture note:
  The football model is single-class — class 0 is the BALL, not a person.
  Stock YOLO used class 0 = person, class 32 = ball. So v5 loads TWO models:
    - football model  -> ball detection (class 0)
    - yolov8s.pt      -> person detection (class 0) for player-centroid fallback
  Running only the football model would silently kill the player-drift fallback.

Instrumentation (printed live + written to tracking.json["metadata"]):
  model paths, model.names, device, crop resolution, imgsz, conf,
  per-frame raw ball candidates, post-dedupe candidates, gate-accepted
  candidates, confirmed-ball %, median candidate box area, median candidate
  confidence, gate-rejection count (FP proxy), median accepted size_score.

Tracker/Kalman/loss-handling logic is UNCHANGED from v4 so the comparison
isolates detection. Compare v5 vs v4 on the same 2-min clip using
confirmed_ball_pct and gate_rejected_count — not whether the camera "looks smoother".
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
# Config
# ---------------------------------------------------------------------------
FFMPEG           = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
CROP_YAWS_DEG    = [0, 90, 180, 270]
CROP_FOV_DEG     = 110
CROP_W           = 1280
CROP_H           = 720
DEDUP_THRESH_DEG = 15

# v5: detection baseline parameters
YOLO_CONF        = 0.12     # was 0.40 — bias detector toward recall
YOLO_IMGSZ       = 1280     # was default 640 — stop discarding ball pixels
BALL_CLASS_ID    = 0        # football model: class 0 == ball
PERSON_CLASS_ID  = 0        # yolov8s.pt: class 0 == person

OUTPUT_W         = 1920
OUTPUT_H         = 1080
OUTPUT_FOV_DEG   = 90
LEAD_DEG         = 3.0

# Loss handling (unchanged from v4)
LOSS_EXTRAPOLATE_FRAMES = 45
LOSS_HOLD_FRAMES        = 90
PLAYER_DRIFT_SPEED_DEG  = 0.5
REACQ_LERP_FRAMES       = 15
BALL_SIZE_HISTORY        = 30
MAHAL_ACCEPT_THRESH      = 6.0

EMA_ALPHA_TRACKING = 0.18
EMA_ALPHA_LOSS     = 0.08
MAX_EXTRAP_VELOCITY_DEG = 3.0


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
# Kalman filter (unchanged from v4)
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
# Geometry helpers (unchanged from v4)
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
    dot = (math.cos(p1) * math.cos(y1) * math.cos(p2) * math.cos(y2) +
           math.cos(p1) * math.sin(y1) * math.cos(p2) * math.sin(y2) +
           math.sin(p1) * math.sin(p2))
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
# Frame extraction (unchanged from v4)
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
# Ball size tracker (unchanged from v4)
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
        return float(np.mean(self._sizes)) if self._sizes else None

    def size_score(self, bbox_area):
        exp = self.expected_size()
        if exp is None or exp == 0:
            return 1.0
        ratio = bbox_area / exp
        return math.exp(-abs(math.log(ratio)) * 2)


# ---------------------------------------------------------------------------
# Main tracking loop
# ---------------------------------------------------------------------------
def run_tracker(equirect_path, output_path, json_path,
                trim_start=120, trim_duration=120,
                ball_model_path="models/football-ball-detection.pt",
                person_model_path="yolov8s.pt"):

    device = get_device()

    # ---- Load both models ----
    print(f"[yolo] loading BALL model {ball_model_path} on {device}")
    ball_model = YOLO(ball_model_path)
    ball_model.to(device)
    print(f"[yolo] ball model.names = {ball_model.names}")

    print(f"[yolo] loading PERSON model {person_model_path} on {device}")
    person_model = YOLO(person_model_path)
    person_model.to(device)
    print(f"[yolo] person model.names = {person_model.names}")

    # ---- Detection-baseline run header ----
    print("=" * 70)
    print("[v5 DETECTION BASELINE]")
    print(f"  ball_model   : {ball_model_path}  names={ball_model.names}")
    print(f"  person_model : {person_model_path}")
    print(f"  device       : {device}")
    print(f"  crop res     : {CROP_W}x{CROP_H}  fov={CROP_FOV_DEG}  yaws={CROP_YAWS_DEG}")
    print(f"  imgsz        : {YOLO_IMGSZ}")
    print(f"  conf         : {YOLO_CONF}")
    print(f"  ball_class   : {BALL_CLASS_ID}  person_class : {PERSON_CLASS_ID}")
    print("=" * 70)

    cap = cv2.VideoCapture(equirect_path)
    fps          = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[video] {total_frames} frames @ {fps:.2f} fps")

    ffmpeg_writer = subprocess.Popen([
        FFMPEG, "-y",
        "-f", "rawvideo", "-vcodec", "rawvideo",
        "-s", f"{OUTPUT_W}x{OUTPUT_H}",
        "-pix_fmt", "bgr24",
        "-r", str(fps),
        "-i", "pipe:0",
        "-vcodec", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        output_path
    ], stdin=subprocess.PIPE)

    kf             = build_kalman()
    kf_initialised = False
    last_yaw, last_pitch = 0.0, 0.0

    frames_since_detection = 0
    confirmed_yaw   = 0.0
    confirmed_pitch = 0.0
    prev_loss_state_was_hold = False

    reacq_lerp_remaining = 0
    reacq_target_yaw     = 0.0
    reacq_target_pitch   = 0.0

    ema_yaw   = None
    ema_pitch = None
    cam_yaw   = 0.0
    cam_pitch = 0.0

    ball_size_tracker = BallSizeTracker()
    tracking_data = []
    swap_events   = []

    # ---- Instrumentation accumulators ----
    instr = {
        "frames_total":            0,
        "frames_with_raw_ball":    0,   # >=1 raw ball candidate before dedupe
        "frames_confirmed_ball":   0,   # ball accepted into tracker this frame
        "raw_ball_candidates":     [],  # per-frame count
        "deduped_ball_candidates": [],  # per-frame count
        "all_candidate_areas":     [],  # box areas of every raw ball candidate
        "all_candidate_confs":     [],  # confs of every raw ball candidate
        "accepted_size_scores":    [],  # size_score of gate-accepted balls
        "gate_rejected_count":     0,   # detections rejected by motion/size gate (FP proxy)
    }

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # ---- Detection ----
        ball_detections   = []
        person_detections = []

        for crop_yaw in CROP_YAWS_DEG:
            crop = extract_crop_frame(frame, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)

            # Ball: football-specific model, recall-biased
            ball_res = ball_model(crop, imgsz=YOLO_IMGSZ, conf=YOLO_CONF,
                                  verbose=False, classes=[BALL_CLASS_ID], device=device)
            for box in ball_res[0].boxes:
                cx   = float((box.xyxy[0][0] + box.xyxy[0][2]) / 2)
                cy_  = float((box.xyxy[0][1] + box.xyxy[0][3]) / 2)
                conf = float(box.conf[0])
                w_box = float(box.xyxy[0][2] - box.xyxy[0][0])
                h_box = float(box.xyxy[0][3] - box.xyxy[0][1])
                yaw_d, pitch_d = crop_pixel_to_yaw_pitch(
                    cx, cy_, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                ball_detections.append((yaw_d, pitch_d, conf, w_box * h_box))
                instr["all_candidate_areas"].append(w_box * h_box)
                instr["all_candidate_confs"].append(conf)

            # Person: stock YOLOv8s for player-centroid fallback
            person_res = person_model(crop, imgsz=YOLO_IMGSZ, conf=0.40,
                                      verbose=False, classes=[PERSON_CLASS_ID], device=device)
            for box in person_res[0].boxes:
                cx   = float((box.xyxy[0][0] + box.xyxy[0][2]) / 2)
                cy_  = float((box.xyxy[0][1] + box.xyxy[0][3]) / 2)
                conf = float(box.conf[0])
                yaw_d, pitch_d = crop_pixel_to_yaw_pitch(
                    cx, cy_, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                person_detections.append((yaw_d, pitch_d, conf))

        deduped_balls   = dedupe_detections([(d[0], d[1], d[2]) for d in ball_detections])
        deduped_persons = dedupe_detections(person_detections)

        # ---- Per-frame instrumentation ----
        instr["frames_total"]            += 1
        instr["raw_ball_candidates"].append(len(ball_detections))
        instr["deduped_ball_candidates"].append(len(deduped_balls))
        if ball_detections:
            instr["frames_with_raw_ball"] += 1

        frame_record = {"frame": frame_idx, "detections": [], "smoothed": None, "loss_state": None}
        ball_seen_this_frame = bool(deduped_balls)

        if ball_seen_this_frame:
            best_ball = deduped_balls[0]
            yaw_meas, pitch_meas = best_ball[0], best_ball[1]
            ball_area = ball_detections[0][3] if ball_detections else None

            if frames_since_detection > 0 and kf_initialised:
                z     = np.array([[yaw_meas], [pitch_meas]])
                innov = z - kf.H @ kf.x
                S     = kf.H @ kf.P @ kf.H.T + kf.R
                try:
                    mahal = float(np.sqrt(innov.T @ np.linalg.inv(S) @ innov))
                except np.linalg.LinAlgError:
                    mahal = 0.0

                size_score = ball_size_tracker.size_score(ball_area) if ball_area else 1.0
                accept = mahal < MAHAL_ACCEPT_THRESH or size_score > 0.5

                if accept:
                    instr["accepted_size_scores"].append(size_score)
                    if frames_since_detection > LOSS_EXTRAPOLATE_FRAMES:
                        swap_events.append({
                            "frame": frame_idx,
                            "frames_lost": frames_since_detection,
                            "mahal_distance": round(mahal, 2),
                            "size_score": round(size_score, 2),
                            "new_yaw": round(yaw_meas, 2),
                            "new_pitch": round(pitch_meas, 2),
                        })
                        reacq_lerp_remaining = REACQ_LERP_FRAMES
                        reacq_target_yaw     = yaw_meas
                        reacq_target_pitch   = pitch_meas
                        print(f"[reacq] frame {frame_idx}: reacquired after {frames_since_detection}f "
                              f"(mahal={mahal:.1f} size={size_score:.2f})")
                else:
                    ball_seen_this_frame = False
                    instr["gate_rejected_count"] += 1
                    print(f"[reject] frame {frame_idx}: detection rejected "
                          f"(mahal={mahal:.1f} size={size_score:.2f})")
            else:
                # Gate only runs after a loss; track accepts unfiltered while tracking
                if ball_area is not None and ball_size_tracker.expected_size():
                    instr["accepted_size_scores"].append(
                        ball_size_tracker.size_score(ball_area))

            if ball_seen_this_frame:
                if not kf_initialised:
                    kf.x = np.array([[yaw_meas], [pitch_meas], [0.], [0.]])
                    kf_initialised = True
                    cam_yaw, cam_pitch = yaw_meas, pitch_meas
                else:
                    kf.predict()
                    kf.update(np.array([[yaw_meas], [pitch_meas]]))
                if ball_area:
                    ball_size_tracker.update(ball_area)
                confirmed_yaw   = float(kf.x[0])
                confirmed_pitch = float(kf.x[1])
                frames_since_detection       = 0
                prev_loss_state_was_hold     = False
                instr["frames_confirmed_ball"] += 1

        else:
            frames_since_detection += 1
            if kf_initialised:
                kf.predict()

        # ---- Camera target (unchanged from v4) ----
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
                print(f"[v4] frame {frame_idx}: hold→extrap transition — velocity reset")

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

            target_yaw   = raw_yaw
            target_pitch = raw_pitch
            loss_state   = f"extrapolating ({frames_since_detection})"
            ema_alpha    = EMA_ALPHA_LOSS

        elif frames_since_detection <= LOSS_EXTRAPOLATE_FRAMES + LOSS_HOLD_FRAMES:
            target_yaw   = confirmed_yaw
            target_pitch = confirmed_pitch
            loss_state   = f"holding ({frames_since_detection})"
            ema_alpha    = EMA_ALPHA_LOSS
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

        # ---- Reacquisition lerp ----
        if reacq_lerp_remaining > 0:
            t = 1.0 - (reacq_lerp_remaining / REACQ_LERP_FRAMES)
            target_yaw   = cam_yaw + t * (reacq_target_yaw   - cam_yaw)
            target_pitch = cam_pitch + t * (reacq_target_pitch - cam_pitch)
            reacq_lerp_remaining -= 1

        cam_yaw, cam_pitch = target_yaw, target_pitch
        last_yaw, last_pitch = cam_yaw, cam_pitch

        # ---- EMA render smoothing ----
        if ema_yaw is None:
            ema_yaw, ema_pitch = cam_yaw, cam_pitch
        else:
            ema_yaw   = ema_alpha * cam_yaw   + (1 - ema_alpha) * ema_yaw
            ema_pitch = ema_alpha * cam_pitch + (1 - ema_alpha) * ema_pitch

        frame_record["detections"] = [
            {"yaw": d[0], "pitch": d[1], "conf": d[2]} for d in deduped_balls
        ]
        frame_record["smoothed"]   = {"yaw": round(ema_yaw, 2), "pitch": round(ema_pitch, 2)}
        frame_record["loss_state"] = loss_state
        tracking_data.append(frame_record)

        # ---- Render ----
        render_yaw = ema_yaw + LEAD_DEG
        out_frame  = extract_crop_frame(frame, render_yaw, OUTPUT_FOV_DEG, OUTPUT_W, OUTPUT_H)
        ffmpeg_writer.stdin.write(out_frame.tobytes())

        frame_idx += 1
        if frame_idx % 100 == 0:
            recent_raw = instr["raw_ball_candidates"][-100:]
            print(f"[tracker] frame {frame_idx}/{total_frames} | "
                  f"yaw={ema_yaw:.1f}° pitch={ema_pitch:.1f}° | {loss_state} | "
                  f"raw_balls/100f avg={np.mean(recent_raw):.2f}")

    cap.release()
    ffmpeg_writer.stdin.close()
    ffmpeg_writer.wait()
    print(f"[done] tracked.mp4 → {output_path}")

    # ---- Compute summary metrics ----
    ft = max(instr["frames_total"], 1)
    def _median(x): return float(np.median(x)) if x else None

    metadata = {
        "version": "v5-detection-baseline",
        "config": {
            "ball_model":   ball_model_path,
            "ball_names":   ball_model.names,
            "person_model": person_model_path,
            "device":       device,
            "crop_w":       CROP_W,
            "crop_h":       CROP_H,
            "crop_fov_deg": CROP_FOV_DEG,
            "crop_yaws":    CROP_YAWS_DEG,
            "imgsz":        YOLO_IMGSZ,
            "conf":         YOLO_CONF,
            "ball_class":   BALL_CLASS_ID,
            "dedup_thresh_deg": DEDUP_THRESH_DEG,
        },
        "detection_metrics": {
            "frames_total":              instr["frames_total"],
            "frames_with_raw_ball":      instr["frames_with_raw_ball"],
            "frames_with_raw_ball_pct":  round(100.0 * instr["frames_with_raw_ball"] / ft, 2),
            "frames_confirmed_ball":     instr["frames_confirmed_ball"],
            "confirmed_ball_pct":        round(100.0 * instr["frames_confirmed_ball"] / ft, 2),
            "gate_rejected_count":       instr["gate_rejected_count"],
            "mean_raw_candidates_per_frame":     round(float(np.mean(instr["raw_ball_candidates"])), 3),
            "mean_deduped_candidates_per_frame": round(float(np.mean(instr["deduped_ball_candidates"])), 3),
            "median_candidate_box_area": _median(instr["all_candidate_areas"]),
            "median_candidate_conf":     _median(instr["all_candidate_confs"]),
            "median_accepted_size_score": _median(instr["accepted_size_scores"]),
        },
        "swap_event_count": len(swap_events),
    }

    print("=" * 70)
    print("[v5 DETECTION METRICS]")
    for k, v in metadata["detection_metrics"].items():
        print(f"  {k:36s}: {v}")
    print("=" * 70)

    with open(json_path, "w") as f:
        json.dump({"fps": fps, "metadata": metadata,
                   "frames": tracking_data, "swap_events": swap_events}, f, indent=2)
    print(f"[done] tracking.json → {json_path} "
          f"(confirmed_ball={metadata['detection_metrics']['confirmed_ball_pct']}% "
          f"raw_ball={metadata['detection_metrics']['frames_with_raw_ball_pct']}% "
          f"gate_rejected={instr['gate_rejected_count']})")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",         default="equirect.mp4")
    parser.add_argument("--output",        default="tracked.mp4")
    parser.add_argument("--json",          default="tracking.json")
    parser.add_argument("--trim-start",    type=int, default=120)
    parser.add_argument("--trim-duration", type=int, default=120)
    parser.add_argument("--ball-model",    default="models/football-ball-detection.pt")
    parser.add_argument("--person-model",  default="yolov8s.pt")
    args = parser.parse_args()

    run_tracker(
        equirect_path=args.input,
        output_path=args.output,
        json_path=args.json,
        trim_start=args.trim_start,
        trim_duration=args.trim_duration,
        ball_model_path=args.ball_model,
        person_model_path=args.person_model,
    )
