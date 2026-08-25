#!/usr/bin/env bash
# EXPERIMENT ONLY: SoccerNet-v3D ball detector Phase 1 gate.
# Branch: experiment/soccernet-ball-detector-01
#
# This wrapper deliberately runs a PURE detector A/B first. Both yolo26m and
# yolo-sn-ball-opt receive the exact same decoded source frame and exact same
# 1920x1920 preprocessed tensor. Only after that detector-only measurement
# completes do we run the untouched canonical OEV control script, solely so
# the existing RunPod workflow retains its proven acceptance/artifact/Drive
# lifecycle. There is NO SoccerNet integration into OEV in this Phase 1 run.

set -uo pipefail
cd /tmp/oev_run

PHASE1_DIR=/tmp/oev_run/soccernet_phase1
CONTROL_MODEL=/runpod-volume/oev-runtime/models/yolo26m.onnx
SOCCERNET_PT=/tmp/oev_run/yolo-sn-ball-opt.pt
SOCCERNET_URL='https://github.com/mguti97/SoccerNet-v3D/releases/download/v1.0.0/yolo-sn-ball-opt.pt'
SOCCERNET_EXPECTED_SIZE=51266130
PHASE1_SCRIPT_URL='https://raw.githubusercontent.com/JhnsonO/ffa-automations/5ba248cb45ad398e7d8ebdc22fe5ea668ff5f264/scripts/oev_soccernet_phase1.py'
CONTROL_SCRIPT_URL='https://raw.githubusercontent.com/JhnsonO/ffa-automations/8819b3d79e247120445ab35a3fe493c3585cd317/runpod_sample_baseline_yolo26_remote.sh'

mkdir -p "$PHASE1_DIR"
: > phase1.log

echo '=== SoccerNet ball detector experiment: PHASE 1 PURE DETECTOR A/B ===' | tee -a phase1.log
printf 'branch_start_main=%s\n' '8819b3d79e247120445ab35a3fe493c3585cd317' | tee -a phase1.log
printf 'soccernet_source=%s\n' 'mguti97/SoccerNet-v3D release v1.0.0 yolo-sn-ball-opt.pt' | tee -a phase1.log
printf 'soccernet_repo_license=%s\n' 'GPL-2.0 repository license; production weight licensing requires separate legal review' | tee -a phase1.log

if [ ! -s left.mp4 ] || [ ! -s right.mp4 ]; then
  echo 'FATAL: pinned sample clips missing before Phase 1' | tee -a phase1.log
  exit 1
fi
if [ ! -s "$CONTROL_MODEL" ]; then
  echo "FATAL: accepted control model missing: $CONTROL_MODEL" | tee -a phase1.log
  exit 1
fi

# Preserve exact control runtime identity and weights before doing any work.
if [ -s /runpod-volume/oev-runtime/manifest.json ]; then
  cp /runpod-volume/oev-runtime/manifest.json "$PHASE1_DIR/control_runtime_manifest.json"
  echo '--- control runtime manifest ---' | tee -a phase1.log
  cat /runpod-volume/oev-runtime/manifest.json | tee -a phase1.log
fi
if [ -s /runpod-volume/oev-runtime/models/models.sha256 ]; then
  cp /runpod-volume/oev-runtime/models/models.sha256 "$PHASE1_DIR/control_models.sha256"
  echo '--- staged model hashes ---' | tee -a phase1.log
  cat /runpod-volume/oev-runtime/models/models.sha256 | tee -a phase1.log
fi
sha256sum "$CONTROL_MODEL" | tee "$PHASE1_DIR/control_yolo26m.sha256" | tee -a phase1.log
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames -show_entries format=duration -of json left.mp4 > "$PHASE1_DIR/left_source_probe.json"
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames -show_entries format=duration -of json right.mp4 > "$PHASE1_DIR/right_source_probe.json"

# Download the exact published release asset. No substitute is permitted.
curl -fL --retry 3 --retry-delay 2 "$SOCCERNET_URL" -o "$SOCCERNET_PT" 2>&1 | tee -a phase1.log
actual_size=$(stat -c %s "$SOCCERNET_PT" 2>/dev/null || echo 0)
if [ "$actual_size" != "$SOCCERNET_EXPECTED_SIZE" ]; then
  echo "FATAL: yolo-sn-ball-opt.pt size mismatch: expected=$SOCCERNET_EXPECTED_SIZE actual=$actual_size" | tee -a phase1.log
  exit 1
fi
sha256sum "$SOCCERNET_PT" | tee "$PHASE1_DIR/soccernet_pt.sha256" | tee -a phase1.log

# Use the SAME Ultralytics version recorded for the accepted staged YOLO26
# runtime where possible. If the manifest is absent, fail rather than guessing.
ULTRA_VERSION=$(python3 - <<'PY'
import json
p='/runpod-volume/oev-runtime/manifest.json'
try:
    print(json.load(open(p))['ultralytics_version'])
except Exception:
    raise SystemExit(1)
PY
) || {
  echo 'FATAL: could not resolve accepted Ultralytics version from runtime manifest' | tee -a phase1.log
  exit 1
}
echo "ultralytics_version=$ULTRA_VERSION" | tee -a phase1.log

python3 -m venv /tmp/soccernet-phase1-venv
/tmp/soccernet-phase1-venv/bin/pip install -q --upgrade pip
/tmp/soccernet-phase1-venv/bin/pip install -q "ultralytics==$ULTRA_VERSION" onnx onnxruntime-gpu opencv-python-headless 2>&1 | tee -a phase1.log

curl -fsSL "$PHASE1_SCRIPT_URL" -o /tmp/oev_run/oev_soccernet_phase1.py
if [ ! -s /tmp/oev_run/oev_soccernet_phase1.py ]; then
  echo 'FATAL: Phase 1 evaluator download failed' | tee -a phase1.log
  exit 1
fi

phase1_start=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "phase1_start=$phase1_start" | tee -a phase1.log
set +e
/tmp/soccernet-phase1-venv/bin/python3 -u /tmp/oev_run/oev_soccernet_phase1.py \
  --left /tmp/oev_run/left.mp4 \
  --right /tmp/oev_run/right.mp4 \
  --control-model "$CONTROL_MODEL" \
  --soccernet-pt "$SOCCERNET_PT" \
  --out-dir "$PHASE1_DIR" 2>&1 | tee -a phase1.log
phase1_rc=${PIPESTATUS[0]}
set -e
phase1_end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "phase1_end=$phase1_end phase1_rc=$phase1_rc" | tee -a phase1.log
if [ "$phase1_rc" -ne 0 ]; then
  echo 'FATAL: Phase 1 detector A/B failed; OEV integration was NOT attempted.' | tee -a phase1.log
  # Make the failure diagnosable through one of the standard pulled files.
  cp phase1.log segment.log
  exit 1
fi

test -s "$PHASE1_DIR/phase1_summary.json" || { echo 'FATAL: Phase 1 summary missing' | tee -a phase1.log; cp phase1.log segment.log; exit 1; }
test -s "$PHASE1_DIR/phase1_model_metadata.json" || { echo 'FATAL: model metadata missing' | tee -a phase1.log; cp phase1.log segment.log; exit 1; }
cat "$PHASE1_DIR/phase1_summary.txt" | tee -a phase1.log

echo '=== Phase 1 complete. NO SoccerNet candidate has entered tracker/world/panner. ===' | tee -a phase1.log

# Run the untouched control from the exact main SHA this experiment branched
# from. This is NOT Phase 2; it only preserves the existing workflow's normal
# OEV control artifact + Drive behavior while the detector gate is reviewed.
curl -fsSL "$CONTROL_SCRIPT_URL" -o /tmp/oev_run/run_control_original.sh
chmod +x /tmp/oev_run/run_control_original.sh
set +e
stdbuf -oL -eL /tmp/oev_run/run_control_original.sh
control_rc=$?
set -e

# Preserve Phase 1 evidence without changing the proven workflow's fixed remote
# artifact pull list: after the control no longer needs match.json, package the
# original calibration plus ALL detector comparison evidence into that slot.
# ZIP supports arbitrary filenames; the artifact entry is still named
# match.json solely because the parent workflow only pulls a fixed set of names.
mkdir -p /tmp/oev_run/phase1_payload
cp -a "$PHASE1_DIR"/. /tmp/oev_run/phase1_payload/ 2>/dev/null || true
cp phase1.log /tmp/oev_run/phase1_payload/phase1.log
[ -s match.json ] && cp match.json /tmp/oev_run/phase1_payload/original_match.json
python3 - <<'PYZIP'
import pathlib, zipfile
root=pathlib.Path('/tmp/oev_run/phase1_payload')
out=pathlib.Path('/tmp/oev_run/match.phase1.zip')
with zipfile.ZipFile(out,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
    for p in sorted(root.rglob('*')):
        if p.is_file():
            z.write(p,p.relative_to(root))
print('phase1 payload bytes',out.stat().st_size)
PYZIP
mv /tmp/oev_run/match.phase1.zip /tmp/oev_run/match.json

# Also surface the concise detector result in a normal text artifact.
{
  echo
  echo '=== SOCCERNET PHASE 1 DETECTOR-ONLY RESULT (integration not attempted) ==='
  cat "$PHASE1_DIR/phase1_summary.txt"
  echo 'Full Phase 1 payload is ZIP content stored in artifact entry match.json.'
} >> acceptance.log 2>/dev/null || true

exit "$control_rc"
