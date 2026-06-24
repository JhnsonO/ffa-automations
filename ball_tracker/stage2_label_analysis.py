#!/usr/bin/env python3
"""
stage2_label_analysis.py — Annotation analysis: ball vs false-positive feature differences.

Reads the human-adjudicated Tier A anchor CSV (verdict column filled) and the
corresponding tracklets manifest JSON. Reports feature differences between
likely_ball and likely_false_positive anchors, ranks discriminating features,
and flags unclear anchors most worth reviewing next.

NO filtering, threshold changes, model training, or modifications to any
frozen files (Stage 1, Stage 1b, Stage 2, renderer).

Usage:
    python3 stage2_label_analysis.py \\
        --csv  tier_a_anchor_adjudication.csv \\
        --tracklets tracklets_tier_a_experimental.json \\
        [--manifest tier_a_anchor_adjudication_manifest.json]

Verdicts accepted (case-insensitive, partial match):
    likely_ball / ball
    likely_false_positive / false_positive / false / fp
    unclear / ?
"""

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path


# ─── Verdict normalisation ────────────────────────────────────────────────────

def _normalise(raw: str) -> str | None:
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


# ─── Observation-level feature extraction ────────────────────────────────────

def _obs_geometry_stats(obs_list: list) -> dict:
    """
    Compute per-tracklet statistics from the frames[] observation list.
    Covers: bbox geometry, confidence, step size (motion smoothness proxy).
    """
    widths, heights, areas, ars, confs = [], [], [], [], []
    steps = []
    prev_yaw, prev_pitch = None, None

    for obs in obs_list:
        geo = obs.get("detection_geometry") or {}
        w = geo.get("bbox_width_px")
        h = geo.get("bbox_height_px")
        a = geo.get("bbox_area_px")
        ar = geo.get("bbox_aspect_ratio")
        if w is not None: widths.append(w)
        if h is not None: heights.append(h)
        if a is not None: areas.append(a)
        if ar is not None: ars.append(ar)

        c = obs.get("weighted_conf")
        if c is not None:
            confs.append(c)

        yaw = obs.get("yaw")
        pitch = obs.get("pitch")
        if yaw is not None and pitch is not None:
            if prev_yaw is not None:
                dy = yaw - prev_yaw
                dp = pitch - prev_pitch
                steps.append(math.sqrt(dy * dy + dp * dp))
            prev_yaw, prev_pitch = yaw, pitch

    def _stats(vals):
        if not vals:
            return None, None, None
        n = len(vals)
        mean = sum(vals) / n
        if n > 1:
            variance = sum((v - mean) ** 2 for v in vals) / n
            std = math.sqrt(variance)
        else:
            std = 0.0
        return round(mean, 4), round(std, 4), round(max(vals), 4)

    return {
        "bbox_width_mean":  _stats(widths)[0],
        "bbox_width_max":   _stats(widths)[2],
        "bbox_height_mean": _stats(heights)[0],
        "bbox_area_mean":   _stats(areas)[0],
        "bbox_ar_mean":     _stats(ars)[0],
        "bbox_ar_max":      _stats(ars)[2],
        "obs_conf_mean":    _stats(confs)[0],
        "obs_conf_std":     _stats(confs)[1],
        "step_mean_deg":    _stats(steps)[0],
        "step_max_deg":     _stats(steps)[2],
        "step_std_deg":     _stats(steps)[1],
        "geo_coverage":     round(len(widths) / max(len(obs_list), 1), 3),
    }


# ─── Tracklet-level feature set ───────────────────────────────────────────────

SCALAR_FEATURES = [
    # (key_in_tracklet, display_name, higher_is_ball_like?)
    ("anchor_strength_candidate",   "anchor_strength",        True),
    ("observation_count",           "obs_count",              True),
    ("net_displacement_deg",        "net_disp_deg",           True),
    ("mean_weighted_conf",          "mean_conf",              True),
    ("spatial_spread_deg",          "spatial_spread_deg",     True),
    ("mean_velocity_deg_per_frame", "mean_vel_deg_fr",        True),
    ("velocity_consistency",        "vel_consistency",        True),
    ("mean_prediction_residual",    "mean_pred_residual",     None),  # ambiguous
    ("coverage_ratio",              "coverage_ratio",         True),
    ("max_internal_gap",            "max_internal_gap",       False),
    ("confirmed_static_hotspot_frac", "static_hotspot_frac", False),
    ("span_frames",                 "span_frames",            True),
]

OBS_FEATURES = [
    ("bbox_width_mean",  "bbox_w_mean",   None),
    ("bbox_height_mean", "bbox_h_mean",   None),
    ("bbox_area_mean",   "bbox_area_mean",None),
    ("bbox_ar_mean",     "bbox_ar_mean",  None),
    ("bbox_ar_max",      "bbox_ar_max",   None),
    ("obs_conf_std",     "conf_std",      False),  # lower std = more stable = ball-like
    ("step_mean_deg",    "step_mean_deg", True),
    ("step_std_deg",     "step_std_deg",  False),  # lower = smoother
    ("geo_coverage",     "geo_coverage",  True),
]


def _extract_features(t: dict, obs_stats: dict) -> dict:
    feats = {}
    for key, dname, _ in SCALAR_FEATURES:
        feats[dname] = t.get(key)
    for key, dname, _ in OBS_FEATURES:
        feats[dname] = obs_stats.get(key)
    # Annotation flags (may or may not be present depending on which
    # audit layer was applied)
    feats["would_suppress_repeated_static"] = int(
        bool(t.get("would_suppress_repeated_static"))
    )
    feats["would_reject_static_motion"] = int(
        bool(t.get("would_reject_static_motion"))
    )
    return feats


# ─── Group statistics ─────────────────────────────────────────────────────────

def _group_stats(records: list[dict], feat_names: list[str]) -> dict:
    """Return {feat: {mean, median, n_valid}} for a group."""
    result = {}
    for fn in feat_names:
        vals = [r[fn] for r in records if r.get(fn) is not None]
        if not vals:
            result[fn] = {"mean": None, "median": None, "n": 0}
            continue
        vals_s = sorted(vals)
        n = len(vals_s)
        mid = n // 2
        median = vals_s[mid] if n % 2 else (vals_s[mid - 1] + vals_s[mid]) / 2
        result[fn] = {
            "mean":   round(sum(vals) / n, 4),
            "median": round(median, 4),
            "n":      n,
        }
    return result


def _effect_size(ball_vals: list[float], fp_vals: list[float]) -> float | None:
    """Absolute normalised mean difference as a simple discriminability proxy."""
    bv = [v for v in ball_vals if v is not None]
    fv = [v for v in fp_vals  if v is not None]
    if not bv or not fv:
        return None
    bm = sum(bv) / len(bv)
    fm = sum(fv) / len(fv)
    all_vals = bv + fv
    n = len(all_vals)
    gm = sum(all_vals) / n
    std = math.sqrt(sum((v - gm) ** 2 for v in all_vals) / n) or 1e-9
    return abs(bm - fm) / std


# ─── Output helpers ───────────────────────────────────────────────────────────

def _hdr(title: str) -> None:
    print()
    print("=" * 72)
    print(f"  {title}")
    print("=" * 72)


def _row(label: str, val) -> None:
    print(f"  {label:<36} {val}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Tier A anchor label analysis")
    ap.add_argument("--csv",       required=True, help="tier_a_anchor_adjudication.csv (with verdict filled)")
    ap.add_argument("--tracklets", required=True, help="tracklets_tier_a_experimental.json")
    ap.add_argument("--manifest",  default=None,  help="tier_a_anchor_adjudication_manifest.json (optional)")
    args = ap.parse_args()

    # ── Load tracklets ────────────────────────────────────────────────────────
    with open(args.tracklets) as f:
        tdata = json.load(f)
    tracklet_map = {t["id"]: t for t in tdata["tracklets"]}

    # ── Load CSV verdicts ─────────────────────────────────────────────────────
    verdicts: dict[str, str] = {}
    csv_rows: dict[str, dict] = {}
    raw_skipped = []
    with open(args.csv, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tid = row["tracklet_id"].strip()
            v = _normalise(row.get("verdict", ""))
            csv_rows[tid] = row
            if v is None:
                raw_skipped.append((tid, row.get("verdict", "")))
            else:
                verdicts[tid] = v

    # ── Label summary ─────────────────────────────────────────────────────────
    balls   = [tid for tid, v in verdicts.items() if v == "ball"]
    fps_    = [tid for tid, v in verdicts.items() if v == "fp"]
    unclear = [tid for tid, v in verdicts.items() if v == "unclear"]
    total_labelled = len(verdicts)
    total_in_csv   = len(csv_rows)

    _hdr("LABEL SUMMARY")
    _row("Total anchors in CSV",    total_in_csv)
    _row("Labelled",                total_labelled)
    _row("  likely_ball",           len(balls))
    _row("  likely_false_positive", len(fps_))
    _row("  unclear",               len(unclear))
    if raw_skipped:
        _row("  blank / unrecognised", len(raw_skipped))
        print(f"    (skipped IDs: {', '.join(t for t, _ in raw_skipped[:10])}{'…' if len(raw_skipped)>10 else ''})")

    if len(balls) == 0 or len(fps_) == 0:
        print()
        print("  ⚠  Need at least one ball and one FP label to compare features.")
        print("     Fill the verdict column and re-run.")
        sys.exit(0)

    # ── Build feature records ─────────────────────────────────────────────────
    def build_record(tid: str) -> dict | None:
        t = tracklet_map.get(tid)
        if t is None:
            print(f"  WARN: {tid} not found in tracklets JSON")
            return None
        obs_stats = _obs_geometry_stats(t.get("frames", []))
        feats = _extract_features(t, obs_stats)
        feats["_id"] = tid
        feats["_verdict"] = verdicts[tid]
        return feats

    ball_records   = [r for tid in balls   for r in [build_record(tid)] if r]
    fp_records     = [r for tid in fps_    for r in [build_record(tid)] if r]
    unclear_records= [r for tid in unclear for r in [build_record(tid)] if r]

    all_feat_names = [d for _, d, _ in SCALAR_FEATURES] + [d for _, d, _ in OBS_FEATURES] + \
                     ["would_suppress_repeated_static", "would_reject_static_motion"]

    ball_stats = _group_stats(ball_records, all_feat_names)
    fp_stats   = _group_stats(fp_records,   all_feat_names)

    # ── Feature comparison table ──────────────────────────────────────────────
    _hdr(f"FEATURE COMPARISON  (ball n={len(ball_records)}  |  FP n={len(fp_records)})")
    header = f"  {'Feature':<28} {'ball_mean':>10} {'ball_med':>9} {'fp_mean':>9} {'fp_med':>9}  {'Δ/σ':>6}"
    print(header)
    print("  " + "-" * 74)

    effect_rows = []
    for fname in all_feat_names:
        bs = ball_stats[fname]
        fs = fp_stats[fname]

        bm = f"{bs['mean']:.4f}" if bs["mean"] is not None else "—"
        bmd= f"{bs['median']:.4f}" if bs["median"] is not None else "—"
        fm = f"{fs['mean']:.4f}" if fs["mean"] is not None else "—"
        fmd= f"{fs['median']:.4f}" if fs["median"] is not None else "—"

        bvals = [r[fname] for r in ball_records if r.get(fname) is not None]
        fvals = [r[fname] for r in fp_records   if r.get(fname) is not None]
        es = _effect_size(bvals, fvals)
        es_s = f"{es:.3f}" if es is not None else "—"

        print(f"  {fname:<28} {bm:>10} {bmd:>9} {fm:>9} {fmd:>9}  {es_s:>6}")

        if es is not None:
            effect_rows.append((fname, es, bvals, fvals))

    # ── Ranked feature discrimination ─────────────────────────────────────────
    effect_rows.sort(key=lambda x: -x[1])

    _hdr("RANKED FEATURES — most discriminating first  (|Δ|/σ)")
    print(f"  {'Rank':<5} {'Feature':<28} {'|Δ|/σ':>7}   {'ball_mean':>10} {'fp_mean':>10}  Interpretation")
    print("  " + "-" * 90)

    all_dir_map = {d: hi for _, d, hi in SCALAR_FEATURES + OBS_FEATURES}

    for rank, (fname, es, bvals, fvals) in enumerate(effect_rows, 1):
        bm = sum(bvals) / len(bvals) if bvals else None
        fm = sum(fvals) / len(fvals) if fvals else None
        bm_s = f"{bm:.4f}" if bm is not None else "—"
        fm_s = f"{fm:.4f}" if fm is not None else "—"

        hi_ball = all_dir_map.get(fname)
        if hi_ball is True:
            interp = "ball > FP ✓" if bm and fm and bm > fm else "ball < FP ✗"
        elif hi_ball is False:
            interp = "ball < FP ✓" if bm and fm and bm < fm else "ball > FP ✗"
        else:
            interp = "—"

        print(f"  {rank:<5} {fname:<28} {es:>7.3f}   {bm_s:>10} {fm_s:>10}  {interp}")

    # ── Unclear anchors — priority review list ────────────────────────────────
    _hdr("UNCLEAR ANCHORS — ranked by review priority")

    if not unclear_records:
        print("  None marked unclear.")
    else:
        # Priority heuristic: anchors with features close to both class means
        # → lowest max class distance scores highest priority for review.
        # Use top-3 discriminating features only.
        top3 = [fname for fname, *_ in effect_rows[:3]]

        def _class_distance(rec):
            dists = []
            for fn in top3:
                v = rec.get(fn)
                bm = ball_stats[fn]["mean"]
                fm = fp_stats[fn]["mean"]
                if v is None or bm is None or fm is None:
                    continue
                db = abs(v - bm)
                df = abs(v - fm)
                dists.append(min(db, df) / (abs(bm - fm) + 1e-9))
            return sum(dists) / len(dists) if dists else 99.0

        scored = [(rec["_id"], _class_distance(rec), rec) for rec in unclear_records]
        scored.sort(key=lambda x: x[1])  # lowest distance = most ambiguous = review first

        print(f"  {'Rank':<5} {'ID':<12} {'ambiguity':>10}   Key feature values")
        print("  " + "-" * 70)
        for rank, (tid, score, rec) in enumerate(scored, 1):
            kv = "  ".join(
                f"{fn}={rec[fn]:.3f}" if rec.get(fn) is not None else f"{fn}=—"
                for fn in top3
            )
            print(f"  {rank:<5} {tid:<12} {score:>10.4f}   {kv}")

        print()
        print("  Note: ambiguity score = avg normalised distance to nearest class mean")
        print("        on the top-3 discriminating features (lower = harder to classify).")

    # ── Quick design hints ────────────────────────────────────────────────────
    _hdr("FEATURE-DESIGN HINTS  (read-only; no thresholds changed)")
    if effect_rows:
        best = effect_rows[0]
        print(f"  Strongest single discriminator: {best[0]}  (|Δ|/σ={best[1]:.3f})")
        top5 = [fn for fn, *_ in effect_rows[:5]]
        print(f"  Top-5 candidate features for a ball-likeness score:")
        for fn in top5:
            print(f"    • {fn}")
    print()
    print("  These features are candidates only.  No score, filter, or threshold is")
    print("  approved here.  Bring findings to the feature-design decision gate.")
    print()


if __name__ == "__main__":
    main()
