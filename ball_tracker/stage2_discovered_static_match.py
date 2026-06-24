#!/usr/bin/env python3
"""
FFA Offline Recovery Pipeline — Stage 2: Discovered-Static Location Match
==========================================================================
Annotation-only layer.  Reads tracklets.json and a reviewed-cluster
configuration (from stage2_repeated_static_report.json), then annotates
each near-static tracklet with whether its median position falls inside a
visually-verified discovered-static location.

Does NOT alter tracklet status, scores, Stage 2 link thresholds, Stage 1
output, Stage 1b quarantine, the renderer, or any frozen module.
Does NOT use or reuse the 4° discovery/clustering radius as an action radius.

Inputs
------
  --tracklets      : stage2 tracklets.json  (never modified)
  --report         : stage2_repeated_static_report.json  (cluster centres,
                     member distributions, member positions)
  --reviewed-ids   : comma-separated cluster IDs that have passed visual
                     review (default: C001,C002,C003,C004,C005,C006,C007,C008,C009)
  --guard-margin   : fixed angular guard added on top of p95 radius (deg, default 0.5)
  --radius-cap     : maximum allowed match radius per cluster (deg, default 6.0)
  --output-dir     : directory for outputs (default: .)

Outputs
-------
  tracklets_repeated_static_audit.json   — copy of all tracklets with annotation
                                           fields added to near-static ones
  stage2_discovered_static_report.json   — per-cluster derived radii, match counts
  stage2_discovered_static_report.txt    — human-readable summary

Per-tracklet annotation fields (near-static tracklets only; others unchanged)
------------------------------------------------------------------------------
  repeated_static_location_match    : bool   — true if median falls inside a
                                               reviewed cluster match radius
  repeated_static_cluster_id        : str|null  — e.g. "C003"
  repeated_static_match_distance_deg: float|null
  repeated_static_match_radius_deg  : float|null  — derived per-cluster radius
  would_suppress_repeated_static    : bool   — true iff match is true

Eligibility for matching (same as repeated-static audit near-static gate)
--------------------------------------------------------------------------
  - status != "rejected_static"
  - net_displacement_deg < NET_DISP_CEILING (1.5°)
  - observation_count >= MIN_OBS (3)
  - span_frames >= MIN_SPAN (5)
  - net_displacement_deg < MAJOR_MOTION_EXCLUSION_DEG (42°)

Per-cluster match radius derivation
-------------------------------------
  1. Collect great-circle distances from cluster centre to each member's
     median position (available in the cluster report).
  2. Compute p95 of those distances.
  3. Add GUARD_MARGIN_DEG (default 0.5°).
  4. Cap at RADIUS_CAP_DEG (default 6.0°).
  5. Floor at MIN_RADIUS_DEG (0.3°) to avoid degenerate single-point clusters.

The global CLUSTER_RADIUS_DEG = 4.0° used during discovery is intentionally
never referenced here; it is a linkage radius, not a location-action radius.
"""

import argparse
import json
import math
import os
import sys

import numpy as np


# ── Parameters ────────────────────────────────────────────────────────────────

# Near-static eligibility — must match repeated-static audit gate exactly
NET_DISP_CEILING           = 1.5    # deg
MAJOR_MOTION_EXCLUSION_DEG = 42.0   # deg
MIN_OBS                    = 3
MIN_SPAN                   = 5
EXCLUDED_STATUSES          = {"rejected_static"}

# Match radius derivation
DEFAULT_GUARD_MARGIN_DEG   = 0.5    # deg added to p95
DEFAULT_RADIUS_CAP_DEG     = 6.0    # hard cap
MIN_RADIUS_DEG             = 0.3    # floor for degenerate clusters

# Sentinel: the discovery radius must never appear here
_DISCOVERY_RADIUS_DEG      = 4.0    # documented for audit; never used below


# ── Geometry ─────────────────────────────────────────────────────────────────

def _to_unit(yaw_deg, pitch_deg):
    y = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    return (
        math.cos(p) * math.sin(y),
        math.sin(p),
        math.cos(p) * math.cos(y),
    )


def _gc_deg(v1, v2):
    dot = float(sum(a * b for a, b in zip(v1, v2)))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


# ── Eligibility ───────────────────────────────────────────────────────────────

def _frames_of(tracklet):
    return sorted(tracklet.get("frames", []), key=lambda f: f["frame"])


def _net_disp(frames):
    if len(frames) < 2:
        return 0.0
    v0 = _to_unit(frames[0]["yaw"], frames[0]["pitch"])
    v1 = _to_unit(frames[-1]["yaw"], frames[-1]["pitch"])
    return _gc_deg(v0, v1)


def _median_pos(frames):
    yaws   = [f["yaw"]   for f in frames]
    pitches = [f["pitch"] for f in frames]
    return float(np.median(yaws)), float(np.median(pitches))


def _is_eligible(tracklet):
    """Return (eligible, net_disp, median_yaw, median_pitch)."""
    status = tracklet.get("status", "")
    if status in EXCLUDED_STATUSES:
        return False, 0.0, 0.0, 0.0
    frames = _frames_of(tracklet)
    obs  = len(frames)
    span = (frames[-1]["frame"] - frames[0]["frame"]) if obs >= 2 else 0
    if obs < MIN_OBS or span < MIN_SPAN:
        return False, 0.0, 0.0, 0.0
    net = _net_disp(frames)
    if net >= MAJOR_MOTION_EXCLUSION_DEG:
        return False, net, 0.0, 0.0
    if net >= NET_DISP_CEILING:
        return False, net, 0.0, 0.0
    med_y, med_p = _median_pos(frames)
    return True, net, med_y, med_p


# ── Per-cluster match radius derivation ──────────────────────────────────────

def _derive_match_radius(cluster, guard_margin, radius_cap):
    """
    Derive a per-cluster match radius from the member distance distribution.

    Uses p95 of member dist_to_centre values (reported in the cluster JSON)
    plus guard_margin, capped at radius_cap.

    Never touches _DISCOVERY_RADIUS_DEG.
    """
    members = cluster.get("members", [])
    if not members:
        return MIN_RADIUS_DEG, 0.0, 0.0

    dists = sorted(m["dist_to_centre_deg"] for m in members)
    n = len(dists)

    # p95 via linear interpolation
    idx = 0.95 * (n - 1)
    lo  = int(idx)
    hi  = min(lo + 1, n - 1)
    frac = idx - lo
    p95 = dists[lo] + frac * (dists[hi] - dists[lo])

    raw_radius = p95 + guard_margin
    final_radius = min(max(raw_radius, MIN_RADIUS_DEG), radius_cap)

    return final_radius, p95, raw_radius


# ── Matching ──────────────────────────────────────────────────────────────────

def _build_cluster_index(report_clusters, reviewed_ids, guard_margin, radius_cap):
    """
    Return list of cluster entry dicts for reviewed clusters only,
    each with a derived match radius.
    """
    index = []
    for c in report_clusters:
        if c["cluster_id"] not in reviewed_ids:
            continue
        radius, p95, raw = _derive_match_radius(c, guard_margin, radius_cap)
        index.append({
            "cluster_id":        c["cluster_id"],
            "centre_yaw_deg":    c["centre_yaw_deg"],
            "centre_pitch_deg":  c["centre_pitch_deg"],
            "centre_unit":       _to_unit(c["centre_yaw_deg"], c["centre_pitch_deg"]),
            "match_radius_deg":  radius,
            "p95_member_dist_deg": p95,
            "raw_radius_deg":    raw,
            "member_count":      c["member_count"],
        })
    return index


def _find_match(med_y, med_p, cluster_index):
    """
    Return (cluster_id, distance_deg, match_radius_deg) for the closest
    cluster whose match radius contains (med_y, med_p), or (None, None, None).
    """
    uv = _to_unit(med_y, med_p)
    best = None
    best_dist = float("inf")
    for entry in cluster_index:
        dist = _gc_deg(uv, entry["centre_unit"])
        if dist <= entry["match_radius_deg"] and dist < best_dist:
            best_dist = dist
            best = entry
    if best is None:
        return None, None, None
    return best["cluster_id"], best_dist, best["match_radius_deg"]


# ── Main ──────────────────────────────────────────────────────────────────────

def run(args):
    reviewed_ids = set(x.strip() for x in args.reviewed_ids.split(","))
    guard_margin = args.guard_margin
    radius_cap   = args.radius_cap

    with open(args.tracklets) as f:
        raw = json.load(f)
    tracklets = raw if isinstance(raw, list) else raw.get("tracklets", [])
    total = len(tracklets)

    with open(args.report) as f:
        report_data = json.load(f)
    report_clusters = report_data["clusters"]

    cluster_index = _build_cluster_index(report_clusters, reviewed_ids,
                                          guard_margin, radius_cap)

    # Annotate tracklets — write to new list; original data untouched in memory
    annotated = []
    eligible_count   = 0
    match_count      = 0
    cluster_matches  = {e["cluster_id"]: [] for e in cluster_index}

    for t in tracklets:
        t_out = dict(t)  # shallow copy; frames list is shared but not mutated

        ok, net, med_y, med_p = _is_eligible(t)
        if not ok:
            # Not eligible — no annotation fields added
            annotated.append(t_out)
            continue

        eligible_count += 1
        cid, dist, radius = _find_match(med_y, med_p, cluster_index)
        matched = cid is not None

        t_out["repeated_static_location_match"]     = matched
        t_out["repeated_static_cluster_id"]         = cid
        t_out["repeated_static_match_distance_deg"] = round(dist, 4)   if dist   is not None else None
        t_out["repeated_static_match_radius_deg"]   = round(radius, 4) if radius is not None else None
        t_out["would_suppress_repeated_static"]     = matched

        if matched:
            match_count += 1
            cluster_matches[cid].append(t["id"])

        annotated.append(t_out)

    # Output structure mirrors tracklets.json wrapper if present
    if isinstance(raw, dict):
        output_tracklets = dict(raw)
        output_tracklets["tracklets"] = annotated
    else:
        output_tracklets = annotated

    os.makedirs(args.output_dir, exist_ok=True)

    # tracklets_repeated_static_audit.json
    audit_path = os.path.join(args.output_dir, "tracklets_repeated_static_audit.json")
    with open(audit_path, "w") as f:
        json.dump(output_tracklets, f, indent=2)

    # Per-cluster report
    cluster_report = []
    for entry in cluster_index:
        cid = entry["cluster_id"]
        cluster_report.append({
            "cluster_id":              cid,
            "centre_yaw_deg":          entry["centre_yaw_deg"],
            "centre_pitch_deg":        entry["centre_pitch_deg"],
            "p95_member_dist_deg":     round(entry["p95_member_dist_deg"], 4),
            "raw_radius_before_cap_deg": round(entry["raw_radius_deg"], 4),
            "match_radius_deg":        round(entry["match_radius_deg"], 4),
            "guard_margin_deg":        guard_margin,
            "radius_cap_deg":          radius_cap,
            "member_count":            entry["member_count"],
            "matched_tracklet_count":  len(cluster_matches.get(cid, [])),
            "matched_tracklet_ids":    sorted(cluster_matches.get(cid, [])),
        })

    summary = {
        "meta": {
            "tracklets_path":          args.tracklets,
            "report_path":             args.report,
            "reviewed_cluster_ids":    sorted(reviewed_ids),
            "total_tracklet_count":    total,
            "eligible_tracklet_count": eligible_count,
            "matched_tracklet_count":  match_count,
            "guard_margin_deg":        guard_margin,
            "radius_cap_deg":          radius_cap,
            "note": (
                "Match radius derived from p95 member dist + guard_margin, "
                "capped at radius_cap. Discovery radius (4.0°) is intentionally "
                "not used as an action radius."
            ),
        },
        "clusters": cluster_report,
    }

    report_json_path = os.path.join(args.output_dir, "stage2_discovered_static_report.json")
    with open(report_json_path, "w") as f:
        json.dump(summary, f, indent=2)

    # Text report
    txt_path = os.path.join(args.output_dir, "stage2_discovered_static_report.txt")
    with open(txt_path, "w") as f:
        _write_text_report(f, summary, cluster_report)

    print(f"[discovered-static-match] total={total}  eligible={eligible_count}"
          f"  matched={match_count}  clusters={len(cluster_index)}", flush=True)
    print(f"[discovered-static-match] audit   -> {audit_path}", flush=True)
    print(f"[discovered-static-match] report  -> {report_json_path}", flush=True)
    print(f"[discovered-static-match] text    -> {txt_path}", flush=True)

    return summary


def _write_text_report(f, summary, cluster_report):
    m = summary["meta"]
    f.write("=" * 70 + "\n")
    f.write("STAGE 2 DISCOVERED-STATIC LOCATION MATCH — ANNOTATION REPORT\n")
    f.write("=" * 70 + "\n\n")
    f.write(f"Tracklets input      : {m['tracklets_path']}\n")
    f.write(f"Cluster report input : {m['report_path']}\n")
    f.write(f"Reviewed cluster IDs : {', '.join(m['reviewed_cluster_ids'])}\n")
    f.write(f"Total tracklets      : {m['total_tracklet_count']}\n")
    f.write(f"Eligible (near-static) : {m['eligible_tracklet_count']}\n")
    f.write(f"Matched (would_suppress) : {m['matched_tracklet_count']}\n\n")
    f.write(f"Guard margin         : {m['guard_margin_deg']}°\n")
    f.write(f"Radius cap           : {m['radius_cap_deg']}°\n")
    f.write(f"Note: {m['note']}\n\n")
    f.write("-" * 70 + "\n")
    f.write("PER-CLUSTER MATCH SUMMARY\n")
    f.write("-" * 70 + "\n")
    for c in cluster_report:
        f.write(f"\n{c['cluster_id']}"
                f"  centre=(yaw={c['centre_yaw_deg']:.2f}°, pitch={c['centre_pitch_deg']:.2f}°)\n")
        f.write(f"  p95_member_dist={c['p95_member_dist_deg']:.3f}°"
                f"  +guard={c['guard_margin_deg']}°"
                f"  raw={c['raw_radius_before_cap_deg']:.3f}°"
                f"  match_radius={c['match_radius_deg']:.3f}°\n")
        f.write(f"  matched={c['matched_tracklet_count']}  "
                f"IDs: {', '.join(c['matched_tracklet_ids']) or '—'}\n")
    f.write("\n" + "-" * 70 + "\n")
    f.write("BEFORE / AFTER (near-static eligible tracklets)\n")
    f.write("-" * 70 + "\n")
    f.write(f"  Eligible near-static tracklets : {m['eligible_tracklet_count']}\n")
    f.write(f"  Would suppress (matched)       : {m['matched_tracklet_count']}\n")
    remaining = m['eligible_tracklet_count'] - m['matched_tracklet_count']
    f.write(f"  Remaining unmatched            : {remaining}\n")
    f.write("\nANNOTATION ONLY — no tracklet status altered.\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Stage 2 discovered-static location match (annotation-only)"
    )
    parser.add_argument("--tracklets",     required=True)
    parser.add_argument("--report",        required=True,
                        help="stage2_repeated_static_report.json")
    parser.add_argument("--reviewed-ids",  default="C001,C002,C003,C004,C005,C006,C007,C008,C009",
                        dest="reviewed_ids")
    parser.add_argument("--guard-margin",  type=float, default=DEFAULT_GUARD_MARGIN_DEG,
                        dest="guard_margin")
    parser.add_argument("--radius-cap",    type=float, default=DEFAULT_RADIUS_CAP_DEG,
                        dest="radius_cap")
    parser.add_argument("--output-dir",    default=".", dest="output_dir")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
