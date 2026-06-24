#!/usr/bin/env python3
"""
Compare two Stage 2 tracklet outputs: ORIGINAL vs Tier A DRY-RUN.

Reports whether the Tier A dry-run materially improves Stage 2 input quality,
without changing any threshold or approving suppression.

CREDIBLE-MOTION CONTINUITY CHECK (replaces tracklet-ID comparison)
-------------------------------------------------------------------
Stage 2 tracklet IDs are NOT stable across runs: removing candidates can split,
merge, or renumber tracklets. Comparing by ID to detect "missing credible motion"
is therefore invalid.

Instead, for every credible-motion window found in the ORIGINAL tracklets
(anchor/passing, net_displacement >= MOTION_FLOOR_DEG), this script checks:

  1. Frame support: does the dry-run candidate file have >= 1 candidate in the
     frame range [start_frame, end_frame]?
     DIAGNOSTIC ONLY — does not count as continuity.
  2. Spatial support: is any of those candidates within SPATIAL_TOL_DEG of the
     original tracklet's median position?
  3. Linked support: does the dry-run tracklet set contain any tracklet that
     overlaps the frame window and has a median position within SPATIAL_TOL_DEG?

CONTINUITY DEFINITION (corrected):
  is_continuous = has_spatial_support OR has_linked_support
  Frame-only support does NOT constitute continuity.

OUTCOME CATEGORIES per motion window:
  spatial_or_linked_continuous  — spatial or linked support present (safe)
  frame_only_unsupported        — frame support only; no spatial or linked (unsafe)
  no_support                    — no frame, spatial, or linked support (unsafe)

ACCEPTANCE VERDICT:
  The dry-run FAILS safety acceptance if any credible-motion window is classified
  as frame_only_unsupported or no_support.

Inputs:
  --original-tracklets    tracklets.json from Stage 2 on original candidates
  --dryrun-tracklets      tracklets.json from Stage 2 on Tier A dry-run candidates
  --dryrun-candidates     stage1_candidates_tier_a_dry_run.json (for frame support check)
  --tier-a-locations      tier_a_locations.json from the filter (for in-radius tally)
  --output-dir
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict, OrderedDict

# A tracklet is "credible motion" if it is anchor/passing and moves at least this far.
MOTION_FLOOR_DEG = 3.0

# Spatial tolerance for continuity check: a dry-run candidate or tracklet is
# considered spatially consistent if it falls within this of the original median.
SPATIAL_TOL_DEG = 2.0

# Outcome category labels
OUTCOME_SPATIAL_OR_LINKED = "spatial_or_linked_continuous"
OUTCOME_FRAME_ONLY        = "frame_only_unsupported"
OUTCOME_NO_SUPPORT        = "no_support"


def _to_unit(yaw_deg, pitch_deg):
    y = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    return (math.cos(p) * math.sin(y), math.sin(p), math.cos(p) * math.cos(y))


def _gc_deg(u, v):
    d = max(-1.0, min(1.0, u[0]*v[0] + u[1]*v[1] + u[2]*v[2]))
    return math.degrees(math.acos(d))


def _status_counts(tracklets):
    c = defaultdict(int)
    for t in tracklets:
        c[t.get("status", "unknown")] += 1
    return dict(c)


def _median_pos(t):
    """Median yaw/pitch from tracklet observations or frames list."""
    # Stage 2 may use 'observations' or 'frames'
    obs = t.get("observations") or t.get("frames") or []
    yaws   = sorted(f.get("yaw")   for f in obs if f.get("yaw")   is not None)
    pitches = sorted(f.get("pitch") for f in obs if f.get("pitch") is not None)
    if not yaws or not pitches:
        return None
    return yaws[len(yaws) // 2], pitches[len(pitches) // 2]


def _in_tier_a(t, locations):
    pos = _median_pos(t)
    if pos is None:
        return None
    uv = _to_unit(pos[0], pos[1])
    for loc in locations:
        cu = _to_unit(loc["centre_yaw_deg"], loc["centre_pitch_deg"])
        if _gc_deg(uv, cu) <= loc["action_radius_deg"]:
            return loc["location_id"]
    return None


def _build_dry_candidate_index(dry_cands_data):
    """
    Build frame -> list of (yaw, pitch) from the dry-run candidate file.
    Handles both frame-indexed dict schema and flat list schema.
    """
    index = defaultdict(list)
    frames = dry_cands_data.get("frames")
    if isinstance(frames, dict):
        for frame_key, cands in frames.items():
            if isinstance(cands, list):
                for c in cands:
                    y, p = c.get("yaw"), c.get("pitch")
                    if y is not None and p is not None:
                        index[int(frame_key)].append((y, p))
    return index


def _check_continuity(orig_t, dry_cand_index, dry_tracklets, spatial_tol):
    """
    For a credible-motion original tracklet, check three support conditions
    against the dry-run.

    Continuity definition (corrected):
      is_continuous = has_spatial_support OR has_linked_support
      Frame-only support is diagnostic only and does NOT constitute continuity.

    Outcome category:
      spatial_or_linked_continuous  — is_continuous is True
      frame_only_unsupported        — frame support only, no spatial/linked
      no_support                    — no support of any kind

    Returns dict with keys:
      frame_range           (start, end)
      median_pos            (yaw, pitch) or None
      has_frame_support     bool  — dry-run has >=1 candidate in frame range
                                    (DIAGNOSTIC ONLY)
      has_spatial_support   bool  — >=1 of those within spatial_tol of median
      has_linked_support    bool  — dry-run tracklet overlaps window + within spatial_tol
      is_continuous         bool  — has_spatial_support OR has_linked_support
      outcome               str   — one of the three outcome category labels
      nearest_frame_dist    float or None  — closest spatial dist in frame range
      nearest_linked_dist   float or None
    """
    start = orig_t.get("start_frame", 0)
    end   = orig_t.get("end_frame",   0)
    mpos  = _median_pos(orig_t)

    # Frame support (diagnostic only)
    frame_cands = []
    for f in range(start, end + 1):
        frame_cands.extend(dry_cand_index.get(f, []))
    has_frame_support = len(frame_cands) > 0

    # Spatial support within frame range
    has_spatial_support = False
    nearest_frame_dist = None
    if mpos is not None:
        mu = _to_unit(mpos[0], mpos[1])
        for (cy, cp) in frame_cands:
            d = _gc_deg(mu, _to_unit(cy, cp))
            if nearest_frame_dist is None or d < nearest_frame_dist:
                nearest_frame_dist = d
            if d <= spatial_tol:
                has_spatial_support = True

    # Linked support: dry-run tracklet overlapping window and spatially close
    has_linked_support = False
    nearest_linked_dist = None
    for dt in dry_tracklets:
        ds = dt.get("start_frame", 0)
        de = dt.get("end_frame",   0)
        # overlapping window
        if ds > end or de < start:
            continue
        if mpos is None:
            has_linked_support = True  # frame-overlap alone counts if no position
            break
        dp = _median_pos(dt)
        if dp is None:
            continue
        mu = _to_unit(mpos[0], mpos[1])
        d = _gc_deg(mu, _to_unit(dp[0], dp[1]))
        if nearest_linked_dist is None or d < nearest_linked_dist:
            nearest_linked_dist = d
        if d <= spatial_tol:
            has_linked_support = True
            break

    # Continuity: spatial OR linked only; frame-only does not count
    is_continuous = has_spatial_support or has_linked_support

    # Outcome category
    if is_continuous:
        outcome = OUTCOME_SPATIAL_OR_LINKED
    elif has_frame_support:
        outcome = OUTCOME_FRAME_ONLY
    else:
        outcome = OUTCOME_NO_SUPPORT

    return OrderedDict([
        ("frame_range",          [start, end]),
        ("median_pos",           list(mpos) if mpos else None),
        ("has_frame_support",    has_frame_support),
        ("has_spatial_support",  has_spatial_support),
        ("has_linked_support",   has_linked_support),
        ("is_continuous",        is_continuous),
        ("outcome",              outcome),
        ("nearest_frame_dist_deg",  round(nearest_frame_dist, 3) if nearest_frame_dist is not None else None),
        ("nearest_linked_dist_deg", round(nearest_linked_dist, 3) if nearest_linked_dist is not None else None),
    ])


def run(args):
    with open(args.original_tracklets) as f:
        orig = json.load(f)["tracklets"]
    with open(args.dryrun_tracklets) as f:
        dry = json.load(f)["tracklets"]
    with open(args.tier_a_locations) as f:
        locations = json.load(f)["locations"]
    with open(args.dryrun_candidates) as f:
        dry_cands_data = json.load(f)
    dry_cand_index = _build_dry_candidate_index(dry_cands_data)

    # Status counts
    orig_status = _status_counts(orig)
    dry_status  = _status_counts(dry)
    all_statuses = sorted(set(orig_status) | set(dry_status))
    status_delta = OrderedDict()
    for s in all_statuses:
        o = orig_status.get(s, 0)
        d = dry_status.get(s, 0)
        status_delta[s] = {"original": o, "dry_run": d, "delta": d - o}

    # Tier A in-radius tracklet tallies
    orig_in = defaultdict(int)
    dry_in  = defaultdict(int)
    for t in orig:
        loc = _in_tier_a(t, locations)
        if loc:
            orig_in[loc] += 1
    for t in dry:
        loc = _in_tier_a(t, locations)
        if loc:
            dry_in[loc] += 1
    tier_a_in_radius = OrderedDict()
    for loc in locations:
        lid = loc["location_id"]
        tier_a_in_radius[lid] = {
            "original_tracklets_in_radius": orig_in.get(lid, 0),
            "dry_run_tracklets_in_radius":  dry_in.get(lid, 0),
        }

    # Credible-motion continuity check
    credible_windows = [
        t for t in orig
        if t.get("net_displacement_deg", 0) >= MOTION_FLOOR_DEG
        and t.get("status") in ("anchor", "passing")
    ]
    continuity_results = []
    frame_only_unsupported = []
    no_support_windows = []
    for t in credible_windows:
        cont = _check_continuity(t, dry_cand_index, dry, SPATIAL_TOL_DEG)
        entry = OrderedDict([
            ("original_id",           t["id"]),
            ("status",                t.get("status")),
            ("net_displacement_deg",  round(t.get("net_displacement_deg", 0), 3)),
            ("falls_in_tier_a",       _in_tier_a(t, locations)),
        ])
        entry.update(cont)
        continuity_results.append(entry)
        if cont["outcome"] == OUTCOME_FRAME_ONLY:
            frame_only_unsupported.append(entry)
        elif cont["outcome"] == OUTCOME_NO_SUPPORT:
            no_support_windows.append(entry)

    # All unsafe windows (frame-only or no-support)
    unsafe_windows = frame_only_unsupported + no_support_windows
    continuous_count = len(credible_windows) - len(unsafe_windows)

    # Acceptance verdict: FAIL if any window is frame-only or no-support
    acceptance_verdict = "PASS" if len(unsafe_windows) == 0 else "FAIL"

    # Outcome category counts
    outcome_counts = OrderedDict([
        (OUTCOME_SPATIAL_OR_LINKED, continuous_count),
        (OUTCOME_FRAME_ONLY,        len(frame_only_unsupported)),
        (OUTCOME_NO_SUPPORT,        len(no_support_windows)),
    ])

    summary = OrderedDict([
        ("experiment",       "stage1_tier_a_dry_run_stage2_comparison"),
        ("motion_floor_deg", MOTION_FLOOR_DEG),
        ("spatial_tol_deg",  SPATIAL_TOL_DEG),
        ("continuity_check_method", "spatial_or_linked_only_not_frame_only"),
        ("continuity_definition",   "is_continuous = has_spatial_support OR has_linked_support"),
        ("frame_support_role",      "diagnostic only — does not constitute continuity"),
        ("acceptance_verdict",      acceptance_verdict),
        ("tracklet_count", {
            "original": len(orig),
            "dry_run":  len(dry),
            "delta":    len(dry) - len(orig),
        }),
        ("status_counts", status_delta),
        ("tier_a_in_radius_tracklets", tier_a_in_radius),
        ("credible_motion_windows_checked",          len(credible_windows)),
        ("credible_motion_windows_continuous",        continuous_count),
        ("credible_motion_windows_frame_only_unsafe", len(frame_only_unsupported)),
        ("credible_motion_windows_no_support",        len(no_support_windows)),
        ("outcome_counts",           outcome_counts),
        ("frame_only_unsupported_windows", frame_only_unsupported),
        ("no_support_windows",             no_support_windows),
        ("all_continuity_results",         continuity_results),
    ])

    os.makedirs(args.output_dir, exist_ok=True)
    out_json = os.path.join(args.output_dir, "tier_a_dry_run_stage2_comparison.json")
    with open(out_json, "w") as f:
        json.dump(summary, f, indent=2)

    lines = []
    lines.append("STAGE 1 TIER A STATIC-LOCATION DRY-RUN — STAGE 2 COMPARISON")
    lines.append("=" * 60)
    lines.append(f"Tracklets : original={len(orig)}  dry_run={len(dry)}  "
                 f"delta={len(dry)-len(orig):+d}")
    lines.append("")
    lines.append("STATUS COUNTS")
    for s, v in status_delta.items():
        lines.append(f"  {s:22} orig={v['original']:4}  dry={v['dry_run']:4}  "
                     f"delta={v['delta']:+d}")
    lines.append("")
    lines.append("TIER A IN-RADIUS TRACKLETS (target locations)")
    for lid, v in tier_a_in_radius.items():
        lines.append(f"  {lid:14} orig={v['original_tracklets_in_radius']:3}  "
                     f"dry={v['dry_run_tracklets_in_radius']:3}")
    lines.append("")
    lines.append(f"CREDIBLE-MOTION CONTINUITY CHECK  "
                 f"(motion_floor={MOTION_FLOOR_DEG}°, spatial_tol={SPATIAL_TOL_DEG}°)")
    lines.append(f"  Continuity definition : spatial OR linked support only")
    lines.append(f"  Frame-only support    : diagnostic only (does NOT count as continuity)")
    lines.append(f"  Windows checked       : {len(credible_windows)}")
    lines.append(f"  {OUTCOME_SPATIAL_OR_LINKED:40}: {continuous_count}")
    lines.append(f"  {OUTCOME_FRAME_ONLY:40}: {len(frame_only_unsupported)}")
    lines.append(f"  {OUTCOME_NO_SUPPORT:40}: {len(no_support_windows)}")
    lines.append("")
    lines.append(f"ACCEPTANCE VERDICT: {acceptance_verdict}")
    if acceptance_verdict == "FAIL":
        lines.append(f"  REASON: {len(unsafe_windows)} credible-motion window(s) have "
                     f"frame-only or no support — spatial/linked continuity not confirmed.")
        if frame_only_unsupported:
            lines.append(f"  FRAME-ONLY UNSUPPORTED windows ({len(frame_only_unsupported)}):")
            for d in frame_only_unsupported:
                lines.append(
                    f"    {d['original_id']} status={d['status']} "
                    f"net_disp={d['net_displacement_deg']}° "
                    f"frames={d['frame_range'][0]}-{d['frame_range'][1]} "
                    f"in_tier_a={d['falls_in_tier_a']} "
                    f"nearest_frame_dist={d['nearest_frame_dist_deg']}°"
                )
        if no_support_windows:
            lines.append(f"  NO-SUPPORT windows ({len(no_support_windows)}):")
            for d in no_support_windows:
                lines.append(
                    f"    {d['original_id']} status={d['status']} "
                    f"net_disp={d['net_displacement_deg']}° "
                    f"frames={d['frame_range'][0]}-{d['frame_range'][1]} "
                    f"in_tier_a={d['falls_in_tier_a']}"
                )
    else:
        lines.append("  All credible-motion windows retain spatial or linked continuity.")

    report_txt = "\n".join(lines) + "\n"
    out_txt = os.path.join(args.output_dir, "tier_a_dry_run_stage2_comparison.txt")
    with open(out_txt, "w") as f:
        f.write(report_txt)

    print(report_txt)
    print(f"Outputs: {out_json}, {out_txt}")

    if acceptance_verdict == "FAIL":
        sys.exit(1)


def main():
    p = argparse.ArgumentParser(
        description="Compare original vs Tier A dry-run Stage 2 outputs"
    )
    p.add_argument("--original-tracklets",  required=True)
    p.add_argument("--dryrun-tracklets",    required=True)
    p.add_argument("--dryrun-candidates",   required=True,
                   help="stage1_candidates_tier_a_dry_run.json for frame support check")
    p.add_argument("--tier-a-locations",    required=True)
    p.add_argument("--output-dir",          default=".")
    run(p.parse_args())


if __name__ == "__main__":
    main()
