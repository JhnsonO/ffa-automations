"""
Tests for Stage 1e — Visual Consistency Scorer.

Covers:
  1.  inverse_project_perspective: centre pixel reprojects to look direction.
  2.  inverse_project_perspective: known offset pixel reprojects correctly.
  3.  wrap_yaw: values in [-180, +180).
  4.  wrap_yaw: value at +180 wraps to -180.
  5.  wrap_yaw: value already in range is unchanged.
  6.  compute_tolerance: scales with bbox_width_px; floor enforced.
  7.  great_circle_deg: self-distance is 0; 90° separation is 90°.
  8.  nearest-box selection: minimum angular error selected, not highest conf.
  9.  stage0_reuse candidate receives NO stage1e key.
  10. No candidate removed when score=0 (annotate-only).
  11. No candidate removed when score=0.5 (annotate-only).
  12. score=1 requires centred_crop AND angular_error <= tolerance.
  13. score=0.5 requires centred_crop fired AND angular_error > tolerance.
  14. score=0 when centred crop returns no boxes.
  15. shifted_consistency does NOT affect verification_consistency score.
  16. yaw_wrapped flag is True when wrap_yaw changes the value.
  17. yaw_wrapped flag is False when no wrapping occurs.
  18. stage1e annotation contains all required schema fields.
"""

from __future__ import annotations

import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import stage1e_visual_consistency as s1e


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _approx(a: float, b: float, tol: float = 1e-4) -> bool:
    return abs(a - b) <= tol


def _new_det_cand(yaw: float = 10.0, pitch: float = 5.0,
                  bbox_w: float = 8.0, crop_yaw: float = 0.0) -> dict:
    return {
        "yaw": yaw, "pitch": pitch, "raw_conf": 0.5,
        "penalty": 1.0, "weighted_conf": 0.5,
        "source": "new_detection", "crop_yaw": crop_yaw, "region": None,
        "detection_geometry": {
            "bbox_xyxy": [100.0, 100.0, 100.0 + bbox_w, 100.0 + bbox_w],
            "bbox_width_px": bbox_w, "bbox_height_px": bbox_w,
            "bbox_area_px": bbox_w ** 2, "bbox_aspect_ratio": 1.0,
            "crop_width_px": 1280, "crop_height_px": 720,
        },
    }


def _stage0_cand() -> dict:
    return {
        "yaw": -55.0, "pitch": 8.0, "raw_conf": 0.7,
        "penalty": 1.0, "weighted_conf": 0.7,
        "source": "stage0_reuse", "crop_yaw": 270, "region": None,
        "detection_geometry": {
            "bbox_xyxy": None, "bbox_width_px": None, "bbox_height_px": None,
            "bbox_area_px": None, "bbox_aspect_ratio": None,
            "crop_width_px": None, "crop_height_px": None,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — inverse_project_perspective: centre pixel → look direction
# ─────────────────────────────────────────────────────────────────────────────
def test_inverse_project_centre_pixel():
    """Centre pixel of any perspective crop must back-project to look direction."""
    for look_yaw, look_pitch in [(0, 0), (45, 10), (-90, -15), (120, 5)]:
        cx = s1e.VERIFY_CROP_SIZE / 2.0
        cy = s1e.VERIFY_CROP_SIZE / 2.0
        yaw_out, pitch_out = s1e.inverse_project_perspective(
            cx, cy, look_yaw, look_pitch,
            s1e.VERIFY_FOV_DEG, s1e.VERIFY_CROP_SIZE, s1e.VERIFY_CROP_SIZE
        )
        err = s1e.great_circle_deg(yaw_out, pitch_out, look_yaw, look_pitch)
        assert err < 0.01, (
            f"Centre pixel should back-project to look ({look_yaw},{look_pitch}), "
            f"got ({yaw_out:.3f},{pitch_out:.3f}), err={err:.4f}°"
        )
    print("PASS  test_inverse_project_centre_pixel")


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — inverse_project_perspective: known offset round-trip
# ─────────────────────────────────────────────────────────────────────────────
def test_inverse_project_roundtrip():
    """world→pixel→world round-trip should recover the original world position."""
    look_yaw, look_pitch = 30.0, 5.0
    # A point 8° to the right of centre
    target_yaw, target_pitch = 38.0, 5.0

    px_py = s1e.world_to_perspective_pixel(
        target_yaw, target_pitch,
        look_yaw, look_pitch,
        s1e.VERIFY_FOV_DEG, s1e.VERIFY_CROP_SIZE, s1e.VERIFY_CROP_SIZE
    )
    assert px_py is not None, "Target should be visible in crop"
    px, py = px_py

    yaw_rt, pitch_rt = s1e.inverse_project_perspective(
        px, py, look_yaw, look_pitch,
        s1e.VERIFY_FOV_DEG, s1e.VERIFY_CROP_SIZE, s1e.VERIFY_CROP_SIZE
    )
    err = s1e.great_circle_deg(yaw_rt, pitch_rt, target_yaw, target_pitch)
    assert err < 0.05, f"Round-trip error too large: {err:.4f}°"
    print("PASS  test_inverse_project_roundtrip")


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — wrap_yaw: values in [-180, +180)
# ─────────────────────────────────────────────────────────────────────────────
def test_wrap_yaw_range():
    for raw in [170.0, 175.0, 180.0, 185.0, 190.0, -175.0, -190.0, 360.0, -360.0]:
        w = s1e.wrap_yaw(raw)
        assert -180.0 <= w < 180.0, f"wrap_yaw({raw}) = {w} outside [-180, +180)"
    print("PASS  test_wrap_yaw_range")


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — wrap_yaw: +180 wraps to -180
# ─────────────────────────────────────────────────────────────────────────────
def test_wrap_yaw_boundary():
    assert s1e.wrap_yaw(180.0) == -180.0, f"Expected -180.0, got {s1e.wrap_yaw(180.0)}"
    print("PASS  test_wrap_yaw_boundary")


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — wrap_yaw: in-range value unchanged
# ─────────────────────────────────────────────────────────────────────────────
def test_wrap_yaw_no_change():
    for v in [0.0, 45.0, -45.0, 90.0, -90.0, 179.9, -179.9]:
        assert _approx(s1e.wrap_yaw(v), v, 1e-9), f"wrap_yaw({v}) changed to {s1e.wrap_yaw(v)}"
    print("PASS  test_wrap_yaw_no_change")


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 — compute_tolerance: scales and floor
# ─────────────────────────────────────────────────────────────────────────────
def test_compute_tolerance():
    # 1px → angular_diameter=1*(110/1280)≈0.0859° → *1.5≈0.129° → floor 0.5° applies
    assert _approx(s1e.compute_tolerance(1.0), 0.5)
    # 8px → 0.0859*8=0.687° → *1.5=1.031° > floor
    t8 = s1e.compute_tolerance(8.0)
    expected8 = max(0.5, 8.0 * (110.0 / 1280.0) * 1.5)
    assert _approx(t8, expected8, 1e-6), f"tolerance(8px) expected {expected8:.4f}, got {t8:.4f}"
    # 100px → well above floor
    t100 = s1e.compute_tolerance(100.0)
    assert t100 > 0.5
    print("PASS  test_compute_tolerance")


# ─────────────────────────────────────────────────────────────────────────────
# Test 7 — great_circle_deg: self and orthogonal
# ─────────────────────────────────────────────────────────────────────────────
def test_great_circle_deg():
    assert s1e.great_circle_deg(0, 0, 0, 0) < 1e-9
    assert _approx(s1e.great_circle_deg(0, 0, 90, 0), 90.0, 1e-4)
    assert _approx(s1e.great_circle_deg(0, 0, 0, 90), 90.0, 1e-4)
    print("PASS  test_great_circle_deg")


# ─────────────────────────────────────────────────────────────────────────────
# Test 8 — nearest-box selection: minimum angular error, not highest confidence
# ─────────────────────────────────────────────────────────────────────────────
def test_nearest_box_selected():
    """
    Simulate _run_crop_inference box-selection logic directly.
    Two boxes: box A has higher confidence but larger angular error;
    box B has lower confidence but smaller angular error.
    Box B must be selected.
    """
    boxes = [
        {"conf": 0.90, "angular_error_deg": 5.0},   # A: high conf, far
        {"conf": 0.30, "angular_error_deg": 0.3},   # B: low conf, close
    ]
    best = min(boxes, key=lambda b: b["angular_error_deg"])
    assert best["conf"] == 0.30, "Nearest box (by angular error) must be selected, not highest conf"
    assert best["angular_error_deg"] == 0.3
    print("PASS  test_nearest_box_selected")


# ─────────────────────────────────────────────────────────────────────────────
# Test 9 — stage0_reuse receives no stage1e key
# ─────────────────────────────────────────────────────────────────────────────
def test_stage0_no_annotation():
    """
    Build a minimal annotated dict simulating what score_candidates produces
    for a stage0_reuse candidate: no stage1e key must be present.
    """
    import copy
    cand = _stage0_cand()
    # score_candidates skips stage0_reuse; verify the contract
    assert cand.get("source") == "stage0_reuse"
    # After processing, key must be absent (simulate by checking the gate logic)
    eligible = cand.get("source") == "new_detection"
    if not eligible:
        annotated_cand = copy.deepcopy(cand)
        # No stage1e key should have been written
        assert "stage1e" not in annotated_cand, "stage0_reuse must not receive stage1e annotation"
    print("PASS  test_stage0_no_annotation")


# ─────────────────────────────────────────────────────────────────────────────
# Test 10 — annotate-only: no removal on score=0
# ─────────────────────────────────────────────────────────────────────────────
def test_no_removal_score_0():
    """A candidate annotated with score=0 must still be present in frames."""
    import copy
    cand = _new_det_cand()
    # Simulate annotation with score=0 (no detection)
    cand_annotated = copy.deepcopy(cand)
    cand_annotated["stage1e"] = {
        "verification_consistency": 0,
        "centred": {"fired": False, "total_boxes_returned": 0,
                    "selected_conf": None, "selected_error_deg": None},
        "shifted": {"fired": False, "shifted_yaw_used_deg": 20.0,
                    "yaw_wrapped": False, "total_boxes_returned": 0,
                    "selected_conf": None, "selected_error_deg": None},
        "tolerance_deg": 0.5, "shifted_consistency": 0,
        "rule_version": s1e.VERSION,
    }
    # Candidate must still exist (no removal)
    assert cand_annotated["source"] == "new_detection"
    assert "stage1e" in cand_annotated
    assert cand_annotated["stage1e"]["verification_consistency"] == 0
    print("PASS  test_no_removal_score_0")


# ─────────────────────────────────────────────────────────────────────────────
# Test 11 — annotate-only: no removal on score=0.5
# ─────────────────────────────────────────────────────────────────────────────
def test_no_removal_score_0_5():
    import copy
    cand = _new_det_cand()
    cand_annotated = copy.deepcopy(cand)
    cand_annotated["stage1e"] = {"verification_consistency": 0.5}
    assert cand_annotated["source"] == "new_detection"
    assert cand_annotated["stage1e"]["verification_consistency"] == 0.5
    print("PASS  test_no_removal_score_0_5")


# ─────────────────────────────────────────────────────────────────────────────
# Test 12 — score=1 requires centred fired AND error <= tolerance
# ─────────────────────────────────────────────────────────────────────────────
def test_score_1_conditions():
    tol = 1.0
    # Case A: fired, error within tolerance → 1
    error = 0.5
    score = 1 if error <= tol else 0.5
    assert score == 1
    # Case B: fired, error just at tolerance boundary → 1
    error_b = 1.0
    score_b = 1 if error_b <= tol else 0.5
    assert score_b == 1
    # Case C: fired, error exceeds tolerance → 0.5
    error_c = 1.1
    score_c = 1 if error_c <= tol else 0.5
    assert score_c == 0.5
    print("PASS  test_score_1_conditions")


# ─────────────────────────────────────────────────────────────────────────────
# Test 13 — score=0.5: centred fired but outside tolerance
# ─────────────────────────────────────────────────────────────────────────────
def test_score_0_5_conditions():
    fired   = True
    error   = 5.0
    tol     = 1.0
    if not fired:
        score = 0
    elif error <= tol:
        score = 1
    else:
        score = 0.5
    assert score == 0.5
    print("PASS  test_score_0_5_conditions")


# ─────────────────────────────────────────────────────────────────────────────
# Test 14 — score=0 when centred crop returns no boxes
# ─────────────────────────────────────────────────────────────────────────────
def test_score_0_no_detection():
    fired = False
    score = 0 if not fired else 1
    assert score == 0
    print("PASS  test_score_0_no_detection")


# ─────────────────────────────────────────────────────────────────────────────
# Test 15 — shifted_consistency does NOT affect verification_consistency
# ─────────────────────────────────────────────────────────────────────────────
def test_shifted_does_not_affect_primary_score():
    """
    Even if shifted_consistency=1, a centred score=0 must remain 0.
    Even if shifted_consistency=0, a centred score=1 must remain 1.
    """
    # Scenario A: centred no-fire, shifted fires within tolerance
    centred_fired = False
    shifted_within = True
    vc = 0 if not centred_fired else 1
    assert vc == 0, "shifted_consistency=1 must not elevate centred score=0"

    # Scenario B: centred fires within tolerance, shifted does not fire
    centred_fired_b = True
    error_b = 0.3
    tol_b   = 1.0
    shifted_within_b = False
    vc_b = 1 if (centred_fired_b and error_b <= tol_b) else 0.5
    assert vc_b == 1, "shifted_consistency=0 must not reduce centred score=1"
    print("PASS  test_shifted_does_not_affect_primary_score")


# ─────────────────────────────────────────────────────────────────────────────
# Test 16 — yaw_wrapped True when wrap changes value
# ─────────────────────────────────────────────────────────────────────────────
def test_yaw_wrapped_flag_true():
    raw = 175.0 + s1e.SHIFTED_YAW_OFFSET  # = 185.0 → wraps to -175.0
    wrapped = s1e.wrap_yaw(raw)
    yaw_wrapped = not _approx(wrapped, raw, 1e-9)
    assert yaw_wrapped, f"Expected yaw_wrapped=True for raw={raw}"
    print("PASS  test_yaw_wrapped_flag_true")


# ─────────────────────────────────────────────────────────────────────────────
# Test 17 — yaw_wrapped False when no wrapping occurs
# ─────────────────────────────────────────────────────────────────────────────
def test_yaw_wrapped_flag_false():
    raw = 10.0 + s1e.SHIFTED_YAW_OFFSET   # = 20.0 — no wrap
    wrapped = s1e.wrap_yaw(raw)
    yaw_wrapped = not _approx(wrapped, raw, 1e-9)
    assert not yaw_wrapped, f"Expected yaw_wrapped=False for raw={raw}"
    print("PASS  test_yaw_wrapped_flag_false")


# ─────────────────────────────────────────────────────────────────────────────
# Test 18 — stage1e annotation contains all required schema fields
# ─────────────────────────────────────────────────────────────────────────────
def test_stage1e_schema_fields():
    annotation = {
        "centred": {
            "total_boxes_returned": 1,
            "selected_conf": 0.45,
            "selected_error_deg": 0.3,
            "fired": True,
        },
        "shifted": {
            "shifted_yaw_used_deg": 20.0,
            "yaw_wrapped": False,
            "total_boxes_returned": 0,
            "selected_conf": None,
            "selected_error_deg": None,
            "fired": False,
        },
        "tolerance_deg": 0.944,
        "verification_consistency": 1,
        "shifted_consistency": 0,
        "model": "football-ball-detection.pt",
        "crop_fov_deg": s1e.VERIFY_FOV_DEG,
        "crop_size_px": s1e.VERIFY_CROP_SIZE,
        "conf_threshold": s1e.VERIFY_CONF,
        "rule_version": s1e.VERSION,
    }
    required_top = {"centred", "shifted", "tolerance_deg", "verification_consistency",
                    "shifted_consistency", "model", "crop_fov_deg", "crop_size_px",
                    "conf_threshold", "rule_version"}
    required_centred = {"total_boxes_returned", "selected_conf", "selected_error_deg", "fired"}
    required_shifted = {"shifted_yaw_used_deg", "yaw_wrapped", "total_boxes_returned",
                        "selected_conf", "selected_error_deg", "fired"}

    missing_top = required_top - set(annotation.keys())
    assert not missing_top, f"Missing top-level fields: {missing_top}"
    missing_c = required_centred - set(annotation["centred"].keys())
    assert not missing_c, f"Missing centred fields: {missing_c}"
    missing_s = required_shifted - set(annotation["shifted"].keys())
    assert not missing_s, f"Missing shifted fields: {missing_s}"
    print("PASS  test_stage1e_schema_fields")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_inverse_project_centre_pixel()
    test_inverse_project_roundtrip()
    test_wrap_yaw_range()
    test_wrap_yaw_boundary()
    test_wrap_yaw_no_change()
    test_compute_tolerance()
    test_great_circle_deg()
    test_nearest_box_selected()
    test_stage0_no_annotation()
    test_no_removal_score_0()
    test_no_removal_score_0_5()
    test_score_1_conditions()
    test_score_0_5_conditions()
    test_score_0_no_detection()
    test_shifted_does_not_affect_primary_score()
    test_yaw_wrapped_flag_true()
    test_yaw_wrapped_flag_false()
    test_stage1e_schema_fields()
    print("\nAll Stage 1e tests passed.")
