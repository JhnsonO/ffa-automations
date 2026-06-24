#!/usr/bin/env python3
"""Backward anchor propagation — experiment-only core.

Given an independently credible later anchor, walk backwards through per-frame
candidate detections. The selection at frame t is based on the predicted prior
position from the *already chosen future path*, never on Stage 2's old tracklet.

This is detector-agnostic: a newer football YOLO only needs to emit candidates
with frame/yaw/pitch/confidence. No renderer, Stage 1/1b/2, or production path
is changed by this module.
"""
from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class BackwardConfig:
    max_jump_deg: float = 8.0
    confidence_weight: float = 0.45
    motion_weight: float = 0.55
    max_gap_frames: int = 3


def clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(x)))


def angular_distance_deg(a_yaw: float, a_pitch: float, b_yaw: float, b_pitch: float) -> float:
    dy = math.radians(a_yaw - b_yaw)
    val = (
        math.sin(math.radians(a_pitch)) * math.sin(math.radians(b_pitch))
        + math.cos(math.radians(a_pitch)) * math.cos(math.radians(b_pitch)) * math.cos(dy)
    )
    return math.degrees(math.acos(clamp(val, -1.0, 1.0)))


def candidate_confidence(candidate: Dict[str, Any]) -> float:
    for key in ("football_conf", "weighted_conf", "raw_conf", "confidence", "score"):
        value = candidate.get(key)
        if value is not None:
            try:
                return clamp(float(value))
            except (TypeError, ValueError):
                pass
    return 0.0


def valid_candidate(candidate: Dict[str, Any]) -> bool:
    try:
        float(candidate["yaw"]); float(candidate["pitch"])
        return True
    except (KeyError, TypeError, ValueError):
        return False


def _predict_previous(path_newest_first: Sequence[Dict[str, Any]]) -> Tuple[float, float]:
    """Linear backwards extrapolation from the two newest chosen points."""
    newest = path_newest_first[-1]
    if len(path_newest_first) < 2:
        return float(newest["yaw"]), float(newest["pitch"])
    previous = path_newest_first[-2]
    return (
        float(newest["yaw"]) + (float(newest["yaw"]) - float(previous["yaw"])),
        float(newest["pitch"]) + (float(newest["pitch"]) - float(previous["pitch"])),
    )


def choose_backward_candidate(
    candidates: Iterable[Dict[str, Any]],
    future_path_newest_first: Sequence[Dict[str, Any]],
    config: BackwardConfig = BackwardConfig(),
) -> Optional[Dict[str, Any]]:
    """Choose the most plausible predecessor without looking at old tracklets."""
    candidates = [c for c in candidates if valid_candidate(c)]
    if not candidates or not future_path_newest_first:
        return None
    pred_yaw, pred_pitch = _predict_previous(future_path_newest_first)
    ranked: List[Tuple[float, Dict[str, Any]]] = []
    for candidate in candidates:
        d = angular_distance_deg(float(candidate["yaw"]), float(candidate["pitch"]), pred_yaw, pred_pitch)
        if d > config.max_jump_deg:
            continue
        motion = 1.0 - d / config.max_jump_deg
        score = config.confidence_weight * candidate_confidence(candidate) + config.motion_weight * motion
        ranked.append((score, candidate))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    chosen = dict(ranked[0][1])
    chosen["backward_score"] = ranked[0][0]
    chosen["backward_predicted_yaw"] = pred_yaw
    chosen["backward_predicted_pitch"] = pred_pitch
    return chosen


def propagate_backward(
    frame_candidates: Dict[int, List[Dict[str, Any]]],
    anchor: Dict[str, Any],
    start_frame: int,
    stop_frame: int,
    config: BackwardConfig = BackwardConfig(),
) -> List[Dict[str, Any]]:
    """Return chronological anchor-backed path from stop_frame through anchor."""
    if not valid_candidate(anchor):
        raise ValueError("anchor requires yaw and pitch")
    path_newest_first = [dict(anchor, frame=int(start_frame), source="anchor")]
    misses = 0
    for frame in range(start_frame - 1, stop_frame - 1, -1):
        chosen = choose_backward_candidate(frame_candidates.get(frame, []), path_newest_first, config)
        if chosen is None:
            misses += 1
            if misses > config.max_gap_frames:
                break
            continue
        misses = 0
        chosen["frame"] = frame
        chosen["source"] = "backward_propagated"
        path_newest_first.append(chosen)
    return list(reversed(path_newest_first))


def _load_frames(path: Path) -> Dict[int, List[Dict[str, Any]]]:
    payload = json.loads(path.read_text())
    frames = payload.get("frames", payload)
    return {int(frame): candidates for frame, candidates in frames.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Backward propagation from a credible later ball anchor")
    parser.add_argument("--candidates", required=True, help="Stage 1-like frame-indexed candidates JSON")
    parser.add_argument("--anchor-frame", required=True, type=int)
    parser.add_argument("--anchor-yaw", required=True, type=float)
    parser.add_argument("--anchor-pitch", required=True, type=float)
    parser.add_argument("--stop-frame", required=True, type=int)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-jump-deg", type=float, default=8.0)
    args = parser.parse_args()

    config = BackwardConfig(max_jump_deg=args.max_jump_deg)
    path = propagate_backward(
        _load_frames(Path(args.candidates)),
        {"yaw": args.anchor_yaw, "pitch": args.anchor_pitch, "football_conf": 1.0},
        args.anchor_frame,
        args.stop_frame,
        config,
    )
    Path(args.output).write_text(json.dumps({"config": asdict(config), "path": path}, indent=2))
    print(f"BACKWARD_PATH_POINTS={len(path)}")


if __name__ == "__main__":
    main()
