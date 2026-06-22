#!/usr/bin/env python3
"""
FFA 360 Fallback Calibration — v2
===================================
Cylindrical-only calibration sweep.
Tests yaw x pitch combinations for the WIDE_FALLBACK panoramic overview.

Projection methods (both included for comparison):
  EQUIRECT BAND  — pure numpy crop of the equirectangular source at target
                   yaw/pitch. Zero distortion. Widest possible horizontal
                   coverage. Output is a horizontal strip.
  CYLINDRICAL    — ffmpeg v360=e:c. True cylindrical projection. Slight
                   vertical compression at edges vs equirect, but correct
                   for side-on pitch overview.

Grid: 3 yaw values x 3 pitch values = 9 panels, shown as two rows:
  Row A: equirect band crops
  Row B: cylindrical crops (via ffmpeg v360=e:c)

Params:
  --yaws   comma-separated yaw centres  (default: -20,0,20)
  --pitches comma-separated pitches     (default: -5,0,5)
  --h-fov  horizontal angular span for cylindrical (default: 160)
  --v-fov  vertical angular span for cylindrical   (default: 45)
  --frame  frame index to sample                   (default: 1100)
"""

import argparse
import math
import os
import subprocess
import tempfile

import cv2
import numpy as np

FFMPEG = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")

THUMB_W = 640
THUMB_H = 240   # shorter — panoramic strips are wide not tall
LABEL_H = 32
FONT    = cv2.FONT_HERSHEY_SIMPLEX
FS      = 0.65
FT      = 2

# Total output width = THUMB_W * n_yaws
# We support up to 5 yaws comfortably


def extract_frame(video_path, frame_idx):
    cap = cv2.VideoCapture(video_path)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps   = cap.get(cv2.CAP_PROP_FPS)
    print(f"[calib] Video: {total} frames @ {fps:.2f} fps")
    cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ret, frame = cap.read()
    cap.release()
    if not ret:
        raise RuntimeError(f"Could not read frame {frame_idx}")
    return frame


def equirect_band_crop(equirect, yaw_deg, pitch_deg, h_span_deg, v_span_deg, out_w, out_h):
    """
    Crop a rectangular band from the equirectangular image.
    Centred on (yaw_deg, pitch_deg), spanning h_span_deg x v_span_deg.
    No distortion — pure pixel mapping from equirect coordinates.
    """
    h_eq, w_eq = equirect.shape[:2]

    # Angular extent to pixel extent in equirect space
    # Equirect: x = (yaw/360 + 0.5) * w, y = (0.5 - pitch/180) * h
    cx = ((yaw_deg / 360.0) + 0.5) % 1.0 * w_eq
    cy = (0.5 - pitch_deg / 180.0) * h_eq

    half_w_px = (h_span_deg / 360.0) * w_eq / 2.0
    half_h_px = (v_span_deg / 180.0) * h_eq / 2.0

    # Build sample grid
    xs = np.linspace(cx - half_w_px, cx + half_w_px, out_w)
    ys = np.linspace(cy - half_h_px, cy + half_h_px, out_h)
    map_x, map_y = np.meshgrid(xs, ys)

    # Wrap x (yaw), clamp y (pitch)
    map_x = (map_x % w_eq).astype(np.float32)
    map_y = np.clip(map_y, 0, h_eq - 1).astype(np.float32)

    return cv2.remap(equirect, map_x, map_y,
                     interpolation=cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_WRAP)


def cylindrical_crop_ffmpeg(video_path, frame_idx, yaw_deg, pitch_deg,
                             h_fov_deg, v_fov_deg, out_w, out_h):
    """
    True cylindrical projection via ffmpeg v360=e:c.
    Extracts frame by index using -vf select filter (frame-accurate).
    """
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    # Use select filter for frame-accurate extraction (no -ss imprecision)
    vf = (
        f"select='eq(n\\,{frame_idx})',"
        f"v360=e:c:"
        f"yaw={yaw_deg}:pitch={pitch_deg}:roll=0:"
        f"h_fov={h_fov_deg}:v_fov={v_fov_deg}:"
        f"w={out_w}:h={out_h}:"
        f"interp=lanczos"
    )
    cmd = [
        FFMPEG, "-y",
        "-i", video_path,
        "-vf", vf,
        "-frames:v", "1",
        "-vsync", "0",
        "-q:v", "2",
        tmp_path
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        stderr_tail = result.stderr.decode(errors="replace")[-800:]
        print(f"[calib] ffmpeg v360 FAILED (exit={result.returncode}):\n{stderr_tail}")
        placeholder = np.zeros((out_h, out_w, 3), dtype=np.uint8)
        cv2.putText(placeholder, "ffmpeg v360 FAILED", (20, out_h//2),
                    FONT, 0.8, (0,0,255), 2, cv2.LINE_AA)
        cv2.putText(placeholder, "see workflow logs", (20, out_h//2+30),
                    FONT, 0.6, (180,180,180), 1, cv2.LINE_AA)
        return placeholder
    img = cv2.imread(tmp_path)
    os.unlink(tmp_path)
    if img is None:
        print(f"[calib] ffmpeg produced unreadable output at {tmp_path}")
        return np.zeros((out_h, out_w, 3), dtype=np.uint8)
    return img


def make_label(text, w, h=LABEL_H, bg=(30, 30, 30), color=(255,255,255)):
    panel = np.zeros((h, w, 3), dtype=np.uint8)
    panel[:] = bg
    ts = cv2.getTextSize(text, FONT, FS, FT)[0]
    x = max(4, (w - ts[0]) // 2)
    y = h - 8
    cv2.putText(panel, text, (x+1, y+1), FONT, FS, (0,0,0), FT+1, cv2.LINE_AA)
    cv2.putText(panel, text, (x, y),     FONT, FS, color, FT, cv2.LINE_AA)
    return panel


def build_montage(video_path, equirect, frame_idx, yaws, pitches, h_fov, v_fov):
    n_yaw   = len(yaws)
    n_pitch = len(pitches)
    total_w = THUMB_W * n_yaw

    sections = []

    # ---- Section A: Equirect band crops ----
    sec_label = make_label(
        f"EQUIRECT BAND CROP  h_span={h_fov}deg  v_span={v_fov}deg  (zero distortion)",
        total_w, h=40, bg=(10, 10, 60), color=(200, 220, 255)
    )
    sections.append(sec_label)

    for pitch in pitches:
        row_cells = []
        for yaw in yaws:
            crop = equirect_band_crop(equirect, yaw, pitch, h_fov, v_fov,
                                       THUMB_W, THUMB_H)
            label = make_label(
                f"EQ  yaw={yaw:+d}  pitch={pitch:+d}",
                THUMB_W, bg=(20, 20, 50)
            )
            row_cells.append(np.vstack([crop, label]))
        sections.append(np.hstack(row_cells))

    # Divider
    sections.append(np.full((6, total_w, 3), 80, dtype=np.uint8))

    # ---- Section B: Cylindrical crops ----
    sec_label2 = make_label(
        f"CYLINDRICAL  v360=e:c  h_fov={h_fov}deg  v_fov={v_fov}deg",
        total_w, h=40, bg=(10, 40, 10), color=(180, 255, 180)
    )
    sections.append(sec_label2)

    for pitch in pitches:
        row_cells = []
        for yaw in yaws:
            print(f"[calib] Cylindrical yaw={yaw} pitch={pitch}...")
            crop = cylindrical_crop_ffmpeg(video_path, frame_idx,
                                            yaw, pitch, h_fov, v_fov,
                                            THUMB_W, THUMB_H)
            label = make_label(
                f"CYL  yaw={yaw:+d}  pitch={pitch:+d}",
                THUMB_W, bg=(10, 30, 10)
            )
            row_cells.append(np.vstack([crop, label]))
        sections.append(np.hstack(row_cells))

    # Header
    header = make_label(
        f"CYLINDRICAL FALLBACK CALIBRATION  frame={frame_idx}  h_fov={h_fov}deg  v_fov={v_fov}deg",
        total_w, h=48, bg=(5, 5, 40), color=(255, 255, 100)
    )

    return np.vstack([header] + sections)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input",    required=True)
    ap.add_argument("--frame",    type=int, default=1100)
    ap.add_argument("--yaws",     default="-20,0,20")
    ap.add_argument("--pitches",  default="-5,0,5")
    ap.add_argument("--h-fov",    type=float, default=160.0,
                    help="Horizontal angular span in degrees")
    ap.add_argument("--v-fov",    type=float, default=45.0,
                    help="Vertical angular span in degrees")
    ap.add_argument("--output",   default="cylindrical_calibration.jpg")
    args = ap.parse_args()

    yaws    = [int(x) for x in args.yaws.split(",")]
    pitches = [int(x) for x in args.pitches.split(",")]

    print(f"[calib] Frame={args.frame}  yaws={yaws}  pitches={pitches}")
    print(f"[calib] h_fov={args.h_fov}  v_fov={args.v_fov}")

    equirect = extract_frame(args.input, args.frame)
    print(f"[calib] Equirect shape: {equirect.shape}")

    montage = build_montage(args.input, equirect, args.frame,
                             yaws, pitches, args.h_fov, args.v_fov)

    cv2.imwrite(args.output, montage, [cv2.IMWRITE_JPEG_QUALITY, 92])
    h, w = montage.shape[:2]
    size_kb = os.path.getsize(args.output) // 1024
    print(f"[calib] Done: {args.output}  ({w}x{h}px, {size_kb}KB)")


if __name__ == "__main__":
    main()
