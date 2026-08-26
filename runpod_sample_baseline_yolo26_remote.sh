#!/usr/bin/env bash
# OEV EXPERIMENT ONLY: targeted optical-flow ball-bridge A/B for sample_02.
#
# This branch deliberately replaces the normal YOLO26 sample-baseline remote
# helper so the existing proven GitHub Actions -> RunPod orchestration can be
# reused without changing main. It consumes the staged 180s sample_02 clips but
# processes only source time 125s..155s for both variants.
#
# A = accepted control configuration.
# B = identical configuration + causal bounded Lucas-Kanade continuation fed
#     into the existing raw-detection -> panorama mapping -> BallTracker chain.
#
# Standard artifact names are preserved so the existing workflow pulls them:
#   segment.log      exact config/window + provenance
#   calibrate.log    shared calibration
#   stitch.log       control + experiment + flow logs
#   acceptance.log   A/B telemetry summary
#   match.json       shared calibration/ROI
#   events.jsonl     both traces, each line tagged variant=control|experiment
#   followcam.mp4    side-by-side render: CONTROL left, EXPERIMENT right

set -euo pipefail
cd /tmp/oev_run

RECO_SRC=/tmp/video-stitcher
RECO_BIN="$RECO_SRC/target/release/reco"
BASE_RECO_SHA=c8b0d74b537d192c7de8d2856de64620a82830cf
HELPER_COMMIT=c54bc70ab790eda67de8efffeac22f8f3c36bf03
CONTAINMENT_COMMIT=a1d909644d38ead44aecaba3ceed3a2a1efec054
WINDOW_START=125
WINDOW_END=155
WINDOW_DURATION=30
FAILURE_START=132
FAILURE_END=141
EXPECTED_MODEL=yolo26m

if [ ! -x "$RECO_BIN" ]; then
  echo "FATAL: validated Reco binary missing before experiment" | tee segment.log
  exit 1
fi
if [ "${YOLO26_VARIANT:-}" != "$EXPECTED_MODEL" ]; then
  echo "FATAL: this controlled experiment requires YOLO26_VARIANT=$EXPECTED_MODEL, got ${YOLO26_VARIANT:-unset}" | tee segment.log
  exit 1
fi
if [ ! -s left.mp4 ] || [ ! -s right.mp4 ]; then
  echo "FATAL: staged sample clips missing" | tee segment.log
  exit 1
fi

LEFT_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 left.mp4)
RIGHT_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 right.mp4)
FPS_EXPR=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate -of csv=p=0 left.mp4 | head -1)
FPS=$(python3 - "$FPS_EXPR" <<'PY'
import sys
from fractions import Fraction
print(float(Fraction(sys.argv[1])))
PY
)

cat > segment.log <<EOF
experiment=optical-flow-ball-bridge-01
sample_id=${SAMPLE_ID:-sample_02}
source_variant=180s staged sample clips
left_duration=$LEFT_DUR
right_duration=$RIGHT_DUR
source_fps=$FPS
control_window=${WINDOW_START}s-${WINDOW_END}s
failure_review_window=${FAILURE_START}s-${FAILURE_END}s
base_reco_sha=$BASE_RECO_SHA
model=$EXPECTED_MODEL
frame_stride=1
detection_interval=1
lookahead_seconds=1.5
panner_preset=broadcast
cluster_alpha=0.08
ball_weight=0.70
dead_zone_rad=0.06
velocity_alpha=0.08
max_velocity_rad_per_sec=0.31
fov_tight=38.0
fov_default=44.0
fov_wide=58.0
ball_containment_enabled=true
ball_containment_enter_fraction=0.80
ball_containment_exit_fraction=0.45
backward_ball_bridging=existing Reco c8b0d74b behavior unchanged
flow_only_change=temporary raw ball continuation during non-Tracking selected-ball gaps
flow_max_seconds=0.85
flow_method=pyramidal Lucas-Kanade + forward/backward consistency + median local motion
comparison_video=CONTROL left | EXPERIMENT right
EOF

# Accepted detector already staged on the persistent network volume.
YOLO_MODEL="/runpod-volume/oev-runtime/models/${EXPECTED_MODEL}.onnx"
if [ ! -s "$YOLO_MODEL" ]; then
  echo "FATAL: $YOLO_MODEL missing" | tee -a segment.log
  exit 1
fi
cp "$YOLO_MODEL" "$EXPECTED_MODEL.onnx"

# Reconstruct the accepted panner overlay explicitly rather than trusting an
# old prompt/default. These values are the canonical main harness profile.
cat > panner_overlay.json <<'JSON'
{
  "cluster_alpha": 0.08,
  "ball_weight": 0.70,
  "dead_zone_rad": 0.06,
  "velocity_alpha": 0.08,
  "max_velocity_rad_per_sec": 0.31,
  "fov_tight": 38.0,
  "fov_default": 44.0,
  "fov_wide": 58.0,
  "ball_containment_enabled": true,
  "ball_containment_enter_fraction": 0.80,
  "ball_containment_exit_fraction": 0.45
}
JSON

# Build one experiment binary containing (1) the already-accepted containment
# patch and (2) an inert flow hook. The hook is empty for control because the
# OEV_FLOW_BRIDGE_FILE environment variable is unset in that process.
echo "=== Applying accepted containment + inert experiment hook ===" | tee patch.log
curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${CONTAINMENT_COMMIT}/experiments/apply_ball_containment.py" \
  -o /tmp/apply_ball_containment.py
curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${HELPER_COMMIT}/experiments/apply_optical_flow_bridge_hook.py" \
  -o /tmp/apply_optical_flow_bridge_hook.py
python3 /tmp/apply_ball_containment.py 2>&1 | tee -a patch.log
python3 /tmp/apply_optical_flow_bridge_hook.py 2>&1 | tee -a patch.log
# Bootstrap installs rustup/cargo under /root/.cargo, but this workload runs in
# a fresh SSH shell. Restore that proven toolchain environment before rebuilding
# the experiment-only patched Reco binary.
. /root/.cargo/env
cd "$RECO_SRC"
git diff --check 2>&1 | tee -a /tmp/oev_run/patch.log
cargo build --release -p reco-cli --features cuda 2>&1 | tee -a /tmp/oev_run/patch.log
cd /tmp/oev_run
if [ ! -x "$RECO_BIN" ]; then
  echo "FATAL: patched experiment binary missing" | tee -a patch.log
  exit 2
fi

# Shared calibration, identical for A and B.
echo "=== calibrate.log: shared calibration ===" | tee calibrate.log
LENS_PROFILE_URL="https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json"
curl -fsSL "$LENS_PROFILE_URL" -o hero10_wide_16by9.json
stdbuf -oL -eL "$RECO_BIN" calibrate left.mp4 right.mp4 \
  --left-profile hero10_wide_16by9.json \
  --right-profile hero10_wide_16by9.json \
  -o match.json 2>&1 | tee -a calibrate.log

python3 - <<'PY'
import json
p='match.json'
m=json.load(open(p))
m['field_roi']={
 'left': [[0.1227,0.9611],[0.0573,0.6846],[0.1802,0.6285],[0.2645,0.5769],[0.4382,0.4864],[0.4988,0.4658],[0.5942,0.4474],[0.7835,0.4175],[0.9285,0.3785],[1.0,1.0],[0.1227,1.0]],
 'right': [[0.0391,0.4206],[0.0818,0.4101],[0.1839,0.4070],[0.2783,0.4070],[0.3448,0.4083],[0.4100,0.4161],[0.4684,0.4319],[0.6239,0.4801],[0.7368,0.5200],[0.7980,0.5465],[0.7454,0.9011],[0.7454,1.0],[0.0,1.0]],
}
json.dump(m,open(p,'w'),indent=2)
print('field_roi injected')
PY

echo "field_roi injected/validated" | tee -a calibrate.log

COMMON_ARGS=(stitch left.mp4 right.mp4 -c match.json
  --model "$EXPECTED_MODEL.onnx"
  --tracking field
  --panner-preset broadcast
  --panner-config panner_overlay.json
  --lookahead 1.5
  --detection-interval 1
  --frame-stride 1
  --start-time "$WINDOW_START"
  --end-time "$WINDOW_END"
  --width 1920 --height 1080)

# A. CONTROL. Same patched binary, but flow hook inert/unset.
echo "=== CONTROL 125s-155s ===" | tee control_stitch.log
unset OEV_FLOW_BRIDGE_FILE || true
stdbuf -oL -eL "$RECO_BIN" "${COMMON_ARGS[@]}" \
  -o control.mp4 --events control_events.jsonl \
  2>&1 | tee -a control_stitch.log
if [ ! -s control.mp4 ] || [ ! -s control_events.jsonl ]; then
  echo "FATAL: control output missing" | tee -a control_stitch.log
  exit 3
fi

# Build the causal flow continuation from control telemetry + original pixels.
# OpenCV is experiment-only tooling and does not alter detector/runtime config.
if ! python3 -c 'import cv2' >/dev/null 2>&1; then
  python3 -m pip install -q --disable-pip-version-check opencv-python-headless==4.10.0.84
fi
curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${HELPER_COMMIT}/experiments/optical_flow_ball_bridge.py" \
  -o /tmp/optical_flow_ball_bridge.py

echo "=== OPTICAL FLOW GENERATION ===" | tee flow_bridge.log
python3 /tmp/optical_flow_ball_bridge.py \
  --events control_events.jsonl \
  --left left.mp4 --right right.mp4 \
  --start-seconds "$WINDOW_START" \
  --duration-seconds "$WINDOW_DURATION" \
  --output flow_bridge.jsonl \
  --report flow_report.json \
  2>&1 | tee -a flow_bridge.log
FLOW_COUNT=$(wc -l < flow_bridge.jsonl | tr -d ' ')
echo "flow_synthetic_frames=$FLOW_COUNT" | tee -a flow_bridge.log
if [ "${FLOW_COUNT:-0}" -le 0 ]; then
  echo "FATAL: optical-flow generator produced no continuation frames; invalid experiment" | tee -a flow_bridge.log
  exit 4
fi

# B. EXPERIMENT. Exact same command; only this environment variable is added.
echo "=== EXPERIMENT 125s-155s + optical flow ===" | tee experiment_stitch.log
OEV_FLOW_BRIDGE_FILE=/tmp/oev_run/flow_bridge.jsonl \
stdbuf -oL -eL "$RECO_BIN" "${COMMON_ARGS[@]}" \
  -o experiment.mp4 --events experiment_events.jsonl \
  2>&1 | tee -a experiment_stitch.log
if [ ! -s experiment.mp4 ] || [ ! -s experiment_events.jsonl ]; then
  echo "FATAL: experiment output missing" | tee -a experiment_stitch.log
  exit 5
fi

# Telemetry-first A/B analysis.
curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${HELPER_COMMIT}/experiments/analyze_optical_flow_ball_bridge.py" \
  -o /tmp/analyze_optical_flow_ball_bridge.py
python3 /tmp/analyze_optical_flow_ball_bridge.py \
  --control control_events.jsonl \
  --experiment experiment_events.jsonl \
  --flow flow_bridge.jsonl \
  --fps "$FPS" \
  --duration-seconds "$WINDOW_DURATION" \
  --failure-start 7 --failure-end 16 \
  --output metrics.json > metrics_stdout.txt

# Preserve both traces through the workflow's existing single events.jsonl pull.
python3 - <<'PY'
import json
with open('events.jsonl','w') as out:
    for variant,path in [('control','control_events.jsonl'),('experiment','experiment_events.jsonl')]:
        for line in open(path):
            if not line.strip():
                continue
            ev=json.loads(line)
            ev['variant']=variant
            out.write(json.dumps(ev,separators=(',',':'))+'\n')
PY

# Render confirmation: same target window, side-by-side. No audio needed for this
# tracking/panning discrimination. Left is always control, right experiment.
ffmpeg -hide_banner -loglevel warning -y \
  -i control.mp4 -i experiment.mp4 \
  -filter_complex "[0:v]scale=960:540[c];[1:v]scale=960:540[e];[c][e]hstack=inputs=2[v]" \
  -map "[v]" -an -c:v libx264 -preset veryfast -crf 20 followcam.mp4

cat patch.log control_stitch.log flow_bridge.log experiment_stitch.log > stitch.log

python3 - <<'PY' > acceptance.log
import json
m=json.load(open('metrics.json'))
f=json.load(open('flow_report.json'))
print('EXPERIMENT_EXECUTION=PASS')
print('comparison_video=followcam.mp4 (CONTROL left | EXPERIMENT right)')
print('window=125.000s-155.000s')
print('failure_window=132.000s-141.000s')
print('flow_synthetic_frames_generated=',m['synthetic_flow_frames_generated'],sep='')
print('flow_acceptance=',json.dumps(m['flow_acceptance'],sort_keys=True),sep='')
print('control=',json.dumps(m['control'],sort_keys=True),sep='')
print('experiment=',json.dumps(m['experiment'],sort_keys=True),sep='')
print('pan_difference=',json.dumps(m['pan_difference'],sort_keys=True),sep='')
print('failure_pan_difference=',json.dumps(m['failure_pan_difference'],sort_keys=True),sep='')
print('flow_report=',json.dumps(f,sort_keys=True),sep='')
PY

# Basic validity gates. Scientific PASS/FAIL is decided after telemetry + visual
# review, not by making the CI job fail when the hypothesis itself is false.
grep -q "Autocam: tracking enabled" control_stitch.log
grep -q "Autocam: tracking enabled" experiment_stitch.log
grep -q "OEV flow bridge: loaded" experiment_stitch.log
[ -s followcam.mp4 ]
[ -s events.jsonl ]
[ -s acceptance.log ]

echo "=== A/B metrics ==="
cat acceptance.log
exit 0
