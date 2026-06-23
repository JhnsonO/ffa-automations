"""
Tests for Stage 1c — detection_geometry metadata preservation.

Covers:
  1. New detection has correct geometry populated.
  2. Stage 0 reused candidate has explicit null geometry (all fields null).
  3. Old Stage 1 candidate file without detection_geometry still loads cleanly
     (backward-compat: missing key does not raise).
"""

import json
import math
import sys
import os
import tempfile

# ── Minimal hotspot-map fixture ───────────────────────────────────────────────
HOTSPOT_MAP = {
    "sphere_bin_deg":      5.0,
    "low_duty_floor":      0.1,
    "duty_cycle_threshold": 0.6,
    "penalty_min":         0.05,
    "bins":                [],
    "hotspot_regions":     [],
}


def _make_hm():
    hm = HOTSPOT_MAP.copy()
    bin_lookup = {}
    return hm, bin_lookup


# ── Import helpers from stage1 without importing ultralytics ─────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import importlib, types

# Stub ultralytics before import so the module loads in CI without the package
ul_stub = types.ModuleType("ultralytics")
ul_stub.YOLO = object
sys.modules.setdefault("ultralytics", ul_stub)

import stage1_candidate_gen as s1


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 — new detection geometry is retained
# ─────────────────────────────────────────────────────────────────────────────
def test_new_detection_geometry_retained():
    hm, bin_lookup = _make_hm()
    x1, y1, x2, y2 = 100.0, 200.0, 140.0, 240.0
    crop_w, crop_h = 1280, 720
    geom = s1._make_detection_geometry(x1, y1, x2, y2, crop_w, crop_h)

    cand = s1.process_candidate(
        yaw=10.0, pitch=5.0, raw_conf=0.5,
        source="new_detection", crop_yaw=0,
        hm=hm, bin_lookup=bin_lookup,
        pitch_min=-30.0, pitch_max=18.0,
        detection_geometry=geom,
    )
    assert cand is not None, "Candidate should not be pitch-rejected"
    dg = cand["detection_geometry"]
    assert dg is not None, "detection_geometry must be present"

    # bbox_xyxy
    assert dg["bbox_xyxy"] == [100.0, 200.0, 140.0, 240.0], \
        f"bbox_xyxy mismatch: {dg['bbox_xyxy']}"

    # derived scalars
    assert abs(dg["bbox_width_px"]  - 40.0) < 1e-6, f"width wrong: {dg['bbox_width_px']}"
    assert abs(dg["bbox_height_px"] - 40.0) < 1e-6, f"height wrong: {dg['bbox_height_px']}"
    assert abs(dg["bbox_area_px"]   - 1600.0) < 1e-6, f"area wrong: {dg['bbox_area_px']}"
    assert abs(dg["bbox_aspect_ratio"] - 1.0) < 1e-4, f"aspect wrong: {dg['bbox_aspect_ratio']}"

    # crop dims
    assert dg["crop_width_px"]  == 1280, f"crop_w wrong: {dg['crop_width_px']}"
    assert dg["crop_height_px"] == 720,  f"crop_h wrong: {dg['crop_height_px']}"

    # All expected keys present
    expected_keys = {
        "bbox_xyxy", "bbox_width_px", "bbox_height_px",
        "bbox_area_px", "bbox_aspect_ratio",
        "crop_width_px", "crop_height_px",
    }
    assert expected_keys == set(dg.keys()), \
        f"Key mismatch: {set(dg.keys())} vs {expected_keys}"

    print("PASS  test_new_detection_geometry_retained")


# ─────────────────────────────────────────────────────────────────────────────
# Test 2 — Stage 0 reused candidate has explicit null geometry
# ─────────────────────────────────────────────────────────────────────────────
def test_stage0_reuse_null_geometry():
    hm, bin_lookup = _make_hm()
    geom = s1._null_detection_geometry()

    cand = s1.process_candidate(
        yaw=20.0, pitch=3.0, raw_conf=0.4,
        source="stage0_reuse", crop_yaw=90,
        hm=hm, bin_lookup=bin_lookup,
        pitch_min=-30.0, pitch_max=18.0,
        detection_geometry=geom,
    )
    assert cand is not None, "Candidate should not be pitch-rejected"
    dg = cand["detection_geometry"]
    assert dg is not None, "detection_geometry key must exist even for stage0_reuse"

    null_fields = [
        "bbox_xyxy", "bbox_width_px", "bbox_height_px",
        "bbox_area_px", "bbox_aspect_ratio",
        "crop_width_px", "crop_height_px",
    ]
    for field in null_fields:
        assert field in dg, f"Missing field: {field}"
        assert dg[field] is None, \
            f"Field '{field}' should be None for stage0_reuse, got {dg[field]!r}"

    print("PASS  test_stage0_reuse_null_geometry")


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — Old Stage 1 file without detection_geometry loads cleanly
# ─────────────────────────────────────────────────────────────────────────────
def test_old_stage1_file_backward_compat():
    """
    Simulate loading a stage1_candidates.json written before Stage 1c.
    The consumer should be able to call .get('detection_geometry') safely
    and the field simply won't be present — no KeyError, no crash.
    """
    old_candidate = {
        "yaw":           12.5,
        "pitch":         4.0,
        "raw_conf":      0.35,
        "penalty":       1.0,
        "weighted_conf": 0.35,
        "source":        "new_detection",
        "crop_yaw":      0,
        "region":        None,
        # NOTE: no "detection_geometry" key — old file format
    }

    old_file = {
        "fps": 30.0,
        "total_frames": 100,
        "pitch_min_deg": -30.0,
        "pitch_max_deg": 18.0,
        "hotspot_map": "hotspot_map.json",
        "stage0_detections": "stage0_detections.json",
        "frames": {"0": [old_candidate]},
    }

    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
        json.dump(old_file, fh)
        tmp_path = fh.name

    try:
        with open(tmp_path) as fh:
            loaded = json.load(fh)

        frames = {int(k): v for k, v in loaded["frames"].items()}
        cand = frames[0][0]

        # Must not raise; .get() returns None for missing key
        dg = cand.get("detection_geometry")
        assert dg is None, f"Expected None for missing key, got {dg!r}"

        # Existing fields must be intact
        assert cand["yaw"]   == 12.5
        assert cand["pitch"] == 4.0
        assert cand["source"] == "new_detection"

    finally:
        os.unlink(tmp_path)

    print("PASS  test_old_stage1_file_backward_compat")


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — _make_detection_geometry correctness with non-square box
# ─────────────────────────────────────────────────────────────────────────────
def test_detection_geometry_non_square():
    geom = s1._make_detection_geometry(10.0, 20.0, 50.0, 60.0, 1280, 720)
    assert abs(geom["bbox_width_px"]  - 40.0) < 1e-6
    assert abs(geom["bbox_height_px"] - 40.0) < 1e-6
    assert abs(geom["bbox_area_px"]   - 1600.0) < 1e-6
    assert abs(geom["bbox_aspect_ratio"] - 1.0) < 1e-4

    # Rectangular box
    geom2 = s1._make_detection_geometry(0.0, 0.0, 60.0, 30.0, 1280, 720)
    assert abs(geom2["bbox_width_px"]  - 60.0) < 1e-6
    assert abs(geom2["bbox_height_px"] - 30.0) < 1e-6
    assert abs(geom2["bbox_area_px"]   - 1800.0) < 1e-6
    assert abs(geom2["bbox_aspect_ratio"] - 2.0) < 1e-4

    print("PASS  test_detection_geometry_non_square")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    test_new_detection_geometry_retained()
    test_stage0_reuse_null_geometry()
    test_old_stage1_file_backward_compat()
    test_detection_geometry_non_square()
    print("\nAll Stage 1c geometry tests passed.")
