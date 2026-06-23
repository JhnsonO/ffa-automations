#!/usr/bin/env python3
"""
micro_redetect.py — 4-frame re-detection probe

Probes: T0088 @ f952, f977, f990;  T0001 @ f14 (control)

For each probe:
  1. Read stored (yaw, pitch) from tracklets.json
  2. Extract equirect frame via ffmpeg
  3. Build Stage 1 crop (fixed crop_yaw nearest stored yaw; FoV=110°, 1280×720)
  4. Run YOLO on ALL 4 Stage-1 crops (same weights/conf/imgsz as Stage 1)
  5. Annotate Stage 1 crop:
       green X  = inverse-projected stored (yaw,pitch)
       red box  = YOLO detection bbox
       blue dot = YOLO detection centre
  6. Show 25° context crop centred on stored (yaw,pitch) with green X at centre
  7. Report angular offset: stored ↔ nearest detection across all 4 crops

Diagnostic splits:
  - Detection on ball, marker elsewhere  → coordinate mapping / serialisation bug
  - Detection off ball, marker matches   → detector false positive
  - No detection near ball               → detector miss

Usage:
  python3 micro_redetect.py [clip.mp4] [tracklets.json] [weights.pt] [out.png]

Defaults: clip.mp4  tracklets.json  football-ball-detection.pt  micro_redetect_panel.png
"""

import json
import math
import os
import subprocess
import sys
import tempfile

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ── Stage 1 geometry (exact match with stage1_candidate_gen.py) ───────────────
CROP_YAWS  = [0, 90, 180, 270]
CROP_FOV   = 110.0
CROP_W     = 1280
CROP_H     = 720
YOLO_CONF  = 0.12
YOLO_IMGSZ = 1280
BALL_CLS   = 0
FPS        = 29.97

# ── Panel layout ──────────────────────────────────────────────────────────────
THUMB_W    = 640        # Stage 1 crop thumbnail width
THUMB_H    = 360        # Stage 1 crop thumbnail height
CTX_SIZE   = 320        # context crop (square, 25° FoV)
CTX_FOV    = 25.0
GAP        = 12
TEXT_H     = 110        # text rows below each image strip
STRIP_H    = max(THUMB_H, CTX_SIZE)       # 360
ROW_H      = STRIP_H + TEXT_H + GAP       # 482
PANEL_W    = THUMB_W + GAP + CTX_SIZE     # 972
BG         = (18, 18, 18)

# ── Probes ────────────────────────────────────────────────────────────────────
PROBES = [
    ("T0088", 952,  "T0088 f952"),
    ("T0088", 977,  "T0088 f977"),
    ("T0088", 990,  "T0088 f990"),
    ("T0001", 14,   "T0001 f14 (ctrl)"),
]


# ── Geometry helpers ──────────────────────────────────────────────────────────

def extract_stage1_crop(frame_bgr, crop_yaw_deg):
    """Stage 1 crop: yaw-only rotation, FoV=110°, 1280×720. Verbatim from stage1_candidate_gen.py."""
    h_eq, w_eq = frame_bgr.shape[:2]
    f  = (CROP_W / 2.0) / math.tan(math.radians(CROP_FOV / 2.0))
    xs = np.linspace(0, CROP_W - 1, CROP_W)
    ys = np.linspace(0, CROP_H - 1, CROP_H)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - CROP_W / 2.0) / f
    ry = -(yv - CROP_H / 2.0) / f
    rz = np.ones_like(rx)
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    cy = math.radians(crop_yaw_deg)
    wx =  math.cos(cy) * rx + math.sin(cy) * rz
    wy =  ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    yaw_m   = np.arctan2(wx, wz)
    pitch_m = np.arcsin(np.clip(wy, -1.0, 1.0))
    map_x = ((yaw_m / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_m / math.pi) * h_eq
    return cv2.remap(frame_bgr,
                     map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def crop_pixel_to_yaw_pitch(px, py, crop_yaw_deg):
    """Stage 1's pixel→angular conversion. Verbatim from stage1_candidate_gen.py."""
    nx = (px - CROP_W / 2.0) / (CROP_W / 2.0)
    ny = (py - CROP_H / 2.0) / (CROP_H / 2.0)
    f  = 1.0 / math.tan(math.radians(CROP_FOV / 2.0))
    ray = np.array([nx / f, -ny / f * (CROP_W / CROP_H), 1.0])
    ray = ray / np.linalg.norm(ray)
    cy  = math.radians(crop_yaw_deg)
    Ry  = np.array([[math.cos(cy), 0, math.sin(cy)],
                    [0,            1, 0           ],
                    [-math.sin(cy),0, math.cos(cy)]])
    world = Ry @ ray
    yaw   = math.degrees(math.atan2(world[0], world[2]))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, world[1]))))
    return yaw, pitch


def yaw_pitch_to_crop_pixel(yaw_s, pitch_s, crop_yaw_deg):
    """
    Inverse of crop_pixel_to_yaw_pitch.
    Returns (px, py) in Stage 1 crop coords, or None if behind camera / out of frame.
    """
    f   = 1.0 / math.tan(math.radians(CROP_FOV / 2.0))
    cy  = math.radians(crop_yaw_deg)
    ys  = math.radians(yaw_s)
    ps  = math.radians(pitch_s)
    # World unit vector
    wx  = math.sin(ys) * math.cos(ps)
    wy  = math.sin(ps)
    wz  = math.cos(ys) * math.cos(ps)
    # Inverse Ry (R^T)
    rx  = math.cos(cy) * wx - math.sin(cy) * wz
    ryc = wy
    rz  = math.sin(cy) * wx + math.cos(cy) * wz
    if rz < 1e-4:
        return None
    # Reverse the w/h-scaled forward transform
    nx  = (rx  / rz) * f
    ny  = -(ryc / rz) * f * (CROP_H / CROP_W)
    px  = nx * (CROP_W / 2.0) + CROP_W / 2.0
    py  = ny * (CROP_H / 2.0) + CROP_H / 2.0
    margin = 5
    if not (-margin <= px <= CROP_W + margin and -margin <= py <= CROP_H + margin):
        return None
    return px, py


def angular_distance(y1, p1, y2, p2):
    dy = math.radians(y1 - y2)
    return math.degrees(math.acos(max(-1.0, min(1.0,
        math.sin(math.radians(p1)) * math.sin(math.radians(p2)) +
        math.cos(math.radians(p1)) * math.cos(math.radians(p2)) * math.cos(dy)
    ))))


def best_crop_yaw(stored_yaw):
    """Stage 1 crop whose yaw-centre is nearest to stored_yaw."""
    def yaw_diff(a, b):
        return abs(((a - b) + 180) % 360 - 180)
    return min(CROP_YAWS, key=lambda c: yaw_diff(stored_yaw, c))


def extract_context_crop(frame_bgr, yaw_deg, pitch_deg):
    """25° FoV gnomonic crop centred at (yaw,pitch) — full pitch+yaw rotation."""
    h_eq, w_eq = frame_bgr.shape[:2]
    f  = (CTX_SIZE / 2.0) / math.tan(math.radians(CTX_FOV / 2.0))
    xs = np.linspace(0, CTX_SIZE - 1, CTX_SIZE)
    ys = np.linspace(0, CTX_SIZE - 1, CTX_SIZE)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - CTX_SIZE / 2.0) / f
    ry = -(yv - CTX_SIZE / 2.0) / f
    rz = np.ones_like(rx)
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    # Pitch rotation
    p = math.radians(pitch_deg)
    cp, sp = math.cos(p), math.sin(p)
    rx2 =  rx
    ry2 =  cp * ry - sp * rz
    rz2 =  sp * ry + cp * rz
    # Yaw rotation
    y_r = math.radians(yaw_deg)
    cy_r, sy_r = math.cos(y_r), math.sin(y_r)
    wx =  cy_r * rx2 + sy_r * rz2
    wy =  ry2
    wz = -sy_r * rx2 + cy_r * rz2
    yaw_m   = np.arctan2(wx, wz)
    pitch_m = np.arcsin(np.clip(wy, -1.0, 1.0))
    map_x = ((yaw_m / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_m / math.pi) * h_eq
    return cv2.remap(frame_bgr,
                     map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


# ── YOLO ──────────────────────────────────────────────────────────────────────

def load_model(weights_path):
    from ultralytics import YOLO
    return YOLO(weights_path)


def run_detector_on_crop(model, crop_bgr, crop_yaw_deg):
    """YOLO on one Stage 1 crop. Returns list of detection dicts."""
    results = model.predict(crop_bgr, conf=YOLO_CONF, imgsz=YOLO_IMGSZ,
                             classes=[BALL_CLS], verbose=False)
    dets = []
    for r in results:
        if r.boxes is None:
            continue
        for box in r.boxes:
            conf_v = float(box.conf[0])
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0].tolist()]
            cx, cy_px = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            yaw_d, pitch_d = crop_pixel_to_yaw_pitch(cx, cy_px, crop_yaw_deg)
            dets.append({
                "conf": conf_v,
                "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                "cx": cx, "cy": cy_px,
                "yaw": yaw_d, "pitch": pitch_d,
                "crop_yaw": crop_yaw_deg,
            })
    return dets


# ── Font helper ───────────────────────────────────────────────────────────────

def load_font(sz=12):
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/freefont/FreeMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]:
        try:
            return ImageFont.truetype(p, sz)
        except Exception:
            pass
    return ImageFont.load_default()


def draw_crosshair(draw, cx, cy, size=14, color=(0, 255, 0), width=2):
    draw.line([(cx - size, cy), (cx + size, cy)], fill=color, width=width)
    draw.line([(cx, cy - size), (cx, cy + size)], fill=color, width=width)


# ── Row builder ───────────────────────────────────────────────────────────────

def build_row(frame_bgr, stored_yaw, stored_pitch,
              dets_by_crop, display_crop_yaw, label):
    """
    dets_by_crop: dict {crop_yaw_deg: [det, ...]}
    display_crop_yaw: which Stage 1 crop to show in the panel
    """
    dets_display = dets_by_crop.get(display_crop_yaw, [])
    all_dets = [d for dets in dets_by_crop.values() for d in dets]

    # ── Stage 1 crop (annotated at full res, then scaled) ─────────────────────
    s1_crop_bgr = extract_stage1_crop(frame_bgr, display_crop_yaw)
    s1_pil = Image.fromarray(cv2.cvtColor(s1_crop_bgr, cv2.COLOR_BGR2RGB))
    draw   = ImageDraw.Draw(s1_pil)
    font   = load_font(20)

    # Stored position — green crosshair
    sp = yaw_pitch_to_crop_pixel(stored_yaw, stored_pitch, display_crop_yaw)
    if sp:
        draw_crosshair(draw, sp[0], sp[1], size=22, color=(0, 255, 0), width=3)
        draw.text((sp[0] + 10, sp[1] - 26),
                  f"stored ({stored_yaw:.1f}°, {stored_pitch:.1f}°)",
                  fill=(0, 255, 80), font=font)

    # Detections on this crop — red box + blue centre
    for det in dets_display:
        x1, y1, x2, y2 = int(det["x1"]), int(det["y1"]), int(det["x2"]), int(det["y2"])
        draw.rectangle([x1, y1, x2, y2], outline=(255, 50, 50), width=3)
        draw.ellipse([det["cx"]-7, det["cy"]-7, det["cx"]+7, det["cy"]+7],
                     fill=(30, 100, 255))
        off = angular_distance(stored_yaw, stored_pitch, det["yaw"], det["pitch"])
        draw.text((x1, max(0, y1 - 24)),
                  f"conf={det['conf']:.2f}  off={off:.1f}°",
                  fill=(255, 200, 0), font=font)

    # Scale thumbnail
    thumb = s1_pil.resize((THUMB_W, THUMB_H), Image.LANCZOS)

    # ── Context crop (25°, centred on stored coords) ──────────────────────────
    ctx_bgr = extract_context_crop(frame_bgr, stored_yaw, stored_pitch)
    ctx_pil = Image.fromarray(cv2.cvtColor(ctx_bgr, cv2.COLOR_BGR2RGB))
    ctx_pil = ctx_pil.resize((CTX_SIZE, CTX_SIZE), Image.LANCZOS)
    ctx_draw = ImageDraw.Draw(ctx_pil)
    draw_crosshair(ctx_draw, CTX_SIZE // 2, CTX_SIZE // 2,
                   size=16, color=(0, 255, 0), width=2)
    ctx_draw.text((4, 4), "25° context", fill=(180, 180, 180), font=load_font(13))

    # ── Assemble row image ────────────────────────────────────────────────────
    row = Image.new("RGB", (PANEL_W, ROW_H), BG)
    row.paste(thumb, (0, 0))
    ctx_y = (STRIP_H - CTX_SIZE) // 2
    row.paste(ctx_pil, (THUMB_W + GAP, max(0, ctx_y)))

    # ── Text area ─────────────────────────────────────────────────────────────
    rdraw = ImageDraw.Draw(row)
    fhd   = load_font(17)
    fbd   = load_font(13)
    ty = STRIP_H + GAP

    rdraw.text((4, ty), label, fill=(230, 210, 60), font=fhd)
    ty += 22

    rdraw.text((4, ty),
               f"stored: yaw={stored_yaw:.2f}°  pitch={stored_pitch:.2f}°  |  "
               f"display crop: {display_crop_yaw}°",
               fill=(180, 180, 180), font=fbd)
    ty += 17

    # Nearest detection across ALL 4 crops
    if all_dets:
        nearest = min(all_dets,
                      key=lambda d: angular_distance(stored_yaw, stored_pitch, d["yaw"], d["pitch"]))
        off_n = angular_distance(stored_yaw, stored_pitch, nearest["yaw"], nearest["pitch"])
        verdict_col  = (80, 255, 80)  if off_n <= 3.0 else (255, 100, 50)
        verdict_text = "≤3° — mapping OK" if off_n <= 3.0 else f"{off_n:.1f}° — GEOMETRY or DETECTOR ISSUE"
        rdraw.text((4, ty),
                   f"YOLO ({sum(len(v) for v in dets_by_crop.values())} dets across all crops)  "
                   f"nearest: conf={nearest['conf']:.2f}  crop={nearest['crop_yaw']}°  "
                   f"yaw={nearest['yaw']:.1f}°  pitch={nearest['pitch']:.1f}°",
                   fill=(100, 200, 255), font=fbd)
        ty += 17
        rdraw.text((4, ty),
                   f"Offset stored↔nearest: {off_n:.2f}°  →  {verdict_text}",
                   fill=verdict_col, font=fbd)

        # Also list dets from non-display crops
        other_crops = [c for c in CROP_YAWS if c != display_crop_yaw and dets_by_crop.get(c)]
        if other_crops:
            ty += 17
            rdraw.text((4, ty),
                       f"Dets on other crops: " + "  |  ".join(
                           f"crop{c}°: {len(dets_by_crop[c])} dets" for c in other_crops
                       ),
                       fill=(160, 160, 160), font=fbd)
    else:
        rdraw.text((4, ty),
                   "YOLO: NO DETECTIONS on any Stage 1 crop",
                   fill=(255, 80, 80), font=fbd)

    # Legend (right side, below context crop)
    lx = THUMB_W + GAP
    ly = max(0, ctx_y) + CTX_SIZE + 4
    rdraw.text((lx, ly),      "green X  = stored coords",           fill=(0, 255, 0),   font=fbd)
    rdraw.text((lx, ly + 15), "red box  = YOLO detect",             fill=(255, 80, 80), font=fbd)
    rdraw.text((lx, ly + 30), "blue dot = detect centre",           fill=(80, 130, 255),font=fbd)
    rdraw.text((lx, ly + 45), f"Left: Stage1 crop ({display_crop_yaw}°)",
               fill=(160, 160, 160), font=fbd)
    rdraw.text((lx, ly + 60), "Right: 25° centred on stored",      fill=(160, 160, 160),font=fbd)

    return row


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    clip_path      = sys.argv[1] if len(sys.argv) > 1 else "clip.mp4"
    tracklets_path = sys.argv[2] if len(sys.argv) > 2 else "tracklets.json"
    weights_path   = sys.argv[3] if len(sys.argv) > 3 else "football-ball-detection.pt"
    out_path       = sys.argv[4] if len(sys.argv) > 4 else "micro_redetect_panel.png"

    print(f"[micro_redetect] clip={clip_path}  tracklets={tracklets_path}  weights={weights_path}")

    # Load tracklets
    with open(tracklets_path) as f:
        tmap = {t["id"]: t for t in json.load(f)["tracklets"]}
    print(f"[micro_redetect] {len(tmap)} tracklets loaded")

    # Load YOLO model
    print(f"[micro_redetect] Loading model: {weights_path}")
    model = load_model(weights_path)

    rows = []
    with tempfile.TemporaryDirectory() as tmpdir:
        for tid, target_frame, label in PROBES:
            print(f"\n── {label} ──")
            tracklet = tmap.get(tid)
            if not tracklet:
                print(f"  WARNING: {tid} not found — skipping")
                continue

            # Find nearest obs to target frame
            frames_list = tracklet.get("frames", [])
            if not frames_list:
                print(f"  WARNING: {tid} has no frames — skipping")
                continue
            obs = min(frames_list, key=lambda o: abs(o["frame"] - target_frame))
            actual_frame = obs["frame"]
            stored_yaw   = obs["yaw"]
            stored_pitch = obs["pitch"]
            delta = actual_frame - target_frame
            print(f"  obs: frame={actual_frame} (target={target_frame}, delta={delta:+d})"
                  f"  yaw={stored_yaw:.3f}°  pitch={stored_pitch:.3f}°")

            # Extract frame via ffmpeg
            t_sec = actual_frame / FPS
            frame_path = os.path.join(tmpdir, f"frame_{actual_frame}.jpg")
            subprocess.run([
                "ffmpeg", "-y",
                "-ss", f"{t_sec:.4f}",
                "-i", clip_path,
                "-frames:v", "1",
                "-q:v", "2",
                frame_path,
            ], check=True, capture_output=True)
            frame_bgr = cv2.imread(frame_path)
            if frame_bgr is None:
                print(f"  ERROR: cv2 could not read extracted frame — skipping")
                continue
            print(f"  frame: {frame_bgr.shape[1]}×{frame_bgr.shape[0]}")

            # Determine display crop (nearest to stored yaw)
            display_crop = best_crop_yaw(stored_yaw)
            print(f"  display crop_yaw={display_crop}° (stored_yaw={stored_yaw:.1f}°)")

            # Run YOLO on all 4 Stage 1 crops
            dets_by_crop = {}
            for cyaw in CROP_YAWS:
                crop_bgr = extract_stage1_crop(frame_bgr, cyaw)
                dets = run_detector_on_crop(model, crop_bgr, cyaw)
                dets_by_crop[cyaw] = dets
                if dets:
                    for d in dets:
                        off = angular_distance(stored_yaw, stored_pitch, d["yaw"], d["pitch"])
                        print(f"    crop{cyaw:4d}° | conf={d['conf']:.3f}  "
                              f"bbox_px=({d['x1']:.0f},{d['y1']:.0f})-({d['x2']:.0f},{d['y2']:.0f})  "
                              f"→ yaw={d['yaw']:.2f}°  pitch={d['pitch']:.2f}°  "
                              f"offset={off:.2f}°")
                else:
                    print(f"    crop{cyaw:4d}° | no detections")

            # Build row
            row = build_row(frame_bgr, stored_yaw, stored_pitch,
                            dets_by_crop, display_crop, label)
            rows.append(row)

    if not rows:
        print("ERROR: no rows produced")
        sys.exit(1)

    # Assemble full panel
    header_h = 52
    sep = 3
    total_h = header_h + len(rows) * ROW_H + (len(rows) - 1) * sep
    panel = Image.new("RGB", (PANEL_W, total_h), BG)
    pdraw = ImageDraw.Draw(panel)
    fhd = load_font(18)
    pdraw.text((6, 10),
               "FFA 360° Micro Re-detect Probe  |  Stage 1 Crop Re-run  |  4 Frames",
               fill=(210, 190, 80), font=fhd)
    pdraw.text((6, 32),
               f"green X=stored  red box=YOLO detect  blue dot=detect centre  "
               f"[Stage1: FoV={CROP_FOV}° conf={YOLO_CONF} imgsz={YOLO_IMGSZ}]",
               fill=(140, 140, 140), font=load_font(13))

    y = header_h
    for i, row in enumerate(rows):
        panel.paste(row, (0, y))
        y += ROW_H + sep

    panel.save(out_path, quality=92)
    print(f"\n[micro_redetect] → {out_path}  size={panel.size}")


if __name__ == "__main__":
    main()
