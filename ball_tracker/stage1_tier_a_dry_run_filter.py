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

WHAT IT REMOVES
---------------
Only candidates that fall inside an approved Tier A per-location action radius.

Approved Tier A locations (from reviewed state):
  C001, C002, C003, C004, C006, C008   — whole-cluster centres from the
                                          repeated-static report
  C005 Sub1                            — defined by member tracklets
                                          T0348, T0343, T0307 only; centre
                                          computed from those three members

Explicitly NOT included: C005 Sub2/Sub3, C007, C009/T0143, and any wide,
singleton, or annotation-only location.

RADIUS DERIVATION (conservative; never the 4° discovery radius)
---------------------------------------------------------------
Per approved decision for this dry-run:

  radius = (max angular distance from the location centre to any of its
            verified members) + GUARD_MARGIN_DEG
  radius = min(radius, RADIUS_CAP_DEG)

  GUARD_MARGIN_DEG = 0.5
  RADIUS_CAP_DEG   = 0.75   (caps C005 Sub1 and C006 for this first practical test)

For whole-cluster Tier A locations the "members" are the cluster members in the
repeated-static report. For C005 Sub1 the members are exactly the three named
tracklets.

INPUTS
------
  --stage1-candidates : ORIGINAL Stage 1b-quarantined candidates (frame-indexed)
  --repeated-static-report : stage2_repeated_static_report.json (cluster centres
                             + members), regenerated in-workflow from tracklets
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

# Approved whole-cluster Tier A location IDs (centre taken from report cluster).
TIER_A_WHOLE_CLUSTERS = ["C001", "C002", "C003", "C004", "C006", "C008"]

# C005 Sub1 is defined ONLY by these member tracklet IDs.
C005_SUB1_MEMBER_IDS = ["T0348", "T0343", "T0307"]
C005_SUB1_ID         = "C005_SUB1"


# ── Geometry ─────────────────────────────────────────────────────────────────
def _to_unit(yaw_deg, pitch_deg):
    y = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    return (
        math.cos(p) * math.sin(y),
        math.sin(p),
        math.cos(p) * math.cos(y),
    )


def _gc_deg(u, v):
    dot = u[0] * v[0] + u[1] * v[1] + u[2] * v[2]
    dot = max(-1.0, min(1.0, dot))
    return math.degrees(math.acos(dot))


def _centre_from_points(points):
    """Mean unit vector of (yaw, pitch) points, renormalised; back to yaw/pitch."""
    sx = sy = sz = 0.0
    for (yaw, pitch) in points:
        x, y, z = _to_unit(yaw, pitch)
        sx += x; sy += y; sz += z
    n = len(points)
    sx /= n; sy /= n; sz /= n
    mag = math.sqrt(sx * sx + sy * sy + sz * sz)
    if mag == 0:
        raise ValueError("degenerate centre")
    sx /= mag; sy /= mag; sz /= mag
    centre_yaw = math.degrees(math.atan2(sx, sz))
    centre_pitch = math.degrees(math.asin(max(-1.0, min(1.0, sy))))
    return centre_yaw, centre_pitch, (sx, sy, sz)


def _derive_radius(centre_unit, member_points):
    """max(member->centre dist) + guard, capped. Returns (radius, raw, max_dist)."""
    max_dist = 0.0
    for (yaw, pitch) in member_points:
        d = _gc_deg(centre_unit, _to_unit(yaw, pitch))
        if d > max_dist:
            max_dist = d
    raw = max_dist + GUARD_MARGIN_DEG
    radius = min(raw, RADIUS_CAP_DEG)
    return radius, raw, max_dist


# ── Build Tier A location index from the repeated-static report ──────────────
def build_tier_a_locations(report):
    clusters = {c["cluster_id"]: c for c in report["clusters"]}
    locations = []

    # Whole-cluster Tier A locations
    for cid in TIER_A_WHOLE_CLUSTERS:
        if cid not in clusters:
            raise KeyError(f"Tier A cluster {cid} not found in repeated-static report")
        c = clusters[cid]
        members = c.get("members", [])
        if not members:
            raise ValueError(f"cluster {cid} has no members in report")
        member_points = [(m["median_yaw_deg"], m["median_pitch_deg"]) for m in members]
        centre_yaw = c["centre_yaw_deg"]
        centre_pitch = c["centre_pitch_deg"]
        centre_unit = _to_unit(centre_yaw, centre_pitch)
        radius, raw, max_dist = _derive_radius(centre_unit, member_points)
        locations.append(OrderedDict([
            ("location_id", cid),
            ("kind", "whole_cluster"),
            ("centre_yaw_deg", round(centre_yaw, 4)),
            ("centre_pitch_deg", round(centre_pitch, 4)),
            ("member_ids", [m["id"] for m in members]),
            ("member_count", len(members)),
            ("max_member_dist_deg", round(max_dist, 4)),
            ("guard_margin_deg", GUARD_MARGIN_DEG),
            ("raw_radius_deg", round(raw, 4)),
            ("radius_cap_deg", RADIUS_CAP_DEG),
            ("action_radius_deg", round(radius, 4)),
            ("centre_unit", centre_unit),
        ]))

    # C005 Sub1 — centre computed from hardcoded approved coordinates.
    # These are the reviewed median_yaw/median_pitch values from the approved
    # discovered-static audit (artifact 7841215970, run 28078249103).
    # Runtime lookup is intentionally avoided: the fresh Stage 2 run inside this
    # workflow may assign different tracklet IDs, so the member IDs cannot be
    # resolved against the live report.
    C005_SUB1_APPROVED_COORDS = {
        "T0307": (-134.725, -22.623),
        "T0343": (-134.672, -22.627),
        "T0348": (-134.709, -22.870),
    }
    sub1_points = list(C005_SUB1_APPROVED_COORDS.values())
    sub1_found  = list(C005_SUB1_APPROVED_COORDS.keys())
    c_yaw, c_pitch, c_unit = _centre_from_points(sub1_points)
    radius, raw, max_dist = _derive_radius(c_unit, sub1_points)
    locations.append(OrderedDict([
        ("location_id", C005_SUB1_ID),
        ("kind", "subcluster"),
        ("centre_yaw_deg", round(c_yaw, 4)),
        ("centre_pitch_deg", round(c_pitch, 4)),
        ("member_ids", sub1_found),
        ("member_count", len(sub1_found)),
        ("max_member_dist_deg", round(max_dist, 4)),
        ("guard_margin_deg", GUARD_MARGIN_DEG),
        ("raw_radius_deg", round(raw, 4)),
        ("radius_cap_deg", RADIUS_CAP_DEG),
        ("action_radius_deg", round(radius, 4)),
        ("centre_unit", c_unit),
    ]))

    # Safety: no action radius may equal/exceed the discovery radius.
    for loc in locations:
        if loc["action_radius_deg"] >= _FORBIDDEN_DISCOVERY_RADIUS_DEG:
            raise AssertionError(
                f"{loc['location_id']} action radius {loc['action_radius_deg']} "
                f">= forbidden discovery radius {_FORBIDDEN_DISCOVERY_RADIUS_DEG}"
            )
    return locations


def _match_location(yaw, pitch, locations):
    """Closest Tier A location whose action radius contains the point, else None."""
    uv = _to_unit(yaw, pitch)
    best = None
    best_dist = None
    for loc in locations:
        d = _gc_deg(uv, loc["centre_unit"])
        if d <= loc["action_radius_deg"] and (best_dist is None or d < best_dist):
            best = loc
            best_dist = d
    if best is None:
        return None, None, None
    return best["location_id"], best_dist, best["action_radius_deg"]


# ── Main filtering ───────────────────────────────────────────────────────────
def run(args):
    with open(args.stage1_candidates) as f:
        stage1 = json.load(f)
    with open(args.repeated_static_report) as f:
        report = json.load(f)

    locations = build_tier_a_locations(report)

    # Real Stage 1 schema (verified):
    #   stage1["frames"] = { "<frame_int>": [candidate, ...], ... }
    #   each frame value is a list of candidate dicts directly.
    #   stage1["quarantined_candidates"] and all other top-level metadata
    #   (fps, total_frames, pitch bounds, hotspot_map, stage0_detections,
    #   stage1b, ...) are preserved untouched.
    frames_raw = stage1.get("frames")
    if not isinstance(frames_raw, dict):
        raise ValueError(
            "Unexpected Stage 1 schema: 'frames' must be a dict of "
            "frame -> [candidate, ...]"
        )

    total_before = 0
    total_removed = 0
    removed_by_cluster = defaultdict(int)
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
            loc_id, dist, radius = _match_location(yaw, pitch, locations)
            if loc_id is None:
                kept.append(cand)
            else:
                total_removed += 1
                removed_by_cluster[loc_id] += 1
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

    # Reassemble: copy ALL original top-level keys, swap only the filtered frames.
    out_stage1 = dict(stage1)
    out_stage1["frames"] = out_frames
    out_stage1["_dry_run_meta"] = {
        "experiment": "stage1_tier_a_discovered_static_dry_run",
        "approved_active_suppression": False,
        "source_candidates": os.path.basename(args.stage1_candidates),
        "guard_margin_deg": GUARD_MARGIN_DEG,
        "radius_cap_deg": RADIUS_CAP_DEG,
        "tier_a_location_ids": [l["location_id"] for l in locations],
    }

    os.makedirs(args.output_dir, exist_ok=True)

    dry_path = os.path.join(args.output_dir, "stage1_candidates_tier_a_dry_run.json")
    with open(dry_path, "w") as f:
        json.dump(out_stage1, f, indent=2)

    audit_path = os.path.join(args.output_dir, "tier_a_removed_audit.json")
    with open(audit_path, "w") as f:
        json.dump({"removed": removed_audit}, f, indent=2)

    # locations.json without the raw unit tuple noise duplicated
    loc_out = []
    for l in locations:
        d = dict(l)
        d.pop("centre_unit", None)
        loc_out.append(d)
    loc_path = os.path.join(args.output_dir, "tier_a_locations.json")
    with open(loc_path, "w") as f:
        json.dump({"locations": loc_out}, f, indent=2)

    summary = OrderedDict([
        ("experiment", "stage1_tier_a_discovered_static_dry_run"),
        ("approved_active_suppression", False),
        ("guard_margin_deg", GUARD_MARGIN_DEG),
        ("radius_cap_deg", RADIUS_CAP_DEG),
        ("total_candidates_before", total_before),
        ("total_candidates_removed", total_removed),
        ("total_candidates_after", total_after),
        ("removed_by_location", dict(removed_by_cluster)),
        ("frames_zero_candidate_before", frames_zero_before),
        ("frames_zero_candidate_after", frames_zero_after),
        ("frames_newly_zero_candidate", frames_newly_zero),
        ("tier_a_locations", loc_out),
    ])
    summary_path = os.path.join(args.output_dir, "tier_a_dry_run_summary.json")
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    print("Tier A dry-run filter complete.")
    print(f"  Locations: {[l['location_id'] for l in locations]}")
    print(f"  Before: {total_before}  Removed: {total_removed}  After: {total_after}")
    print(f"  Removed by location: {dict(removed_by_cluster)}")
    print(f"  Frames newly zero-candidate: {frames_newly_zero}")
    print(f"  Outputs: {dry_path}, {audit_path}, {summary_path}, {loc_path}")


def main():
    p = argparse.ArgumentParser(description="FFA Stage 1 Tier A discovered-static DRY-RUN filter")
    p.add_argument("--stage1-candidates", required=True,
                   help="ORIGINAL Stage 1b-quarantined candidates (frame-indexed)")
    p.add_argument("--repeated-static-report", required=True,
                   help="stage2_repeated_static_report.json with cluster centres + members")
    p.add_argument("--output-dir", default="tier_a_dry_run_output")
    run(p.parse_args())


if __name__ == "__main__":
    main()

