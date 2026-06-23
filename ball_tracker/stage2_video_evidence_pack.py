"""
stage2_video_evidence_pack.py — Stage 2 visual evidence pack from source video.

For each selected tracklet, renders perspective crops centred on each observation's
yaw/pitch, marks the detection, and tiles into a contact sheet PNG.

Observation schema (from stage2_temporal_link.py):
  tracklet["frames"] = [{"frame", "yaw", "pitch", "weighted_conf", "score", ...}, ...]

Selection:
  - All near-zero-displacement anchors (<2°) — required
  - Top 20 anchors by anchor_strength desc
  - Top 20 passing by best_available_score desc
  - Top 20 near-zero tracklets all classes (disp asc, anchors first)
  Deduplicated by tracklet id.

Frame deduplication:
  Builds master frame-index set, deduplicates, extracts each unique frame once
  via ffmpeg seek, caches to disk, reuses across tracklets.

Crop geometry (matches stage1_candidate_gen.py exactly):
  CROP_FOV_DEG = 110
  CROP_W = 1280 / CROP_H = 720
  CROP_YAWS = [0, 90, 180, 270]
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

# ── Layout ────────────────────────────────────────────────────────────────────
TILE_W       = 320
TILE_H       = 180
HEADER_H     = 28
LABEL_H      = 40
TILE_TOTAL_H = TILE_H + LABEL_H
MAX_CROPS    = 10
BG_COLOUR    = (20, 20, 20)
HEADER_COLOUR = {
    "anchor":          (30, 90, 30),
    "passing":         (30, 50, 90),
    "fragment":        (70, 50, 20),
    "rejected_static": (80, 20, 20),
}
TEXT_COLOUR  = (230, 230, 230)
CROSS_COLOUR = (0, 255, 80)   # in BGR for cv2, converted at draw time


# ── Perspective crop (identical to stage1_candidate_gen.py) ───────────────────
def extract_crop_frame(equirect_bgr, crop_yaw_deg, fov_deg=CROP_FOV_DEG,
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
                     cv2.INTER_LINEAR,
                     borderMode=cv2.BORDER_WRAP)


def nearest_crop_yaw(yaw_deg):
    """Return nearest standard crop yaw for a given world yaw."""
    def adiff(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)
    return min(CROP_YAWS, key=lambda c: adiff(yaw_deg, c))


def yaw_pitch_to_crop_pixel(yaw_deg, pitch_deg, crop_yaw_deg,
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


# ── ffmpeg single-frame extraction ────────────────────────────────────────────
def extract_frame_ffmpeg(video_path, frame_idx, fps, out_path):
    ts = frame_idx / fps
    cmd = ["ffmpeg", "-y", "-ss", f"{ts:.6f}", "-i", str(video_path),
           "-vframes", "1", "-q:v", "2", str(out_path)]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0 and os.path.exists(out_path)


# ── Font ──────────────────────────────────────────────────────────────────────
def get_font(size=11):
    for path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
    ]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ── Single tile ───────────────────────────────────────────────────────────────
def render_tile(equirect_bgr, obs, cum_disp, font):
    yaw, pitch = obs["yaw"], obs["pitch"]
    crop_yaw = nearest_crop_yaw(yaw)
    crop_bgr = extract_crop_frame(equirect_bgr, crop_yaw)

    pos = yaw_pitch_to_crop_pixel(yaw, pitch, crop_yaw)
    if pos:
        px, py = int(pos[0]), int(pos[1])
        r = 12
        cv2.circle(crop_bgr, (px, py), r, (0, 255, 80), 2)
        cv2.line(crop_bgr, (px - r, py), (px + r, py), (0, 255, 80), 1)
        cv2.line(crop_bgr, (px, py - r), (px, py + r), (0, 255, 80), 1)

    crop_rgb = cv2.cvtColor(
        cv2.resize(crop_bgr, (TILE_W, TILE_H), interpolation=cv2.INTER_AREA),
        cv2.COLOR_BGR2RGB)
    tile = Image.new("RGB", (TILE_W, TILE_TOTAL_H), BG_COLOUR)
    tile.paste(Image.fromarray(crop_rgb), (0, 0))

    draw = ImageDraw.Draw(tile)
    conf = obs.get("weighted_conf", obs.get("conf", 0))
    label = (f"fr{obs['frame']}  y{yaw:.1f}  p{pitch:.1f}\n"
             f"cf{conf:.2f}  Δ{cum_disp:.2f}°  cy{crop_yaw}")
    draw.text((4, TILE_H + 3), label, fill=TEXT_COLOUR, font=font)
    return tile


# ── One tracklet row ──────────────────────────────────────────────────────────
def render_row(tracklet, frame_cache, fps, font, max_crops=MAX_CROPS):
    obs_list = tracklet.get("frames", [])
    if not obs_list:
        return None

    step = max(1, len(obs_list) // max_crops)
    sampled = obs_list[::step][:max_crops]
    n = len(sampled)

    row_w = TILE_W * n
    row_h = HEADER_H + TILE_TOTAL_H
    row = Image.new("RGB", (row_w, row_h), BG_COLOUR)
    draw = ImageDraw.Draw(row)

    status = tracklet.get("status", "fragment")
    hc = HEADER_COLOUR.get(status, (50, 50, 50))
    draw.rectangle([0, 0, row_w - 1, HEADER_H - 1], fill=hc)

    astr = tracklet.get("anchor_strength_candidate")
    astr_s = f"str={astr:.3f}" if astr is not None else ""
    hdr = (f"{tracklet['id']}  {status}  obs={tracklet.get('observation_count', len(obs_list))}  "
           f"disp={tracklet.get('net_displacement_deg', 0):.2f}°  {astr_s}  "
           f"sh={tracklet.get('confirmed_static_hotspot_frac', 0):.2f}")
    draw.text((6, 6), hdr, fill=TEXT_COLOUR, font=font)

    # Compute cumulative displacement per sampled obs
    cum = 0.0
    prev_yaw, prev_pitch = None, None

    for i, ob in enumerate(sampled):
        if prev_yaw is not None:
            dy = math.radians(ob["yaw"] - prev_yaw)
            dp = math.radians(ob["pitch"] - prev_pitch)
            cum += math.degrees(math.sqrt(dy**2 + dp**2))
        prev_yaw, prev_pitch = ob["yaw"], ob["pitch"]

        fidx = ob["frame"]
        equirect = frame_cache.get(fidx)
        if equirect is None:
            tile = Image.new("RGB", (TILE_W, TILE_TOTAL_H), (40, 0, 0))
            ImageDraw.Draw(tile).text((4, 4), f"fr{fidx}\nNO FRAME",
                                      fill=(200, 80, 80), font=font)
        else:
            try:
                tile = render_tile(equirect, ob, cum, font)
            except Exception as e:
                tile = Image.new("RGB", (TILE_W, TILE_TOTAL_H), (40, 0, 0))
                ImageDraw.Draw(tile).text((4, 4), f"ERR\n{str(e)[:40]}",
                                          fill=(200, 80, 80), font=font)
        row.paste(tile, (i * TILE_W, HEADER_H))

    return row


# ── Tracklet selection ────────────────────────────────────────────────────────
def select_tracklets(tracklets):
    anchors = [t for t in tracklets if t["status"] == "anchor"]
    passing = [t for t in tracklets if t["status"] == "passing"]

    near_zero_anchors = sorted(
        [t for t in anchors if t.get("net_displacement_deg", 0) < 2.0],
        key=lambda t: t.get("net_displacement_deg", 0))

    top_anchors = sorted(
        anchors, key=lambda t: -(t.get("anchor_strength_candidate") or 0))[:20]

    top_passing = sorted(
        passing, key=lambda t: -(t.get("best_available_score") or 0))[:20]

    all_near_zero = sorted(
        [t for t in tracklets if t.get("net_displacement_deg", 0) < 2.0],
        key=lambda t: (
            {"anchor": 0, "passing": 1, "fragment": 2}.get(t["status"], 3),
            t.get("net_displacement_deg", 0)
        ))[:20]

    seen, selected = set(), []
    for group in [near_zero_anchors, top_anchors, top_passing, all_near_zero]:
        for t in group:
            if t["id"] not in seen:
                seen.add(t["id"])
                selected.append(t)
    return selected


# ── Frame index collection ─────────────────────────────────────────────────────
def collect_frames(selected, max_crops=MAX_CROPS):
    indices = set()
    for t in selected:
        obs = t.get("frames", [])
        step = max(1, len(obs) // max_crops)
        for ob in obs[::step][:max_crops]:
            indices.add(ob["frame"])
    return sorted(indices)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracklets",  required=True)
    ap.add_argument("--video",      required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--max-crops-per-tracklet", type=int, default=MAX_CROPS)
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. Load tracklets
    print("[evidence] Loading tracklets …")
    with open(args.tracklets) as f:
        tdata = json.load(f)
    tracklets = tdata["tracklets"]
    print(f"  {len(tracklets)} tracklets")

    # 2. Probe video — fail-fast on unreadable file or missing FPS only
    print(f"[evidence] Probing: {args.video}")
    if not os.path.exists(args.video):
        print("FATAL: video not found"); sys.exit(1)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-of", "json", args.video],
        capture_output=True, text=True)
    if probe.returncode != 0:
        print(f"FATAL ffprobe: {probe.stderr}"); sys.exit(1)
    stream = json.loads(probe.stdout)["streams"][0]
    w, h = stream["width"], stream["height"]
    num, den = stream["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    if fps <= 0:
        print("FATAL: FPS=0"); sys.exit(1)
    total_frames = int(stream.get("nb_frames", 0))
    print(f"  {w}×{h}  fps={fps:.4f}  frames={total_frames}")

    # 3. Select tracklets
    print("[evidence] Selecting tracklets …")
    selected = select_tracklets(tracklets)
    from collections import Counter
    sc = Counter(t["status"] for t in selected)
    print(f"  {len(selected)} selected: " + "  ".join(f"{k}={v}" for k,v in sorted(sc.items())))

    # 4. Deduplicate frame indices
    frame_indices = collect_frames(selected, args.max_crops_per_tracklet)
    print(f"[evidence] Unique frames to extract: {len(frame_indices)}")

    # 5. Extract frames
    frame_dir = out_dir / "frame_cache"
    frame_dir.mkdir(exist_ok=True)
    frame_cache = {}
    for i, fidx in enumerate(frame_indices):
        out_path = frame_dir / f"frame_{fidx:06d}.jpg"
        if not out_path.exists():
            ok = extract_frame_ffmpeg(args.video, fidx, fps, out_path)
            if not ok:
                print(f"  WARN: failed frame {fidx}")
                continue
        img = cv2.imread(str(out_path))
        if img is not None:
            frame_cache[fidx] = img
        if (i + 1) % 50 == 0 or (i + 1) == len(frame_indices):
            print(f"  {i+1}/{len(frame_indices)} frames cached")
    print(f"[evidence] Cache: {len(frame_cache)}/{len(frame_indices)}")

    # 6. Render rows
    font = get_font()
    rows = []
    for t in selected:
        row = render_row(t, frame_cache, fps, font, args.max_crops_per_tracklet)
        if row:
            rows.append(row)
    if not rows:
        print("FATAL: no rows rendered"); sys.exit(1)

    # 7. Assemble sheet
    print("[evidence] Assembling sheet …")
    sheet_w = max(r.width for r in rows)
    sheet_h = sum(r.height for r in rows)
    sheet = Image.new("RGB", (sheet_w, sheet_h), BG_COLOUR)
    y = 0
    for r in rows:
        sheet.paste(r, (0, y))
        y += r.height
    out_png = out_dir / "stage2_video_evidence_pack.png"
    sheet.save(str(out_png))
    print(f"[evidence] Saved: {out_png}  ({out_png.stat().st_size/1e6:.1f} MB)")

    # 8. Summary
    lines = [
        "Stage 2 Video Evidence Pack",
        f"Tracklets : {len(selected)}",
        f"Frames    : {len(frame_cache)}/{len(frame_indices)} cached",
        f"PNG       : {out_png.stat().st_size/1e6:.1f} MB",
        "",
        "Tracklets:",
    ]
    for t in selected:
        astr = t.get("anchor_strength_candidate")
        lines.append(
            f"  {t['id']}  {t['status']:20s}  "
            f"disp={t.get('net_displacement_deg',0):.2f}°  "
            f"obs={t.get('observation_count',0)}  "
            f"str={f'{astr:.3f}' if astr is not None else '—':6s}  "
            f"sh={t.get('confirmed_static_hotspot_frac',0):.2f}")
    (out_dir / "stage2_evidence_summary.txt").write_text("\n".join(lines))
    print("[evidence] Done.")


if __name__ == "__main__":
    main()
