#!/usr/bin/env python3
"""
test_geometry_propagation.py — Prove Stage 1c detection_geometry survives
into Tier A experimental tracklet observations.

Tests:
  1. Geometry from a fresh Stage 1c candidate (bbox_xyxy + dimensions) propagates
     into the matching tracklet observation.
  2. Stage 0 reuse candidate (explicit null geometry) propagates null correctly.
  3. Observations with no matching candidate key get detection_geometry=None.
  4. geo_coverage_fraction is non-zero when fresh Stage 1c candidates are present.
  5. geo_coverage_fraction is 0.0 when only Stage 0 reuse (null) candidates are present.
  6. Source candidate dict is not mutated by propagation.
  7. Multiple tracklets each receive correct per-observation geometry.

No tracking logic, thresholds, Tier A, renderer, or frozen files are changed.
"""

import json
import math
import os
import sys
import unittest

# Insert ball_tracker on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stage2_tier_a_experimental_output as s2e


# ── Fixture helpers ────────────────────────────────────────────────────────────

def _fresh_geo(w=12, h=11, area=132, ar=1.09):
    """Stage 1c fresh detection geometry (all fields populated)."""
    return {
        "bbox_xyxy": [100, 200, 112, 211],
        "bbox_width_px": w,
        "bbox_height_px": h,
        "bbox_area_px": area,
        "bbox_aspect_ratio": ar,
        "crop_width_px": 1280,
        "crop_height_px": 720,
    }


def _null_geo():
    """Stage 0 reuse geometry (all fields explicitly null)."""
    return {
        "bbox_xyxy": None,
        "bbox_width_px": None,
        "bbox_height_px": None,
        "bbox_area_px": None,
        "bbox_aspect_ratio": None,
        "crop_width_px": None,
        "crop_height_px": None,
    }


def _cand(yaw, pitch, wconf, geo=None):
    """Stage 1c candidate dict matching real schema."""
    c = {"yaw": yaw, "pitch": pitch, "weighted_conf": wconf}
    if geo is not None:
        c["detection_geometry"] = geo
    return c


def _filtered_candidates(frame_cands: dict) -> dict:
    """Build filtered_data with real Stage 1c frames schema."""
    return {"frames": {str(k): v for k, v in frame_cands.items()}}


def _tracklets_data(tracklets: list) -> dict:
    """Wrap tracklet list in real schema."""
    return {"tracklets": tracklets}


def _obs(frame, yaw, pitch, conf=0.35):
    """Tracklet observation as written by stage2_temporal_link.py (no geometry key)."""
    return {
        "frame": frame,
        "yaw": round(yaw, 4),
        "pitch": round(pitch, 4),
        "weighted_conf": conf,
        "score": None,
        "alternates": [],
    }


# ── Tests ──────────────────────────────────────────────────────────────────────

class TestGeometryPropagation(unittest.TestCase):

    def test_1_fresh_geometry_propagates(self):
        """Fresh Stage 1c geometry must appear in the matching tracklet observation."""
        geo = _fresh_geo()
        filtered = _filtered_candidates({
            10: [_cand(5.0, 3.0, 0.40, geo=geo)],
        })
        idx = s2e._build_geometry_index(filtered)

        t_data = _tracklets_data([{
            "id": "T0001",
            "status": "anchor",
            "frames": [_obs(10, 5.0, 3.0)],
        }])
        updated, stats = s2e._propagate_geometry(t_data, idx)

        obs = updated["tracklets"][0]["frames"][0]
        self.assertIn("detection_geometry", obs)
        self.assertIsNotNone(obs["detection_geometry"])
        self.assertEqual(obs["detection_geometry"]["bbox_xyxy"], [100, 200, 112, 211])
        self.assertEqual(obs["detection_geometry"]["bbox_width_px"], 12)
        self.assertEqual(obs["detection_geometry"]["bbox_area_px"], 132)

    def test_2_null_geo_propagates_as_null(self):
        """Stage 0 reuse candidate (explicit null geometry) must propagate null values."""
        geo = _null_geo()
        filtered = _filtered_candidates({
            20: [_cand(10.0, -5.0, 0.15, geo=geo)],
        })
        idx = s2e._build_geometry_index(filtered)

        t_data = _tracklets_data([{
            "id": "T0002",
            "status": "fragment",
            "frames": [_obs(20, 10.0, -5.0)],
        }])
        updated, stats = s2e._propagate_geometry(t_data, idx)

        obs = updated["tracklets"][0]["frames"][0]
        self.assertIn("detection_geometry", obs)
        geo_out = obs["detection_geometry"]
        self.assertIsNotNone(geo_out)
        # All fields should be None (Stage 0 reuse)
        self.assertIsNone(geo_out["bbox_xyxy"])
        self.assertIsNone(geo_out["bbox_area_px"])

    def test_3_unmatched_observation_gets_none(self):
        """Observation with no matching candidate must get detection_geometry=None."""
        filtered = _filtered_candidates({
            30: [_cand(20.0, 0.0, 0.30, geo=_fresh_geo())],
        })
        idx = s2e._build_geometry_index(filtered)

        # Frame 99 has no candidate in filtered_data
        t_data = _tracklets_data([{
            "id": "T0003",
            "status": "passing",
            "frames": [_obs(99, 50.0, 10.0)],
        }])
        updated, stats = s2e._propagate_geometry(t_data, idx)

        obs = updated["tracklets"][0]["frames"][0]
        self.assertIn("detection_geometry", obs)
        self.assertIsNone(obs["detection_geometry"])

    def test_4_geo_coverage_nonzero_with_fresh_candidates(self):
        """geo_coverage_fraction must be > 0.0 when fresh Stage 1c candidates are present."""
        geo = _fresh_geo()
        filtered = _filtered_candidates({
            1: [_cand(1.0, 0.0, 0.45, geo=geo)],
            2: [_cand(2.0, 0.0, 0.45, geo=_fresh_geo(w=10, h=9, area=90, ar=1.1))],
        })
        idx = s2e._build_geometry_index(filtered)

        t_data = _tracklets_data([{
            "id": "T0004",
            "status": "anchor",
            "frames": [_obs(1, 1.0, 0.0), _obs(2, 2.0, 0.0)],
        }])
        _, stats = s2e._propagate_geometry(t_data, idx)

        self.assertGreater(stats["geo_coverage_fraction"], 0.0,
            f"Expected non-zero coverage, got {stats['geo_coverage_fraction']}")
        self.assertEqual(stats["geometry_populated"], 2)

    def test_5_coverage_zero_when_only_null_candidates(self):
        """geo_coverage_fraction must be 0.0 when all candidates have null geometry."""
        filtered = _filtered_candidates({
            1: [_cand(1.0, 0.0, 0.15, geo=_null_geo())],
            2: [_cand(2.0, 0.0, 0.15, geo=_null_geo())],
        })
        idx = s2e._build_geometry_index(filtered)

        t_data = _tracklets_data([{
            "id": "T0005",
            "status": "fragment",
            "frames": [_obs(1, 1.0, 0.0), _obs(2, 2.0, 0.0)],
        }])
        _, stats = s2e._propagate_geometry(t_data, idx)

        self.assertEqual(stats["geo_coverage_fraction"], 0.0)
        self.assertEqual(stats["geometry_populated"], 0)

    def test_6_source_candidate_not_mutated(self):
        """Source candidate dict must be unchanged after propagation."""
        geo = _fresh_geo()
        geo_copy = json.loads(json.dumps(geo))
        cand = _cand(5.0, 3.0, 0.40, geo=geo)
        filtered = _filtered_candidates({10: [cand]})
        idx = s2e._build_geometry_index(filtered)

        t_data = _tracklets_data([{
            "id": "T0006",
            "status": "passing",
            "frames": [_obs(10, 5.0, 3.0)],
        }])
        s2e._propagate_geometry(t_data, idx)

        # Original candidate should be unchanged
        self.assertEqual(cand["detection_geometry"], geo_copy)
        self.assertEqual(cand["yaw"], 5.0)

    def test_7_multiple_tracklets_correct_geometry(self):
        """Each tracklet observation must receive geometry from the correct candidate."""
        geo_a = _fresh_geo(w=8, h=8, area=64, ar=1.0)
        geo_b = _fresh_geo(w=20, h=15, area=300, ar=1.33)
        filtered = _filtered_candidates({
            5: [_cand(10.0, 2.0, 0.50, geo=geo_a)],
            5: [_cand(10.0, 2.0, 0.50, geo=geo_a),
                _cand(45.0, -5.0, 0.40, geo=geo_b)],
        })
        idx = s2e._build_geometry_index(filtered)

        t_data = _tracklets_data([
            {"id": "T0007A", "status": "anchor",   "frames": [_obs(5, 10.0, 2.0)]},
            {"id": "T0007B", "status": "passing",  "frames": [_obs(5, 45.0, -5.0)]},
        ])
        updated, stats = s2e._propagate_geometry(t_data, idx)

        obs_a = updated["tracklets"][0]["frames"][0]
        obs_b = updated["tracklets"][1]["frames"][0]

        self.assertEqual(obs_a["detection_geometry"]["bbox_width_px"], 8,
            "T0007A should get geo_a (w=8)")
        self.assertEqual(obs_b["detection_geometry"]["bbox_width_px"], 20,
            "T0007B should get geo_b (w=20)")
        self.assertEqual(stats["geometry_populated"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
