#!/usr/bin/env bash
# OEV Test Runtime v1 -- benchmark run, executed INSIDE a pod started
# from the custom ghcr.io/.../oev-test-runtime image.
#
# Unlike runpod_followcam_remote.sh (frozen production script), this
# script assumes the environment is already fully built into the image:
#   - /opt/oev-runtime/bin/reco       (pinned Reco SHA, --features cuda)
#   - /opt/oev-runtime/models/yolo26{s,m,l,x}.onnx  (pre-exported @1920)
#   - left.mp4 / right.mp4            (pre-cut 19s benchmark pack,
#                                       uploaded to /tmp/oev_run/ by the
#                                       calling workflow BEFORE this
#                                       script runs)
#
# It performs ZERO rust builds, ZERO apt installs, ZERO yolo exports --
# only calibrate + stitch against the already-baked binary/models. This
# is the entire point of the runtime: prove the wall-clock difference
# against runpod_followcam_remote.sh's ~40min environment/download tax.
#
# Same tracking/panner/ROI/--no-zero-copy contract as the frozen
# production script -- CPU-detector-only, by the documented decision
# that GPU detection requires --zero-copy, which stays off in this
# ticket. Detector model is YOLO26m (not yolov8n) per the accepted A/B
# result.
#
# Exit codes: 1=env sanity failure, 2=calibrate/field_roi failure,
#             3=stitch failure, 4=missing output, 5=acceptance failure

set -uo pipefail
cd /tmp/oev_run || exit 1

RECO_BIN="/opt/oev-runtime/bin/reco"
MODEL_PATH="/opt/oev-runtime/models/yolo26m.onnx"

ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
echo "timing_env_sanity_start=$(ts)" | tee -a timing.log

# --- Environment sanity: everything must already exist. Any install/
#     build/export invocation appearing anywhere below this point is a
#     bug in this script, not expected behaviour -- the point of the
#     image is that none of that happens at experiment time.
[ -x "$RECO_BIN" ] || { echo "FATAL: $RECO_BIN not found/executable -- image did not bake reco-cli" | tee -a timing.log; exit 1; }
[ -f "$MODEL_PATH" ] || { echo "FATAL: $MODEL_PATH not found -- image did not bake YOLO26m" | tee -a timing.log; exit 1; }
[ -f left.mp4 ] || { echo "FATAL: left.mp4 (benchmark pack) not present in /tmp/oev_run" | tee -a timing.log; exit 1; }
[ -f right.mp4 ] || { echo "FATAL: right.mp4 (benchmark pack) not present in /tmp/oev_run" | tee -a timing.log; exit 1; }
sha256sum -c /opt/oev-runtime/models/models.sha256 --ignore-missing || { echo "FATAL: baked model checksum mismatch" | tee -a timing.log; exit 1; }
echo "Environment sanity PASSED: reco=$($RECO_BIN --version 2>&1), model=$MODEL_PATH present, benchmark pack present." | tee -a timing.log
echo "timing_env_sanity_end=$(ts)" | tee -a timing.log

echo "=== calibrate.log: reco calibrate ===" | tee calibrate.log
echo "timing_calibrate_start=$(ts)" | tee -a timing.log
LENS_PROFILE_URL="https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json"
curl -fsSL "$LENS_PROFILE_URL" -o hero10_wide_16by9.json
if [ ! -s hero10_wide_16by9.json ]; then
  echo "FATAL: failed to download lens profile from $LENS_PROFILE_URL" | tee -a calibrate.log
  exit 2
fi
stdbuf -oL -eL "$RECO_BIN" calibrate left.mp4 right.mp4 \
  --left-profile hero10_wide_16by9.json \
  --right-profile hero10_wide_16by9.json \
  -o match.json 2>&1 | tee -a calibrate.log
calibrate_rc=${PIPESTATUS[0]}
if [ "$calibrate_rc" -ne 0 ] || [ ! -f match.json ]; then
  echo "FATAL: reco calibrate failed (exit $calibrate_rc) or match.json missing" | tee -a calibrate.log
  exit 2
fi
echo "Calibrate OK: match.json written" | tee -a calibrate.log

# Same St Margaret's field ROI as production (runpod_followcam_remote.sh),
# reproduced verbatim -- do not re-derive.
python3 - <<'PYROI'
import json

with open("match.json") as f:
    match = json.load(f)

match["field_roi"] = {
    "left": [
        [0.1227, 0.9611], [0.0573, 0.6846], [0.1802, 0.6285],
        [0.2645, 0.5769], [0.4382, 0.4864], [0.4988, 0.4658],
        [0.5942, 0.4474], [0.7835, 0.4175], [0.9285, 0.3785],
        [1.0000, 1.0000], [0.1227, 1.0000],
    ],
    "right": [
        [0.0391, 0.4206], [0.0818, 0.4101], [0.1839, 0.4070],
        [0.2783, 0.4070], [0.3448, 0.4083], [0.4100, 0.4161],
        [0.4684, 0.4319], [0.6239, 0.4801], [0.7368, 0.5200],
        [0.7980, 0.5465], [0.7454, 0.9011], [0.7454, 1.0000],
        [0.0000, 1.0000],
    ],
}

with open("match.json", "w") as f:
    json.dump(match, f, indent=2)

assert len(match["field_roi"]["left"]) == 11
assert len(match["field_roi"]["right"]) == 13
print("field_roi injected: left=%d pts, right=%d pts" % (
    len(match["field_roi"]["left"]), len(match["field_roi"]["right"])))
PYROI
if [ $? -ne 0 ]; then
  echo "FATAL: field_roi injection into match.json failed" | tee -a calibrate.log
  exit 2
fi
echo "timing_calibrate_end=$(ts)" | tee -a timing.log

echo "=== stitch.log: reco stitch (YOLO26m@1920, CPU detector, --no-zero-copy) ===" | tee stitch.log
echo "timing_render_start=$(ts)" | tee -a timing.log
STITCH_ARGS=(stitch left.mp4 right.mp4 -c match.json -o followcam.mp4
  --model "$MODEL_PATH"
  --tracking field
  --panner-preset broadcast
  --lookahead 1.5
  --detection-interval 1
  --events events.jsonl
  --no-zero-copy
  --width 1920 --height 1080)
echo "reco stitch args: ${STITCH_ARGS[*]}" | tee -a stitch.log
stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log
stitch_rc=${PIPESTATUS[0]}
echo "timing_render_end=$(ts)" | tee -a timing.log
if [ "$stitch_rc" -ne 0 ]; then
  echo "FATAL: reco stitch failed (exit $stitch_rc), see stitch.log" | tee -a stitch.log
  exit 3
fi
if [ ! -f followcam.mp4 ]; then
  echo "FATAL: stitch reported success but followcam.mp4 missing" | tee -a stitch.log
  exit 4
fi
echo "Stitch OK: followcam.mp4 written" | tee -a stitch.log

echo "=== acceptance.log: verifying AI-driven follow-cam + zero-build/export/install evidence ===" | tee acceptance.log
python3 - <<'PY' 2>&1 | tee -a acceptance.log
import json, sys

accept_fail = False
stitch_log = open('stitch.log').read()

if "Autocam: tracking enabled" not in stitch_log and "Autocam: YOLO ball tracking enabled" not in stitch_log:
    print("FAIL: no autocam-tracking-enabled line found in stitch.log")
    accept_fail = True
else:
    print("OK: autocam tracking enabled per stitch.log")

try:
    lines = open('events.jsonl').read().splitlines()
except FileNotFoundError:
    print("FAIL: events.jsonl missing")
    accept_fail = True
    lines = []

detections_with_hits = 0
pan_yaws = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    if ev.get('kind') == 'detections_raw' and ev.get('detections'):
        detections_with_hits += 1
    if ev.get('kind') == 'pan_decision':
        pose = ev.get('pose') or {}
        yaw = pose.get('yaw')
        if yaw is not None:
            pan_yaws.append(yaw)

print(f"Total event lines: {len(lines)}")
print(f"detections_raw events with >=1 detection: {detections_with_hits}")
print(f"pan_decision events with a yaw value: {len(pan_yaws)}")

if detections_with_hits == 0:
    print("FAIL: no detections_raw event contained any detection")
    accept_fail = True

if len(pan_yaws) < 2:
    print("FAIL: fewer than 2 pan_decision events with a pose")
    accept_fail = True
else:
    yaw_spread = max(pan_yaws) - min(pan_yaws)
    print(f"pan_decision yaw spread (radians): {yaw_spread}")
    if yaw_spread < 1e-4:
        print("FAIL: pan_decision yaw never changes -- camera is static")
        accept_fail = True
    else:
        print("OK: pan_decision yaw shows real movement")

if accept_fail:
    sys.exit(1)
print("ACCEPTANCE (tracking): PASS")
PY
tracking_accept_rc=${PIPESTATUS[0]}

# Mechanical zero-build/export/install check: this run's own logs must
# contain no evidence of cargo build, yolo export, or apt-get install --
# that's the whole point of baking the image. Grep everything gathered
# in this script's working directory.
echo "=== Zero-build/export/install evidence check ===" | tee -a acceptance.log
build_evidence_fail=0
for pattern in 'cargo build' 'yolo export' 'apt-get install'; do
  if grep -qiE "$pattern" *.log 2>/dev/null; then
    echo "FAIL: found '$pattern' invocation evidence in this run's logs -- image is not actually eliminating this step" | tee -a acceptance.log
    build_evidence_fail=1
  else
    echo "OK: no '$pattern' evidence in this run's logs" | tee -a acceptance.log
  fi
done

if [ "$tracking_accept_rc" -ne 0 ] || [ "$build_evidence_fail" -ne 0 ]; then
  echo "FATAL: acceptance FAILED -- see acceptance.log" | tee -a acceptance.log
  exit 5
fi
echo "Acceptance OK: AI tracking confirmed active; zero rebuild/re-export/re-install evidence confirmed." | tee -a acceptance.log
echo "=== All stages completed ==="
