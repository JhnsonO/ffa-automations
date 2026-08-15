#!/usr/bin/env bash
# TEST-ONLY runner for experiment/lookahead-ball-containment-01.
# Reuses the exact YOLO26 sample baseline runner and changes only:
#   - fixed 44-degree FOV
#   - post-run containment + camera-step diagnostics from events.jsonl
# The Reco source anti-snap/containment guard is injected by runpod_bootstrap.sh.
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
new_log = '  echo "Panner overlay active: cluster_alpha=${CLUSTER_ALPHA_OVERRIDE}, fixed_fov=44.0deg, stable_ball_guard=v2_anti_snap (panner_overlay.json)" | tee -a stitch.log\n'
if s.count(old_log) != 1:
    raise SystemExit(f"expected exactly one panner-overlay log line, found {s.count(old_log)}")
p.write_text(s.replace(old_log, new_log))
PY

chmod +x "$BASE_SCRIPT"
echo "TEST_ONLY_PANNER_PROFILE=lookahead_ball_containment_02 cluster_alpha=${CLUSTER_ALPHA_OVERRIDE:-unset} fixed_fov=44.0 lookahead=${LOOKAHEAD:-unset} safe_margin_deg=3.0 coast_hold_frames=18 reacquire_confirm_frames=3 max_yaw_step_deg=0.75"

"$BASE_SCRIPT"

python3 - <<'PY' 2>&1 | tee containment_metrics.log
import json
import math
import re
from pathlib import Path

EVENTS = Path("events.jsonl")
if not EVENTS.is_file():
    raise SystemExit("FATAL: events.jsonl missing; cannot measure anti-snap experiment")

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

def angle_diff(a, b):
    d = a - b
    return math.atan2(math.sin(d), math.cos(d))

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

tracking_frames = []
coasting_frames = []
actual_outside_frames = []
safe_violation_frames = []
yaw_offsets = []
pitch_offsets = []

for idx in sorted(set(world_by_frame) & set(pan_by_frame)):
    world = world_by_frame[idx]
    ball = world.get("ball")
    if not isinstance(ball, dict):
        continue
    state = str(ball.get("state", "")).lower()
    if state == "coasting":
        coasting_frames.append(idx)
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

# Final rendered-camera step sizes. These are the numbers that correspond to
# visible one-frame snaps, using shortest yaw delta so panorama wrap is safe.
pan_steps = []
prev_idx = None
prev_pose = None
for idx in sorted(pan_by_frame):
    pose = pan_by_frame[idx]
    try:
        py = float(pose["yaw"])
        pp = float(pose["pitch"])
    except (KeyError, TypeError, ValueError):
        prev_idx = None
        prev_pose = None
        continue
    if prev_pose is not None and prev_idx is not None and idx == prev_idx + 1:
        yaw_step = abs(math.degrees(angle_diff(py, prev_pose[0])))
        pitch_step = abs(math.degrees(pp - prev_pose[1]))
        pan_steps.append((idx, yaw_step, pitch_step))
    prev_idx = idx
    prev_pose = (py, pp)

stitch_text = Path("stitch.log").read_text(errors="replace") if Path("stitch.log").is_file() else ""
guard_rows = [
    (float(a), float(b))
    for a, b in re.findall(
        r"BALL_CONTAINMENT_GUARD frame=\d+ yaw_correction_deg=([0-9.]+) pitch_correction_deg=([0-9.]+)",
        stitch_text,
    )
]
anti_snap_rows = [
    (float(a), float(b))
    for a, b in re.findall(
        r"BALL_ANTI_SNAP frame=\d+ yaw_reduction_deg=([0-9.]+) pitch_reduction_deg=([0-9.]+)",
        stitch_text,
    )
]
reacquire_holds = len(re.findall(r"BALL_GUARD_REACQUIRE_HOLD", stitch_text))
reacquire_accepts = len(re.findall(r"BALL_GUARD_REACQUIRE_ACCEPT", stitch_text))

max_yaw_step = max((x[1] for x in pan_steps), default=0.0)
max_pitch_step = max((x[2] for x in pan_steps), default=0.0)

metrics = {
    "experiment": "lookahead_ball_containment_02_anti_snap",
    "definition": "stable Tracking/Coasting ball guard after centered smoothing with final camera slew ceiling",
    "fov_expected_deg": 44.0,
    "output_aspect": "16:9",
    "safe_margin_deg": SAFE_MARGIN_DEG,
    "tracking_ball_frames": len(tracking_frames),
    "coasting_ball_frames": len(coasting_frames),
    "actual_outside_frame_frames": len(actual_outside_frames),
    "actual_outside_frame_pct": (100.0 * len(actual_outside_frames) / len(tracking_frames)) if tracking_frames else None,
    "safe_zone_violation_frames": len(safe_violation_frames),
    "safe_zone_violation_pct": (100.0 * len(safe_violation_frames) / len(tracking_frames)) if tracking_frames else None,
    "longest_actual_outside_streak_frames": longest_streak(actual_outside_frames),
    "max_abs_yaw_offset_deg": max(yaw_offsets, default=None),
    "max_abs_pitch_offset_deg": max(pitch_offsets, default=None),
    "camera_max_yaw_step_deg": max_yaw_step,
    "camera_max_pitch_step_deg": max_pitch_step,
    "camera_yaw_steps_gt_1deg": sum(1 for _, y, _ in pan_steps if y > 1.0),
    "camera_yaw_steps_gt_2deg": sum(1 for _, y, _ in pan_steps if y > 2.0),
    "camera_yaw_steps_gt_5deg": sum(1 for _, y, _ in pan_steps if y > 5.0),
    "camera_yaw_steps_gt_10deg": sum(1 for _, y, _ in pan_steps if y > 10.0),
    "guard_activations": len(guard_rows),
    "guard_max_yaw_correction_deg": max((a for a, _ in guard_rows), default=0.0),
    "anti_snap_activations": len(anti_snap_rows),
    "anti_snap_max_yaw_reduction_deg": max((a for a, _ in anti_snap_rows), default=0.0),
    "reacquire_jump_holds": reacquire_holds,
    "reacquire_jump_accepts": reacquire_accepts,
}

Path("containment_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
print(json.dumps(metrics, indent=2))

if not pan_steps:
    raise SystemExit("FATAL: no consecutive PanDecision frames available for anti-snap evaluation")
# This experiment prioritizes removing visible one-frame snapping. Ball
# containment is reported, not used as a hard failure, because the slew limiter
# intentionally chooses smooth movement over teleporting the camera.
if max_yaw_step > 0.85:
    raise SystemExit(
        f"FATAL: final camera yaw still jumps {max_yaw_step:.3f} deg in one frame; expected <=0.85 deg"
    )
if metrics["camera_yaw_steps_gt_2deg"]:
    raise SystemExit(
        f"FATAL: {metrics['camera_yaw_steps_gt_2deg']} final-camera yaw steps remain >2 deg/frame"
    )
PY
