"""
Tests for Stage 1d — Geometry Quarantine Filter.

Covers:
  1. new_detection with area > 100 is rejected (area rule).
  2. new_detection with AR > 1.25 is rejected (AR rule).
  3. new_detection with both violations is rejected with both reasons annotated.
  4. new_detection within bounds passes through.
  5. stage0_reuse with null geometry always passes (no filtering).
  6. Rejected candidates appear in geometry_quarantined_candidates, not frames.
  7. Stage 1b quarantined_candidates is preserved untouched.
  8. Output schema is a strict superset of input schema (stage1d key added).
  9. frames_newly_zero_candidate increments when all frame candidates are rejected.
 10. Null-geometry new_detection (malformed) passes through unchanged.
 11. Default thresholds match specification (area=100, AR=1.25).
 12. Custom threshold values are honoured.
 13. geometry_quarantine annotation has required fields.
 14. stage0_reuse count matches input regardless of geometry values.
"""

from __future__ import annotations

import copy
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import stage1d_geometry_filter as s1d


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _geo(area: float, ar: float) -> dict:
    w = (area * ar) ** 0.5
    h = area / w
    return {
        "bbox_xyxy": [0.0, 0.0, w, h],
        "bbox_width_px": round(w, 4),
        "bbox_height_px": round(h, 4),
        "bbox_area_px": round(area, 4),
        "bbox_aspect_ratio": round(ar, 4),
        "crop_width_px": 1280,
        "crop_height_px": 720,
    }


def _null_geo() -> dict:
    return {
        "bbox_xyxy": None,
        "bbox_width_px": None,
        "bbox_height_px": None,
        "bbox_area_px": None,
        "bbox_aspect_ratio": None,
        "crop_width_px": None,
        "crop_height_px": None,
    }


def _new_det(area: float = 30.0, ar: float = 1.0) -> dict:
    return {
        "yaw": 10.0, "pitch": 5.0, "raw_conf": 0.5,
        "penalty": 1.0, "weighted_conf": 0.5,
        "source": "new_detection", "crop_yaw": 0, "region": None,
        "detection_geometry": _geo(area, ar),
    }


def _stage0() -> dict:
    return {
        "yaw": -55.0, "pitch": 8.0, "raw_conf": 0.7,
        "penalty": 1.0, "weighted_conf": 0.7,
        "source": "stage0_reuse", "crop_yaw": 270, "region": None,
        "detection_geometry": _null_geo(),
    }


def _wrap(*candidates) -> dict:
    """Wrap candidates in a minimal Stage 1b-compatible document."""
    return {
        "fps": 30.0,
        "total_frames": 10,
        "pitch_min_deg": -30.0,
        "pitch_max_deg": 18.0,
        "hotspot_map": "hotspot_map.json",
        "stage0_detections": "stage0.json",
        "frames": {"0": list(candidates)},
        "quarantined_candidates": {"5": [copy.deepcopy(_new_det(10.0))]},
        "stage1b": {"version": "stage1b_static_quarantine_v1"},
    }


def _run(doc: dict, **kwargs) -> tuple[dict, dict]:
    return s1d.apply_geometry_filter(doc, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_area_rule_rejects():
    doc = _wrap(_new_det(area=101.0, ar=1.0))
    out, report = _run(doc)
    assert out["frames"]["0"] == [], "Over-area candidate must not be in frames"
    geo_q = out["geometry_quarantined_candidates"]
    assert "0" in geo_q and len(geo_q["0"]) == 1
    reasons = geo_q["0"][0]["geometry_quarantine"]["reasons"]
    assert any("bbox_area_px" in r for r in reasons), f"Expected area reason: {reasons}"
    assert report["summary"]["candidates_geo_quarantined"] == 1
    print("PASS  test_area_rule_rejects")


def test_ar_rule_rejects():
    doc = _wrap(_new_det(area=30.0, ar=1.26))
    out, report = _run(doc)
    assert out["frames"]["0"] == []
    geo_q = out["geometry_quarantined_candidates"]
    reasons = geo_q["0"][0]["geometry_quarantine"]["reasons"]
    assert any("bbox_aspect_ratio" in r for r in reasons), f"Expected AR reason: {reasons}"
    assert report["summary"]["candidates_geo_quarantined"] == 1
    print("PASS  test_ar_rule_rejects")


def test_both_rules_annotated():
    doc = _wrap(_new_det(area=200.0, ar=1.50))
    out, _ = _run(doc)
    geo_q = out["geometry_quarantined_candidates"]
    reasons = geo_q["0"][0]["geometry_quarantine"]["reasons"]
    assert len(reasons) == 2, f"Expected 2 reasons, got {reasons}"
    assert any("bbox_area_px" in r for r in reasons)
    assert any("bbox_aspect_ratio" in r for r in reasons)
    print("PASS  test_both_rules_annotated")


def test_passing_candidate_stays_in_frames():
    doc = _wrap(_new_det(area=50.0, ar=1.10))
    out, report = _run(doc)
    assert len(out["frames"]["0"]) == 1
    assert "geometry_quarantined_candidates" in out
    assert out["geometry_quarantined_candidates"].get("0") is None or \
           out["geometry_quarantined_candidates"].get("0") == []
    assert report["summary"]["candidates_active"] == 1
    assert report["summary"]["candidates_geo_quarantined"] == 0
    print("PASS  test_passing_candidate_stays_in_frames")


def test_stage0_reuse_always_passes():
    doc = _wrap(_stage0())
    out, report = _run(doc)
    assert len(out["frames"]["0"]) == 1, "stage0_reuse must not be filtered"
    assert report["summary"]["stage0_reuse_unchanged"] == 1
    assert report["summary"]["candidates_geo_quarantined"] == 0
    print("PASS  test_stage0_reuse_always_passes")


def test_rejected_in_geo_quarantined_not_frames():
    doc = _wrap(_new_det(area=300.0, ar=1.0))
    out, _ = _run(doc)
    assert out["frames"]["0"] == []
    assert "0" in out["geometry_quarantined_candidates"]
    assert len(out["geometry_quarantined_candidates"]["0"]) == 1
    print("PASS  test_rejected_in_geo_quarantined_not_frames")


def test_stage1b_quarantined_preserved():
    doc = _wrap(_new_det())
    original_q = copy.deepcopy(doc["quarantined_candidates"])
    out, _ = _run(doc)
    assert out["quarantined_candidates"] == original_q, \
        "Stage 1b quarantined_candidates must not be modified"
    print("PASS  test_stage1b_quarantined_preserved")


def test_output_schema_superset():
    doc = _wrap(_new_det())
    out, _ = _run(doc)
    for key in ["fps", "total_frames", "frames", "quarantined_candidates",
                "stage1b", "geometry_quarantined_candidates", "stage1d"]:
        assert key in out, f"Missing expected key: {key}"
    assert out["stage1d"]["version"] == s1d.VERSION
    print("PASS  test_output_schema_superset")


def test_frames_newly_zero_increments():
    doc = _wrap(_new_det(area=500.0))
    out, report = _run(doc)
    assert report["summary"]["frames_newly_zero_candidate"] == 1
    print("PASS  test_frames_newly_zero_increments")


def test_null_geometry_new_detection_passes():
    """Malformed new_detection with missing geometry block passes through."""
    candidate = {
        "yaw": 10.0, "pitch": 5.0, "raw_conf": 0.5,
        "penalty": 1.0, "weighted_conf": 0.5,
        "source": "new_detection", "crop_yaw": 0, "region": None,
        # no detection_geometry key at all
    }
    doc = _wrap(candidate)
    out, report = _run(doc)
    assert len(out["frames"]["0"]) == 1, "Null-geo new_detection must pass through"
    assert report["summary"]["candidates_geo_quarantined"] == 0
    print("PASS  test_null_geometry_new_detection_passes")


def test_default_thresholds():
    assert s1d.DEFAULT_AREA_MAX_PX == 100.0
    assert s1d.DEFAULT_AR_MAX == 1.25
    print("PASS  test_default_thresholds")


def test_custom_thresholds_honoured():
    # With area_max=200 the 150px² candidate should pass
    doc = _wrap(_new_det(area=150.0, ar=1.0))
    out, report = _run(doc, area_max_px=200.0, ar_max=1.25)
    assert len(out["frames"]["0"]) == 1
    assert report["summary"]["candidates_geo_quarantined"] == 0

    # With area_max=100 (default) same candidate should fail
    out2, report2 = _run(doc, area_max_px=100.0, ar_max=1.25)
    assert out2["frames"]["0"] == []
    assert report2["summary"]["candidates_geo_quarantined"] == 1
    print("PASS  test_custom_thresholds_honoured")


def test_geometry_quarantine_annotation_fields():
    doc = _wrap(_new_det(area=200.0, ar=1.0))
    out, _ = _run(doc)
    annotation = out["geometry_quarantined_candidates"]["0"][0]["geometry_quarantine"]
    for field in ["reasons", "rule_version", "area_max_px", "ar_max"]:
        assert field in annotation, f"Missing annotation field: {field}"
    assert annotation["rule_version"] == s1d.VERSION
    print("PASS  test_geometry_quarantine_annotation_fields")


def test_stage0_count_invariant():
    """stage0_reuse_unchanged == number of stage0_reuse candidates in input."""
    s0_count = 4
    candidates = [_stage0() for _ in range(s0_count)] + [_new_det(50.0)]
    doc = {
        "fps": 30.0, "total_frames": 10,
        "pitch_min_deg": -30.0, "pitch_max_deg": 18.0,
        "hotspot_map": "", "stage0_detections": "",
        "frames": {"0": candidates},
        "quarantined_candidates": {},
        "stage1b": {},
    }
    _, report = _run(doc)
    assert report["summary"]["stage0_reuse_unchanged"] == s0_count
    print("PASS  test_stage0_count_invariant")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_area_rule_rejects()
    test_ar_rule_rejects()
    test_both_rules_annotated()
    test_passing_candidate_stays_in_frames()
    test_stage0_reuse_always_passes()
    test_rejected_in_geo_quarantined_not_frames()
    test_stage1b_quarantined_preserved()
    test_output_schema_superset()
    test_frames_newly_zero_increments()
    test_null_geometry_new_detection_passes()
    test_default_thresholds()
    test_custom_thresholds_honoured()
    test_geometry_quarantine_annotation_fields()
    test_stage0_count_invariant()
    print("\nAll Stage 1d geometry filter tests passed.")
