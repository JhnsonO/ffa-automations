#!/usr/bin/env python3
"""
stage2_tier_a_anchor_review.py — Visual evidence pack for Tier A experimental anchors.

For every anchor in tracklets_tier_a_experimental.json, renders three panels:
  early observation | midpoint observation | late observation

Each panel:
  - Perspective crop centred on the candidate yaw/pitch (110° FoV, 1280×720)
  - Green crosshair + circle at the detection point
  - Overlay text: tracklet ID, frame, yaw/pitch, weighted_conf, anchor_strength

Each anchor gets its own full-width page with:
  - Colour-coded header bar (anchor_strength: green ≥0.7, amber ≥0.55, red <0.55)
  - Three side-by-side panels (early | mid | late)
  - Metrics strip below the panels
  - Human verdict field: [ ] likely ball  [ ] likely false positive  [ ] unclear

After the anchors, a compact passing summary table (one row per passing tracklet,
no frames extracted).

Crop geometry matches stage1_candidate_gen.py exactly:
  CROP_FOV_DEG = 110
  CROP_W = 1280 / CROP_H = 720

Usage:
  python3 stage2_tier_a_anchor_review.py \\
    --tracklets tracklets_tier_a_experimental.json \\
    --video     /path/to/equirect.mp4 \\
    --output-dir /tmp/anchor_review

Outputs:
  tier_a_anchor_review.pdf   — one page per anchor + passing summary
  tier_a_anchor_review.png   — same layout as PNG contact sheet
  tier_a_anchor_review_summary.txt
"""

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ── Crop geometry (must match stage1_candidate_gen.py) ────────────────────────
CROP_FOV_DEG = 110
CROP_W       = 1280
CROP_H       = 720
CROP_YAWS    = [0, 90, 180, 270]

# ── Panel layout ──────────────────────────────────────────────────────────────
PANEL_W       = 640
PANEL_H       = 360
PAGE_W        = PANEL_W * 3          # 3 panels side by side
HDR_H         = 52
METRICS_H     = 60
VERDICT_H     = 44
PANEL_LABEL_H = 38
PAGE_H        = HDR_H + PANEL_H + PANEL_LABEL_H + METRICS_H + VERDICT_H

BG            = (14, 14, 22)
WHITE         = (230, 230, 230)
DIM           = (130, 130, 145)
GREEN         = (60, 200, 80)
AMBER         = (220, 190, 50)
RED_COL       = (210, 70, 70)
HDR_GREEN     = (20, 80, 30)
HDR_AMBER     = (80, 65, 15)
HDR_RED       = (80, 20, 20)
PASS_BG       = (18, 22, 40)
CROSS_BGR     = (0, 255, 80)


def _font(size, bold=False):
    paths = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSansMono{'Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationMono-{'Bold' if bold else 'Regular'}.ttf",
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ── Projection ────────────────────────────────────────────────────────────────

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
    f = (w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    ya = math.radians(yaw_deg)
    pa = math.radians(pitch_deg)
    wx = math.sin(ya) * math.cos(pa)
    wy = math.sin(pa)
    wz = math.cos(ya) * math.cos(pa)
    cy = math.radians(crop_yaw_deg)
    cos_y, sin_y = math.cos(-cy), math.sin(-cy)
    rx =  cos_y * wx + sin_y * wz
    rz = -sin_y * wx + cos_y * wz
    wx, wz = rx, rz
    if wz <= 0:
        return None
    px =  (wx / wz) * f + w / 2.0
    py = -(wy / wz) * f + h / 2.0
    if 0 <= px < w and 0 <= py < h:
        return (px, py)
    return None


# ── ffmpeg seek ───────────────────────────────────────────────────────────────

def extract_frame(video_path, frame_idx, fps, out_path):
    ts = frame_idx / fps
    r = subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{ts:.6f}", "-i", str(video_path),
         "-vframes", "1", "-q:v", "2", str(out_path)],
        capture_output=True)
    return r.returncode == 0 and Path(out_path).exists()


# ── Panel render ──────────────────────────────────────────────────────────────

def render_panel(equirect_bgr, obs, label, fn, fn_bold, fn_small):
    """Return PIL Image (PANEL_W × (PANEL_H + PANEL_LABEL_H))."""
    yaw   = obs["yaw"]
    pitch = obs["pitch"]
    conf  = obs.get("weighted_conf", obs.get("conf", 0.0))
    frame = obs["frame"]

    crop_yaw = nearest_crop_yaw(yaw)
    crop_bgr = extract_crop(equirect_bgr, crop_yaw)

    # Draw marker
    pos = yaw_pitch_to_pixel(yaw, pitch, crop_yaw)
    if pos:
        px, py = int(pos[0]), int(pos[1])
        r = 16
        cv2.circle(crop_bgr, (px, py), r, CROSS_BGR, 2)
        cv2.line(crop_bgr,   (px - r, py), (px + r, py), CROSS_BGR, 1)
        cv2.line(crop_bgr,   (px, py - r), (px, py + r), CROSS_BGR, 1)
        # Small filled dot at centre
        cv2.circle(crop_bgr, (px, py), 3, CROSS_BGR, -1)

    # Resize to panel
    small_bgr = cv2.resize(crop_bgr, (PANEL_W, PANEL_H), interpolation=cv2.INTER_AREA)
    panel_rgb = cv2.cvtColor(small_bgr, cv2.COLOR_BGR2RGB)

    total_h = PANEL_H + PANEL_LABEL_H
    panel = Image.new("RGB", (PANEL_W, total_h), BG)
    panel.paste(Image.fromarray(panel_rgb), (0, 0))

    draw = ImageDraw.Draw(panel)

    # Label bar background
    draw.rectangle([0, PANEL_H, PANEL_W - 1, total_h - 1], fill=(22, 22, 35))

    # Label text
    draw.text((6, PANEL_H + 4),  label,               fill=AMBER,  font=fn_bold)
    draw.text((6, PANEL_H + 21), f"fr {frame}  yaw {yaw:.2f}°  pitch {pitch:.2f}°",
              fill=WHITE, font=fn)
    draw.text((PANEL_W - 140, PANEL_H + 21), f"conf {conf:.3f}",
              fill=GREEN, font=fn)

    return panel


# ── Anchor page ───────────────────────────────────────────────────────────────

def render_anchor_page(tracklet, frame_cache, fn, fn_bold, fn_small):
    """Return PIL Image (PAGE_W × PAGE_H) for one anchor."""
    page = Image.new("RGB", (PAGE_W, PAGE_H), BG)
    draw = ImageDraw.Draw(page)

    t_id   = tracklet["id"]
    status = tracklet.get("status", "anchor")
    astr   = tracklet.get("anchor_strength_candidate")
    disp   = tracklet.get("net_displacement_deg", 0.0)
    sh     = tracklet.get("confirmed_static_hotspot_frac", 0.0)
    obs_n  = tracklet.get("observation_count", 0)
    conf_m = tracklet.get("mean_weighted_conf", 0.0)
    obs_list = tracklet.get("frames", [])

    # Header colour by anchor strength
    if astr is not None and astr >= 0.70:
        hdr_col = HDR_GREEN
        str_col = GREEN
    elif astr is not None and astr >= 0.55:
        hdr_col = HDR_AMBER
        str_col = AMBER
    else:
        hdr_col = HDR_RED
        str_col = RED_COL

    draw.rectangle([0, 0, PAGE_W - 1, HDR_H - 1], fill=hdr_col)

    astr_s = f"{astr:.3f}" if astr is not None else "—"
    hdr_line1 = f"ANCHOR  {t_id}   str={astr_s}   disp={disp:.2f}°   obs={obs_n}   sh={sh:.2f}"
    hdr_line2 = f"mean_conf={conf_m:.3f}   status={status}"
    draw.text((10, 6),  hdr_line1, fill=WHITE, font=fn_bold)
    draw.text((10, 30), hdr_line2, fill=DIM,   font=fn_small)

    # Select early / mid / late observations
    if len(obs_list) == 0:
        panels_obs = []
    elif len(obs_list) == 1:
        panels_obs = [("EARLY", obs_list[0]), ("MID", obs_list[0]), ("LATE", obs_list[0])]
    elif len(obs_list) == 2:
        panels_obs = [("EARLY", obs_list[0]), ("MID", obs_list[0]), ("LATE", obs_list[-1])]
    else:
        mid_idx = len(obs_list) // 2
        panels_obs = [
            ("EARLY", obs_list[0]),
            ("MID",   obs_list[mid_idx]),
            ("LATE",  obs_list[-1]),
        ]

    panel_top = HDR_H
    for col, (label, obs) in enumerate(panels_obs):
        fidx = obs["frame"]
        equirect = frame_cache.get(fidx)
        if equirect is None:
            placeholder = Image.new("RGB", (PANEL_W, PANEL_H + PANEL_LABEL_H), (40, 8, 8))
            d = ImageDraw.Draw(placeholder)
            d.text((10, 10), f"fr {fidx}\nNO FRAME", fill=(200, 80, 80), font=fn)
            panel_img = placeholder
        else:
            try:
                panel_img = render_panel(equirect, obs, label, fn, fn_bold, fn_small)
            except Exception as e:
                panel_img = Image.new("RGB", (PANEL_W, PANEL_H + PANEL_LABEL_H), (40, 8, 8))
                ImageDraw.Draw(panel_img).text((6, 6), f"ERR\n{str(e)[:60]}", fill=(200,80,80), font=fn_small)

        page.paste(panel_img, (col * PANEL_W, panel_top))

    # Metrics strip
    metrics_top = HDR_H + PANEL_H + PANEL_LABEL_H
    draw.rectangle([0, metrics_top, PAGE_W - 1, metrics_top + METRICS_H - 1], fill=(18, 18, 30))

    early_obs = panels_obs[0][1] if panels_obs else {}
    late_obs  = panels_obs[-1][1] if panels_obs else {}
    span = late_obs.get("frame", 0) - early_obs.get("frame", 0)

    metrics_txt = (
        f"frame span: {span}   "
        f"first_frame: {obs_list[0]['frame'] if obs_list else '—'}   "
        f"last_frame: {obs_list[-1]['frame'] if obs_list else '—'}   "
        f"anchor_strength: {astr_s}   "
        f"static_hotspot_frac: {sh:.3f}   "
        f"net_disp: {disp:.3f}°"
    )
    draw.text((10, metrics_top + 8),  metrics_txt, fill=DIM, font=fn_small)
    draw.text((10, metrics_top + 28),
              f"obs_count: {obs_n}   mean_conf: {conf_m:.4f}   status: {status}",
              fill=DIM, font=fn_small)

    # Verdict field
    verdict_top = metrics_top + METRICS_H
    draw.rectangle([0, verdict_top, PAGE_W - 1, PAGE_H - 1], fill=(10, 10, 20))
    draw.text((10, verdict_top + 10),
              "VERDICT:   [ ] likely ball     [ ] likely false positive     [ ] unclear",
              fill=WHITE, font=fn_bold)

    return page


# ── Passing summary table row ─────────────────────────────────────────────────

ROW_H_PASS = 24

def render_passing_header(page_w, fn_bold):
    row = Image.new("RGB", (page_w, ROW_H_PASS + 8), (25, 30, 55))
    draw = ImageDraw.Draw(row)
    draw.text((10, 8),
              f"{'ID':<12} {'disp°':>7} {'obs':>5} {'mean_conf':>10} {'best_score':>11} {'sh':>6}",
              fill=AMBER, font=fn_bold)
    return row


def render_passing_row(t, page_w, i, fn):
    bg = (18, 22, 40) if i % 2 == 0 else (22, 26, 46)
    row = Image.new("RGB", (page_w, ROW_H_PASS), bg)
    draw = ImageDraw.Draw(row)
    disp   = t.get("net_displacement_deg", 0.0)
    obs_n  = t.get("observation_count", 0)
    conf_m = t.get("mean_weighted_conf", 0.0)
    bas    = t.get("best_available_score")
    sh     = t.get("confirmed_static_hotspot_frac", 0.0)
    bas_s  = f"{bas:.3f}" if bas is not None else "—"
    draw.text((10, 4),
              f"{t['id']:<12} {disp:>7.2f} {obs_n:>5} {conf_m:>10.4f} {bas_s:>11} {sh:>6.3f}",
              fill=WHITE, font=fn)
    return row


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Tier A anchor visual evidence pack")
    ap.add_argument("--tracklets",   required=True, help="tracklets_tier_a_experimental.json")
    ap.add_argument("--video",       required=True, help="Source equirectangular MP4")
    ap.add_argument("--output-dir",  default="tier_a_anchor_review")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load tracklets
    print("[anchor-review] Loading tracklets …")
    with open(args.tracklets) as f:
        tdata = json.load(f)
    all_tracklets = tdata["tracklets"]
    anchors  = sorted(
        [t for t in all_tracklets if t["status"] == "anchor"],
        key=lambda t: -(t.get("anchor_strength_candidate") or 0.0)
    )
    passing  = sorted(
        [t for t in all_tracklets if t["status"] == "passing"],
        key=lambda t: -(t.get("best_available_score") or 0.0)
    )
    print(f"  anchors={len(anchors)}  passing={len(passing)}")

    # Probe video
    print(f"[anchor-review] Probing: {args.video}")
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

    # Collect unique frame indices (early/mid/late per anchor)
    needed_frames = set()
    for t in anchors:
        obs = t.get("frames", [])
        if not obs:
            continue
        needed_frames.add(obs[0]["frame"])
        needed_frames.add(obs[len(obs) // 2]["frame"])
        needed_frames.add(obs[-1]["frame"])
    frame_list = sorted(needed_frames)
    print(f"[anchor-review] Unique frames to extract: {len(frame_list)}")

    # Extract frames
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
        if (i + 1) % 20 == 0 or (i + 1) == len(frame_list):
            print(f"  {i+1}/{len(frame_list)} frames cached")
    print(f"[anchor-review] Cache: {len(frame_cache)}/{len(frame_list)}")

    # Fonts
    fn       = _font(13)
    fn_bold  = _font(13, bold=True)
    fn_small = _font(11)
    fn_hdr   = _font(15, bold=True)

    # Render pages
    pages = []
    for i, t in enumerate(anchors):
        print(f"  Rendering anchor {i+1}/{len(anchors)}: {t['id']} …")
        page = render_anchor_page(t, frame_cache, fn, fn_bold, fn_small)
        pages.append(page)

    # Passing summary section
    pass_section_h = ROW_H_PASS + 8 + ROW_H_PASS * len(passing) + 60  # header + rows + title
    pass_page = Image.new("RGB", (PAGE_W, pass_section_h), BG)
    dp = ImageDraw.Draw(pass_page)
    dp.rectangle([0, 0, PAGE_W - 1, 42], fill=(25, 30, 55))
    dp.text((10, 10), f"PASSING TRACKLETS SUMMARY  ({len(passing)} total)",
            fill=AMBER, font=fn_hdr)

    y = 48
    hdr = render_passing_header(PAGE_W, fn_bold)
    pass_page.paste(hdr, (0, y))
    y += hdr.height
    for i, t in enumerate(passing):
        row = render_passing_row(t, PAGE_W, i, fn)
        pass_page.paste(row, (0, y))
        y += row.height

    pages.append(pass_page)

    # Assemble PNG contact sheet
    total_h = sum(p.height for p in pages)
    sheet = Image.new("RGB", (PAGE_W, total_h), BG)
    y = 0
    for p in pages:
        sheet.paste(p, (0, y))
        y += p.height

    png_path = out_dir / "tier_a_anchor_review.png"
    sheet.save(str(png_path))
    print(f"[anchor-review] PNG: {png_path}  ({png_path.stat().st_size/1e6:.1f} MB)")

    # Text summary
    lines = [
        "=== Tier A Experimental Anchor Visual Evidence Pack ===",
        "EXPERIMENT ONLY — visual quality review for track-quality decision.",
        "",
        f"Anchors rendered : {len(anchors)}",
        f"Frames extracted : {len(frame_cache)} / {len(frame_list)}",
        "",
        "Anchor list (anchor_strength desc):",
        f"  {'ID':<12} {'str':>6} {'disp°':>7} {'obs':>5} {'sh':>6} {'mean_conf':>10}",
    ]
    for t in anchors:
        astr = t.get("anchor_strength_candidate")
        lines.append(
            f"  {t['id']:<12} {f'{astr:.3f}' if astr is not None else '—':>6} "
            f"{t.get('net_displacement_deg',0):>7.2f} "
            f"{t.get('observation_count',0):>5} "
            f"{t.get('confirmed_static_hotspot_frac',0):>6.3f} "
            f"{t.get('mean_weighted_conf',0):>10.4f}"
        )
    lines += ["", "Verdict fields on each anchor page for human review."]
    (out_dir / "tier_a_anchor_review_summary.txt").write_text("\n".join(lines))
    print("[anchor-review] Summary written.")
    print("[anchor-review] Done.")


if __name__ == "__main__":
    main()
