#!/usr/bin/env python3
"""
FFA 360 Fallback Calibration — v1
===================================
Renders a single-frame comparison montage across fallback FOV/pitch
combinations, plus one cylindrical panoramic strip, to calibrate the
WIDE_FALLBACK view in render_segment.py.

For a given fallback frame (default: frame 1100, known LOST), renders:
  Perspective grid (3 FOV x 3 pitch = 9 crops):
    FOV:   120deg, 135deg, 150deg
    Pitch:  -5deg,   0deg,  +5deg
  Cylindrical panoramic strip (180deg horizontal, 40deg vertical band):
    Rendered via ffmpeg v360 filter

Outputs:
  calibration_montage.jpg  -- labelled grid comparison image

Usage:
  python3 render_calibration.py \
    --input equirect_trim.mp4 \
    --frame 1100 \
    --fallback-yaw 0.0 \
    --output calibration_montage.jpg
"""

import argparse
import math
import os
import subprocess
import tempfile

import cv2
import numpy as np

FFMPEG = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")

OUTPUT_W = 1920
OUTPUT_H = 1080

PERSP_FOVS    = [120, 135, 150]
PERSP_PITCHES = [-5, 0, 5]

THUMB_W = 640
THUMB_H = 360
LABEL_H = 36
FONT    = cv2.FONT_HERSHEY_SIMPLEX
FS      = 0.75
FT      = 2


def extract_frame(video_path, frame_idx):
    cap = cv2.VideoCapture(video_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ret, frame = cap.read()
    cap.release()
    if not ret:
        raise RuntimeError(f"Could not read frame {frame_idx} from {video_path}")
    return frame


def perspective_crop(equirect, yaw_deg, pitch_deg, fov_deg, out_w, out_h):
    h_eq, w_eq = equirect.shape[:2]
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
    cp = math.radians(pitch_deg)
    wy2 = math.cos(cp) * wy - math.sin(cp) * wz
    wz2 = math.sin(cp) * wy + math.cos(cp) * wz
    wx2 = wx
    yaw_map   = np.arctan2(wx2, wz2)
    pitch_map = np.arcsin(np.clip(wy2, -1, 1))
    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    return cv2.remap(equirect,
                     map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def cylindrical_crop_via_ffmpeg(equirect_path, frame_idx, yaw_deg, pitch_deg,
                                 h_fov_deg, v_fov_deg, out_w, out_h):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    t_sec = frame_idx / 29.97
    cmd = [
        FFMPEG, "-y",
        "-ss", f"{t_sec:.4f}",
        "-i", equirect_path,
        "-frames:v", "1",
        "-vf", (
            f"v360=e:flat:"
            f"yaw={yaw_deg}:pitch={pitch_deg}:roll=0:"
            f"h_fov={h_fov_deg}:v_fov={v_fov_deg}:"
            f"w={out_w}:h={out_h}:"
            f"interp=lanczos"
        ),
        "-q:v", "2",
        tmp_path
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        print(f"[calib] ffmpeg v360 failed: {result.stderr.decode()[-500:]}")
        return np.full((out_h, out_w, 3), 80, dtype=np.uint8)
    img = cv2.imread(tmp_path)
    os.unlink(tmp_path)
    return img


def make_label(text, w, h=LABEL_H, bg=(30, 30, 30)):
    panel = np.zeros((h, w, 3), dtype=np.uint8)
    panel[:] = bg
    ts = cv2.getTextSize(text, FONT, FS, FT)[0]
    x = max(4, (w - ts[0]) // 2)
    y = h - 8
    cv2.putText(panel, text, (x+1, y+1), FONT, FS, (0,0,0), FT+1, cv2.LINE_AA)
    cv2.putText(panel, text, (x, y),     FONT, FS, (255,255,255), FT, cv2.LINE_AA)
    return panel


def build_perspective_grid(equirect, fallback_yaw):
    rows = []
    for fov in PERSP_FOVS:
        cols = []
        for pitch in PERSP_PITCHES:
            crop = perspective_crop(equirect, fallback_yaw, pitch, fov, OUTPUT_W, OUTPUT_H)
            thumb = cv2.resize(crop, (THUMB_W, THUMB_H))
            label = make_label(f"PERSP  FOV={fov}deg  pitch={pitch:+d}deg  yaw={fallback_yaw:.0f}deg", THUMB_W)
            cell = np.vstack([thumb, label])
            cols.append(cell)
        rows.append(np.hstack(cols))
    return np.vstack(rows)


def build_cylindrical_panel(equirect_path, frame_idx, fallback_yaw, pitch=-5):
    CYL_W = THUMB_W * 3
    CYL_H = THUMB_H
    H_FOV = 180.0
    V_FOV = 40.0
    crop = cylindrical_crop_via_ffmpeg(
        equirect_path, frame_idx,
        yaw_deg=fallback_yaw, pitch_deg=pitch,
        h_fov_deg=H_FOV, v_fov_deg=V_FOV,
        out_w=CYL_W, out_h=CYL_H
    )
    label = make_label(
        f"CYLINDRICAL  H_FOV={H_FOV:.0f}deg  V_FOV={V_FOV:.0f}deg  pitch={pitch:+d}deg  yaw={fallback_yaw:.0f}deg  (minimal distortion)",
        CYL_W, bg=(20, 40, 20)
    )
    return np.vstack([crop, label])


def build_montage(equirect, equirect_path, frame_idx, fallback_yaw):
    header_w = THUMB_W * 3
    header = make_label(
        f"FALLBACK CALIBRATION -- Frame {frame_idx}  |  Fallback yaw={fallback_yaw:.0f}deg",
        header_w, h=50, bg=(10, 10, 60)
    )
    grid = build_perspective_grid(equirect, fallback_yaw)
    cyl  = build_cylindrical_panel(equirect_path, frame_idx, fallback_yaw)
    divider = np.full((8, THUMB_W * 3, 3), 60, dtype=np.uint8)
    return np.vstack([header, grid, divider, cyl])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input",        required=True)
    ap.add_argument("--frame",        type=int, default=1100)
    ap.add_argument("--fallback-yaw", type=float, default=0.0)
    ap.add_argument("--output",       default="calibration_montage.jpg")
    args = ap.parse_args()

    print(f"[calib] Extracting frame {args.frame} from {args.input}...")
    equirect = extract_frame(args.input, args.frame)
    print(f"[calib] Equirect shape: {equirect.shape}")
    print("[calib] Building perspective grid (9 crops)...")
    print("[calib] Building cylindrical panoramic strip (ffmpeg v360)...")
    montage = build_montage(equirect, args.input, args.frame, args.fallback_yaw)
    cv2.imwrite(args.output, montage, [cv2.IMWRITE_JPEG_QUALITY, 92])
    h, w = montage.shape[:2]
    size_kb = os.path.getsize(args.output) // 1024
    print(f"[calib] Done: {args.output}  ({w}x{h}px, {size_kb}KB)")


if __name__ == "__main__":
    main()
