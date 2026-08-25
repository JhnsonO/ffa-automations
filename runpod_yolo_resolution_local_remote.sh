#!/usr/bin/env bash
# Controlled YOLO inference-resolution wrapper for experiment/yolo-highres-01.
# Reuses the exact accepted-v4 control wrapper; only redirects its nested
# YOLO model path from the network volume to the locally exported ONNX model.
set -euo pipefail
cd /tmp/oev_run
: "${YOLO_RESOLUTION:?YOLO_RESOLUTION must be set}"

CONTROL_SHA="ed55cc36485b64e0ef3993d29e3b53dfd81fe941"
CONTROL="/tmp/oev_run/runpod_yolo_resolution_accepted_v4_control.sh"
curl -fsSL "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${CONTROL_SHA}/runpod_sample_baseline_yolo26_remote.sh" -o "$CONTROL"
test -s "$CONTROL"

python3 - "$CONTROL" "$YOLO_RESOLUTION" <<'PY_CONTROL'
from pathlib import Path
import sys
p=Path(sys.argv[1]); res=sys.argv[2]; s=p.read_text()
marker='test -s "$V3_RUNNER"\n'
if s.count(marker)!=1:
    raise SystemExit(f'expected one V3 marker, found {s.count(marker)}')
inject=r'''python3 - "$V3_RUNNER" <<'PY_LOCAL_MODEL_HOOK'
from pathlib import Path
p=Path("/tmp/oev_run/runpod_sample_baseline_yolo26_v3_exact.sh")
s=p.read_text()
marker='test -s "$BASE_SCRIPT"\n'
if s.count(marker)!=1:
    raise SystemExit(f"expected one base marker, found {s.count(marker)}")
hook=r'''python3 - "$BASE_SCRIPT" <<'PY_LOCAL_MODEL_PATH'
from pathlib import Path
p=Path("/tmp/oev_run/runpod_sample_baseline_yolo26_base.sh")
s=p.read_text()
old='YOLO_MODEL="/runpod-volume/oev-runtime/models/${YOLO26_VARIANT}.onnx"'
new='YOLO_MODEL="/tmp/oev_run/${YOLO26_VARIANT}.onnx"'
if s.count(old)!=1:
    raise SystemExit(f"expected one network-volume YOLO model path, found {s.count(old)}")
p.write_text(s.replace(old,new,1))
PY_LOCAL_MODEL_PATH
'''
p.write_text(s.replace(marker, marker+hook, 1))
PY_LOCAL_MODEL_HOOK
'''
s=s.replace(marker, marker+inject, 1)
s=s.replace('"experiment": "yolo_resolution_control_1920_v4_stride1"', f'"experiment": "yolo_resolution_{res}_v4_stride1"')
s=s.replace('"definition": "accepted v4 stride1 camera behavior; YOLO26m 1920 control"', f'"definition": "accepted v4 stride1 camera behavior; YOLO26m {res} inference-resolution arm"')
s=s.replace('TEST_ONLY_RUNNER_DELTA=yolo_resolution_control_1920', f'TEST_ONLY_RUNNER_DELTA=yolo_resolution_{res}')
p.write_text(s)
PY_CONTROL

chmod +x "$CONTROL"
echo "YOLO_RESOLUTION_ARM=${YOLO_RESOLUTION} local_onnx=/tmp/oev_run/yolo26m.onnx accepted_v4_control_sha=${CONTROL_SHA}"
exec "$CONTROL"
