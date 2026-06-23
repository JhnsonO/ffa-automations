#!/usr/bin/env python3
"""
FFA Offline Recovery Pipeline — Stage 1b: Confirmed-Static Quarantine
======================================================================

Purpose
-------
Create a reversible, Stage-1-compatible candidate view with detections inside
*confirmed static* Stage 0 hotspot regions removed from active ``frames``.

This is deliberately not a detector rerun and does not modify Stage 1 output
in place. Every excluded candidate is retained in ``quarantined_candidates``
with its original fields and an audit record explaining why it was excluded.

A region is eligible only when:
    hotspot_region.peak_duty >= hotspot_map.duty_cycle_threshold

The region geometry comes from the Stage 0 map; no coordinates are hard-coded.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


VERSION = "stage1b_static_quarantine_v1"
DEFAULT_DUTY_THRESHOLD = 0.6


def angular_distance_deg(yaw_a: float, pitch_a: float,
                         yaw_b: float, pitch_b: float) -> float:
    """Great-circle distance on the yaw/pitch sphere, in degrees."""
    ya, pa, yb, pb = map(math.radians, (yaw_a, pitch_a, yaw_b, pitch_b))
    dot = (
        math.sin(pa) * math.sin(pb)
        + math.cos(pa) * math.cos(pb) * math.cos(ya - yb)
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def _region_label(region: dict[str, Any]) -> str:
    return f"({float(region['centre_yaw']):.1f},{float(region['centre_pitch']):.1f})"


def confirmed_static_regions(hotspot_map: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    """Return duty threshold and regions eligible for quarantine."""
    threshold = float(hotspot_map.get("duty_cycle_threshold", DEFAULT_DUTY_THRESHOLD))
    regions: list[dict[str, Any]] = []

    for entry in hotspot_map.get("hotspot_regions", []):
        if float(entry.get("peak_duty", 0.0)) < threshold:
            continue
        radius = float(entry.get("radius_deg", 0.0))
        if radius <= 0:
            continue
        regions.append({
            "label": _region_label(entry),
            "centre_yaw": float(entry["centre_yaw"]),
            "centre_pitch": float(entry["centre_pitch"]),
            "radius_deg": radius,
            "peak_duty": float(entry["peak_duty"]),
        })

    return threshold, regions


def matching_static_region(candidate: dict[str, Any],
                           regions: list[dict[str, Any]]) -> tuple[dict[str, Any], float] | None:
    """Return the nearest matching confirmed-static region, if any."""
    if "yaw" not in candidate or "pitch" not in candidate:
        return None

    matches: list[tuple[dict[str, Any], float]] = []
    for region in regions:
        distance = angular_distance_deg(
            float(candidate["yaw"]), float(candidate["pitch"]),
            region["centre_yaw"], region["centre_pitch"],
        )
        if distance <= region["radius_deg"]:
            matches.append((region, distance))

    return min(matches, key=lambda item: item[1]) if matches else None


def _empty_region_stats(region: dict[str, Any]) -> dict[str, Any]:
    return {
        "region": region["label"],
        "centre_yaw": region["centre_yaw"],
        "centre_pitch": region["centre_pitch"],
        "radius_deg": region["radius_deg"],
        "peak_duty": region["peak_duty"],
        "candidate_count": 0,
        "frame_count": 0,
        "raw_conf_sum": 0.0,
        "weighted_conf_sum": 0.0,
        "mean_angular_distance_deg": None,
        "max_angular_distance_deg": None,
    }


def quarantine_stage1_data(stage1: dict[str, Any], hotspot_map: dict[str, Any],
                           stage1_file_id: str = "", hotspot_map_file_id: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """Build quarantined Stage 1 output and a standalone audit report payload."""
    threshold, regions = confirmed_static_regions(hotspot_map)
    frames_raw = stage1.get("frames", {})
    if not isinstance(frames_raw, dict):
        raise ValueError("Expected Stage 1 'frames' to be a dictionary keyed by frame number")

    active_frames: dict[str, list[dict[str, Any]]] = {}
    quarantined_frames: dict[str, list[dict[str, Any]]] = {}

    region_stats = {region["label"]: _empty_region_stats(region) for region in regions}
    region_frame_sets: dict[str, set[str]] = defaultdict(set)
    region_distances: dict[str, list[float]] = defaultdict(list)

    total_before = 0
    total_active = 0
    total_quarantined = 0
    frames_with_candidates_before = 0
    frames_with_candidates_after = 0
    frames_newly_zero = 0

    for frame_key, raw_candidates in frames_raw.items():
        candidates = raw_candidates if isinstance(raw_candidates, list) else []
        if candidates:
            frames_with_candidates_before += 1

        active: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []

        for candidate in candidates:
            total_before += 1
            match = matching_static_region(candidate, regions)
            if match is None:
                active.append(copy.deepcopy(candidate))
                total_active += 1
                continue

            region, distance = match
            audit_candidate = copy.deepcopy(candidate)
            audit_candidate["quarantine"] = {
                "reason": "confirmed_static_hotspot",
                "region": region["label"],
                "region_peak_duty": region["peak_duty"],
                "duty_cycle_threshold": threshold,
                "region_radius_deg": region["radius_deg"],
                "angular_distance_deg": round(distance, 4),
                "rule_version": VERSION,
            }
            quarantined.append(audit_candidate)
            total_quarantined += 1

            stats = region_stats[region["label"]]
            stats["candidate_count"] += 1
            stats["raw_conf_sum"] += float(candidate.get("raw_conf", 0.0))
            stats["weighted_conf_sum"] += float(candidate.get("weighted_conf", 0.0))
            region_frame_sets[region["label"]].add(str(frame_key))
            region_distances[region["label"]].append(distance)

        active_frames[str(frame_key)] = active
        if quarantined:
            quarantined_frames[str(frame_key)] = quarantined
        if candidates and not active:
            frames_newly_zero += 1
        if active:
            frames_with_candidates_after += 1

    for label, stats in region_stats.items():
        distances = region_distances[label]
        stats["frame_count"] = len(region_frame_sets[label])
        stats["raw_conf_sum"] = round(stats["raw_conf_sum"], 4)
        stats["weighted_conf_sum"] = round(stats["weighted_conf_sum"], 4)
        if distances:
            stats["mean_angular_distance_deg"] = round(sum(distances) / len(distances), 4)
            stats["max_angular_distance_deg"] = round(max(distances), 4)

    output = {
        key: copy.deepcopy(value)
        for key, value in stage1.items()
        if key not in {"frames", "quarantined_candidates", "stage1b"}
    }
    output["frames"] = active_frames
    output["quarantined_candidates"] = quarantined_frames
    output["stage1b"] = {
        "version": VERSION,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "input_stage1_file_id": stage1_file_id,
        "input_hotspot_map_file_id": hotspot_map_file_id,
        "rule": {
            "name": "confirmed_static_region_quarantine",
            "condition": "hotspot_region.peak_duty >= duty_cycle_threshold AND candidate_distance <= region.radius_deg",
            "duty_cycle_threshold": threshold,
        },
        "confirmed_static_regions": regions,
        "counts": {
            "candidates_before": total_before,
            "candidates_active": total_active,
            "candidates_quarantined": total_quarantined,
            "frames_total": len(frames_raw),
            "frames_with_candidates_before": frames_with_candidates_before,
            "frames_with_candidates_after": frames_with_candidates_after,
            "frames_newly_zero_candidate": frames_newly_zero,
        },
    }

    report = {
        "version": VERSION,
        "created_utc": output["stage1b"]["created_utc"],
        "input": {
            "stage1_file_id": stage1_file_id,
            "hotspot_map_file_id": hotspot_map_file_id,
        },
        "rule": output["stage1b"]["rule"],
        "summary": output["stage1b"]["counts"],
        "confirmed_static_regions": list(region_stats.values()),
    }
    return output, report


def text_report(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "=" * 70,
        "STAGE 1B — CONFIRMED-STATIC QUARANTINE — REPORT",
        "=" * 70,
        f"Rule: {report['rule']['condition']}",
        f"Duty-cycle threshold: {report['rule']['duty_cycle_threshold']:.4f}",
        "",
        "CANDIDATE COUNTS",
        "-" * 70,
        f"Candidates before          : {summary['candidates_before']}",
        f"Candidates active          : {summary['candidates_active']}",
        f"Candidates quarantined     : {summary['candidates_quarantined']}",
        f"Frames newly zero-candidate: {summary['frames_newly_zero_candidate']}",
        f"Frames with candidates     : {summary['frames_with_candidates_before']} -> {summary['frames_with_candidates_after']}",
        "",
        "QUARANTINE BY CONFIRMED-STATIC REGION",
        "-" * 70,
    ]
    regions = report["confirmed_static_regions"]
    if not regions:
        lines.append("No confirmed-static regions met the map threshold; no candidates quarantined.")
    else:
        for region in regions:
            lines.extend([
                f"{region['region']}  duty={region['peak_duty']:.4f}  radius={region['radius_deg']:.3f}°",
                f"  candidates={region['candidate_count']}  frames={region['frame_count']}  raw_sum={region['raw_conf_sum']:.4f}  weighted_sum={region['weighted_conf_sum']:.4f}",
                f"  mean_distance={region['mean_angular_distance_deg']}°  max_distance={region['max_angular_distance_deg']}°",
            ])
    lines.extend([
        "",
        "All quarantined evidence is retained in stage1_candidates_quarantined.json",
        "under top-level quarantined_candidates with reason, region, and distance.",
        "=" * 70,
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Reversible Stage 1b confirmed-static quarantine")
    parser.add_argument("--stage1-candidates", required=True)
    parser.add_argument("--hotspot-map", required=True)
    parser.add_argument("--output-dir", default="stage1b_output")
    parser.add_argument("--stage1-file-id", default="")
    parser.add_argument("--hotspot-map-file-id", default="")
    args = parser.parse_args()

    with open(args.stage1_candidates) as f:
        stage1 = json.load(f)
    with open(args.hotspot_map) as f:
        hotspot_map = json.load(f)

    output, report = quarantine_stage1_data(
        stage1, hotspot_map,
        stage1_file_id=args.stage1_file_id,
        hotspot_map_file_id=args.hotspot_map_file_id,
    )

    os.makedirs(args.output_dir, exist_ok=True)
    out_candidates = os.path.join(args.output_dir, "stage1_candidates_quarantined.json")
    out_report_json = os.path.join(args.output_dir, "stage1b_quarantine_report.json")
    out_report_txt = os.path.join(args.output_dir, "stage1b_quarantine_report.txt")

    with open(out_candidates, "w") as f:
        json.dump(output, f, indent=2)
    with open(out_report_json, "w") as f:
        json.dump(report, f, indent=2)
    with open(out_report_txt, "w") as f:
        f.write(text_report(report) + "\n")

    print(text_report(report))
    print(f"\n[stage1b] Candidates -> {out_candidates}")
    print(f"[stage1b] Report JSON -> {out_report_json}")
    print(f"[stage1b] Report text -> {out_report_txt}")


if __name__ == "__main__":
    main()
