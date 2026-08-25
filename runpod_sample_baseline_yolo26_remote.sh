#!/usr/bin/env bash
# TEST-ONLY controlled YOLO inference-resolution experiment.
# Base runner is pinned to the exact main HEAD this branch was created from.
# Behavioural delta: YOLO26m static ONNX input 1920x1920 -> 2560x2560 only.
set -euo pipefail
cd /tmp/oev_run

BASE_FFA_SHA="21184152f96e9d6938fb02e849d6b9bc0c64d387"
BASE_RUNNER="/tmp/oev_run/runpod_sample_baseline_yolo26_1920_control.sh"
HIGHRES=2560
ULTRALYTICS_VERSION="8.4.118"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${BASE_FFA_SHA}/runpod_sample_baseline_yolo26_remote.sh" \
  -o "$BASE_RUNNER"
test -s "$BASE_RUNNER"

python3 - "$BASE_RUNNER" <<'PY'
from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text()
old = r''': "${YOLO26_VARIANT:=yolo26m}"
YOLO_MODEL="/runpod-volume/oev-runtime/models/${YOLO26_VARIANT}.onnx"
if [ ! -s "$YOLO_MODEL" ]; then
  echo "FATAL: ${YOLO_MODEL} not found on attached volume -- run oev-populate-volume.yml for this datacenter first, no fresh-export fallback for YOLO26" | tee -a segment.log
  exit 1
fi
echo "Using YOLO model: $YOLO_MODEL (YOLO26 A/B variant, no re-export)" | tee -a segment.log
cp "$YOLO_MODEL" "/tmp/oev_run/${YOLO26_VARIANT}.onnx"
'''
new = r''': "${YOLO26_VARIANT:=yolo26m}"
if [ "$YOLO26_VARIANT" != "yolo26m" ]; then
  echo "FATAL: resolution experiment is pinned to yolo26m; got ${YOLO26_VARIANT}" | tee -a segment.log
  exit 1
fi
CONTROL_MODEL="/runpod-volume/oev-runtime/models/yolo26m.onnx"
if [ ! -s "$CONTROL_MODEL" ]; then
  echo "FATAL: canonical YOLO26m control model missing: $CONTROL_MODEL" | tee -a segment.log
  exit 1
fi

echo "=== YOLO resolution experiment model preparation ===" | tee -a segment.log
echo "experiment_variable=inference_resolution" | tee -a segment.log
echo "control_resolution=1920x1920" | tee -a segment.log
echo "highres_resolution=2560x2560" | tee -a segment.log
echo "ultralytics_version=8.4.118" | tee -a segment.log
sha256sum "$CONTROL_MODEL" | sed 's/^/control_onnx_sha256=/' | tee -a segment.log
stat -c 'control_onnx_bytes=%s' "$CONTROL_MODEL" | tee -a segment.log

EXPORT_DIR="/tmp/oev_yolo2560_export"
EXPORT_VENV="/tmp/oev_yolo2560_venv"
rm -rf "$EXPORT_DIR" "$EXPORT_VENV"
mkdir -p "$EXPORT_DIR"
python3 -m venv "$EXPORT_VENV"
"$EXPORT_VENV/bin/pip" install -q --upgrade pip
"$EXPORT_VENV/bin/pip" install -q "ultralytics==8.4.118" onnxruntime-gpu
export_start=$(date +%s)
(
  cd "$EXPORT_DIR"
  "$EXPORT_VENV/bin/yolo" export model=yolo26m.pt format=onnx imgsz=2560
)
export_end=$(date +%s)
HIGHRES_MODEL="$EXPORT_DIR/yolo26m.onnx"
if [ ! -s "$HIGHRES_MODEL" ]; then
  echo "FATAL: 2560 YOLO26m export missing: $HIGHRES_MODEL" | tee -a segment.log
  exit 1
fi
if [ ! -s "$EXPORT_DIR/yolo26m.pt" ]; then
  echo "FATAL: source yolo26m.pt missing after export" | tee -a segment.log
  exit 1
fi
sha256sum "$EXPORT_DIR/yolo26m.pt" | sed 's/^/source_pt_sha256=/' | tee -a segment.log
sha256sum "$HIGHRES_MODEL" | sed 's/^/highres_onnx_sha256=/' | tee -a segment.log
stat -c 'highres_onnx_bytes=%s' "$HIGHRES_MODEL" | tee -a segment.log
echo "highres_model_export_seconds=$((export_end-export_start))" | tee -a segment.log

CONTROL_MODEL="$CONTROL_MODEL" HIGHRES_MODEL="$HIGHRES_MODEL" "$EXPORT_VENV/bin/python3" - <<'PYSHAPE' | tee -a segment.log
import os
import onnxruntime as ort
for label, path in [('control', os.environ['CONTROL_MODEL']), ('highres', os.environ['HIGHRES_MODEL'])]:
    s = ort.InferenceSession(path, providers=['CPUExecutionProvider'])
    print(f"{label}_onnx_input_shape={s.get_inputs()[0].shape}")
PYSHAPE

cp "$HIGHRES_MODEL" "/tmp/oev_run/yolo26m.onnx"
echo "Using YOLO model: /tmp/oev_run/yolo26m.onnx (same YOLO26m weights, test-only 2560 static inference input)" | tee -a segment.log
'''
if s.count(old) != 1:
    raise SystemExit(f"expected exactly one canonical YOLO26 model block, found {s.count(old)}")
s = s.replace(old, new, 1)

# Add passive GPU/VRAM telemetry around the unchanged Reco stitch command.
old_run = 'stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log\nstitch_rc=${PIPESTATUS[0]}\n'
new_run = '''GPU_TELEMETRY=gpu_telemetry.csv\necho "timestamp_ms,gpu_util_pct,memory_used_mib,memory_total_mib" > "$GPU_TELEMETRY"\n( while true; do\n    ts=$(date +%s%3N)\n    vals=$(nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ') || true\n    [ -n "$vals" ] && echo "$ts,$vals" >> "$GPU_TELEMETRY"\n    sleep 1\n  done ) &\nGPU_MON_PID=$!\nstdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log\nstitch_rc=${PIPESTATUS[0]}\nkill "$GPU_MON_PID" 2>/dev/null || true\nwait "$GPU_MON_PID" 2>/dev/null || true\n'''
if s.count(old_run) != 1:
    raise SystemExit(f"expected exactly one canonical stitch invocation, found {s.count(old_run)}")
s = s.replace(old_run, new_run, 1)

p.write_text(s)
print("Prepared exact-main YOLO26m runner with inference resolution as the sole behavioural delta: 1920 -> 2560")
PY

chmod +x "$BASE_RUNNER"
echo "TEST_ONLY_RUNNER_DELTA=yolo26m_inference_resolution_1920_to_2560 base_main_sha=${BASE_FFA_SHA}"
exec "$BASE_RUNNER"
