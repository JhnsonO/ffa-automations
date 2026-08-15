#!/usr/bin/env bash
# TEST-ONLY runner for experiment/lookahead-ball-containment-01.
# Reuses the exact YOLO26 sample baseline runner, changes only:
#   - fixed 44-degree FOV (same prior panner experiment)
#   - post-run containment measurement from events.jsonl
# The Reco source containment guard itself is injected by runpod_bootstrap.sh.
set -euo pipefail
cd /tmp/oev_run

BASE_SHA="aad4aedcf8aa6f949c4b80bbbab5580dd322d24a"
BASE_URL="https://raw.githubusercontent.com/JhnsonO/ffa-automations/${BASE_SHA}/runpod_sample_baseline_yolo26_remote.sh"
BASE_SCRIPT="/tmp/oev_run/runpod_sample_baseline_yolo26_base.sh"

curl -fsSL "$BASE_URL" -o "$BASE_SCRIPT"
test -s "$BASE_SCRIPT"

python3 - "$BASE_SCRIPT" <<'PY'
from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text()
old = '  echo "{\\"cluster_alpha\\": ${CLUSTER_ALPHA_OVERRIDE}}" > panner_overlay.json\n'
new = r'''  printf '{"cluster_alpha": %s, "fov_tight": 44.0, "fov_wide": 44.0, "fov_default": 44.0}\n' "$CLUSTER_ALPHA_OVERRIDE" > panner_overlay.json''' + "\n"
if s.count(old) != 1:
    raise SystemExit(f"expected exactly one panner-overlay line, found {s.count(old)}")
s = s.replace(old, new)

old_log = '  echo "Panner overlay active: cluster_alpha=${CLUSTER_ALPHA_OVERRIDE} (panner_overlay.json)" | tee -a stitch.log\n'
new_log = '  echo "Panner overlay active: cluster_alpha=${CLUSTER_ALPHA_OVERRIDE}, fixed_fov=44.0deg, hard_tracking_ball_guard=post_smooth (panner_overlay.json)" | tee -a stitch.log\n'
if s.count(old_log) != 1:
    raise SystemExit(f"expected exactly one panner-overlay log line, found {s.count(old_log)}")
p.write_text(s.replace(old_log, new_log))
PY

chmod +x "$BASE_SCRIPT"
echo "TEST_ONLY_PANNER_PROFILE=lookahead_ball_containment_01 cluster_alpha=${CLUSTER_ALPHA_OVERRIDE:-unset} fixed_fov=44.0 lookahead=${LOOKAHEAD:-unset} safe_margin_deg=3.0"

"$BASE_SCRIPT"

python3 - <<'PY' 2>&1 | tee containment_metrics.log
import json
import math
import re
from pathlib import Path

EVENTS = Path("events.jsonl")
if not EVENTS.is_file():
    raise SystemExit("FATAL: events.jsonl missing; cannot measure containment")

world_by_frame = {}
pan_by_frame = {}

for raw in EVENTS.read_text().splitlines():
    raw = raw.strip()
    if not raw:
        continue
    try:
        ev = json.loads(raw)
    except json.JSONDecodeError:
        continue
    kind = ev.get("kind")
    idx = ev.get("frame_index")
    if idx is None:
        continue
    if kind == "world_state":
        world_by_frame[idx] = ev
    elif kind == "pan_decision":
        pan_by_frame[idx] = ev.get("pose") or {}

ASPECT = 16.0 / 9.0
SAFE_MARGIN_DEG = 3.0
SAFE_MARGIN = math.radians(SAFE_MARGIN_DEG)

tracking_frames = []
actual_outside_frames = []
safe_violation_frames = []
yaw_offsets = []
pitch_offsets = []

def angle_diff(a, b):
    d = a - b
    return math.atan2(math.sin(d), math.cos(d))

for idx in sorted(set(world_by_frame) & set(pan_by_frame)):
    world = world_by_frame[idx]
    ball = world.get("ball")
    if not isinstance(ball, dict):
        continue
    state = str(ball.get("state", "")).lower()
    if state != "tracking":
        continue

    pose = pan_by_frame[idx]
    try:
        by = float(ball["yaw"])
        bp = float(ball["pitch"])
        py = float(pose["yaw"])
        pp = float(pose["pitch"])
        hfov_deg = float(pose["fov_degrees"])
    except (KeyError, TypeError, ValueError):
        continue
    if not all(math.isfinite(x) for x in (by, bp, py, pp, hfov_deg)) or hfov_deg <= 0:
        continue

    half_h = math.radians(hfov_deg * 0.5)
    half_v = math.atan(math.tan(half_h) / ASPECT)
    dy = abs(angle_diff(by, py))
    dp = abs(bp - pp)

    tracking_frames.append(idx)
    yaw_offsets.append(math.degrees(dy))
    pitch_offsets.append(math.degrees(dp))

    if dy > half_h or dp > half_v:
        actual_outside_frames.append(idx)

    safe_h = max(0.0, half_h - SAFE_MARGIN)
    safe_v = max(0.0, half_v - SAFE_MARGIN)
    if dy > safe_h or dp > safe_v:
        safe_violation_frames.append(idx)

def longest_streak(frames):
    best = cur = 0
    prev = None
    for idx in frames:
        if prev is not None and idx == prev + 1:
            cur += 1
        else:
            cur = 1
        best = max(best, cur)
        prev = idx
    return best

stitch_text = Path("stitch.log").read_text(errors="replace") if Path("stitch.log").is_file() else ""
guard_rows = [
    (float(a), float(b))
    for a, b in re.findall(
        r"BALL_CONTAINMENT_GUARD frame=\d+ yaw_correction_deg=([0-9.]+) pitch_correction_deg=([0-9.]+)",
        stitch_text,
    )
]

n = len(tracking_frames)
metrics = {
    "experiment": "lookahead_ball_containment_01",
    "definition": "fresh Tracking ball vs final post-centered-smooth PanDecision",
    "fov_expected_deg": 44.0,
    "output_aspect": "16:9",
    "safe_margin_deg": SAFE_MARGIN_DEG,
    "tracking_ball_frames": n,
    "actual_outside_frame_frames": len(actual_outside_frames),
    "actual_outside_frame_pct": (100.0 * len(actual_outside_frames) / n) if n else None,
    "safe_zone_violation_frames": len(safe_violation_frames),
    "safe_zone_violation_pct": (100.0 * len(safe_violation_frames) / n) if n else None,
    "longest_actual_outside_streak_frames": longest_streak(actual_outside_frames),
    "longest_safe_zone_violation_streak_frames": longest_streak(safe_violation_frames),
    "max_abs_yaw_offset_deg": max(yaw_offsets, default=None),
    "max_abs_pitch_offset_deg": max(pitch_offsets, default=None),
    "guard_activations": len(guard_rows),
    "guard_max_yaw_correction_deg": max((a for a, _ in guard_rows), default=0.0),
    "guard_max_pitch_correction_deg": max((b for _, b in guard_rows), default=0.0),
}

Path("containment_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
print(json.dumps(metrics, indent=2))

# Experiment acceptance: when the tracker has a fresh ball measurement, the
# final requested viewport should never place it outside the actual frame.
# Safe-margin violations are reported diagnostically but do not fail the job:
# coverage clamping / geometry may make a 3-degree margin impossible near edges.
if n == 0:
    raise SystemExit("FATAL: no Tracking-ball frames available for containment evaluation")
if actual_outside_frames:
    raise SystemExit(
        f"FATAL: {len(actual_outside_frames)}/{n} Tracking-ball frames remain outside final viewport"
    )
PY
