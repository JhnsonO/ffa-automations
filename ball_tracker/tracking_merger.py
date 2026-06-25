#!/usr/bin/env python3
"""Build a JSON-only Phase B tracking_final candidate view.

This module never wires the renderer to tracking_final.json. It only writes the
merged candidate artefact for later visual approval.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

VERSION = "tracking_merger_v1"


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _frame_mapping(payload: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    frames = payload.get("frames")
    if not isinstance(frames, Mapping):
        raise ValueError("Candidate payload must contain a dict under 'frames'.")

    output: dict[str, list[dict[str, Any]]] = {}
    for key, candidates in frames.items():
        try:
            frame = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Non-integer frame key: {key!r}") from exc
        values = candidates if isinstance(candidates, list) else []
        output[str(frame)] = [
            copy.deepcopy(dict(candidate))
            for candidate in values
            if isinstance(candidate, Mapping)
        ]
    return output


def _candidate_from_detection(detection: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    yaw = _finite_float(detection.get("yaw"))
    pitch = _finite_float(detection.get("pitch"))
    if yaw is None or pitch is None:
        raise ValueError("Repair/VLM detection requires finite yaw and pitch.")

    confidence = (
        _finite_float(detection.get("weighted_conf"))
        or _finite_float(detection.get("raw_conf"))
        or _finite_float(detection.get("conf"))
        or 0.0
    )
    crop_yaw = _finite_float(detection.get("crop_yaw"))
    geometry = detection.get("detection_geometry")
    if not isinstance(geometry, Mapping):
        geometry = {
            "bbox_xyxy": None,
            "bbox_area_px": None,
            "bbox_aspect_ratio": None,
        }

    candidate = copy.deepcopy(dict(detection))
    candidate.pop("frame", None)
    candidate["yaw"] = yaw
    candidate["pitch"] = pitch
    candidate["raw_conf"] = confidence
    candidate["penalty"] = _finite_float(candidate.get("penalty")) or 1.0
    candidate["weighted_conf"] = confidence
    candidate["source"] = source
    candidate["crop_yaw"] = crop_yaw if crop_yaw is not None else yaw
    candidate["region"] = candidate.get("region", "phase_b")
    candidate["detection_geometry"] = copy.deepcopy(dict(geometry))
    return candidate


def _repairs(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    repairs = payload.get("repairs", [])
    return [item for item in repairs if isinstance(item, Mapping)] if isinstance(repairs, list) else []


def _decisions(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, Mapping):
        return []
    decisions = payload.get("decisions", [])
    return [item for item in decisions if isinstance(item, Mapping)] if isinstance(decisions, list) else []


def merge_tracking_payloads(
    candidates_payload: Mapping[str, Any],
    repairs_payload: Any,
    decisions_payload: Any,
) -> dict[str, Any]:
    """Merge repairs then VLM ball decisions into a Stage-1-shaped frame map."""
    result = {
        key: copy.deepcopy(value)
        for key, value in candidates_payload.items()
        if key not in {"frames", "tracking_final"}
    }
    frames = _frame_mapping(candidates_payload)
    repairs_applied = 0
    vlm_applied = 0

    for repair in _repairs(repairs_payload):
        try:
            frame = int(repair["frame"])
        except (KeyError, TypeError, ValueError):
            continue
        raw_candidate = repair.get("candidate", repair)
        if not isinstance(raw_candidate, Mapping):
            continue
        frames[str(frame)] = [
            _candidate_from_detection(raw_candidate, source="bidirectional")
        ]
        repairs_applied += 1

    for decision in _decisions(decisions_payload):
        if decision.get("decision") != "ball":
            continue
        try:
            frame = int(decision["frame"])
        except (KeyError, TypeError, ValueError):
            continue
        raw_detection = decision.get("detection")
        if not isinstance(raw_detection, Mapping):
            continue
        frames[str(frame)] = [
            _candidate_from_detection(raw_detection, source="vlm")
        ]
        vlm_applied += 1

    result["frames"] = {
        key: frames[key]
        for key in sorted(frames, key=lambda value: int(value))
    }
    result["tracking_final"] = {
        "version": VERSION,
        "generated": datetime.now(timezone.utc).isoformat(),
        "camera_wiring": "disabled",
        "repairs_applied": repairs_applied,
        "vlm_ball_decisions_applied": vlm_applied,
    }
    return result


def merge_tracking_final(
    candidates_path: str | Path,
    repairs_path: str | Path,
    decisions_path: str | Path,
    output_path: str | Path = "tracking_final.json",
) -> dict[str, Any]:
    """Read Phase B inputs, write tracking_final.json, and return its payload."""
    with Path(candidates_path).open("r", encoding="utf-8") as handle:
        candidates_payload = json.load(handle)
    with Path(repairs_path).open("r", encoding="utf-8") as handle:
        repairs_payload = json.load(handle)
    with Path(decisions_path).open("r", encoding="utf-8") as handle:
        decisions_payload = json.load(handle)

    if not isinstance(candidates_payload, Mapping):
        raise ValueError("Stage 1b candidates JSON must be an object.")

    merged = merge_tracking_payloads(candidates_payload, repairs_payload, decisions_payload)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(merged, handle, indent=2)
        handle.write("\n")
    return merged


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create JSON-only tracking_final candidate output.")
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--repairs", required=True)
    parser.add_argument("--decisions", required=True)
    parser.add_argument("--output", default="tracking_final.json")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    result = merge_tracking_final(args.candidates, args.repairs, args.decisions, args.output)
    metadata = result["tracking_final"]
    print(
        "[tracking-merger] "
        f"repairs_applied={metadata['repairs_applied']} "
        f"vlm_ball_decisions_applied={metadata['vlm_ball_decisions_applied']} "
        "camera_wiring=disabled"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
