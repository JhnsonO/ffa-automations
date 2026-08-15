#!/usr/bin/env bash
# TEST-ONLY runner for experiment/lookahead-ball-containment-01.
# Exact same standardized 180s clip/settings as v2; only the source-side
# ball-signal hysteresis and final camera dynamics differ.
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
new_log = '  echo "Panner overlay active: cluster_alpha=${CLUSTER_ALPHA_OVERRIDE}, fixed_fov=44.0deg, stable_ball_guard=v3_trajectory_hysteresis_accel_limit (panner_overlay.json)" | tee -a stitch.log\n'
if s.count(old_log) != 1:
    raise SystemExit(f"expected exactly one panner-overlay log line, found {s.count(old_log)}")
p.write_text(s.replace(old_log, new_log))
PY

chmod +x "$BASE_SCRIPT"
echo "TEST_ONLY_PANNER_PROFILE=lookahead_ball_containment_03 cluster_alpha=${CLUSTER_ALPHA_OVERRIDE:-unset} fixed_fov=44.0 lookahead=${LOOKAHEAD:-unset} innovation_gate_deg=3.0 switch_confirm_frames=18 max_yaw_step_deg=0.75 yaw_accel_deg_per_frame2=0.08"

"$BASE_SCRIPT"

python3 - <<'PY' 2>&1 | tee containment_metrics.log
import json
import math
import re
from pathlib import Path

EVENTS = Path("events.jsonl")
if not EVENTS.is_file():
    raise SystemExit("FATAL: events.jsonl missing; cannot measure v3 experiment")

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

def pct(values, q):
    if not values:
        return 0.0
    vals = sorted(values)
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    frac = pos - lo
    return vals[lo] * (1.0 - frac) + vals[hi] * frac

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
    if state not in ("tracking", "coasting"):
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

    if state == "tracking":
        tracking_frames.append(idx)
    yaw_offsets.append(math.degrees(dy))
    pitch_offsets.append(math.degrees(dp))

    if dy > half_h or dp > half_v:
        actual_outside_frames.append(idx)

    safe_h = max(0.0, half_h - SAFE_MARGIN)
    safe_v = max(0.0, half_v - SAFE_MARGIN)
    if dy > safe_h or dp > safe_v:
        safe_violation_frames.append(idx)

steps = []
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
        yaw_step = math.degrees(angle_diff(py, prev_pose[0]))
        pitch_step = math.degrees(pp - prev_pose[1])
        steps.append((idx, yaw_step, pitch_step))
    prev_idx = idx
    prev_pose = (py, pp)

yaw_accels = []
pitch_accels = []
reversals_02 = 0
reversals_03 = 0
for (_, y0, p0), (idx, y1, p1) in zip(steps, steps[1:]):
    yaw_accels.append((idx, y1 - y0))
    pitch_accels.append((idx, p1 - p0))
    if y0 * y1 < 0:
        if abs(y0) > 0.2 and abs(y1) > 0.2:
            reversals_02 += 1
        if abs(y0) > 0.3 and abs(y1) > 0.3:
            reversals_03 += 1

stitch_text = Path("stitch.log").read_text(errors="replace") if Path("stitch.log").is_file() else ""
guard_rows = [
    (float(a), float(b))
    for a, b in re.findall(
        r"BALL_CONTAINMENT_GUARD frame=\d+ yaw_correction_deg=([0-9.]+) pitch_correction_deg=([0-9.]+)",
        stitch_text,
    )
]
dynamics_rows = [
    (float(a), float(b))
    for a, b in re.findall(
        r"BALL_CAMERA_DYNAMICS frame=\d+ yaw_reduction_deg=([0-9.]+) pitch_reduction_deg=([0-9.]+)",
        stitch_text,
    )
]
signal_holds = len(re.findall(r"BALL_SIGNAL_HOLD", stitch_text))
signal_switch_accepts = len(re.findall(r"BALL_SIGNAL_SWITCH_ACCEPT", stitch_text))

abs_yaw_steps = [abs(y) for _, y, _ in steps]
abs_pitch_steps = [abs(p) for _, _, p in steps]
abs_yaw_accels = [abs(a) for _, a in yaw_accels]
abs_pitch_accels = [abs(a) for _, a in pitch_accels]

metrics = {
    "experiment": "lookahead_ball_containment_03_trajectory_hysteresis_accel_limit",
    "definition": "upstream stabilized WorldState ball + post-smoothing containment + acceleration-limited final camera",
    "fov_expected_deg": 44.0,
    "safe_margin_deg": SAFE_MARGIN_DEG,
    "tracking_ball_frames": len(tracking_frames),
    "coasting_ball_frames": len(coasting_frames),
    "actual_outside_frame_frames_tracking_or_coasting": len(actual_outside_frames),
    "safe_zone_violation_frames_tracking_or_coasting": len(safe_violation_frames),
    "longest_actual_outside_streak_frames": longest_streak(actual_outside_frames),
    "max_abs_yaw_offset_deg": max(yaw_offsets, default=None),
    "camera_max_yaw_step_deg": max(abs_yaw_steps, default=0.0),
    "camera_p99_yaw_step_deg": pct(abs_yaw_steps, 0.99),
    "camera_max_pitch_step_deg": max(abs_pitch_steps, default=0.0),
    "camera_max_abs_yaw_accel_deg_per_frame2": max(abs_yaw_accels, default=0.0),
    "camera_p99_abs_yaw_accel_deg_per_frame2": pct(abs_yaw_accels, 0.99),
    "camera_max_abs_pitch_accel_deg_per_frame2": max(abs_pitch_accels, default=0.0),
    "camera_adjacent_direction_reversals_gt_0_2deg": reversals_02,
    "camera_adjacent_direction_reversals_gt_0_3deg": reversals_03,
    "camera_yaw_steps_gt_1deg": sum(1 for x in abs_yaw_steps if x > 1.0),
    "guard_activations": len(guard_rows),
    "guard_max_yaw_correction_deg": max((a for a, _ in guard_rows), default=0.0),
    "camera_dynamics_activations": len(dynamics_rows),
    "camera_dynamics_max_yaw_reduction_deg": max((a for a, _ in dynamics_rows), default=0.0),
    "ball_signal_holds": signal_holds,
    "ball_signal_switch_accepts": signal_switch_accepts,
}

Path("containment_metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
print(json.dumps(metrics, indent=2))

if not steps:
    raise SystemExit("FATAL: no consecutive PanDecision frames available")
if metrics["camera_max_yaw_step_deg"] > 0.80:
    raise SystemExit(
        f"FATAL: final camera yaw step {metrics['camera_max_yaw_step_deg']:.3f} deg exceeds 0.80"
    )
if metrics["camera_max_abs_yaw_accel_deg_per_frame2"] > 0.30:
    raise SystemExit(
        f"FATAL: yaw acceleration change {metrics['camera_max_abs_yaw_accel_deg_per_frame2']:.3f} deg/frame^2 exceeds 0.30"
    )
if reversals_03:
    raise SystemExit(
        f"FATAL: {reversals_03} adjacent hard direction reversals remain above 0.3 deg/frame"
    )
PY
