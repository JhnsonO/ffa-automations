#!/usr/bin/env python3
"""Bidirectional, evidence-first repair of short Stage 1b candidate gaps."""

from __future__ import annotations

import argparse
import copy
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

VERSION = "bidirectional_resolver_v1"


@dataclass(frozen=True)
class ResolverConfig:
    anchor_confidence: float = 0.15
    trace_confidence: float = 0.03
    borderline_confidence: float = 0.06
    min_bbox_area_px: float = 4.0
    max_bbox_area_px: float = 2500.0
    min_bbox_aspect_ratio: float = 0.25
    max_bbox_aspect_ratio: float = 4.0
    fence_yaw: float = -77.4
    fence_pitch: float = -3.9
    fence_radius_deg: float = 5.0
    stable_frames: int = 1
    anchor_match_deg: float = 6.0
    max_yaw_step_deg: float = 3.0
    max_pitch_step_deg: float = 1.5
    agreement_deg: float = 1.25
    max_resolvable_window_frames: int = 29
    max_review_frames_per_window: int = 12
    confidence_weight: float = 0.45
    motion_weight: float = 0.55


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    return max(minimum, min(maximum, value))


def signed_yaw_delta_deg(target_yaw: float, current_yaw: float) -> float:
    """Shortest signed yaw step from current_yaw to target_yaw."""
    return (target_yaw - current_yaw + 540.0) % 360.0 - 180.0


def angular_distance_deg(
    yaw_a: float,
    pitch_a: float,
    yaw_b: float,
    pitch_b: float,
) -> float:
    ya, pa, yb, pb = map(math.radians, (yaw_a, pitch_a, yaw_b, pitch_b))
    dot = (
        math.sin(pa) * math.sin(pb)
        + math.cos(pa) * math.cos(pb) * math.cos(ya - yb)
    )
    return math.degrees(math.acos(_clamp(dot, -1.0, 1.0)))


def _candidate_confidence(candidate: Mapping[str, Any]) -> float:
    for key in ("weighted_conf", "raw_conf", "conf", "confidence", "score"):
        number = _finite_float(candidate.get(key))
        if number is not None:
            return _clamp(number)
    return 0.0


def _candidate_point(candidate: Mapping[str, Any]) -> tuple[float, float] | None:
    yaw = _finite_float(candidate.get("yaw"))
    pitch = _finite_float(candidate.get("pitch"))
    if yaw is None or pitch is None:
        return None
    return yaw, pitch


def _candidate_geometry(candidate: Mapping[str, Any]) -> tuple[float, float] | None:
    geometry = candidate.get("detection_geometry")
    if not isinstance(geometry, Mapping):
        return None

    area = _finite_float(geometry.get("bbox_area_px"))
    aspect = _finite_float(geometry.get("bbox_aspect_ratio"))
    if area is not None and aspect is not None:
        return area, aspect

    bbox = geometry.get("bbox_xyxy")
    if not isinstance(bbox, Sequence) or isinstance(bbox, (str, bytes)) or len(bbox) != 4:
        return None

    coords = [_finite_float(value) for value in bbox]
    if any(value is None for value in coords):
        return None

    x1, y1, x2, y2 = (float(value) for value in coords)
    width = abs(x2 - x1)
    height = abs(y2 - y1)
    if width <= 0.0 or height <= 0.0:
        return None
    return width * height, width / height


def is_in_fence_zone(candidate: Mapping[str, Any], config: ResolverConfig) -> bool:
    point = _candidate_point(candidate)
    if point is None:
        return False
    return angular_distance_deg(
        point[0], point[1], config.fence_yaw, config.fence_pitch
    ) <= config.fence_radius_deg


def geometry_is_plausible(candidate: Mapping[str, Any], config: ResolverConfig) -> bool:
    geometry = _candidate_geometry(candidate)
    if geometry is None:
        return False
    area, aspect = geometry
    return (
        config.min_bbox_area_px <= area <= config.max_bbox_area_px
        and config.min_bbox_aspect_ratio <= aspect <= config.max_bbox_aspect_ratio
    )


def is_quality_candidate(candidate: Mapping[str, Any], config: ResolverConfig) -> bool:
    """Anchor quality gate required before any backtracking."""
    return (
        _candidate_point(candidate) is not None
        and _candidate_confidence(candidate) >= config.anchor_confidence
        and geometry_is_plausible(candidate, config)
        and not is_in_fence_zone(candidate, config)
    )


def is_anchor_quality_candidate(candidate: Mapping[str, Any], config: ResolverConfig) -> bool:
    """Anchor quality gate for selection. Geometry is non-blocking when null (no data).
    Stage 1b carries ~8% null-geometry rows from Stage 0 reuse; these are acceptable
    anchors when confidence and fence-zone checks pass."""
    geometry = _candidate_geometry(candidate)
    geometry_ok = geometry is None or geometry_is_plausible(candidate, config)
    return (
        _candidate_point(candidate) is not None
        and _candidate_confidence(candidate) >= config.anchor_confidence
        and geometry_ok
        and not is_in_fence_zone(candidate, config)
    )


def _is_trace_candidate(candidate: Mapping[str, Any], config: ResolverConfig) -> bool:
    return (
        _candidate_point(candidate) is not None
        and _candidate_confidence(candidate) >= config.trace_confidence
        and geometry_is_plausible(candidate, config)
        and not is_in_fence_zone(candidate, config)
    )


def _within_corridor(
    candidate: Mapping[str, Any],
    reference: Mapping[str, Any],
    frame_delta: int,
    config: ResolverConfig,
) -> bool:
    candidate_point = _candidate_point(candidate)
    reference_point = _candidate_point(reference)
    if candidate_point is None or reference_point is None or frame_delta <= 0:
        return False

    yaw_delta = abs(signed_yaw_delta_deg(candidate_point[0], reference_point[0]))
    pitch_delta = abs(candidate_point[1] - reference_point[1])
    return (
        yaw_delta <= config.max_yaw_step_deg * frame_delta
        and pitch_delta <= config.max_pitch_step_deg * frame_delta
    )


def frame_candidates_from_payload(payload: Any) -> dict[int, list[dict[str, Any]]]:
    """Read the frozen Stage 1/1b dict-keyed candidate shape."""
    if isinstance(payload, Mapping):
        raw_frames = payload.get("frames", payload)
    else:
        raise ValueError("Candidate payload must be a mapping with a 'frames' dictionary.")
    if not isinstance(raw_frames, Mapping):
        raise ValueError("Candidate payload 'frames' must be a dictionary keyed by frame number.")

    normalised: dict[int, list[dict[str, Any]]] = {}
    for key, raw_candidates in raw_frames.items():
        try:
            frame = int(key)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Non-integer frame key: {key!r}") from exc
        candidates = raw_candidates if isinstance(raw_candidates, list) else []
        normalised[frame] = [
            copy.deepcopy(dict(candidate))
            for candidate in candidates
            if isinstance(candidate, Mapping)
        ]
    return normalised


def loss_windows_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        windows = payload.get("loss_windows", [])
    else:
        windows = payload
    if not isinstance(windows, list):
        raise ValueError("loss_windows input must be a list or contain 'loss_windows'.")
    return [copy.deepcopy(dict(window)) for window in windows if isinstance(window, Mapping)]


def _window_is_bridgeable(window: Mapping[str, Any]) -> bool:
    return window.get("bridgeable") is True or window.get("status") == "bridgeable"


def _select_anchor_candidate(
    candidates: Iterable[Mapping[str, Any]],
    expected_yaw: Any,
    expected_pitch: Any,
    config: ResolverConfig,
) -> dict[str, Any] | None:
    expected_yaw_float = _finite_float(expected_yaw)
    expected_pitch_float = _finite_float(expected_pitch)
    ranked: list[tuple[float, dict[str, Any]]] = []

    for candidate in candidates:
        if not is_anchor_quality_candidate(candidate, config):
            continue
        point = _candidate_point(candidate)
        assert point is not None
        if expected_yaw_float is not None and expected_pitch_float is not None:
            if angular_distance_deg(
                point[0], point[1], expected_yaw_float, expected_pitch_float
            ) > config.anchor_match_deg:
                continue
        ranked.append((_candidate_confidence(candidate), copy.deepcopy(dict(candidate))))

    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _stable_anchor(
    frame_candidates: Mapping[int, Sequence[Mapping[str, Any]]],
    anchor_frame: int,
    anchor_candidate: Mapping[str, Any],
    direction: int,
    config: ResolverConfig,
) -> bool:
    if config.stable_frames <= 1:
        return True

    previous = dict(anchor_candidate)
    previous_frame = anchor_frame
    for offset in range(1, config.stable_frames):
        frame = anchor_frame + direction * offset
        options = [
            candidate
            for candidate in frame_candidates.get(frame, [])
            if is_anchor_quality_candidate(candidate, config)
            and _within_corridor(candidate, previous, abs(frame - previous_frame), config)
        ]
        if not options:
            return False
        options.sort(key=_candidate_confidence, reverse=True)
        previous = dict(options[0])
        previous_frame = frame
    return True


def quality_anchor_for_window(
    frame_candidates: Mapping[int, Sequence[Mapping[str, Any]]],
    window: Mapping[str, Any],
    side: str,
    config: ResolverConfig = ResolverConfig(),
) -> tuple[dict[str, Any] | None, str | None]:
    if side not in {"left", "right"}:
        raise ValueError("side must be 'left' or 'right'.")

    if side == "left":
        frame_value = window.get("last_trusted_frame")
        expected_yaw = window.get("last_trusted_yaw")
        expected_pitch = window.get("last_trusted_pitch")
        stable_direction = -1
    else:
        frame_value = window.get("first_reacquisition_frame")
        expected_yaw = window.get("first_reacquisition_yaw")
        expected_pitch = window.get("first_reacquisition_pitch")
        stable_direction = 1

    try:
        frame = int(frame_value)
    except (TypeError, ValueError):
        return None, f"{side}_anchor_missing"

    candidate = _select_anchor_candidate(
        frame_candidates.get(frame, []), expected_yaw, expected_pitch, config
    )
    if candidate is None:
        return None, f"{side}_anchor_failed_quality_gate"
    if not _stable_anchor(frame_candidates, frame, candidate, stable_direction, config):
        return None, f"{side}_anchor_not_stable"

    candidate["frame"] = frame
    candidate["anchor_side"] = side
    return candidate, None


def _predict_position(path: Sequence[Mapping[str, Any]], target_frame: int) -> tuple[float, float]:
    last = path[-1]
    last_point = _candidate_point(last)
    if last_point is None:
        raise ValueError("Trace path contains a point without yaw/pitch.")
    last_frame = int(last["frame"])
    if len(path) < 2:
        return last_point

    previous = path[-2]
    previous_point = _candidate_point(previous)
    if previous_point is None:
        return last_point
    previous_frame = int(previous["frame"])
    time_delta = last_frame - previous_frame
    if time_delta == 0:
        return last_point

    yaw_velocity = signed_yaw_delta_deg(last_point[0], previous_point[0]) / time_delta
    pitch_velocity = (last_point[1] - previous_point[1]) / time_delta
    future_delta = target_frame - last_frame
    return (
        last_point[0] + yaw_velocity * future_delta,
        last_point[1] + pitch_velocity * future_delta,
    )


def _select_trace_candidate(
    candidates: Iterable[Mapping[str, Any]],
    reference: Mapping[str, Any],
    predicted_yaw: float,
    predicted_pitch: float,
    frame_delta: int,
    config: ResolverConfig,
) -> dict[str, Any] | None:
    ranked: list[tuple[float, dict[str, Any]]] = []
    prediction = {"yaw": predicted_yaw, "pitch": predicted_pitch}

    for candidate in candidates:
        if not _is_trace_candidate(candidate, config):
            continue
        if not _within_corridor(candidate, reference, frame_delta, config):
            continue
        if not _within_corridor(candidate, prediction, frame_delta, config):
            continue

        point = _candidate_point(candidate)
        assert point is not None
        yaw_error = abs(signed_yaw_delta_deg(point[0], predicted_yaw))
        pitch_error = abs(point[1] - predicted_pitch)
        yaw_quality = 1.0 - min(1.0, yaw_error / (config.max_yaw_step_deg * frame_delta))
        pitch_quality = 1.0 - min(1.0, pitch_error / (config.max_pitch_step_deg * frame_delta))
        motion_quality = (yaw_quality + pitch_quality) / 2.0
        confidence_quality = _clamp(
            _candidate_confidence(candidate) / max(config.anchor_confidence, 1e-9)
        )
        score = config.confidence_weight * confidence_quality + config.motion_weight * motion_quality
        chosen = copy.deepcopy(dict(candidate))
        chosen["trace_score"] = round(score, 6)
        chosen["predicted_yaw"] = round(predicted_yaw, 6)
        chosen["predicted_pitch"] = round(predicted_pitch, 6)
        ranked.append((score, chosen))

    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def forward_trace(
    frame_candidates: Mapping[int, Sequence[Mapping[str, Any]]],
    left_anchor: Mapping[str, Any],
    start_frame: int,
    end_frame: int,
    config: ResolverConfig = ResolverConfig(),
) -> dict[int, dict[str, Any]]:
    path: list[dict[str, Any]] = [copy.deepcopy(dict(left_anchor))]
    selected: dict[int, dict[str, Any]] = {}

    for frame in range(start_frame, end_frame + 1):
        reference = path[-1]
        frame_delta = frame - int(reference["frame"])
        predicted_yaw, predicted_pitch = _predict_position(path, frame)
        candidate = _select_trace_candidate(
            frame_candidates.get(frame, []), reference, predicted_yaw, predicted_pitch,
            frame_delta, config,
        )
        if candidate is None:
            continue
        candidate["frame"] = frame
        candidate["trace_direction"] = "forward"
        selected[frame] = candidate
        path.append(candidate)
    return selected


def backward_trace(
    frame_candidates: Mapping[int, Sequence[Mapping[str, Any]]],
    right_anchor: Mapping[str, Any],
    start_frame: int,
    end_frame: int,
    config: ResolverConfig = ResolverConfig(),
) -> dict[int, dict[str, Any]]:
    path: list[dict[str, Any]] = [copy.deepcopy(dict(right_anchor))]
    selected: dict[int, dict[str, Any]] = {}

    for frame in range(end_frame, start_frame - 1, -1):
        reference = path[-1]
        frame_delta = int(reference["frame"]) - frame
        predicted_yaw, predicted_pitch = _predict_position(path, frame)
        candidate = _select_trace_candidate(
            frame_candidates.get(frame, []), reference, predicted_yaw, predicted_pitch,
            frame_delta, config,
        )
        if candidate is None:
            continue
        candidate["frame"] = frame
        candidate["trace_direction"] = "backward"
        selected[frame] = candidate
        path.append(candidate)
    return selected


def _repair_candidate(
    frame: int,
    forward_candidate: Mapping[str, Any],
    backward_candidate: Mapping[str, Any],
    agreement_deg: float,
) -> dict[str, Any]:
    forward_score = _candidate_confidence(forward_candidate)
    backward_score = _candidate_confidence(backward_candidate)
    source_candidate = forward_candidate if forward_score >= backward_score else backward_candidate
    repaired = copy.deepcopy(dict(source_candidate))
    for key in ("trace_direction", "trace_score", "predicted_yaw", "predicted_pitch"):
        repaired.pop(key, None)
    repaired["frame"] = frame
    repaired["source"] = "bidirectional"
    repaired["raw_conf"] = max(
        _finite_float(repaired.get("raw_conf")) or 0.0,
        forward_score,
        backward_score,
    )
    repaired["weighted_conf"] = round(min(forward_score, backward_score), 6)
    repaired["bidirectional"] = {
        "forward_conf": round(forward_score, 6),
        "backward_conf": round(backward_score, 6),
        "agreement_deg": round(agreement_deg, 6),
    }
    return repaired


def _queue_candidate(
    forward_candidate: Mapping[str, Any] | None,
    backward_candidate: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    options = [candidate for candidate in (forward_candidate, backward_candidate) if candidate]
    if not options:
        return None
    return copy.deepcopy(dict(max(options, key=_candidate_confidence)))


def _corridor_context(
    forward_candidate: Mapping[str, Any] | None,
    backward_candidate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    def point(candidate: Mapping[str, Any] | None) -> dict[str, float] | None:
        if candidate is None:
            return None
        candidate_point = _candidate_point(candidate)
        if candidate_point is None:
            return None
        return {"yaw": candidate_point[0], "pitch": candidate_point[1]}

    forward_point = point(forward_candidate)
    backward_point = point(backward_candidate)
    centre: dict[str, float] | None = None
    if forward_point and backward_point:
        centre = {
            "yaw": forward_point["yaw"] + signed_yaw_delta_deg(
                backward_point["yaw"], forward_point["yaw"]
            ) / 2.0,
            "pitch": (forward_point["pitch"] + backward_point["pitch"]) / 2.0,
        }
    elif forward_point:
        centre = dict(forward_point)
    elif backward_point:
        centre = dict(backward_point)
    return {"forward": forward_point, "backward": backward_point, "centre": centre}


def _review_frame(
    frame: int,
    reason: str,
    forward_candidate: Mapping[str, Any] | None,
    backward_candidate: Mapping[str, Any] | None,
) -> dict[str, Any]:
    selected = _queue_candidate(forward_candidate, backward_candidate)
    return {
        "frame": frame,
        "reason": reason,
        "eligible_for_vlm": selected is not None,
        "candidate": selected,
        "forward_candidate": copy.deepcopy(dict(forward_candidate)) if forward_candidate else None,
        "backward_candidate": copy.deepcopy(dict(backward_candidate)) if backward_candidate else None,
        "corridor": _corridor_context(forward_candidate, backward_candidate),
    }


def _sample_review_frames(items: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    if len(items) <= maximum:
        return items

    def rank(item: Mapping[str, Any]) -> tuple[float, int]:
        candidate = item.get("candidate")
        confidence = _candidate_confidence(candidate) if isinstance(candidate, Mapping) else 0.0
        return confidence, -int(item["frame"])

    return sorted(sorted(items, key=rank, reverse=True)[:maximum], key=lambda item: int(item["frame"]))


def _queue_window(
    window: Mapping[str, Any],
    reason: str,
    frame_items: list[dict[str, Any]],
    left_anchor: Mapping[str, Any] | None = None,
    right_anchor: Mapping[str, Any] | None = None,
    config: ResolverConfig = ResolverConfig(),
) -> dict[str, Any]:
    return {
        "window_id": str(window.get("window_id", "")),
        "start_frame": int(window.get("start_frame", 0)),
        "end_frame": int(window.get("end_frame", 0)),
        "duration_frames": int(window.get("duration_frames", 0)),
        "reason": reason,
        "eligible_for_vlm": any(item.get("eligible_for_vlm") for item in frame_items),
        "left_anchor": copy.deepcopy(dict(left_anchor)) if left_anchor else None,
        "right_anchor": copy.deepcopy(dict(right_anchor)) if right_anchor else None,
        "frames": _sample_review_frames(frame_items, config.max_review_frames_per_window),
    }


def resolve_window(
    window: Mapping[str, Any],
    frame_candidates: Mapping[int, Sequence[Mapping[str, Any]]],
    config: ResolverConfig = ResolverConfig(),
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    try:
        start_frame = int(window["start_frame"])
        end_frame = int(window["end_frame"])
        duration = int(window["duration_frames"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid loss window: {window!r}") from exc

    if end_frame < start_frame or duration != end_frame - start_frame + 1:
        raise ValueError(f"Loss-window frame range/duration mismatch: {window!r}")
    if not _window_is_bridgeable(window):
        return [], _queue_window(window, "window_not_bridgeable", [], config=config)

    if duration >= config.max_resolvable_window_frames + 1:
        samples: list[dict[str, Any]] = []
        for frame in range(start_frame, end_frame + 1):
            candidates = [
                copy.deepcopy(candidate)
                for candidate in frame_candidates.get(frame, [])
                if _is_trace_candidate(candidate, config)
            ]
            if not candidates:
                continue
            candidates.sort(key=_candidate_confidence, reverse=True)
            samples.append(
                {
                    "frame": frame,
                    "reason": "long_window",
                    "eligible_for_vlm": True,
                    "candidate": candidates[0],
                    "forward_candidate": None,
                    "backward_candidate": None,
                    "corridor": {"forward": None, "backward": None, "centre": _corridor_context(candidates[0], None)["centre"]},
                }
            )
        return [], _queue_window(window, "long_window", samples, config=config)

    left_anchor, left_error = quality_anchor_for_window(frame_candidates, window, "left", config)
    right_anchor, right_error = quality_anchor_for_window(frame_candidates, window, "right", config)
    if left_anchor is None or right_anchor is None:
        reasons = [reason for reason in (left_error, right_error) if reason]
        return [], _queue_window(
            window,
            ";".join(reasons) if reasons else "anchor_quality_gate_failed",
            [],
            left_anchor=left_anchor,
            right_anchor=right_anchor,
            config=config,
        )

    forward = forward_trace(frame_candidates, left_anchor, start_frame, end_frame, config)
    backward = backward_trace(frame_candidates, right_anchor, start_frame, end_frame, config)
    repairs: list[dict[str, Any]] = []
    review_frames: list[dict[str, Any]] = []

    for frame in range(start_frame, end_frame + 1):
        forward_candidate = forward.get(frame)
        backward_candidate = backward.get(frame)
        if forward_candidate is not None and backward_candidate is not None:
            forward_point = _candidate_point(forward_candidate)
            backward_point = _candidate_point(backward_candidate)
            assert forward_point is not None
            assert backward_point is not None
            agreement = angular_distance_deg(
                forward_point[0], forward_point[1], backward_point[0], backward_point[1]
            )
            low_support = min(
                _candidate_confidence(forward_candidate),
                _candidate_confidence(backward_candidate),
            ) < config.borderline_confidence
            if agreement <= config.agreement_deg and not low_support:
                repairs.append(
                    {
                        "window_id": str(window.get("window_id", "")),
                        "frame": frame,
                        "reason": "forward_backward_agreement",
                        "agreement_deg": round(agreement, 6),
                        "candidate": _repair_candidate(
                            frame, forward_candidate, backward_candidate, agreement
                        ),
                    }
                )
                continue
            review_frames.append(
                _review_frame(
                    frame,
                    "borderline_support" if low_support else "trace_disagreement",
                    forward_candidate,
                    backward_candidate,
                )
            )
        elif forward_candidate is not None or backward_candidate is not None:
            review_frames.append(
                _review_frame(frame, "one_sided_trace", forward_candidate, backward_candidate)
            )

    if review_frames:
        return repairs, _queue_window(
            window,
            "unresolved_trace_frames",
            review_frames,
            left_anchor=left_anchor,
            right_anchor=right_anchor,
            config=config,
        )
    if len(repairs) == duration:
        return repairs, None

    # No corridor candidates found — attempt anchor-to-anchor linear interpolation.
    # If both anchors agree within agreement_deg, interpolate yaw/pitch linearly
    # across gap frames and emit as repairs (source="anchor_interpolation").
    left_point = _candidate_point(left_anchor)
    right_point = _candidate_point(right_anchor)
    if left_point is not None and right_point is not None:
        anchor_agreement = angular_distance_deg(
            left_point[0], left_point[1], right_point[0], right_point[1]
        )
        if anchor_agreement <= config.agreement_deg:
            total_steps = duration + 1  # left_anchor frame -> right_anchor frame
            for i, frame in enumerate(range(start_frame, end_frame + 1), start=1):
                t = i / total_steps
                interp_yaw = left_point[0] + t * (right_point[0] - left_point[0])
                interp_pitch = left_point[1] + t * (right_point[1] - left_point[1])
                anchor_conf = min(
                    _candidate_confidence(left_anchor),
                    _candidate_confidence(right_anchor),
                )
                repaired = copy.deepcopy(dict(left_anchor))
                for key in ("trace_direction", "trace_score", "predicted_yaw",
                            "predicted_pitch", "anchor_side"):
                    repaired.pop(key, None)
                repaired["frame"] = frame
                repaired["yaw"] = round(interp_yaw, 6)
                repaired["pitch"] = round(interp_pitch, 6)
                repaired["source"] = "anchor_interpolation"
                repaired["raw_conf"] = round(anchor_conf, 6)
                repaired["weighted_conf"] = round(anchor_conf, 6)
                repaired["bidirectional"] = {
                    "forward_conf": round(_candidate_confidence(left_anchor), 6),
                    "backward_conf": round(_candidate_confidence(right_anchor), 6),
                    "agreement_deg": round(anchor_agreement, 6),
                    "interpolation_t": round(t, 6),
                }
                repairs.append(
                    {
                        "window_id": str(window.get("window_id", "")),
                        "frame": frame,
                        "reason": "anchor_interpolation",
                        "agreement_deg": round(anchor_agreement, 6),
                        "candidate": repaired,
                    }
                )
            if len(repairs) == duration:
                return repairs, None

    return repairs, _queue_window(
        window,
        "no_corridor_supported_candidates",
        [],
        left_anchor=left_anchor,
        right_anchor=right_anchor,
        config=config,
    )


def resolve_loss_windows(
    loss_windows: Sequence[Mapping[str, Any]],
    frame_candidates: Mapping[int, Sequence[Mapping[str, Any]]],
    config: ResolverConfig = ResolverConfig(),
) -> tuple[dict[str, Any], dict[str, Any]]:
    repairs: list[dict[str, Any]] = []
    queue: list[dict[str, Any]] = []
    for window in loss_windows:
        window_repairs, queue_item = resolve_window(window, frame_candidates, config)
        repairs.extend(window_repairs)
        if queue_item is not None:
            queue.append(queue_item)

    generated = datetime.now(timezone.utc).isoformat()
    queued_window_ids = {str(item.get("window_id", "")) for item in queue if item.get("window_id")}
    repaired_window_ids = {str(item.get("window_id", "")) for item in repairs if item.get("window_id")}
    repair_payload = {
        "version": VERSION,
        "generated": generated,
        "config": asdict(config),
        "repairs": repairs,
        "summary": {
            "windows_seen": len(loss_windows),
            "repair_frames": len(repairs),
            "fully_resolved_windows": len(repaired_window_ids - queued_window_ids),
            "partially_resolved_windows": len(repaired_window_ids & queued_window_ids),
        },
    }
    queue_payload = {
        "version": VERSION,
        "generated": generated,
        "config": asdict(config),
        "queue": queue,
        "summary": {
            "windows_queued": len(queue),
            "review_frames": sum(len(item["frames"]) for item in queue),
            "eligible_review_frames": sum(
                1 for item in queue for frame in item["frames"] if frame.get("eligible_for_vlm")
            ),
        },
    }
    return repair_payload, queue_payload


def resolve_from_payloads(
    loss_windows_payload: Any,
    candidates_payload: Any,
    config: ResolverConfig = ResolverConfig(),
) -> tuple[dict[str, Any], dict[str, Any]]:
    return resolve_loss_windows(
        loss_windows_from_payload(loss_windows_payload),
        frame_candidates_from_payload(candidates_payload),
        config,
    )


def write_resolution_outputs(
    loss_windows_path: str | Path,
    candidates_path: str | Path,
    repairs_path: str | Path = "bidirectional_repairs.json",
    queue_path: str | Path = "ai_review_queue.json",
    config: ResolverConfig = ResolverConfig(),
) -> tuple[dict[str, Any], dict[str, Any]]:
    with Path(loss_windows_path).open("r", encoding="utf-8") as handle:
        loss_payload = json.load(handle)
    with Path(candidates_path).open("r", encoding="utf-8") as handle:
        candidates_payload = json.load(handle)
    repairs, queue = resolve_from_payloads(loss_payload, candidates_payload, config)
    for path, payload in ((Path(repairs_path), repairs), (Path(queue_path), queue)):
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
    return repairs, queue


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resolve Stage 1b loss windows bidirectionally.")
    parser.add_argument("--loss-windows", required=True)
    parser.add_argument("--candidates", required=True)
    parser.add_argument("--repairs-output", default="bidirectional_repairs.json")
    parser.add_argument("--queue-output", default="ai_review_queue.json")
    parser.add_argument("--anchor-confidence", type=float, default=ResolverConfig.anchor_confidence)
    parser.add_argument("--trace-confidence", type=float, default=ResolverConfig.trace_confidence)
    parser.add_argument("--borderline-confidence", type=float, default=ResolverConfig.borderline_confidence)
    parser.add_argument("--max-resolvable-window-frames", type=int, default=ResolverConfig.max_resolvable_window_frames)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    config = ResolverConfig(
        anchor_confidence=args.anchor_confidence,
        trace_confidence=args.trace_confidence,
        borderline_confidence=args.borderline_confidence,
        max_resolvable_window_frames=args.max_resolvable_window_frames,
    )
    repairs, queue = write_resolution_outputs(
        args.loss_windows,
        args.candidates,
        args.repairs_output,
        args.queue_output,
        config,
    )
    print(
        "[bidirectional] "
        f"repair_frames={repairs['summary']['repair_frames']} "
        f"queued_windows={queue['summary']['windows_queued']} "
        f"eligible_review_frames={queue['summary']['eligible_review_frames']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
