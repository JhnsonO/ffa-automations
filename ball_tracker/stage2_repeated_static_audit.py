#!/usr/bin/env python3
"""
FFA Offline Recovery Pipeline — Stage 2: Repeated-Static Location Audit
========================================================================
Annotation-only layer.  Reads tracklets.json (and optionally the static-
motion audit report) and identifies angular locations where multiple
independently-formed, near-static tracklets recur across clearly separated
time windows — a strong signal for a fixed false-positive scene feature.

Does NOT alter tracklet status, Stage 2 link thresholds, Stage 1 output,
the renderer, or any frozen module.

Inputs
------
  --tracklets   : stage2 tracklets.json
  --audit       : (optional) stage2_audit_report.json from stage2_static_motion_audit.py
                  When supplied, near-static eligibility is derived from the
                  audit's would_reject_static_motion or borderline flags.
                  When absent, a local net-displacement filter is applied.
  --output-dir  : directory for outputs (default: current dir)

Outputs
-------
  stage2_repeated_static_report.json   — machine-readable cluster report
  stage2_repeated_static_report.txt    — human-readable summary
  stage2_repeated_static_review/       — per-cluster text cards (top clusters)

Cluster eligibility
-------------------
A tracklet is included in clustering when ALL hold:
  1. net_disp_deg < NET_DISP_CEILING   (1.5° — near-static filter)
  2. status NOT in EXCLUDED_STATUSES   (rejected_static always excluded)
  3. obs_count   >= MIN_OBS            (3 — floor for meaningful position)
  4. span_frames >= MIN_SPAN           (5 — floor to avoid single-frame noise)

  Additionally, MAJOR_MOTION_EXCLUSION_DEG (42°) guards major-motion tracklets
  such as T0373.  Any tracklet with net_disp_deg >= this value is excluded
  regardless of the near-static ceiling.

Cluster formation
-----------------
Single-linkage angular clustering on great-circle distance between tracklet
median positions.  Two tracklets are in the same cluster when their median
positions are within CLUSTER_RADIUS_DEG (4.0°).

Recurrence criterion
--------------------
A cluster is flagged as a REPEATED-STATIC LOCATION when:
  - member count >= MIN_CLUSTER_MEMBERS (3)
  - temporal span of member frame windows >= MIN_TEMPORAL_SEPARATION_FRAMES (150)
  - at least 2 distinct time windows exist, separated by >= MIN_WINDOW_GAP_FRAMES (50)

  A time window is defined as the [first_frame, last_frame] interval of a member
  tracklet.  Distinct windows are those whose mid-points are >= MIN_WINDOW_GAP_FRAMES
  apart (after sorting by mid-point).

Output schema (JSON)
--------------------
{
  "meta": {
    "tracklets_path": str,
    "audit_path": str | null,
    "eligible_tracklet_count": int,
    "total_tracklet_count": int,
    "cluster_count": int,
    "repeated_static_cluster_count": int,
    "parameters": { ... }
  },
  "clusters": [
    {
      "cluster_id": str,           // "C001", "C002", ...
      "centre_yaw_deg": float,
      "centre_pitch_deg": float,
      "cluster_radius_deg": float, // max member-to-centre distance
      "member_count": int,
      "member_ids": [str, ...],
      "is_repeated_static": bool,
      "distinct_window_count": int,
      "distinct_windows": [
        {"representative_id": str, "first_frame": int, "last_frame": int, "mid_frame": int}
      ],
      "overall_first_frame": int,
      "overall_last_frame": int,
      "overall_temporal_span_frames": int,
      "total_obs_count": int,
      "members": [
        {
          "id": str,
          "status": str,
          "obs_count": int,
          "span_frames": int,
          "net_disp_deg": float,
          "first_frame": int,
          "last_frame": int,
          "median_yaw_deg": float,
          "median_pitch_deg": float,
          "dist_to_centre_deg": float
        },
        ...
      ]
    },
    ...
  ]
}
"""

import argparse
import json
import math
import os
import sys

import numpy as np


# ── Parameters ────────────────────────────────────────────────────────────────

# Near-static eligibility
NET_DISP_CEILING           = 1.5    # deg — near-static ceiling (same as motion-audit gate)
MAJOR_MOTION_EXCLUSION_DEG = 42.0   # deg — hard exclusion for clearly moving tracklets
MIN_OBS                    = 3
MIN_SPAN                   = 5
EXCLUDED_STATUSES          = {"rejected_static"}

# Clustering
CLUSTER_RADIUS_DEG         = 4.0   # deg — single-linkage merge radius

# Recurrence
MIN_CLUSTER_MEMBERS              = 3
MIN_TEMPORAL_SEPARATION_FRAMES   = 150  # total span across all member windows
MIN_WINDOW_GAP_FRAMES            = 50   # gap between distinct time-window mid-points

# Review pack
TOP_CLUSTER_COUNT                = 5    # emit text cards for top N clusters


# ── Geometry helpers ──────────────────────────────────────────────────────────

def _to_unit(yaw_deg, pitch_deg):
    y = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    return np.array([
        math.cos(p) * math.sin(y),
        math.sin(p),
        math.cos(p) * math.cos(y),
    ])


def _gc_deg(v1, v2):
    dot = float(np.clip(np.dot(v1, v2), -1.0, 1.0))
    return math.degrees(math.acos(dot))


def _median_position(frames):
    """Return (median_yaw_deg, median_pitch_deg) from a list of frame dicts."""
    yaws   = [f["yaw"]   for f in frames]
    pitchs = [f["pitch"] for f in frames]
    return float(np.median(yaws)), float(np.median(pitchs))


def _net_disp(frames):
    """Great-circle displacement between first and last frame positions (deg)."""
    if len(frames) < 2:
        return 0.0
    first = frames[0]
    last  = frames[-1]
    v0 = _to_unit(first["yaw"], first["pitch"])
    v1 = _to_unit(last["yaw"],  last["pitch"])
    return _gc_deg(v0, v1)


# ── Eligibility ───────────────────────────────────────────────────────────────

def _is_eligible(tracklet, audit_map):
    """
    Return (eligible: bool, net_disp_deg: float, median_yaw: float, median_pitch: float).

    When audit_map is supplied, near-static is inferred from
    would_reject_static_motion OR is_borderline flags; the net-disp ceiling
    is still applied as a hard guard.

    When audit_map is absent, net_disp_deg < NET_DISP_CEILING is the sole
    near-static criterion (plus obs/span/status floors).
    """
    tid    = tracklet["id"]
    status = tracklet.get("status", "")
    frames = sorted(tracklet.get("frames", []), key=lambda f: f["frame"])
    obs    = len(frames)
    span   = (frames[-1]["frame"] - frames[0]["frame"]) if obs >= 2 else 0

    if status in EXCLUDED_STATUSES:
        return False, 0.0, 0.0, 0.0
    if obs < MIN_OBS:
        return False, 0.0, 0.0, 0.0
    if span < MIN_SPAN:
        return False, 0.0, 0.0, 0.0

    net = _net_disp(frames)

    # Always exclude major-motion tracklets (e.g. T0373)
    if net >= MAJOR_MOTION_EXCLUSION_DEG:
        return False, net, 0.0, 0.0

    med_yaw, med_pitch = _median_position(frames)

    # Near-static check
    if audit_map and tid in audit_map:
        a = audit_map[tid].get("_audit", {})
        near_static = a.get("would_reject_static_motion", False) or a.get("is_borderline", False)
        # Also include via net_disp ceiling even if audit doesn't flag it
        near_static = near_static or (net < NET_DISP_CEILING)
    else:
        near_static = (net < NET_DISP_CEILING)

    if not near_static:
        return False, net, med_yaw, med_pitch

    return True, net, med_yaw, med_pitch


# ── Clustering (single-linkage on great-circle distance) ─────────────────────

def _cluster_tracklets(eligible):
    """
    eligible: list of dicts — each has keys:
      id, status, obs_count, span_frames, net_disp_deg,
      first_frame, last_frame, median_yaw_deg, median_pitch_deg, unit_vec

    Returns list of lists (member dicts), one per cluster.
    """
    if not eligible:
        return []

    # Build adjacency: i <-> j if gc_deg < CLUSTER_RADIUS_DEG
    n = len(eligible)
    vecs = [e["unit_vec"] for e in eligible]

    # Union-Find
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if _gc_deg(vecs[i], vecs[j]) < CLUSTER_RADIUS_DEG:
                union(i, j)

    # Group
    from collections import defaultdict
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(eligible[i])

    return list(groups.values())


# ── Cluster summary ───────────────────────────────────────────────────────────

def _summarise_cluster(members, cluster_id):
    """Produce the cluster dict for the JSON report."""
    # Compute weighted-average unit vector (by obs_count) → centre
    vecs    = np.array([m["unit_vec"] for m in members])
    weights = np.array([m["obs_count"] for m in members], dtype=float)
    weights /= weights.sum()
    centre_vec = vecs.T @ weights
    norm = np.linalg.norm(centre_vec)
    if norm > 1e-9:
        centre_vec /= norm
    else:
        centre_vec = vecs[0]

    # Convert unit vec → yaw/pitch
    cx, cy, cz = centre_vec
    centre_pitch = math.degrees(math.asin(float(np.clip(cy, -1, 1))))
    centre_yaw   = math.degrees(math.atan2(float(cx), float(cz)))

    # Radius: max member-to-centre distance
    radius = max(_gc_deg(m["unit_vec"], centre_vec) for m in members)

    # Temporal windows
    sorted_by_mid = sorted(members, key=lambda m: (m["first_frame"] + m["last_frame"]) / 2)
    distinct_windows = []
    for m in sorted_by_mid:
        mid = (m["first_frame"] + m["last_frame"]) // 2
        if not distinct_windows:
            distinct_windows.append({
                "representative_id": m["id"],
                "first_frame": m["first_frame"],
                "last_frame":  m["last_frame"],
                "mid_frame":   mid,
            })
        else:
            last_mid = distinct_windows[-1]["mid_frame"]
            if mid - last_mid >= MIN_WINDOW_GAP_FRAMES:
                distinct_windows.append({
                    "representative_id": m["id"],
                    "first_frame": m["first_frame"],
                    "last_frame":  m["last_frame"],
                    "mid_frame":   mid,
                })

    overall_first = min(m["first_frame"] for m in members)
    overall_last  = max(m["last_frame"]  for m in members)
    temporal_span = overall_last - overall_first
    total_obs     = sum(m["obs_count"] for m in members)

    is_repeated = (
        len(members)         >= MIN_CLUSTER_MEMBERS
        and temporal_span    >= MIN_TEMPORAL_SEPARATION_FRAMES
        and len(distinct_windows) >= 2
    )

    member_records = sorted([
        {
            "id":               m["id"],
            "status":           m["status"],
            "obs_count":        m["obs_count"],
            "span_frames":      m["span_frames"],
            "net_disp_deg":     round(m["net_disp_deg"], 4),
            "first_frame":      m["first_frame"],
            "last_frame":       m["last_frame"],
            "median_yaw_deg":   round(m["median_yaw_deg"], 3),
            "median_pitch_deg": round(m["median_pitch_deg"], 3),
            "dist_to_centre_deg": round(_gc_deg(m["unit_vec"], centre_vec), 3),
        }
        for m in members
    ], key=lambda x: x["id"])

    return {
        "cluster_id":                    cluster_id,
        "centre_yaw_deg":                round(centre_yaw, 3),
        "centre_pitch_deg":              round(centre_pitch, 3),
        "cluster_radius_deg":            round(radius, 3),
        "member_count":                  len(members),
        "member_ids":                    [m["id"] for m in sorted(members, key=lambda x: x["id"])],
        "is_repeated_static":            is_repeated,
        "distinct_window_count":         len(distinct_windows),
        "distinct_windows":              distinct_windows,
        "overall_first_frame":           overall_first,
        "overall_last_frame":            overall_last,
        "overall_temporal_span_frames":  temporal_span,
        "total_obs_count":               total_obs,
        "members":                       member_records,
    }


# ── Distinct-window extraction (public, for tests) ────────────────────────────

def compute_distinct_windows(member_list):
    """
    Given a list of member dicts (each with first_frame, last_frame, id),
    return a list of distinct time windows separated by >= MIN_WINDOW_GAP_FRAMES.
    Public so tests can call it directly.
    """
    sorted_m = sorted(member_list, key=lambda m: (m["first_frame"] + m["last_frame"]) / 2)
    windows = []
    for m in sorted_m:
        mid = (m["first_frame"] + m["last_frame"]) // 2
        if not windows or mid - windows[-1]["mid_frame"] >= MIN_WINDOW_GAP_FRAMES:
            windows.append({
                "representative_id": m["id"],
                "first_frame": m["first_frame"],
                "last_frame":  m["last_frame"],
                "mid_frame":   mid,
            })
    return windows


# ── Main run ──────────────────────────────────────────────────────────────────

def run(args):
    with open(args.tracklets) as f:
        data = json.load(f)

    tracklets = data if isinstance(data, list) else data.get("tracklets", [])

    # Optional audit map (tid → audited tracklet dict)
    audit_map = {}
    if getattr(args, "audit", None) and os.path.exists(args.audit):
        with open(args.audit) as f:
            audit_data = json.load(f)
        for at in audit_data.get("tracklets", []):
            audit_map[at["id"]] = at

    # Build eligible list
    eligible = []
    for t in tracklets:
        ok, net, med_yaw, med_pitch = _is_eligible(t, audit_map)
        if not ok:
            continue
        frames = sorted(t.get("frames", []), key=lambda f: f["frame"])
        eligible.append({
            "id":               t["id"],
            "status":           t.get("status", ""),
            "obs_count":        len(frames),
            "span_frames":      (frames[-1]["frame"] - frames[0]["frame"]) if len(frames) >= 2 else 0,
            "net_disp_deg":     net,
            "first_frame":      frames[0]["frame"]  if frames else 0,
            "last_frame":       frames[-1]["frame"] if frames else 0,
            "median_yaw_deg":   med_yaw,
            "median_pitch_deg": med_pitch,
            "unit_vec":         _to_unit(med_yaw, med_pitch),
        })

    # Cluster
    raw_clusters = _cluster_tracklets(eligible)

    # Summarise and sort by member count descending, then temporal span
    clusters = []
    for i, members in enumerate(
        sorted(raw_clusters, key=lambda g: (-len(g), -(max(m["last_frame"] for m in g) - min(m["first_frame"] for m in g))))
    ):
        cid = f"C{i+1:03d}"
        clusters.append(_summarise_cluster(members, cid))

    repeated = [c for c in clusters if c["is_repeated_static"]]

    # JSON report
    report = {
        "meta": {
            "tracklets_path":               args.tracklets,
            "audit_path":                   getattr(args, "audit", None),
            "eligible_tracklet_count":      len(eligible),
            "total_tracklet_count":         len(tracklets),
            "cluster_count":                len(clusters),
            "repeated_static_cluster_count": len(repeated),
            "parameters": {
                "net_disp_ceiling_deg":             NET_DISP_CEILING,
                "major_motion_exclusion_deg":       MAJOR_MOTION_EXCLUSION_DEG,
                "min_obs":                          MIN_OBS,
                "min_span_frames":                  MIN_SPAN,
                "cluster_radius_deg":               CLUSTER_RADIUS_DEG,
                "min_cluster_members":              MIN_CLUSTER_MEMBERS,
                "min_temporal_separation_frames":   MIN_TEMPORAL_SEPARATION_FRAMES,
                "min_window_gap_frames":            MIN_WINDOW_GAP_FRAMES,
            },
        },
        "clusters": clusters,
    }

    os.makedirs(args.output_dir, exist_ok=True)
    json_path = os.path.join(args.output_dir, "stage2_repeated_static_report.json")
    with open(json_path, "w") as f:
        json.dump(report, f, indent=2)

    # Text report
    txt_path = os.path.join(args.output_dir, "stage2_repeated_static_report.txt")
    with open(txt_path, "w") as f:
        _write_text_report(f, report, clusters, repeated)

    # Per-cluster text cards for top N repeated clusters
    review_dir = os.path.join(args.output_dir, "stage2_repeated_static_review")
    os.makedirs(review_dir, exist_ok=True)
    top = [c for c in clusters if c["is_repeated_static"]][:TOP_CLUSTER_COUNT]
    for c in top:
        card_path = os.path.join(review_dir, f"{c['cluster_id']}_card.txt")
        with open(card_path, "w") as f:
            _write_cluster_card(f, c)

    print(f"[repeated-static-audit] eligible={len(eligible)}/{len(tracklets)}"
          f"  clusters={len(clusters)}  repeated-static={len(repeated)}")
    print(f"[repeated-static-audit] JSON  -> {json_path}")
    print(f"[repeated-static-audit] text  -> {txt_path}")
    if top:
        print(f"[repeated-static-audit] cards -> {review_dir}/ ({len(top)} clusters)")

    return report


# ── Text writers ──────────────────────────────────────────────────────────────

def _write_text_report(f, report, clusters, repeated):
    m = report["meta"]
    f.write("=" * 70 + "\n")
    f.write("STAGE 2 REPEATED-STATIC LOCATION AUDIT\n")
    f.write("=" * 70 + "\n\n")
    f.write(f"Tracklets input  : {m['tracklets_path']}\n")
    if m["audit_path"]:
        f.write(f"Audit input      : {m['audit_path']}\n")
    f.write(f"Total tracklets  : {m['total_tracklet_count']}\n")
    f.write(f"Eligible (near-static) : {m['eligible_tracklet_count']}\n")
    f.write(f"Clusters formed  : {m['cluster_count']}\n")
    f.write(f"Repeated-static clusters : {m['repeated_static_cluster_count']}\n\n")

    p = m["parameters"]
    f.write("Parameters\n")
    f.write(f"  net_disp_ceiling        : {p['net_disp_ceiling_deg']}°\n")
    f.write(f"  major_motion_exclusion  : {p['major_motion_exclusion_deg']}°\n")
    f.write(f"  cluster_radius          : {p['cluster_radius_deg']}°\n")
    f.write(f"  min_cluster_members     : {p['min_cluster_members']}\n")
    f.write(f"  min_temporal_separation : {p['min_temporal_separation_frames']} frames\n")
    f.write(f"  min_window_gap          : {p['min_window_gap_frames']} frames\n\n")

    f.write("-" * 70 + "\n")
    f.write("REPEATED-STATIC LOCATIONS\n")
    f.write("-" * 70 + "\n")
    if not repeated:
        f.write("  None found.\n\n")
    else:
        for c in repeated:
            f.write(f"\n{c['cluster_id']}  centre=(yaw={c['centre_yaw_deg']:.2f}°, "
                    f"pitch={c['centre_pitch_deg']:.2f}°)  "
                    f"radius={c['cluster_radius_deg']:.2f}°\n")
            f.write(f"  members={c['member_count']}  "
                    f"distinct_windows={c['distinct_window_count']}  "
                    f"frames {c['overall_first_frame']}–{c['overall_last_frame']}  "
                    f"span={c['overall_temporal_span_frames']}  "
                    f"total_obs={c['total_obs_count']}\n")
            f.write(f"  IDs: {', '.join(c['member_ids'])}\n")
            for w in c["distinct_windows"]:
                f.write(f"    window [{w['first_frame']}–{w['last_frame']}] "
                        f"rep={w['representative_id']}\n")

    f.write("\n" + "-" * 70 + "\n")
    f.write("ALL CLUSTERS (sorted by member count)\n")
    f.write("-" * 70 + "\n")
    for c in clusters:
        flag = "*** REPEATED-STATIC ***" if c["is_repeated_static"] else ""
        f.write(f"\n{c['cluster_id']}  {flag}\n")
        f.write(f"  centre=(yaw={c['centre_yaw_deg']:.2f}°, pitch={c['centre_pitch_deg']:.2f}°)  "
                f"radius={c['cluster_radius_deg']:.2f}°\n")
        f.write(f"  members={c['member_count']}  windows={c['distinct_window_count']}  "
                f"frames {c['overall_first_frame']}–{c['overall_last_frame']}  "
                f"span={c['overall_temporal_span_frames']}\n")
        f.write(f"  IDs: {', '.join(c['member_ids'])}\n")


def _write_cluster_card(f, c):
    f.write("=" * 70 + "\n")
    f.write(f"CLUSTER CARD: {c['cluster_id']}\n")
    f.write("=" * 70 + "\n\n")
    f.write(f"Centre        : yaw={c['centre_yaw_deg']:.3f}°  pitch={c['centre_pitch_deg']:.3f}°\n")
    f.write(f"Radius        : {c['cluster_radius_deg']:.3f}°\n")
    f.write(f"Members       : {c['member_count']}\n")
    f.write(f"Distinct windows : {c['distinct_window_count']}\n")
    f.write(f"Frames        : {c['overall_first_frame']} – {c['overall_last_frame']}  "
            f"(span={c['overall_temporal_span_frames']})\n")
    f.write(f"Total obs     : {c['total_obs_count']}\n")
    f.write(f"REPEATED-STATIC : {'YES' if c['is_repeated_static'] else 'NO'}\n\n")

    f.write("Distinct time windows:\n")
    for i, w in enumerate(c["distinct_windows"], 1):
        f.write(f"  Window {i}: frames {w['first_frame']}–{w['last_frame']}  "
                f"rep={w['representative_id']}\n")

    f.write("\nMembers:\n")
    for m in c["members"]:
        f.write(f"  {m['id']}  status={m['status']}  obs={m['obs_count']}  "
                f"span={m['span_frames']}  net={m['net_disp_deg']:.3f}°  "
                f"frames={m['first_frame']}–{m['last_frame']}  "
                f"dist_to_centre={m['dist_to_centre_deg']:.3f}°\n")

    f.write("\nREVIEW NOTE: Verify in footage whether the location above\n"
            "corresponds to a fixed scene feature (advertising board, fence post,\n"
            "line marking, etc.).  If confirmed, this cluster is a systematic\n"
            "false-positive source.\n")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Stage 2 repeated-static location audit (annotation-only)"
    )
    parser.add_argument("--tracklets",   required=True, help="stage2 tracklets.json")
    parser.add_argument("--audit",       default=None,  help="stage2_audit_report.json (optional)")
    parser.add_argument("--output-dir",  default=".",   dest="output_dir")
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
