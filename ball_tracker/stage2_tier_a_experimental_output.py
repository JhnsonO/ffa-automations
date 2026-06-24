#!/usr/bin/env python3
"""
FFA Stage 2 — Tier A Experimental Output Path
==============================================
EXPERIMENT ONLY. Do not wire into renderer or activate globally.

Decision context (24 June 2026):
  T0275, T0334, T0394 were visually reviewed and are false associations,
  not credible ball tracks. Tier A dry-run safety review is cleared for
  this smoke clip.

Purpose
-------
1. Starts from Stage 1b-quarantined candidates.
2. Applies the frozen Tier A location manifest (via stage1_tier_a_dry_run_filter.py).
3. Runs Stage 2 temporal linker unchanged on the filtered candidates.
4. Propagates detection_geometry from Stage 1c candidates into tracklet observations.
   stage2_temporal_link.py is frozen and does not carry geometry; this step
   stitches it back using a (frame, yaw, pitch) lookup without altering any
   tracking logic or thresholds.
5. Writes clearly labelled experimental output files.
6. Generates a focused review pack with:
   - all experimental anchors
   - highest-confidence passing tracklets
   - representative fragments
   - side-by-side original vs Tier A summary counts

Inputs
------
  --stage1-candidates  : Stage 1b-quarantined candidates (frame-indexed JSON)
  --original-tracklets : Existing tracklets.json (original Stage 2 run)
  --hotspot-map        : hotspot_map.json
  --output-dir         : output directory

Outputs (all labelled experimental)
-------
  stage1_candidates_tier_a_experimental.json
  tracklets_tier_a_experimental.json
  gaps_tier_a_experimental.json
  tier_a_experimental_review_pack.png
  tier_a_experimental_summary.txt
  tier_a_experimental_counts.json
"""

import argparse
import json
import math
import os
import sys
import importlib.util

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    sys.exit("Pillow required: pip install Pillow")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _great_circle(y1, p1, y2, p2):
    def uv(y, p):
        yr, pr = math.radians(y), math.radians(p)
        return (math.cos(pr) * math.sin(yr), math.sin(pr), math.cos(pr) * math.cos(yr))
    a, b = uv(y1, p1), uv(y2, p2)
    dot = max(-1.0, min(1.0, a[0]*b[0] + a[1]*b[1] + a[2]*b[2]))
    return math.degrees(math.acos(dot))


# ── Geometry propagation ───────────────────────────────────────────────────────

def _build_geometry_index(filtered_candidates: dict) -> dict:
    """
    Build a lookup from (frame_int, yaw_4dp, pitch_4dp) -> detection_geometry.
    Candidates with no detection_geometry key map to None.
    Stage 0 reuse candidates carry explicit null values inside detection_geometry;
    those are preserved as-is so downstream consumers can distinguish
    'geometry absent' (key missing) from 'Stage-0-reuse null'.
    """
    index = {}
    frames_raw = filtered_candidates.get("frames", {})
    for frame_key, cands in frames_raw.items():
        fidx = int(frame_key)
        for c in (cands if isinstance(cands, list) else []):
            yaw = c.get("yaw")
            pitch = c.get("pitch")
            if yaw is None or pitch is None:
                continue
            key = (fidx, round(yaw, 4), round(pitch, 4))
            # detection_geometry may be absent (old candidate), present with values,
            # or present with all-null values (Stage 0 reuse). Preserve whatever is there.
            index[key] = c.get("detection_geometry")
    return index


def _propagate_geometry(tracklets_data: dict, geometry_index: dict) -> tuple[dict, dict]:
    """
    Inject detection_geometry into each tracklet observation in-place.
    Returns (updated_tracklets_data, coverage_stats).
    Observations with no matching candidate get detection_geometry=None.
    Source values are never modified.
    """
    total_obs = 0
    matched_obs = 0
    populated_obs = 0  # matched AND has at least one non-null geometry field

    for t in tracklets_data.get("tracklets", []):
        for obs in t.get("frames", []):
            total_obs += 1
            frame = obs.get("frame")
            yaw = obs.get("yaw")
            pitch = obs.get("pitch")
            if frame is None or yaw is None or pitch is None:
                obs["detection_geometry"] = None
                continue
            key = (int(frame), round(yaw, 4), round(pitch, 4))
            geo = geometry_index.get(key)
            obs["detection_geometry"] = geo
            if geo is not None:
                matched_obs += 1
                # Check if any field is non-null (Stage 1c fresh detection vs Stage 0 reuse)
                if isinstance(geo, dict) and any(v is not None for v in geo.values()):
                    populated_obs += 1

    coverage_stats = {
        "total_observations": total_obs,
        "geometry_matched": matched_obs,
        "geometry_populated": populated_obs,
        "geo_coverage_fraction": round(populated_obs / total_obs, 4) if total_obs else 0.0,
    }
    return tracklets_data, coverage_stats


# ── Review pack ────────────────────────────────────────────────────────────────

TILE_W      = 960
ROW_H       = 22
HEADER_H    = 56
LABEL_H     = 28
MAX_ROWS    = 10
TILE_H      = HEADER_H + MAX_ROWS * ROW_H + LABEL_H
COLS        = 2

BG          = (12, 12, 18)
HDR_BG      = (30, 30, 50)
ROW_ALT     = (20, 20, 32)
SEP         = (55, 55, 80)
WHITE       = (225, 225, 225)
DIM         = (120, 120, 135)
GREEN       = (80, 220, 100)
YELLOW      = (220, 200, 60)
RED         = (220, 80, 80)
CYAN        = (80, 200, 220)
MAGENTA     = (200, 80, 200)
ORANGE      = (240, 160, 50)


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


def _sample(frames, n=MAX_ROWS):
    if len(frames) <= n:
        return frames
    idx = [0]
    step = (len(frames) - 1) / (n - 1)
    for i in range(1, n - 1):
        idx.append(round(i * step))
    idx.append(len(frames) - 1)
    seen, out = set(), []
    for i in sorted(set(idx)):
        if i not in seen:
            out.append(frames[i])
            seen.add(i)
    return out


def _conf_col(c):
    return GREEN if c >= 0.60 else (YELLOW if c >= 0.30 else RED)


def _disp_col(d):
    return GREEN if d >= 5.0 else (YELLOW if d >= 1.0 else RED)


def _draw_tile(draw, x0, y0, t, section_label, rank, fh, fr, fl):
    frames = t.get("frames", [])
    samples = _sample(frames)

    status = t["status"]
    status_col = GREEN if status == "anchor" else (YELLOW if status == "passing" else MAGENTA)

    draw.rectangle([x0, y0, x0 + TILE_W, y0 + HEADER_H - 1], fill=HDR_BG)
    draw.text((x0 + 6, y0 + 4), f"#{rank:02d}  {t['id']}  [{status}]  [TIER_A_EXPERIMENTAL]",
              font=fh, fill=status_col)

    astr = t.get("anchor_strength_candidate")
    basc = t.get("best_available_score")
    disp = t.get("net_displacement_deg", 0.0)
    spread = t.get("spatial_spread_deg", 0.0)
    obs = t.get("observation_count", 0)
    cov = t.get("coverage_ratio", 0.0)
    mconf = t.get("mean_weighted_conf", 0.0)
    sh = t.get("confirmed_static_hotspot_frac", 0.0)
    vc = t.get("velocity_consistency", 0.0)

    astr_s = f"{astr:.3f}" if astr is not None else "—"
    basc_s = f"{basc:.3f}" if basc is not None else "—"
    line2 = (f"obs={obs}  cov={cov:.2f}  conf={mconf:.3f}  "
             f"disp={disp:.2f}°  spread={spread:.2f}°  "
             f"str={astr_s}  bas={basc_s}  sh={sh:.2f}  vel_c={vc:.2f}")
    draw.text((x0 + 6, y0 + 30), line2, font=fl, fill=DIM)
    draw.line([(x0, y0 + HEADER_H), (x0 + TILE_W, y0 + HEADER_H)], fill=SEP)

    # Column headers
    ry = y0 + HEADER_H
    draw.rectangle([x0, ry, x0 + TILE_W, ry + ROW_H - 1], fill=(24, 24, 40))
    for cx, lbl in [(6, "frame"), (95, "yaw°"), (200, "pitch°"),
                    (300, "conf"), (385, "Δdisp°"), (485, "cumdisp°")]:
        draw.text((x0 + cx, ry + 3), lbl, font=fl, fill=DIM)
    ry += ROW_H

    first_yaw = samples[0]["yaw"] if samples else 0.0
    first_pitch = samples[0]["pitch"] if samples else 0.0
    prev_y, prev_p = first_yaw, first_pitch

    for ri, fr_ in enumerate(samples):
        bg = ROW_ALT if ri % 2 == 0 else BG
        draw.rectangle([x0, ry, x0 + TILE_W, ry + ROW_H - 1], fill=bg)
        yaw, pitch, conf, frame = fr_["yaw"], fr_["pitch"], fr_["weighted_conf"], fr_["frame"]
        cum = _great_circle(first_yaw, first_pitch, yaw, pitch)
        delta = _great_circle(prev_y, prev_p, yaw, pitch)
        prev_y, prev_p = yaw, pitch
        draw.text((x0 + 6,   ry + 3), f"{frame:5d}", font=fr, fill=WHITE)
        draw.text((x0 + 95,  ry + 3), f"{yaw:+8.2f}", font=fr, fill=CYAN)
        draw.text((x0 + 200, ry + 3), f"{pitch:+7.2f}", font=fr, fill=CYAN)
        draw.text((x0 + 300, ry + 3), f"{conf:.3f}", font=fr, fill=_conf_col(conf))
        draw.text((x0 + 385, ry + 3), f"{delta:.3f}", font=fr, fill=YELLOW)
        draw.text((x0 + 485, ry + 3), f"{cum:.3f}", font=fr, fill=_disp_col(cum))
        ry += ROW_H

    for _ in range(MAX_ROWS - len(samples)):
        draw.rectangle([x0, ry, x0 + TILE_W, ry + ROW_H - 1], fill=BG)
        ry += ROW_H

    draw.rectangle([x0, ry, x0 + TILE_W, ry + LABEL_H - 1], fill=(18, 18, 30))
    sf, ef = t.get("start_frame", 0), t.get("end_frame", 0)
    span = t.get("span_frames", 0)
    draw.text((x0 + 6, ry + 6),
              f"{section_label}  frames {sf}–{ef}  span={span}  max_gap={t.get('max_internal_gap', 0)}",
              font=fl, fill=DIM)
    draw.rectangle([x0, y0, x0 + TILE_W - 1, y0 + TILE_H - 1], outline=SEP)


def _build_review_pack(all_t, output_dir):
    # All anchors (sorted by anchor_strength desc)
    anchors = sorted(
        [t for t in all_t if t["status"] == "anchor"],
        key=lambda t: -(t.get("anchor_strength_candidate") or 0)
    )
    # Top passing by best_available_score (cap 20)
    passing = sorted(
        [t for t in all_t if t["status"] == "passing"],
        key=lambda t: -(t.get("best_available_score") or 0)
    )[:20]
    # Representative fragments: every 5th by observation_count desc (cap 10)
    fragments_pool = sorted(
        [t for t in all_t if t["status"] == "fragment"],
        key=lambda t: -t.get("observation_count", 0)
    )
    fragments = [fragments_pool[i] for i in range(0, min(len(fragments_pool), 50), 5)][:10]

    sections = [
        ("A: ALL ANCHORS [TIER_A_EXPERIMENTAL]", anchors),
        ("B: TOP PASSING TRACKLETS [TIER_A_EXPERIMENTAL]", passing),
        ("C: REPRESENTATIVE FRAGMENTS [TIER_A_EXPERIMENTAL]", fragments),
    ]

    SECTION_TILE_ROWS = max(math.ceil(max(len(s[1]) for s in sections) / COLS), 1)
    section_h = SECTION_TILE_ROWS * TILE_H + 50
    total_h = len(sections) * section_h + 30 * len(sections) + 60
    sheet_w = COLS * TILE_W + 10

    sheet = Image.new("RGB", (sheet_w, total_h), BG)
    draw = ImageDraw.Draw(sheet)

    fh = _font(14, bold=True)
    fr = _font(13)
    fl = _font(11)
    fs = _font(16, bold=True)

    y = 20
    for sec_title, tile_list in sections:
        draw.rectangle([0, y, sheet_w, y + 36], fill=(28, 28, 55))
        draw.text((10, y + 8), sec_title, font=fs, fill=ORANGE)
        y += 44

        for ti, t in enumerate(tile_list):
            col = ti % COLS
            row = ti // COLS
            x0 = col * TILE_W
            y0 = y + row * TILE_H
            sec_lbl = sec_title.split(":")[0]
            _draw_tile(draw, x0, y0, t, sec_lbl, ti + 1, fh, fr, fl)

        actual_rows = math.ceil(max(len(tile_list), 1) / COLS)
        y += actual_rows * TILE_H + 30

    out_img = os.path.join(output_dir, "tier_a_experimental_review_pack.png")
    sheet.save(out_img)
    print(f"[review_pack] → {out_img}  size={sheet.size}")
    return anchors, passing, fragments


# ── Main ───────────────────────────────────────────────────────────────────────

def run(args):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    filter_path = os.path.join(repo_root, "ball_tracker", "stage1_tier_a_dry_run_filter.py")
    linker_path = os.path.join(repo_root, "ball_tracker", "stage2_temporal_link.py")

    os.makedirs(args.output_dir, exist_ok=True)

    # ── Step 1: Apply Tier A filter ──────────────────────────────────────────
    print("\n=== STEP 1: Tier A filter (frozen manifest) ===")
    filter_mod = _load_module("tier_a_filter", filter_path)

    class FilterArgs:
        stage1_candidates = args.stage1_candidates
        output_dir = os.path.join(args.output_dir, "_filter_tmp")

    os.makedirs(FilterArgs.output_dir, exist_ok=True)
    filter_mod.run(FilterArgs())

    filtered_path = os.path.join(FilterArgs.output_dir, "stage1_candidates_tier_a_dry_run.json")

    # Rename to experimental label
    experimental_candidates_path = os.path.join(
        args.output_dir, "stage1_candidates_tier_a_experimental.json"
    )
    with open(filtered_path) as f:
        filtered_data = json.load(f)
    # Add experimental label to meta
    meta = filtered_data.get("_dry_run_meta", {})
    meta["experimental_output_label"] = "stage1_candidates_tier_a_experimental"
    meta["decision_cleared"] = (
        "T0275/T0334/T0394 reviewed as false associations. "
        "Safety review cleared 24 June 2026."
    )
    filtered_data["_dry_run_meta"] = meta
    with open(experimental_candidates_path, "w") as f:
        json.dump(filtered_data, f, indent=2)
    print(f"  → {experimental_candidates_path}")

    # ── Step 2: Stage 2 linker (unchanged) ──────────────────────────────────
    print("\n=== STEP 2: Stage 2 temporal linker (unchanged) ===")
    linker_mod = _load_module("stage2_linker", linker_path)

    linker_tmp = os.path.join(args.output_dir, "_linker_tmp")
    os.makedirs(linker_tmp, exist_ok=True)

    class LinkerArgs:
        stage1_candidates = experimental_candidates_path
        hotspot_map = args.hotspot_map
        output_dir = linker_tmp
        min_support_conf = 0.10
        max_link_gap = 5
        base_tolerance = 5.0
        max_speed = 8.0
        min_anchor_str = 0.55

    # Patch defaults before run
    linker_mod.MIN_SUPPORT_CONF = LinkerArgs.min_support_conf
    linker_mod.MAX_LINK_GAP = LinkerArgs.max_link_gap
    linker_mod.BASE_TOLERANCE_DEG = LinkerArgs.base_tolerance
    linker_mod.MAX_SPEED_DEG_PER_FRAME = LinkerArgs.max_speed
    linker_mod.MIN_ANCHOR_STRENGTH = LinkerArgs.min_anchor_str
    linker_mod._tracklet_counter = 0

    linker_mod.run(LinkerArgs())

    # ── Step 3: Propagate detection_geometry from Stage 1c candidates ────────
    print("\n=== STEP 3: Geometry propagation (Stage 1c → tracklet observations) ===")
    print("  stage2_temporal_link.py is frozen; geometry stitched here without")
    print("  altering tracking logic, thresholds, or source values.")

    geometry_index = _build_geometry_index(filtered_data)
    print(f"  Geometry index built: {len(geometry_index)} candidate keys")

    with open(os.path.join(linker_tmp, "tracklets.json")) as f:
        tracklets_data = json.load(f)

    tracklets_data, coverage_stats = _propagate_geometry(tracklets_data, geometry_index)

    print(f"  total_observations   : {coverage_stats['total_observations']}")
    print(f"  geometry_matched     : {coverage_stats['geometry_matched']}")
    print(f"  geometry_populated   : {coverage_stats['geometry_populated']}  "
          f"(non-null fields; Stage 1c fresh detections)")
    print(f"  geo_coverage_fraction: {coverage_stats['geo_coverage_fraction']:.4f}")

    if coverage_stats["total_observations"] > 0 and coverage_stats["geometry_populated"] == 0:
        print("  WARN: geo_coverage_fraction=0.0 — geometry propagation produced no populated fields.")
        print("        Check that filtered_data uses the Stage 1c schema with detection_geometry.")

    # Rename to experimental labels
    experimental_tracklets_path = os.path.join(
        args.output_dir, "tracklets_tier_a_experimental.json"
    )
    experimental_gaps_path = os.path.join(
        args.output_dir, "gaps_tier_a_experimental.json"
    )

    tracklets_data["_experimental_meta"] = {
        "label": "tracklets_tier_a_experimental",
        "input": "stage1_candidates_tier_a_experimental.json",
        "linker": "stage2_temporal_link.py (unchanged)",
        "approved_active_suppression": False,
        "geometry_propagation": coverage_stats,
    }
    with open(experimental_tracklets_path, "w") as f:
        json.dump(tracklets_data, f, indent=2)

    with open(os.path.join(linker_tmp, "gaps.json")) as f:
        gaps_data = json.load(f)
    gaps_data["_experimental_meta"] = {
        "label": "gaps_tier_a_experimental",
        "input": "stage1_candidates_tier_a_experimental.json",
    }
    with open(experimental_gaps_path, "w") as f:
        json.dump(gaps_data, f, indent=2)

    print(f"  → {experimental_tracklets_path}")
    print(f"  → {experimental_gaps_path}")

    # ── Step 4: Load original tracklets for comparison ───────────────────────
    print("\n=== STEP 4: Side-by-side counts ===")
    all_t = tracklets_data["tracklets"]

    from collections import defaultdict
    exp_counts = defaultdict(int)
    for t in all_t:
        exp_counts[t["status"]] += 1

    orig_counts = {}
    if args.original_tracklets and os.path.exists(args.original_tracklets):
        with open(args.original_tracklets) as f:
            orig_data = json.load(f)
        orig_all = orig_data["tracklets"]
        for t in orig_all:
            s = t["status"]
            orig_counts[s] = orig_counts.get(s, 0) + 1
        orig_total = len(orig_all)
    else:
        orig_total = None
        print("  WARN: --original-tracklets not provided or not found; skipping comparison.")

    exp_total = len(all_t)

    counts_doc = {
        "experiment": "tier_a_experimental",
        "approved_active_suppression": False,
        "original": {"total": orig_total, "by_status": orig_counts} if orig_total else None,
        "experimental": {"total": exp_total, "by_status": dict(exp_counts)},
        "geometry_coverage": coverage_stats,
    }
    counts_path = os.path.join(args.output_dir, "tier_a_experimental_counts.json")
    with open(counts_path, "w") as f:
        json.dump(counts_doc, f, indent=2)

    # Print side-by-side
    all_statuses = sorted(set(list(orig_counts.keys()) + list(exp_counts.keys())))
    print(f"\n  {'Status':<25} {'Original':>10} {'Tier A Exp':>12}  {'Delta':>8}")
    print(f"  {'-'*25} {'-'*10} {'-'*12}  {'-'*8}")
    for s in all_statuses:
        o = orig_counts.get(s, 0)
        e = exp_counts.get(s, 0)
        delta = e - o
        delta_s = f"{delta:+d}" if orig_total else "—"
        print(f"  {s:<25} {o:>10} {e:>12}  {delta_s:>8}")
    print(f"  {'TOTAL':<25} {orig_total or '—':>10} {exp_total:>12}")

    # ── Step 5: Review pack ──────────────────────────────────────────────────
    print("\n=== STEP 5: Review pack ===")
    anchors, passing, fragments = _build_review_pack(all_t, args.output_dir)

    # Text summary
    lines = [
        "=== Tier A Experimental Stage 2 Output — Review Summary ===",
        "EXPERIMENT ONLY — No active suppression approved.",
        "T0275/T0334/T0394 reviewed as false associations. Safety review cleared 24 June 2026.",
        "",
        "--- Geometry Propagation ---",
        f"  total_observations   : {coverage_stats['total_observations']}",
        f"  geometry_matched     : {coverage_stats['geometry_matched']}",
        f"  geometry_populated   : {coverage_stats['geometry_populated']}",
        f"  geo_coverage_fraction: {coverage_stats['geo_coverage_fraction']:.4f}",
        "",
        "--- Side-by-Side Counts ---",
    ]
    if orig_total:
        lines.append(f"{'Status':<25} {'Original':>10} {'Tier A Exp':>12}  {'Delta':>8}")
        for s in all_statuses:
            o = orig_counts.get(s, 0)
            e = exp_counts.get(s, 0)
            lines.append(f"{s:<25} {o:>10} {e:>12}  {e-o:>+8d}")
        lines.append(f"{'TOTAL':<25} {orig_total:>10} {exp_total:>12}")
    else:
        lines.append(f"Experimental total: {exp_total}")
        for s, c in sorted(exp_counts.items()):
            lines.append(f"  {s}: {c}")
    lines += [
        "",
        "--- All Experimental Anchors (by anchor_strength desc) ---",
    ]
    for i, t in enumerate(anchors):
        astr = t.get("anchor_strength_candidate")
        astr_s = f"{astr:.3f}" if astr is not None else "—"
        lines.append(
            f"  #{i+1:02d} {t['id']}  disp={t.get('net_displacement_deg',0):.2f}°  "
            f"str={astr_s}  obs={t['observation_count']}  "
            f"conf={t['mean_weighted_conf']:.3f}  sh={t['confirmed_static_hotspot_frac']:.2f}"
        )
    lines += [
        "",
        "--- Top Passing Tracklets (by best_available_score desc, top 20) ---",
    ]
    for i, t in enumerate(passing):
        basc = t.get("best_available_score")
        basc_s = f"{basc:.3f}" if basc is not None else "—"
        lines.append(
            f"  #{i+1:02d} {t['id']}  disp={t.get('net_displacement_deg',0):.2f}°  "
            f"bas={basc_s}  obs={t['observation_count']}  "
            f"conf={t['mean_weighted_conf']:.3f}"
        )
    lines += [
        "",
        "--- Representative Fragments (every 5th by obs desc, cap 10) ---",
    ]
    for i, t in enumerate(fragments):
        lines.append(
            f"  #{i+1:02d} {t['id']}  disp={t.get('net_displacement_deg',0):.2f}°  "
            f"obs={t['observation_count']}  conf={t['mean_weighted_conf']:.3f}"
        )
    lines += [
        "",
        "ACCEPTANCE QUESTION:",
        "Does Tier A filtering leave a smaller, more plausible set of ball-track",
        "candidates worth connecting to the follow-cam stage?",
    ]

    summary_path = os.path.join(args.output_dir, "tier_a_experimental_summary.txt")
    with open(summary_path, "w") as f:
        f.write("\n".join(lines))
    print(f"\n[summary] → {summary_path}")

    print("\n=== All outputs ===")
    for fname in sorted(os.listdir(args.output_dir)):
        p = os.path.join(args.output_dir, fname)
        if os.path.isfile(p):
            print(f"  {fname}  ({os.path.getsize(p):,} bytes)")


def main():
    ap = argparse.ArgumentParser(
        description="FFA Stage 2 Tier A Experimental Output Path (EXPERIMENT ONLY)"
    )
    ap.add_argument("--stage1-candidates",  required=True,
                    help="Stage 1b-quarantined candidates (frame-indexed JSON)")
    ap.add_argument("--original-tracklets", default=None,
                    help="Original tracklets.json for side-by-side count comparison")
    ap.add_argument("--hotspot-map",        required=True)
    ap.add_argument("--output-dir",         default="tier_a_experimental_output")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
