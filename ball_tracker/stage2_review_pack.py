#!/usr/bin/env python3
"""
FFA Stage 2 — Tracklet Review Pack
Generates a visual review sheet from tracklets.json (no equirect video required).

Three sections:
  A. 20 anchors ranked by anchor_strength (descending)
  B. 20 passing tracklets (ranked by best_available_score descending)
  C. 20 near-zero-displacement tracklets (ranked by net_displacement_deg ascending)

Each tile: header row + 6–10 chronological candidate rows showing
  frame, yaw, pitch, weighted_conf, cumulative displacement from start.

Output: stage2_review_pack.png + stage2_review_summary.txt
"""

import argparse, json, math, sys, os
from collections import defaultdict

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow required: pip install Pillow")

# ── Layout ────────────────────────────────────────────────────────────────────
TILE_W        = 900
ROW_H         = 22
HEADER_H      = 52
LABEL_H       = 28
MAX_ROWS      = 10      # max candidate rows per tile (6–10)
MIN_ROWS      = 6
TILE_H        = HEADER_H + MAX_ROWS * ROW_H + LABEL_H
TILES_PER_COL = 20
SECTION_GAP   = 30
COLS          = 2

BG            = (15, 15, 15)
HEADER_BG     = (35, 35, 50)
ROW_ALT       = (22, 22, 30)
SEP           = (60, 60, 80)
WHITE         = (230, 230, 230)
DIM           = (130, 130, 140)
GREEN         = (80, 220, 100)
YELLOW        = (220, 200, 60)
RED           = (220, 80, 80)
CYAN          = (80, 200, 220)
MAGENTA       = (200, 80, 200)


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


def great_circle_deg(y1, p1, y2, p2):
    def to_v(y, p):
        yr, pr = math.radians(y), math.radians(p)
        return (math.cos(pr) * math.sin(yr), math.sin(pr), math.cos(pr) * math.cos(yr))
    v1, v2 = to_v(y1, p1), to_v(y2, p2)
    dot = sum(a * b for a, b in zip(v1, v2))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def sample_frames(frame_list, n_max=MAX_ROWS, n_min=MIN_ROWS):
    """Return up to n_max chronologically-spaced frames."""
    if len(frame_list) <= n_max:
        return frame_list
    # always include first and last
    indices = [0]
    step = (len(frame_list) - 1) / (n_max - 1)
    for i in range(1, n_max - 1):
        indices.append(round(i * step))
    indices.append(len(frame_list) - 1)
    seen = set()
    out = []
    for i in sorted(set(indices)):
        if i not in seen:
            out.append(frame_list[i])
            seen.add(i)
    return out


def conf_colour(c):
    if c >= 0.60:
        return GREEN
    if c >= 0.30:
        return YELLOW
    return RED


def disp_colour(d):
    if d >= 5.0:
        return GREEN
    if d >= 1.0:
        return YELLOW
    return RED


def draw_tile(draw, x0, y0, tracklet, section_label, rank,
              font_hdr, font_row, font_lbl):
    t = tracklet
    frames = t.get("frames", [])
    samples = sample_frames(frames)

    # ── Header ───────────────────────────────────────────────────────────────
    draw.rectangle([x0, y0, x0 + TILE_W, y0 + HEADER_H - 1], fill=HEADER_BG)

    status = t["status"]
    status_col = GREEN if status == "anchor" else (YELLOW if status == "passing" else MAGENTA)

    draw.text((x0 + 6, y0 + 4),
              f"#{rank:02d}  {t['id']}  [{status}]",
              font=font_hdr, fill=status_col)

    astr = t.get("anchor_strength_candidate")
    basc = t.get("best_available_score")
    disp = t.get("net_displacement_deg", 0.0)
    spread = t.get("spatial_spread_deg", 0.0)
    obs = t.get("observation_count", 0)
    cov = t.get("coverage_ratio", 0.0)
    mconf = t.get("mean_weighted_conf", 0.0)
    sh_frac = t.get("confirmed_static_hotspot_frac", 0.0)
    vel_c = t.get("velocity_consistency", 0.0)

    astr_str = f"{astr:.3f}" if astr is not None else "—"
    basc_str = f"{basc:.3f}" if basc is not None else "—"

    line2 = (f"obs={obs}  cov={cov:.2f}  conf={mconf:.3f}  "
             f"disp={disp:.2f}°  spread={spread:.2f}°  "
             f"str={astr_str}  bas={basc_str}  "
             f"sh={sh_frac:.2f}  vel_c={vel_c:.2f}")
    draw.text((x0 + 6, y0 + 26), line2, font=font_lbl, fill=DIM)

    # ── Separator ────────────────────────────────────────────────────────────
    draw.line([(x0, y0 + HEADER_H), (x0 + TILE_W, y0 + HEADER_H)], fill=SEP)

    # ── Column headers ────────────────────────────────────────────────────────
    ry = y0 + HEADER_H
    draw.rectangle([x0, ry, x0 + TILE_W, ry + ROW_H - 1], fill=(28, 28, 42))
    cols_hdr = [
        (6,   "frame",   DIM),
        (90,  "yaw°",    DIM),
        (190, "pitch°",  DIM),
        (285, "conf",    DIM),
        (365, "Δdisp°",  DIM),
        (460, "cumdisp°",DIM),
    ]
    for cx, lbl, col in cols_hdr:
        draw.text((x0 + cx, ry + 3), lbl, font=font_lbl, fill=col)
    ry += ROW_H

    # ── Candidate rows ────────────────────────────────────────────────────────
    first_yaw = samples[0]["yaw"] if samples else 0.0
    first_pitch = samples[0]["pitch"] if samples else 0.0
    prev_yaw = first_yaw
    prev_pitch = first_pitch

    for ri, fr in enumerate(samples):
        row_bg = ROW_ALT if ri % 2 == 0 else BG
        draw.rectangle([x0, ry, x0 + TILE_W, ry + ROW_H - 1], fill=row_bg)

        yaw   = fr["yaw"]
        pitch = fr["pitch"]
        conf  = fr["weighted_conf"]
        frame = fr["frame"]
        cum_disp = great_circle_deg(first_yaw, first_pitch, yaw, pitch)
        delta_disp = great_circle_deg(prev_yaw, prev_pitch, yaw, pitch)
        prev_yaw, prev_pitch = yaw, pitch

        draw.text((x0 + 6,   ry + 3), f"{frame:5d}", font=font_row, fill=WHITE)
        draw.text((x0 + 90,  ry + 3), f"{yaw:+8.2f}", font=font_row, fill=CYAN)
        draw.text((x0 + 190, ry + 3), f"{pitch:+7.2f}", font=font_row, fill=CYAN)
        draw.text((x0 + 285, ry + 3), f"{conf:.3f}", font=font_row, fill=conf_colour(conf))
        draw.text((x0 + 365, ry + 3), f"{delta_disp:.3f}", font=font_row, fill=YELLOW)
        draw.text((x0 + 460, ry + 3), f"{cum_disp:.3f}", font=font_row,
                  fill=disp_colour(cum_disp))

        ry += ROW_H

    # fill remaining rows if fewer than MAX_ROWS samples
    for _ in range(MAX_ROWS - len(samples)):
        draw.rectangle([x0, ry, x0 + TILE_W, ry + ROW_H - 1], fill=BG)
        ry += ROW_H

    # ── Section label strip ───────────────────────────────────────────────────
    draw.rectangle([x0, ry, x0 + TILE_W, ry + LABEL_H - 1], fill=(20, 20, 35))
    span = t.get("span_frames", 0)
    sf = t.get("start_frame", 0)
    ef = t.get("end_frame", 0)
    draw.text((x0 + 6, ry + 6),
              f"{section_label}  frames {sf}–{ef}  span={span}  max_gap={t.get('max_internal_gap', 0)}",
              font=font_lbl, fill=DIM)

    # tile border
    draw.rectangle([x0, y0, x0 + TILE_W - 1, y0 + TILE_H - 1], outline=SEP)


def build_pack(tracklets_path, output_dir):
    with open(tracklets_path) as f:
        data = json.load(f)
    all_t = data["tracklets"]

    # ── Build sections ────────────────────────────────────────────────────────
    anchors = sorted(
        [t for t in all_t if t["status"] == "anchor"],
        key=lambda t: -(t.get("anchor_strength_candidate") or 0)
    )[:20]

    passing = sorted(
        [t for t in all_t if t["status"] == "passing"],
        key=lambda t: -(t.get("best_available_score") or 0)
    )[:20]

    zero_disp = sorted(
        [t for t in all_t if t["status"] not in ("rejected_static",)],
        key=lambda t: t.get("net_displacement_deg", 999)
    )[:20]

    sections = [
        ("A: ANCHORS (by anchor_strength desc)", anchors),
        ("B: PASSING (by best_available_score desc)", passing),
        ("C: NEAR-ZERO DISPLACEMENT (all statuses, disp asc)", zero_disp),
    ]

    # ── Layout ───────────────────────────────────────────────────────────────
    # 2 columns of tiles per section, 10 tiles per column = 20 per section
    # Each section: ceil(20/2) rows = 10 rows
    SECTION_TILE_ROWS = 10
    section_h = SECTION_TILE_ROWS * TILE_H + 50   # 50 = section title bar
    total_h   = len(sections) * section_h + SECTION_GAP * len(sections) + 60
    sheet_w   = COLS * TILE_W + 10

    sheet = Image.new("RGB", (sheet_w, total_h), BG)
    draw  = ImageDraw.Draw(sheet)

    font_hdr = _font(14, bold=True)
    font_row = _font(13)
    font_lbl = _font(11)
    font_sec = _font(16, bold=True)

    y_cursor = 20

    for sec_title, tile_list in sections:
        # Section title bar
        draw.rectangle([0, y_cursor, sheet_w, y_cursor + 36], fill=(30, 30, 55))
        draw.text((10, y_cursor + 8), sec_title, font=font_sec, fill=(180, 180, 255))
        y_cursor += 44

        for ti, t in enumerate(tile_list):
            col = ti % COLS
            row = ti // COLS
            x0  = col * TILE_W
            y0  = y_cursor + row * TILE_H
            sec_lbl = sec_title.split(":")[0]
            draw_tile(draw, x0, y0, t, sec_lbl, ti + 1,
                      font_hdr, font_row, font_lbl)

        actual_rows = math.ceil(len(tile_list) / COLS)
        y_cursor += actual_rows * TILE_H + SECTION_GAP

    out_img = os.path.join(output_dir, "stage2_review_pack.png")
    sheet.save(out_img)
    print(f"[review_pack] Saved → {out_img}  size={sheet.size}")

    # ── Text summary ──────────────────────────────────────────────────────────
    status_counts = defaultdict(int)
    for t in all_t:
        status_counts[t["status"]] += 1

    lines = [
        "=== Stage 2 Tracklet Review Summary ===",
        f"Total tracklets: {len(all_t)}",
    ]
    for k, v in sorted(status_counts.items()):
        lines.append(f"  {k}: {v}")
    lines += ["", "Section A — Anchors (top 20 by anchor_strength):"]
    for i, t in enumerate(anchors):
        astr = t.get("anchor_strength_candidate")
        astr_s = f"{astr:.3f}" if astr is not None else "—"
        lines.append(
            f"  #{i+1:02d} {t['id']}  disp={t.get('net_displacement_deg',0):.2f}°  "
            f"str={astr_s}  obs={t['observation_count']}  "
            f"conf={t['mean_weighted_conf']:.3f}  sh={t['confirmed_static_hotspot_frac']:.2f}"
        )
    lines += ["", "Section C — Near-zero displacement (top 20 asc):"]
    for i, t in enumerate(zero_disp):
        lines.append(
            f"  #{i+1:02d} {t['id']}  [{t['status']}]  "
            f"disp={t.get('net_displacement_deg',0):.4f}°  "
            f"obs={t['observation_count']}  sh={t['confirmed_static_hotspot_frac']:.2f}"
        )

    out_txt = os.path.join(output_dir, "stage2_review_summary.txt")
    with open(out_txt, "w") as f:
        f.write("\n".join(lines))
    print(f"[review_pack] Summary → {out_txt}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tracklets",   required=True, help="tracklets.json from Stage 2")
    ap.add_argument("--output-dir",  default="stage2_review_output")
    args = ap.parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    build_pack(args.tracklets, args.output_dir)


if __name__ == "__main__":
    main()
