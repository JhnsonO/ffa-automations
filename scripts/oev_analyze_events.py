#!/usr/bin/env python3
"""Deterministic telemetry report for OEV follow-cam event artifacts.

Telemetry is diagnostic only. The rendered video still requires human product review.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib
import tempfile
from dataclasses import dataclass
from typing import Any, Iterable

TRACKED_STATES = ("Tracking", "Bridged", "Coasting", "Lost")
TRUSTED_STATES = {"Tracking", "Bridged"}


@dataclass
class Frame:
    index: int
    ball: dict[str, Any] | None = None
    pose: dict[str, Any] | None = None
    raw_ball_present: bool = False


def angular_delta(a: float, b: float) -> float:
    return (a - b + math.pi) % (2.0 * math.pi) - math.pi


def load_profile(config_path: pathlib.Path | None, profile_name: str | None) -> tuple[float | None, int | None, list[dict[str, Any]]]:
    if not config_path:
        return None, None, []
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not profile_name:
        raise ValueError("--profile-name is required with --config")
    profile = config["profiles"][profile_name]
    return float(profile["fps"]), int(profile["ball_class_id"]), list(profile.get("review_windows", []))


def parse_events(path: pathlib.Path, ball_class_id: int) -> dict[int, Frame]:
    frames: dict[int, Frame] = {}
    with path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, 1):
            if not raw.strip():
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at {path}:{line_no}: {exc}") from exc
            kind = event.get("kind")
            if kind not in {"detections_raw", "world_state", "pose_presented"}:
                continue
            index = int(event["frame_index"])
            frame = frames.setdefault(index, Frame(index=index))
            if kind == "world_state":
                ball = event.get("ball")
                frame.ball = ball if isinstance(ball, dict) else None
            elif kind == "pose_presented":
                pose = event.get("pose")
                frame.pose = pose if isinstance(pose, dict) else None
            else:
                detections = event.get("detections") or []
                frame.raw_ball_present = any(
                    isinstance(det, dict)
                    and det.get("class_id") == ball_class_id
                    and isinstance(det.get("position"), dict)
                    for det in detections
                )
    return frames


def is_known(frame: Frame) -> bool:
    return frame.ball is not None and frame.ball.get("state") != "Lost"


def is_trusted(frame: Frame) -> bool:
    return frame.ball is not None and frame.ball.get("state") in TRUSTED_STATES


def is_oof(frame: Frame) -> bool:
    if not is_known(frame) or not frame.pose:
        return False
    fov = frame.pose.get("fov_degrees")
    if fov is None:
        return False
    offset = abs(angular_delta(float(frame.ball["yaw"]), float(frame.pose["yaw"])))
    return offset > math.radians(float(fov) / 2.0)


def intervals(indices: Iterable[int], fps: float) -> list[dict[str, Any]]:
    ordered = sorted(set(indices))
    if not ordered:
        return []
    groups: list[tuple[int, int]] = []
    start = prev = ordered[0]
    for index in ordered[1:]:
        if index == prev + 1:
            prev = index
            continue
        groups.append((start, prev))
        start = prev = index
    groups.append((start, prev))
    result = []
    for start, end in groups:
        result.append({
            "start_frame": start,
            "end_frame": end,
            "start_s": start / fps,
            "end_s": (end + 1) / fps,
            "duration_s": (end - start + 1) / fps,
            "frames": end - start + 1,
        })
    result.sort(key=lambda item: (-item["frames"], item["start_frame"]))
    return result


def subset_metrics(frames: dict[int, Frame], fps: float, start_s: float | None = None, end_s: float | None = None) -> dict[str, Any]:
    selected = []
    for index in sorted(frames):
        t = index / fps
        if start_s is not None and t < start_s:
            continue
        if end_s is not None and t >= end_s:
            continue
        selected.append(frames[index])

    state_counts = collections.Counter()
    raw_present = known = trusted = known_oof = trusted_oof = none_count = 0
    oof_indices: list[int] = []
    trusted_oof_indices: list[int] = []
    jumps = {"gt_0_25_rad": 0, "gt_0_50_rad": 0, "gt_1_00_rad": 0}
    previous_known: Frame | None = None

    for frame in selected:
        raw_present += int(frame.raw_ball_present)
        if frame.ball is None:
            state_counts["None"] += 1
            none_count += 1
        else:
            state_counts[str(frame.ball.get("state") or "Unknown")] += 1
        if is_known(frame):
            known += 1
            if is_oof(frame):
                known_oof += 1
                oof_indices.append(frame.index)
            if previous_known and previous_known.index + 1 == frame.index:
                jump = math.hypot(
                    angular_delta(float(frame.ball["yaw"]), float(previous_known.ball["yaw"])),
                    float(frame.ball["pitch"]) - float(previous_known.ball["pitch"]),
                )
                jumps["gt_0_25_rad"] += int(jump > 0.25)
                jumps["gt_0_50_rad"] += int(jump > 0.50)
                jumps["gt_1_00_rad"] += int(jump > 1.00)
            previous_known = frame
        else:
            previous_known = None
        if is_trusted(frame):
            trusted += 1
            if is_oof(frame):
                trusted_oof += 1
                trusted_oof_indices.append(frame.index)

    total = len(selected)
    return {
        "frames": total,
        "start_s": start_s,
        "end_s": end_s,
        "raw_ball_present_frames": raw_present,
        "raw_ball_present_pct": (100.0 * raw_present / total) if total else None,
        "world_ball_none_frames": none_count,
        "world_ball_none_pct": (100.0 * none_count / total) if total else None,
        "state_counts": {name: state_counts.get(name, 0) for name in (*TRACKED_STATES, "None")},
        "known_frames": known,
        "known_horizontal_oof_frames": known_oof,
        "known_horizontal_oof_pct": (100.0 * known_oof / known) if known else None,
        "trusted_frames": trusted,
        "trusted_horizontal_oof_frames": trusted_oof,
        "trusted_horizontal_oof_pct": (100.0 * trusted_oof / trusted) if trusted else None,
        "trajectory_jumps": jumps,
        "longest_known_oof_intervals": intervals(oof_indices, fps)[:10],
        "longest_trusted_oof_intervals": intervals(trusted_oof_indices, fps)[:10],
    }


def log_metrics(path: pathlib.Path | None) -> dict[str, int]:
    keys = {
        "dormant_learned": "dormant candidate LEARNED",
        "dormant_released": "dormant candidate MOVED",
        "dormant_ignored": "ignore dormant candidate",
        "active_ball_switches": "active-ball switch",
    }
    counts = {key: 0 for key in keys}
    if not path or not path.exists():
        return counts
    text = path.read_text(encoding="utf-8", errors="replace")
    for key, needle in keys.items():
        counts[key] = text.count(needle)
    return counts


def compare_metrics(candidate: dict[str, Any], reference: dict[str, Any]) -> dict[str, Any]:
    keys = ["raw_ball_present_pct", "world_ball_none_pct", "known_horizontal_oof_pct", "trusted_horizontal_oof_pct", "known_frames", "trusted_frames"]
    result = {key: candidate[key] - reference[key] for key in keys}
    result["state_counts"] = {
        key: candidate["state_counts"].get(key, 0) - reference["state_counts"].get(key, 0)
        for key in set(candidate["state_counts"]) | set(reference["state_counts"])
    }
    return result


def render_markdown(report: dict[str, Any]) -> str:
    c = report["candidate"]["overall"]
    lines = [
        "# OEV harness telemetry", "",
        "> Telemetry is diagnostic only; the rendered video still requires human product review.", "",
        "## Candidate", "",
        f"- Frames: **{c['frames']}**",
        f"- Raw ball present: **{c['raw_ball_present_frames']} / {c['frames']} ({c['raw_ball_present_pct']:.2f}%)**",
        f"- `world.ball=None`: **{c['world_ball_none_frames']} ({c['world_ball_none_pct']:.2f}%)**",
        f"- States: Tracking **{c['state_counts']['Tracking']}**, Bridged **{c['state_counts']['Bridged']}**, Coasting **{c['state_counts']['Coasting']}**, Lost **{c['state_counts']['Lost']}**",
        f"- Known-ball horizontal OOF: **{c['known_horizontal_oof_pct']:.2f}%** ({c['known_horizontal_oof_frames']}/{c['known_frames']})",
        f"- Trusted Tracking/Bridged horizontal OOF: **{c['trusted_horizontal_oof_pct']:.2f}%** ({c['trusted_horizontal_oof_frames']}/{c['trusted_frames']})",
        f"- >1.0 rad single-frame trajectory jumps: **{c['trajectory_jumps']['gt_1_00_rad']}**", "",
    ]
    logs = report["candidate"].get("log_counts", {})
    if any(logs.values()):
        lines += ["## Tracker diagnostics", "",
            f"- Dormant learned: **{logs.get('dormant_learned', 0)}**",
            f"- Dormant released/moved: **{logs.get('dormant_released', 0)}**",
            f"- Dormant candidate suppressions: **{logs.get('dormant_ignored', 0)}**",
            f"- Forced/active-ball switch log entries: **{logs.get('active_ball_switches', 0)}**", ""]
    if report.get("reference"):
        d = report["delta_vs_reference"]
        lines += ["## Delta vs reference", "",
            f"- `world.ball=None`: **{d['world_ball_none_pct']:+.2f} pp**",
            f"- Known-ball horizontal OOF: **{d['known_horizontal_oof_pct']:+.2f} pp**",
            f"- Trusted horizontal OOF: **{d['trusted_horizontal_oof_pct']:+.2f} pp**",
            f"- Tracking frames: **{d['state_counts'].get('Tracking', 0):+d}**",
            f"- Bridged frames: **{d['state_counts'].get('Bridged', 0):+d}**", ""]
    lines += ["## Longest known-ball horizontal OOF intervals", ""]
    for item in c["longest_known_oof_intervals"][:8]:
        lines.append(f"- {item['start_s']:.2f}s–{item['end_s']:.2f}s — **{item['duration_s']:.2f}s**")
    if not c["longest_known_oof_intervals"]:
        lines.append("- None")
    lines += ["", "## Human-review windows", ""]
    for window in report["candidate"].get("windows", []):
        m = window["metrics"]
        pct = m["known_horizontal_oof_pct"] or 0.0
        lines.append(f"- **{window['label']}** ({window['start_s']:.1f}–{window['end_s']:.1f}s): None {m['world_ball_none_frames']}f; known OOF {pct:.1f}% ({m['known_horizontal_oof_frames']}/{m['known_frames']})")
    lines.append("")
    return "\n".join(lines)


def build_report(events: pathlib.Path, *, fps: float, ball_class_id: int, windows: list[dict[str, Any]], stitch_log: pathlib.Path | None = None, reference_events: pathlib.Path | None = None, reference_stitch_log: pathlib.Path | None = None) -> dict[str, Any]:
    candidate_frames = parse_events(events, ball_class_id)
    candidate = {"overall": subset_metrics(candidate_frames, fps), "log_counts": log_metrics(stitch_log), "windows": []}
    for window in windows:
        candidate["windows"].append({"label": str(window["label"]), "start_s": float(window["start_s"]), "end_s": float(window["end_s"]), "metrics": subset_metrics(candidate_frames, fps, float(window["start_s"]), float(window["end_s"]))})
    report: dict[str, Any] = {"fps": fps, "ball_class_id": ball_class_id, "candidate": candidate, "reference": None, "delta_vs_reference": None}
    if reference_events and reference_events.exists():
        reference_frames = parse_events(reference_events, ball_class_id)
        reference = {"overall": subset_metrics(reference_frames, fps), "log_counts": log_metrics(reference_stitch_log)}
        report["reference"] = reference
        report["delta_vs_reference"] = compare_metrics(candidate["overall"], reference["overall"])
    return report


def self_test() -> None:
    events = [
        {"kind": "detections_raw", "frame_index": 0, "detections": [{"class_id": 32, "position": {"yaw": 0, "pitch": 0}}]},
        {"kind": "world_state", "frame_index": 0, "ball": {"yaw": 0.0, "pitch": 0.0, "state": "Tracking"}},
        {"kind": "pose_presented", "frame_index": 0, "pose": {"yaw": 0.0, "pitch": 0.0, "fov_degrees": 40.0}},
        {"kind": "detections_raw", "frame_index": 1, "detections": []},
        {"kind": "world_state", "frame_index": 1, "ball": {"yaw": 1.0, "pitch": 0.0, "state": "Bridged"}},
        {"kind": "pose_presented", "frame_index": 1, "pose": {"yaw": 0.0, "pitch": 0.0, "fov_degrees": 40.0}},
        {"kind": "detections_raw", "frame_index": 2, "detections": []},
        {"kind": "world_state", "frame_index": 2, "ball": None},
        {"kind": "pose_presented", "frame_index": 2, "pose": {"yaw": 0.0, "pitch": 0.0, "fov_degrees": 40.0}},
        {"kind": "detections_raw", "frame_index": 3, "detections": []},
        {"kind": "world_state", "frame_index": 3, "ball": {"yaw": 0.0, "pitch": 0.0, "state": "Lost"}},
        {"kind": "pose_presented", "frame_index": 3, "pose": {"yaw": 0.0, "pitch": 0.0, "fov_degrees": 40.0}},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "events.jsonl"
        path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
        result = build_report(path, fps=2.0, ball_class_id=32, windows=[])["candidate"]["overall"]
        assert result["frames"] == 4
        assert result["raw_ball_present_frames"] == 1
        assert result["state_counts"] == {"Tracking": 1, "Bridged": 1, "Coasting": 0, "Lost": 1, "None": 1}
        assert result["known_frames"] == 2 and result["known_horizontal_oof_frames"] == 1
        assert result["trusted_frames"] == 2 and result["trusted_horizontal_oof_frames"] == 1
        assert result["world_ball_none_frames"] == 1
    print("OEV_ANALYZER_SELF_TEST=PASS")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=pathlib.Path)
    parser.add_argument("--stitch-log", type=pathlib.Path)
    parser.add_argument("--reference-events", type=pathlib.Path)
    parser.add_argument("--reference-stitch-log", type=pathlib.Path)
    parser.add_argument("--config", type=pathlib.Path)
    parser.add_argument("--profile-name")
    parser.add_argument("--fps", type=float)
    parser.add_argument("--ball-class-id", type=int)
    parser.add_argument("--json-out", type=pathlib.Path)
    parser.add_argument("--markdown-out", type=pathlib.Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test(); return 0
    if not args.events:
        parser.error("--events is required unless --self-test is used")
    profile_fps, profile_ball_class, windows = load_profile(args.config, args.profile_name)
    fps = args.fps or profile_fps
    ball_class_id = args.ball_class_id if args.ball_class_id is not None else profile_ball_class
    if not fps or fps <= 0:
        parser.error("fps must be supplied directly or through the profile")
    if ball_class_id is None:
        parser.error("ball class id must be supplied directly or through the profile")
    report = build_report(args.events, fps=float(fps), ball_class_id=int(ball_class_id), windows=windows, stitch_log=args.stitch_log, reference_events=args.reference_events, reference_stitch_log=args.reference_stitch_log)
    text = render_markdown(report)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True); args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True); args.markdown_out.write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
