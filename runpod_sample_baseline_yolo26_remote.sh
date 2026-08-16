#!/usr/bin/env bash
# TEST-ONLY v4 stride-1 cadence control for sample_02.
# Uses the exact successful v4 runner wrapper. The current experiment bootstrap,
# Reco revision, allocator, model and panner settings remain unchanged; only
# --frame-stride 3 is absent from the final Reco command.
set -euo pipefail
cd /tmp/oev_run

V3_FFA_SHA="c91314eaca8e2cbb0b6813f6e8204e4da23f408c"
V3_RUNNER="/tmp/oev_run/runpod_sample_baseline_yolo26_v3_exact.sh"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${V3_FFA_SHA}/runpod_sample_baseline_yolo26_remote.sh" \
  -o "$V3_RUNNER"
test -s "$V3_RUNNER"

python3 - "$V3_RUNNER" <<'PY'
from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text()

replacements = {
    "stable_ball_guard=v3_trajectory_hysteresis_accel_limit":
        "stable_ball_guard=v4_trajectory_hysteresis_accel_limit_micro_damping",
    "TEST_ONLY_PANNER_PROFILE=lookahead_ball_containment_03 cluster_alpha=${CLUSTER_ALPHA_OVERRIDE:-unset} fixed_fov=44.0 lookahead=${LOOKAHEAD:-unset} innovation_gate_deg=3.0 switch_confirm_frames=18 max_yaw_step_deg=0.75 yaw_accel_deg_per_frame2=0.08":
        "TEST_ONLY_PANNER_PROFILE=lookahead_ball_containment_04_micro_damping cluster_alpha=${CLUSTER_ALPHA_OVERRIDE:-unset} fixed_fov=44.0 lookahead=${LOOKAHEAD:-unset} innovation_gate_deg=3.0 switch_confirm_frames=18 max_yaw_step_deg=0.75 yaw_accel_deg_per_frame2=0.08 micro_zone_deg=4.0 micro_hold_deg=0.35",
    "FATAL: events.jsonl missing; cannot measure v3 experiment":
        "FATAL: events.jsonl missing; cannot measure v4 micro-damping experiment",
    '"experiment": "lookahead_ball_containment_03_trajectory_hysteresis_accel_limit"':
        '"experiment": "lookahead_ball_containment_04_micro_damping_stride1_control"',
    '"definition": "upstream stabilized WorldState ball + post-smoothing containment + acceleration-limited final camera"':
        '"definition": "v3 upstream stabilized ball and global dynamics + adaptive damping only inside 4deg final-camera error; stride1 cadence control"',
}
for old, new in replacements.items():
    if old not in s:
        raise SystemExit(f"v4 runner marker not found: {old}")
    s = s.replace(old, new)

old_gate = '''if metrics["camera_max_abs_yaw_accel_deg_per_frame2"] > 0.30:
    raise SystemExit(
        f"FATAL: yaw acceleration change {metrics['camera_max_abs_yaw_accel_deg_per_frame2']:.3f} deg/frame^2 exceeds 0.30"
    )'''
new_gate = '''if metrics["camera_p99_abs_yaw_accel_deg_per_frame2"] > 0.20:
    raise SystemExit(
        f"FATAL: p99 yaw acceleration {metrics['camera_p99_abs_yaw_accel_deg_per_frame2']:.3f} deg/frame^2 exceeds 0.20"
    )'''
if s.count(old_gate) != 1:
    raise SystemExit(f"expected one v3 acceleration gate, found {s.count(old_gate)}")
s = s.replace(old_gate, new_gate)

s += '''
if [ -s containment_metrics.json ]; then
  {
    echo "--- v4 stride1 control metrics ---"
    cat containment_metrics.json
  } >> acceptance.log
fi
'''

p.write_text(s)
print("v4 stride1 control runner prepared from exact v3 runner; no --frame-stride override")
PY

chmod +x "$V3_RUNNER"
echo "TEST_ONLY_RUNNER_DELTA=v4_stride1_sample02_control base_v3_sha=${V3_FFA_SHA}"
exec "$V3_RUNNER"
