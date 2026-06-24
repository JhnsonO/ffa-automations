#!/usr/bin/env python3
"""
Detect gaps in Stage 1 football candidates that require recovery/backtracking.

The detector treats a frame as trusted when at least one candidate reaches the
configured confidence threshold. Consecutive untrusted frames are grouped into
loss windows and annotated with the trusted observations on either side.
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_MIN_CONFIDENCE = 0.15

_CONFIDENCE_KEYS = ("weighted_conf", "raw_conf", "confidence", "conf")
_FRAME_NUMBER_KEYS = (
    "frame_index",
    "frame_idx",
    "frame_number",
    "frame",
    "index",
)


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _frame_number(frame: Mapping[str, Any], position: int) -> int:
    for key in _FRAME_NUMBER_KEYS:
        number = _finite_float(frame.get(key))
        if number is not None and number.is_integer():
            return int(number)
    return position


def _candidate_score(candidate: Mapping[str, Any]) -> float | None:
    for key in _CONFIDENCE_KEYS:
        score = _finite_float(candidate.get(key))
        if score is not None:
            return score
    return None


def _best_trusted_candidate(
    candidates: Sequence[Any],
    min_confidence: float,
) -> Mapping[str, Any] | None:
    best_candidate: Mapping[str, Any] | None = None
    best_score: float | None = None
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        score = _candidate_score(candidate)
        if score is None or score < min_confidence:
            continue
        if best_score is None or score > best_score:
            best_candidate = candidate
            best_score = score
    return best_candidate


def _point_from_candidate(
    candidate: Mapping[str, Any],
    frame_number: int,
) -> dict[str, float | int | None]:
    return {
        "frame": frame_number,
        "yaw": _finite_float(candidate.get("yaw")),
        "pitch": _finite_float(candidate.get("pitch")),
    }


def _normalise_frames(payload: Any) -> list[Mapping[str, Any]]:
    frames: Any
    if isinstance(payload, list):
        frames = payload
    elif isinstance(payload, Mapping):
        frames = payload.get("frames")
    else:
        raise ValueError(
            "Stage 1 candidates JSON must be a list of frame objects or an "
            'object containing a "frames" list.'
        )
    if not isinstance(frames, list):
        raise ValueError(
            'Stage 1 candidates JSON must contain a list under "frames".'
        )
    normalised: list[Mapping[str, Any]] = []
    for position, frame in enumerate(frames):
        if not isinstance(frame, Mapping):
            raise ValueError(
                f"Frame at list position {position} must be a JSON object."
            )
        normalised.append(frame)
    return normalised


def detect_loss_windows(
    frames: Sequence[Mapping[str, Any]],
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    if not math.isfinite(min_confidence):
        raise ValueError("min_confidence must be a finite number.")

    total_candidates = 0
    loss_windows: list[dict[str, Any]] = []

    previous_trusted: dict[str, float | int | None] | None = None
    active_window: dict[str, Any] | None = None

    for position, frame in enumerate(frames):
        frame_number = _frame_number(frame, position)
        raw_candidates = frame.get("candidates", [])
        candidates = raw_candidates if isinstance(raw_candidates, list) else []
        total_candidates += len(candidates)

        trusted_candidate = _best_trusted_candidate(candidates, min_confidence)

        if trusted_candidate is not None:
            trusted_point = _point_from_candidate(trusted_candidate, frame_number)
            if active_window is not None:
                active_window["first_reacquisition_frame"] = trusted_point["frame"]
                active_window["first_reacquisition_yaw"] = trusted_point["yaw"]
                active_window["first_reacquisition_pitch"] = trusted_point["pitch"]
                active_window["status"] = (
                    "bridgeable"
                    if active_window["last_trusted_frame"] is not None
                    else "isolated"
                )
                loss_windows.append(active_window)
                active_window = None
            previous_trusted = trusted_point
            continue

        if active_window is None:
            active_window = {
                "window_id": f"W{len(loss_windows) + 1:04d}",
                "start_frame": frame_number,
                "end_frame": frame_number,
                "duration_frames": 1,
                "last_trusted_yaw": (
                    previous_trusted["yaw"] if previous_trusted is not None else None
                ),
                "last_trusted_pitch": (
                    previous_trusted["pitch"] if previous_trusted is not None else None
                ),
                "last_trusted_frame": (
                    previous_trusted["frame"] if previous_trusted is not None else None
                ),
                "first_reacquisition_frame": None,
                "first_reacquisition_yaw": None,
                "first_reacquisition_pitch": None,
                "status": "",
            }
        else:
            active_window["end_frame"] = frame_number
            active_window["duration_frames"] += 1

    if active_window is not None:
        active_window["status"] = (
            "open"
            if active_window["last_trusted_frame"] is not None
            else "isolated"
        )
        loss_windows.append(active_window)

    summary = {
        "total_windows": len(loss_windows),
        "bridgeable": sum(w["status"] == "bridgeable" for w in loss_windows),
        "open": sum(w["status"] == "open" for w in loss_windows),
        "isolated": sum(w["status"] == "isolated" for w in loss_windows),
    }

    return {
        "generated": datetime.now(timezone.utc).isoformat(),
        "total_frames": len(frames),
        "total_candidates": total_candidates,
        "loss_windows": loss_windows,
        "summary": summary,
    }


def detect_loss_windows_from_payload(
    payload: Any,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    return detect_loss_windows(_normalise_frames(payload), min_confidence=min_confidence)


def write_loss_windows(
    input_path: str | Path,
    output_path: str | Path = "loss_windows.json",
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    source = Path(input_path)
    destination = Path(output_path)
    with source.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    report = detect_loss_windows_from_payload(payload, min_confidence=min_confidence)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2)
        handle.write("\n")
    return report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Detect untrusted Stage 1 candidate loss windows.")
    parser.add_argument("--input", required=True, help="Path to Stage 1 candidates JSON.")
    parser.add_argument("--output", default="loss_windows.json", help="Output path for the loss-window report.")
    parser.add_argument("--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    report = write_loss_windows(input_path=args.input, output_path=args.output, min_confidence=args.min_confidence)
    print(f"Wrote {args.output}: {report['summary']['total_windows']} loss windows across {report['total_frames']} frames.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
