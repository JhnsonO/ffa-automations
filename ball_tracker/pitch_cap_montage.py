#!/usr/bin/env python3
"""
FFA 360 — Pitch Cap Visual Audit
==================================
Renders a montage of the frames where PITCH_HARD_MAX=18° rejected a candidate
that was near the tracker's expected position.

For each case:
  - Extract the source perspective crop (derived from candidate yaw → nearest crop_yaw)
  - Draw the candidate bounding box in red
  - Overlay: frame number, yaw, pitch, conf, dist_to_tracker
  - Tile into a single montage image

Cases (from detector audit):
  frame=300  yaw=-37.22  pitch=23.95  conf=0.789  crop_yaw=0
  frame=348  yaw=-37.23  pitch=23.95  conf=0.811  crop_yaw=0
  frame=590  yaw=-38.15  pitch=18.12  conf=0.756  crop_yaw=0
  frame=880  yaw=-51.59  pitch=18.03  conf=0.757  crop_yaw=270
  frame=1073 yaw=-32.19  pitch=19.05  conf=0.41   crop_yaw=0

Output: pitch_cap_montage.png
"""

import math
import os
import cv2
import numpy as np

FFMPEG     = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
CROP_FOV_DEG = 110
CROP_W       = 1280
CROP_H       = 720

# Cases to render
CASES = [
    {"frame": 300,  "yaw": -37.22, "pitch": 23.95, "conf": 0.789, "crop_yaw": 0,   "dist": 9.84},
    {"frame": 348,  "yaw": -37.23, "pitch": 23.95, "conf": 0.811, "crop_yaw": 0,   "dist": 44.15},
    {"frame": 590,  "yaw": -38.15, "pitch": 18.12, "conf": 0.756, "crop_yaw": 0,   "dist": 7.86},
    {"frame": 880,  "yaw": -51.59, "pitch": 18.03, "conf": 0.757, "crop_yaw": 270, "dist": 8.12},
    {"frame": 1073, "yaw": -32.19, "pitch": 19.05, "conf": 0.41,  "crop_yaw": 0,   "dist": 0.92},
]


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


def yaw_pitch_to_crop_pixel(yaw_deg, pitch_deg, crop_yaw_deg, fov_deg, w, h):
    """Project sphere (yaw, pitch) back to pixel in given crop."""
    # World ray from yaw/pitch
    yaw_r   = math.radians(yaw_deg)
    pitch_r = math.radians(pitch_deg)
    wx = math.sin(yaw_r) * math.cos(pitch_r)
    wy = math.sin(pitch_r)
    wz = math.cos(yaw_r) * math.cos(pitch_r)
    # Rotate by -crop_yaw to get into crop-local space
    cy = math.radians(crop_yaw_deg)
    rx =  math.cos(cy) * wx - math.sin(cy) * wz
    ry = wy
    rz =  math.sin(cy) * wx + math.cos(cy) * wz
    if rz <= 0:
        return None, None  # behind crop
    f = (w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    px = rx / rz * f + w / 2.0
    py = -ry / rz * f * (w / h) + h / 2.0
    if not (0 <= px < w and 0 <= py < h):
        return None, None
    return int(px), int(py)


def render_case(equirect_path, case, thumb_w=960, thumb_h=540):
    cap = cv2.VideoCapture(equirect_path, cv2.CAP_FFMPEG)
    cap.set(cv2.CAP_PROP_POS_FRAMES, case["frame"])
    ret, frame = cap.read()
    cap.release()
    if not ret:
        return None

    crop = extract_crop_frame(frame, case["crop_yaw"], CROP_FOV_DEG, CROP_W, CROP_H)

    # Project candidate yaw/pitch back to pixel
    px, py = yaw_pitch_to_crop_pixel(
        case["yaw"], case["pitch"], case["crop_yaw"], CROP_FOV_DEG, CROP_W, CROP_H
    )

    # Draw candidate marker
    BOX = 60  # half-size of bounding box indicator
    if px is not None:
        cv2.rectangle(crop,
                      (max(0, px - BOX), max(0, py - BOX)),
                      (min(CROP_W, px + BOX), min(CROP_H, py + BOX)),
                      (0, 0, 255), 3)
        cv2.circle(crop, (px, py), 8, (0, 0, 255), -1)

    # Overlay text
    lines = [
        f"Frame {case['frame']}  crop_yaw={case['crop_yaw']}deg",
        f"yaw={case['yaw']:.2f}  pitch={case['pitch']:.2f}  conf={case['conf']:.3f}",
        f"dist_to_tracker={case['dist']:.1f}deg  REJECTED: pitch>{18.0}",
    ]
    y0 = 40
    for line in lines:
        cv2.putText(crop, line, (20, y0), cv2.FONT_HERSHEY_SIMPLEX, 1.1,
                    (0, 0, 0), 5, cv2.LINE_AA)
        cv2.putText(crop, line, (20, y0), cv2.FONT_HERSHEY_SIMPLEX, 1.1,
                    (255, 255, 255), 2, cv2.LINE_AA)
        y0 += 45

    # Thumbnail
    thumb = cv2.resize(crop, (thumb_w, thumb_h))
    return thumb


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",  default="equirect_trim.mp4")
    parser.add_argument("--output", default="pitch_cap_montage.png")
    args = parser.parse_args()

    os.environ["OPENCV_FFMPEG_READ_ATTEMPTS"] = "65536"

    thumbs = []
    for case in CASES:
        print(f"Rendering frame {case['frame']} crop_yaw={case['crop_yaw']}...")
        thumb = render_case(args.input, case)
        if thumb is not None:
            thumbs.append(thumb)
            print(f"  OK: {thumb.shape}")
        else:
            print(f"  FAILED: could not read frame {case['frame']}")
            # Placeholder
            placeholder = np.zeros((540, 960, 3), dtype=np.uint8)
            cv2.putText(placeholder, f"Frame {case['frame']} read failed",
                        (50, 270), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (128, 128, 128), 2)
            thumbs.append(placeholder)

    if not thumbs:
        print("No thumbnails generated")
        return

    # Tile: 3 top, 2 bottom (centered)
    tw, th = thumbs[0].shape[1], thumbs[0].shape[0]
    BORDER = 8
    LABEL_H = 0

    # Row 1: cases 0,1,2
    row1 = np.hstack([
        np.pad(thumbs[0], ((BORDER,BORDER),(BORDER,BORDER),(0,0)), constant_values=40),
        np.pad(thumbs[1], ((BORDER,BORDER),(BORDER,BORDER),(0,0)), constant_values=40),
        np.pad(thumbs[2], ((BORDER,BORDER),(BORDER,BORDER),(0,0)), constant_values=40),
    ])
    # Row 2: cases 3,4 centered
    pad_w = (tw + 2*BORDER) // 2
    blank = np.full((th + 2*BORDER, pad_w, 3), 40, dtype=np.uint8)
    row2 = np.hstack([
        blank,
        np.pad(thumbs[3], ((BORDER,BORDER),(BORDER,BORDER),(0,0)), constant_values=40),
        np.pad(thumbs[4], ((BORDER,BORDER),(BORDER,BORDER),(0,0)), constant_values=40),
        blank,
    ])

    # Header bar
    header = np.full((70, row1.shape[1], 3), 20, dtype=np.uint8)
    cv2.putText(header, "PITCH CAP VISUAL AUDIT — frames where pitch>18deg rejected candidate near tracker",
                (20, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.85, (200, 200, 200), 2, cv2.LINE_AA)

    montage = np.vstack([header, row1, row2])
    cv2.imwrite(args.output, montage)
    print(f"Montage written: {args.output}  ({montage.shape[1]}x{montage.shape[0]})")


if __name__ == "__main__":
    main()
