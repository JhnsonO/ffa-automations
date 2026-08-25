#!/usr/bin/env bash
# EXPERIMENT ONLY — SoccerNet-v3D pure detector A/B.
# No Reco tracker, world state, panner, containment, renderer or encoder runs here.
# Both detectors receive the same decoded source frame and same 1920x1920 tensor.
set -euo pipefail
cd /tmp/oev_phase1

OUT=/tmp/oev_phase1/out
CONTROL_MODEL=/runpod-volume/oev-runtime/models/yolo26m.onnx
SOCCERNET_PT=/tmp/oev_phase1/yolo-sn-ball-opt.pt
SOCCERNET_URL='https://github.com/mguti97/SoccerNet-v3D/releases/download/v1.0.0/yolo-sn-ball-opt.pt'
SOCCERNET_EXPECTED_SIZE=51266130
SOCCERNET_EXPECTED_SHA='a3082fb435a8501ae17cfe4ac78e66ca7041205e115feda344d34d1693064f36'
CONTROL_EXPECTED_SHA='f056f75e155987f3a3709feaaa519d1cee6052711cb10d9160f98413d3f3b5ca'
mkdir -p "$OUT"
exec > >(tee "$OUT/phase1.log") 2>&1

echo 'PHASE1_DETECTOR_ONLY_START'
echo 'experiment_branch=experiment/soccernet-ball-detector-01'
echo 'branch_start_main=8819b3d79e247120445ab35a3fe493c3585cd317'
echo 'sample=GX010197-seed1384188843/sample_02/180s'
echo 'resolution=1920 stride=1 detection_cadence=every_frame threshold=0.10'

for f in left.mp4 right.mp4 "$CONTROL_MODEL" oev_soccernet_phase1.py; do
  test -s "$f" || { echo "FATAL missing $f"; exit 10; }
done

control_sha=$(sha256sum "$CONTROL_MODEL" | awk '{print $1}')
[ "$control_sha" = "$CONTROL_EXPECTED_SHA" ] || { echo "FATAL control SHA drift: $control_sha"; exit 11; }
printf '%s  %s\n' "$control_sha" "$CONTROL_MODEL" > "$OUT/control_yolo26m.sha256"

ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames \
  -show_entries format=duration -of json left.mp4 > "$OUT/left_source_probe.json"
ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames \
  -show_entries format=duration -of json right.mp4 > "$OUT/right_source_probe.json"

curl -fL --retry 3 --retry-delay 2 "$SOCCERNET_URL" -o "$SOCCERNET_PT"
actual_size=$(stat -c %s "$SOCCERNET_PT")
[ "$actual_size" = "$SOCCERNET_EXPECTED_SIZE" ] || { echo "FATAL SoccerNet size drift: $actual_size"; exit 12; }
sn_sha=$(sha256sum "$SOCCERNET_PT" | awk '{print $1}')
[ "$sn_sha" = "$SOCCERNET_EXPECTED_SHA" ] || { echo "FATAL SoccerNet SHA drift: $sn_sha"; exit 13; }
printf '%s  %s\n' "$sn_sha" "$SOCCERNET_PT" > "$OUT/soccernet_pt.sha256"

ULTRA_VERSION=$(python3 - <<'PY'
import json
p='/runpod-volume/oev-runtime/manifest.json'
print(json.load(open(p))['ultralytics_version'])
PY
)
[ "$ULTRA_VERSION" = '8.4.118' ] || { echo "FATAL Ultralytics runtime drift: $ULTRA_VERSION"; exit 14; }
cp /runpod-volume/oev-runtime/manifest.json "$OUT/control_runtime_manifest.json"
cp /runpod-volume/oev-runtime/models/models.sha256 "$OUT/control_models.sha256"

echo "ultralytics_version=$ULTRA_VERSION"
python3 -m venv --system-site-packages /tmp/soccernet-phase1-venv
/tmp/soccernet-phase1-venv/bin/pip install -q --upgrade pip
/tmp/soccernet-phase1-venv/bin/pip install -q --no-deps "ultralytics==$ULTRA_VERSION"
/tmp/soccernet-phase1-venv/bin/pip install -q --no-deps "onnxruntime-gpu==1.26.0" "onnx==1.19.1"
/tmp/soccernet-phase1-venv/bin/pip install -q --no-deps \
  "opencv-python-headless==4.11.0.86" ultralytics-thop polars py-cpuinfo \
  matplotlib pillow pyyaml requests scipy psutil pandas seaborn nvidia-ml-py \
  cycler contourpy fonttools kiwisolver packaging pyparsing python-dateutil six

# Hard gate: detector-only runner needs CUDA inference, but deliberately does
# NOT require NVDEC/Vulkan because Phase 1 decodes via OpenCV and tests detectors only.
/tmp/soccernet-phase1-venv/bin/python3 - <<'PY'
import torch, onnxruntime as ort
from ultralytics import YOLO
print('gpu=', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'NONE')
print('torch=', torch.__version__, 'cuda=', torch.version.cuda, 'available=', torch.cuda.is_available())
print('ort=', ort.__version__, 'providers=', ort.get_available_providers())
assert torch.cuda.is_available()
assert str(torch.version.cuda).startswith('12.')
assert ort.__version__ == '1.26.0'
assert 'CUDAExecutionProvider' in ort.get_available_providers()
print('ultralytics_import=OK', YOLO)
PY

nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free --format=csv,noheader | tee "$OUT/gpu_before.txt"
start_epoch=$(date +%s)
start_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "phase1_start=$start_iso"

/tmp/soccernet-phase1-venv/bin/python3 -u /tmp/oev_phase1/oev_soccernet_phase1.py \
  --left /tmp/oev_phase1/left.mp4 \
  --right /tmp/oev_phase1/right.mp4 \
  --control-model "$CONTROL_MODEL" \
  --soccernet-pt "$SOCCERNET_PT" \
  --out-dir "$OUT"

end_epoch=$(date +%s)
end_iso=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "phase1_end=$end_iso"
echo "phase1_wall_seconds=$((end_epoch-start_epoch))" | tee "$OUT/phase1_wall_seconds.txt"
nvidia-smi --query-gpu=name,driver_version,memory.total,memory.free,memory.used --format=csv,noheader | tee "$OUT/gpu_after.txt"
test -s "$OUT/phase1_summary.json"
test -s "$OUT/phase1_frame_comparison.csv"
test -s "$OUT/phase1_camera_comparison.csv"
echo 'PHASE1_DETECTOR_ONLY_COMPLETE'