#!/usr/bin/env python3
"""
Fixture tests for stage2_repeated_static_audit.py

Coverage:
  1. Seven separated near-static tracklets form one location cluster
  2. Moving tracklet excluded (net_disp >= MAJOR_MOTION_EXCLUSION_DEG)
  3. Close-in-time fragments do not overstate independent recurrence
  4. rejected_static tracklets are excluded
  5. Tracklet below MIN_OBS floor is excluded
  6. Tracklet below MIN_SPAN floor is excluded
  7. Cluster with too few members not flagged repeated-static
  8. Cluster with sufficient members but insufficient temporal span not flagged
  9. Cluster with members and span but only one window not flagged
 10. Valid repeated-static cluster: correct centre, radius, window count
 11. compute_distinct_windows: correct window splitting
 12. compute_distinct_windows: close mid-points collapse into one window
 13. Two clusters at different angular locations are independent
 14. Net-disp ceiling excludes moderately moving tracklet (< MAJOR_MOTION but >= NET_DISP_CEILING)

Run: python3 -m pytest ball_tracker/tests/test_stage2_repeated_static_audit.py -v
  or: python3 ball_tracker/tests/test_stage2_repeated_static_audit.py
"""

import argparse
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stage2_repeated_static_audit as audit


# ── Fixture helpers ───────────────────────────────────────────────────────────

def _make_tracklet(tid, yaw, pitch, first_frame, last_frame, obs=None, status="anchor", net_disp_deg=None):
    """
    Build a minimal tracklet dict.
    Observations are evenly spaced between first_frame and last_frame.
    If net_disp_deg is set, the last observation is displaced by that amount in yaw.
    """
    if obs is None:
        obs = max(3, last_frame - first_frame + 1)
    frames_out = []
    step = max(1, (last_frame - first_frame) // max(1, obs - 1))
    for i in range(obs):
        fn = first_frame + i * step
        fy = yaw
        fp = pitch
        # Apply a linear drift so that net_disp_deg is approximately correct
        if net_disp_deg is not None and obs > 1:
            fy = yaw + net_disp_deg * (i / (obs - 1))
        frames_out.append({"frame": fn, "yaw": fy, "pitch": fp, "weighted_conf": 0.5})
    return {"id": tid, "status": status, "frames": frames_out}


def _run_on_tracklets(tracklet_list, extra_args=None):
    """Write tracklets to a temp file, run audit.run(), return report dict."""
    with tempfile.TemporaryDirectory() as tmp:
        t_path = os.path.join(tmp, "tracklets.json")
        with open(t_path, "w") as f:
            json.dump(tracklet_list, f)
        args = argparse.Namespace(
            tracklets=t_path,
            audit=None,
            output_dir=tmp,
        )
        if extra_args:
            for k, v in extra_args.items():
                setattr(args, k, v)
        return audit.run(args)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestRepeatedStaticAudit(unittest.TestCase):

    # 1. Seven near-static tracklets at the same angular location form one cluster
    def test_seven_near_static_form_one_cluster(self):
        tracklets = [
            _make_tracklet(f"T{i:04d}", yaw=24.5, pitch=13.2,
                           first_frame=100 + i * 400,
                           last_frame=200 + i * 400, obs=15, net_disp_deg=0.3)
            for i in range(7)
        ]
        report = _run_on_tracklets(tracklets)
        self.assertEqual(report["meta"]["eligible_tracklet_count"], 7)
        self.assertEqual(report["meta"]["cluster_count"], 1)
        self.assertEqual(report["clusters"][0]["member_count"], 7)

    # 2. Moving tracklet (net >= MAJOR_MOTION_EXCLUSION_DEG) is excluded
    def test_major_motion_tracklet_excluded(self):
        # Static tracklet
        static = _make_tracklet("T0001", yaw=24.5, pitch=13.2,
                                 first_frame=100, last_frame=200, obs=15, net_disp_deg=0.3)
        # Moving tracklet — T0373 analogue: net ~42°
        moving = _make_tracklet("T0002", yaw=24.5, pitch=13.2,
                                 first_frame=500, last_frame=600, obs=20, net_disp_deg=42.5)
        report = _run_on_tracklets([static, moving])
        eligible_ids = [
            m["id"]
            for c in report["clusters"]
            for m in c["members"]
        ]
        self.assertIn("T0001", eligible_ids)
        self.assertNotIn("T0002", eligible_ids)
        self.assertEqual(report["meta"]["eligible_tracklet_count"], 1)

    # 3. Close-in-time fragments do not overstate independent recurrence
    def test_close_in_time_fragments_single_window(self):
        # Three tracklets at the same location.
        # Window algorithm compares each new mid to the LAST ACCEPTED window mid.
        # T0001 mid=110 → accepted.
        # T0002 mid=130 (gap 20 < 50) → collapses.
        # T0003 mid=148 (gap vs last-accepted=110: 38 < 50) → collapses.
        # Result: one distinct window.
        tracklets = [
            _make_tracklet("T0001", yaw=24.5, pitch=13.2, first_frame=100, last_frame=120, obs=10, net_disp_deg=0.2),
            _make_tracklet("T0002", yaw=24.6, pitch=13.3, first_frame=120, last_frame=140, obs=10, net_disp_deg=0.2),
            _make_tracklet("T0003", yaw=24.4, pitch=13.1, first_frame=138, last_frame=158, obs=10, net_disp_deg=0.2),
        ]
        report = _run_on_tracklets(tracklets)
        c = report["clusters"][0]
        self.assertEqual(c["member_count"], 3)
        self.assertEqual(c["distinct_window_count"], 1)
        self.assertFalse(c["is_repeated_static"])

    # 4. rejected_static tracklets are excluded
    def test_rejected_static_status_excluded(self):
        t = _make_tracklet("T0001", yaw=24.5, pitch=13.2,
                            first_frame=100, last_frame=200, obs=15,
                            net_disp_deg=0.2, status="rejected_static")
        report = _run_on_tracklets([t])
        self.assertEqual(report["meta"]["eligible_tracklet_count"], 0)
        self.assertEqual(report["meta"]["cluster_count"], 0)

    # 5. Tracklet below MIN_OBS floor is excluded
    def test_below_min_obs_excluded(self):
        t = _make_tracklet("T0001", yaw=24.5, pitch=13.2,
                            first_frame=100, last_frame=150, obs=2, net_disp_deg=0.2)
        report = _run_on_tracklets([t])
        self.assertEqual(report["meta"]["eligible_tracklet_count"], 0)

    # 6. Tracklet below MIN_SPAN floor is excluded
    def test_below_min_span_excluded(self):
        # obs=5 but first_frame == last_frame → span = 0
        t = {"id": "T0001", "status": "anchor",
             "frames": [{"frame": 100, "yaw": 24.5, "pitch": 13.2, "weighted_conf": 0.5}] * 5}
        report = _run_on_tracklets([t])
        self.assertEqual(report["meta"]["eligible_tracklet_count"], 0)

    # 7. Cluster with < MIN_CLUSTER_MEMBERS not flagged repeated-static
    def test_too_few_members_not_flagged(self):
        tracklets = [
            _make_tracklet("T0001", yaw=24.5, pitch=13.2, first_frame=100, last_frame=200, obs=15, net_disp_deg=0.2),
            _make_tracklet("T0002", yaw=24.6, pitch=13.3, first_frame=800, last_frame=900, obs=15, net_disp_deg=0.2),
        ]
        report = _run_on_tracklets(tracklets)
        # 2 members < MIN_CLUSTER_MEMBERS (3)
        c = report["clusters"][0]
        self.assertFalse(c["is_repeated_static"])

    # 8. Cluster with sufficient members but insufficient temporal span
    def test_insufficient_temporal_span_not_flagged(self):
        # 3 members all within frames 100–200 → span = 100 < MIN_TEMPORAL_SEPARATION_FRAMES (150)
        tracklets = [
            _make_tracklet("T0001", yaw=24.5, pitch=13.2, first_frame=100, last_frame=130, obs=10, net_disp_deg=0.2),
            _make_tracklet("T0002", yaw=24.5, pitch=13.2, first_frame=150, last_frame=175, obs=10, net_disp_deg=0.2),
            _make_tracklet("T0003", yaw=24.5, pitch=13.2, first_frame=185, last_frame=200, obs=10, net_disp_deg=0.2),
        ]
        report = _run_on_tracklets(tracklets)
        c = report["clusters"][0]
        self.assertLess(c["overall_temporal_span_frames"], 150)
        self.assertFalse(c["is_repeated_static"])

    # 9. Cluster with members and span but only one distinct window not flagged
    def test_one_window_not_flagged(self):
        # 4 members, mids at 105, 117, 129, 141 — all gaps vs first accepted (105) < 50
        # So only one distinct window → not flagged as repeated-static.
        tracklets = [
            _make_tracklet(f"T{i:04d}", yaw=24.5, pitch=13.2,
                           first_frame=100 + i * 12, last_frame=110 + i * 12, obs=8, net_disp_deg=0.2)
            for i in range(4)
        ]
        report = _run_on_tracklets(tracklets)
        c = report["clusters"][0]
        self.assertEqual(c["distinct_window_count"], 1)
        self.assertFalse(c["is_repeated_static"])

    # 10. Valid repeated-static cluster: correct is_repeated_static, member count, window count
    def test_valid_repeated_static_cluster(self):
        tracklets = [
            _make_tracklet("T0001", yaw=24.5, pitch=13.2, first_frame=100,  last_frame=200, obs=15, net_disp_deg=0.2),
            _make_tracklet("T0002", yaw=24.6, pitch=13.3, first_frame=500,  last_frame=600, obs=15, net_disp_deg=0.2),
            _make_tracklet("T0003", yaw=24.4, pitch=13.1, first_frame=1000, last_frame=1100, obs=15, net_disp_deg=0.2),
        ]
        report = _run_on_tracklets(tracklets)
        self.assertEqual(report["meta"]["repeated_static_cluster_count"], 1)
        c = report["clusters"][0]
        self.assertTrue(c["is_repeated_static"])
        self.assertEqual(c["member_count"], 3)
        self.assertGreaterEqual(c["distinct_window_count"], 2)
        self.assertGreaterEqual(c["overall_temporal_span_frames"], 150)
        # Centre should be near 24.5°/13.2°
        self.assertAlmostEqual(c["centre_yaw_deg"], 24.5, delta=1.0)
        self.assertAlmostEqual(c["centre_pitch_deg"], 13.2, delta=1.0)

    # 11. compute_distinct_windows splits correctly when gap >= MIN_WINDOW_GAP_FRAMES
    def test_distinct_windows_splits_correctly(self):
        members = [
            {"id": "T0001", "first_frame": 100, "last_frame": 150},   # mid=125
            {"id": "T0002", "first_frame": 500, "last_frame": 600},   # mid=550 — gap 425
            {"id": "T0003", "first_frame": 1000, "last_frame": 1100}, # mid=1050 — gap 500
        ]
        windows = audit.compute_distinct_windows(members)
        self.assertEqual(len(windows), 3)

    # 12. compute_distinct_windows: close mid-points collapse into one window
    def test_distinct_windows_collapses_close(self):
        # Window algorithm compares each new mid to the LAST ACCEPTED window mid.
        # T0001 mid=110 → accepted.
        # T0002 mid=130 (gap 20 < 50) → collapses.
        # T0003 mid=155 (gap vs last-accepted=110: 45 < 50) → collapses.
        # Result: one distinct window.
        members = [
            {"id": "T0001", "first_frame": 100, "last_frame": 120},  # mid=110
            {"id": "T0002", "first_frame": 120, "last_frame": 140},  # mid=130
            {"id": "T0003", "first_frame": 145, "last_frame": 165},  # mid=155 — gap vs T0001=45 < 50
        ]
        windows = audit.compute_distinct_windows(members)
        self.assertEqual(len(windows), 1)

    # 13. Two clusters at different angular locations remain independent
    def test_two_clusters_independent(self):
        loc_a = [
            _make_tracklet(f"TA{i:02d}", yaw=24.5, pitch=13.2,
                           first_frame=100 + i * 400, last_frame=200 + i * 400, obs=12, net_disp_deg=0.2)
            for i in range(3)
        ]
        loc_b = [
            _make_tracklet(f"TB{i:02d}", yaw=-77.4, pitch=-3.9,
                           first_frame=100 + i * 400, last_frame=200 + i * 400, obs=12, net_disp_deg=0.2)
            for i in range(3)
        ]
        report = _run_on_tracklets(loc_a + loc_b)
        self.assertEqual(report["meta"]["cluster_count"], 2)
        self.assertEqual(report["meta"]["repeated_static_cluster_count"], 2)

    # 14. Moderately moving tracklet (net >= NET_DISP_CEILING but < MAJOR_MOTION) is excluded
    def test_moderate_motion_excluded_by_ceiling(self):
        # net_disp_deg = 2.0 — above NET_DISP_CEILING (1.5) but well below MAJOR_MOTION (42°)
        t = _make_tracklet("T0001", yaw=24.5, pitch=13.2,
                            first_frame=100, last_frame=200, obs=15, net_disp_deg=2.0)
        report = _run_on_tracklets([t])
        self.assertEqual(report["meta"]["eligible_tracklet_count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
