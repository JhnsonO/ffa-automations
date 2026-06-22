#!/usr/bin/env python3
"""
Stage 2 fixture tests — 7 assertions.
Uses real Stage 0 / Stage 1 schemas:
  - stage1_candidates: {"frames": {"0": [...], "1": [...]}}
  - hotspot_map: {"hotspot_regions": [...], "duty_cycle_threshold": 0.6, ...}
Run before full clip dispatch.
"""

import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stage2_temporal_link as s2


# ── Schema helpers matching real Stage 0 / Stage 1 output ────────────────────

def _cand(yaw, pitch, wconf):
    """Real stage1 candidate dict."""
    return {"yaw": yaw, "pitch": pitch, "weighted_conf": wconf}


def _stage1(frame_dict):
    """Real stage1_candidates.json: frames keyed by string frame number."""
    return {"frames": {str(k): v for k, v in frame_dict.items()}}


def _hotspot_map(regions, duty_threshold=0.6):
    """Real hotspot_map.json schema."""
    return {
        "duty_cycle_threshold": duty_threshold,
        "hotspot_regions": regions,
    }


def _region(centre_yaw, centre_pitch, peak_duty, radius_deg=6.0):
    """Real hotspot_region entry."""
    return {
        "centre_yaw":   centre_yaw,
        "centre_pitch": centre_pitch,
        "radius_deg":   radius_deg,
        "peak_duty":    peak_duty,
    }


def _run_stage2(frame_dict, hotspot_map):
    """Run stage2 on synthetic data using real schemas; return (tracklets, gaps)."""
    s2._tracklet_counter = 0

    with tempfile.TemporaryDirectory() as tmpdir:
        s1_path = os.path.join(tmpdir, "stage1_candidates.json")
        hm_path = os.path.join(tmpdir, "hotspot_map.json")
        with open(s1_path, "w") as f:
            json.dump(_stage1(frame_dict), f)
        with open(hm_path, "w") as f:
            json.dump(hotspot_map, f)

        import argparse
        args = argparse.Namespace(
            stage1_candidates=s1_path,
            hotspot_map=hm_path,
            output_dir=os.path.join(tmpdir, "out"),
            min_support_conf=0.10,
            max_link_gap=5,
            base_tolerance=5.0,
            max_speed=8.0,
            min_anchor_str=0.55,
        )
        s2.MIN_SUPPORT_CONF        = 0.10
        s2.MAX_LINK_GAP            = 5
        s2.BASE_TOLERANCE_DEG      = 5.0
        s2.MAX_SPEED_DEG_PER_FRAME = 8.0
        s2.MIN_ANCHOR_STRENGTH     = 0.55

        s2.run(args)

        with open(os.path.join(tmpdir, "out", "tracklets.json")) as f:
            tracklets = json.load(f)["tracklets"]
        with open(os.path.join(tmpdir, "out", "gaps.json")) as f:
            gaps = json.load(f)["gaps"]

    return tracklets, gaps


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestStage2Fixtures(unittest.TestCase):

    # 1. Moving non-hotspot tracklet → anchor
    def test_1_moving_tracklet_is_anchor(self):
        """31 frames, ball arcs 1 deg/frame, no hotspot. Must become anchor."""
        frames = {}
        for i in range(31):
            frames[100 + i] = [_cand(float(i), 0.0, 0.50)]

        t, g = _run_stage2(frames, _hotspot_map([]))
        anchors = [x for x in t if x["status"] == "anchor"]
        self.assertGreaterEqual(len(anchors), 1, "Expected at least one anchor")
        best = max(anchors, key=lambda x: x["anchor_strength_candidate"])
        self.assertGreaterEqual(best["anchor_strength_candidate"], s2.MIN_ANCHOR_STRENGTH)

    # 2. Fence reserve cluster never forms tracklet; gap → reserve_only
    def test_2_fence_reserve_never_forms_tracklet(self):
        """Fence cands (wconf=0.06) in confirmed hotspot. Must not form tracklets.
        Gap before support frames must be reserve_only with dominant_reserve_static_hotspot."""
        hmap = _hotspot_map([
            _region(-77.4, -3.9, peak_duty=0.75, radius_deg=6.0)
        ], duty_threshold=0.6)

        frames = {}
        # Frames 0–99: only fence reserve candidates
        for i in range(100):
            frames[i] = [_cand(-77.4, -3.9, 0.06)]
        # Frames 100–130: real ball + fence present
        for i in range(31):
            frames[100 + i] = [
                _cand(float(i), 0.0, 0.50),
                _cand(-77.4, -3.9, 0.06),
            ]

        t, g = _run_stage2(frames, hmap)

        # No tracklet formed purely from reserve candidates
        for tk in t:
            if tk["start_frame"] < 100 and tk["end_frame"] < 100:
                self.fail(f"Tracklet {tk['id']} formed from reserve-only fence candidates")

        pre_gaps = [gp for gp in g if gp["end_frame"] < 100]
        self.assertGreater(len(pre_gaps), 0, "Expected a gap before frame 100")
        self.assertEqual(pre_gaps[0]["gap_reason"], "reserve_only")
        self.assertTrue(pre_gaps[0]["dominant_reserve_static_hotspot"])

    # 3. Gap with only reserve candidates → gap_reason: reserve_only
    def test_3_reserve_only_gap(self):
        """Frames 50–99: reserve-only. At least one reserve_only gap expected."""
        frames = {}
        for i in range(50):
            frames[i] = [_cand(i * 0.5, 0.0, 0.40)]
        for i in range(50):
            frames[50 + i] = [_cand(30.0 + i * 0.1, 5.0, 0.05)]
        for i in range(31):
            frames[100 + i] = [_cand(50.0 + i * 0.5, 0.0, 0.40)]

        t, g = _run_stage2(frames, _hotspot_map([]))

        reserve_gaps = [gp for gp in g if gp["gap_reason"] == "reserve_only"]
        self.assertGreater(len(reserve_gaps), 0, "Expected at least one reserve_only gap")

    # 4. GENUINE static_suspect: partial hotspot overlap, only 1 secondary condition
    # 4. GENUINE static_suspect: partial hotspot overlap, only 1 secondary condition
    def test_4_static_suspect_anchor_strength_capped(self):
        """Ball drifts yaw=0→24 over 25 frames (1 deg/frame, pitch=0, wconf=0.35).
        Hotspot at yaw=12, radius=7° → obs at frames 5-19 (15/25) inside hotspot.
        sh_frac=0.60 in [0.5, 0.70). net_disp=24° → cond_disp false.
        spatial_spread~7° → cond_spread false. span=25>=20, cov=1.0>=0.6 → cond_span true.
        Exactly 1 secondary condition → static_suspect only (not rejected_static).
        anchor_strength_candidate must be <= 0.40."""
        hmap = _hotspot_map([
            _region(12.0, 0.0, peak_duty=0.70, radius_deg=7.0)
        ], duty_threshold=0.6)

        frames = {}
        for i in range(25):
            frames[i] = [_cand(float(i), 0.0, 0.35)]

        t, g = _run_stage2(frames, hmap)

        suspects = [x for x in t if x.get("static_suspect")]

        self.assertGreater(len(suspects), 0,
            f"Expected static_suspect. Got statuses={[tk['status'] for tk in t]}, "
            f"sh_fracs={[tk['confirmed_static_hotspot_frac'] for tk in t]}")

        for s in suspects:
            if s["anchor_strength_candidate"] is not None:
                self.assertLessEqual(
                    s["anchor_strength_candidate"], 0.40,
                    f"static_suspect anchor_strength {s['anchor_strength_candidate']} exceeds 0.40"
                )

    # 5. Seam-crossing tracklet links normally
    def test_5_seam_crossing_links_normally(self):
        """Ball crosses ±180° seam. Must produce one continuous tracklet."""
        frames = {}
        yaws = [175.0, 177.0, 179.0, -179.0, -177.0, -175.0,
                -173.0, -171.0, -169.0, -167.0, -165.0,
                -163.0, -161.0, -159.0, -157.0, -155.0]
        for i, yaw in enumerate(yaws):
            frames[i] = [_cand(yaw, 0.0, 0.50)]

        t, g = _run_stage2(frames, _hotspot_map([]))

        self.assertGreater(len(t), 0)
        best = max(t, key=lambda x: x["observation_count"])
        self.assertGreaterEqual(
            best["observation_count"], len(yaws) - 2,
            f"Seam crossing broke the tracklet (best obs={best['observation_count']})"
        )

    # 6. Distractor does not steal true tracklet
    def test_6_distractor_does_not_steal(self):
        """True ball at yaw=0→20, distractor at yaw=90. Must form separate tracklets."""
        frames = {}
        for i in range(21):
            frames[i] = [
                _cand(float(i), 0.0, 0.50),
                _cand(90.0,     0.0, 0.45),
            ]

        t, g = _run_stage2(frames, _hotspot_map([]))

        self.assertGreaterEqual(len(t), 2, "Distractor should form its own tracklet")

        true_track = min(t, key=lambda tk: abs(tk["frames"][0]["yaw"] - 0.0))
        dist_track = min(t, key=lambda tk: abs(tk["frames"][0]["yaw"] - 90.0))

        self.assertNotEqual(true_track["id"], dist_track["id"])

        for fd in true_track["frames"]:
            self.assertLess(
                abs(fd["yaw"] - fd["frame"] * 1.0), 5.0,
                f"True tracklet frame {fd['frame']} yaw drifted to distractor"
            )

    # 7. Low-motion non-hotspot tracklet → not marked static
    def test_7_low_motion_non_hotspot_not_static(self):
        """Nearly stationary, NO confirmed hotspot. Must not be static."""
        frames = {}
        for i in range(40):
            frames[i] = [_cand(45.0 + i * 0.01, 5.0, 0.35)]

        t, g = _run_stage2(frames, _hotspot_map([]))

        for tk in t:
            self.assertNotEqual(tk["status"], "rejected_static",
                "Non-hotspot tracklet incorrectly rejected as static")
            self.assertFalse(tk.get("static_suspect", False),
                "Non-hotspot tracklet incorrectly marked static_suspect")


if __name__ == "__main__":
    unittest.main(verbosity=2)
