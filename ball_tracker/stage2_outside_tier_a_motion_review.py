#!/usr/bin/env python3
"""
Stage 2 — Outside-Tier-A Credible-Motion Visual Review Pack
============================================================
Evidence-collection only. Does NOT change any thresholds, tracklet status,
filter, radii, linker, renderer, or frozen files.

For each outside-Tier-A window (T0275, T0334, T0394) that was flagged as
frame_only_unsupported in the Tier A dry-run comparison:

  For each of early / mid / late sample frames in the original window:
    - Perspective crop centred on the original tracklet median position (FOV=80°)
    - Original Stage 1 candidate positions in the frame overlaid (green dots)
    - Dry-run remaining candidate positions overlaid (cyan dots)
    - Original linked track observation in this frame (yellow reticle)
    - Dry-run linked track observation in this frame if any (magenta dot)
    - Equirect thumbnail with location marker

  Per-window summary panel:
    - net_displacement_deg, frame range, in_tier_a, nearest_frame_dist
    - Verdict row for human annotation (likely real ball / likely clutter / unclear)

Inputs:
  --video                  equirectangular source .mp4
  --original-tracklets     tracklets.json from Stage 2 on original candidates
  --dryrun-tracklets       tracklets.json from Stage 2 on dry-run candidates
  --original-candidates    stage1_candidates_quarantined.json (full original)
  --dryrun-candidates      stage1_candidates_tier_a_dry_run.json
  --output-dir

The three target windows are hard-coded from run 28087760893 evidence:
  T0275  passing  net_disp=17.145°  frames=2175-2190  nearest_frame_dist=2.064°
  T0334  passing  net_disp=42.102°  frames=2369-2378  nearest_frame_dist=21.311°
  T0394  passing  net_disp=5.902°   frames=2650-2669  nearest_frame_dist=5.182°
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile
from collections import defaultdict

import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow required: pip install Pillow")

# ── Target windows (hard-coded from run 28087760893) ─────────────────────────
TARGET_WINDOWS = [
    {
        "original_id":         "T0275",
        "status":              "passing",
        "net_displacement_deg": 17.145,
        "frame_range":         [2175, 2190],
        "falls_in_tier_a":     None,
        "nearest_frame_dist_deg": 2.064,
    },
    {
        "original_id":         "T0334",
        "status":              "passing",
        "net_displacement_deg": 42.102,
        "frame_range":         [2369, 2378],
        "falls_in_tier_a":     None,
        "nearest_frame_dist_deg": 21.311,
    },
    {
        "original_id":         "T0394",
        "status":              "passing",
        "net_displacement_deg": 5.902,
        "frame_range":         [2650, 2669],
        "falls_in_tier_a":     None,
        "nearest_frame_dist_deg": 5.182,
    },
]

# ── Layout ────────────────────────────────────────────────────────────────────
FPS          = 29.97
CROP_FOV_DEG = 80
CROP_W       = 640
CROP_H       = 400
THUMB_W      = 320
THUMB_H      = 160
TILES_PER_WIN = 3  # early / mid / late
TILE_W       = CROP_W + THUMB_W + 20
TILE_H       = CROP_H + 100
WIN_GAP      = 50

BG        = (12, 12, 18)
HEADER_BG = (28, 28, 45)
TILE_BG   = (20, 20, 32)
WHITE     = (230, 230, 230)
DIM       = (110, 110, 125)
GREEN     = (80, 220, 100)
YELLOW    = (220, 200, 60)
RED       = (220, 80, 80)
CYAN      = (60, 210, 230)
MAGENTA   = (210, 80, 210)
ORANGE    = (230, 150, 50)
RETICLE   = (255, 80, 80)

SEC_HDR_H  = 50
TILE_HDR_H = 28
SUMMARY_H  = 80


def _font(size, bold=False):
    paths = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSansMono{'Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationMono-{'Bold' if bold else 'Regular'}.ttf",
    ]
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


# ── Geometry ──────────────────────────────────────────────────────────────────

def _to_unit(yaw_deg, pitch_deg):
    y, p = math.radians(yaw_deg), math.radians(pitch_deg)
    return math.cos(p) * math.sin(y), math.sin(p), math.cos(p) * math.cos(y)


def _gc_deg(u, v):
    d = max(-1.0, min(1.0, u[0]*v[0] + u[1]*v[1] + u[2]*v[2]))
    return math.degrees(math.acos(d))


def extract_crop(eq_np, centre_yaw, centre_pitch,
                 fov_deg=CROP_FOV_DEG, out_w=CROP_W, out_h=CROP_H):
    h_eq, w_eq = eq_np.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs, ys = np.linspace(0, out_w-1, out_w), np.linspace(0, out_h-1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_w/2.0) / f
    ry = -(yv - out_h/2.0) / f
    rz = np.ones_like(rx)
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx/norm, ry/norm, rz/norm
    cy = math.radians(centre_yaw)
    wx  =  math.cos(cy)*rx + math.sin(cy)*rz
    wy  = ry.copy()
    wz  = -math.sin(cy)*rx + math.cos(cy)*rz
    cp = math.radians(centre_pitch)
    wy2 =  math.cos(cp)*wy  - math.sin(cp)*wz
    wz2 =  math.sin(cp)*wy  + math.cos(cp)*wz
    yaw_map   = np.arctan2(wx, wz2)
    pitch_map = np.arcsin(np.clip(wy2, -1, 1))
    map_x = ((yaw_map / (2*math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    try:
        import cv2
        return cv2.remap(eq_np, map_x.astype(np.float32), map_y.astype(np.float32),
                         interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
    except ImportError:
        mx = (map_x % w_eq).astype(np.int32)
        my = np.clip(map_y.astype(np.int32), 0, h_eq-1)
        return eq_np[my, mx]


def world_to_crop_px(obs_yaw, obs_pitch, centre_yaw, centre_pitch,
                     fov_deg=CROP_FOV_DEG, out_w=CROP_W, out_h=CROP_H):
    try:
        f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
        oy, op = math.radians(obs_yaw), math.radians(obs_pitch)
        vx = math.cos(op)*math.sin(oy)
        vy = math.sin(op)
        vz = math.cos(op)*math.cos(oy)
        cy = math.radians(centre_yaw)
        rx  =  math.cos(cy)*vx - math.sin(cy)*vz
        ry2 = vy
        rz  =  math.sin(cy)*vx + math.cos(cy)*vz
        cp = math.radians(centre_pitch)
        ry3 =  math.cos(cp)*ry2 + math.sin(cp)*rz
        rz2 = -math.sin(cp)*ry2 + math.cos(cp)*rz
        if rz2 <= 0:
            return -1, -1
        px = int(out_w/2.0 + f*rx/rz2)
        py = int(out_h/2.0 - f*ry3/rz2)
        return px, py
    except Exception:
        return -1, -1


def yaw_pitch_to_equirect_px(yaw_deg, pitch_deg, w_eq, h_eq):
    x = ((yaw_deg / 360.0) + 0.5) * w_eq
    y = (0.5 - pitch_deg / 180.0) * h_eq
    return int(x) % w_eq, int(y)


# ── Frame extraction ──────────────────────────────────────────────────────────

def extract_frame_np(video_path, frame_num, tmpdir, fps=FPS):
    t = frame_num / fps
    out_path = os.path.join(tmpdir, f"frame_{frame_num:05d}.jpg")
    if not os.path.exists(out_path):
        r = subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{t:.4f}", "-i", video_path,
             "-frames:v", "1", "-q:v", "2", out_path],
            capture_output=True)
        if not os.path.exists(out_path):
            raise RuntimeError(f"ffmpeg failed frame {frame_num}: {r.stderr.decode()[:200]}")
    img = Image.open(out_path).convert("RGB")
    arr = np.array(img)
    return arr[:, :, ::-1]  # RGB→BGR


# ── Data loading ──────────────────────────────────────────────────────────────

def load_tracklet_by_id(tracklets_json_path):
    """Return dict: id → tracklet dict."""
    with open(tracklets_json_path) as f:
        data = json.load(f)
    items = data if isinstance(data, list) else data.get("tracklets", [])
    return {t["id"]: t for t in items}


def tracklet_median_pos(t):
    obs = t.get("observations") or t.get("frames") or []
    yaws   = sorted(o["yaw"]   for o in obs if o.get("yaw")   is not None)
    pitches = sorted(o["pitch"] for o in obs if o.get("pitch") is not None)
    if not yaws or not pitches:
        return None
    return yaws[len(yaws)//2], pitches[len(pitches)//2]


def tracklet_obs_at_frame(t, frame_num):
    """Return (yaw, pitch) of observation closest to frame_num, or None."""
    obs = t.get("observations") or t.get("frames") or []
    best, best_d = None, float("inf")
    for o in obs:
        d = abs(o.get("frame", -9999) - frame_num)
        if d < best_d:
            best_d = d
            best = o
    if best and best_d <= 5:
        return best.get("yaw"), best.get("pitch")
    return None


def build_candidate_frame_index(cands_path):
    """frame_int → list of (yaw, pitch)."""
    with open(cands_path) as f:
        data = json.load(f)
    index = defaultdict(list)
    frames = data.get("frames", {})
    if isinstance(frames, dict):
        for fk, clist in frames.items():
            if isinstance(clist, list):
                for c in clist:
                    y, p = c.get("yaw"), c.get("pitch")
                    if y is not None and p is not None:
                        index[int(fk)].append((y, p))
    return index


def find_overlapping_dry_tracklet(dry_tracklets_by_id, frame_num, centre_yaw, centre_pitch,
                                   spatial_tol=10.0):
    """Return first dry-run tracklet overlapping frame_num and within spatial_tol of centre."""
    cu = _to_unit(centre_yaw, centre_pitch)
    for tid, t in dry_tracklets_by_id.items():
        if t.get("start_frame", 0) > frame_num or t.get("end_frame", 0) < frame_num:
            continue
        pos = tracklet_median_pos(t)
        if pos is None:
            continue
        if _gc_deg(cu, _to_unit(pos[0], pos[1])) <= spatial_tol:
            return t
    return None


# ── Drawing ───────────────────────────────────────────────────────────────────

def draw_reticle(draw, cx, cy, radius=16, colour=RETICLE, lw=2):
    r = radius
    draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline=colour, width=lw)
    draw.line([cx-r-6, cy, cx+r+6, cy], fill=colour, width=lw)
    draw.line([cx, cy-r-6, cx, cy+r+6], fill=colour, width=lw)


def dot(draw, px, py, colour, r=6):
    if px < 0 or py < 0:
        return
    draw.ellipse([px-r, py-r, px+r, py+r], fill=colour, outline=WHITE, width=1)


def draw_equirect_thumb(eq_np, yaw_deg, pitch_deg):
    h_eq, w_eq = eq_np.shape[:2]
    rgb = eq_np[:, :, ::-1]
    thumb = Image.fromarray(rgb).resize((THUMB_W, THUMB_H), Image.BILINEAR)
    d = ImageDraw.Draw(thumb)
    tx, ty = yaw_pitch_to_equirect_px(yaw_deg, pitch_deg, THUMB_W, THUMB_H)
    r = 5
    d.ellipse([tx-r, ty-r, tx+r, ty+r], fill=RETICLE, outline=WHITE, width=1)
    d.line([tx-r-4, ty, tx+r+4, ty], fill=RETICLE, width=1)
    d.line([tx, ty-r-4, tx, ty+r+4], fill=RETICLE, width=1)
    return thumb


# ── Tile renderer ─────────────────────────────────────────────────────────────

def render_tile(eq_np, frame_num, centre_yaw, centre_pitch,
                orig_cands_at_frame, dry_cands_at_frame,
                orig_obs, dry_obs,
                win_id, label, font_sm, font_md):
    """
    One tile: perspective crop + equirect thumb + overlays.

    orig_obs / dry_obs: (yaw, pitch) or None — original/dry-run track obs this frame.
    orig_cands_at_frame / dry_cands_at_frame: list of (yaw, pitch).
    """
    tile = Image.new("RGB", (TILE_W, TILE_H), TILE_BG)
    d = ImageDraw.Draw(tile)

    # Perspective crop centred on original tracklet median
    try:
        crop_bgr = extract_crop(eq_np, centre_yaw, centre_pitch)
        crop_img = Image.fromarray(crop_bgr[:, :, ::-1])
    except Exception as e:
        crop_img = Image.new("RGB", (CROP_W, CROP_H), (40, 0, 0))
        ImageDraw.Draw(crop_img).text((10, 10), f"EXTRACT ERR\n{e}", fill=RED, font=font_sm)

    cd = ImageDraw.Draw(crop_img)

    # Centre reticle = original tracklet median location
    draw_reticle(cd, CROP_W//2, CROP_H//2, colour=RETICLE)

    # Original candidates in this frame — green dots
    for (cy_c, cp_c) in orig_cands_at_frame:
        px, py = world_to_crop_px(cy_c, cp_c, centre_yaw, centre_pitch)
        dot(cd, px, py, GREEN, r=5)

    # Dry-run remaining candidates in this frame — cyan dots
    for (cy_c, cp_c) in dry_cands_at_frame:
        px, py = world_to_crop_px(cy_c, cp_c, centre_yaw, centre_pitch)
        dot(cd, px, py, CYAN, r=5)

    # Original track obs this frame — yellow reticle
    if orig_obs:
        px, py = world_to_crop_px(orig_obs[0], orig_obs[1], centre_yaw, centre_pitch)
        if 0 <= px < CROP_W and 0 <= py < CROP_H:
            draw_reticle(cd, px, py, radius=12, colour=YELLOW, lw=2)

    # Dry-run track obs — magenta dot
    if dry_obs:
        px, py = world_to_crop_px(dry_obs[0], dry_obs[1], centre_yaw, centre_pitch)
        dot(cd, px, py, MAGENTA, r=7)

    tile.paste(crop_img, (0, TILE_HDR_H))

    # Equirect thumb
    thumb = draw_equirect_thumb(eq_np, centre_yaw, centre_pitch)
    tile.paste(thumb, (CROP_W + 10, TILE_HDR_H))

    # Tile header
    d.rectangle([0, 0, TILE_W, TILE_HDR_H-1], fill=HEADER_BG)
    d.text((6, 5), f"{win_id} | {label} | frame {frame_num:04d}", fill=CYAN, font=font_md)

    # Footer
    fy = TILE_HDR_H + CROP_H + 4
    d.text((6, fy),
           f"centre  yaw={centre_yaw:.2f}°  pitch={centre_pitch:.2f}°",
           fill=WHITE, font=font_sm)
    legend = ("● orig cand  ● dry cand  ✛ orig track  ● dry track  ✛ median")
    d.text((6, fy+16),
           f"GREEN=orig_cand  CYAN=dry_cand  YELLOW=orig_track  MAGENTA=dry_track",
           fill=DIM, font=font_sm)

    n_orig = len(orig_cands_at_frame)
    n_dry  = len(dry_cands_at_frame)
    d.text((6, fy+32),
           f"orig_cands={n_orig}  dry_cands={n_dry}  "
           f"orig_track={'YES' if orig_obs else 'NO'}  "
           f"dry_track={'YES' if dry_obs else 'NO'}",
           fill=ORANGE, font=font_sm)

    d.text((CROP_W+10, TILE_HDR_H + THUMB_H + 4), "← equirect thumb", fill=DIM, font=font_sm)

    return tile


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args):
    font_sm   = _font(12)
    font_md   = _font(14)
    font_bold = _font(15, bold=True)

    print("Loading data...", flush=True)
    orig_by_id = load_tracklet_by_id(args.original_tracklets)
    dry_by_id  = load_tracklet_by_id(args.dryrun_tracklets)
    orig_cand_index = build_candidate_frame_index(args.original_candidates)
    dry_cand_index  = build_candidate_frame_index(args.dryrun_candidates)

    os.makedirs(args.output_dir, exist_ok=True)

    # Total image height
    win_block_h = SEC_HDR_H + TILES_PER_WIN * TILE_H + SUMMARY_H
    total_h = len(TARGET_WINDOWS) * win_block_h + (len(TARGET_WINDOWS)-1) * WIN_GAP + 20
    canvas = Image.new("RGB", (TILE_W, total_h), BG)
    draw   = ImageDraw.Draw(canvas)

    verdict_rows = []

    with tempfile.TemporaryDirectory() as tmpdir:
        y_offset = 10

        for win_meta in TARGET_WINDOWS:
            win_id    = win_meta["original_id"]
            f_start   = win_meta["frame_range"][0]
            f_end     = win_meta["frame_range"][1]
            net_disp  = win_meta["net_displacement_deg"]
            nearest   = win_meta["nearest_frame_dist_deg"]
            in_tier_a = win_meta["falls_in_tier_a"]

            print(f"\nProcessing {win_id}  frames {f_start}-{f_end}  net_disp={net_disp}°", flush=True)

            orig_t = orig_by_id.get(win_id)
            if orig_t is None:
                print(f"  WARNING: {win_id} not found in original tracklets — skipping", flush=True)
                continue

            centre = tracklet_median_pos(orig_t)
            if centre is None:
                print(f"  WARNING: {win_id} has no position data — skipping", flush=True)
                continue
            centre_yaw, centre_pitch = centre

            # Section header
            draw.rectangle([0, y_offset, TILE_W, y_offset + SEC_HDR_H - 1], fill=HEADER_BG)
            draw.text((8, y_offset + 6), f"OUTSIDE-TIER-A REVIEW: {win_id}", fill=WHITE, font=font_bold)
            draw.text((8, y_offset + 24),
                      f"status={win_meta['status']}  net_disp={net_disp:.3f}°  "
                      f"frames={f_start}-{f_end}  in_tier_a={in_tier_a}  "
                      f"nearest_frame_dist={nearest:.3f}°  "
                      f"median=({centre_yaw:.2f}°, {centre_pitch:.2f}°)",
                      fill=CYAN, font=font_sm)
            y_offset += SEC_HDR_H

            # Sample frames: early / mid / late
            frame_span = f_end - f_start
            if frame_span == 0:
                sample_frames = [f_start, f_start, f_end]
            elif frame_span == 1:
                sample_frames = [f_start, f_start, f_end]
            else:
                sample_frames = [
                    f_start,
                    f_start + frame_span // 2,
                    f_end,
                ]
            labels = ["EARLY", "MID", "LATE"]

            for label, frame_num in zip(labels, sample_frames):
                print(f"  {label} frame {frame_num}", flush=True)

                eq_np = extract_frame_np(args.video, frame_num, tmpdir, FPS)

                orig_cands = orig_cand_index.get(frame_num, [])
                dry_cands  = dry_cand_index.get(frame_num, [])

                orig_obs_pos = tracklet_obs_at_frame(orig_t, frame_num)
                dry_linked   = find_overlapping_dry_tracklet(dry_by_id, frame_num,
                                                              centre_yaw, centre_pitch)
                dry_obs_pos  = None
                if dry_linked:
                    dry_obs_pos = tracklet_obs_at_frame(dry_linked, frame_num)

                tile = render_tile(
                    eq_np, frame_num, centre_yaw, centre_pitch,
                    orig_cands, dry_cands,
                    orig_obs_pos, dry_obs_pos,
                    win_id, label, font_sm, font_md,
                )
                canvas.paste(tile, (0, y_offset))
                y_offset += TILE_H

            # Summary / verdict row
            draw.rectangle([0, y_offset, TILE_W, y_offset + SUMMARY_H - 1], fill=(18, 24, 18))
            draw.text((8, y_offset + 6),
                      f"VERDICT [{win_id}] — annotate below:",
                      fill=YELLOW, font=font_bold)
            draw.text((8, y_offset + 26),
                      "[ ] likely real ball    [ ] likely false association/static clutter    [ ] unclear",
                      fill=WHITE, font=font_md)
            draw.text((8, y_offset + 46),
                      f"Notes: ________________________________________________________",
                      fill=DIM, font=font_sm)
            y_offset += SUMMARY_H + WIN_GAP

            verdict_rows.append({
                "window": win_id,
                "status": win_meta["status"],
                "net_displacement_deg": net_disp,
                "frames": f"{f_start}-{f_end}",
                "in_tier_a": str(in_tier_a),
                "nearest_frame_dist_deg": nearest,
                "median_yaw": round(centre_yaw, 3),
                "median_pitch": round(centre_pitch, 3),
                "verdict": "",
                "notes": "",
            })

    # Save image
    out_png = os.path.join(args.output_dir, "outside_tier_a_motion_review.png")
    canvas.save(out_png)
    print(f"\nSaved: {out_png}  ({canvas.size[0]}×{canvas.size[1]})", flush=True)

    # Verdict table
    headers = ["window", "status", "net_displacement_deg", "frames", "in_tier_a",
               "nearest_frame_dist_deg", "median_yaw", "median_pitch", "verdict", "notes"]
    tsv_lines = ["\t".join(headers)]
    md_lines  = ["| " + " | ".join(headers) + " |",
                 "| " + " | ".join(["---"]*len(headers)) + " |"]
    for row in verdict_rows:
        vals = [str(row[h]) for h in headers]
        tsv_lines.append("\t".join(vals))
        md_lines.append("| " + " | ".join(vals) + " |")

    tsv_path = os.path.join(args.output_dir, "outside_tier_a_verdict_table.tsv")
    md_path  = os.path.join(args.output_dir, "outside_tier_a_verdict_table.md")
    with open(tsv_path, "w") as f:
        f.write("\n".join(tsv_lines) + "\n")
    with open(md_path, "w") as f:
        f.write("\n".join(md_lines) + "\n")

    print(f"Saved: {tsv_path}")
    print(f"Saved: {md_path}")
    print("\n=== VERDICT TABLE ===")
    print("\n".join(md_lines))


def main():
    p = argparse.ArgumentParser(
        description="Outside-Tier-A credible-motion visual review pack"
    )
    p.add_argument("--video",                 required=True)
    p.add_argument("--original-tracklets",    required=True)
    p.add_argument("--dryrun-tracklets",      required=True)
    p.add_argument("--original-candidates",   required=True,
                   help="stage1_candidates_quarantined.json")
    p.add_argument("--dryrun-candidates",     required=True,
                   help="stage1_candidates_tier_a_dry_run.json")
    p.add_argument("--output-dir",            default=".")
    run(p.parse_args())


if __name__ == "__main__":
    main()
