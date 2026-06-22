#!/usr/bin/env python3
"""
FFA 360 — Pitch Cap Visual Audit v2
=====================================
For each of 4 frames, renders a 2-panel row:
  LEFT : equirectangular frame with candidate location (red) + tracker target (cyan) marked
  RIGHT: perspective crop centred on candidate, with candidate box (red) + tracker target (cyan)

Overlay on each panel: frame, yaw, pitch, conf, crop_id, tracker_target, state, loss_state

Frames: 300, 348, 590, 880
"""

import math, os, sys
import cv2
import numpy as np

os.environ["OPENCV_FFMPEG_READ_ATTEMPTS"] = "65536"
FFMPEG       = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
CROP_FOV_DEG = 110
CROP_W, CROP_H = 1280, 720

CASES = [
    {"frame": 300,  "yaw": -37.22, "pitch": 23.95, "conf": 0.789, "crop_yaw": 0,
     "tr_yaw": -37.58, "tr_pitch": 14.12, "state": "LOST",     "loss": "player_drift(139)"},
    {"frame": 348,  "yaw": -37.23, "pitch": 23.95, "conf": 0.811, "crop_yaw": 0,
     "tr_yaw": -76.49, "tr_pitch":  1.43, "state": "TRACKING", "loss": "extrapolating(4)"},
    {"frame": 590,  "yaw": -38.15, "pitch": 18.12, "conf": 0.756, "crop_yaw": 0,
     "tr_yaw": -45.54, "tr_pitch": 14.73, "state": "TRACKING", "loss": "extrapolating(3)"},
    {"frame": 880,  "yaw": -51.59, "pitch": 18.03, "conf": 0.757, "crop_yaw": 270,
     "tr_yaw": -53.16, "tr_pitch": 10.05, "state": "TRACKING", "loss": "extrapolating(3)"},
]

# ── Geometry helpers ──────────────────────────────────────────────────────────

def yaw_pitch_to_equirect_pixel(yaw_deg, pitch_deg, eq_w, eq_h):
    """Convert sphere coords to equirectangular pixel."""
    x = int(((yaw_deg + 180) % 360) / 360 * eq_w)
    y = int((90 - pitch_deg) / 180 * eq_h)
    x = max(0, min(eq_w - 1, x))
    y = max(0, min(eq_h - 1, y))
    return x, y

def extract_crop(eq_frame, crop_yaw_deg, fov_deg=CROP_FOV_DEG, out_w=CROP_W, out_h=CROP_H):
    h_eq, w_eq = eq_frame.shape[:2]
    f  = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
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
    return cv2.remap(eq_frame,
                     map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)

def yaw_pitch_to_crop_pixel(yaw_deg, pitch_deg, crop_yaw_deg, fov_deg=CROP_FOV_DEG, w=CROP_W, h=CROP_H):
    """Project sphere coord into perspective crop pixel. Returns (px, py) or (None, None)."""
    yaw_r   = math.radians(yaw_deg)
    pitch_r = math.radians(pitch_deg)
    wx = math.sin(yaw_r) * math.cos(pitch_r)
    wy = math.sin(pitch_r)
    wz = math.cos(yaw_r) * math.cos(pitch_r)
    cy = math.radians(crop_yaw_deg)
    rx =  math.cos(cy) * wx - math.sin(cy) * wz
    ry = wy
    rz =  math.sin(cy) * wx + math.cos(cy) * wz
    if rz <= 0.01:
        return None, None
    f  = (w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    px = rx / rz * f + w / 2.0
    py = -ry / rz * f * (w / h) + h / 2.0
    if not (0 <= px < w and 0 <= py < h):
        return None, None
    return int(px), int(py)

# ── Drawing helpers ───────────────────────────────────────────────────────────

RED    = (0,   0,   255)
CYAN   = (255, 200,   0)
WHITE  = (255, 255, 255)
BLACK  = (0,   0,     0)
YELLOW = (0,   220, 255)

def draw_marker(img, px, py, color, label=None, box_half=55, thickness=3):
    if px is None: return
    h, w = img.shape[:2]
    x1 = max(0, px - box_half); y1 = max(0, py - box_half)
    x2 = min(w, px + box_half); y2 = min(h, py + box_half)
    cv2.rectangle(img, (x1, y1), (x2, y2), color, thickness)
    cv2.circle(img,   (px, py), 7, color, -1)
    if label:
        cv2.putText(img, label, (x1, max(20, y1-8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, BLACK,  4, cv2.LINE_AA)
        cv2.putText(img, label, (x1, max(20, y1-8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.65, color,  1, cv2.LINE_AA)

def put_text_block(img, lines, x, y, scale=0.72, line_h=30):
    for line in lines:
        cv2.putText(img, line, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, BLACK,  4, cv2.LINE_AA)
        cv2.putText(img, line, (x, y), cv2.FONT_HERSHEY_SIMPLEX, scale, WHITE,  1, cv2.LINE_AA)
        y += line_h

# ── Per-case render ───────────────────────────────────────────────────────────

PANEL_W, PANEL_H = 960, 540   # each sub-panel (left=equirect, right=crop)

def render_case(eq_frame, case):
    eq_h, eq_w = eq_frame.shape[:2]

    # ── LEFT: equirectangular panel ───────────────────────────────────────
    eq_small = cv2.resize(eq_frame, (PANEL_W, PANEL_H))
    sx = PANEL_W / eq_w; sy = PANEL_H / eq_h

    # Candidate position
    cx, cy_eq = yaw_pitch_to_equirect_pixel(case["yaw"], case["pitch"], eq_w, eq_h)
    draw_marker(eq_small, int(cx*sx), int(cy_eq*sy), RED, "CAND", box_half=14, thickness=2)

    # Tracker target position
    tx, ty_eq = yaw_pitch_to_equirect_pixel(case["tr_yaw"], case["tr_pitch"], eq_w, eq_h)
    draw_marker(eq_small, int(tx*sx), int(ty_eq*sy), CYAN, "TRACKER", box_half=14, thickness=2)

    # Mark crop FOV edges roughly (just label the crop yaw)
    crop_cx, _ = yaw_pitch_to_equirect_pixel(case["crop_yaw"], 0, eq_w, eq_h)
    cv2.line(eq_small, (int(crop_cx*sx), 0), (int(crop_cx*sx), PANEL_H), (180,180,180), 1)

    put_text_block(eq_small, [
        f"Frame {case['frame']}  EQUIRECTANGULAR",
        f"RED = candidate  yaw={case['yaw']}  pitch={case['pitch']}  conf={case['conf']}",
        f"CYAN = tracker   yaw={case['tr_yaw']}  pitch={case['tr_pitch']}",
        f"state={case['state']}  {case['loss']}",
    ], 12, 32, scale=0.62, line_h=26)

    # ── RIGHT: perspective crop panel ────────────────────────────────────
    crop = extract_crop(eq_frame, case["crop_yaw"])

    # Candidate in crop
    cpx, cpy = yaw_pitch_to_crop_pixel(case["yaw"], case["pitch"], case["crop_yaw"])
    draw_marker(crop, cpx, cpy, RED, f"CAND p={case['pitch']:.1f}", box_half=55, thickness=3)

    # Tracker target in crop (may be off-screen)
    tpx, tpy = yaw_pitch_to_crop_pixel(case["tr_yaw"], case["tr_pitch"], case["crop_yaw"])
    draw_marker(crop, tpx, tpy, CYAN, f"TRACKER p={case['tr_pitch']:.1f}", box_half=40, thickness=2)

    crop_small = cv2.resize(crop, (PANEL_W, PANEL_H))
    put_text_block(crop_small, [
        f"Frame {case['frame']}  CROP yaw={case['crop_yaw']}deg",
        f"Cand: yaw={case['yaw']}  pitch={case['pitch']}  conf={case['conf']}  REJECTED: pitch>{18}",
        f"Tracker: yaw={case['tr_yaw']}  pitch={case['tr_pitch']}",
    ], 12, 32, scale=0.62, line_h=26)

    return np.hstack([eq_small, crop_small])

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--input",  default="equirect_trim.mp4")
    p.add_argument("--output", default="pitch_cap_montage.png")
    args = p.parse_args()

    rows = []
    for case in CASES:
        cap = cv2.VideoCapture(args.input, cv2.CAP_FFMPEG)
        cap.set(cv2.CAP_PROP_POS_FRAMES, case["frame"])
        ret, frame = cap.read()
        cap.release()
        if not ret:
            print(f"WARN: could not read frame {case['frame']}")
            placeholder = np.full((PANEL_H, PANEL_W*2, 3), 40, dtype=np.uint8)
            cv2.putText(placeholder, f"Frame {case['frame']} read failed",
                        (80, PANEL_H//2), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (180,180,180), 2)
            rows.append(placeholder)
            continue
        print(f"Rendering frame {case['frame']}...")
        row = render_case(frame, case)
        rows.append(row)
        print(f"  OK")

    BORDER = 6
    HEADER_H = 64
    row_w = rows[0].shape[1]

    header = np.full((HEADER_H, row_w, 3), 18, dtype=np.uint8)
    cv2.putText(header,
        "PITCH CAP VISUAL AUDIT  |  RED=rejected candidate  CYAN=tracker target  |  Question: is the red box on the football?",
        (16, 42), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (210,210,210), 2, cv2.LINE_AA)

    dividers = [np.full((BORDER, row_w, 3), 40, dtype=np.uint8)] * (len(rows)-1)
    parts = [header]
    for i, row in enumerate(rows):
        parts.append(row)
        if i < len(rows)-1:
            parts.append(np.full((BORDER, row_w, 3), 40, dtype=np.uint8))

    montage = np.vstack(parts)
    cv2.imwrite(args.output, montage)
    print(f"\nMontage: {args.output}  ({montage.shape[1]}x{montage.shape[0]})")

if __name__ == "__main__":
    main()
