import importlib.util
import os
import unittest

HERE = os.path.dirname(__file__)
SCRIPT = os.path.normpath(os.path.join(HERE, "..", "experiments", "multi_cue_diagnostic.py"))
spec = importlib.util.spec_from_file_location("multi_cue_diagnostic", SCRIPT)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class MultiCueDiagnosticTests(unittest.TestCase):
    def test_selected_observations_uses_early_mid_late(self):
        tracklet = {"frames": [{"frame": 1}, {"frame": 2}, {"frame": 3}, {"frame": 4}, {"frame": 5}]}
        picked = mod.selected_observations(tracklet)
        self.assertEqual([label for label, _ in picked], ["EARLY", "MID", "LATE"])
        self.assertEqual([obs["frame"] for _, obs in picked], [1, 3, 5])

    def test_geometry_values_derives_missing_scalar_fields_from_bbox(self):
        candidate = {"detection_geometry": {"bbox_xyxy": [10, 20, 40, 60]}}
        values = mod.geometry_values(candidate, {})
        self.assertEqual(values["width"], 30.0)
        self.assertEqual(values["height"], 40.0)
        self.assertEqual(values["area"], 1200.0)
        self.assertAlmostEqual(values["aspect"], 0.75)

    def test_missing_geometry_is_unknown(self):
        self.assertIsNone(mod.geometry_cue({"width": None, "height": None, "area": None, "aspect": None}))

    def test_fused_score_renormalises_missing_cues(self):
        score = mod.fused_score({"detector": 1.0, "view_band": None, "pose": None, "geometry": None, "temporal": 0.0})
        self.assertAlmostEqual(score, 0.35 / (0.35 + 0.25))

    def test_pose_missing_is_unknown_not_negative(self):
        metrics = mod.pose_metrics((50.0, 50.0), [])
        self.assertIsNone(metrics["pose_score"])
        self.assertIsNone(metrics["ankle_distance_px"])

    def test_candidate_matching_requires_close_coordinate_match(self):
        obs = {"yaw": 10.0, "pitch": 1.0}
        cands = [{"yaw": 10.005, "pitch": 1.002}, {"yaw": 20.0, "pitch": 1.0}]
        self.assertEqual(mod.match_source_candidate(cands, obs), cands[0])
        self.assertIsNone(mod.match_source_candidate([{"yaw": 10.2, "pitch": 1.0}], obs))


if __name__ == "__main__":
    unittest.main()
