#!/usr/bin/env python3
"""
FFA Offline Recovery Pipeline — Stage 1d: Geometry Quarantine Filter
=====================================================================

Purpose
-------
Apply a reversible geometry-based post-filter to active candidates produced
by Stage 1b (confirmed-static quarantine).

Only candidates with ``source == "new_detection"`` carry detection_geometry
and are eligible for filtering.  Candidates with ``source == "stage0_reuse"``
(null geometry) are always passed through unchanged.

Rejection rules (applied independently; any match quarantines the candidate):
  - bbox_area_px  > AREA_MAX_PX   (default 100)
  - bbox_aspect_ratio > AR_MAX    (default 1.25)

All rejected candidates are moved from ``frames`` to a new top-level key
``geometry_quarantined_candidates``, annotated with one or more reason strings.
The ``frames`` and existing ``quarantined_candidates`` (Stage 1b) collections
are not otherwise modified.

Output schema is a strict superset of the Stage 1b output schema: any consumer
that reads ``frames`` and ``quarantined_candidates`` continues to work.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
from datetime import datetime, timezone
from typing import Any

VERSION = "stage1d_geometry_filter_v1"

DEFAULT_AREA_MAX_PX: float = 100.0
DEFAULT_AR_MAX: float = 1.25


# ─────────────────────────────────────────────────────────────────────────────
# Core logic
# ─────────────────────────────────────────────────────────────────────────────

def _geometry_rejection_reasons(
    candidate: dict[str, Any],
    area_max_px: float,
    ar_max: float,
) -> list[str]:
    """Return list of rejection reason strings for a candidate (empty = pass)."""
    if candidate.get("source") != "new_detection":
        return []

    dg = candidate.get("detection_geometry")
    if dg is None:
        # Malformed new_detection with no geometry block — pass through,
        # do not attempt to filter.
        return []

    reasons: list[str] = []

    area = dg.get("bbox_area_px")
    if area is not None and area > area_max_px:
        reasons.append(f"bbox_area_px>{area_max_px:.1f} (actual={area:.2f})")

    ar = dg.get("bbox_aspect_ratio")
    if ar is not None and ar > ar_max:
        reasons.append(f"bbox_aspect_ratio>{ar_max:.4f} (actual={ar:.4f})")

    return reasons


def apply_geometry_filter(
    stage1_quarantined: dict[str, Any],
    area_max_px: float = DEFAULT_AREA_MAX_PX,
    ar_max: float = DEFAULT_AR_MAX,
    input_file_id: str = "",
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Filter active candidates by geometry rules.

    Parameters
    ----------
    stage1_quarantined : dict
        The full Stage 1b output JSON (frames + quarantined_candidates + stage1b).
    area_max_px : float
        Reject new_detection candidates whose bbox_area_px exceeds this value.
    ar_max : float
        Reject new_detection candidates whose bbox_aspect_ratio exceeds this value.
    input_file_id : str
        Identifier of the input file, written into the audit block.

    Returns
    -------
    output : dict
        Modified Stage 1 dict with filtered frames and geometry_quarantined_candidates.
    report : dict
        Standalone audit report for this stage.
    """
    frames_raw = stage1_quarantined.get("frames", {})

    active_frames: dict[str, list[dict[str, Any]]] = {}
    geo_quarantined: dict[str, list[dict[str, Any]]] = {}

    counts = {
        "candidates_before": 0,
        "candidates_active": 0,
        "candidates_geo_quarantined": 0,
        "new_detection_before": 0,
        "new_detection_active": 0,
        "new_detection_geo_quarantined": 0,
        "stage0_reuse_unchanged": 0,
        "frames_total": len(frames_raw),
        "frames_newly_zero_candidate": 0,
        "rejected_by_area_only": 0,
        "rejected_by_ar_only": 0,
        "rejected_by_both": 0,
    }

    for frame_key, candidates in frames_raw.items():
        if not isinstance(candidates, list):
            active_frames[str(frame_key)] = candidates
            continue

        active: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []

        had_candidates = bool(candidates)

        for candidate in candidates:
            counts["candidates_before"] += 1
            source = candidate.get("source")
            if source == "new_detection":
                counts["new_detection_before"] += 1
            elif source == "stage0_reuse":
                counts["stage0_reuse_unchanged"] += 1

            reasons = _geometry_rejection_reasons(candidate, area_max_px, ar_max)

            if not reasons:
                active.append(copy.deepcopy(candidate))
                counts["candidates_active"] += 1
                if source == "new_detection":
                    counts["new_detection_active"] += 1
            else:
                annotated = copy.deepcopy(candidate)
                annotated["geometry_quarantine"] = {
                    "reasons": reasons,
                    "rule_version": VERSION,
                    "area_max_px": area_max_px,
                    "ar_max": ar_max,
                }
                rejected.append(annotated)
                counts["candidates_geo_quarantined"] += 1
                if source == "new_detection":
                    counts["new_detection_geo_quarantined"] += 1

                # Tally rule breakdown
                has_area = any("bbox_area_px" in r for r in reasons)
                has_ar   = any("bbox_aspect_ratio" in r for r in reasons)
                if has_area and has_ar:
                    counts["rejected_by_both"] += 1
                elif has_area:
                    counts["rejected_by_area_only"] += 1
                elif has_ar:
                    counts["rejected_by_ar_only"] += 1

        active_frames[str(frame_key)] = active
        if rejected:
            geo_quarantined[str(frame_key)] = rejected

        if had_candidates and not active:
            counts["frames_newly_zero_candidate"] += 1

    created_utc = datetime.now(timezone.utc).isoformat()

    # Build output as superset of input schema
    output = {
        key: copy.deepcopy(value)
        for key, value in stage1_quarantined.items()
        if key not in {"frames", "geometry_quarantined_candidates", "stage1d"}
    }
    output["frames"] = active_frames
    # Preserve existing Stage 1b quarantined_candidates untouched
    if "quarantined_candidates" in stage1_quarantined:
        output["quarantined_candidates"] = copy.deepcopy(
            stage1_quarantined["quarantined_candidates"]
        )
    output["geometry_quarantined_candidates"] = geo_quarantined
    output["stage1d"] = {
        "version": VERSION,
        "created_utc": created_utc,
        "input_file_id": input_file_id,
        "rules": {
            "area_max_px": area_max_px,
            "ar_max": ar_max,
            "applies_to": "new_detection",
            "null_geometry_action": "pass_through",
        },
        "counts": counts,
    }

    report = {
        "version": VERSION,
        "created_utc": created_utc,
        "input": {"file_id": input_file_id},
        "rules": output["stage1d"]["rules"],
        "summary": counts,
    }

    return output, report


# ─────────────────────────────────────────────────────────────────────────────
# Text report
# ─────────────────────────────────────────────────────────────────────────────

def text_report(report: dict[str, Any]) -> str:
    s = report["summary"]
    r = report["rules"]
    lines = [
        "=" * 70,
        "STAGE 1D — GEOMETRY QUARANTINE FILTER — REPORT",
        "=" * 70,
        f"Rule: bbox_area_px > {r['area_max_px']} OR bbox_aspect_ratio > {r['ar_max']}",
        f"Applies to: {r['applies_to']} only  |  null-geometry: {r['null_geometry_action']}",
        "",
        "CANDIDATE COUNTS",
        "-" * 70,
        f"Candidates before (active from Stage 1b): {s['candidates_before']}",
        f"  new_detection :  {s['new_detection_before']}",
        f"  stage0_reuse  :  {s['stage0_reuse_unchanged']} (unchanged)",
        "",
        f"Geo-quarantined (new_detection only)    : {s['candidates_geo_quarantined']}",
        f"  area rule only  : {s['rejected_by_area_only']}",
        f"  AR rule only    : {s['rejected_by_ar_only']}",
        f"  both rules      : {s['rejected_by_both']}",
        "",
        f"Candidates active after Stage 1d        : {s['candidates_active']}",
        f"  new_detection passing: {s['new_detection_active']}",
        f"  stage0_reuse (pass-through): {s['stage0_reuse_unchanged']}",
        "",
        f"Frames newly zero-candidate             : {s['frames_newly_zero_candidate']}",
        "",
        "All geometry-rejected evidence is retained in",
        "stage1_candidates_geo_filtered.json under geometry_quarantined_candidates",
        "with reasons, rule_version, and threshold values.",
        "=" * 70,
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reversible Stage 1d geometry post-filter"
    )
    parser.add_argument("--stage1b-candidates", required=True,
                        help="Path to stage1_candidates_quarantined.json (Stage 1b output)")
    parser.add_argument("--output-dir", default="stage1d_output")
    parser.add_argument("--area-max-px", type=float, default=DEFAULT_AREA_MAX_PX)
    parser.add_argument("--ar-max",      type=float, default=DEFAULT_AR_MAX)
    parser.add_argument("--input-file-id", default="")
    args = parser.parse_args()

    with open(args.stage1b_candidates) as f:
        stage1_quarantined = json.load(f)

    output, report = apply_geometry_filter(
        stage1_quarantined,
        area_max_px=args.area_max_px,
        ar_max=args.ar_max,
        input_file_id=args.input_file_id,
    )

    os.makedirs(args.output_dir, exist_ok=True)
    out_candidates   = os.path.join(args.output_dir, "stage1_candidates_geo_filtered.json")
    out_report_json  = os.path.join(args.output_dir, "stage1d_geometry_report.json")
    out_report_txt   = os.path.join(args.output_dir, "stage1d_geometry_report.txt")

    with open(out_candidates, "w") as f:
        json.dump(output, f, indent=2)
    with open(out_report_json, "w") as f:
        json.dump(report, f, indent=2)
    with open(out_report_txt, "w") as f:
        f.write(text_report(report) + "\n")

    print(text_report(report))
    print(f"\n[stage1d] Candidates -> {out_candidates}")
    print(f"[stage1d] Report JSON -> {out_report_json}")
    print(f"[stage1d] Report text -> {out_report_txt}")


if __name__ == "__main__":
    main()
