#!/usr/bin/env python3
"""Fixture tests for reversible Stage 1b confirmed-static quarantine."""

import copy
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stage1b_static_quarantine as s1b


def cand(yaw, pitch, raw=0.8, weighted=0.1, source="new_detection", crop_yaw=270):
    return {
        "yaw": yaw,
        "pitch": pitch,
        "raw_conf": raw,
        "weighted_conf": weighted,
        "penalty": round(weighted / raw, 4) if raw else 1.0,
        "source": source,
        "crop_yaw": crop_yaw,
        "region": None,
    }


def hotspot_map():
    return {
        "duty_cycle_threshold": 0.6,
        "hotspot_regions": [
            {"centre_yaw": -77.0, "centre_pitch": -3.0, "radius_deg": 2.0, "peak_duty": 0.9},
            {"centre_yaw": 25.0, "centre_pitch": 13.0, "radius_deg": 2.0, "peak_duty": 0.25},
        ],
    }


class TestStage1bQuarantine(unittest.TestCase):

    def test_confirmed_static_is_quarantined_and_preserved(self):
        source = {
            "fps": 30.0,
            "total_frames": 3,
            "frames": {
                "0": [cand(-77.0, -3.0), cand(10.0, 0.0)],
                "1": [cand(-76.0, -3.0)],
                "2": [],
            },
        }
        original = copy.deepcopy(source)
        output, report = s1b.quarantine_stage1_data(source, hotspot_map())

        self.assertEqual(len(output["frames"]["0"]), 1)
        self.assertEqual(output["frames"]["0"][0]["yaw"], 10.0)
        self.assertEqual(output["frames"]["1"], [])
        self.assertEqual(output["quarantined_candidates"]["0"] [0]["quarantine"]["reason"], "confirmed_static_hotspot")
        self.assertEqual(output["quarantined_candidates"]["0"] [0]["quarantine"]["region"], "(-77.0,-3.0)")
        self.assertEqual(report["summary"]["candidates_before"], 3)
        self.assertEqual(report["summary"]["candidates_quarantined"], 2)
        self.assertEqual(report["summary"]["candidates_active"], 1)
        self.assertEqual(report["summary"]["frames_newly_zero_candidate"], 1)
        self.assertEqual(source, original, "Adapter must not mutate original Stage 1 data")

    def test_nonconfirmed_hotspot_remains_active(self):
        source = {"frames": {"0": [cand(25.0, 13.0)]}}
        output, report = s1b.quarantine_stage1_data(source, hotspot_map())

        self.assertEqual(len(output["frames"]["0"]), 1)
        self.assertEqual(output.get("quarantined_candidates", {}), {})
        self.assertEqual(report["summary"]["candidates_quarantined"], 0)

    def test_seam_distance_is_spherical(self):
        hmap = {
            "duty_cycle_threshold": 0.6,
            "hotspot_regions": [
                {"centre_yaw": 179.0, "centre_pitch": 0.0, "radius_deg": 3.0, "peak_duty": 0.8},
            ],
        }
        source = {"frames": {"0": [cand(-179.0, 0.0)]}}
        output, report = s1b.quarantine_stage1_data(source, hmap)

        self.assertEqual(output["frames"]["0"], [])
        self.assertEqual(report["summary"]["candidates_quarantined"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
