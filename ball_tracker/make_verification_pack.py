#!/usr/bin/env python3
"""
FFA 360° Ball Tracker — Stage 2 Verification Pack
Generates per-observation review tiles for T0001, T0017, T0088.

Layout per tracklet:
  Row A  [tight 9° FoV] — clean candidate centre, crosshair only, no scores
  Row B  [context 25° FoV] — same centre, crosshair only
  Row C  [analysis] — alternates + gap Kalman predictions (clearly labelled,
          not mixed into review rows)

Label slots under each tile: ball_at_centre / ball_nearby_but_offset /
                              not_ball / occluded_or_unclear
Provenance: raw_associated_candidate for all observation frames.

Usage:
  python3 make_verification_pack.py <clip.mp4> <tracklets.json> <gaps.json> <output.png>
"""

import sys, os, json, subprocess, tempfile, math
import numpy as np
from PIL import Image, ImageDraw, ImageFont

# ── Crop parameters ─────────────────────────────────────────────────────────
TIGHT_FOV   = 9.0    # degrees — review tile
CONTEXT_FOV = 25.0   # degrees — orientation tile
TILE_W      = 320    # output pixels for each tile (both FoVs, square)
TILE_H      = 320

# ── Layout ───────────────────────────────────────────────────────────────────
FONT_SIZE    = 13
PAD          = 5
BG           = (18, 18, 18)
HEADER_H     = 28
LABEL_H      = 52    # space for label text below each tile
SECTION_H    = 22    # thin separator between tracklets

# ── Tracklets to verify (loaded from tracklets.json) ────────────────────────
TARGET_IDS   = {"T0001", "T0017", "T0088"}

# ── Sample stride: at most N frames per tracklet to keep pack manageable ────
MAX_SAMPLES  = 10

# ── Gap Kalman stride: sample predicted positions inside gaps ────────────────
GAP_SAMPLES  = 4

FPS = 29.97

# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers (reused from stage1_candidate_gen.py)
# ─────────────────────────────────────────────────────────────────────────────

def perspective_crop_fov(equirect_img, centre_yaw_deg, centre_pitch_deg, fov_deg,
                          out_w=TILE_W, out_h=TILE_H):
    """Extract a gnomonic perspective crop centred at (yaw, pitch) with given FoV."""
    img = np.array(equirect_img)
    h_eq, w_eq = img.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))

    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_w / 2.0) / f
    ry = -(yv - out_h / 2.0) / f
    rz = np.ones_like(rx)
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm

    # Rotate: first tilt up/down (pitch), then pan left/right (yaw)
    p = math.radians(centre_pitch_deg)
    cp, sp = math.cos(p), math.sin(p)
    rx2 =  rx
    ry2 =  cp * ry - sp * rz
    rz2 =  sp * ry + cp * rz

    y = math.radians(centre_yaw_deg)
    cy_r, sy_r = math.cos(y), math.sin(y)
    wx =  cy_r * rx2 + sy_r * rz2
    wy =  ry2
    wz = -sy_r * rx2 + cy_r * rz2

    import cv2
    yaw_map   = np.arctan2(wx, wz)
    pitch_map = np.arcsin(np.clip(wy, -1.0, 1.0))
    map_x = ((yaw_map  / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi)         * h_eq
    crop  = cv2.remap(img,
                      map_x.astype(np.float32),
                      map_y.astype(np.float32),
                      interpolation=cv2.INTER_LINEAR,
                      borderMode=cv2.BORDER_WRAP)
    return Image.fromarray(crop)


def draw_crosshair(img, color=(0, 255, 0), size=18, thickness=2):
    draw = ImageDraw.Draw(img)
    cx, cy = img.width // 2, img.height // 2
    draw.line([(cx - size, cy), (cx + size, cy)], fill=color, width=thickness)
    draw.line([(cx, cy - size), (cx, cy + size)], fill=color, width=thickness)


def load_font(size=FONT_SIZE):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)
    except Exception:
        return ImageFont.load_default()


def add_caption(img, lines, bg=(0, 0, 0, 180), fg=(230, 230, 230)):
    """Overlay small caption text at bottom of image in-place."""
    draw = ImageDraw.Draw(img)
    font = load_font(11)
    y = img.height - len(lines) * 14 - 4
    for line in lines:
        draw.text((4, y), line, fill=fg, font=font)
        y += 14


# ─────────────────────────────────────────────────────────────────────────────
# Frame extraction
# ─────────────────────────────────────────────────────────────────────────────

def extract_frame(video_path, frame_num, tmpdir):
    ts = frame_num / FPS
    out = os.path.join(tmpdir, f"frame_{frame_num:05d}.jpg")
    if not os.path.exists(out):
        subprocess.run([
            "ffmpeg", "-loglevel", "error",
            "-ss", f"{ts:.4f}", "-i", video_path,
            "-frames:v", "1", "-q:v", "2", out
        ], check=True)
    return Image.open(out).convert("RGB")


# ─────────────────────────────────────────────────────────────────────────────
# Tile builders
# ─────────────────────────────────────────────────────────────────────────────

def make_review_tile(equirect, yaw, pitch, fov, frame_num, label_text):
    """Clean review tile: crosshair only, no score/id overlay."""
    tile = perspective_crop_fov(equirect, yaw, pitch, fov)
    draw_crosshair(tile, color=(0, 255, 0) if fov < 15 else (0, 200, 255))
    # Minimal caption: frame only (no conf, no tracklet id — avoids bias)
    add_caption(tile, [f"f{frame_num}", label_text])
    return tile


def make_analysis_tile(equirect, yaw, pitch, fov, frame_num, caption_lines, color=(255, 165, 0)):
    """Analysis tile: different crosshair color, explicit caption."""
    tile = perspective_crop_fov(equirect, yaw, pitch, fov)
    draw_crosshair(tile, color=color, size=14, thickness=2)
    add_caption(tile, caption_lines)
    return tile


def label_slot_image(width=TILE_W):
    """Blank box with label options printed — reviewer circles/ticks one."""
    img = Image.new("RGB", (width, LABEL_H), color=(28, 28, 28))
    draw = ImageDraw.Draw(img)
    font = load_font(11)
    labels = [
        "[ ] ball_at_centre",
        "[ ] ball_nearby_but_offset",
        "[ ] not_ball",
        "[ ] occluded_or_unclear",
    ]
    # Two columns
    col_w = width // 2
    for i, lbl in enumerate(labels):
        col = i % 2
        row = i // 2
        draw.text((col * col_w + 4, row * 22 + 4), lbl, fill=(180, 180, 180), font=font)
    return img


# ─────────────────────────────────────────────────────────────────────────────
# Row/sheet assembly
# ─────────────────────────────────────────────────────────────────────────────

def make_section_header(text, width, height=HEADER_H, bg=(40, 40, 60)):
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    draw.text((8, 6), text, fill=(220, 220, 255), font=load_font(13))
    return img


def make_row_label(text, height, width=120, bg=(30, 30, 30)):
    img = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(img)
    draw.text((4, height // 2 - 7), text, fill=(180, 180, 180), font=load_font(11))
    return img


def hstack(images, pad=PAD, bg=BG):
    total_w = sum(i.width for i in images) + pad * (len(images) - 1)
    max_h   = max(i.height for i in images)
    out = Image.new("RGB", (total_w, max_h), bg)
    x = 0
    for img in images:
        out.paste(img, (x, 0))
        x += img.width + pad
    return out


def vstack(images, pad=PAD, bg=BG):
    max_w   = max(i.width for i in images)
    total_h = sum(i.height for i in images) + pad * (len(images) - 1)
    out = Image.new("RGB", (max_w, total_h), bg)
    y = 0
    for img in images:
        out.paste(img, (0, y))
        y += img.height + pad
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Tracklet pack builder
# ─────────────────────────────────────────────────────────────────────────────

def sample_frames(tracklet, max_n=MAX_SAMPLES):
    frames = tracklet.get("frames", [])
    if not frames:
        return []
    if len(frames) <= max_n:
        return frames
    # Evenly spaced sample
    indices = [round(i * (len(frames) - 1) / (max_n - 1)) for i in range(max_n)]
    return [frames[i] for i in sorted(set(indices))]


def build_tracklet_block(video_path, tracklet, gaps, tmpdir):
    tid = tracklet["id"]
    samples = sample_frames(tracklet)
    n = len(samples)

    # ── Row A: tight review tiles (9° FoV) ───────────────────────────────────
    tight_tiles  = []
    context_tiles= []
    label_slots  = []
    alt_tiles    = []   # analysis panel

    for obs in samples:
        frame_num = obs["frame"]
        yaw       = obs["yaw"]
        pitch     = obs["pitch"]
        conf      = obs.get("weighted_conf", 0.0)

        equirect = extract_frame(video_path, frame_num, tmpdir)

        # Review tile A — tight, clean
        t_tile = make_review_tile(equirect, yaw, pitch, TIGHT_FOV, frame_num,
                                  "provenance:raw_assoc")
        tight_tiles.append(t_tile)

        # Review tile B — context, clean
        c_tile = make_review_tile(equirect, yaw, pitch, CONTEXT_FOV, frame_num, "")
        context_tiles.append(c_tile)

        # Label slot
        label_slots.append(label_slot_image(TILE_W))

        # Analysis tile — alternates (different color per alternate)
        alt_colors = [(255, 100, 100), (255, 200, 50), (100, 100, 255)]
        alts = obs.get("alternates", [])
        if alts:
            sub_tiles = []
            for idx, alt in enumerate(alts[:3]):
                color = alt_colors[idx % len(alt_colors)]
                cap = [
                    f"ALT {idx+1}  f{frame_num}",
                    f"yaw={alt['yaw']:.1f} pitch={alt['pitch']:.1f}",
                    f"conf={alt.get('weighted_conf', alt.get('raw_conf', 0)):.2f}",
                    "[ANALYSIS — not a review tile]",
                ]
                at = make_analysis_tile(equirect, alt["yaw"], alt["pitch"],
                                        CONTEXT_FOV, frame_num, cap, color=color)
                sub_tiles.append(at)
            alt_tiles.append(hstack(sub_tiles))
        else:
            # No alternates for this frame
            placeholder = Image.new("RGB", (TILE_W, TILE_H), (25, 25, 25))
            d = ImageDraw.Draw(placeholder)
            d.text((10, TILE_H // 2 - 7), f"f{frame_num} — no alternates",
                   fill=(80, 80, 80), font=load_font(11))
            alt_tiles.append(placeholder)

    # ── Gap predictions for this tracklet ────────────────────────────────────
    gap_prediction_tiles = []
    for gap in gaps:
        if gap.get("preceding_tracklet_id") == tid or gap.get("following_tracklet_id") == tid:
            gstart = gap["start_frame"]
            gend   = gap["end_frame"]
            # Sample evenly inside the gap
            gap_len = gend - gstart
            if gap_len <= 0:
                continue
            step = max(1, gap_len // GAP_SAMPLES)
            pred_frames = list(range(gstart, gend, step))[:GAP_SAMPLES]

            # Kalman prediction: linearly interpolate between gap boundary yaw/pitch
            yaw_s   = gap.get("start_yaw",   0.0)
            pitch_s = gap.get("start_pitch", 0.0)
            yaw_e   = gap.get("end_yaw",     yaw_s)
            pitch_e = gap.get("end_pitch",   pitch_s)

            for pf in pred_frames:
                t = (pf - gstart) / max(1, gap_len)
                py = yaw_s   + t * (yaw_e   - yaw_s)
                pp = pitch_s + t * (pitch_e - pitch_s)
                equirect = extract_frame(video_path, pf, tmpdir)
                cap = [
                    f"GAP PRED  f{pf}",
                    f"yaw={py:.1f} pitch={pp:.1f}",
                    f"gap {gstart}→{gend} ({gap_len}fr)",
                    "[KALMAN INTERPOLATION — NOT an observation]",
                ]
                gt = make_analysis_tile(equirect, py, pp, CONTEXT_FOV, pf, cap,
                                        color=(180, 0, 255))
                gap_prediction_tiles.append(gt)

    # ── Assemble tracklet block ───────────────────────────────────────────────
    sheet_w = TILE_W * n + PAD * (n - 1)

    header_text = (
        f"{tid} | span {tracklet['start_frame']}–{tracklet['end_frame']} "
        f"| obs={tracklet['observation_count']} "
        f"| net_disp={tracklet['net_displacement_deg']:.1f}° "
        f"| status={tracklet['status']}"
        # anchor_strength deliberately OMITTED during review
    )
    header = make_section_header(header_text, sheet_w)

    row_label_tight   = make_row_label("ROW A\ntight 9°", TILE_H, width=80)
    row_label_context = make_row_label("ROW B\nctx 25°",  TILE_H, width=80)
    row_label_labels  = make_row_label("LABELS",          LABEL_H, width=80)

    row_a = hstack([row_label_tight]   + tight_tiles)
    row_b = hstack([row_label_context] + context_tiles)
    row_l = hstack([row_label_labels]  + label_slots)

    # Analysis section separator
    analysis_header = make_section_header(
        f"  ↳ ANALYSIS PANEL — {tid} (alternates + gap predictions — do not use for labelling)",
        sheet_w + 80, bg=(50, 30, 30))

    alt_row = hstack(alt_tiles)

    block_parts = [header, row_a, row_b, row_l, analysis_header, alt_row]

    if gap_prediction_tiles:
        gap_header = make_section_header(
            f"  ↳ GAP KALMAN PREDICTIONS — {tid} (interpolated, NOT observations)",
            sheet_w + 80, bg=(50, 30, 50))
        gap_row = hstack(gap_prediction_tiles)
        block_parts += [gap_header, gap_row]

    separator = Image.new("RGB", (sheet_w + 80, SECTION_H), (60, 60, 80))

    return vstack(block_parts + [separator])


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) != 5:
        print("Usage: make_verification_pack.py <clip.mp4> <tracklets.json> <gaps.json> <output.png>")
        sys.exit(1)

    video_path     = sys.argv[1]
    tracklets_path = sys.argv[2]
    gaps_path      = sys.argv[3]
    output_path    = sys.argv[4]

    with open(tracklets_path) as f:
        all_tracklets = json.load(f)["tracklets"]
    with open(gaps_path) as f:
        all_gaps = json.load(f)["gaps"]

    targets = {t["id"]: t for t in all_tracklets if t["id"] in TARGET_IDS}
    missing = TARGET_IDS - set(targets.keys())
    if missing:
        print(f"WARNING: tracklets not found: {missing}")

    # Overall title
    title_h = 40
    title_w = 1200
    title = Image.new("RGB", (title_w, title_h), (20, 20, 40))
    draw  = ImageDraw.Draw(title)
    draw.text((10, 10),
              "FFA 360° Stage 2 — Verification Pack  |  T0001 / T0017 / T0088  |  "
              "Label each tile independently. Anchor score hidden during review.",
              fill=(200, 200, 255), font=load_font(13))

    blocks = [title]

    with tempfile.TemporaryDirectory() as tmpdir:
        for tid in ["T0001", "T0017", "T0088"]:
            if tid not in targets:
                continue
            print(f"Building block for {tid}...", flush=True)
            block = build_tracklet_block(video_path, targets[tid], all_gaps, tmpdir)
            blocks.append(block)

    # Final vstack — normalise width
    max_w = max(b.width for b in blocks)
    padded = []
    for b in blocks:
        if b.width < max_w:
            p = Image.new("RGB", (max_w, b.height), BG)
            p.paste(b, (0, 0))
            padded.append(p)
        else:
            padded.append(b)

    sheet = vstack(padded, pad=2)
    sheet.save(output_path, "PNG")
    print(f"Saved: {output_path} ({sheet.width}×{sheet.height})")


if __name__ == "__main__":
    main()
