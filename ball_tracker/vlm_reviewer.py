#!/usr/bin/env python3
"""Targeted VLM review for unresolved bidirectional recovery windows."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ball_tracker.detector_interface import VLMResult, vlm_backend

VERSION = "vlm_reviewer_v1"


def _queue_items(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        items = payload.get("queue", payload.get("windows", []))
    else:
        items = payload
    if not isinstance(items, list):
        raise ValueError("ai_review_queue must contain a list under 'queue'.")
    return [item for item in items if isinstance(item, Mapping)]


def _review_frames(queue_item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    frames = queue_item.get("frames", [])
    if not isinstance(frames, list):
        return []
    return [frame for frame in frames if isinstance(frame, Mapping)]


def _candidate_context(review_frame: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    candidates: list[Mapping[str, Any]] = []
    for key in ("candidate", "forward_candidate", "backward_candidate"):
        value = review_frame.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
    return candidates


def _pack_path(packs_dir: Path, window_id: str, frame: int) -> Path:
    return packs_dir / window_id / f"frame_{frame}.jpg"


def _decision_payload(
    *,
    window_id: str,
    frame: int,
    decision: str,
    confidence: float,
    reasoning: str,
    detection: Mapping[str, Any] | None,
    dry_run: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "window_id": window_id,
        "frame": frame,
        "decision": decision,
        "confidence": confidence,
        "reasoning": reasoning,
        "dry_run": dry_run,
    }
    if detection is not None:
        result["detection"] = dict(detection)
    return result


def review_queue(
    queue_payload: Any,
    packs_dir: str | Path,
    *,
    model: str | None = None,
    max_calls: int | None = None,
) -> dict[str, Any]:
    """Review only corridor-gated queue frames with usable candidate evidence."""
    packs_root = Path(packs_dir)
    api_key_present = bool(os.environ.get("ANTHROPIC_API_KEY"))
    decisions: list[dict[str, Any]] = []
    calls_attempted = 0
    skipped_ineligible = 0
    skipped_missing_pack = 0
    seen: set[tuple[str, int]] = set()

    for queue_item in _queue_items(queue_payload):
        window_id = str(queue_item.get("window_id", ""))
        if not window_id:
            continue

        for review_frame in _review_frames(queue_item):
            if not review_frame.get("eligible_for_vlm", False):
                skipped_ineligible += 1
                continue

            try:
                frame = int(review_frame["frame"])
            except (KeyError, TypeError, ValueError):
                skipped_ineligible += 1
                continue

            key = (window_id, frame)
            if key in seen:
                continue
            seen.add(key)

            candidates = _candidate_context(review_frame)
            if not candidates:
                skipped_ineligible += 1
                continue

            image_path = _pack_path(packs_root, window_id, frame)
            if api_key_present and not image_path.is_file():
                skipped_missing_pack += 1
                decisions.append(
                    _decision_payload(
                        window_id=window_id,
                        frame=frame,
                        decision="uncertain",
                        confidence=0.0,
                        reasoning=f"skipped: review pack missing at {image_path}",
                        detection=None,
                        dry_run=False,
                    )
                )
                continue

            if max_calls is not None and calls_attempted >= max_calls:
                decisions.append(
                    _decision_payload(
                        window_id=window_id,
                        frame=frame,
                        decision="uncertain",
                        confidence=0.0,
                        reasoning="skipped: max_calls limit reached",
                        detection=None,
                        dry_run=not api_key_present,
                    )
                )
                continue

            result: VLMResult = vlm_backend(
                image_path if image_path.is_file() else None,
                frame=frame,
                candidates=candidates,
                model=model,
            )
            calls_attempted += 0 if result.dry_run else 1
            detection = result[0] if result and result.decision == "ball" else None
            decision = result.decision
            reasoning = result.reasoning
            confidence = result.confidence

            if decision == "ball" and detection is None:
                decision = "uncertain"
                confidence = 0.0
                reasoning = (
                    "VLM claimed ball but did not return a valid detection schema; "
                    "kept unresolved."
                )

            decisions.append(
                _decision_payload(
                    window_id=window_id,
                    frame=frame,
                    decision=decision,
                    confidence=confidence,
                    reasoning=reasoning,
                    detection=detection,
                    dry_run=result.dry_run,
                )
            )

    return {
        "version": VERSION,
        "generated": datetime.now(timezone.utc).isoformat(),
        "dry_run": not api_key_present,
        "model": model or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        "decisions": decisions,
        "summary": {
            "decisions": len(decisions),
            "ball": sum(item["decision"] == "ball" for item in decisions),
            "not_ball": sum(item["decision"] == "not_ball" for item in decisions),
            "uncertain": sum(item["decision"] == "uncertain" for item in decisions),
            "api_calls": calls_attempted,
            "skipped_ineligible": skipped_ineligible,
            "skipped_missing_pack": skipped_missing_pack,
        },
    }


def write_ai_decisions(
    queue_path: str | Path,
    packs_dir: str | Path,
    output_path: str | Path = "ai_decisions.json",
    *,
    model: str | None = None,
    max_calls: int | None = None,
) -> dict[str, Any]:
    with Path(queue_path).open("r", encoding="utf-8") as handle:
        queue_payload = json.load(handle)
    decisions = review_queue(queue_payload, packs_dir, model=model, max_calls=max_calls)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8") as handle:
        json.dump(decisions, handle, indent=2)
        handle.write("\n")
    return decisions


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run corridor-gated VLM review packs.")
    parser.add_argument("--queue", required=True)
    parser.add_argument("--packs-dir", default="ai_review_packs")
    parser.add_argument("--output", default="ai_decisions.json")
    parser.add_argument("--model", default=None)
    parser.add_argument("--max-calls", type=int, default=None)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    result = write_ai_decisions(
        args.queue,
        args.packs_dir,
        args.output,
        model=args.model,
        max_calls=args.max_calls,
    )
    print(
        "[vlm-reviewer] "
        f"dry_run={result['dry_run']} "
        f"decisions={result['summary']['decisions']} "
        f"api_calls={result['summary']['api_calls']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
