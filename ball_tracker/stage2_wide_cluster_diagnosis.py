#!/usr/bin/env python3
"""
FFA Stage 2 — Wide Cluster Diagnosis
=====================================
Diagnostic-only. Investigates whether wide repeated-static clusters are
single scene locations or single-linkage chains of multiple locations.

For each target cluster:
  1. Member-level angular-distance distribution from cluster centre.
  2. Natural subclusters via strict local grouping (gap-based + distance threshold).
  3. Pairwise member separation matrix.
  4. Temporal window and representative member report.
  5. Recommendation: keep / split / annotation-only.

Does NOT alter any matching radius, existing annotation, thresholds, statuses,
scores, Stage 1, Stage 1b, Stage 2 linking, renderer, or hotspot map.

Inputs
------
  --report       : stage2_repeated_static_report.json
  --target       : comma-separated cluster IDs (default: C005,C006,C007,C009)
  --split-radius : strict local grouping radius in degrees (default: 1.2°)
  --output-dir   : directory for outputs

Outputs
-------
  wide_cluster_diagnosis.json   — machine-readable per-cluster diagnosis
  wide_cluster_diagnosis.txt    — human-readable report
  wide_cluster_subpack.png      — visual pack (member positions on yaw/pitch scatter
                                  + pairwise distance heatmap per cluster)

Recommendation logic
--------------------
  KEEP AS ONE   : all members within split_radius of the tightest subgroup centre;
                  no natural gap in the distance distribution.
  SPLIT         : clear gap in distance distribution (gap > GAP_THRESHOLD_DEG)
                  separating a tight core from outlier members.
  ANNOTATION-ONLY: members so spread (or so few) that no coherent single location
                   can be identified with confidence (max_pairwise > SPREAD_LIMIT).

Constants (not action thresholds — diagnostic only)
----------------------------------------------------
  SPLIT_RADIUS_DEG = 1.2   local grouping radius
  GAP_THRESHOLD_DEG = 0.5  min gap between consecutive sorted distances to flag split
  SPREAD_LIMIT_DEG  = 3.5  max pairwise distance before annotation-only is recommended
"""

import argparse
import json
import math
import os
import sys

import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow required: pip install Pillow")

# ── Diagnostic constants (not action thresholds) ──────────────────────────────
SPLIT_RADIUS_DEG   = 1.2   # local grouping radius for subcluster detection
GAP_THRESHOLD_DEG  = 0.5   # min gap in sorted dist list to flag a natural split
SPREAD_LIMIT_DEG   = 3.5   # max pairwise → recommend annotation-only

# ── Geometry ──────────────────────────────────────────────────────────────────

def _to_unit(yaw_deg, pitch_deg):
    y, p = math.radians(yaw_deg), math.radians(pitch_deg)
    return (math.cos(p) * math.sin(y), math.sin(p), math.cos(p) * math.cos(y))


def _gc_deg(v1, v2):
    dot = float(sum(a * b for a, b in zip(v1, v2)))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def _pairwise(members):
    """Return (n×n) pairwise great-circle distance matrix."""
    units = [_to_unit(m["median_yaw_deg"], m["median_pitch_deg"]) for m in members]
    n = len(units)
    mat = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            d = _gc_deg(units[i], units[j])
            mat[i][j] = mat[j][i] = d
    return mat


def _subcluster(members, split_radius):
    """
    Simple single-pass greedy subcluster assignment at split_radius.
    Each member joins the first existing subcluster whose centre is within
    split_radius, or seeds a new one.  Centres updated as running mean
    (unit-vector average, re-normalised).
    Returns list of lists of member indices.
    """
    if not members:
        return []
    centres = []   # list of unit vectors
    groups  = []   # list of index lists

    for i, m in enumerate(members):
        uv = np.array(_to_unit(m["median_yaw_deg"], m["median_pitch_deg"]))
        assigned = False
        for g_idx, c in enumerate(centres):
            if _gc_deg(uv.tolist(), c.tolist()) <= split_radius:
                groups[g_idx].append(i)
                # update centre
                new_c = centres[g_idx] * (len(groups[g_idx]) - 1) + uv
                centres[g_idx] = new_c / np.linalg.norm(new_c)
                assigned = True
                break
        if not assigned:
            centres.append(uv.copy())
            groups.append([i])

    return groups


def _gap_in_dists(dists_sorted):
    """Return the largest consecutive gap and its position."""
    if len(dists_sorted) < 2:
        return 0.0, -1
    gaps = [(dists_sorted[i+1] - dists_sorted[i], i)
            for i in range(len(dists_sorted) - 1)]
    max_gap, max_idx = max(gaps, key=lambda x: x[0])
    return max_gap, max_idx


def _recommend(members, pairwise_mat, subclusters, split_radius):
    """
    Return (recommendation_str, reason_str).
    """
    dists = sorted(m["dist_to_centre_deg"] for m in members)
    max_pair = max(pairwise_mat[i][j]
                   for i in range(len(members))
                   for j in range(i + 1, len(members))) if len(members) > 1 else 0.0
    gap, gap_pos = _gap_in_dists(dists)

    if max_pair > SPREAD_LIMIT_DEG:
        return ("ANNOTATION-ONLY",
                f"max pairwise {max_pair:.2f}° > spread limit {SPREAD_LIMIT_DEG}°; "
                "no coherent single location identifiable")

    if gap >= GAP_THRESHOLD_DEG and len(subclusters) > 1:
        core_size = len(subclusters[0])
        return ("SPLIT",
                f"natural gap {gap:.2f}° at dist rank {gap_pos+1}/{len(dists)}; "
                f"{len(subclusters)} subclusters at split_radius={split_radius}°; "
                f"core has {core_size} member(s)")

    return ("KEEP AS ONE",
            f"all members within {dists[-1]:.2f}° of centre; "
            f"no gap >= {GAP_THRESHOLD_DEG}° in distance distribution")


# ── Per-cluster diagnosis ─────────────────────────────────────────────────────

def diagnose_cluster(c, split_radius):
    members   = sorted(c["members"], key=lambda m: m["dist_to_centre_deg"])
    pairwise  = _pairwise(members)
    subclusters = _subcluster(members, split_radius)
    rec, reason = _recommend(members, pairwise, subclusters, split_radius)

    dists = [m["dist_to_centre_deg"] for m in members]
    gap, gap_pos = _gap_in_dists(sorted(dists))

    subcluster_summaries = []
    for g in subclusters:
        g_members = [members[i] for i in g]
        yaws   = [m["median_yaw_deg"]   for m in g_members]
        pitches = [m["median_pitch_deg"] for m in g_members]
        sub_centre_yaw   = float(np.mean(yaws))
        sub_centre_pitch = float(np.mean(pitches))
        sub_max_dist = max(
            _gc_deg(_to_unit(m["median_yaw_deg"], m["median_pitch_deg"]),
                    _to_unit(sub_centre_yaw, sub_centre_pitch))
            for m in g_members
        ) if len(g_members) > 1 else 0.0
        subcluster_summaries.append({
            "member_count":        len(g),
            "member_ids":          [m["id"] for m in g_members],
            "centre_yaw_deg":      round(sub_centre_yaw, 3),
            "centre_pitch_deg":    round(sub_centre_pitch, 3),
            "max_dist_from_sub_centre_deg": round(sub_max_dist, 3),
            "frame_range":         [min(m["first_frame"] for m in g_members),
                                    max(m["last_frame"]  for m in g_members)],
            "representative_id":   g_members[0]["id"],
        })

    return {
        "cluster_id":          c["cluster_id"],
        "centre_yaw_deg":      c["centre_yaw_deg"],
        "centre_pitch_deg":    c["centre_pitch_deg"],
        "cluster_radius_deg":  c["cluster_radius_deg"],
        "member_count":        c["member_count"],
        "recommendation":      rec,
        "reason":              reason,
        "dist_from_centre":    [round(m["dist_to_centre_deg"], 3) for m in members],
        "max_pairwise_deg":    round(
            max((pairwise[i][j] for i in range(len(members))
                 for j in range(i+1, len(members))), default=0.0), 3),
        "largest_gap_deg":     round(gap, 3),
        "gap_at_rank":         gap_pos + 1 if gap_pos >= 0 else None,
        "subcluster_count":    len(subclusters),
        "subclusters":         subcluster_summaries,
        "members":             [
            {
                "id":              m["id"],
                "median_yaw_deg":  m["median_yaw_deg"],
                "median_pitch_deg": m["median_pitch_deg"],
                "dist_to_centre_deg": m["dist_to_centre_deg"],
                "first_frame":     m["first_frame"],
                "last_frame":      m["last_frame"],
                "obs_count":       m["obs_count"],
                "net_disp_deg":    m["net_disp_deg"],
                "status":          m["status"],
            }
            for m in members
        ],
        "pairwise_matrix": [[round(pairwise[i][j], 3) for j in range(len(members))]
                            for i in range(len(members))],
    }


# ── Text report ───────────────────────────────────────────────────────────────

def write_text_report(path, diagnoses, split_radius):
    with open(path, "w") as f:
        f.write("=" * 70 + "\n")
        f.write("STAGE 2 WIDE CLUSTER DIAGNOSIS\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Split radius (local grouping) : {split_radius}°\n")
        f.write(f"Gap threshold                 : {GAP_THRESHOLD_DEG}°\n")
        f.write(f"Spread limit (annotation-only): {SPREAD_LIMIT_DEG}°\n\n")
        f.write("Diagnostic only — no radii, statuses, or thresholds changed.\n\n")

        for d in diagnoses:
            f.write("-" * 70 + "\n")
            f.write(f"{d['cluster_id']}  centre=(yaw={d['centre_yaw_deg']:.3f}°, "
                    f"pitch={d['centre_pitch_deg']:.3f}°)  "
                    f"members={d['member_count']}  "
                    f"cluster_radius={d['cluster_radius_deg']:.3f}°\n\n")

            f.write(f"  *** RECOMMENDATION: {d['recommendation']} ***\n")
            f.write(f"  Reason: {d['reason']}\n\n")

            f.write(f"  Distances from centre (sorted): "
                    f"{[f'{x:.3f}°' for x in d['dist_from_centre']]}\n")
            f.write(f"  Largest gap in dist sequence  : {d['largest_gap_deg']:.3f}°"
                    f"  (at rank {d['gap_at_rank']})\n")
            f.write(f"  Max pairwise separation       : {d['max_pairwise_deg']:.3f}°\n\n")

            f.write("  Members (closest→farthest from centre):\n")
            for m in d["members"]:
                f.write(f"    {m['id']:6s}  yaw={m['median_yaw_deg']:8.3f}°  "
                        f"pitch={m['median_pitch_deg']:7.3f}°  "
                        f"dist={m['dist_to_centre_deg']:.3f}°  "
                        f"frames={m['first_frame']}–{m['last_frame']}  "
                        f"obs={m['obs_count']}  status={m['status']}\n")

            f.write(f"\n  Pairwise distance matrix (°):\n")
            ids = [m["id"] for m in d["members"]]
            col_w = 8
            f.write("         " + "".join(f"{x:>{col_w}}" for x in ids) + "\n")
            for i, row in enumerate(d["pairwise_matrix"]):
                f.write(f"  {ids[i]:6s}  " +
                        "".join(f"{v:>{col_w}.3f}" for v in row) + "\n")

            if d["subclusters"]:
                f.write(f"\n  Subclusters at split_radius={split_radius}°:\n")
                for j, sc in enumerate(d["subclusters"], 1):
                    f.write(f"    Sub{j}  members={sc['member_count']}  "
                            f"centre=(yaw={sc['centre_yaw_deg']:.3f}°, "
                            f"pitch={sc['centre_pitch_deg']:.3f}°)  "
                            f"max_dist={sc['max_dist_from_sub_centre_deg']:.3f}°  "
                            f"frames={sc['frame_range'][0]}–{sc['frame_range'][1]}  "
                            f"rep={sc['representative_id']}\n")
                    f.write(f"           IDs: {', '.join(sc['member_ids'])}\n")
            f.write("\n")


# ── Visual pack ───────────────────────────────────────────────────────────────

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


# Colour palette for subclusters
SUBCOLOURS = [
    (80, 200, 100),   # green
    (230, 160, 50),   # orange
    (80, 160, 230),   # blue
    (210, 80, 210),   # magenta
    (80, 220, 220),   # cyan
]
BG        = (12, 12, 18)
HEADER_BG = (28, 28, 45)
WHITE     = (230, 230, 230)
DIM       = (110, 110, 125)
RED       = (220, 80, 80)
YELLOW    = (220, 200, 60)
GREY      = (80, 80, 95)


def render_cluster_panel(d, panel_w, font_sm, font_md, font_bold):
    """
    Render one cluster panel: scatter plot of member yaw/pitch positions
    + pairwise distance bar chart + subcluster labels.
    """
    SCATTER_W = 420
    SCATTER_H = 280
    BAR_W     = 260
    BAR_H     = SCATTER_H
    PAD       = 12
    HEADER_H  = 32
    TEXT_H    = (len(d["members"]) + len(d["subclusters"]) + 2) * 16 + 20

    panel_h = HEADER_H + max(SCATTER_H, BAR_H) + TEXT_H + PAD * 3
    panel   = Image.new("RGB", (panel_w, panel_h), BG)
    draw    = ImageDraw.Draw(panel)

    # Header
    rec_colour = {
        "KEEP AS ONE":     (80, 200, 100),
        "SPLIT":           (220, 180, 50),
        "ANNOTATION-ONLY": (220, 80, 80),
    }.get(d["recommendation"], WHITE)
    draw.rectangle([0, 0, panel_w, HEADER_H - 1], fill=HEADER_BG)
    draw.text((8, 4),
              f"{d['cluster_id']}  centre=({d['centre_yaw_deg']:.2f}°, {d['centre_pitch_deg']:.2f}°)  "
              f"members={d['member_count']}  ▶ {d['recommendation']}",
              fill=rec_colour, font=font_bold)

    # Scatter: yaw (x) vs pitch (y) for each member
    members = d["members"]
    if members:
        yaws   = [m["median_yaw_deg"]   for m in members]
        pitches = [m["median_pitch_deg"] for m in members]
        yaw_min, yaw_max   = min(yaws),    max(yaws)
        pit_min, pit_max   = min(pitches), max(pitches)
        # add margin
        yaw_rng = max(yaw_max - yaw_min, 0.5)
        pit_rng = max(pit_max - pit_min, 0.5)
        yaw_min -= yaw_rng * 0.2;  yaw_max += yaw_rng * 0.2
        pit_min -= pit_rng * 0.2;  pit_max += pit_rng * 0.2

        def to_px(yaw, pit):
            sx = PAD + int((yaw - yaw_min) / (yaw_max - yaw_min) * (SCATTER_W - 2 * PAD))
            sy = HEADER_H + PAD + SCATTER_H - PAD - int(
                (pit - pit_min) / (pit_max - pit_min) * (SCATTER_H - 2 * PAD))
            return sx, sy

        # axes labels
        draw.text((PAD, HEADER_H + SCATTER_H + 2), f"yaw  [{yaw_min:.1f}° → {yaw_max:.1f}°]",
                  fill=DIM, font=font_sm)

        # build subcluster colour map
        sub_colour_map = {}
        for s_idx, sc in enumerate(d["subclusters"]):
            col = SUBCOLOURS[s_idx % len(SUBCOLOURS)]
            for mid in sc["member_ids"]:
                sub_colour_map[mid] = col

        # grid lines
        for m in members:
            px, py = to_px(m["median_yaw_deg"], m["median_pitch_deg"])
            draw.line([px, HEADER_H + PAD, px, HEADER_H + SCATTER_H - PAD],
                      fill=(30, 30, 40), width=1)
            draw.line([PAD, py, SCATTER_W - PAD, py], fill=(30, 30, 40), width=1)

        # cluster centre
        cx, cy = to_px(d["centre_yaw_deg"], d["centre_pitch_deg"])
        r = 6
        draw.ellipse([cx-r, cy-r, cx+r, cy+r], outline=WHITE, width=2)
        draw.line([cx-r-4, cy, cx+r+4, cy], fill=WHITE, width=1)
        draw.line([cx, cy-r-4, cx, cy+r+4], fill=WHITE, width=1)

        # member dots
        for m in members:
            px, py = to_px(m["median_yaw_deg"], m["median_pitch_deg"])
            col = sub_colour_map.get(m["id"], GREY)
            r2 = 7
            draw.ellipse([px-r2, py-r2, px+r2, py+r2], fill=col, outline=WHITE, width=1)
            draw.text((px+9, py-7), m["id"], fill=col, font=font_sm)

        # subcluster centres
        for s_idx, sc in enumerate(d["subclusters"]):
            col = SUBCOLOURS[s_idx % len(SUBCOLOURS)]
            sx, sy = to_px(sc["centre_yaw_deg"], sc["centre_pitch_deg"])
            draw.rectangle([sx-4, sy-4, sx+4, sy+4], outline=col, width=2)

    # Bar chart: sorted distances from centre
    bar_x0 = SCATTER_W + PAD * 2
    bar_y0 = HEADER_H + PAD
    dists  = d["dist_from_centre"]  # already sorted
    if dists:
        max_dist = max(dists) if dists else 1.0
        bar_slot = (BAR_H - 2 * PAD) // len(dists)
        bar_slot = max(bar_slot, 10)
        draw.text((bar_x0, bar_y0 - 14), "dist from centre →", fill=DIM, font=font_sm)
        for i, dist in enumerate(dists):
            by = bar_y0 + i * bar_slot
            bw = int(dist / max(max_dist, 0.01) * (BAR_W - 60))
            mid = d["members"][i]
            col = sub_colour_map.get(mid["id"], GREY) if members else GREY
            draw.rectangle([bar_x0, by, bar_x0 + bw, by + bar_slot - 3], fill=col)
            draw.text((bar_x0 + bw + 4, by + 1),
                      f"{dist:.3f}°  {mid['id']}", fill=WHITE, font=font_sm)

        # Gap indicator
        sorted_dists = sorted(dists)
        gap, gap_pos = _gap_in_dists(sorted_dists)
        if gap >= GAP_THRESHOLD_DEG:
            gy = bar_y0 + (gap_pos + 1) * bar_slot - bar_slot // 2
            draw.line([bar_x0, gy, bar_x0 + BAR_W - 60, gy], fill=YELLOW, width=2)
            draw.text((bar_x0 + BAR_W - 55, gy - 8),
                      f"gap {gap:.2f}°", fill=YELLOW, font=font_sm)

    # Text summary
    ty = HEADER_H + SCATTER_H + PAD * 2
    draw.text((PAD, ty), f"Reason: {d['reason']}", fill=WHITE, font=font_sm)
    ty += 16
    draw.text((PAD, ty),
              f"Max pairwise: {d['max_pairwise_deg']:.3f}°   "
              f"Largest gap: {d['largest_gap_deg']:.3f}°   "
              f"Subclusters: {d['subcluster_count']}",
              fill=DIM, font=font_sm)
    ty += 16
    for s_idx, sc in enumerate(d["subclusters"]):
        col = SUBCOLOURS[s_idx % len(SUBCOLOURS)]
        draw.text((PAD, ty),
                  f"  Sub{s_idx+1}: {sc['member_count']} members  "
                  f"centre=({sc['centre_yaw_deg']:.2f}°, {sc['centre_pitch_deg']:.2f}°)  "
                  f"max_dist={sc['max_dist_from_sub_centre_deg']:.3f}°  "
                  f"frames={sc['frame_range'][0]}–{sc['frame_range'][1]}  "
                  f"IDs: {', '.join(sc['member_ids'])}",
                  fill=col, font=font_sm)
        ty += 16

    return panel


def render_visual_pack(diagnoses, out_path, split_radius):
    font_sm   = _font(11)
    font_md   = _font(13)
    font_bold = _font(14, bold=True)

    PANEL_W = 920
    GAP     = 20
    TITLE_H = 44

    panels = [render_cluster_panel(d, PANEL_W, font_sm, font_md, font_bold)
              for d in diagnoses]
    total_h = TITLE_H + sum(p.height + GAP for p in panels)

    pack = Image.new("RGB", (PANEL_W, total_h), BG)
    draw = ImageDraw.Draw(pack)
    draw.rectangle([0, 0, PANEL_W, TITLE_H - 1], fill=(20, 20, 45))
    draw.text((10, 8),
              "FFA Stage 2 — Wide Cluster Diagnosis (C005, C006, C007, C009)",
              fill=WHITE, font=font_bold)
    draw.text((10, 26),
              f"split_radius={split_radius}°  gap_threshold={GAP_THRESHOLD_DEG}°  "
              f"spread_limit={SPREAD_LIMIT_DEG}°  |  Diagnostic only — no thresholds changed",
              fill=DIM, font=font_sm)

    y = TITLE_H
    for p in panels:
        pack.paste(p, (0, y))
        y += p.height + GAP

    pack.save(out_path, "PNG")
    print(f"[wide-cluster-diagnosis] visual pack -> {out_path}  "
          f"({pack.width}×{pack.height})", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Wide cluster diagnosis (diagnostic only)")
    parser.add_argument("--report",       required=True,
                        help="stage2_repeated_static_report.json")
    parser.add_argument("--target",       default="C005,C006,C007,C009")
    parser.add_argument("--split-radius", type=float, default=SPLIT_RADIUS_DEG,
                        dest="split_radius")
    parser.add_argument("--output-dir",   default=".", dest="output_dir")
    args = parser.parse_args()

    target_ids = [x.strip() for x in args.target.split(",")]
    os.makedirs(args.output_dir, exist_ok=True)

    with open(args.report) as f:
        report = json.load(f)

    diagnoses = []
    for cid in target_ids:
        c = next((x for x in report["clusters"] if x["cluster_id"] == cid), None)
        if not c:
            print(f"[wide-cluster-diagnosis] WARNING: {cid} not found — skipping", flush=True)
            continue
        diag = diagnose_cluster(c, args.split_radius)
        diagnoses.append(diag)
        print(f"[wide-cluster-diagnosis] {cid}  → {diag['recommendation']}  "
              f"(max_pairwise={diag['max_pairwise_deg']:.3f}°  "
              f"gap={diag['largest_gap_deg']:.3f}°  "
              f"subclusters={diag['subcluster_count']})", flush=True)

    # JSON
    json_path = os.path.join(args.output_dir, "wide_cluster_diagnosis.json")
    with open(json_path, "w") as f:
        json.dump({"split_radius_deg": args.split_radius,
                   "gap_threshold_deg": GAP_THRESHOLD_DEG,
                   "spread_limit_deg": SPREAD_LIMIT_DEG,
                   "diagnoses": diagnoses}, f, indent=2)
    print(f"[wide-cluster-diagnosis] JSON  -> {json_path}", flush=True)

    # Text
    txt_path = os.path.join(args.output_dir, "wide_cluster_diagnosis.txt")
    write_text_report(txt_path, diagnoses, args.split_radius)
    print(f"[wide-cluster-diagnosis] text  -> {txt_path}", flush=True)

    # Visual pack
    png_path = os.path.join(args.output_dir, "wide_cluster_subpack.png")
    render_visual_pack(diagnoses, png_path, args.split_radius)

    print("[wide-cluster-diagnosis] DONE", flush=True)


if __name__ == "__main__":
    main()
