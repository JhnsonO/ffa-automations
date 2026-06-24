#!/usr/bin/env python3
"""
FFA Stage 1 — Tier A discovered-static DRY-RUN filter.

PURPOSE
-------
Measure whether removing ONLY the reviewed, tight, fixed-scene false-positive
locations materially improves Stage 2 input quality.

This is a DRY-RUN EXPERIMENT. It produces a derived candidate file and an audit
of everything removed. It does NOT modify the original Stage 1 candidates, does
NOT tune any threshold, and approves NO active suppression.

LOCATION IDENTITY — FROZEN MANIFEST
------------------------------------
All Tier A locations are identified by stable physical coordinates, NOT by
cluster IDs assigned by the repeated-static audit. Cluster IDs (C001, C002 …)
are regenerated on each Stage 2 run and may renumber. The TIER_A_MANIFEST below
is the sole source of truth for this dry-run. It was derived from the reviewed
repeated-static audit (artifact 7841215970, run 28078249103) and the wide-cluster
diagnosis review session.

Each entry is immutable for this experiment:
  location_id      — stable physical label, not a cluster label
  centre_yaw_deg   — reviewed
  centre_pitch_deg — reviewed
  action_radius_deg — max(member→centre dist) + 0.5° guard, capped 0.75°
  member_ids       — tracklet IDs in the reviewed audit (informational only;
                     not looked up at runtime because re-linking may renumber them)
  review_tier      — "tier_a_suppression_candidate"
  review_status    — "reviewed_approved_for_dry_run"

This script does NOT load, reference, or depend on any repeated-static report at
runtime. The --repeated-static-report argument has been removed.

RADIUS DERIVATION (conservative; never the 4° discovery radius)
---------------------------------------------------------------
  radius = (max angular distance from the location centre to any of its
            verified members) + GUARD_MARGIN_DEG
  radius = min(radius, RADIUS_CAP_DEG)

  GUARD_MARGIN_DEG = 0.5
  RADIUS_CAP_DEG   = 0.75

INPUTS
------
  --stage1-candidates : ORIGINAL Stage 1b-quarantined candidates (frame-indexed)
  --output-dir

OUTPUTS
-------
  stage1_candidates_tier_a_dry_run.json   derived, dry-run candidates
  tier_a_removed_audit.json               every removed candidate, with match info
  tier_a_dry_run_summary.json             before/after counts
  tier_a_locations.json                   resolved centres + radii actually used
"""

import argparse
import json
import math
import os
from collections import defaultdict, OrderedDict

# ── Conservative radius constants (dry-run) ──────────────────────────────────
GUARD_MARGIN_DEG = 0.5
RADIUS_CAP_DEG   = 0.75

# Sentinel: the discovery/linkage radius must never be used as an action radius.
_FORBIDDEN_DISCOVERY_RADIUS_DEG = 4.0

# ── FROZEN TIER A LOCATION MANIFEST ──────────────────────────────────────────
# Source: reviewed repeated-static audit, artifact 7841215970, run 28078249103.
# Wide-cluster diagnosis reviewed session (C005 Sub1, C006).
# DO NOT modify without explicit review decision.
#
# action_radius_deg derivation:
#   max(member→centre angular dist) + 0.5° guard, capped at 0.75°
#   (verified below in _validate_manifest at import time)
#
# member_ids are informational only. They are not resolved at runtime.
TIER_A_MANIFEST = [
    {
        "location_id":       "LOC_001",
        "physical_label":    "Stage0-hotspot-near-yaw24",
        "centre_yaw_deg":    24.492,
        "centre_pitch_deg":  13.191,
        "action_radius_deg": 0.643,
        "max_member_dist_deg": 0.143,
        "review_tier":       "tier_a_suppression_candidate",
        "review_status":     "reviewed_approved_for_dry_run",
        "evidence_artifact": "7841215970",
        "member_ids": [
            "T0035","T0050","T0056","T0057","T0062","T0067","T0074","T0077",
            "T0095","T0099","T0103","T0107","T0110","T0114","T0117","T0122",
            "T0129","T0130","T0134","T0140","T0150","T0156","T0159","T0169",
            "T0174","T0185","T0188","T0196","T0213","T0226","T0228","T0235",
            "T0247","T0251","T0257","T0270","T0272","T0275","T0300","T0330",
            "T0338","T0356","T0368","T0370","T0380","T0385","T0395","T0400",
            "T0406","T0415","T0434","T0448","T0451","T0462","T0499","T0525","T0530",
        ],
    },
    {
        "location_id":       "LOC_002",
        "physical_label":    "Stage0-hotspot-near-yaw-23",
        "centre_yaw_deg":    -22.716,
        "centre_pitch_deg":  -18.746,
        "action_radius_deg": 0.624,
        "max_member_dist_deg": 0.124,
        "review_tier":       "tier_a_suppression_candidate",
        "review_status":     "reviewed_approved_for_dry_run",
        "evidence_artifact": "7841215970",
        "member_ids": [
            "T0002","T0022","T0025","T0027","T0030","T0083","T0131","T0137",
            "T0141","T0144","T0176","T0183","T0186","T0199","T0203","T0208",
            "T0227","T0234","T0237","T0239","T0240","T0248","T0255","T0346",
            "T0382","T0390","T0396","T0414","T0418","T0428","T0430","T0436",
            "T0438","T0443","T0464","T0472","T0475","T0477","T0479","T0483",
            "T0484","T0491","T0495","T0501","T0502","T0507","T0527",
        ],
    },
    {
        "location_id":       "LOC_003",
        "physical_label":    "discovered-static-yaw134",
        "centre_yaw_deg":    133.538,
        "centre_pitch_deg":  -18.472,
        "action_radius_deg": 0.677,
        "max_member_dist_deg": 0.177,
        "review_tier":       "tier_a_suppression_candidate",
        "review_status":     "reviewed_approved_for_dry_run",
        "evidence_artifact": "7841215970",
        "member_ids": ["T0309","T0339","T0374","T0429","T0431","T0440","T0468","T0480"],
    },
    {
        "location_id":       "LOC_004",
        "physical_label":    "discovered-static-yaw-137",
        "centre_yaw_deg":    -137.353,
        "centre_pitch_deg":  -17.320,
        "action_radius_deg": 0.737,
        "max_member_dist_deg": 0.237,
        "review_tier":       "tier_a_suppression_candidate",
        "review_status":     "reviewed_approved_for_dry_run",
        "evidence_artifact": "7841215970",
        "member_ids": ["T0039","T0084","T0206","T0381","T0408","T0423"],
    },
    {
        "location_id":       "LOC_006",
        "physical_label":    "discovered-static-yaw-56",
        "centre_yaw_deg":    -55.542,
        "centre_pitch_deg":  15.806,
        "action_radius_deg": 0.750,
        "max_member_dist_deg": 0.714,
        "review_tier":       "tier_a_suppression_candidate",
        "review_status":     "reviewed_approved_for_dry_run",
        "evidence_artifact": "7841215970",
        "member_ids": ["T0191","T0350","T0442","T0457","T0460","T0503"],
    },
    {
        "location_id":       "LOC_008",
        "physical_label":    "discovered-static-yaw-139",
        "centre_yaw_deg":    -139.183,
        "centre_pitch_deg":  -21.666,
        "action_radius_deg": 0.597,
        "max_member_dist_deg": 0.097,
        "review_tier":       "tier_a_suppression_candidate",
        "review_status":     "reviewed_approved_for_dry_run",
        "evidence_artifact": "7841215970",
        "member_ids": ["T0231","T0260","T0518"],
    },
    {
        "location_id":       "LOC_C005_SUB1",
        "physical_label":    "discovered-static-C005-subcluster1",
        "centre_yaw_deg":    -134.702,
        "centre_pitch_deg":  -22.707,
        "action_radius_deg": 0.664,
        "max_member_dist_deg": 0.164,
        "review_tier":       "tier_a_suppression_candidate",
        "review_status":     "reviewed_approved_for_dry_run",
        "evidence_artifact": "7841215970",
        "member_ids": ["T0307","T0343","T0348"],
        "notes": (
            "C005 Sub1. Centre computed from 3 approved member coordinates: "
            "T0307=(-134.725,-22.623) T0343=(-134.672,-22.627) T0348=(-134.709,-22.870). "
            "Member IDs are from the reviewed audit run; not resolved at runtime."
        ),
    },
]


def _to_unit(yaw_deg, pitch_deg):
    y = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    return (
        math.cos(p) * math.sin(y),
        math.sin(p),
        math.cos(p) * math.cos(y),
    )


def _gc_deg(u, v):
    dot = max(-1.0, min(1.0, u[0]*v[0] + u[1]*v[1] + u[2]*v[2]))
    return math.degrees(math.acos(dot))


def _validate_manifest():
    """Sanity-check manifest at startup: radii within bounds, no discovery-radius leak."""
    for loc in TIER_A_MANIFEST:
        r = loc["action_radius_deg"]
        if r > RADIUS_CAP_DEG + 1e-6:
            raise AssertionError(
                f"{loc['location_id']} action_radius {r} > cap {RADIUS_CAP_DEG}"
            )
        if r >= _FORBIDDEN_DISCOVERY_RADIUS_DEG:
            raise AssertionError(
                f"{loc['location_id']} action_radius {r} >= discovery radius "
                f"{_FORBIDDEN_DISCOVERY_RADIUS_DEG}"
            )
    print(f"Manifest validation OK: {len(TIER_A_MANIFEST)} locations, "
          f"max radius {max(l['action_radius_deg'] for l in TIER_A_MANIFEST):.4f}°")


_validate_manifest()


def _match_location(yaw, pitch):
    """Closest manifest location whose action radius contains the point, else None."""
    uv = _to_unit(yaw, pitch)
    best = None
    best_dist = None
    for loc in TIER_A_MANIFEST:
        cu = _to_unit(loc["centre_yaw_deg"], loc["centre_pitch_deg"])
        d = _gc_deg(uv, cu)
        if d <= loc["action_radius_deg"] and (best_dist is None or d < best_dist):
            best = loc
            best_dist = d
    if best is None:
        return None, None, None
    return best["location_id"], best_dist, best["action_radius_deg"]


def run(args):
    with open(args.stage1_candidates) as f:
        stage1 = json.load(f)

    frames_raw = stage1.get("frames")
    if not isinstance(frames_raw, dict):
        raise ValueError(
            "Unexpected Stage 1 schema: 'frames' must be a dict of "
            "frame -> [candidate, ...]"
        )

    total_before = 0
    total_removed = 0
    removed_by_location = defaultdict(int)
    removed_audit = []

    out_frames = {}
    frames_zero_before = 0
    frames_zero_after = 0

    for frame_key, cands in frames_raw.items():
        if not isinstance(cands, list):
            raise ValueError(f"frame {frame_key} value is not a list of candidates")
        total_before += len(cands)
        if len(cands) == 0:
            frames_zero_before += 1

        kept = []
        for idx, cand in enumerate(cands):
            yaw = cand.get("yaw")
            pitch = cand.get("pitch")
            if yaw is None or pitch is None:
                kept.append(cand)
                continue
            loc_id, dist, radius = _match_location(yaw, pitch)
            if loc_id is None:
                kept.append(cand)
            else:
                total_removed += 1
                removed_by_location[loc_id] += 1
                removed_audit.append(OrderedDict([
                    ("frame", frame_key),
                    ("candidate_index", idx),
                    ("yaw", yaw),
                    ("pitch", pitch),
                    ("matched_tier_a_location", loc_id),
                    ("angular_distance_deg", round(dist, 4)),
                    ("applied_action_radius_deg", round(radius, 4)),
                    ("original_candidate", cand),
                ]))

        out_frames[frame_key] = kept
        if len(kept) == 0:
            frames_zero_after += 1

    total_after = total_before - total_removed
    frames_newly_zero = frames_zero_after - frames_zero_before

    out_stage1 = dict(stage1)
    out_stage1["frames"] = out_frames
    out_stage1["_dry_run_meta"] = {
        "experiment": "stage1_tier_a_discovered_static_dry_run",
        "approved_active_suppression": False,
        "location_source": "frozen_manifest_not_runtime_cluster_ids",
        "source_candidates": os.path.basename(args.stage1_candidates),
        "guard_margin_deg": GUARD_MARGIN_DEG,
        "radius_cap_deg": RADIUS_CAP_DEG,
        "tier_a_location_ids": [l["location_id"] for l in TIER_A_MANIFEST],
    }

    os.makedirs(args.output_dir, exist_ok=True)

    dry_path = os.path.join(args.output_dir, "stage1_candidates_tier_a_dry_run.json")
    with open(dry_path, "w") as f:
        json.dump(out_stage1, f, indent=2)

    audit_path = os.path.join(args.output_dir, "tier_a_removed_audit.json")
    with open(audit_path, "w") as f:
        json.dump({"removed": removed_audit}, f, indent=2)

    loc_out = [
        {k: v for k, v in loc.items() if k != "notes"}
        for loc in TIER_A_MANIFEST
    ]
    loc_path = os.path.join(args.output_dir, "tier_a_locations.json")
    with open(loc_path, "w") as f:
        json.dump({"locations": loc_out}, f, indent=2)

    summary = OrderedDict([
        ("experiment", "stage1_tier_a_discovered_static_dry_run"),
        ("approved_active_suppression", False),
        ("location_source", "frozen_manifest_not_runtime_cluster_ids"),
        ("guard_margin_deg", GUARD_MARGIN_DEG),
        ("radius_cap_deg", RADIUS_CAP_DEG),
        ("total_candidates_before", total_before),
        ("total_candidates_removed", total_removed),
        ("total_candidates_after", total_after),
        ("removed_by_location", dict(removed_by_location)),
        ("frames_zero_candidate_before", frames_zero_before),
        ("frames_zero_candidate_after", frames_zero_after),
        ("frames_newly_zero_candidate", frames_newly_zero),
        ("tier_a_locations", loc_out),
    ])
    summary_path = os.path.join(args.output_dir, "tier_a_dry_run_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print("Tier A dry-run filter complete.")
    print(f"  Location source: frozen manifest ({len(TIER_A_MANIFEST)} locations)")
    print(f"  Before: {total_before}  Removed: {total_removed}  After: {total_after}")
    print(f"  Removed by location: {dict(removed_by_location)}")
    print(f"  Frames newly zero-candidate: {frames_newly_zero}")
    print(f"  Outputs: {dry_path}, {audit_path}, {summary_path}, {loc_path}")


def main():
    p = argparse.ArgumentParser(
        description="FFA Stage 1 Tier A discovered-static DRY-RUN filter"
    )
    p.add_argument("--stage1-candidates", required=True,
                   help="ORIGINAL Stage 1b-quarantined candidates (frame-indexed)")
    p.add_argument("--output-dir", default="tier_a_dry_run_output")
    run(p.parse_args())


if __name__ == "__main__":
    main()
