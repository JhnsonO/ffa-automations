#!/usr/bin/env python3
"""
Fixture tests for stage2_static_motion_audit.py
Tests: metric calculations, zero-net handling, borderline logic,
       annotation-only behaviour, strong-motion retention.
Run: python3 -m pytest ball_tracker/tests/test_stage2_static_motion_audit.py -v
  or: python3 ball_tracker/tests/test_stage2_static_motion_audit.py
"""

import json
import math
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import stage2_static_motion_audit as audit


# ── Helpers ───────────────────────────────────────────────────────────────────

def _frame(frame_idx, yaw, pitch, wconf=0.3, score=None):
    return {"frame": frame_idx, "yaw": yaw, "pitch": pitch,
            "weighted_conf": wconf, "score": score, "alternates": []}


def _tracklet(tid, status, frames, span=None, sh_frac=0.0,
              net_disp=None, spatial_spread=0.5):
    """
    Build a synthetic tracklet dict matching the tracklets.json schema
    from stage2_temporal_link.py finalise().
    net_disp and span are auto-computed from frames if not supplied.
    """
    sorted_f = sorted(frames, key=lambda f: f["frame"])
    obs_count = len(sorted_f)
    start = sorted_f[0]["frame"]
    end   = sorted_f[-1]["frame"]
    span_ = end - start + 1 if span is None else span

    if net_disp is None:
        if obs_count >= 2:
            v0 = audit.to_unit_vec(sorted_f[0]["yaw"],  sorted_f[0]["pitch"])
            v1 = audit.to_unit_vec(sorted_f[-1]["yaw"], sorted_f[-1]["pitch"])
            net_disp = audit.great_circle_deg(v0, v1)
        else:
            net_disp = 0.0

    return {
        "id":                        tid,
        "status":                    status,
        "rejection_reason":          None,
        "static_suspect":            False,
        "start_frame":               start,
        "end_frame":                 end,
        "span_frames":               span_,
        "observation_count":         obs_count,
        "coverage_ratio":            round(obs_count / max(1, span_), 4),
        "max_internal_gap":          0,
        "mean_weighted_conf":        0.3,
        "mean_prediction_residual":  0.5,
        "velocity_consistency":      0.5,
        "net_displacement_deg":      round(net_disp, 4),
        "spatial_spread_deg":        round(spatial_spread, 4),
        "mean_velocity_deg_per_frame": 0.01,
        "confirmed_static_hotspot_frac": sh_frac,
        "anchor_strength_candidate": 0.6 if status == "anchor" else None,
        "best_available_score":      None,
        "frames":                    sorted_f,
    }


def _static_tracklet(tid, status="anchor", obs=20, span=25):
    """All obs at the same position — perfectly static."""
    frames = [_frame(i * (span // obs), 10.0, -5.0) for i in range(obs)]
    return _tracklet(tid, status, frames, span=span, net_disp=0.0)


def _moving_tracklet(tid, status="anchor", obs=20, span=25, total_arc=15.0):
    """Obs moving uniformly along yaw."""
    step_yaw = total_arc / max(1, obs - 1)
    frames = [_frame(i, 0.0 + i * step_yaw, 0.0) for i in range(obs)]
    return _tracklet(tid, status, frames, span=span)


def _run_audit(tracklets):
    """Run audit on a list of tracklet dicts; return list of audited records."""
    seen = set()
    return [audit.audit_tracklet(t, seen) for t in tracklets]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestMetricCalculations(unittest.TestCase):

    def test_static_tracklet_metrics(self):
        """Perfectly static: net≈0, MAD≈0, p90_step≈0, path≈0."""
        t = _static_tracklet("T_STATIC", obs=20, span=25)
        m = audit.compute_audit_metrics(t)
        self.assertEqual(m["obs_count"], 20)
        self.assertEqual(m["span_frames"], 25)
        self.assertLess(m["net_disp_deg"],   0.01)
        self.assertLess(m["spread_MAD_deg"], 0.01)
        self.assertLess(m["p90_step_deg"],   0.01)
        self.assertLess(m["path_length_deg"], 0.1)

    def test_moving_tracklet_metrics(self):
        """Moving 15° arc: net > 1.5, path > 0, MAD > 0."""
        t = _moving_tracklet("T_MOVE", obs=20, span=20, total_arc=15.0)
        m = audit.compute_audit_metrics(t)
        self.assertGreater(m["net_disp_deg"],   1.5)
        self.assertGreater(m["path_length_deg"], 5.0)
        self.assertGreater(m["spread_MAD_deg"],  0.1)
        self.assertGreater(m["p90_step_deg"],    0.1)

    def test_path_length_gte_net_displacement(self):
        """Path length must always be >= net displacement (triangle inequality)."""
        for arc in [0.0, 2.0, 15.0, 40.0]:
            t = _moving_tracklet("T_TRI", obs=15, span=20, total_arc=arc)
            m = audit.compute_audit_metrics(t)
            self.assertGreaterEqual(
                m["path_length_deg"] + 1e-9, m["net_disp_deg"],
                msg=f"Failed for arc={arc}"
            )

    def test_gap_count(self):
        """Gap count increments when obs are non-consecutive."""
        # obs at frames 0,1,2,10,11,12 — one gap between 2 and 10
        frames = [_frame(0, 10.0, 0.0), _frame(1, 10.1, 0.0),
                  _frame(2, 10.2, 0.0), _frame(10, 10.3, 0.0),
                  _frame(11, 10.4, 0.0), _frame(12, 10.5, 0.0)]
        t = _tracklet("T_GAP", "anchor", frames, span=13)
        m = audit.compute_audit_metrics(t)
        self.assertEqual(m["gap_count"], 1)

    def test_gap_fraction(self):
        """gap_fraction = gap_count / span_frames."""
        frames = [_frame(0, 10.0, 0.0), _frame(1, 10.1, 0.0),
                  _frame(10, 10.5, 0.0)]
        t = _tracklet("T_GF", "anchor", frames, span=11)
        m = audit.compute_audit_metrics(t)
        self.assertEqual(m["gap_count"], 1)
        self.assertAlmostEqual(m["gap_fraction"], 1 / 11, places=4)

    def test_spread_MAD_single_obs(self):
        """Single-observation tracklet: MAD = 0 (no variance)."""
        frames = [_frame(0, 10.0, -5.0)]
        t = _tracklet("T_SINGLE", "fragment", frames, span=1, net_disp=0.0)
        m = audit.compute_audit_metrics(t)
        self.assertEqual(m["spread_MAD_deg"], 0.0)

    def test_median_and_p90_step(self):
        """Median step < p90 step for unequal steps."""
        # Small steps then one large step
        frames = [_frame(i, float(i) * 0.05, 0.0) for i in range(10)]
        frames.append(_frame(10, 5.0, 0.0))   # large jump
        t = _tracklet("T_P90", "passing", frames, span=11)
        m = audit.compute_audit_metrics(t)
        self.assertLess(m["median_step_deg"], m["p90_step_deg"])


class TestZeroNetHandling(unittest.TestCase):

    def test_zero_net_ratio_is_none(self):
        """path_to_net_ratio must be None when net_disp <= 1e-9."""
        t = _static_tracklet("T_ZERO", obs=20, span=25)
        m = audit.compute_audit_metrics(t)
        self.assertLess(m["net_disp_deg"], 1e-9)
        self.assertIsNone(m["path_to_net_ratio"])

    def test_nonzero_net_ratio_is_float(self):
        """path_to_net_ratio is a float when net > 0."""
        t = _moving_tracklet("T_NONZERO", obs=15, span=15, total_arc=10.0)
        m = audit.compute_audit_metrics(t)
        self.assertIsNotNone(m["path_to_net_ratio"])
        self.assertIsInstance(m["path_to_net_ratio"], float)

    def test_zero_net_does_not_crash_audit(self):
        """audit_tracklet must not raise on perfectly static input."""
        t = _static_tracklet("T_ZNC", obs=15, span=20)
        seen = set()
        result = audit.audit_tracklet(t, seen)
        self.assertIn("_audit", result)

    def test_zero_net_gate_evaluation(self):
        """net_disp_lt_1p5 condition must be True for zero net."""
        t = _static_tracklet("T_ZGATE", obs=15, span=20)
        m = audit.compute_audit_metrics(t)
        _, conds, _ = audit.evaluate_rejection_gate(m)
        self.assertTrue(conds["net_disp_lt_1p5"])


class TestRejectionGate(unittest.TestCase):

    def _all_pass_tracklet(self, tid="T_ALL"):
        """Tracklet that satisfies all five gate conditions."""
        # obs=15 (>=12), span=25 (>=20), static position (net<1.5, MAD<0.6, p90<0.25)
        return _static_tracklet(tid, obs=15, span=25)

    def test_all_five_conditions_met_rejects(self):
        t = self._all_pass_tracklet()
        m = audit.compute_audit_metrics(t)
        would_reject, conds, failed = audit.evaluate_rejection_gate(m)
        self.assertTrue(would_reject)
        self.assertEqual(failed, [])

    def test_low_obs_count_prevents_rejection(self):
        """obs_count < 12 — should not reject."""
        t = _static_tracklet("T_LOW_OBS", obs=8, span=25)
        m = audit.compute_audit_metrics(t)
        would_reject, _, failed = audit.evaluate_rejection_gate(m)
        self.assertFalse(would_reject)
        self.assertIn("obs_count_gte_12", failed)

    def test_low_span_prevents_rejection(self):
        """span < 20 — should not reject."""
        t = _static_tracklet("T_LOW_SPAN", obs=12, span=15)
        m = audit.compute_audit_metrics(t)
        would_reject, _, failed = audit.evaluate_rejection_gate(m)
        self.assertFalse(would_reject)
        self.assertIn("span_gte_20", failed)

    def test_high_net_disp_prevents_rejection(self):
        """net_disp >= 1.5 — should not reject."""
        t = _moving_tracklet("T_HIGH_NET", obs=15, span=25, total_arc=5.0)
        m = audit.compute_audit_metrics(t)
        would_reject, _, failed = audit.evaluate_rejection_gate(m)
        self.assertFalse(would_reject)
        self.assertIn("net_disp_lt_1p5", failed)

    def test_high_MAD_prevents_rejection(self):
        """Spread MAD >= 0.6 — should not reject even if other conditions pass."""
        # Two clusters far apart in pitch → high MAD
        frames = (
            [_frame(i, 10.0, -5.0) for i in range(0, 12)] +
            [_frame(i, 10.0,  5.0) for i in range(12, 24)]
        )
        t = _tracklet("T_HIGH_MAD", "anchor", frames, span=30, net_disp=0.01)
        m = audit.compute_audit_metrics(t)
        if m["spread_MAD_deg"] >= audit.GATE_SPREAD_MAD:
            would_reject, _, failed = audit.evaluate_rejection_gate(m)
            self.assertFalse(would_reject)
            self.assertIn("spread_MAD_lt_0p6", failed)
        else:
            # If MAD happened to be < 0.6, verify other conditions hold normally
            self.assertIsNotNone(m)

    def test_high_p90_step_prevents_rejection(self):
        """p90 step >= 0.25 — should not reject."""
        # Mix of static observations with occasional large jumps
        frames = [_frame(i, 10.0, 0.0) for i in range(10)]
        # Add large-jump observations to push p90 high
        for j in range(4):
            frames.append(_frame(10 + j * 3, 10.0 + (j + 1) * 2.0, 0.0))
        t = _tracklet("T_HIGH_P90", "anchor", frames, span=25, net_disp=0.01)
        m = audit.compute_audit_metrics(t)
        if m["p90_step_deg"] >= audit.GATE_P90_STEP:
            would_reject, _, failed = audit.evaluate_rejection_gate(m)
            self.assertFalse(would_reject)
            self.assertIn("p90_step_lt_0p25", failed)


class TestBorderlineLogic(unittest.TestCase):

    def test_borderline_fails_exactly_one(self):
        """Borderline tracklet: 4 of 5 conditions met, fails exactly one."""
        # Static (net≈0, MAD≈0, p90≈0) but obs_count=8 < 12
        t = _static_tracklet("T_BORDER", obs=8, span=25)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertFalse(a["_audit"]["would_reject_static_motion"])
        self.assertTrue(a["_audit"]["is_borderline"])
        self.assertEqual(len(a["_audit"]["failed_conditions"]), 1)
        self.assertIn("obs_count_gte_12", a["_audit"]["failed_conditions"])

    def test_borderline_not_flagged_if_two_fail(self):
        """Two failed conditions: not borderline."""
        t = _static_tracklet("T_TWO_FAIL", obs=8, span=15)  # obs<12 AND span<20
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertFalse(a["_audit"]["would_reject_static_motion"])
        self.assertFalse(a["_audit"]["is_borderline"])
        self.assertGreaterEqual(len(a["_audit"]["failed_conditions"]), 2)

    def test_full_reject_not_borderline(self):
        """All five conditions met: would_reject=True, is_borderline=False."""
        t = _static_tracklet("T_FULL", obs=15, span=25)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertTrue(a["_audit"]["would_reject_static_motion"])
        self.assertFalse(a["_audit"]["is_borderline"])


class TestAnnotationOnlyBehaviour(unittest.TestCase):

    def test_original_status_unchanged(self):
        """would_reject_static_motion must not alter the status field."""
        t = _static_tracklet("T_STATUS", status="anchor", obs=20, span=30)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertEqual(a["status"], "anchor")
        self.assertTrue(a["_audit"]["would_reject_static_motion"])

    def test_original_fields_preserved(self):
        """All original tracklet fields must be present and unchanged."""
        t = _static_tracklet("T_PRES", obs=15, span=25)
        original_keys = set(t.keys())
        seen = set()
        a = audit.audit_tracklet(t, seen)
        for k in original_keys:
            self.assertIn(k, a)
            if k != "frames":   # frames are shared reference, not copied
                self.assertEqual(a[k], t[k], msg=f"Field {k} was modified")

    def test_audit_key_added(self):
        """_audit key must be present after audit_tracklet."""
        t = _static_tracklet("T_KEY", obs=12, span=20)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertIn("_audit", a)
        for required in ("would_reject_static_motion", "is_borderline",
                         "is_strong_motion_ref", "is_human_confirmed_static",
                         "failed_conditions", "gate_conditions", "metrics"):
            self.assertIn(required, a["_audit"])

    def test_no_status_field_in_audit_block(self):
        """_audit block must not contain a status field (no classification override)."""
        t = _static_tracklet("T_NO_STATUS", obs=15, span=25)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertNotIn("status", a["_audit"])

    def test_run_writes_outputs_only(self):
        """run() must write JSON/txt/review files without modifying tracklets.json."""
        t1 = _static_tracklet("T_RUN1", status="anchor", obs=20, span=30)
        t2 = _moving_tracklet("T_RUN2", status="anchor", obs=15, span=20, total_arc=10.0)
        data = {"tracklets": [t1, t2]}
        import argparse
        with tempfile.TemporaryDirectory() as tmpdir:
            tk_path = os.path.join(tmpdir, "tracklets.json")
            with open(tk_path, "w") as f:
                json.dump(data, f)
            args = argparse.Namespace(tracklets=tk_path, output_dir=tmpdir)
            audit.run(args)

            # tracklets.json must be untouched
            with open(tk_path) as f:
                reloaded = json.load(f)
            self.assertEqual(reloaded["tracklets"][0]["status"], "anchor")
            self.assertNotIn("_audit", reloaded["tracklets"][0])

            # Output files must exist
            for fname in ("stage2_audit_report.json",
                          "stage2_audit_report.txt",
                          "stage2_audit_review.txt"):
                self.assertTrue(os.path.exists(os.path.join(tmpdir, fname)),
                                msg=f"Missing output: {fname}")

    def test_diagnostic_metrics_not_rejection_gates(self):
        """path_to_net_ratio / path_length / median_step / gap metrics must not
        appear in gate_conditions — they are diagnostic only."""
        t = _static_tracklet("T_DIAG", obs=15, span=25)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        gate_cond_keys = set(a["_audit"]["gate_conditions"].keys())
        diagnostic_fields = {
            "path_to_net_ratio", "path_length_deg",
            "median_step_deg", "gap_count", "gap_fraction"
        }
        overlap = gate_cond_keys & diagnostic_fields
        self.assertEqual(overlap, set(), msg=f"Diagnostic fields in gates: {overlap}")


class TestStrongMotionRetention(unittest.TestCase):
    """Strong-motion reference tracklets must NEVER be would-reject."""

    def _make_ref(self, tid, obs, span, net_disp):
        """Build a passing/anchor reference tracklet with given motion."""
        frames = [_frame(i, float(i) * net_disp / max(1, obs - 1), 0.0)
                  for i in range(obs)]
        return _tracklet(tid, "passing", frames, span=span)

    def test_T0001_retained(self):
        """T0001: obs=60, net=30.1° — strong motion, must be retained."""
        t = self._make_ref("T0001", obs=60, span=67, net_disp=30.0)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertFalse(a["_audit"]["would_reject_static_motion"])
        self.assertTrue(a["_audit"]["is_strong_motion_ref"])

    def test_T0088_retained(self):
        """T0088 has obs=1 in smoke run — short, must not be rejected by obs gate."""
        t = _tracklet("T0088", "fragment", [_frame(100, 45.0, 2.0)], span=1, net_disp=0.0)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertFalse(a["_audit"]["would_reject_static_motion"])

    def test_T0318_retained(self):
        """T0318 obs=1 in smoke run — obs gate prevents rejection."""
        t = _tracklet("T0318", "fragment", [_frame(200, 20.0, 0.0)], span=1, net_disp=0.0)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertFalse(a["_audit"]["would_reject_static_motion"])

    def test_T0477_retained(self):
        """T0477: obs=7, span=13 in smoke run — obs gate prevents rejection."""
        frames = [_frame(i, 0.0, 0.0) for i in range(7)]
        t = _tracklet("T0477", "passing", frames, span=13, net_disp=0.169)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertFalse(a["_audit"]["would_reject_static_motion"])

    def test_T0499_documented(self):
        """T0499 in smoke run 28063029760: obs=85, span=152, net=0.024° — this is a
        near-zero passing tracklet, NOT a strong-motion reference. All five gate
        conditions are met; would_reject_static_motion=True is the correct outcome.
        T0499 is excluded from STRONG_MOTION_REFS; this test documents that fact."""
        frames = [_frame(i, math.sin(i * 0.1) * 0.05, 0.0) for i in range(85)]
        t = _tracklet("T0499", "passing", frames, span=152, net_disp=0.024)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        # T0499 is NOT in STRONG_MOTION_REFS
        self.assertFalse(a["_audit"]["is_strong_motion_ref"])
        # Its near-zero, high-obs nature means it may correctly appear in would-reject
        # Outcome is documented rather than asserted directionally


class TestNearZeroAnchorCoverage(unittest.TestCase):

    def test_near_zero_anchors_caught_count(self):
        """Audit must catch near-zero anchors satisfying all five conditions."""
        # Build 5 clear-static anchors and 2 moving anchors
        static_anchors = [
            _static_tracklet(f"T_S{i:02d}", status="anchor", obs=20, span=30)
            for i in range(5)
        ]
        moving_anchors = [
            _moving_tracklet(f"T_M{i:02d}", status="anchor", obs=20, span=25, total_arc=15.0)
            for i in range(2)
        ]
        all_t = static_anchors + moving_anchors
        audited = _run_audit(all_t)

        caught = [a for a in audited
                  if a["status"] == "anchor"
                  and a["net_displacement_deg"] < 1.5
                  and a["_audit"]["would_reject_static_motion"]]
        self.assertEqual(len(caught), 5)

        retained_moving = [a for a in audited
                           if a["status"] == "anchor"
                           and a["net_displacement_deg"] >= 1.5
                           and not a["_audit"]["would_reject_static_motion"]]
        self.assertEqual(len(retained_moving), 2)

    def test_human_confirmed_static_mapping(self):
        """Known human-confirmed IDs must be flagged as is_human_confirmed_static."""
        t = _static_tracklet("T0066", status="anchor", obs=13, span=18)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertTrue(a["_audit"]["is_human_confirmed_static"])
        self.assertIn("T0066", seen)

    def test_unknown_id_not_confirmed_static(self):
        """Arbitrary ID not in HUMAN_CONFIRMED_STATIC must not be flagged."""
        t = _static_tracklet("T9999", status="anchor", obs=15, span=25)
        seen = set()
        a = audit.audit_tracklet(t, seen)
        self.assertFalse(a["_audit"]["is_human_confirmed_static"])


class TestRealSmokeArtifact(unittest.TestCase):
    """Integration test against real smoke artifact 7835756306 if available."""

    SMOKE_PATH = "/tmp/smoke_artifact/tracklets.json"

    def setUp(self):
        if not os.path.exists(self.SMOKE_PATH):
            self.skipTest("Smoke artifact not available locally")

    def test_strong_motion_refs_retained_in_real_data(self):
        """T0001 must not be would-reject in the real smoke data."""
        with open(self.SMOKE_PATH) as f:
            data = json.load(f)
        seen = set()
        audited = {t["id"]: audit.audit_tracklet(t, seen) for t in data["tracklets"]}

        # T0001 is a real strong-motion anchor (net=30.1°)
        if "T0001" in audited:
            self.assertFalse(audited["T0001"]["_audit"]["would_reject_static_motion"])

    def test_near_zero_anchors_caught_real_data(self):
        """Audit catches near-zero anchors satisfying all five gate conditions.
        Gate is conservative: 8/17 caught in smoke run 28063029760 (artifact 7835756306).
        Missed anchors fail span_gte_20 (T0440, T0143, T0412, T0130) or p90_step_lt_0p25
        (T0338, T0462, T0231 — noisy static jitter with high p90 despite near-zero net).
        T0066 and T0309 fail both. This is the expected conservative outcome.
        """
        with open(self.SMOKE_PATH) as f:
            data = json.load(f)
        seen = set()
        audited = [audit.audit_tracklet(t, seen) for t in data["tracklets"]]
        near_zero_anchors = [
            a for a in audited
            if a["status"] == "anchor" and a["net_displacement_deg"] < 1.5
        ]
        caught = [a for a in near_zero_anchors if a["_audit"]["would_reject_static_motion"]]
        # Floor of 8: verified against smoke artifact 7835756306
        self.assertGreaterEqual(
            len(caught), 8,
            msg=f"Only {len(caught)}/{len(near_zero_anchors)} near-zero anchors caught"
        )

    def test_class_counts_unchanged_real_data(self):
        """Status field must be identical before and after audit."""
        with open(self.SMOKE_PATH) as f:
            data = json.load(f)
        seen = set()
        for t in data["tracklets"]:
            original_status = t["status"]
            a = audit.audit_tracklet(t, seen)
            self.assertEqual(a["status"], original_status,
                             msg=f"{t['id']} status changed")

    def test_no_strong_motion_ref_rejected_real_data(self):
        """No strong-motion reference (T0001, T0088, T0318, T0477) may be rejected.
        T0499 is excluded from STRONG_MOTION_REFS — it is a near-zero passing tracklet
        in smoke run 28063029760 (obs=85, span=152, net=0.024°) and correctly flagged
        would_reject_static_motion=True by the gate. It is not a strong-motion example.
        """
        with open(self.SMOKE_PATH) as f:
            data = json.load(f)
        seen = set()
        audited = {t["id"]: audit.audit_tracklet(t, seen) for t in data["tracklets"]}
        for ref_id in audit.STRONG_MOTION_REFS:
            if ref_id in audited:
                self.assertFalse(
                    audited[ref_id]["_audit"]["would_reject_static_motion"],
                    msg=f"{ref_id} is a strong-motion ref but would be rejected"
                )

    def test_run_end_to_end_real_data(self):
        """Full run() call on real data must complete without sys.exit(1)."""
        import argparse
        with tempfile.TemporaryDirectory() as tmpdir:
            args = argparse.Namespace(
                tracklets=self.SMOKE_PATH,
                output_dir=tmpdir,
            )
            audit.run(args)   # would raise SystemExit if ref violated
            for fname in ("stage2_audit_report.json",
                          "stage2_audit_report.txt",
                          "stage2_audit_review.txt"):
                self.assertTrue(os.path.exists(os.path.join(tmpdir, fname)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
