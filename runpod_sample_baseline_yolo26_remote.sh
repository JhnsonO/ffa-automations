#!/usr/bin/env bash
# HIGH-RES ARM for experiment/yolo-highres-01.
# Single experiment variable vs the accepted control: YOLO26m static ONNX input
# resolution 1920 -> 2560. Same official yolo26m.pt weights, Ultralytics 8.4.118,
# accepted-v4 stride-1 tracker/panner/camera path, footage, thresholds and render.
set -euo pipefail
cd /tmp/oev_run

CONTROL_SHA="d19bd859dde3c57fa05ec4032b8e9b025d02e8ba"
CONTROL="/tmp/oev_run/runpod_yolo_resolution_control_1920.sh"
ULTRALYTICS_VERSION="8.4.118"
YOLO_RESOLUTION="2560"
MODEL_DIR="/tmp/oev_run/highres_model"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${CONTROL_SHA}/runpod_sample_baseline_yolo26_remote.sh" \
  -o "$CONTROL"
test -s "$CONTROL"

# The network-volume 1920 control was produced by oev_populate_volume_remote.sh
# with exactly this Ultralytics version and `model=yolo26m.pt`. Re-export the
# same official weights at 2560 into an isolated local directory. Export time is
# deliberately outside the measured OEV stitch processing window.
mkdir -p "$MODEL_DIR"
python3 -m venv /tmp/yolo-highres-venv
/tmp/yolo-highres-venv/bin/pip install -q --upgrade pip
/tmp/yolo-highres-venv/bin/pip install -q "ultralytics==${ULTRALYTICS_VERSION}" onnxruntime-gpu
(
  cd "$MODEL_DIR"
  /tmp/yolo-highres-venv/bin/yolo export model=yolo26m.pt format=onnx imgsz="$YOLO_RESOLUTION" \
    2>&1 | tee /tmp/oev_run/model_export.log
)
test -s "$MODEL_DIR/yolo26m.pt"
test -s "$MODEL_DIR/yolo26m.onnx"

/tmp/yolo-highres-venv/bin/python3 - "$MODEL_DIR/yolo26m.onnx" "$YOLO_RESOLUTION" <<'PY_SHAPE' | tee model_shape.log
import sys
import onnxruntime as ort
model, res = sys.argv[1], int(sys.argv[2])
sess = ort.InferenceSession(model, providers=['CPUExecutionProvider'])
shape = sess.get_inputs()[0].shape
print('onnx_input_shape=' + 'x'.join(str(x) for x in shape))
expected = [1, 3, res, res]
if shape != expected:
    raise SystemExit(f'FATAL: expected static ONNX input {expected}, got {shape}')
PY_SHAPE

{
  echo "experiment=yolo_resolution_highres_2560_v4_stride1"
  echo "resolution=${YOLO_RESOLUTION}"
  echo "ultralytics_version=${ULTRALYTICS_VERSION}"
  echo "weights_source=official_yolo26m.pt"
  sha256sum "$MODEL_DIR/yolo26m.pt" "$MODEL_DIR/yolo26m.onnx"
  cat model_shape.log
} | tee model_identity.txt

# Redirect only the nested accepted control's model source from the staged
# 1920 ONNX to this locally exported 2560 ONNX. All behavior below the model
# path remains the exact pinned accepted-v4 control.
python3 - "$CONTROL" <<'PY_CONTROL'
from pathlib import Path
p = Path('/tmp/oev_run/runpod_yolo_resolution_control_1920.sh')
s = p.read_text()
marker = 'test -s "$V3_RUNNER"\n'
if s.count(marker) != 1:
    raise SystemExit(f'expected one V3 marker, found {s.count(marker)}')
inject = r"""python3 - "$V3_RUNNER" <<'PY_HIGHRES_V3'
from pathlib import Path
p = Path("/tmp/oev_run/runpod_sample_baseline_yolo26_v3_exact.sh")
s = p.read_text()
marker = 'test -s "$BASE_SCRIPT"\n'
if s.count(marker) != 1:
    raise SystemExit(f"expected one base marker, found {s.count(marker)}")
hook = r'''python3 - "$BASE_SCRIPT" <<'PY_HIGHRES_MODEL_PATH'
from pathlib import Path
p = Path("/tmp/oev_run/runpod_sample_baseline_yolo26_base.sh")
s = p.read_text()
old = 'YOLO_MODEL="/runpod-volume/oev-runtime/models/${YOLO26_VARIANT}.onnx"'
new = 'YOLO_MODEL="/tmp/oev_run/highres_model/${YOLO26_VARIANT}.onnx"'
if s.count(old) != 1:
    raise SystemExit(f"expected one staged YOLO model path, found {s.count(old)}")
p.write_text(s.replace(old, new, 1))
PY_HIGHRES_MODEL_PATH
'''
p.write_text(s.replace(marker, marker + hook, 1))
PY_HIGHRES_V3
"""
s = s.replace(marker, marker + inject, 1)
repls = {
    '"experiment": "yolo_resolution_control_1920_v4_stride1"':
        '"experiment": "yolo_resolution_highres_2560_v4_stride1"',
    '"definition": "accepted v4 stride1 camera behavior; YOLO26m 1920 control"':
        '"definition": "accepted v4 stride1 camera behavior; YOLO26m 2560 inference-resolution arm"',
    'TEST_ONLY_RUNNER_DELTA=yolo_resolution_control_1920':
        'TEST_ONLY_RUNNER_DELTA=yolo_resolution_highres_2560',
}
for old, new in repls.items():
    if old not in s:
        raise SystemExit(f'control label marker missing: {old}')
    s = s.replace(old, new)
p.write_text(s)
PY_CONTROL

chmod +x "$CONTROL"
echo "YOLO_RESOLUTION_ARM=2560 accepted_control_sha=${CONTROL_SHA} ultralytics=${ULTRALYTICS_VERSION} model=$MODEL_DIR/yolo26m.onnx"
set +e
"$CONTROL"
base_rc=$?
set -e

# Preserve explicit model identity inside an artifact the canonical workflow
# already pulls, while also leaving standalone files for the expanded pull list.
if [ -s acceptance.log ]; then
  {
    echo "--- yolo highres model identity ---"
    cat model_identity.txt
  } >> acceptance.log
fi
exit "$base_rc"
