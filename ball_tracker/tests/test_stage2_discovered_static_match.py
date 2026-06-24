#!/usr/bin/env python3
"""
Fixture tests for stage2_discovered_static_match.py

Coverage:
  1.  A near-static tracklet whose median falls inside a reviewed cluster's
      match radius is annotated with repeated_static_location_match=True
      and would_suppress_repeated_static=True.
  2.  A moving tracklet (net_disp >= MAJOR_MOTION_EXCLUSION_DEG) passing
      through the same area receives no annotation fields.
  3.  A tracklet with net_disp >= NET_DISP_CEILING but < MAJOR_MOTION is
      ineligible; no annotation fields added.
  4.  A rejected_static tracklet is ineligible regardless of position.
  5.  A tracklet below MIN_OBS is ineligible.
  6.  A tracklet below MIN_SPAN is ineligible.
  7.  A near-static tracklet whose median is outside all cluster radii is
      annotated with repeated_static_location_match=False.
  8.  Global 4° discovery radius is never used as the match radius:
      derived radius must differ from 4.0° for clusters with tight members.
  9.  Match radius derivation respects the radius cap (DEFAULT_RADIUS_CAP_DEG).
 10.  Match radius derivation respects the guard margin.
 11.  Original tracklets.json dict is never modified in place.
 12.  tracklets_repeated_static_audit.json contains all original tracklets
      (eligible and ineligible), not a filtered subset.
 13.  Only reviewed cluster IDs are used; a cluster not in reviewed_ids
      does not match even when the median falls within its discovery radius.

Run:
  python3 -m pytest ball_tracker/tests/test_stage2_discovered_static_match.py -v
  python3 ball_tracker/tests/test_stage2_discovered_static_match.py
"""

import argparse
import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stage2_discovered_static_match as dsm


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _make_tracklet(tid, yaw, pitch, first_frame, last_frame,
                   obs=None, status="anchor", net_disp_deg=0.0):
    """Build a minimal tracklet dict with evenly-spaced observations."""
    if obs is None:
        obs = max(3, last_frame - first_frame + 1)
    frames_out = []
    step = max(1, (last_frame - first_frame) // max(1, obs - 1))
    for i in range(obs):
        fn = first_frame + i * step
        fy = yaw + net_disp_deg * (i / max(1, obs - 1))
        frames_out.append({"frame": fn, "yaw": fy, "pitch": pitch, "weighted_conf": 0.5})
    return {"id": tid, "status": status, "frames": frames_out}


def _make_cluster(cid, yaw, pitch, member_dists):
    """
    Build a minimal cluster dict matching the repeated-static report schema.
    member_dists: list of dist_to_centre_deg values for the members.
    """
    members = [
        {"id": f"{cid}_M{i:02d}", "dist_to_centre_deg": d,
         "status": "anchor", "obs_count": 10, "span_frames": 100,
         "net_disp_deg": 0.1, "first_frame": 100, "last_frame": 200,
         "median_yaw_deg": yaw, "median_pitch_deg": pitch}
        for i, d in enumerate(member_dists)
    ]
    return {
        "cluster_id":                  cid,
        "centre_yaw_deg":              yaw,
        "centre_pitch_deg":            pitch,
        "cluster_radius_deg":          max(member_dists) if member_dists else 0.0,
        "member_count":                len(members),
        "member_ids":                  [m["id"] for m in members],
        "is_repeated_static":          True,
        "distinct_window_count":       3,
        "distinct_windows":            [],
        "overall_first_frame":         100,
        "overall_last_frame":          3000,
        "overall_temporal_span_frames": 2900,
        "total_obs_count":             len(members) * 10,
        "members":                     members,
    }


def _run(tracklets_list, clusters_list, reviewed_ids="C001",
         guard_margin=0.5, radius_cap=6.0):
    """Write inputs to temp files, run dsm.run(), return (summary, audit_tracklets)."""
    with tempfile.TemporaryDirectory() as td:
        t_path = os.path.join(td, "tracklets.json")
        r_path = os.path.join(td, "report.json")
        out_dir = os.path.join(td, "out")
        os.makedirs(out_dir)

        with open(t_path, "w") as f:
            json.dump({"tracklets": tracklets_list}, f)

        report = {
            "meta": {"parameters": {}},
            "clusters": clusters_list,
        }
        with open(r_path, "w") as f:
            json.dump(report, f)

        args = argparse.Namespace(
            tracklets=t_path,
            report=r_path,
            reviewed_ids=reviewed_ids,
            guard_margin=guard_margin,
            radius_cap=radius_cap,
            output_dir=out_dir,
        )
        summary = dsm.run(args)

        audit_path = os.path.join(out_dir, "tracklets_repeated_static_audit.json")
        with open(audit_path) as f:
            audit_data = json.load(f)

        audit_tracklets = (
            audit_data["tracklets"]
            if isinstance(audit_data, dict)
            else audit_data
        )
        return summary, audit_tracklets


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestDiscoveredStaticMatch(unittest.TestCase):

    # 1. Near-static tracklet inside cluster radius → annotated as match
    def test_match_near_static_inside_radius(self):
        t = _make_tracklet("T0001", yaw=24.0, pitch=13.0,
                           first_frame=100, last_frame=500, obs=20)
        # cluster at (24.5, 13.2); members all within ~0.8°
        c = _make_cluster("C001", yaw=24.5, pitch=13.2,
                          member_dists=[0.3, 0.5, 0.6, 0.7, 0.8])
        summary, audited = _run([t], [c], reviewed_ids="C001")

        rec = next(r for r in audited if r["id"] == "T0001")
        self.assertTrue(rec["repeated_static_location_match"])
        self.assertEqual(rec["repeated_static_cluster_id"], "C001")
        self.assertIsNotNone(rec["repeated_static_match_distance_deg"])
        self.assertIsNotNone(rec["repeated_static_match_radius_deg"])
        self.assertTrue(rec["would_suppress_repeated_static"])

    # 2. Moving tracklet (net_disp >= MAJOR_MOTION_EXCLUSION_DEG) → no annotation
    def test_no_match_major_motion_tracklet(self):
        # net_disp ~45° — well above exclusion threshold
        t = _make_tracklet("T0002", yaw=24.0, pitch=13.0,
                           first_frame=100, last_frame=500, obs=20,
                           net_disp_deg=45.0)
        c = _make_cluster("C001", yaw=24.5, pitch=13.2,
                          member_dists=[0.3, 0.5, 0.7])
        _, audited = _run([t], [c], reviewed_ids="C001")

        rec = next(r for r in audited if r["id"] == "T0002")
        self.assertNotIn("repeated_static_location_match", rec)
        self.assertNotIn("would_suppress_repeated_static", rec)

    # 3. Moderately moving tracklet (>= NET_DISP_CEILING, < MAJOR_MOTION) → no annotation
    def test_no_match_moderate_motion(self):
        t = _make_tracklet("T0003", yaw=24.0, pitch=13.0,
                           first_frame=100, last_frame=500, obs=20,
                           net_disp_deg=2.0)  # > 1.5° ceiling
        c = _make_cluster("C001", yaw=24.5, pitch=13.2,
                          member_dists=[0.3, 0.5, 0.7])
        _, audited = _run([t], [c], reviewed_ids="C001")

        rec = next(r for r in audited if r["id"] == "T0003")
        self.assertNotIn("repeated_static_location_match", rec)

    # 4. rejected_static tracklet → no annotation
    def test_no_match_rejected_static(self):
        t = _make_tracklet("T0004", yaw=24.0, pitch=13.0,
                           first_frame=100, last_frame=500, obs=20,
                           status="rejected_static")
        c = _make_cluster("C001", yaw=24.5, pitch=13.2,
                          member_dists=[0.3, 0.5, 0.7])
        _, audited = _run([t], [c], reviewed_ids="C001")

        rec = next(r for r in audited if r["id"] == "T0004")
        self.assertNotIn("repeated_static_location_match", rec)

    # 5. Tracklet below MIN_OBS → no annotation
    def test_no_match_below_min_obs(self):
        t = _make_tracklet("T0005", yaw=24.0, pitch=13.0,
                           first_frame=100, last_frame=110, obs=2)
        c = _make_cluster("C001", yaw=24.5, pitch=13.2,
                          member_dists=[0.3, 0.5])
        _, audited = _run([t], [c], reviewed_ids="C001")

        rec = next(r for r in audited if r["id"] == "T0005")
        self.assertNotIn("repeated_static_location_match", rec)

    # 6. Tracklet below MIN_SPAN → no annotation
    def test_no_match_below_min_span(self):
        t = _make_tracklet("T0006", yaw=24.0, pitch=13.0,
                           first_frame=100, last_frame=103, obs=4)
        c = _make_cluster("C001", yaw=24.5, pitch=13.2,
                          member_dists=[0.3, 0.5])
        _, audited = _run([t], [c], reviewed_ids="C001")

        rec = next(r for r in audited if r["id"] == "T0006")
        self.assertNotIn("repeated_static_location_match", rec)

    # 7. Near-static tracklet outside all cluster radii → match=False
    def test_no_match_outside_radius(self):
        # tracklet at yaw=90, pitch=0 — far from cluster at (24.5, 13.2)
        t = _make_tracklet("T0007", yaw=90.0, pitch=0.0,
                           first_frame=100, last_frame=500, obs=20)
        c = _make_cluster("C001", yaw=24.5, pitch=13.2,
                          member_dists=[0.3, 0.5, 0.7])
        _, audited = _run([t], [c], reviewed_ids="C001")

        rec = next(r for r in audited if r["id"] == "T0007")
        self.assertFalse(rec["repeated_static_location_match"])
        self.assertIsNone(rec["repeated_static_cluster_id"])
        self.assertFalse(rec["would_suppress_repeated_static"])

    # 8. Discovery radius (4.0°) is never the match radius for tight clusters
    def test_discovery_radius_not_reused(self):
        # Cluster with member dists all < 1°; p95 + guard < 4.0°
        c = _make_cluster("C001", yaw=24.5, pitch=13.2,
                          member_dists=[0.2, 0.3, 0.4, 0.5, 0.5, 0.6])
        index = dsm._build_cluster_index([c], {"C001"}, guard_margin=0.5, radius_cap=6.0)
        derived_radius = index[0]["match_radius_deg"]
        self.assertNotAlmostEqual(derived_radius, 4.0, places=3,
                                  msg="Discovery radius 4.0° must not be reused as match radius")
        self.assertLess(derived_radius, 4.0)

    # 9. Radius cap is respected
    def test_radius_cap_applied(self):
        # Member dists spread up to 10° — without cap this would exceed 6°
        c = _make_cluster("C001", yaw=24.5, pitch=13.2,
                          member_dists=[1.0, 3.0, 5.0, 8.0, 10.0])
        index = dsm._build_cluster_index([c], {"C001"}, guard_margin=0.5, radius_cap=6.0)
        self.assertLessEqual(index[0]["match_radius_deg"], 6.0)

    # 10. Guard margin is added to p95
    def test_guard_margin_added(self):
        member_dists = [0.5, 0.6, 0.7, 0.8, 0.9]  # p95 ≈ 0.88
        c = _make_cluster("C001", yaw=24.5, pitch=13.2, member_dists=member_dists)
        _, p95, raw = dsm._derive_match_radius(c, guard_margin=0.5, radius_cap=6.0)
        self.assertAlmostEqual(raw, p95 + 0.5, places=4)

    # 11. Original tracklets dict not mutated
    def test_original_tracklets_not_mutated(self):
        t = _make_tracklet("T0011", yaw=24.0, pitch=13.0,
                           first_frame=100, last_frame=500, obs=20)
        original_keys = set(t.keys())
        c = _make_cluster("C001", yaw=24.5, pitch=13.2, member_dists=[0.3, 0.5])

        with tempfile.TemporaryDirectory() as td:
            t_path = os.path.join(td, "tracklets.json")
            r_path = os.path.join(td, "report.json")
            out_dir = os.path.join(td, "out")
            os.makedirs(out_dir)
            with open(t_path, "w") as f:
                json.dump({"tracklets": [t]}, f)
            with open(r_path, "w") as f:
                json.dump({"meta": {}, "clusters": [c]}, f)
            original_content = open(t_path).read()
            args = argparse.Namespace(tracklets=t_path, report=r_path,
                                      reviewed_ids="C001", guard_margin=0.5,
                                      radius_cap=6.0, output_dir=out_dir)
            dsm.run(args)
            after_content = open(t_path).read()

        self.assertEqual(original_content, after_content,
                         "tracklets.json must not be modified on disk")
        # In-memory: original dict must not have gained annotation keys
        self.assertEqual(set(t.keys()), original_keys,
                         "Original tracklet dict must not be mutated in memory")

    # 12. Audit output contains ALL tracklets (eligible + ineligible)
    def test_audit_output_contains_all_tracklets(self):
        t_near = _make_tracklet("T_NEAR", yaw=24.0, pitch=13.0,
                                first_frame=100, last_frame=500, obs=20)
        t_move = _make_tracklet("T_MOVE", yaw=24.0, pitch=13.0,
                                first_frame=100, last_frame=500, obs=20,
                                net_disp_deg=50.0)
        t_rej  = _make_tracklet("T_REJ",  yaw=24.0, pitch=13.0,
                                first_frame=100, last_frame=500, obs=20,
                                status="rejected_static")
        c = _make_cluster("C001", yaw=24.5, pitch=13.2, member_dists=[0.3, 0.5])
        _, audited = _run([t_near, t_move, t_rej], [c], reviewed_ids="C001")

        ids_out = {r["id"] for r in audited}
        self.assertIn("T_NEAR", ids_out)
        self.assertIn("T_MOVE", ids_out)
        self.assertIn("T_REJ",  ids_out)
        self.assertEqual(len(audited), 3)

    # 13. Unreviewd cluster ID is not used for matching
    def test_unreviewed_cluster_not_matched(self):
        # Cluster C999 exists in report but is not in reviewed_ids
        t = _make_tracklet("T0013", yaw=24.0, pitch=13.0,
                           first_frame=100, last_frame=500, obs=20)
        c = _make_cluster("C999", yaw=24.5, pitch=13.2,
                          member_dists=[0.3, 0.5, 0.7])
        _, audited = _run([t], [c], reviewed_ids="C001")  # C001 not in report

        rec = next(r for r in audited if r["id"] == "T0013")
        # C999 not reviewed; C001 not in report → no match
        self.assertFalse(rec["repeated_static_location_match"])


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
