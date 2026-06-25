#!/usr/bin/env python3
"""Replay verified Phase B repairs into a copy of v11 tracking.json.

Additive/offline only: does not modify run_tracker.py or render_segment.py.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
from pathlib import Path
from typing import Any, Mapping


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _load_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _repair_candidates_by_frame(repairs_payload: Any) -> dict[int, dict[str, Any]]:
    if not isinstance(repairs_payload, Mapping):
        raise ValueError("Repairs payload must be a JSON object.")
    repairs = repairs_payload.get("repairs")
    if not isinstance(repairs, list):
        raise ValueError("Repairs payload must contain a list under 'repairs'.")

    output: dict[int, dict[str, Any]] = {}
    for repair in repairs:
        if not isinstance(repair, Mapping):
            continue
        try:
            frame_number = int(repair["frame"])
        except (KeyError, TypeError, ValueError):
            continue

        candidate = repair.get("candidate")
        if not isinstance(candidate, Mapping):
            continue

        yaw = _finite_float(candidate.get("yaw"))
        pitch = _finite_float(candidate.get("pitch"))
        weighted_conf = _finite_float(candidate.get("weighted_conf"))
        source = candidate.get("source")
        bidirectional = candidate.get("bidirectional")
        if (
            yaw is None
            or pitch is None
            or weighted_conf is None
            or not isinstance(source, str)
            or not isinstance(bidirectional, Mapping)
            or not 0.0 <= weighted_conf <= 1.0
        ):
            continue

        normalised = copy.deepcopy(dict(candidate))
        normalised["yaw"] = yaw
        normalised["pitch"] = pitch
        normalised["weighted_conf"] = weighted_conf
        output[frame_number] = normalised
    return output


def _repair_detection(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "yaw": candidate["yaw"],
        "pitch": candidate["pitch"],
        "conf": candidate["weighted_conf"],
        "source": candidate["source"],
        "weighted_conf": candidate["weighted_conf"],
        "phase_b_repair": True,
        "bidirectional": copy.deepcopy(candidate["bidirectional"]),
    }


def replay_tracking_final(
    tracking_path: str | Path,
    repairs_path: str | Path,
    output_path: str | Path,
) -> dict[str, int]:
    """Create tracking_repaired.json from valid repair frames only.

    Override only when a matching repair exists and v11 best_score is null.
    tracker_state is intentionally preserved.
    """
    tracking = _load_json(tracking_path)
    repairs_payload = _load_json(repairs_path)
    if not isinstance(tracking, Mapping):
        raise ValueError("tracking.json must be a JSON object.")
    frames = tracking.get("frames")
    if not isinstance(frames, list):
        raise ValueError("tracking.json must contain a list under 'frames'.")

    repairs_by_frame = _repair_candidates_by_frame(repairs_payload)
    output = copy.deepcopy(dict(tracking))
    output_frames = output["frames"]
    summary = {
        "tracking_frames": len(output_frames),
        "valid_repairs": len(repairs_by_frame),
        "overrides_applied": 0,
        "skipped_existing_v11_confirmation": 0,
        "skipped_invalid_tracking_frame": 0,
        "repairs_not_found_in_tracking": 0,
    }
    seen_tracking_frames: set[int] = set()

    for frame_record in output_frames:
        if not isinstance(frame_record, dict):
            summary["skipped_invalid_tracking_frame"] += 1
            continue
        try:
            frame_number = int(frame_record["frame"])
        except (KeyError, TypeError, ValueError):
            summary["skipped_invalid_tracking_frame"] += 1
            continue
        seen_tracking_frames.add(frame_number)

        candidate = repairs_by_frame.get(frame_number)
        if candidate is None:
            continue
        if frame_record.get("best_score") is not None:
            summary["skipped_existing_v11_confirmation"] += 1
            continue

        existing_smoothed = frame_record.get("smoothed")
        if not isinstance(existing_smoothed, Mapping):
            existing_smoothed = {}
        frame_record["smoothed"] = {
            **copy.deepcopy(dict(existing_smoothed)),
            "yaw": candidate["yaw"],
            "pitch": candidate["pitch"],
        }

        detections = frame_record.get("detections")
        if not isinstance(detections, list):
            detections = []
        frame_record["detections"] = [*detections, _repair_detection(candidate)]
        frame_record["best_score"] = candidate["weighted_conf"]
        frame_record["loss_state"] = "phase_b_repair"
        frame_record["phase_b_override"] = True
        frame_record["phase_b_source"] = candidate["source"]
        frame_record["phase_b_weighted_conf"] = candidate["weighted_conf"]
        frame_record["phase_b_bidirectional"] = copy.deepcopy(candidate["bidirectional"])
        summary["overrides_applied"] += 1

    summary["repairs_not_found_in_tracking"] = len(set(repairs_by_frame).difference(seen_tracking_frames))
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
        handle.write("\n")
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay verified Phase B repairs for offline A/B validation.")
    parser.add_argument("--tracking", required=True, help="Path to original v11 tracking.json")
    parser.add_argument("--repairs", required=True, help="Path to bidirectional_repairs.json")
    parser.add_argument("--output", default="tracking_repaired.json", help="Output replayed tracking JSON")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    summary = replay_tracking_final(args.tracking, args.repairs, args.output)
    print(
        "[replay-tracking-final] "
        f"tracking_frames={summary['tracking_frames']} "
        f"valid_repairs={summary['valid_repairs']} "
        f"overrides_applied={summary['overrides_applied']} "
        f"skipped_existing_confirmation={summary['skipped_existing_v11_confirmation']} "
        f"repairs_not_found={summary['repairs_not_found_in_tracking']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
