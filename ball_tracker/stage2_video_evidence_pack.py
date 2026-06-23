"""
stage2_video_evidence_pack.py — Stage 2 visual evidence pack from source video.

For each selected tracklet, renders perspective crops centred on each candidate's
yaw/pitch, marks the detection, and tiles into a contact sheet PNG.

Selection:
  - 20 anchors by anchor_strength desc
  - All near-zero-displacement anchors (<2°) included (even if they overlap the 20)
  - 20 passing by best_available_score desc
  - 20 near-zero tracklets (all classes, disp asc) — filled by anchors first, then
    passing, then fragments
  Total unique tracklets capped at ~57.

Frame deduplication:
  Builds a master frame-index set across all candidates, deduplicates, extracts each
  unique frame once via ffmpeg seek, caches to disk, reuses across tracklets.

Crop geometry (must match stage1_candidate_gen.py exactly):
  CROP_FOV_DEG = 110
  CROP_W       = 1280
  CROP_H       = 720

Usage:
  python3 stage2_video_evidence_pack.py \
      --tracklets    /tmp/stage2_out/tracklets.json \
      --candidates   /tmp/stage1d/stage1_candidates_geo_filtered.json \
      --video        /tmp/video/equirect_trim.mp4 \
      --output-dir   /tmp/stage2_evidence \
      [--max-crops-per-tracklet 10]
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
from PIL import Image, ImageDraw, ImageFont

# ── Crop geometry (must match stage1_candidate_gen.py) ────────────────────────
CROP_FOV_DEG = 110
CROP_W       = 1280
CROP_H       = 720

# ── Layout ────────────────────────────────────────────────────────────────────
TILE_W          = 320          # display width per crop tile
TILE_H          = 180          # display height per crop tile
HEADER_H        = 28           # per-tracklet header bar height
LABEL_H         = 36           # per-tile label strip height
TILE_TOTAL_H    = TILE_H + LABEL_H
MAX_CROPS       = 10           # candidates sampled per tracklet
FONT_SIZE       = 11
BG_COLOUR       = (20, 20, 20)
HEADER_COLOUR   = {"anchor": (30, 90, 30), "passing": (30, 50, 90),
                   "fragment": (70, 50, 20), "rejected_static": (80, 20, 20)}
TEXT_COLOUR     = (230, 230, 230)
CROSS_COLOUR    = (0, 255, 80)   # BGR → RGB handled at draw time
CROSS_R         = 12


# ── Equirectangular → perspective crop (identical to stage1_candidate_gen.py) ─
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


def yaw_pitch_to_crop_pixel(yaw_deg, pitch_deg, crop_yaw_deg,
                             fov_deg=CROP_FOV_DEG, w=CROP_W, h=CROP_H):
    """Project (yaw, pitch) into crop-pixel coords. Returns (px, py) or None."""
    f = (w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    ya = math.radians(yaw_deg)
    pa = math.radians(pitch_deg)
    # world unit vector
    wx = math.sin(ya) * math.cos(pa)
    wy = math.sin(pa)
    wz = math.cos(ya) * math.cos(pa)
    # rotate by -crop_yaw
    cy = math.radians(crop_yaw_deg)
    cos_y, sin_y = math.cos(-cy), math.sin(-cy)
    rx =  cos_y * wx + sin_y * wz
    rz = -sin_y * wx + cos_y * wz
    wx, wz = rx, rz
    if wz <= 0:
        return None   # behind the camera
    px =  (wx / wz) * f + w / 2.0
    py = -(wy / wz) * f + h / 2.0
    if 0 <= px < w and 0 <= py < h:
        return (px, py)
    return None


# ── Best crop yaw for a candidate ─────────────────────────────────────────────
def best_crop_yaw(cand):
    """Return the crop_yaw from the candidate json, or nearest of [0,90,180,270]."""
    cy = cand.get("crop_yaw")
    if cy is not None:
        return float(cy)
    # nearest of the 4 standard yaws
    yaw = cand["yaw"]
    options = [0, 90, 180, 270]
    def angular_diff(a, b):
        d = abs(a - b) % 360
        return min(d, 360 - d)
    return min(options, key=lambda o: angular_diff(yaw, o))


# ── ffmpeg single-frame extraction ────────────────────────────────────────────
def extract_frame_ffmpeg(video_path, frame_idx, fps, out_path):
    ts = frame_idx / fps
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{ts:.6f}",
        "-i", str(video_path),
        "-vframes", "1",
        "-q:v", "2",
        str(out_path)
    ]
    r = subprocess.run(cmd, capture_output=True)
    return r.returncode == 0 and os.path.exists(out_path)


# ── Font (fallback to default if DejaVu not available) ────────────────────────
def get_font(size=FONT_SIZE):
    for path in ["/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
                 "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"]:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ── Tile renderer ─────────────────────────────────────────────────────────────
def render_tile(equirect_bgr, cand, frame_idx, cum_disp, tracklet_id, font):
    crop_yaw = best_crop_yaw(cand)
    crop_bgr = extract_crop_frame(equirect_bgr, crop_yaw)
    # Draw crosshair at detection position
    pos = yaw_pitch_to_crop_pixel(cand["yaw"], cand["pitch"], crop_yaw)
    if pos:
        px, py = int(pos[0]), int(pos[1])
        cv2.circle(crop_bgr, (px, py), CROSS_R, CROSS_COLOUR[::-1], 2)  # BGR
        cv2.line(crop_bgr, (px - CROSS_R, py), (px + CROSS_R, py), CROSS_COLOUR[::-1], 1)
        cv2.line(crop_bgr, (px, py - CROSS_R), (px, py + CROSS_R), CROSS_COLOUR[::-1], 1)

    # Resize to display tile
    crop_rgb = cv2.cvtColor(
        cv2.resize(crop_bgr, (TILE_W, TILE_H), interpolation=cv2.INTER_AREA),
        cv2.COLOR_BGR2RGB)
    tile_img = Image.new("RGB", (TILE_W, TILE_TOTAL_H), BG_COLOUR)
    tile_img.paste(Image.fromarray(crop_rgb), (0, 0))

    # Label strip
    draw = ImageDraw.Draw(tile_img)
    label = (f"fr{frame_idx}  y{cand['yaw']:.1f}°  p{cand['pitch']:.1f}°\n"
             f"cf{cand.get('weighted_conf', cand.get('conf', 0)):.2f}  "
             f"Δ{cum_disp:.2f}°  cy{crop_yaw:.0f}°")
    draw.text((4, TILE_H + 2), label, fill=TEXT_COLOUR, font=font)
    return tile_img


# ── Row renderer (one tracklet) ───────────────────────────────────────────────
def render_tracklet_row(tracklet, frame_cache, candidates_by_id, video_path,
                        fps, font, max_crops=MAX_CROPS):
    tid = tracklet["id"]
    status = tracklet.get("status", "fragment")
    obs = tracklet.get("observations", [])
    if not obs:
        return None

    # Sample evenly
    step = max(1, len(obs) // max_crops)
    sampled = obs[::step][:max_crops]
    n = len(sampled)

    row_w = TILE_W * n
    row_h = HEADER_H + TILE_TOTAL_H
    row_img = Image.new("RGB", (row_w, row_h), BG_COLOUR)
    draw = ImageDraw.Draw(row_img)

    # Header bar
    hc = HEADER_COLOUR.get(status, (50, 50, 50))
    draw.rectangle([0, 0, row_w - 1, HEADER_H - 1], fill=hc)
    astr = tracklet.get("anchor_strength_candidate")
    astr_s = f"str={astr:.3f}" if astr is not None else ""
    hdr = (f"{tid}  {status}  obs={tracklet.get('observation_count', len(obs))}  "
           f"disp={tracklet.get('net_displacement_deg', 0):.2f}°  {astr_s}  "
           f"sh={tracklet.get('confirmed_static_hotspot_frac', 0):.2f}")
    draw.text((6, 6), hdr, fill=TEXT_COLOUR, font=font)

    # Tiles
    for i, ob in enumerate(sampled):
        frame_idx = ob["frame"]
        cum_disp  = ob.get("cumulative_displacement_deg", 0.0)
        cand_id   = ob.get("candidate_id") or ob.get("id")
        # Resolve candidate dict — prefer candidates_by_id lookup for yaw/pitch/conf
        cand = candidates_by_id.get(cand_id) if cand_id else None
        if cand is None:
            # Fall back to observation fields
            cand = {"yaw": ob["yaw"], "pitch": ob["pitch"],
                    "weighted_conf": ob.get("conf", ob.get("weighted_conf", 0)),
                    "crop_yaw": ob.get("crop_yaw")}

        equirect_bgr = frame_cache.get(frame_idx)
        if equirect_bgr is None:
            tile_img = Image.new("RGB", (TILE_W, TILE_TOTAL_H), (40, 0, 0))
            ImageDraw.Draw(tile_img).text((4, 4), f"fr{frame_idx}\nNO FRAME", fill=(200, 80, 80), font=font)
        else:
            try:
                tile_img = render_tile(equirect_bgr, cand, frame_idx, cum_disp, tid, font)
            except Exception as e:
                tile_img = Image.new("RGB", (TILE_W, TILE_TOTAL_H), (40, 0, 0))
                ImageDraw.Draw(tile_img).text((4, 4), f"ERR\n{str(e)[:40]}", fill=(200, 80, 80), font=font)

        row_img.paste(tile_img, (i * TILE_W, HEADER_H))

    return row_img


# ── Tracklet selection ────────────────────────────────────────────────────────
def select_tracklets(tracklets):
    anchors  = [t for t in tracklets if t["status"] == "anchor"]
    passing  = [t for t in tracklets if t["status"] == "passing"]
    all_t    = tracklets

    # All near-zero anchors (disp < 2°) — required
    near_zero_anchors = sorted(
        [t for t in anchors if t.get("net_displacement_deg", 0) < 2.0],
        key=lambda t: t.get("net_displacement_deg", 0))

    # Top 20 anchors by strength
    top_anchors = sorted(anchors,
                         key=lambda t: -(t.get("anchor_strength_candidate") or 0))[:20]

    # Top 20 passing by score
    top_passing = sorted(passing,
                         key=lambda t: -(t.get("best_available_score") or 0))[:20]

    # 20 near-zero across all classes (disp asc), anchors first
    all_near_zero = sorted(
        [t for t in all_t if t.get("net_displacement_deg", 0) < 2.0],
        key=lambda t: (
            {"anchor": 0, "passing": 1, "fragment": 2}.get(t["status"], 3),
            t.get("net_displacement_deg", 0)
        ))[:20]

    # Merge preserving order, deduplicate by id
    seen = set()
    selected = []
    for group in [near_zero_anchors, top_anchors, top_passing, all_near_zero]:
        for t in group:
            if t["id"] not in seen:
                seen.add(t["id"])
                selected.append(t)

    return selected


# ── Frame index collection ────────────────────────────────────────────────────
def collect_frame_indices(selected_tracklets, max_crops=MAX_CROPS):
    indices = set()
    for t in selected_tracklets:
        obs = t.get("observations", [])
        step = max(1, len(obs) // max_crops)
        for ob in obs[::step][:max_crops]:
            indices.add(ob["frame"])
    return sorted(indices)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracklets",  required=True)
    ap.add_argument("--candidates", required=True)
    ap.add_argument("--video",      required=True)
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--max-crops-per-tracklet", type=int, default=MAX_CROPS)
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Load tracklets ───────────────────────────────────────────────────
    print("[evidence] Loading tracklets …")
    with open(args.tracklets) as f:
        tdata = json.load(f)
    tracklets = tdata["tracklets"]
    print(f"  {len(tracklets)} tracklets loaded")

    # ── 2. Load candidates for yaw/pitch/conf/crop_yaw lookup ─────────────
    print("[evidence] Loading candidates …")
    with open(args.candidates) as f:
        cdata = json.load(f)
    frames_dict = cdata.get("frames", {})
    candidates_by_id = {}
    for frame_cands in frames_dict.values():
        for c in frame_cands:
            cid = c.get("id") or c.get("candidate_id")
            if cid:
                candidates_by_id[cid] = c
    print(f"  {len(candidates_by_id)} candidates indexed")

    # ── 3. Validate video ───────────────────────────────────────────────────
    print(f"[evidence] Probing video: {args.video}")
    if not os.path.exists(args.video):
        print("FATAL: video file not found"); sys.exit(1)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-of", "json", args.video],
        capture_output=True, text=True)
    if probe.returncode != 0:
        print(f"FATAL: ffprobe failed: {probe.stderr}"); sys.exit(1)

    pdata = json.loads(probe.stdout)
    stream = pdata["streams"][0]
    w, h = stream["width"], stream["height"]
    print(f"  Resolution: {w}×{h}")

    # Equirectangular sanity: expect 2:1 ratio
    if abs(w / h - 2.0) > 0.15:
        print(f"FATAL: aspect ratio {w/h:.3f} — expected ~2.0 for equirectangular"); sys.exit(1)
    print("  Equirectangular aspect ratio OK")

    # FPS
    num, den = stream["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    if fps <= 0:
        print("FATAL: could not determine FPS"); sys.exit(1)
    print(f"  FPS: {fps:.4f}")

    total_frames = int(stream.get("nb_frames", 0))
    if total_frames:
        print(f"  Frames: {total_frames}")

    # ── 4. Select tracklets ─────────────────────────────────────────────────
    print("[evidence] Selecting tracklets …")
    selected = select_tracklets(tracklets)
    print(f"  Selected {len(selected)} tracklets")
    from collections import Counter
    sc = Counter(t["status"] for t in selected)
    for k, v in sorted(sc.items()):
        print(f"    {k}: {v}")

    # ── 5. Collect + deduplicate frame indices ──────────────────────────────
    print("[evidence] Collecting frame indices …")
    frame_indices = collect_frame_indices(selected, args.max_crops_per_tracklet)
    print(f"  Unique frames to extract: {len(frame_indices)}")

    # ── 6. Extract frames (one ffmpeg call per unique frame) ────────────────
    frame_dir = out_dir / "frame_cache"
    frame_dir.mkdir(exist_ok=True)
    frame_cache = {}
    print("[evidence] Extracting frames …")
    for i, fidx in enumerate(frame_indices):
        out_path = frame_dir / f"frame_{fidx:06d}.jpg"
        if not out_path.exists():
            ok = extract_frame_ffmpeg(args.video, fidx, fps, out_path)
            if not ok:
                print(f"  WARN: failed to extract frame {fidx}")
                continue
        img = cv2.imread(str(out_path))
        if img is not None:
            frame_cache[fidx] = img
        if (i + 1) % 50 == 0 or (i + 1) == len(frame_indices):
            print(f"  {i+1}/{len(frame_indices)} frames cached")

    print(f"[evidence] Frame cache: {len(frame_cache)}/{len(frame_indices)} loaded")

    # ── 7. Render rows ──────────────────────────────────────────────────────
    font = get_font()
    rows = []
    print("[evidence] Rendering tracklet rows …")
    for t in selected:
        row = render_tracklet_row(t, frame_cache, candidates_by_id,
                                  args.video, fps, font,
                                  args.max_crops_per_tracklet)
        if row:
            rows.append(row)

    if not rows:
        print("FATAL: no rows rendered"); sys.exit(1)

    # ── 8. Assemble contact sheet ───────────────────────────────────────────
    print("[evidence] Assembling contact sheet …")
    sheet_w = max(r.width for r in rows)
    sheet_h = sum(r.height for r in rows)
    sheet = Image.new("RGB", (sheet_w, sheet_h), BG_COLOUR)
    y = 0
    for r in rows:
        sheet.paste(r, (0, y))
        y += r.height

    out_png = out_dir / "stage2_video_evidence_pack.png"
    sheet.save(str(out_png), optimize=False)
    size_mb = out_png.stat().st_size / 1e6
    print(f"[evidence] Saved: {out_png}  ({size_mb:.1f} MB)")

    # ── 9. Summary text ─────────────────────────────────────────────────────
    summary_lines = [
        f"Stage 2 Video Evidence Pack",
        f"Tracklets selected : {len(selected)}",
        f"Unique frames      : {len(frame_indices)}",
        f"Frames cached      : {len(frame_cache)}",
        f"Rows rendered      : {len(rows)}",
        f"Output PNG         : {size_mb:.1f} MB",
        "",
        "Tracklet breakdown:",
    ]
    for t in selected:
        astr = t.get("anchor_strength_candidate")
        summary_lines.append(
            f"  {t['id']}  {t['status']:20s}  "
            f"disp={t.get('net_displacement_deg', 0):.2f}°  "
            f"obs={t.get('observation_count', 0)}  "
            f"str={f'{astr:.3f}' if astr is not None else '—':6s}  "
            f"sh={t.get('confirmed_static_hotspot_frac', 0):.2f}")

    summary_path = out_dir / "stage2_evidence_summary.txt"
    summary_path.write_text("\n".join(summary_lines))
    print(f"[evidence] Summary: {summary_path}")
    print("[evidence] Done.")


if __name__ == "__main__":
    main()
