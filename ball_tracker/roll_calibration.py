#!/usr/bin/env python3
"""
FFA 360 Roll Calibration — v1
================================
Renders a single wide-overview frame at 5 roll offsets and outputs a
side-by-side montage JPG so you can pick the value where pitch lines
look level.

Usage:
  python3 roll_calibration.py \\
    --input equirect_trim.mp4 \\
    --frame 800 \\
    --yaw 0 --pitch 5 --fov 120 \\
    --rolls -8,-4,0,4,8 \\
    --output roll_calibration.jpg
"""

import argparse
import math
import os

import cv2
import numpy as np

THUMB_W = 960
THUMB_H = 540
LABEL_H = 40
FONT    = cv2.FONT_HERSHEY_SIMPLEX
FS      = 0.9
FT      = 2


def extract_frame(video_path, frame_idx):
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps   = cap.get(cv2.CAP_PROP_FPS)
    print(f"[roll_calib] {total} frames @ {fps:.2f} fps")
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ret, frame = cap.read()
    cap.release()
    if not ret:
        raise RuntimeError(f"Could not read frame {frame_idx} from {video_path}")
    print(f"[roll_calib] Extracted frame {frame_idx}")
    return frame


def extract_crop_with_roll(equirect, yaw_deg, pitch_deg, roll_deg, fov_deg, out_w, out_h):
    """
    Perspective crop from equirectangular with yaw, pitch, and roll.
    Roll rotates the virtual camera around its optical axis (horizon levelling).
    Positive roll = clockwise rotation of the camera = counter-clockwise correction.
    """
    h_eq, w_eq = equirect.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))

    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)

    # Output pixel → ray in camera space
    rx = (xv - out_w / 2.0) / f
    ry = -(yv - out_h / 2.0) / f
    rz = np.ones_like(rx)

    # Apply roll (rotation around z / optical axis)
    cr = math.cos(math.radians(roll_deg))
    sr = math.sin(math.radians(roll_deg))
    rx_r = cr * rx - sr * ry
    ry_r = sr * rx + cr * ry
    rz_r = rz

    # Normalise
    norm = np.sqrt(rx_r**2 + ry_r**2 + rz_r**2)
    rx_r, ry_r, rz_r = rx_r / norm, ry_r / norm, rz_r / norm

    # Apply yaw (rotation around world Y)
    cy = math.radians(yaw_deg)
    wx  =  math.cos(cy) * rx_r + math.sin(cy) * rz_r
    wy  =  ry_r
    wz  = -math.sin(cy) * rx_r + math.cos(cy) * rz_r

    # Apply pitch (rotation around world X)
    cp = math.radians(pitch_deg)
    wx2 = wx
    wy2 =  math.cos(cp) * wy - math.sin(cp) * wz
    wz2 =  math.sin(cp) * wy + math.cos(cp) * wz

    yaw_map   = np.arctan2(wx2, wz2)
    pitch_map = np.arcsin(np.clip(wy2, -1, 1))

    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq

    return cv2.remap(equirect,
                     map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_WRAP)


def make_label(text, w):
    bar = np.zeros((LABEL_H, w, 3), dtype=np.uint8)
    tw, th = cv2.getTextSize(text, FONT, FS, FT)[0]
    x = (w - tw) // 2
    y = (LABEL_H + th) // 2
    cv2.putText(bar, text, (x+1, y+1), FONT, FS, (0,0,0),   FT+1, cv2.LINE_AA)
    cv2.putText(bar, text, (x,   y),   FONT, FS, (255,255,255), FT, cv2.LINE_AA)
    return bar


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",   required=True)
    parser.add_argument("--frame",   type=int,   default=800)
    parser.add_argument("--yaw",     type=float, default=0.0)
    parser.add_argument("--pitch",   type=float, default=5.0)
    parser.add_argument("--fov",     type=float, default=120.0)
    parser.add_argument("--rolls",   default="-8,-4,0,4,8")
    parser.add_argument("--output",  default="roll_calibration.jpg")
    args = parser.parse_args()

    rolls = [float(r) for r in args.rolls.split(",")]
    print(f"[roll_calib] Testing rolls: {rolls}")
    print(f"[roll_calib] Pose: yaw={args.yaw}° pitch={args.pitch}° fov={args.fov}°")

    equirect = extract_frame(args.input, args.frame)

    panels = []
    for roll in rolls:
        crop = extract_crop_with_roll(equirect, args.yaw, args.pitch, roll,
                                      args.fov, THUMB_W, THUMB_H)
        label_text = f"roll={roll:+.0f}°  (yaw={args.yaw:.0f} pitch={args.pitch:.0f} fov={args.fov:.0f})"
        label = make_label(label_text, THUMB_W)
        panel = np.vstack([label, crop])
        panels.append(panel)
        print(f"[roll_calib] Rendered roll={roll:+.0f}°")

    montage = np.hstack(panels)
    cv2.imwrite(args.output, montage, [cv2.IMWRITE_JPEG_QUALITY, 92])
    size_kb = os.path.getsize(args.output) // 1024
    print(f"[roll_calib] Saved {args.output} ({size_kb}KB)  shape={montage.shape}")


if __name__ == "__main__":
    main()
