#!/usr/bin/env python3
"""
FFA 360 Ball Tracker
====================
Input:  equirect.mp4  (equirectangular MP4 from GoPro stitch pipeline)
Output: tracked.mp4   (16:9 virtual follow-cam centred on ball)
        tracking.json (per-frame yaw/pitch detections + smoothed track)

Pipeline:
  equirect.mp4
    → ffmpeg trim → equirect_trim.mp4
    → per frame: 4 perspective crops at yaw 0/90/180/270° (110° FOV)
    → YOLOv8 ball detection on each crop
    → map crop-pixel → yaw/pitch on sphere
    → dedupe overlapping detections
    → Kalman smoother on global yaw/pitch
    → ffmpeg render: virtual 16:9 cam centred on smoothed position
    → tracked.mp4 + tracking.json
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from filterpy.kalman import KalmanFilter
from ultralytics import YOLO

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FFMPEG = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
CROP_YAWS_DEG = [0, 90, 180, 270]      # centre yaw of each crop
CROP_FOV_DEG  = 110                     # horizontal FOV per crop
CROP_W        = 1280                    # perspective crop width (px)
CROP_H        = 720                     # perspective crop height (px)
DEDUP_THRESH_DEG = 15                   # angular distance to merge detections
CONF_THRESHOLD   = 0.25                 # YOLO confidence floor
BALL_CLASS_ID    = 32                   # COCO class 32 = sports ball
OUTPUT_W      = 1920
OUTPUT_H      = 1080
OUTPUT_FOV_DEG = 90                     # virtual cam horizontal FOV
LEAD_DEG      = 3.0                     # look slightly ahead of ball (yaw)

# Kalman: state = [yaw, pitch, dyaw, dpitch], measurement = [yaw, pitch]
def build_kalman():
    kf = KalmanFilter(dim_x=4, dim_z=2)
    dt = 1.0
    kf.F = np.array([[1, 0, dt, 0],
                     [0, 1,  0, dt],
                     [0, 0,  1,  0],
                     [0, 0,  0,  1]], dtype=float)
    kf.H = np.array([[1, 0, 0, 0],
                     [0, 1, 0, 0]], dtype=float)
    kf.R *= 5.0     # measurement noise
    kf.Q *= 0.1     # process noise
    kf.P *= 10.0
    return kf


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def crop_pixel_to_yaw_pitch(px, py, crop_yaw_deg, fov_deg, w, h):
    """
    Convert pixel (px, py) in a rectilinear perspective crop to
    (yaw, pitch) in degrees on the equirectangular sphere.

    crop_yaw_deg: centre yaw of this crop in degrees (0=front, 90=right…)
    fov_deg:      horizontal field of view of the crop
    w, h:         crop dimensions in pixels
    """
    # Normalised coords in [-1, 1] range
    nx = (px - w / 2.0) / (w / 2.0)
    ny = (py - h / 2.0) / (h / 2.0)

    # Focal length in normalised units
    f = 1.0 / math.tan(math.radians(fov_deg / 2.0))

    # Ray in camera space (camera looks along +Z)
    ray = np.array([nx / f, -ny / f * (w / h), 1.0])
    ray = ray / np.linalg.norm(ray)

    # Rotate ray by crop yaw (rotation around Y axis)
    cy = math.radians(crop_yaw_deg)
    Ry = np.array([[ math.cos(cy), 0, math.sin(cy)],
                   [            0, 1,            0],
                   [-math.sin(cy), 0, math.cos(cy)]])
    world_ray = Ry @ ray

    # Convert to yaw/pitch
    yaw   = math.degrees(math.atan2(world_ray[0], world_ray[2]))
    pitch = math.degrees(math.asin(np.clip(world_ray[1], -1, 1)))
    return yaw, pitch


def angular_distance(y1, p1, y2, p2):
    """Great-circle angular distance between two yaw/pitch points (degrees)."""
    y1, p1, y2, p2 = map(math.radians, [y1, p1, y2, p2])
    dot = (math.cos(p1) * math.cos(y1) * math.cos(p2) * math.cos(y2) +
           math.cos(p1) * math.sin(y1) * math.cos(p2) * math.sin(y2) +
           math.sin(p1) * math.sin(p2))
    return math.degrees(math.acos(np.clip(dot, -1, 1)))


def dedupe_detections(detections, thresh_deg=DEDUP_THRESH_DEG):
    """
    Merge detections that are within thresh_deg of each other.
    Keep the highest-confidence one from each cluster.
    detections: list of (yaw, pitch, conf)
    """
    if not detections:
        return []
    detections = sorted(detections, key=lambda d: -d[2])
    kept = []
    for d in detections:
        too_close = False
        for k in kept:
            if angular_distance(d[0], d[1], k[0], k[1]) < thresh_deg:
                too_close = True
                break
        if not too_close:
            kept.append(d)
    return kept


# ---------------------------------------------------------------------------
# FFmpeg helpers
# ---------------------------------------------------------------------------

def trim_video(src, dst, start_sec, duration_sec):
    cmd = [
        FFMPEG, "-y",
        "-ss", str(start_sec),
        "-i", src,
        "-t", str(duration_sec),
        "-c", "copy",
        dst
    ]
    print(f"[trim] {src} → {dst} ({start_sec}s + {duration_sec}s)")
    subprocess.run(cmd, check=True)


def extract_crop_frame(equirect_frame, crop_yaw_deg, fov_deg, out_w, out_h):
    """
    Project one perspective crop from a numpy equirectangular frame.
    Uses OpenCV remap with a precomputed map for this yaw.
    """
    h_eq, w_eq = equirect_frame.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))

    # Build pixel maps
    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)

    # Normalised ray in camera space
    rx = (xv - out_w / 2.0) / f
    ry = -(yv - out_h / 2.0) / f
    rz = np.ones_like(rx)
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm

    # Rotate by yaw
    cy = math.radians(crop_yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz

    # To equirectangular pixel coords
    yaw_map   = np.arctan2(wx, wz)                         # [-π, π]
    pitch_map = np.arcsin(np.clip(wy, -1, 1))              # [-π/2, π/2]

    map_x = ((yaw_map   / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq

    map_x = map_x.astype(np.float32)
    map_y = map_y.astype(np.float32)

    crop = cv2.remap(equirect_frame, map_x, map_y,
                     interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_WRAP)
    return crop


def render_virtual_cam_frame(equirect_frame, yaw_deg, pitch_deg,
                              fov_deg, out_w, out_h):
    """Render a virtual camera view centred on (yaw_deg, pitch_deg)."""
    return extract_crop_frame(equirect_frame, yaw_deg, fov_deg, out_w, out_h)
    # Note: pitch steering not implemented in v1 (ball stays near horizon)


# ---------------------------------------------------------------------------
# Main tracking loop
# ---------------------------------------------------------------------------

def run_tracker(equirect_path, output_path, json_path,
                trim_start=120, trim_duration=120,
                model_path="yolov8n.pt"):

    work_dir = Path(output_path).parent
    # Input is already equirect_trim.mp4 from stitch step — no re-trim needed
    trimmed_path = equirect_path

    # 2. Load YOLO
    print(f"[yolo] loading {model_path}")
    model = YOLO(model_path)

    # 3. Open video
    cap = cv2.VideoCapture(trimmed_path)
    fps = cap.get(cv2.CAP_PROP_FPS)
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[video] {total_frames} frames @ {fps:.2f} fps")

    # 4. Setup output writer (pipe to ffmpeg)
    ffmpeg_writer = subprocess.Popen([
        FFMPEG, "-y",
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
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

    # 5. Kalman smoother
    kf = build_kalman()
    kf_initialised = False
    last_yaw, last_pitch = 0.0, 0.0

    tracking_data = []

    frame_idx = 0
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        # --- 4 perspective crops + YOLO ---
        all_detections = []
        for crop_yaw in CROP_YAWS_DEG:
            crop = extract_crop_frame(frame, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
            results = model(crop, verbose=False, conf=CONF_THRESHOLD,
                            classes=[BALL_CLASS_ID])
            for box in results[0].boxes:
                cx = float((box.xyxy[0][0] + box.xyxy[0][2]) / 2)
                cy = float((box.xyxy[0][1] + box.xyxy[0][3]) / 2)
                conf = float(box.conf[0])
                yaw, pitch = crop_pixel_to_yaw_pitch(
                    cx, cy, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                all_detections.append((yaw, pitch, conf))

        # --- Dedupe ---
        deduped = dedupe_detections(all_detections)

        # --- Kalman update ---
        frame_record = {"frame": frame_idx, "detections": [], "smoothed": None}

        if deduped:
            best = deduped[0]  # highest conf after dedupe
            yaw_meas, pitch_meas = best[0], best[1]
            frame_record["detections"] = [
                {"yaw": d[0], "pitch": d[1], "conf": d[2]} for d in deduped
            ]

            if not kf_initialised:
                kf.x = np.array([[yaw_meas], [pitch_meas], [0.], [0.]])
                kf_initialised = True
            else:
                kf.predict()
                kf.update(np.array([[yaw_meas], [pitch_meas]]))
        else:
            if kf_initialised:
                kf.predict()

        if kf_initialised:
            smooth_yaw   = float(kf.x[0])
            smooth_pitch = float(kf.x[1])
            last_yaw, last_pitch = smooth_yaw, smooth_pitch
            frame_record["smoothed"] = {"yaw": smooth_yaw, "pitch": smooth_pitch}
        else:
            smooth_yaw, smooth_pitch = last_yaw, last_pitch

        tracking_data.append(frame_record)

        # --- Render virtual cam ---
        cam_yaw = smooth_yaw + LEAD_DEG
        out_frame = render_virtual_cam_frame(
            frame, cam_yaw, smooth_pitch, OUTPUT_FOV_DEG, OUTPUT_W, OUTPUT_H)

        ffmpeg_writer.stdin.write(out_frame.tobytes())

        frame_idx += 1
        if frame_idx % 100 == 0:
            print(f"[tracker] frame {frame_idx}/{total_frames} | "
                  f"ball yaw={smooth_yaw:.1f}° pitch={smooth_pitch:.1f}°")

    cap.release()
    ffmpeg_writer.stdin.close()
    ffmpeg_writer.wait()
    print(f"[done] tracked.mp4 → {output_path}")

    # --- Write JSON ---
    with open(json_path, "w") as f:
        json.dump({"fps": fps, "frames": tracking_data}, f, indent=2)
    print(f"[done] tracking.json → {json_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",    default="equirect.mp4")
    parser.add_argument("--output",   default="tracked.mp4")
    parser.add_argument("--json",     default="tracking.json")
    parser.add_argument("--trim-start",    type=int, default=120)
    parser.add_argument("--trim-duration", type=int, default=120)
    parser.add_argument("--model",    default="yolov8n.pt")
    args = parser.parse_args()

    run_tracker(
        equirect_path=args.input,
        output_path=args.output,
        json_path=args.json,
        trim_start=args.trim_start,
        trim_duration=args.trim_duration,
        model_path=args.model,
    )
