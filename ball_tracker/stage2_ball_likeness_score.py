#!/usr/bin/env python3
"""
stage2_ball_likeness_score.py — Temporal ball-likeness score: out-of-sample validation.

Diagnostic-only. No thresholds changed. No verdicts assigned.
No modifications to Stage 1, Stage 1b, Stage 2, Tier A behaviour, or renderer.

Inputs:
  --csv        tier_a_anchor_adjudication_filled.csv  (with verdicts filled for ball/FP)
  --tracklets  tracklets_tier_a_experimental.json

Outputs (to --output-dir):
  ball_likeness_scores.csv          ranked CSV of unclear anchors
  ball_likeness_review_pack.png     visual review pack:
                                      top-8 unclear + bottom-8 unclear
                                      + mandatory FP controls (T0093, T0080)
  ball_likeness_summary.txt         score formula, feature values, FP rank check

Formula (documented explicitly):

  Four normalised features, each scaled to [0,1] using min-max across all anchors:
    F1 = obs_count             (weight 0.35)  — primary discriminator Δ/σ=2.54
    F2 = spatial_spread_deg    (weight 0.30)  — second-strongest spatial signal Δ/σ=2.15
    F3 = vel_consistency       (weight 0.20)  — motion smoothness proxy Δ/σ=1.53
    F4 = net_disp_deg          (weight 0.15)  — net displacement Δ/σ=1.69

  Weights reflect label-analysis discriminability ranking. span_frames excluded because
  it is collinear with obs_count (r≈0.99 expected). anchor_strength excluded because
  Δ/σ=1.55 is marginally lower and it would double-count the confidence signal.

  score = 0.35*F1 + 0.30*F2 + 0.20*F3 + 0.15*F4

  Min-max normalisation uses the full anchor population (ball + FP + unclear) so that
  the scale is grounded in observed data, not arbitrary. Anchors missing a feature
  contribute 0.0 to that component (conservative).
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path

MANDATORY_FP_CONTROLS = ["T0093", "T0080"]


# ── Verdict normalisation ─────────────────────────────────────────────────────

def _normalise_verdict(raw: str) -> str | None:
    r = raw.strip().lower().replace(" ", "_").replace("-", "_")
    if not r:
        return None
    if r in ("likely_ball", "ball", "b"):
        return "ball"
    if r in ("likely_false_positive", "false_positive", "false", "fp", "f"):
        return "fp"
    if r in ("unclear", "?", "u", "unsure"):
        return "unclear"
    return None


# ── Feature extraction ────────────────────────────────────────────────────────

def _vel_consistency(obs_list: list) -> float | None:
    """Velocity consistency: fraction of inter-frame steps below 2× median step size."""
    steps = []
    prev_yaw, prev_pitch = None, None
    for obs in obs_list:
        yaw = obs.get("yaw")
        pitch = obs.get("pitch")
        if yaw is not None and pitch is not None:
            if prev_yaw is not None:
                dy = yaw - prev_yaw
                dp = pitch - prev_pitch
                steps.append(math.sqrt(dy * dy + dp * dp))
            prev_yaw, prev_pitch = yaw, pitch
    if len(steps) < 2:
        return None
    sorted_steps = sorted(steps)
    n = len(sorted_steps)
    mid = n // 2
    median = sorted_steps[mid] if n % 2 else (sorted_steps[mid - 1] + sorted_steps[mid]) / 2
    if median == 0:
        return 1.0
    threshold = 2.0 * median
    consistent = sum(1 for s in steps if s <= threshold)
    return round(consistent / len(steps), 4)


def _extract(tracklet: dict) -> dict:
    obs = tracklet.get("frames", [])
    return {
        "obs_count":          tracklet.get("observation_count"),
        "spatial_spread_deg": tracklet.get("spatial_spread_deg"),
        "vel_consistency":    _vel_consistency(obs),
        "net_disp_deg":       tracklet.get("net_displacement_deg"),
    }


FEATURES = ["obs_count", "spatial_spread_deg", "vel_consistency", "net_disp_deg"]
WEIGHTS  = [0.35,         0.30,                  0.20,               0.15]


# ── Normalisation ─────────────────────────────────────────────────────────────

def _minmax_ranges(all_records: list[dict]) -> dict:
    ranges = {}
    for feat in FEATURES:
        vals = [r[feat] for r in all_records if r[feat] is not None]
        if not vals:
            ranges[feat] = (0.0, 1.0)
        else:
            lo, hi = min(vals), max(vals)
            ranges[feat] = (lo, hi if hi != lo else lo + 1.0)
    return ranges


def _enrich(rec: dict, ranges: dict) -> dict:
    def _n(v, lo, hi):
        return round(max(0.0, min(1.0, (v - lo) / (hi - lo))), 4) if v is not None else 0.0
    lo_obs, hi_obs = ranges["obs_count"]
    lo_sp,  hi_sp  = ranges["spatial_spread_deg"]
    lo_vc,  hi_vc  = ranges["vel_consistency"]
    lo_nd,  hi_nd  = ranges["net_disp_deg"]
    rec["f1_obs"]    = _n(rec["obs_count"],          lo_obs, hi_obs)
    rec["f2_spread"] = _n(rec["spatial_spread_deg"], lo_sp,  hi_sp)
    rec["f3_vel"]    = _n(rec["vel_consistency"],    lo_vc,  hi_vc)
    rec["f4_disp"]   = _n(rec["net_disp_deg"],       lo_nd,  hi_nd)
    rec["score"]     = round(
        0.35 * rec["f1_obs"] + 0.30 * rec["f2_spread"] +
        0.20 * rec["f3_vel"] + 0.15 * rec["f4_disp"], 4
    )
    return rec


# ── PNG visual review pack ────────────────────────────────────────────────────

def _tile(draw, tx, ty, rec, font_title, font_body, font_small,
          TILE_W, TILE_H, label="UNCLEAR", forced=False):
    score = rec["score"]
    if forced:
        bg = (60, 30, 70)  # purple for mandatory FP controls
    elif score >= 0.55:
        bg = (30, 70, 40)
    elif score >= 0.35:
        bg = (70, 60, 20)
    else:
        bg = (70, 30, 30)
    draw.rectangle([tx, ty, tx + TILE_W, ty + TILE_H], fill=bg, outline=(100, 100, 100))
    tid = rec["tracklet_id"]
    tag = "  ★ MANDATORY FP CONTROL" if forced else f"  [{label}]"
    lines = [
        f"{tid}{tag}",
        f"SCORE {score:.4f}   {'rank #' + str(rec.get('rank', '—')) if not forced else 'fp_rank #' + str(rec.get('fp_rank', '—'))}",
        (f"obs={rec['obs_count']}  spread={rec['spatial_spread_deg']}°  "
         f"vel_cons={rec['vel_consistency']}  net_disp={rec['net_disp_deg']}°"),
        f"F1={rec['f1_obs']:.3f}  F2={rec['f2_spread']:.3f}  F3={rec['f3_vel']:.3f}  F4={rec['f4_disp']:.3f}",
        "[NO AUTOMATIC VERDICT]",
    ]
    fonts = [font_title, font_body, font_small, font_small, font_body]
    colours = [(255, 255, 200), (200, 240, 200) if not forced else (220, 180, 255),
               (200, 200, 200), (180, 200, 180), (220, 180, 80)]
    ly = ty + 8
    for line, fnt, col_c in zip(lines, fonts, colours):
        draw.text((tx + 10, ly), line, fill=col_c, font=fnt)
        ly += 22


def _make_review_pack(top8, bot8, mandatory_fp_recs, output_path: Path) -> None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("WARN: Pillow not available, skipping PNG output")
        return

    TILE_W, TILE_H = 480, 130
    PAD = 12
    SECTION_HEADER_H = 38
    COLS = 4

    def _rows(n): return math.ceil(n / COLS)

    sections = [
        ("TOP 8 HIGHEST-SCORING UNCLEAR ANCHORS", top8, "UNCLEAR"),
        ("BOTTOM 8 LOWEST-SCORING UNCLEAR ANCHORS", bot8, "UNCLEAR"),
        ("MANDATORY KNOWN-FP CONTROLS (T0093, T0080)", mandatory_fp_recs, "CONFIRMED FP"),
    ]

    total_h = PAD
    for _, items, _ in sections:
        r = _rows(len(items))
        total_h += SECTION_HEADER_H + r * (TILE_H + PAD) + PAD

    total_w = COLS * (TILE_W + PAD) + PAD

    img = Image.new("RGB", (total_w, total_h), (30, 30, 30))
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 13)
        font_body  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
    except Exception:
        font_title = font_body = font_small = ImageFont.load_default()

    cy = PAD
    for section_label, items, label in sections:
        draw.rectangle([PAD, cy, total_w - PAD, cy + SECTION_HEADER_H - 4],
                       fill=(50, 70, 100) if "CONTROL" not in section_label else (70, 40, 80))
        draw.text((PAD + 8, cy + 10), section_label,
                  fill=(220, 230, 255) if "CONTROL" not in section_label else (240, 200, 255),
                  font=font_title)
        cy += SECTION_HEADER_H

        forced = "CONTROL" in section_label
        for idx, rec in enumerate(items):
            col = idx % COLS
            row = idx // COLS
            tx = PAD + col * (TILE_W + PAD)
            ty = cy + row * (TILE_H + PAD)
            _tile(draw, tx, ty, rec, font_title, font_body, font_small,
                  TILE_W, TILE_H, label=label, forced=forced)

        rows_used = _rows(len(items))
        cy += rows_used * (TILE_H + PAD) + PAD

    img.save(str(output_path), "PNG")
    print(f"Review pack saved: {output_path}  ({img.size[0]}×{img.size[1]})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Temporal ball-likeness score — out-of-sample validation")
    ap.add_argument("--csv",        required=True, help="tier_a_anchor_adjudication_filled.csv")
    ap.add_argument("--tracklets",  required=True, help="tracklets_tier_a_experimental.json")
    ap.add_argument("--output-dir", default=".",   help="output directory")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Load tracklets
    with open(args.tracklets) as f:
        tdata = json.load(f)
    tracklet_map = {t["id"]: t for t in tdata["tracklets"]}

    # Load CSV verdicts
    verdicts: dict[str, str] = {}
    with open(args.csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = row["tracklet_id"].strip()
            v = _normalise_verdict(row.get("verdict", ""))
            if v is not None:
                verdicts[tid] = v

    balls   = [tid for tid, v in verdicts.items() if v == "ball"]
    fps_ids = [tid for tid, v in verdicts.items() if v == "fp"]
    unclear = [tid for tid, v in verdicts.items() if v == "unclear"]

    print(f"Labels — ball: {len(balls)}  FP: {len(fps_ids)}  unclear: {len(unclear)}")
    if not unclear:
        print("No unclear anchors found. Exiting.")
        sys.exit(0)

    # Verify mandatory FP controls are present
    for ctrl in MANDATORY_FP_CONTROLS:
        if ctrl not in fps_ids:
            print(f"WARN: mandatory FP control {ctrl} not found in FP labels")

    # Extract features
    def _build(tid: str) -> dict | None:
        t = tracklet_map.get(tid)
        if t is None:
            print(f"WARN: {tid} not found in tracklets JSON")
            return None
        feats = _extract(t)
        feats["tracklet_id"] = tid
        return feats

    ball_recs    = [r for tid in balls   for r in [_build(tid)] if r]
    fp_recs      = [r for tid in fps_ids for r in [_build(tid)] if r]
    unclear_recs = [r for tid in unclear for r in [_build(tid)] if r]
    all_recs     = ball_recs + fp_recs + unclear_recs

    # Normalisation ranges over full population
    ranges = _minmax_ranges(all_recs)

    print("\n=== NORMALISATION RANGES (full anchor population) ===")
    print(f"  {'Feature':<22}  min        max")
    for feat in FEATURES:
        lo, hi = ranges[feat]
        print(f"  {feat:<22}  {lo:.4f}     {hi:.4f}")

    print("\n=== FORMULA ===")
    print("  score = 0.35 * norm(obs_count)")
    print("        + 0.30 * norm(spatial_spread_deg)")
    print("        + 0.20 * norm(vel_consistency)")
    print("        + 0.15 * norm(net_disp_deg)")
    print("  norm(x) = (x - min) / (max - min), clamped [0,1]; missing → 0.0")

    # Score all groups
    for r in unclear_recs:
        _enrich(r, ranges)
    for r in fp_recs:
        _enrich(r, ranges)

    unclear_ranked = sorted(unclear_recs, key=lambda r: -r["score"])
    for i, r in enumerate(unclear_ranked, 1):
        r["rank"] = i

    fp_ranked = sorted(fp_recs, key=lambda r: -r["score"])
    for i, r in enumerate(fp_ranked, 1):
        r["fp_rank"] = i

    # Build mandatory FP control list (T0093, T0080 always included)
    fp_by_id = {r["tracklet_id"]: r for r in fp_recs}
    mandatory_fp_recs = [fp_by_id[c] for c in MANDATORY_FP_CONTROLS if c in fp_by_id]
    missing_controls  = [c for c in MANDATORY_FP_CONTROLS if c not in fp_by_id]
    if missing_controls:
        print(f"WARN: mandatory FP controls not found in tracklets: {missing_controls}")

    # Write CSV (unclear anchors only, ranked)
    csv_path = out_dir / "ball_likeness_scores.csv"
    fieldnames = [
        "rank", "tracklet_id", "score",
        "obs_count", "spatial_spread_deg", "vel_consistency", "net_disp_deg",
        "f1_obs", "f2_spread", "f3_vel", "f4_disp",
    ]
    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(unclear_ranked)
    print(f"\nScored CSV: {csv_path}  ({len(unclear_ranked)} unclear anchors)")

    # Build summary
    top8 = unclear_ranked[:8]
    bot8 = unclear_ranked[-8:] if len(unclear_ranked) >= 8 else list(reversed(unclear_ranked))

    fp_rank_lines = []
    for r in fp_ranked:
        outrank_count = sum(1 for u in unclear_ranked if r["score"] > u["score"])
        ctrl_tag = " ★CONTROL" if r["tracklet_id"] in MANDATORY_FP_CONTROLS else ""
        fp_rank_lines.append(
            f"  {r['tracklet_id']:<10}{ctrl_tag:<12}  score={r['score']:.4f}  "
            f"would outrank {outrank_count}/{len(unclear_ranked)} unclear anchors"
        )

    def _row(r):
        return (f"  {r['rank']:<6} {r['tracklet_id']:<10} {r['score']:>7.4f}  "
                f"{str(r['obs_count'])[:5]:>5}  {str(r['spatial_spread_deg'])[:8]:>8}  "
                f"{str(r['vel_consistency'])[:9]:>9}  {str(r['net_disp_deg'])[:9]:>9}")

    col_header = (f"  {'Rank':<6} {'ID':<10} {'Score':>7}  {'obs':>5}  "
                  f"{'spread':>8}  {'vel_cons':>9}  {'net_disp':>9}")

    summary_lines = [
        "TEMPORAL BALL-LIKENESS SCORE — OUT-OF-SAMPLE VALIDATION",
        "=" * 60,
        "",
        "FORMULA (documented)",
        "  score = 0.35 * norm(obs_count)",
        "        + 0.30 * norm(spatial_spread_deg)",
        "        + 0.20 * norm(vel_consistency)",
        "        + 0.15 * norm(net_disp_deg)",
        "  norm(): min-max over full anchor population (ball+FP+unclear)",
        "  Missing feature → 0.0 (conservative)",
        "",
        "FEATURE WEIGHTS AND RATIONALE",
        "  F1 obs_count          0.35  — Δ/σ=2.54 (primary discriminator)",
        "  F2 spatial_spread_deg 0.30  — Δ/σ=2.15 (spatial coverage)",
        "  F3 vel_consistency    0.20  — Δ/σ=1.53 (motion smoothness)",
        "  F4 net_disp_deg       0.15  — Δ/σ=1.69 (net displacement)",
        "  span_frames excluded: collinear with obs_count.",
        "  anchor_strength excluded: Δ/σ=1.55, double-counts confidence.",
        "",
        "NORMALISATION RANGES (full population)",
    ] + [
        f"  {feat:<22}  min={ranges[feat][0]:.4f}  max={ranges[feat][1]:.4f}"
        for feat in FEATURES
    ] + [
        "",
        f"TRAINING LABELS:  ball={', '.join(balls)}",
        f"CONFIRMED FPs:    {', '.join(fps_ids)}",
        f"UNCLEAR (scored out-of-sample): {len(unclear_ranked)}",
        "",
        "TOP 8 HIGHEST-SCORING UNCLEAR ANCHORS",
        col_header,
        "  " + "-" * 60,
    ] + [_row(r) for r in top8] + [
        "",
        "BOTTOM 8 LOWEST-SCORING UNCLEAR ANCHORS",
        col_header,
        "  " + "-" * 60,
    ] + [_row(r) for r in bot8] + [
        "",
        "CONFIRMED FP RANK CHECK (safety check — all confirmed FPs)",
        f"  {'ID':<10}{'tag':<12}  score     outranks",
        "  " + "-" * 55,
    ] + fp_rank_lines + [
        "",
        "MANDATORY KNOWN-FP CONTROLS IN REVIEW PACK: "
        + ", ".join(MANDATORY_FP_CONTROLS),
        "",
        "NO AUTOMATIC VERDICTS ASSIGNED.",
        "Diagnostic-only. No filtering, thresholds, Stage 1/1b/2, or production files changed.",
    ]

    summary_path = out_dir / "ball_likeness_summary.txt"
    summary_path.write_text("\n".join(summary_lines) + "\n")
    print(f"Summary: {summary_path}")
    print()
    for line in summary_lines:
        print(line)

    # PNG review pack
    png_path = out_dir / "ball_likeness_review_pack.png"
    _make_review_pack(top8, bot8, mandatory_fp_recs, png_path)

    print("\nDone. Outputs:")
    for p in [csv_path, summary_path, png_path]:
        if p.exists():
            print(f"  {p}  ({p.stat().st_size / 1e3:.1f} KB)")


if __name__ == "__main__":
    main()
