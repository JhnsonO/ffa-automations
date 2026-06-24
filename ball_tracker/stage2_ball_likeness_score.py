#!/usr/bin/env python3
"""
stage2_ball_likeness_score.py — Temporal ball-likeness score: out-of-sample validation.

Diagnostic-only. No thresholds changed. No verdicts assigned.
No modifications to Stage 1, Stage 1b, Stage 2, Tier A behaviour, or renderer.

Inputs:
  --csv        tier_a_anchor_adjudication.csv  (with verdicts filled for ball/FP)
  --tracklets  tracklets_tier_a_experimental.json

Outputs (to --output-dir):
  ball_likeness_scores.csv          ranked CSV of all unclear anchors
  ball_likeness_review_pack.png     visual review pack: top-8 + bottom-8 unclear anchors
  ball_likeness_summary.txt         summary including FP rank check

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
import os
import sys
from pathlib import Path


# ── Verdict normalisation (matches stage2_label_analysis.py) ─────────────────

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
        "obs_count":         tracklet.get("observation_count"),
        "spatial_spread_deg":tracklet.get("spatial_spread_deg"),
        "vel_consistency":   _vel_consistency(obs),
        "net_disp_deg":      tracklet.get("net_displacement_deg"),
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


def _score(rec: dict, ranges: dict) -> float:
    total = 0.0
    for feat, w in zip(FEATURES, WEIGHTS):
        v = rec.get(feat)
        lo, hi = ranges[feat]
        if v is None:
            norm = 0.0
        else:
            norm = max(0.0, min(1.0, (v - lo) / (hi - lo)))
        total += w * norm
    return round(total, 4)


# ── PNG visual review pack ────────────────────────────────────────────────────

def _make_review_pack(top8: list[dict], bot8: list[dict],
                      fp_records: list[dict], ranges: dict,
                      output_path: Path) -> None:
    """
    Compact visual review pack: two panels (top-8 highest, bottom-8 lowest unclear).
    Text-only tiles; no video frames needed.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        print("WARN: Pillow not available, skipping PNG output")
        return

    # Layout constants
    TILE_W, TILE_H = 440, 130
    PAD = 12
    SECTION_HEADER_H = 36
    COLS = 4

    def _rows(n): return math.ceil(n / COLS)

    sections = [
        ("TOP 8 HIGHEST-SCORING UNCLEAR ANCHORS", top8),
        ("BOTTOM 8 LOWEST-SCORING UNCLEAR ANCHORS", bot8),
    ]

    total_h = PAD
    for _, items in sections:
        r = _rows(len(items))
        total_h += SECTION_HEADER_H + r * (TILE_H + PAD) + PAD

    total_w = COLS * (TILE_W + PAD) + PAD

    img = Image.new("RGB", (total_w, total_h), (30, 30, 30))
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 14)
        font_body  = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 12)
        font_small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 10)
    except Exception:
        font_title = font_body = font_small = ImageFont.load_default()

    # FP score range for summary line
    fp_scores = [r["score"] for r in fp_records]
    fp_max = max(fp_scores) if fp_scores else 0.0
    fp_min = min(fp_scores) if fp_scores else 0.0

    cy = PAD
    for section_label, items in sections:
        # Section header
        draw.rectangle([PAD, cy, total_w - PAD, cy + SECTION_HEADER_H - 4],
                       fill=(50, 70, 100))
        draw.text((PAD + 8, cy + 8), section_label, fill=(220, 230, 255), font=font_title)
        cy += SECTION_HEADER_H

        for idx, rec in enumerate(items):
            col = idx % COLS
            row = idx // COLS
            tx = PAD + col * (TILE_W + PAD)
            ty = cy + row * (TILE_H + PAD)

            score = rec["score"]
            # Colour tile by score: green→amber→red
            if score >= 0.55:
                bg = (30, 70, 40)
            elif score >= 0.35:
                bg = (70, 60, 20)
            else:
                bg = (70, 30, 30)

            draw.rectangle([tx, ty, tx + TILE_W, ty + TILE_H], fill=bg, outline=(100, 100, 100))

            lines = [
                f"{rec['tracklet_id']}   SCORE {score:.4f}   rank #{rec['rank']}",
                f"obs_count={rec['obs_count']}  spread={rec['spatial_spread_deg']}°  "
                f"vel_cons={rec['vel_consistency']}  net_disp={rec['net_disp_deg']}°",
                f"F1={rec['f1_obs']:.3f}  F2={rec['f2_spread']:.3f}  "
                f"F3={rec['f3_vel']:.3f}  F4={rec['f4_disp']:.3f}",
                f"[NO AUTOMATIC VERDICT]",
            ]
            fonts = [font_title, font_small, font_small, font_body]
            colours = [(255, 255, 200), (200, 200, 200), (180, 200, 180), (220, 180, 80)]
            ly = ty + 10
            for line, fnt, col_c in zip(lines, fonts, colours):
                draw.text((tx + 10, ly), line, fill=col_c, font=fnt)
                ly += 24

        rows_used = _rows(len(items))
        cy += rows_used * (TILE_H + PAD) + PAD

    # FP summary footer
    footer_h = 54
    fy = total_h - footer_h - PAD
    draw.rectangle([PAD, fy, total_w - PAD, fy + footer_h],
                   fill=(50, 30, 50), outline=(120, 80, 120))
    draw.text((PAD + 10, fy + 8),
              f"CONFIRMED FALSE-POSITIVE SCORE RANGE (n={len(fp_records)}):  "
              f"min={fp_min:.4f}  max={fp_max:.4f}",
              fill=(220, 180, 220), font=font_title)
    draw.text((PAD + 10, fy + 28),
              "See ball_likeness_summary.txt for FP rank detail. No automatic verdicts.",
              fill=(180, 160, 180), font=font_body)

    img.save(str(output_path), "PNG")
    print(f"Review pack saved: {output_path}  ({img.size[0]}×{img.size[1]})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Temporal ball-likeness score — out-of-sample validation")
    ap.add_argument("--csv",       required=True, help="tier_a_anchor_adjudication.csv (verdicts filled)")
    ap.add_argument("--tracklets", required=True, help="tracklets_tier_a_experimental.json")
    ap.add_argument("--output-dir", default=".", help="output directory")
    args = ap.parse_args()

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── Load tracklets ────────────────────────────────────────────────────────
    with open(args.tracklets) as f:
        tdata = json.load(f)
    tracklet_map = {t["id"]: t for t in tdata["tracklets"]}

    # ── Load CSV verdicts ─────────────────────────────────────────────────────
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

    # ── Extract features for all anchors ─────────────────────────────────────
    def _build(tid: str) -> dict | None:
        t = tracklet_map.get(tid)
        if t is None:
            print(f"WARN: {tid} not found in tracklets JSON")
            return None
        feats = _extract(t)
        feats["tracklet_id"] = tid
        return feats

    ball_recs   = [r for tid in balls   for r in [_build(tid)] if r]
    fp_recs     = [r for tid in fps_ids for r in [_build(tid)] if r]
    unclear_recs= [r for tid in unclear for r in [_build(tid)] if r]

    all_recs = ball_recs + fp_recs + unclear_recs

    # ── Min-max normalisation (population: all anchors) ───────────────────────
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

    # ── Score every unclear anchor ────────────────────────────────────────────
    def _enrich(rec: dict, ranges: dict) -> dict:
        lo_obs, hi_obs = ranges["obs_count"]
        lo_sp,  hi_sp  = ranges["spatial_spread_deg"]
        lo_vc,  hi_vc  = ranges["vel_consistency"]
        lo_nd,  hi_nd  = ranges["net_disp_deg"]
        def _n(v, lo, hi): return round(max(0.0, min(1.0, (v - lo) / (hi - lo))), 4) if v is not None else 0.0
        rec["f1_obs"]    = _n(rec["obs_count"],          lo_obs, hi_obs)
        rec["f2_spread"] = _n(rec["spatial_spread_deg"], lo_sp,  hi_sp)
        rec["f3_vel"]    = _n(rec["vel_consistency"],    lo_vc,  hi_vc)
        rec["f4_disp"]   = _n(rec["net_disp_deg"],       lo_nd,  hi_nd)
        rec["score"]     = _score(rec, ranges)
        return rec

    for r in unclear_recs:
        _enrich(r, ranges)

    unclear_ranked = sorted(unclear_recs, key=lambda r: -r["score"])
    for i, r in enumerate(unclear_ranked, 1):
        r["rank"] = i

    # ── Score confirmed FPs ───────────────────────────────────────────────────
    for r in fp_recs:
        _enrich(r, ranges)
    fp_ranked = sorted(fp_recs, key=lambda r: -r["score"])
    for i, r in enumerate(fp_ranked, 1):
        r["fp_rank"] = i

    # ── Write CSV ─────────────────────────────────────────────────────────────
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

    # ── Summary ───────────────────────────────────────────────────────────────
    top8 = unclear_ranked[:8]
    bot8 = unclear_ranked[-8:] if len(unclear_ranked) >= 8 else unclear_ranked[::-1]

    # Compute FP rank position: how many unclear anchors would a given FP outrank?
    fp_rank_summary_lines = []
    for r in fp_ranked:
        outrank_count = sum(1 for u in unclear_ranked if r["score"] > u["score"])
        fp_rank_summary_lines.append(
            f"  {r['tracklet_id']:<10}  score={r['score']:.4f}  "
            f"would outrank {outrank_count}/{len(unclear_ranked)} unclear anchors"
        )

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
        "NORMALISATION RANGES (full population)",
    ] + [
        f"  {feat:<22}  min={ranges[feat][0]:.4f}  max={ranges[feat][1]:.4f}"
        for feat in FEATURES
    ] + [
        "",
        f"CONFIRMED BALL ANCHORS: {', '.join(balls)}",
        f"CONFIRMED FP ANCHORS:   {', '.join(fps_ids)}",
        f"UNCLEAR ANCHORS SCORED: {len(unclear_ranked)}",
        "",
        "TOP 8 HIGHEST-SCORING UNCLEAR ANCHORS",
        f"  {'Rank':<6} {'ID':<10} {'Score':>7}  {'obs':>5}  {'spread':>8}  {'vel_cons':>9}  {'net_disp':>9}",
        "  " + "-" * 60,
    ] + [
        f"  {r['rank']:<6} {r['tracklet_id']:<10} {r['score']:>7.4f}  "
        f"{str(r['obs_count'])[:5]:>5}  {str(r['spatial_spread_deg'])[:8]:>8}  "
        f"{str(r['vel_consistency'])[:9]:>9}  {str(r['net_disp_deg'])[:9]:>9}"
        for r in top8
    ] + [
        "",
        "BOTTOM 8 LOWEST-SCORING UNCLEAR ANCHORS",
        f"  {'Rank':<6} {'ID':<10} {'Score':>7}  {'obs':>5}  {'spread':>8}  {'vel_cons':>9}  {'net_disp':>9}",
        "  " + "-" * 60,
    ] + [
        f"  {r['rank']:<6} {r['tracklet_id']:<10} {r['score']:>7.4f}  "
        f"{str(r['obs_count'])[:5]:>5}  {str(r['spatial_spread_deg'])[:8]:>8}  "
        f"{str(r['vel_consistency'])[:9]:>9}  {str(r['net_disp_deg'])[:9]:>9}"
        for r in bot8
    ] + [
        "",
        "CONFIRMED FP RANK CHECK",
        "  (Would any known FP score highly under this formula?)",
    ] + fp_rank_summary_lines + [
        "",
        "NO AUTOMATIC VERDICTS ASSIGNED.",
        "This is a diagnostic-only output for human review.",
        "No filtering, thresholds, Stage 1/1b/2, or production files changed.",
    ]

    summary_path = out_dir / "ball_likeness_summary.txt"
    summary_path.write_text("\n".join(summary_lines) + "\n")
    print(f"Summary: {summary_path}")

    # Print summary to stdout too
    print()
    for line in summary_lines:
        print(line)

    # ── PNG review pack ───────────────────────────────────────────────────────
    png_path = out_dir / "ball_likeness_review_pack.png"
    _make_review_pack(top8, bot8, fp_recs, ranges, png_path)

    print("\nDone. Outputs:")
    for p in [csv_path, summary_path, png_path]:
        if p.exists():
            print(f"  {p}  ({p.stat().st_size / 1e3:.1f} KB)")


if __name__ == "__main__":
    main()
