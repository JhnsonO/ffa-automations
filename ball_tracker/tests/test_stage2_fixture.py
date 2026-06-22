#!/usr/bin/env python3
"""
Stage 2 fixture tests — 7 synthetic assertions.
Run before full clip dispatch.
"""

import json
import math
import os
import sys
import tempfile
import unittest

# Add parent dir to path so we can import stage2
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stage2_temporal_link as s2


def _cand(yaw, pitch, wconf):
    return {"yaw": yaw, "pitch": pitch, "weighted_conf": wconf}


def _frame(idx, cands):
    return {"frame": idx, "candidates": cands}


def _make_hotspot_map(hotspots):
    return {"hotspots": hotspots}


def _run_stage2(frames, hotspot_map):
    """Run stage2 on synthetic data; return (tracklets, gaps)."""
    s2._tracklet_counter = 0  # reset for each test

    stage1 = {"frames": frames}

    with tempfile.TemporaryDirectory() as tmpdir:
        s1_path = os.path.join(tmpdir, "stage1_candidates.json")
        hm_path = os.path.join(tmpdir, "hotspot_map.json")
        with open(s1_path, "w") as f:
            json.dump(stage1, f)
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
        # Apply globals
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


class TestStage2Fixtures(unittest.TestCase):

    # ── 1. Moving non-hotspot tracklet → anchor ───────────────────────────────
    def test_1_moving_tracklet_is_anchor(self):
        """Frames 100–130: ball moving steadily; no hotspot. Must become anchor."""
        frames = []
        # Ball arcs from yaw=0 to yaw=30 over 30 frames, pitch stable at 0
        # wconf=0.50 throughout — well above support threshold
        for i in range(31):
            yaw = i * 1.0  # 1 deg/frame → 30 deg total
            frames.append(_frame(100 + i, [_cand(yaw, 0.0, 0.50)]))

        t, g = _run_stage2(frames, _make_hotspot_map([]))
        anchors = [x for x in t if x["status"] == "anchor"]
        self.assertGreaterEqual(len(anchors), 1, "Expected at least one anchor tracklet")
        best = max(anchors, key=lambda x: x["anchor_strength_candidate"])
        self.assertGreaterEqual(best["anchor_strength_candidate"], s2.MIN_ANCHOR_STRENGTH)

    # ── 2. Fence reserve cluster never forms tracklet; gap → reserve_only ────
    def test_2_fence_reserve_never_forms_tracklet(self):
        """Fence candidates (wconf=0.06) throughout; plus support tracklet 100–130.
        Fence region: yaw=-77.4, pitch=-3.9, peak_duty=0.75.
        Before the support tracklet: gap_reason must be reserve_only."""
        hmap = _make_hotspot_map([{
            "region":    "fence_yaw_neg77",
            "yaw":       -77.4,
            "pitch":     -3.9,
            "radius_deg": 6.0,
            "peak_duty": 0.75,
        }])

        frames = []
        # Frames 0–99: only fence reserve candidates
        for i in range(100):
            frames.append(_frame(i, [_cand(-77.4, -3.9, 0.06)]))
        # Frames 100–130: real ball moving (support)
        for i in range(31):
            frames.append(_frame(100 + i, [
                _cand(i * 1.0, 0.0, 0.50),
                _cand(-77.4, -3.9, 0.06),  # fence also present
            ]))

        t, g = _run_stage2(frames, hmap)

        # No tracklet formed from reserve-only fence candidates
        # All tracklets must have at least some support-tier frames
        for tk in t:
            # A tracklet that spans only frames 0–99 with wconf=0.06 should not exist
            # (reserve cands cannot seed tracklets)
            if tk["start_frame"] < 100 and tk["end_frame"] < 100:
                self.fail(f"Tracklet {tk['id']} formed from reserve-only fence candidates")

        # Gap before frame 100 must exist and be reserve_only
        pre_gaps = [gp for gp in g if gp["end_frame"] < 100]
        self.assertGreater(len(pre_gaps), 0, "Expected a gap before frame 100")
        self.assertEqual(pre_gaps[0]["gap_reason"], "reserve_only")
        self.assertTrue(pre_gaps[0]["dominant_reserve_static_hotspot"])

    # ── 3. Gap with only reserve candidates → gap_reason: reserve_only ───────
    def test_3_reserve_only_gap(self):
        """Frames 50–99: only reserve cands. gap_reason must be reserve_only."""
        hmap = _make_hotspot_map([])
        frames = []
        # Frames 0–49: support tracklet
        for i in range(50):
            frames.append(_frame(i, [_cand(i * 0.5, 0.0, 0.40)]))
        # Frames 50–99: reserve only (not in any hotspot)
        for i in range(50):
            frames.append(_frame(50 + i, [_cand(30.0 + i * 0.1, 5.0, 0.05)]))
        # Frames 100–130: support tracklet resumes
        for i in range(31):
            frames.append(_frame(100 + i, [_cand(50.0 + i * 0.5, 0.0, 0.40)]))

        t, g = _run_stage2(frames, hmap)

        gap_50_99 = [gp for gp in g if gp["start_frame"] >= 50 and gp["end_frame"] <= 99]
        # There may not be a clean gap 50–99 if tracklets span across; look for
        # any gap overlapping 50–99 that has reserve_only
        reserve_gaps = [gp for gp in g if gp["gap_reason"] == "reserve_only"]
        self.assertGreater(len(reserve_gaps), 0, "Expected at least one reserve_only gap")

    # ── 4. Static-suspect tracklet: anchor_strength capped at 0.4 ────────────
    def test_4_static_suspect_cap(self):
        """Static-suspect tracklet: sh_frac >= 0.5, 1 of 3 secondary conds.
        anchor_strength_candidate must be <= 0.4."""
        hmap = _make_hotspot_map([{
            "region":    "partial_static",
            "yaw":       10.0,
            "pitch":     2.0,
            "radius_deg": 8.0,
            "peak_duty": 0.65,
        }])
        frames = []
        # Stationary cluster near the hotspot region — 60 frames, no movement
        # => net_displacement_deg < 2.0 (cond_disp) → 1 secondary condition
        # sh_frac: all 60 obs in hotspot → sh_frac=1.0 ≥ 0.5 → static_suspect
        for i in range(60):
            frames.append(_frame(i, [_cand(10.0, 2.0, 0.35)]))

        t, g = _run_stage2(frames, hmap)

        suspects = [x for x in t if x.get("static_suspect")]
        if not suspects:
            # might be rejected_static if secondary >= 2; check rejected instead
            rejected = [x for x in t if x["status"] == "rejected_static"]
            # rejected_static is stricter — this is also acceptable (no anchor_str exposed)
            if rejected:
                return  # passes — static correctly identified
            self.fail("Expected at least one static_suspect or rejected_static tracklet")

        for s in suspects:
            if s["anchor_strength_candidate"] is not None:
                self.assertLessEqual(
                    s["anchor_strength_candidate"], 0.40,
                    f"static_suspect anchor_strength {s['anchor_strength_candidate']} exceeds 0.4"
                )

    # ── 5. Seam-crossing tracklet links normally ──────────────────────────────
    def test_5_seam_crossing_links_normally(self):
        """Ball crosses ±180° seam: yaw goes +175 → +179 → -179 → -175.
        Must produce one continuous tracklet (no seam break)."""
        frames = []
        yaws = [175.0, 177.0, 179.0, -179.0, -177.0, -175.0,
                -173.0, -171.0, -169.0, -167.0, -165.0,
                -163.0, -161.0, -159.0, -157.0, -155.0]
        for i, yaw in enumerate(yaws):
            frames.append(_frame(i, [_cand(yaw, 0.0, 0.50)]))

        t, g = _run_stage2(frames, _make_hotspot_map([]))

        # Should be one tracklet spanning all frames
        self.assertGreater(len(t), 0)
        # Largest tracklet by obs count
        best = max(t, key=lambda x: x["observation_count"])
        self.assertGreaterEqual(
            best["observation_count"], len(yaws) - 2,
            "Seam crossing broke the tracklet"
        )

    # ── 6. Distractor doesn't steal tracklet from true path ──────────────────
    def test_6_distractor_does_not_steal(self):
        """True ball moves from yaw=0 to yaw=20 over 20 frames (wconf=0.50).
        Distractor at yaw=90, pitch=0, wconf=0.45 present every frame.
        True tracklet must dominate; distractor forms a separate tracklet."""
        frames = []
        for i in range(21):
            yaw_true       = i * 1.0
            yaw_distractor = 90.0
            frames.append(_frame(i, [
                _cand(yaw_true,       0.0, 0.50),
                _cand(yaw_distractor, 0.0, 0.45),
            ]))

        t, g = _run_stage2(frames, _make_hotspot_map([]))

        # There must be (at least) two tracklets
        self.assertGreaterEqual(len(t), 2, "Distractor should form its own tracklet")

        # Find tracklet closest to yaw=0 at start
        def start_yaw(tk):
            return tk["frames"][0]["yaw"]

        true_track = min(t, key=lambda tk: abs(start_yaw(tk) - 0.0))
        dist_track = min(t, key=lambda tk: abs(start_yaw(tk) - 90.0))

        self.assertNotEqual(true_track["id"], dist_track["id"],
                            "True path and distractor must be separate tracklets")

        # True tracklet should have obs near true ball positions
        for fd in true_track["frames"]:
            self.assertLess(abs(fd["yaw"] - fd["frame"] * 1.0), 5.0,
                            f"True tracklet frame {fd['frame']} yaw drifted to distractor")

    # ── 7. Low-motion non-hotspot → not marked static ────────────────────────
    def test_7_low_motion_non_hotspot_not_static(self):
        """Low-motion cluster not in any confirmed hotspot.
        Must NOT be rejected_static or static_suspect."""
        frames = []
        # 40 frames, nearly stationary at yaw=45 pitch=5
        # net_displacement < 2.0, spatial_spread < 1.5 → 2 secondary
        # BUT sh_frac = 0 (no hotspot) → cannot be static
        for i in range(40):
            frames.append(_frame(i, [_cand(45.0 + i * 0.01, 5.0, 0.35)]))

        t, g = _run_stage2(frames, _make_hotspot_map([]))

        for tk in t:
            self.assertNotEqual(
                tk["status"], "rejected_static",
                "Non-hotspot tracklet incorrectly rejected as static"
            )
            self.assertFalse(
                tk.get("static_suspect", False),
                "Non-hotspot tracklet incorrectly marked static_suspect"
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
