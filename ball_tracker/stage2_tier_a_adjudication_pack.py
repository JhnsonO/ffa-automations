#!/usr/bin/env python3
"""
stage2_tier_a_adjudication_pack.py — High-resolution human adjudication pack
for the 26 Tier A experimental anchors.

PURPOSE
-------
Each anchor page gives a reviewer enough visual information to decide whether
the detected object is a football, a body part, fence/net/mount, pitch texture,
or an unknown static object.  No automatic verdict is produced.

PAGE LAYOUT (per anchor)
------------------------
Header bar  — tracklet ID, anchor strength, displacement, obs count, hotspot frac.

Three observation rows (EARLY / MID / LATE):
  [CONTEXT 960×540]  [ZOOM 600×600]
  Context  : full 110° FoV perspective crop at native 1280×720, downscaled to 960×540.
             Green crosshair + circle at the detection point; bbox rectangle where available.
  Zoom     : 300-px square centred on the detection pixel (in the 1280×720 crop),
             upscaled 2× to 600×600 with nearest-neighbour to preserve sharpness.
             Candidate marker + bbox overlay (where available).
             Label slots: tracklet ID, frame, yaw/pitch, conf, anchor strength.

Metrics strip — frame span, first/last frame, anchor strength, static hotspot frac,
                net displacement, observation count, mean confidence.

Verdict field — checkboxes:  likely ball  |  likely false positive  |  unclear

OUTPUTS
-------
  tier_a_anchor_adjudication.pdf         — paginated high-res review pack (one page per anchor)
  tier_a_anchor_adjudication.png         — PNG contact sheet (same pages stacked)
  tier_a_anchor_adjudication.csv         — one row per anchor; verdict column blank
  tier_a_anchor_adjudication_manifest.json — anchor → source frames mapping
  tier_a_anchor_adjudication_summary.txt — concise manifest summary

INVARIANTS
----------
  - No filtering, thresholds, tracking, Stage 1/1b/2, or renderer logic is touched.
  - Crop geometry matches stage1_candidate_gen.py exactly: CROP_FOV_DEG=110, 1280×720.
  - No automatic verdicts are produced.
"""

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ── Crop geometry — MUST match stage1_candidate_gen.py ──────────────────────
CROP_FOV_DEG = 110
CROP_W       = 1280
CROP_H       = 720
CROP_YAWS    = [0, 90, 180, 270]

# ── Page geometry ────────────────────────────────────────────────────────────
# Context panel: 1280×720 native → displayed at 960×540
CTX_W, CTX_H       = 960, 540
# Zoom window in native-crop pixels (radius around detection centre)
ZOOM_HALF_PX       = 150          # 300×300 in native → upscale 2× → 600×600
ZOOM_DISPLAY_SZ    = 600          # final on-page size

PAGE_W             = CTX_W + ZOOM_DISPLAY_SZ   # 1560
HDR_H              = 58
OBS_ROW_LABEL_H    = 28
OBS_ROW_H          = CTX_H + OBS_ROW_LABEL_H   # 568
N_OBS_ROWS         = 3
METRICS_H          = 64
VERDICT_H          = 50
PAGE_H             = HDR_H + N_OBS_ROWS * OBS_ROW_H + METRICS_H + VERDICT_H

# ── Colours ──────────────────────────────────────────────────────────────────
BG          = (14, 14, 22)
WHITE       = (230, 230, 230)
DIM         = (130, 130, 145)
GREEN       = (60, 200, 80)
AMBER       = (220, 190, 50)
RED_COL     = (210, 70, 70)
HDR_GREEN   = (20, 80, 30)
HDR_AMBER   = (80, 65, 15)
HDR_RED     = (80, 20, 20)
CROSS_BGR   = (0, 255, 80)        # OpenCV BGR
BBOX_BGR    = (255, 80, 0)        # OpenCV BGR — orange
ZOOM_BORDER = (60, 60, 90)

# ── Fonts ────────────────────────────────────────────────────────────────────

def _font(size, bold=False):
    paths = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSansMono{'Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationMono-{'Bold' if bold else 'Regular'}.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ── Equirectangular → perspective projection ─────────────────────────────────

def extract_crop(equirect_bgr, crop_yaw_deg, fov_deg=CROP_FOV_DEG,
                 out_w=CROP_W, out_h=CROP_H):
    h_eq, w_eq = equirect_bgr.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(-(out_w / 2), out_w / 2, out_w)
    ys = np.linspace(-(out_h / 2), out_h / 2, out_h)
    xg, yg = np.meshgrid(xs, ys)
    zg = np.full_like(xg, f)
    norm = np.sqrt(xg**2 + yg**2 + zg**2)
    wx, wy, wz = xg / norm, -yg / norm, zg / norm
    cy = math.radians(crop_yaw_deg)
    cos_y, sin_y = math.cos(cy), math.sin(cy)
    rx =  cos_y * wx + sin_y * wz
    rz = -sin_y * wx + cos_y * wz
    wx, wz = rx, rz
    yaw_map   = np.arctan2(wx, wz)
    pitch_map = np.arcsin(np.clip(wy, -1, 1))
    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    return cv2.remap(equirect_bgr,
                     map_x.astype(np.float32),
                     map_y.astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def nearest_crop_yaw(yaw_deg):
    def adiff(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)
    return min(CROP_YAWS, key=lambda c: adiff(yaw_deg, c))


def yaw_pitch_to_pixel(yaw_deg, pitch_deg, crop_yaw_deg,
                        fov_deg=CROP_FOV_DEG, w=CROP_W, h=CROP_H):
    """Return (px, py) in the 1280×720 native crop, or None if off-frame."""
    f = (w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    ya = math.radians(yaw_deg)
    pa = math.radians(pitch_deg)
    wx_w = math.sin(ya) * math.cos(pa)
    wy_w = math.sin(pa)
    wz_w = math.cos(ya) * math.cos(pa)
    cy = math.radians(crop_yaw_deg)
    cos_y, sin_y = math.cos(-cy), math.sin(-cy)
    rx =  cos_y * wx_w + sin_y * wz_w
    rz = -sin_y * wx_w + cos_y * wz_w
    wx_w, wz_w = rx, rz
    if wz_w <= 0:
        return None
    px =  (wx_w / wz_w) * f + w / 2.0
    py = -(wy_w / wz_w) * f + h / 2.0
    if 0 <= px < w and 0 <= py < h:
        return (px, py)
    return None


# ── ffmpeg frame seek ─────────────────────────────────────────────────────────

def extract_frame(video_path, frame_idx, fps, out_path):
    ts = frame_idx / fps
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{ts:.6f}", "-i", str(video_path),
         "-vframes", "1", "-q:v", "2", str(out_path)],
        capture_output=True)
    return r.returncode == 0 and Path(out_path).exists()


# ── Bbox: crop-pixel coordinates from stored bbox_xyxy ───────────────────────

def get_bbox_in_crop(obs):
    """
    Return (x1,y1,x2,y2) in 1280×720 crop pixels, or None.

    The stored bbox_xyxy is in the crop-local pixel space from
    stage1_candidate_gen.py — use it directly.
    """
    geo = obs.get("detection_geometry") or {}
    bbox = geo.get("bbox_xyxy")
    if bbox and len(bbox) == 4 and all(v is not None for v in bbox):
        return tuple(int(round(v)) for v in bbox)
    return None


# ── Context panel (960×540) ──────────────────────────────────────────────────

def render_context_panel(crop_bgr_native, pos, bbox_crop):
    """
    Return OpenCV BGR image (CTX_W × CTX_H).
    pos  : (px, py) in 1280×720 space or None
    bbox : (x1,y1,x2,y2) in 1280×720 space or None
    """
    annotated = crop_bgr_native.copy()

    if bbox_crop:
        x1, y1, x2, y2 = bbox_crop
        cv2.rectangle(annotated, (x1, y1), (x2, y2), BBOX_BGR, 2)

    if pos:
        px, py = int(pos[0]), int(pos[1])
        r = 20
        cv2.circle(annotated, (px, py), r, CROSS_BGR, 2)
        cv2.line(annotated, (px - r - 6, py), (px + r + 6, py), CROSS_BGR, 1)
        cv2.line(annotated, (px, py - r - 6), (px, py + r + 6), CROSS_BGR, 1)
        cv2.circle(annotated, (px, py), 4, CROSS_BGR, -1)

    return cv2.resize(annotated, (CTX_W, CTX_H), interpolation=cv2.INTER_AREA)


# ── Zoom panel (600×600) ─────────────────────────────────────────────────────

def render_zoom_panel(crop_bgr_native, pos, bbox_crop, obs, fn, fn_bold, fn_small,
                      t_id, anchor_strength):
    """
    Return PIL Image (ZOOM_DISPLAY_SZ × ZOOM_DISPLAY_SZ).
    Centres on the candidate pixel; falls back to frame centre if pos is None.
    """
    h_n, w_n = crop_bgr_native.shape[:2]   # 720, 1280
    half = ZOOM_HALF_PX

    if pos:
        cx, cy = int(pos[0]), int(pos[1])
    else:
        cx, cy = w_n // 2, h_n // 2

    x1 = max(0, cx - half)
    y1 = max(0, cy - half)
    x2 = min(w_n, cx + half)
    y2 = min(h_n, cy + half)

    patch = crop_bgr_native[y1:y2, x1:x2].copy()

    # Offset of candidate inside the patch
    rel_cx = cx - x1
    rel_cy = cy - y1

    # Bbox inside the patch (clip to patch bounds)
    rel_bbox = None
    if bbox_crop:
        bx1 = max(0, bbox_crop[0] - x1)
        by1 = max(0, bbox_crop[1] - y1)
        bx2 = min(patch.shape[1], bbox_crop[2] - x1)
        by2 = min(patch.shape[0], bbox_crop[3] - y1)
        if bx2 > bx1 and by2 > by1:
            rel_bbox = (bx1, by1, bx2, by2)

    # Draw on native-scale patch
    if pos:
        r = 10
        cv2.circle(patch, (rel_cx, rel_cy), r, CROSS_BGR, 1)
        cv2.line(patch, (rel_cx - r - 4, rel_cy), (rel_cx + r + 4, rel_cy), CROSS_BGR, 1)
        cv2.line(patch, (rel_cx, rel_cy - r - 4), (rel_cx, rel_cy + r + 4), CROSS_BGR, 1)
        cv2.circle(patch, (rel_cx, rel_cy), 3, CROSS_BGR, -1)
    if rel_bbox:
        cv2.rectangle(patch, (rel_bbox[0], rel_bbox[1]), (rel_bbox[2], rel_bbox[3]),
                      BBOX_BGR, 1)

    # Upscale 2× with nearest neighbour (preserve pixel sharpness)
    zoomed_bgr = cv2.resize(patch, (ZOOM_DISPLAY_SZ, ZOOM_DISPLAY_SZ),
                            interpolation=cv2.INTER_NEAREST)

    # Convert to PIL and add text overlay
    zoomed_rgb = cv2.cvtColor(zoomed_bgr, cv2.COLOR_BGR2RGB)
    zoom_img = Image.fromarray(zoomed_rgb)
    dz = ImageDraw.Draw(zoom_img)

    frame  = obs.get("frame", "?")
    yaw    = obs.get("yaw", 0.0)
    pitch  = obs.get("pitch", 0.0)
    conf   = obs.get("weighted_conf", obs.get("conf", 0.0))
    astr_s = f"{anchor_strength:.3f}" if anchor_strength is not None else "—"

    # Semi-transparent label area at bottom
    label_h = 80
    overlay = Image.new("RGBA", (ZOOM_DISPLAY_SZ, label_h), (0, 0, 0, 170))
    zoom_rgba = zoom_img.convert("RGBA")
    zoom_rgba.paste(overlay, (0, ZOOM_DISPLAY_SZ - label_h), overlay)
    zoom_img = zoom_rgba.convert("RGB")
    dz = ImageDraw.Draw(zoom_img)

    ly = ZOOM_DISPLAY_SZ - label_h + 4
    dz.text((6, ly),      f"{t_id}  fr {frame}",          fill=AMBER,  font=fn_bold)
    dz.text((6, ly + 18), f"yaw {yaw:.2f}°  pitch {pitch:.2f}°", fill=WHITE, font=fn_small)
    dz.text((6, ly + 34), f"conf {conf:.3f}  str {astr_s}",      fill=GREEN, font=fn_small)

    if not pos:
        dz.text((6, ly + 50), "NO DETECTION POS", fill=RED_COL, font=fn_small)
    elif not bbox_crop:
        dz.text((6, ly + 50), "no bbox", fill=DIM, font=fn_small)
    else:
        w_bb = bbox_crop[2] - bbox_crop[0]
        h_bb = bbox_crop[3] - bbox_crop[1]
        dz.text((6, ly + 50), f"bbox {w_bb}×{h_bb}px", fill=DIM, font=fn_small)

    # Thin border
    dz.rectangle([0, 0, ZOOM_DISPLAY_SZ - 1, ZOOM_DISPLAY_SZ - 1],
                 outline=ZOOM_BORDER, width=2)

    return zoom_img


# ── Observation row (context + zoom side-by-side, plus label bar) ─────────────

def render_obs_row(equirect_bgr, obs, row_label, fn, fn_bold, fn_small, t_id, anchor_strength):
    """Return PIL Image (PAGE_W × OBS_ROW_H)."""
    yaw   = obs.get("yaw", 0.0)
    pitch = obs.get("pitch", 0.0)

    crop_yaw = nearest_crop_yaw(yaw)
    crop_bgr = extract_crop(equirect_bgr, crop_yaw)
    pos      = yaw_pitch_to_pixel(yaw, pitch, crop_yaw)
    bbox     = get_bbox_in_crop(obs)

    ctx_bgr  = render_context_panel(crop_bgr, pos, bbox)
    zoom_pil = render_zoom_panel(crop_bgr, pos, bbox, obs, fn, fn_bold, fn_small,
                                 t_id, anchor_strength)

    row = Image.new("RGB", (PAGE_W, OBS_ROW_H), BG)

    # Label bar
    row_draw = ImageDraw.Draw(row)
    row_draw.rectangle([0, 0, PAGE_W - 1, OBS_ROW_LABEL_H - 1], fill=(20, 20, 38))
    frame  = obs.get("frame", "?")
    conf   = obs.get("weighted_conf", obs.get("conf", 0.0))
    row_draw.text((6, 6),
                  f"{row_label}  fr {frame}  yaw {yaw:.2f}°  pitch {pitch:.2f}°  conf {conf:.3f}",
                  fill=WHITE, font=fn_bold)

    # Context panel
    ctx_rgb = cv2.cvtColor(ctx_bgr, cv2.COLOR_BGR2RGB)
    ctx_pil = Image.fromarray(ctx_rgb)
    row.paste(ctx_pil, (0, OBS_ROW_LABEL_H))

    # Zoom panel
    row.paste(zoom_pil, (CTX_W, OBS_ROW_LABEL_H))

    # Fill gap if zoom is narrower than remaining width
    return row


def make_error_row(message, fn_small):
    row = Image.new("RGB", (PAGE_W, OBS_ROW_H), (40, 8, 8))
    ImageDraw.Draw(row).text((10, 20), f"ERROR: {message}", fill=(200, 80, 80), font=fn_small)
    return row


# ── Anchor page ───────────────────────────────────────────────────────────────

def render_anchor_page(tracklet, frame_cache, fps, fn, fn_bold, fn_small, fn_hdr):
    page = Image.new("RGB", (PAGE_W, PAGE_H), BG)
    draw = ImageDraw.Draw(page)

    t_id   = tracklet["id"]
    astr   = tracklet.get("anchor_strength_candidate")
    disp   = tracklet.get("net_displacement_deg", 0.0)
    sh     = tracklet.get("confirmed_static_hotspot_frac", 0.0)
    obs_n  = tracklet.get("observation_count", 0)
    conf_m = tracklet.get("mean_weighted_conf", 0.0)
    status = tracklet.get("status", "anchor")
    obs_list = tracklet.get("frames", [])

    # Header colour
    if astr is not None and astr >= 0.70:
        hdr_col, str_col = HDR_GREEN, GREEN
    elif astr is not None and astr >= 0.55:
        hdr_col, str_col = HDR_AMBER, AMBER
    else:
        hdr_col, str_col = HDR_RED, RED_COL

    draw.rectangle([0, 0, PAGE_W - 1, HDR_H - 1], fill=hdr_col)
    astr_s = f"{astr:.3f}" if astr is not None else "—"
    draw.text((10, 6),  f"ANCHOR  {t_id}   str={astr_s}   disp={disp:.2f}°   obs={obs_n}   sh={sh:.3f}",
              fill=WHITE, font=fn_hdr)
    draw.text((10, 32), f"mean_conf={conf_m:.4f}   status={status}",
              fill=DIM, font=fn_small)

    # Select early / mid / late observations
    if len(obs_list) == 0:
        obs_triples = [("EARLY", None), ("MID", None), ("LATE", None)]
    elif len(obs_list) == 1:
        obs_triples = [("EARLY", obs_list[0])] * 3
    elif len(obs_list) == 2:
        obs_triples = [("EARLY", obs_list[0]), ("MID", obs_list[0]), ("LATE", obs_list[-1])]
    else:
        mid_idx = len(obs_list) // 2
        obs_triples = [
            ("EARLY", obs_list[0]),
            ("MID",   obs_list[mid_idx]),
            ("LATE",  obs_list[-1]),
        ]

    y_cursor = HDR_H
    for label, obs in obs_triples:
        if obs is None:
            row_img = make_error_row("no observations", fn_small)
        else:
            fidx = obs["frame"]
            equirect = frame_cache.get(fidx)
            if equirect is None:
                row_img = make_error_row(f"frame {fidx} not cached", fn_small)
            else:
                try:
                    row_img = render_obs_row(equirect, obs, label, fn, fn_bold, fn_small,
                                            t_id, astr)
                except Exception as exc:
                    row_img = make_error_row(str(exc)[:120], fn_small)

        page.paste(row_img, (0, y_cursor))
        y_cursor += OBS_ROW_H

    # Metrics strip
    metrics_top = HDR_H + N_OBS_ROWS * OBS_ROW_H
    draw.rectangle([0, metrics_top, PAGE_W - 1, metrics_top + METRICS_H - 1], fill=(18, 18, 30))

    early_obs = obs_triples[0][1] or {}
    late_obs  = obs_triples[-1][1] or {}
    span = (late_obs.get("frame", 0) - early_obs.get("frame", 0)) if obs_list else 0
    first_fr  = obs_list[0]["frame"] if obs_list else "—"
    last_fr   = obs_list[-1]["frame"] if obs_list else "—"

    draw.text((10, metrics_top + 8),
              f"frame span: {span}   first: {first_fr}   last: {last_fr}   "
              f"anchor_strength: {astr_s}   static_hotspot_frac: {sh:.3f}",
              fill=DIM, font=fn_small)
    draw.text((10, metrics_top + 28),
              f"obs_count: {obs_n}   mean_conf: {conf_m:.4f}   net_disp: {disp:.3f}°   status: {status}",
              fill=DIM, font=fn_small)

    # Separator
    draw.line([(0, metrics_top + METRICS_H - 1), (PAGE_W - 1, metrics_top + METRICS_H - 1)],
              fill=(50, 50, 80), width=1)

    # Verdict field
    verdict_top = metrics_top + METRICS_H
    draw.rectangle([0, verdict_top, PAGE_W - 1, PAGE_H - 1], fill=(10, 10, 20))
    draw.text((10, verdict_top + 12),
              "VERDICT:   [ ] likely ball          [ ] likely false positive          [ ] unclear",
              fill=WHITE, font=fn_hdr)

    return page


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="High-res Tier A anchor adjudication pack")
    ap.add_argument("--tracklets",   required=True, help="tracklets_tier_a_experimental.json")
    ap.add_argument("--video",       required=True, help="Source equirectangular MP4")
    ap.add_argument("--output-dir",  default="tier_a_adjudication")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Load tracklets ────────────────────────────────────────────────────────
    print("[adjudication] Loading tracklets …")
    with open(args.tracklets) as f:
        tdata = json.load(f)
    all_tracklets = tdata["tracklets"]
    anchors = sorted(
        [t for t in all_tracklets if t["status"] == "anchor"],
        key=lambda t: -(t.get("anchor_strength_candidate") or 0.0)
    )
    print(f"  anchors={len(anchors)} (of {len(all_tracklets)} total tracklets)")
    if not anchors:
        print("FATAL: no anchor tracklets found"); sys.exit(1)

    # ── Probe video ───────────────────────────────────────────────────────────
    print(f"[adjudication] Probing: {args.video}")
    if not Path(args.video).exists():
        print("FATAL: video not found"); sys.exit(1)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-of", "json", args.video],
        capture_output=True, text=True)
    if probe.returncode != 0:
        print(f"FATAL ffprobe: {probe.stderr}"); sys.exit(1)
    stream = json.loads(probe.stdout)["streams"][0]
    w_v, h_v = stream["width"], stream["height"]
    num, den = stream["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    if fps <= 0:
        print("FATAL: FPS=0"); sys.exit(1)
    print(f"  {w_v}×{h_v}  fps={fps:.4f}")

    # ── Collect unique frames ─────────────────────────────────────────────────
    needed_frames = set()
    for t in anchors:
        obs = t.get("frames", [])
        if not obs:
            continue
        needed_frames.add(obs[0]["frame"])
        mid_idx = len(obs) // 2
        needed_frames.add(obs[mid_idx]["frame"])
        needed_frames.add(obs[-1]["frame"])
    frame_list = sorted(needed_frames)
    print(f"[adjudication] Unique frames to extract: {len(frame_list)}")

    # ── Extract frames ────────────────────────────────────────────────────────
    frame_dir = out_dir / "frame_cache"
    frame_dir.mkdir(exist_ok=True)
    frame_cache = {}
    for i, fidx in enumerate(frame_list):
        out_path = frame_dir / f"frame_{fidx:06d}.jpg"
        if not out_path.exists():
            ok = extract_frame(args.video, fidx, fps, out_path)
            if not ok:
                print(f"  WARN: failed frame {fidx}")
                continue
        img = cv2.imread(str(out_path))
        if img is not None:
            frame_cache[fidx] = img
        if (i + 1) % 10 == 0 or (i + 1) == len(frame_list):
            print(f"  {i+1}/{len(frame_list)} frames cached")
    print(f"[adjudication] Frame cache: {len(frame_cache)}/{len(frame_list)}")

    # ── Fonts ─────────────────────────────────────────────────────────────────
    fn       = _font(13)
    fn_bold  = _font(14, bold=True)
    fn_small = _font(11)
    fn_hdr   = _font(15, bold=True)

    # ── Render pages ──────────────────────────────────────────────────────────
    pages = []
    for i, t in enumerate(anchors):
        print(f"  Rendering anchor {i+1}/{len(anchors)}: {t['id']} …", flush=True)
        page = render_anchor_page(t, frame_cache, fps, fn, fn_bold, fn_small, fn_hdr)
        pages.append(page)

    # ── PNG contact sheet ─────────────────────────────────────────────────────
    total_h = sum(p.height for p in pages)
    sheet   = Image.new("RGB", (PAGE_W, total_h), BG)
    y = 0
    for p in pages:
        sheet.paste(p, (0, y))
        y += p.height

    png_path = out_dir / "tier_a_anchor_adjudication.png"
    sheet.save(str(png_path))
    size_mb = png_path.stat().st_size / 1e6
    print(f"[adjudication] PNG: {png_path}  ({size_mb:.1f} MB)")

    # ── PDF (one page per anchor) ─────────────────────────────────────────────
    pdf_path = out_dir / "tier_a_anchor_adjudication.pdf"
    try:
        if pages:
            pages[0].save(
                str(pdf_path), "PDF", resolution=150,
                save_all=True, append_images=pages[1:]
            )
            print(f"[adjudication] PDF: {pdf_path}  ({pdf_path.stat().st_size/1e6:.1f} MB)")
    except Exception as exc:
        print(f"  WARN: PDF generation failed: {exc} — PNG is the primary output")

    # ── CSV ───────────────────────────────────────────────────────────────────
    csv_path = out_dir / "tier_a_anchor_adjudication.csv"
    csv_fields = [
        "tracklet_id", "anchor_strength", "net_displacement_deg",
        "observation_count", "mean_weighted_conf",
        "confirmed_static_hotspot_frac",
        "first_frame", "last_frame", "frame_span",
        "verdict",           # blank — for reviewer
        "notes",             # blank — for reviewer
    ]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=csv_fields)
        w.writeheader()
        for t in anchors:
            obs = t.get("frames", [])
            first_fr = obs[0]["frame"] if obs else None
            last_fr  = obs[-1]["frame"] if obs else None
            span     = (last_fr - first_fr) if (first_fr is not None and last_fr is not None) else None
            astr     = t.get("anchor_strength_candidate")
            w.writerow({
                "tracklet_id":                    t["id"],
                "anchor_strength":                f"{astr:.4f}" if astr is not None else "",
                "net_displacement_deg":           f"{t.get('net_displacement_deg', 0.0):.4f}",
                "observation_count":              t.get("observation_count", 0),
                "mean_weighted_conf":             f"{t.get('mean_weighted_conf', 0.0):.4f}",
                "confirmed_static_hotspot_frac":  f"{t.get('confirmed_static_hotspot_frac', 0.0):.4f}",
                "first_frame":                    first_fr if first_fr is not None else "",
                "last_frame":                     last_fr if last_fr is not None else "",
                "frame_span":                     span if span is not None else "",
                "verdict":                        "",
                "notes":                          "",
            })
    print(f"[adjudication] CSV: {csv_path}")

    # ── Manifest ──────────────────────────────────────────────────────────────
    manifest = {
        "description": "Tier A experimental anchor adjudication manifest",
        "anchor_count": len(anchors),
        "source_video": args.video,
        "anchors": []
    }
    for t in anchors:
        obs = t.get("frames", [])
        mid_idx = len(obs) // 2
        sel = {}
        if obs:
            sel["early"] = {"frame": obs[0]["frame"],
                            "yaw":   obs[0].get("yaw"), "pitch": obs[0].get("pitch")}
            sel["mid"]   = {"frame": obs[mid_idx]["frame"],
                            "yaw":   obs[mid_idx].get("yaw"), "pitch": obs[mid_idx].get("pitch")}
            sel["late"]  = {"frame": obs[-1]["frame"],
                            "yaw":   obs[-1].get("yaw"), "pitch": obs[-1].get("pitch")}
        manifest["anchors"].append({
            "tracklet_id":   t["id"],
            "anchor_strength": t.get("anchor_strength_candidate"),
            "observation_count": t.get("observation_count", 0),
            "frame_range": {
                "first": obs[0]["frame"] if obs else None,
                "last":  obs[-1]["frame"] if obs else None,
            },
            "selected_frames": sel,
            "all_frames": [o["frame"] for o in obs],
        })

    manifest_path = out_dir / "tier_a_anchor_adjudication_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(f"[adjudication] Manifest: {manifest_path}")

    # ── Text summary ──────────────────────────────────────────────────────────
    lines = [
        "=== Tier A Anchor Human Adjudication Pack ===",
        "EXPERIMENT ONLY — no automatic verdicts produced.",
        "",
        f"Anchors : {len(anchors)}",
        f"Frames  : {len(frame_cache)}/{len(frame_list)} extracted",
        "",
        "Page layout per anchor:",
        "  Header   : tracklet ID, anchor_strength, displacement, obs count, hotspot frac",
        "  EARLY row: [context 960×540] [zoom 600×600 centred on candidate]",
        "  MID row  : same",
        "  LATE row : same",
        "  Metrics  : frame span, first/last, anchor_strength, sh, net_disp, mean_conf",
        "  Verdict  : [ ] likely ball  [ ] likely false positive  [ ] unclear",
        "",
        "Zoom crop: 300px radius in 1280×720 native crop → upscaled 2× (nearest-neighbour).",
        "Bbox drawn (orange) where detection_geometry.bbox_xyxy is present.",
        "",
        f"{'ID':<12} {'str':>6} {'disp°':>7} {'obs':>5} {'sh':>6} {'mean_conf':>10}",
        "-" * 54,
    ]
    for t in anchors:
        astr = t.get("anchor_strength_candidate")
        lines.append(
            f"{t['id']:<12} {f'{astr:.3f}' if astr is not None else '—':>6} "
            f"{t.get('net_displacement_deg', 0):>7.2f} "
            f"{t.get('observation_count', 0):>5} "
            f"{t.get('confirmed_static_hotspot_frac', 0):>6.3f} "
            f"{t.get('mean_weighted_conf', 0):>10.4f}"
        )
    lines += [
        "",
        "Outputs:",
        "  tier_a_anchor_adjudication.pdf      — paginated review pack",
        "  tier_a_anchor_adjudication.png      — PNG contact sheet",
        "  tier_a_anchor_adjudication.csv      — one row per anchor; verdict blank",
        "  tier_a_anchor_adjudication_manifest.json — anchor → source frames",
    ]
    (out_dir / "tier_a_anchor_adjudication_summary.txt").write_text("\n".join(lines))
    print("[adjudication] Summary written.")
    print("[adjudication] Done.")


if __name__ == "__main__":
    main()
