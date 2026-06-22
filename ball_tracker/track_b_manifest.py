#!/usr/bin/env python3
"""
FFA Track B — Stratified Manifest Generator
============================================
Reads stage1_candidates.json (and optionally hotspot_map.json) and builds
a stratified 75-frame manifest for the detector audit.

Stratification is entirely from observable Stage 1 features — no tracking.json,
no assumption about where the ball actually is.

Strata (deduplicated; a frame may carry multiple labels):
  temporal_early    — first third of clip
  temporal_mid      — middle third
  temporal_late     — final third
  zero_candidates   — Stage 1 produced zero candidates for this frame
  single_candidate  — exactly one candidate
  multi_candidate   — two or more candidates
  high_conf         — top-quartile weighted_conf among frames with candidates
  low_conf          — bottom-quartile weighted_conf among frames with candidates
  cluttered         — three or more deduped candidates
  hotspot_adjacent  — at least one candidate inside a hotspot region (penalty < 0.5)
  hotspot_neutral   — all candidates in neutral regions (penalty >= 0.9)

Output: track_b_manifest.json
  {
    "total_frames": N,
    "fps": F,
    "sample_count": 75,
    "frames": [
      {
        "frame_idx": int,
        "timestamp_s": float,
        "strata": [str, ...],
        "stage1_candidate_count": int,
        "top_weighted_conf": float | null,
        "top_raw_conf": float | null,
        "top_yaw": float | null,
        "top_pitch": float | null,
        "min_penalty": float | null,
        "hotspot_adjacent": bool
      },
      ...
    ]
  }
"""

import argparse
import json
import math
import random
import sys

TARGET_SAMPLE    = 75
HOTSPOT_PENALTY_THRESH = 0.5   # candidate penalty below this = hotspot-adjacent
NEUTRAL_PENALTY_THRESH = 0.9   # candidate penalty above this = neutral


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def stratum_budget():
    """Returns ordered list of (stratum_name, target_count).
    Total > TARGET_SAMPLE intentionally — dedup across strata trims to target.
    """
    return [
        ("temporal_early",   10),
        ("temporal_mid",     10),
        ("temporal_late",    10),
        ("zero_candidates",  10),
        ("single_candidate",  8),
        ("multi_candidate",   8),
        ("high_conf",         7),
        ("low_conf",          7),
        ("cluttered",         6),
        ("hotspot_adjacent",  6),
        ("hotspot_neutral",   5),
    ]


def label_frame(frame_idx, cands, total_frames, high_thresh, low_thresh):
    """Return set of stratum labels for one frame."""
    labels = set()

    # Temporal
    third = total_frames // 3
    if frame_idx < third:
        labels.add("temporal_early")
    elif frame_idx < 2 * third:
        labels.add("temporal_mid")
    else:
        labels.add("temporal_late")

    # Candidate count
    n = len(cands)
    if n == 0:
        labels.add("zero_candidates")
    elif n == 1:
        labels.add("single_candidate")
    else:
        labels.add("multi_candidate")

    if n >= 3:
        labels.add("cluttered")

    # Confidence strata (only for frames with candidates)
    if n > 0:
        top_wc = max(c["weighted_conf"] for c in cands)
        if top_wc >= high_thresh:
            labels.add("high_conf")
        if top_wc <= low_thresh:
            labels.add("low_conf")

        # Hotspot proximity
        min_pen = min(c["penalty"] for c in cands)
        if min_pen < HOTSPOT_PENALTY_THRESH:
            labels.add("hotspot_adjacent")
        if all(c["penalty"] >= NEUTRAL_PENALTY_THRESH for c in cands):
            labels.add("hotspot_neutral")

    return labels


def build_manifest(stage1_path, hotspot_map_path, output_path, seed=42):
    random.seed(seed)

    with open(stage1_path) as f:
        s1 = json.load(f)

    total_frames = s1["total_frames"]
    fps          = s1["fps"]
    # Keys are strings in JSON
    frames_raw   = {int(k): v for k, v in s1["frames"].items()}

    print(f"[manifest] Stage 1: {total_frames} total frames, "
          f"{len(frames_raw)} with candidate data, fps={fps:.2f}")

    # Compute per-frame metadata
    frame_meta = {}
    all_top_wc = []
    for fi in range(total_frames):
        cands = frames_raw.get(fi, [])
        top_wc   = max((c["weighted_conf"] for c in cands), default=None)
        top_rc   = max((c["raw_conf"]      for c in cands), default=None)
        top_cand = max(cands, key=lambda c: c["weighted_conf"]) if cands else None
        min_pen  = min((c["penalty"] for c in cands), default=None)
        frame_meta[fi] = {
            "cands":     cands,
            "n":         len(cands),
            "top_wc":    top_wc,
            "top_rc":    top_rc,
            "top_yaw":   round(top_cand["yaw"],   3) if top_cand else None,
            "top_pitch": round(top_cand["pitch"], 3) if top_cand else None,
            "min_pen":   min_pen,
        }
        if top_wc is not None:
            all_top_wc.append(top_wc)

    # Confidence thresholds (quartiles across frames that have candidates)
    all_top_wc.sort()
    n_wc = len(all_top_wc)
    high_thresh = all_top_wc[int(0.75 * n_wc)] if n_wc else 1.0
    low_thresh  = all_top_wc[int(0.25 * n_wc)] if n_wc else 0.0
    print(f"[manifest] Conf quartiles: low<={low_thresh:.3f}  high>={high_thresh:.3f}")

    # Label every frame
    labeled = {}
    for fi, meta in frame_meta.items():
        labels = label_frame(fi, meta["cands"], total_frames, high_thresh, low_thresh)
        labeled[fi] = labels

    # Build per-stratum candidate pools
    stratum_pools = {}
    for fi, labels in labeled.items():
        for lbl in labels:
            stratum_pools.setdefault(lbl, []).append(fi)

    for lbl, pool in stratum_pools.items():
        print(f"[manifest]   stratum '{lbl}': {len(pool)} eligible frames")

    # Greedy stratified sampling — fill each stratum budget in order
    selected = set()
    budget   = stratum_budget()

    for stratum, target in budget:
        pool = stratum_pools.get(stratum, [])
        # Prefer frames not yet selected (diversity), then fill from already-selected if needed
        novel = [fi for fi in pool if fi not in selected]
        random.shuffle(novel)
        picks = novel[:target]
        if len(picks) < target:
            # Top up: already-selected that belong to this stratum
            already = [fi for fi in pool if fi in selected]
            random.shuffle(already)
            picks += already[:target - len(picks)]
        selected.update(picks)
        print(f"[manifest]   '{stratum}': picked {len(picks)} "
              f"(total selected so far: {len(selected)})")

    # Trim or top-up to exactly TARGET_SAMPLE
    selected_list = sorted(selected)
    if len(selected_list) > TARGET_SAMPLE:
        # Trim: remove frames that have the most strata labels (least unique value)
        # i.e. keep frames with rare label combinations
        def rarity(fi):
            lbls = labeled[fi]
            return sum(len(stratum_pools.get(l, [])) for l in lbls)
        selected_list.sort(key=rarity)
        selected_list = sorted(selected_list[:TARGET_SAMPLE])
    elif len(selected_list) < TARGET_SAMPLE:
        # Top-up with random novel frames
        remaining = [fi for fi in range(total_frames) if fi not in selected]
        random.shuffle(remaining)
        selected_list = sorted(selected_list + remaining[:TARGET_SAMPLE - len(selected_list)])

    print(f"[manifest] Final sample: {len(selected_list)} frames "
          f"({selected_list[0]}…{selected_list[-1]})")

    # Build output records
    out_frames = []
    for fi in selected_list:
        meta   = frame_meta[fi]
        labels = sorted(labeled[fi])
        is_hotspot_adj = meta["min_pen"] is not None and meta["min_pen"] < HOTSPOT_PENALTY_THRESH
        out_frames.append({
            "frame_idx":              fi,
            "timestamp_s":            round(fi / fps, 3) if fps else None,
            "strata":                 labels,
            "stage1_candidate_count": meta["n"],
            "top_weighted_conf":      round(meta["top_wc"], 4) if meta["top_wc"] is not None else None,
            "top_raw_conf":           round(meta["top_rc"], 4) if meta["top_rc"] is not None else None,
            "top_yaw":                meta["top_yaw"],
            "top_pitch":              meta["top_pitch"],
            "min_penalty":            round(meta["min_pen"], 4) if meta["min_pen"] is not None else None,
            "hotspot_adjacent":       is_hotspot_adj,
        })

    # Stratum coverage summary
    stratum_coverage = {}
    for rec in out_frames:
        for lbl in rec["strata"]:
            stratum_coverage[lbl] = stratum_coverage.get(lbl, 0) + 1

    manifest = {
        "total_frames":   total_frames,
        "fps":            fps,
        "sample_count":   len(out_frames),
        "high_conf_thresh": round(high_thresh, 4),
        "low_conf_thresh":  round(low_thresh, 4),
        "stratum_coverage": stratum_coverage,
        "frames":         out_frames,
    }

    with open(output_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[manifest] Written -> {output_path}")
    print(f"[manifest] Stratum coverage: {stratum_coverage}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage1-candidates", required=True)
    ap.add_argument("--output",            default="track_b_manifest.json")
    ap.add_argument("--seed",              type=int, default=42)
    args = ap.parse_args()
    build_manifest(args.stage1_candidates, None, args.output, args.seed)
