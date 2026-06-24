#!/usr/bin/env python3
"""
Fixture tests for stage2_tier_a_dry_run_compare.py continuity logic.

Covers:
  1. frame-only support must fail continuity (outcome: frame_only_unsupported)
  2. spatial support passes continuity (outcome: spatial_or_linked_continuous)
  3. linked-tracklet support passes continuity (outcome: spatial_or_linked_continuous)
  4. no support fails continuity (outcome: no_support)
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from stage2_tier_a_dry_run_compare import (
    _check_continuity,
    OUTCOME_SPATIAL_OR_LINKED,
    OUTCOME_FRAME_ONLY,
    OUTCOME_NO_SUPPORT,
    SPATIAL_TOL_DEG,
)
from collections import defaultdict


# ---------------------------------------------------------------------------
# Helpers to build minimal fixtures
# ---------------------------------------------------------------------------

def make_tracklet(start, end, yaw, pitch, status="anchor", net_disp=10.0):
    obs = [{"yaw": yaw, "pitch": pitch}]
    return {
        "id": "T_TEST",
        "status": status,
        "start_frame": start,
        "end_frame": end,
        "net_displacement_deg": net_disp,
        "observations": obs,
    }


def make_cand_index(frame, yaw, pitch):
    """Single-candidate dry-run index."""
    idx = defaultdict(list)
    idx[frame].append((yaw, pitch))
    return idx


def make_dry_tracklet(start, end, yaw, pitch):
    return {
        "id": "DT_TEST",
        "status": "anchor",
        "start_frame": start,
        "end_frame": end,
        "net_displacement_deg": 5.0,
        "observations": [{"yaw": yaw, "pitch": pitch}],
    }


# ---------------------------------------------------------------------------
# Test 1: frame-only support → NOT continuous, outcome = frame_only_unsupported
# ---------------------------------------------------------------------------
def test_frame_only_fails_continuity():
    """
    Dry-run has a candidate in the frame window, but it is spatially far
    from the original median. No linked tracklet. Must be frame_only_unsupported.
    """
    orig_t = make_tracklet(start=100, end=200, yaw=45.0, pitch=10.0)

    # Candidate in-range but far away (>SPATIAL_TOL_DEG)
    far_yaw = 45.0 + SPATIAL_TOL_DEG + 5.0  # clearly outside tolerance
    cand_index = make_cand_index(frame=150, yaw=far_yaw, pitch=10.0)

    result = _check_continuity(orig_t, cand_index, dry_tracklets=[], spatial_tol=SPATIAL_TOL_DEG)

    assert result["has_frame_support"]   is True,  "Expected frame support"
    assert result["has_spatial_support"] is False, "Expected no spatial support"
    assert result["has_linked_support"]  is False, "Expected no linked support"
    assert result["is_continuous"]       is False, "Frame-only must NOT be continuous"
    assert result["outcome"] == OUTCOME_FRAME_ONLY, \
        f"Expected {OUTCOME_FRAME_ONLY}, got {result['outcome']}"
    print("PASS test_frame_only_fails_continuity")


# ---------------------------------------------------------------------------
# Test 2: spatial support → continuous, outcome = spatial_or_linked_continuous
# ---------------------------------------------------------------------------
def test_spatial_support_passes():
    """
    Dry-run has a candidate within SPATIAL_TOL_DEG of the original median.
    Must be spatial_or_linked_continuous.
    """
    orig_t = make_tracklet(start=100, end=200, yaw=45.0, pitch=10.0)

    # Candidate close enough
    close_yaw = 45.0 + SPATIAL_TOL_DEG * 0.5
    cand_index = make_cand_index(frame=150, yaw=close_yaw, pitch=10.0)

    result = _check_continuity(orig_t, cand_index, dry_tracklets=[], spatial_tol=SPATIAL_TOL_DEG)

    assert result["has_frame_support"]   is True,  "Expected frame support"
    assert result["has_spatial_support"] is True,  "Expected spatial support"
    assert result["is_continuous"]       is True,  "Spatial support must be continuous"
    assert result["outcome"] == OUTCOME_SPATIAL_OR_LINKED, \
        f"Expected {OUTCOME_SPATIAL_OR_LINKED}, got {result['outcome']}"
    print("PASS test_spatial_support_passes")


# ---------------------------------------------------------------------------
# Test 3: linked support only (no spatial candidate) → continuous
# ---------------------------------------------------------------------------
def test_linked_support_passes():
    """
    No dry-run candidates in the frame window. A dry-run tracklet overlaps
    the window and is within SPATIAL_TOL_DEG of the original median.
    Must be spatial_or_linked_continuous.
    """
    orig_t = make_tracklet(start=100, end=200, yaw=45.0, pitch=10.0)

    empty_index = defaultdict(list)  # no candidates in range

    # Linked tracklet overlapping the window, spatially close
    linked_t = make_dry_tracklet(start=120, end=180, yaw=45.0 + SPATIAL_TOL_DEG * 0.4, pitch=10.0)

    result = _check_continuity(orig_t, empty_index, dry_tracklets=[linked_t], spatial_tol=SPATIAL_TOL_DEG)

    assert result["has_frame_support"]   is False, "Expected no frame support"
    assert result["has_spatial_support"] is False, "Expected no spatial support"
    assert result["has_linked_support"]  is True,  "Expected linked support"
    assert result["is_continuous"]       is True,  "Linked support must be continuous"
    assert result["outcome"] == OUTCOME_SPATIAL_OR_LINKED, \
        f"Expected {OUTCOME_SPATIAL_OR_LINKED}, got {result['outcome']}"
    print("PASS test_linked_support_passes")


# ---------------------------------------------------------------------------
# Test 4: no support at all → not continuous, outcome = no_support
# ---------------------------------------------------------------------------
def test_no_support_fails():
    """
    No dry-run candidates in the frame window. No overlapping linked tracklet.
    Must be no_support.
    """
    orig_t = make_tracklet(start=100, end=200, yaw=45.0, pitch=10.0)

    empty_index = defaultdict(list)

    # Tracklet does NOT overlap the window
    non_overlapping_t = make_dry_tracklet(start=300, end=400, yaw=45.0, pitch=10.0)

    result = _check_continuity(orig_t, empty_index, dry_tracklets=[non_overlapping_t], spatial_tol=SPATIAL_TOL_DEG)

    assert result["has_frame_support"]   is False, "Expected no frame support"
    assert result["has_spatial_support"] is False, "Expected no spatial support"
    assert result["has_linked_support"]  is False, "Expected no linked support"
    assert result["is_continuous"]       is False, "No support must NOT be continuous"
    assert result["outcome"] == OUTCOME_NO_SUPPORT, \
        f"Expected {OUTCOME_NO_SUPPORT}, got {result['outcome']}"
    print("PASS test_no_support_fails")


# ---------------------------------------------------------------------------
# Test 5: frame-only even when linked tracklet is far (spatial check enforced)
# ---------------------------------------------------------------------------
def test_linked_far_still_frame_only():
    """
    Candidate in range but spatially far. Linked tracklet overlaps but is also
    spatially far. Must be frame_only_unsupported (frame support present, but
    linked support is spatial-gated and fails).
    """
    orig_t = make_tracklet(start=100, end=200, yaw=45.0, pitch=10.0)

    far_yaw = 45.0 + SPATIAL_TOL_DEG + 5.0
    cand_index = make_cand_index(frame=150, yaw=far_yaw, pitch=10.0)

    # Linked tracklet overlaps but is also far
    far_linked_t = make_dry_tracklet(start=120, end=180, yaw=far_yaw, pitch=10.0)

    result = _check_continuity(orig_t, cand_index, dry_tracklets=[far_linked_t], spatial_tol=SPATIAL_TOL_DEG)

    assert result["has_frame_support"]   is True,  "Expected frame support"
    assert result["has_spatial_support"] is False, "Expected no spatial support"
    assert result["has_linked_support"]  is False, "Expected no linked support (too far)"
    assert result["is_continuous"]       is False, "Must not be continuous"
    assert result["outcome"] == OUTCOME_FRAME_ONLY, \
        f"Expected {OUTCOME_FRAME_ONLY}, got {result['outcome']}"
    print("PASS test_linked_far_still_frame_only")


if __name__ == "__main__":
    test_frame_only_fails_continuity()
    test_spatial_support_passes()
    test_linked_support_passes()
    test_no_support_fails()
    test_linked_far_still_frame_only()
    print("\nAll 5 fixture tests PASSED")
