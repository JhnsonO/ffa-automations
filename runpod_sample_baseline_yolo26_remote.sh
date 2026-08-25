#!/usr/bin/env bash
# CONTROL for experiment/yolo-highres-01.
# Accepted v4 stride-1 sample_02 runner, current workflow/bootstrap/ReCo bridge.
# Passive nvidia-smi sampling is instrumentation only.
set -euo pipefail
cd /tmp/oev_run

V3_FFA_SHA="c91314eaca8e2cbb0b6813f6e8204e4da23f408c"
V3_RUNNER="/tmp/oev_run/runpod_sample_baseline_yolo26_v3_exact.sh"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${V3_FFA_SHA}/runpod_sample_baseline_yolo26_remote.sh" \
  -o "$V3_RUNNER"
test -s "$V3_RUNNER"

# Instrument the exact nested base runner without changing detector/tracker/panner behavior.
python3 - "$V3_RUNNER" <<'PY_INSTRUMENT'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text()
marker = 'test -s "$BASE_SCRIPT"\n'
if s.count(marker) != 1:
    raise SystemExit(f"expected one V3 base-script marker, found {s.count(marker)}")
inject = r"""python3 - "$BASE_SCRIPT" <<'PY_BASE_INSTRUMENT'
from pathlib import Path
import sys
p = Path(sys.argv[1])
s = p.read_text()
old = 'stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log\nstitch_rc=${PIPESTATUS[0]}\n'
new = r'''GPU_TELEMETRY=gpu_telemetry.csv
echo "timestamp_ms,gpu_util_pct,memory_used_mib,memory_total_mib" > "$GPU_TELEMETRY"
( while true; do
    ts=$(date +%s%3N)
    vals=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ') || true
    [ -n "$vals" ] && echo "$ts,$vals" >> "$GPU_TELEMETRY"
    sleep 1
  done ) &
GPU_MON_PID=$!
stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log
stitch_rc=${PIPESTATUS[0]}
kill "$GPU_MON_PID" 2>/dev/null || true
wait "$GPU_MON_PID" 2>/dev/null || true
'''
if s.count(old) != 1:
    raise SystemExit(f"expected one base stitch invocation, found {s.count(old)}")
p.write_text(s.replace(old, new, 1))
PY_BASE_INSTRUMENT
"""
p.write_text(s.replace(marker, marker + inject, 1))
PY_INSTRUMENT

# Exact accepted v4 stride-1 adaptation from experiment/sample02-v4-stride1-control.
python3 - "$V3_RUNNER" <<'PY_V4'
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
        '"experiment": "yolo_resolution_control_1920_v4_stride1"',
    '"definition": "upstream stabilized WorldState ball + post-smoothing containment + acceleration-limited final camera"':
        '"definition": "accepted v4 stride1 camera behavior; YOLO26m 1920 control"',
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
s += '''\nif [ -s containment_metrics.json ]; then\n  {\n    echo "--- yolo resolution control v4 stride1 metrics ---"\n    cat containment_metrics.json\n  } >> acceptance.log\nfi\n'''
p.write_text(s)
PY_V4

chmod +x "$V3_RUNNER"
echo "TEST_ONLY_RUNNER_DELTA=yolo_resolution_control_1920 accepted_v4_stride1=1 base_v3_sha=${V3_FFA_SHA}"
set +e
"$V3_RUNNER"
base_rc=$?
set -e

if [ -s gpu_telemetry.csv ]; then
  set +e
  python3 - <<'PYGPU' | tee -a acceptance.log
import csv, math, statistics
rows=[]
with open('gpu_telemetry.csv', newline='') as f:
    for row in csv.DictReader(f):
        try: rows.append((float(row['gpu_util_pct']), float(row['memory_used_mib']), float(row['memory_total_mib'])))
        except (KeyError, TypeError, ValueError): pass
print('--- passive GPU telemetry ---')
print(f'gpu_samples={len(rows)}')
if rows:
    def pct(xs,p):
        ys=sorted(xs); pos=(len(ys)-1)*p; lo=math.floor(pos); hi=math.ceil(pos)
        return ys[lo] if lo==hi else ys[lo]+(ys[hi]-ys[lo])*(pos-lo)
    u=[r[0] for r in rows]; m=[r[1] for r in rows]
    print(f'gpu_util_mean_pct={statistics.fmean(u):.2f}')
    print(f'gpu_util_p95_pct={pct(u,.95):.2f}')
    print(f'gpu_util_max_pct={max(u):.2f}')
    print(f'vram_mean_mib={statistics.fmean(m):.1f}')
    print(f'vram_p95_mib={pct(m,.95):.1f}')
    print(f'vram_max_mib={max(m):.1f}')
    print(f'vram_total_mib={rows[0][2]:.1f}')
PYGPU
  set -e
fi
exit "$base_rc"
