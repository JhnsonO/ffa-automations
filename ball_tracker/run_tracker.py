#!/usr/bin/env python3
"""
FFA 360 Ball Tracker — v4
==========================
Input:  equirect_trim.mp4
Output: tracked.mp4 + tracking.json

v4 changes vs v3:
  - GPU: YOLO now runs on CUDA if available (device='cuda'), falls back to CPU
  - Track B snap fixes:
      1. Kalman velocity cap: max 3°/frame during extrapolation (stops racing camera)
      2. Kalman velocity reset to zero on hold→extrapolating transition (stops stale velocity jumps)
      3. EMA alpha drops to 0.08 during loss states (slower/smoother camera when ball missing)
  - Model default still yolov8s; GPU makes this much faster
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
CONF_THRESHOLD   = 0.40
BALL_CLASS_ID    = 32
PERSON_CLASS_ID  = 0
OUTPUT_W         = 1920
OUTPUT_H         = 1080
OUTPUT_FOV_DEG   = 90
LEAD_DEG         = 3.0

# Loss handling
LOSS_EXTRAPOLATE_FRAMES = 45
LOSS_HOLD_FRAMES        = 90
PLAYER_DRIFT_SPEED_DEG  = 0.5
REACQ_LERP_FRAMES       = 15
BALL_SIZE_HISTORY        = 30
MAHAL_ACCEPT_THRESH      = 6.0

# v4: EMA — different alphas for tracking vs loss
EMA_ALPHA_TRACKING = 0.18   # snappier when ball is found
EMA_ALPHA_LOSS     = 0.08   # slower/smoother when ball is missing

# v4: Kalman velocity cap during extrapolation
MAX_EXTRAP_VELOCITY_DEG = 3.0   # degrees/frame max


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
# Geometry helpers
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
                model_path="yolov8s.pt"):

    device = get_device()

    print(f"[yolo] loading {model_path} on {device}")
    model = YOLO(model_path)
    # Warm up model on device
    model.to(device)

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
    prev_loss_state_was_hold = False   # v4: track hold→extrap transition

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
            results = model(crop, verbose=False, conf=CONF_THRESHOLD,
                            classes=[BALL_CLASS_ID, PERSON_CLASS_ID],
                            device=device)
            for box in results[0].boxes:
                cls  = int(box.cls[0])
                cx   = float((box.xyxy[0][0] + box.xyxy[0][2]) / 2)
                cy_  = float((box.xyxy[0][1] + box.xyxy[0][3]) / 2)
                conf = float(box.conf[0])
                yaw_d, pitch_d = crop_pixel_to_yaw_pitch(
                    cx, cy_, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                if cls == BALL_CLASS_ID:
                    w_box = float(box.xyxy[0][2] - box.xyxy[0][0])
                    h_box = float(box.xyxy[0][3] - box.xyxy[0][1])
                    ball_detections.append((yaw_d, pitch_d, conf, w_box * h_box))
                elif cls == PERSON_CLASS_ID:
                    person_detections.append((yaw_d, pitch_d, conf))

        deduped_balls   = dedupe_detections([(d[0], d[1], d[2]) for d in ball_detections])
        deduped_persons = dedupe_detections(person_detections)

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
                    print(f"[reject] frame {frame_idx}: detection rejected "
                          f"(mahal={mahal:.1f} size={size_score:.2f})")

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

        else:
            frames_since_detection += 1
            if kf_initialised:
                kf.predict()

        # ---- Camera target ----
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
            # v4: if we just left the hold state, reset Kalman velocity to zero
            if prev_loss_state_was_hold:
                kf.x[2] = 0.0
                kf.x[3] = 0.0
                prev_loss_state_was_hold = False
                print(f"[v4] frame {frame_idx}: hold→extrap transition — velocity reset")

            raw_yaw   = float(kf.x[0])
            raw_pitch = float(kf.x[1])

            # v4: velocity cap — clamp per-frame movement to MAX_EXTRAP_VELOCITY_DEG
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

        # ---- EMA render smoothing (variable alpha) ----
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
            print(f"[tracker] frame {frame_idx}/{total_frames} | "
                  f"yaw={ema_yaw:.1f}° pitch={ema_pitch:.1f}° | {loss_state}")

    cap.release()
    ffmpeg_writer.stdin.close()
    ffmpeg_writer.wait()
    print(f"[done] tracked.mp4 → {output_path}")

    with open(json_path, "w") as f:
        json.dump({"fps": fps, "frames": tracking_data, "swap_events": swap_events}, f, indent=2)
    print(f"[done] tracking.json → {json_path} ({len(swap_events)} swap events)")


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
    parser.add_argument("--model",         default="yolov8s.pt")
    args = parser.parse_args()

    run_tracker(
        equirect_path=args.input,
        output_path=args.output,
        json_path=args.json,
        trim_start=args.trim_start,
        trim_duration=args.trim_duration,
        model_path=args.model,
    )
