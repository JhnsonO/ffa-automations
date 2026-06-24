import unittest

from ball_tracker.experiments.backward_anchor_propagation import BackwardConfig, propagate_backward


class BackwardAnchorPropagationTests(unittest.TestCase):
    def test_reconstructs_smooth_path_back_from_later_anchor(self):
        frames = {
            10: [{"yaw": 10.0, "pitch": 0.0, "football_conf": 0.8}],
            9: [{"yaw": 9.0, "pitch": 0.0, "football_conf": 0.7}],
            8: [
                {"yaw": 8.0, "pitch": 0.0, "football_conf": 0.65},
                {"yaw": -30.0, "pitch": 4.0, "football_conf": 0.99},
            ],
        }
        path = propagate_backward(
            frames,
            {"yaw": 10.0, "pitch": 0.0, "football_conf": 1.0},
            start_frame=10,
            stop_frame=8,
            config=BackwardConfig(max_jump_deg=5.0),
        )
        self.assertEqual([point["frame"] for point in path], [8, 9, 10])
        self.assertAlmostEqual(path[0]["yaw"], 8.0)

    def test_stops_after_configured_gap(self):
        frames = {10: [{"yaw": 10.0, "pitch": 0.0, "football_conf": 0.8}]}
        path = propagate_backward(
            frames,
            {"yaw": 10.0, "pitch": 0.0},
            start_frame=10,
            stop_frame=1,
            config=BackwardConfig(max_gap_frames=2),
        )
        self.assertEqual(len(path), 1)


if __name__ == "__main__":
    unittest.main()
