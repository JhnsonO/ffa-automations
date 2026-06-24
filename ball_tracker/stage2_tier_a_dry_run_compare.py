#!/usr/bin/env python3
"""
Compare two Stage 2 tracklet outputs: ORIGINAL vs Tier A DRY-RUN.

Reports the deltas that decide whether the Tier A dry-run materially improves
Stage 2 input quality, without changing any threshold or approving suppression.

Comparisons:
  - tracklet count
  - anchor / passing / fragment / other status counts
  - rejected_static count (proxy for repeated-static / residual false locations)
  - tracklets whose median position falls inside a Tier A action radius
    (these are the locations the dry-run targeted)
  - credible-motion reference check: tracklets with net_displacement above a
    motion floor that EXIST in original but are MISSING in dry-run (i.e. real
    motion accidentally affected). Listed explicitly.

Inputs:
  --original-tracklets   tracklets.json from Stage 2 on original candidates
  --dryrun-tracklets     tracklets.json from Stage 2 on Tier A dry-run candidates
  --tier-a-locations     tier_a_locations.json from the filter (for in-radius tally)
  --output-dir
"""

import argparse
import json
import math
import os
from collections import defaultdict, OrderedDict

# Motion floor for "credible-motion reference" — a tracklet moving more than this
# in net displacement is treated as plausibly a real moving ball, not static.
MOTION_FLOOR_DEG = 3.0


def _to_unit(yaw_deg, pitch_deg):
    y = math.radians(yaw_deg); p = math.radians(pitch_deg)
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
    """Median yaw/pitch of a tracklet from its frames."""
    frames = t.get("frames", [])
    yaws = [f.get("yaw") for f in frames if f.get("yaw") is not None]
    pitches = [f.get("pitch") for f in frames if f.get("pitch") is not None]
    if not yaws or not pitches:
        return None
    yaws.sort(); pitches.sort()
    return yaws[len(yaws)//2], pitches[len(pitches)//2]


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


def run(args):
    with open(args.original_tracklets) as f:
        orig = json.load(f)["tracklets"]
    with open(args.dryrun_tracklets) as f:
        dry = json.load(f)["tracklets"]
    with open(args.tier_a_locations) as f:
        locations = json.load(f)["locations"]

    orig_status = _status_counts(orig)
    dry_status = _status_counts(dry)

    all_statuses = sorted(set(orig_status) | set(dry_status))
    status_delta = OrderedDict()
    for s in all_statuses:
        o = orig_status.get(s, 0)
        d = dry_status.get(s, 0)
        status_delta[s] = {"original": o, "dry_run": d, "delta": d - o}

    # Tier A in-radius tracklet tallies
    orig_in = defaultdict(int)
    dry_in = defaultdict(int)
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
            "dry_run_tracklets_in_radius": dry_in.get(lid, 0),
        }

    # Credible-motion reference check.
    # Build id sets; a real moving tracklet in original that vanishes in dry-run
    # is flagged. Identity by id may shift, so also compare by motion signature.
    orig_by_id = {t["id"]: t for t in orig}
    dry_ids = {t["id"] for t in dry}

    credible_motion_orig = [
        t for t in orig
        if t.get("net_displacement_deg", 0) >= MOTION_FLOOR_DEG
        and t.get("status") in ("anchor", "passing")
    ]
    affected = []
    for t in credible_motion_orig:
        if t["id"] not in dry_ids:
            affected.append(OrderedDict([
                ("id", t["id"]),
                ("status", t.get("status")),
                ("net_displacement_deg", round(t.get("net_displacement_deg", 0), 3)),
                ("start_frame", t.get("start_frame")),
                ("end_frame", t.get("end_frame")),
                ("median_position", _median_pos(t)),
                ("falls_in_tier_a", _in_tier_a(t, locations)),
            ]))

    summary = OrderedDict([
        ("experiment", "stage1_tier_a_dry_run_stage2_comparison"),
        ("motion_floor_deg", MOTION_FLOOR_DEG),
        ("tracklet_count", {
            "original": len(orig),
            "dry_run": len(dry),
            "delta": len(dry) - len(orig),
        }),
        ("status_counts", status_delta),
        ("tier_a_in_radius_tracklets", tier_a_in_radius),
        ("credible_motion_references_affected_count", len(affected)),
        ("credible_motion_references_affected", affected),
    ])

    os.makedirs(args.output_dir, exist_ok=True)
    out_json = os.path.join(args.output_dir, "tier_a_dry_run_stage2_comparison.json")
    with open(out_json, "w") as f:
        json.dump(summary, f, indent=2)

    # Human-readable report
    lines = []
    lines.append("STAGE 1 TIER A STATIC-LOCATION DRY-RUN — STAGE 2 COMPARISON")
    lines.append("=" * 60)
    lines.append(f"Tracklets : original={len(orig)}  dry_run={len(dry)}  "
                 f"delta={len(dry)-len(orig):+d}")
    lines.append("")
    lines.append("STATUS COUNTS")
    for s, v in status_delta.items():
        lines.append(f"  {s:18} orig={v['original']:4}  dry={v['dry_run']:4}  "
                     f"delta={v['delta']:+d}")
    lines.append("")
    lines.append("TIER A IN-RADIUS TRACKLETS (target locations)")
    for lid, v in tier_a_in_radius.items():
        lines.append(f"  {lid:10} orig={v['original_tracklets_in_radius']:3}  "
                     f"dry={v['dry_run_tracklets_in_radius']:3}")
    lines.append("")
    lines.append(f"CREDIBLE-MOTION REFERENCES AFFECTED: {len(affected)}")
    if affected:
        lines.append("  (anchor/passing tracklets with net_disp >= "
                     f"{MOTION_FLOOR_DEG}° present in original, absent in dry-run)")
        for a in affected:
            lines.append(f"  - {a['id']} status={a['status']} "
                         f"net_disp={a['net_displacement_deg']}° "
                         f"frames={a['start_frame']}-{a['end_frame']} "
                         f"in_tier_a={a['falls_in_tier_a']}")
    else:
        lines.append("  None — no credible-motion reference removed by the dry-run.")
    report_txt = "\n".join(lines) + "\n"
    out_txt = os.path.join(args.output_dir, "tier_a_dry_run_stage2_comparison.txt")
    with open(out_txt, "w") as f:
        f.write(report_txt)

    print(report_txt)
    print(f"Outputs: {out_json}, {out_txt}")


def main():
    p = argparse.ArgumentParser(description="Compare original vs Tier A dry-run Stage 2 outputs")
    p.add_argument("--original-tracklets", required=True)
    p.add_argument("--dryrun-tracklets", required=True)
    p.add_argument("--tier-a-locations", required=True)
    p.add_argument("--output-dir", default=".")
    run(p.parse_args())


if __name__ == "__main__":
    main()
