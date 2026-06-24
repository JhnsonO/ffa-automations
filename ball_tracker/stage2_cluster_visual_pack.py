#!/usr/bin/env python3
"""
FFA Stage 2 — Repeated-Static Cluster Visual Verification Pack
===============================================================
Evidence-collection only. Does NOT change any thresholds, tracklet status,
Stage 1, Stage 1b, Stage 2 linking, renderer, or hotspot-map behaviour.

For each target cluster (C003–C009 by default):
  - Selects 3 representative temporal windows: early, middle, late
  - Extracts the equirectangular frame at each sample point
  - Renders a perspective crop centred on the cluster yaw/pitch (FoV=80°)
  - Overlays a centre reticle + label (cluster ID, yaw, pitch, frame, tracklet)
  - Adds the full equirect thumbnail with location marker for context

Also produces:
  - One composite PNG review pack
  - A concise verdict table (TSV) for human annotation

C001 and C002 appear in a short reference section only (already inside known
Stage 0 hotspot regions — no new verification effort spent on them).

Inputs
------
  --video          : equirectangular source (clip.mp4 / full .mp4)
  --report         : stage2_repeated_static_report.json
  --tracklets      : stage2 tracklets.json  (for per-member frame lists)
  --output-dir     : output directory (default: .)
  --target-clusters: comma-separated cluster IDs to verify (default: C003,C004,C005,C006,C007,C008,C009)
  --fps            : source FPS (default: 29.97)

Outputs
-------
  cluster_visual_pack.png   — composite review image
  verdict_table.tsv         — blank verdict table for human annotation
  verdict_table.md          — same table in Markdown
"""

import argparse
import json
import math
import os
import subprocess
import sys
import tempfile

import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow required: pip install Pillow")

# ── Layout ────────────────────────────────────────────────────────────────────
FPS               = 29.97
CROP_FOV_DEG      = 80          # perspective crop FoV for cluster location view
CROP_W            = 640
CROP_H            = 400
THUMB_W           = 320         # equirect thumbnail width
THUMB_H           = 160

TILE_W            = CROP_W + THUMB_W + 20   # crop + thumb side by side + gap
TILE_H            = CROP_H + 80             # crop height + header/footer
TILES_PER_CLUSTER = 3           # early / middle / late
CLUSTER_GAP       = 40
REF_SECTION_H     = 180

BG           = (12, 12, 18)
HEADER_BG    = (28, 28, 45)
TILE_BG      = (20, 20, 32)
WHITE        = (230, 230, 230)
DIM          = (110, 110, 125)
GREEN        = (80, 220, 100)
YELLOW       = (220, 200, 60)
RED          = (220, 80, 80)
CYAN         = (60, 210, 230)
ORANGE       = (230, 150, 50)
MAGENTA      = (210, 80, 210)
RETICLE      = (255, 80, 80)    # bright red crosshair

SECTION_HEADER_H = 36
TILE_HEADER_H    = 28


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


# ── Geometry ─────────────────────────────────────────────────────────────────

def extract_crop(equirect_np, centre_yaw_deg, centre_pitch_deg,
                 fov_deg=CROP_FOV_DEG, out_w=CROP_W, out_h=CROP_H):
    """Perspective crop from equirect numpy (H×W×3 BGR) centred on yaw/pitch."""
    h_eq, w_eq = equirect_np.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_w / 2.0) / f
    ry = -(yv - out_h / 2.0) / f
    rz = np.ones_like(rx)
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm

    # Rotate by yaw around Y-axis
    cy = math.radians(centre_yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry.copy()
    wz = -math.sin(cy) * rx + math.cos(cy) * rz

    # Rotate by pitch around X-axis
    cp = math.radians(centre_pitch_deg)
    wx2 = wx.copy()
    wy2 = math.cos(cp) * wy - math.sin(cp) * wz
    wz2 = math.sin(cp) * wy + math.cos(cp) * wz

    yaw_map   = np.arctan2(wx2, wz2)
    pitch_map = np.arcsin(np.clip(wy2, -1, 1))

    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq

    try:
        import cv2
        result = cv2.remap(equirect_np,
                           map_x.astype(np.float32), map_y.astype(np.float32),
                           interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)
        return result
    except ImportError:
        # Fallback: nearest-neighbour via numpy (no cv2)
        mx = (map_x % w_eq).astype(np.int32)
        my = np.clip(map_y.astype(np.int32), 0, h_eq - 1)
        return equirect_np[my, mx]


def yaw_pitch_to_equirect_px(yaw_deg, pitch_deg, w_eq, h_eq):
    """World yaw/pitch → pixel position in equirectangular image."""
    x = ((yaw_deg / 360.0) + 0.5) * w_eq
    y = (0.5 - pitch_deg / 180.0) * h_eq
    return int(x) % w_eq, int(y)


# ── Frame extraction ──────────────────────────────────────────────────────────

def extract_frame_np(video_path, frame_num, tmpdir, fps=FPS):
    """Extract frame as numpy array (BGR via PIL as RGB then flip)."""
    t = frame_num / fps
    out_path = os.path.join(tmpdir, f"frame_{frame_num:05d}.jpg")
    if not os.path.exists(out_path):
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{t:.4f}",
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "2",
            out_path,
        ]
        r = subprocess.run(cmd, capture_output=True)
        if not os.path.exists(out_path):
            raise RuntimeError(
                f"ffmpeg failed for frame {frame_num}: {r.stderr.decode()[:200]}"
            )
    img = Image.open(out_path).convert("RGB")
    arr = np.array(img)
    return arr[:, :, ::-1]  # RGB→BGR for consistent pipeline with cv2 path


# ── Tracklet frame lookup ─────────────────────────────────────────────────────

def build_tracklet_frame_map(tracklets_data):
    """tid → sorted list of {frame, yaw, pitch} dicts."""
    tmap = {}
    items = tracklets_data if isinstance(tracklets_data, list) else tracklets_data.get("tracklets", [])
    for t in items:
        frames = sorted(t.get("frames", []), key=lambda f: f["frame"])
        tmap[t["id"]] = frames
    return tmap


def select_temporal_samples(member_ids, tmap, n=3):
    """
    Select n representative frame samples spread across the full temporal range
    of the cluster (early / middle / late).

    Returns list of dicts: {frame, yaw, pitch, tracklet_id}
    """
    # Collect all observation frames across all members
    all_obs = []
    for tid in member_ids:
        for obs in tmap.get(tid, []):
            all_obs.append({
                "frame": obs["frame"],
                "yaw":   obs["yaw"],
                "pitch": obs["pitch"],
                "tracklet_id": tid,
            })
    if not all_obs:
        return []
    all_obs.sort(key=lambda x: x["frame"])

    if len(all_obs) <= n:
        return all_obs

    # Evenly space n samples: early, mid, late
    indices = []
    for i in range(n):
        frac = i / (n - 1)
        idx = round(frac * (len(all_obs) - 1))
        indices.append(idx)

    return [all_obs[i] for i in indices]


# ── Drawing helpers ───────────────────────────────────────────────────────────

def draw_reticle(draw, cx, cy, radius=16, colour=RETICLE, line_width=2):
    """Draw crosshair + circle at (cx, cy)."""
    r = radius
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=colour, width=line_width)
    draw.line([cx - r - 6, cy, cx + r + 6, cy], fill=colour, width=line_width)
    draw.line([cx, cy - r - 6, cx, cy + r + 6], fill=colour, width=line_width)


def draw_equirect_thumb(equirect_np, yaw_deg, pitch_deg, thumb_w=THUMB_W, thumb_h=THUMB_H):
    """Return PIL Image: scaled equirect thumbnail with location dot."""
    h_eq, w_eq = equirect_np.shape[:2]
    rgb = equirect_np[:, :, ::-1]  # BGR→RGB
    thumb = Image.fromarray(rgb).resize((thumb_w, thumb_h), Image.BILINEAR)
    draw = ImageDraw.Draw(thumb)
    tx, ty = yaw_pitch_to_equirect_px(yaw_deg, pitch_deg, thumb_w, thumb_h)
    r = 5
    draw.ellipse([tx - r, ty - r, tx + r, ty + r], fill=RETICLE, outline=WHITE, width=1)
    draw.line([tx - r - 4, ty, tx + r + 4, ty], fill=RETICLE, width=1)
    draw.line([tx, ty - r - 4, tx, ty + r + 4], fill=RETICLE, width=1)
    return thumb


def render_tile(equirect_np, sample, cluster_id, centre_yaw, centre_pitch,
                label, font_sm, font_md, font_bold):
    """
    Render one tile (perspective crop + equirect thumb + labels).
    Returns PIL Image of size (TILE_W, TILE_H).
    """
    tile = Image.new("RGB", (TILE_W, TILE_H), TILE_BG)
    draw = ImageDraw.Draw(tile)

    frame_num = sample["frame"]
    obs_yaw   = sample["yaw"]
    obs_pitch = sample["pitch"]
    tid       = sample["tracklet_id"]

    # Perspective crop centred on cluster centre
    try:
        crop_bgr = extract_crop(equirect_np, centre_yaw, centre_pitch)
        crop_rgb = crop_bgr[:, :, ::-1]
        crop_img = Image.fromarray(crop_rgb)
    except Exception as e:
        crop_img = Image.new("RGB", (CROP_W, CROP_H), (40, 0, 0))
        ImageDraw.Draw(crop_img).text((10, 10), f"EXTRACT ERROR\n{e}", fill=RED, font=font_sm)

    # Draw reticle at centre of crop (cluster centre is always at centre)
    crop_draw = ImageDraw.Draw(crop_img)
    draw_reticle(crop_draw, CROP_W // 2, CROP_H // 2)

    # Observation offset marker: where this specific obs sits relative to centre
    # Project obs yaw/pitch into crop pixel space
    obs_px, obs_py = _world_to_crop_px(obs_yaw, obs_pitch, centre_yaw, centre_pitch,
                                        CROP_FOV_DEG, CROP_W, CROP_H)
    if 0 <= obs_px < CROP_W and 0 <= obs_py < CROP_H:
        # Yellow dot for actual detection position
        r2 = 6
        crop_draw.ellipse([obs_px - r2, obs_py - r2, obs_px + r2, obs_py + r2],
                          fill=YELLOW, outline=WHITE, width=1)

    tile.paste(crop_img, (0, TILE_HEADER_H))

    # Equirect thumbnail
    thumb = draw_equirect_thumb(equirect_np, centre_yaw, centre_pitch)
    tile.paste(thumb, (CROP_W + 10, TILE_HEADER_H))

    # Tile header
    draw.rectangle([0, 0, TILE_W, TILE_HEADER_H - 1], fill=HEADER_BG)
    draw.text((6, 5), f"{cluster_id} | {label} | frame {frame_num:04d} | {tid}",
              fill=CYAN, font=font_md)

    # Footer below crop
    fy = TILE_HEADER_H + CROP_H + 4
    draw.text((6, fy),
              f"cluster centre  yaw={centre_yaw:.2f}°  pitch={centre_pitch:.2f}°",
              fill=WHITE, font=font_sm)
    draw.text((6, fy + 16),
              f"obs position    yaw={obs_yaw:.2f}°  pitch={obs_pitch:.2f}°  "
              f"Δ={_gc_deg_pair(obs_yaw, obs_pitch, centre_yaw, centre_pitch):.2f}°",
              fill=YELLOW, font=font_sm)

    # Thumb labels (right side)
    tx = CROP_W + 10
    draw.text((tx, TILE_HEADER_H + thumb.height + 4), "← equirect (full FOV)", fill=DIM, font=font_sm)

    return tile


def _world_to_crop_px(obs_yaw, obs_pitch, centre_yaw, centre_pitch, fov_deg, out_w, out_h):
    """Project obs yaw/pitch into perspective crop pixel coordinates."""
    try:
        f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
        # Get unit vector for obs
        oy, op = math.radians(obs_yaw), math.radians(obs_pitch)
        vx = math.cos(op) * math.sin(oy)
        vy = math.sin(op)
        vz = math.cos(op) * math.cos(oy)

        # Rotate by -centre_yaw (inverse rotation)
        cy = math.radians(centre_yaw)
        rx = math.cos(cy) * vx - math.sin(cy) * vz
        ry2 = vy
        rz = math.sin(cy) * vx + math.cos(cy) * vz

        # Rotate by -centre_pitch
        cp = math.radians(centre_pitch)
        ry3 = math.cos(cp) * ry2 + math.sin(cp) * rz
        rz2 = -math.sin(cp) * ry2 + math.cos(cp) * rz

        if rz2 <= 0:
            return -1, -1  # behind camera

        px = int(out_w / 2.0 + f * rx / rz2)
        py = int(out_h / 2.0 - f * ry3 / rz2)
        return px, py
    except Exception:
        return -1, -1


def _gc_deg_pair(y1, p1, y2, p2):
    v1 = np.array([math.cos(math.radians(p1)) * math.sin(math.radians(y1)),
                   math.sin(math.radians(p1)),
                   math.cos(math.radians(p1)) * math.cos(math.radians(y1))])
    v2 = np.array([math.cos(math.radians(p2)) * math.sin(math.radians(y2)),
                   math.sin(math.radians(p2)),
                   math.cos(math.radians(p2)) * math.cos(math.radians(y2))])
    return math.degrees(math.acos(float(np.clip(np.dot(v1, v2), -1, 1))))


# ── Reference section (C001/C002) ────────────────────────────────────────────

def render_reference_section(report_clusters, width, font_sm, font_md, font_bold):
    """Compact text-only reference block for C001 and C002."""
    h = REF_SECTION_H
    img = Image.new("RGB", (width, h), (18, 18, 30))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, SECTION_HEADER_H - 1], fill=(35, 25, 50))
    draw.text((10, 8), "REFERENCE — C001 & C002 (inside Stage 0 hotspot; not re-verified here)",
              fill=MAGENTA, font=font_bold)

    ref_ids = ["C001", "C002"]
    y = SECTION_HEADER_H + 8
    for cid in ref_ids:
        c = next((x for x in report_clusters if x["cluster_id"] == cid), None)
        if not c:
            draw.text((10, y), f"{cid}  — not found in report", fill=DIM, font=font_sm)
            y += 20
            continue
        hotspot_note = "INSIDE Stage 0 hotspot — confirmed false-positive source"
        line = (f"{cid}  yaw={c['centre_yaw_deg']:.2f}°  pitch={c['centre_pitch_deg']:.2f}°  "
                f"members={c['member_count']}  windows={c['distinct_window_count']}  "
                f"frames {c['overall_first_frame']}–{c['overall_last_frame']}  "
                f"obs={c['total_obs_count']}  |  {hotspot_note}")
        draw.text((10, y), line, fill=GREEN, font=font_sm)
        y += 22

    draw.text((10, y + 4),
              "Verdict: CONFIRMED FIXED SCENE  (Stage 0 hotspot overlap confirmed independently)",
              fill=DIM, font=font_sm)
    return img


# ── Verdict table ─────────────────────────────────────────────────────────────

def write_verdict_table(path_tsv, path_md, target_clusters, report_clusters):
    headers = ["Cluster", "Yaw (°)", "Pitch (°)", "Members", "Windows",
               "Frames", "Obs", "Hotspot", "Verdict", "Notes"]

    rows = []
    for cid in target_clusters:
        c = next((x for x in report_clusters if x["cluster_id"] == cid), None)
        if not c:
            continue
        rows.append([
            cid,
            f"{c['centre_yaw_deg']:.2f}",
            f"{c['centre_pitch_deg']:.2f}",
            str(c["member_count"]),
            str(c["distinct_window_count"]),
            f"{c['overall_first_frame']}–{c['overall_last_frame']}",
            str(c["total_obs_count"]),
            "No",  # C003–C009 are all outside hotspot
            "[ ]",
            "",
        ])

    with open(path_tsv, "w") as f:
        f.write("\t".join(headers) + "\n")
        for r in rows:
            f.write("\t".join(r) + "\n")

    with open(path_md, "w") as f:
        f.write("# Stage 2 Repeated-Static Cluster Visual Verification — Verdict Table\n\n")
        f.write("> Evidence-collection only. No suppression rule created yet.\n")
        f.write("> **Verdict options:** `confirmed fixed scene` | `uncertain` | `credible ball — do not suppress`\n\n")
        f.write("| " + " | ".join(headers) + " |\n")
        f.write("|" + "|".join(["---"] * len(headers)) + "|\n")
        for r in rows:
            f.write("| " + " | ".join(r) + " |\n")
        f.write("\n## Reference (inside Stage 0 hotspot — not re-verified)\n\n")
        f.write("| Cluster | Verdict |\n|---|---|\n")
        for ref in ["C001", "C002"]:
            f.write(f"| {ref} | confirmed fixed scene (Stage 0 hotspot) |\n")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Cluster visual verification pack (evidence only)")
    parser.add_argument("--video",            required=True)
    parser.add_argument("--report",           required=True,
                        help="stage2_repeated_static_report.json")
    parser.add_argument("--tracklets",        required=True,
                        help="stage2 tracklets.json")
    parser.add_argument("--output-dir",       default=".", dest="output_dir")
    parser.add_argument("--target-clusters",  default="C003,C004,C005,C006,C007,C008,C009",
                        dest="target_clusters")
    parser.add_argument("--fps",              type=float, default=FPS)
    args = parser.parse_args()

    target_ids = [x.strip() for x in args.target_clusters.split(",")]
    os.makedirs(args.output_dir, exist_ok=True)

    print(f"[cluster-visual-pack] Reading report: {args.report}", flush=True)
    with open(args.report) as f:
        report = json.load(f)
    report_clusters = report["clusters"]

    print(f"[cluster-visual-pack] Reading tracklets: {args.tracklets}", flush=True)
    with open(args.tracklets) as f:
        tdata = json.load(f)
    tmap = build_tracklet_frame_map(tdata)

    font_sm   = _font(11)
    font_md   = _font(13)
    font_bold = _font(14, bold=True)

    # ── Render each target cluster ────────────────────────────────────────────
    section_images = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for cid in target_ids:
            cluster = next((c for c in report_clusters if c["cluster_id"] == cid), None)
            if not cluster:
                print(f"[cluster-visual-pack] WARNING: {cid} not found in report — skipping", flush=True)
                continue

            cy   = cluster["centre_yaw_deg"]
            cp   = cluster["centre_pitch_deg"]
            mids = cluster["member_ids"]
            print(f"[cluster-visual-pack] {cid}  yaw={cy:.2f}  pitch={cp:.2f}  "
                  f"members={len(mids)}", flush=True)

            samples = select_temporal_samples(mids, tmap, n=TILES_PER_CLUSTER)
            if not samples:
                print(f"  WARNING: no observation frames found for {cid}", flush=True)
                continue

            labels = []
            if len(samples) == 1:
                labels = ["only"]
            elif len(samples) == 2:
                labels = ["early", "late"]
            else:
                labels = ["early"] + ["middle"] * (len(samples) - 2) + ["late"]

            # Section header bar
            sec_h = SECTION_HEADER_H + len(samples) * (TILE_H + 6) + 10
            sec_w = TILE_W
            sec_img = Image.new("RGB", (sec_w, sec_h), BG)
            sec_draw = ImageDraw.Draw(sec_img)

            # Cluster info header
            sec_draw.rectangle([0, 0, sec_w, SECTION_HEADER_H - 1], fill=(28, 35, 55))
            hotspot_txt = ("No hotspot overlap — requires visual verification")
            header_line = (f"{cid}  yaw={cy:.2f}°  pitch={cp:.2f}°  "
                           f"members={cluster['member_count']}  "
                           f"windows={cluster['distinct_window_count']}  "
                           f"frames {cluster['overall_first_frame']}–{cluster['overall_last_frame']}  "
                           f"obs={cluster['total_obs_count']}")
            sec_draw.text((8, 4), header_line, fill=CYAN, font=font_bold)
            sec_draw.text((8, 20), hotspot_txt, fill=YELLOW, font=font_sm)

            tile_y = SECTION_HEADER_H + 4

            for sample, label in zip(samples, labels):
                frame_num = sample["frame"]
                print(f"  frame {frame_num:04d} ({label})", flush=True)
                try:
                    eq_bgr = extract_frame_np(args.video, frame_num, tmpdir, fps=args.fps)
                except Exception as e:
                    print(f"  ERROR extracting frame {frame_num}: {e}", flush=True)
                    # Placeholder tile
                    err_img = Image.new("RGB", (TILE_W, TILE_H), (30, 10, 10))
                    ImageDraw.Draw(err_img).text((10, 10), f"FRAME {frame_num} ERROR: {e}",
                                                 fill=RED, font=font_sm)
                    sec_img.paste(err_img, (0, tile_y))
                    tile_y += TILE_H + 6
                    continue

                tile = render_tile(eq_bgr, sample, cid, cy, cp,
                                   label, font_sm, font_md, font_bold)
                sec_img.paste(tile, (0, tile_y))
                tile_y += TILE_H + 6

            section_images.append(sec_img)

    # ── Reference section ─────────────────────────────────────────────────────
    ref_w = TILE_W
    ref_img = render_reference_section(report_clusters, ref_w, font_sm, font_md, font_bold)
    section_images.append(ref_img)

    # ── Composite pack ────────────────────────────────────────────────────────
    total_h = sum(img.height + CLUSTER_GAP for img in section_images)
    max_w   = max(img.width for img in section_images)

    # Title bar
    title_h = 50
    pack = Image.new("RGB", (max_w, total_h + title_h), BG)
    pack_draw = ImageDraw.Draw(pack)
    pack_draw.rectangle([0, 0, max_w, title_h - 1], fill=(20, 20, 45))
    pack_draw.text((10, 8),  "FFA Stage 2 — Repeated-Static Cluster Visual Verification Pack",
                   fill=WHITE, font=font_bold)
    pack_draw.text((10, 28), f"Clusters: {', '.join(target_ids)}  |  Evidence collection only — no suppression rule created",
                   fill=DIM, font=font_sm)

    y = title_h
    for img in section_images:
        pack.paste(img, (0, y))
        y += img.height + CLUSTER_GAP

    pack_path = os.path.join(args.output_dir, "cluster_visual_pack.png")
    pack.save(pack_path, "PNG")
    print(f"[cluster-visual-pack] pack -> {pack_path}  ({pack.width}×{pack.height})", flush=True)

    # ── Verdict table ─────────────────────────────────────────────────────────
    tsv_path = os.path.join(args.output_dir, "verdict_table.tsv")
    md_path  = os.path.join(args.output_dir, "verdict_table.md")
    write_verdict_table(tsv_path, md_path, target_ids, report_clusters)
    print(f"[cluster-visual-pack] verdict table -> {tsv_path}", flush=True)
    print(f"[cluster-visual-pack] verdict table -> {md_path}", flush=True)
    print("[cluster-visual-pack] DONE", flush=True)


if __name__ == "__main__":
    main()
