#!/usr/bin/env bash
# Diagnostic-only same-pod A/B for option 1: remove the per-frame Vulkan CPU
# completion poll by using a reverse Vulkan->CUDA completion semaphore per
# decode slot. No production volume state is modified.
set -uo pipefail

BASE_SHA="f27cbb6d0d65fcf9a11fb4d82d119ae214695318"
ASYNC_SHA="a5de6a23294eea91e0f52831ae4f3686ec619be1"
REPO="https://github.com/JhnsonO/video-stitcher"
BENCH_DIR="/tmp/oev_run"
MODEL="/runpod-volume/oev-runtime/models/yolo26m.onnx"
RUN_DIR="/tmp/oev_option1"
mkdir -p "$RUN_DIR/frames"
cd "$RUN_DIR" || exit 1
: > summary.log
: > timing.log

say() { echo "$*" | tee -a summary.log; }
now() { python3 - <<'PY'
import time
print(f"{time.monotonic():.6f}")
PY
}
elapsed() { python3 - "$1" "$2" <<'PY'
import sys
print(f"{float(sys.argv[2])-float(sys.argv[1]):.6f}")
PY
}

say "=== OPTION1 ASYNC CUDA/VULKAN SLOT HANDOFF ==="
say "base_sha=$BASE_SHA"
say "async_sha=$ASYNC_SHA"

# Use the exact Drive benchmark pair already proven by the earlier zero-copy
# acceptance workflows. The workflow downloads these files before this script
# runs; fail closed if they are absent rather than guessing a volume filename.
LEFT_SRC="$BENCH_DIR/left.mp4"
RIGHT_SRC="$BENCH_DIR/right.mp4"
for f in "$LEFT_SRC" "$RIGHT_SRC" "$MODEL"; do
  if [ ! -s "$f" ]; then
    say "FATAL missing proven benchmark input: $f"
    exit 2
  fi
done
cp "$LEFT_SRC" left.mp4
cp "$RIGHT_SRC" right.mp4
ffprobe -v error -select_streams v:0 -show_entries stream=codec_name,pix_fmt,width,height,r_frame_rate -of default=nw=1 left.mp4 | tee input_probe.log

apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  git curl build-essential pkg-config cmake clang libclang-dev \
  libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libavfilter-dev \
  ffmpeg ca-certificates >/dev/null
if ! command -v cargo >/dev/null 2>&1; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y >/dev/null
fi
export PATH="/root/.cargo/bin:${PATH}"

CUDA_LIB_DIR=$(find /usr/local -maxdepth 1 -type d -name 'cuda-12.*' 2>/dev/null | sort -V | tail -1)
if [ -z "$CUDA_LIB_DIR" ]; then say "FATAL no CUDA 12 directory"; exit 3; fi
export LD_LIBRARY_PATH="${CUDA_LIB_DIR}/lib64:${LD_LIBRARY_PATH:-}"
EGL_ICD_PATH="/tmp/nvidia_egl_icd.json"
cat > "$EGL_ICD_PATH" <<'JSON'
{
  "file_format_version": "1.0.1",
  "ICD": {"library_path": "libEGL_nvidia.so.0", "api_version": "1.4.312"}
}
JSON
export VK_DRIVER_FILES="$EGL_ICD_PATH"
export VK_ICD_FILENAMES="$EGL_ICD_PATH"
unset DISPLAY
VULKAN_CHECK=$(env -u DISPLAY vulkaninfo 2>&1 | grep -iE 'deviceName|deviceType|driverInfo' | head -6)
echo "$VULKAN_CHECK" | tee gpu_env.log
if ! grep -q 'DISCRETE_GPU' <<< "$VULKAN_CHECK" || ! grep -q 'NVIDIA' <<< "$VULKAN_CHECK"; then
  say "FATAL Vulkan is not the accepted NVIDIA discrete GPU"
  exit 3
fi
GPU_NAME=$(grep -m1 'deviceName' gpu_env.log | sed 's/.*= *//')
say "gpu=$GPU_NAME"

build_reco() {
  local sha="$1" dir="$2" log="$3"
  rm -rf "$dir"
  git clone -q "$REPO" "$dir"
  cd "$dir" || return 1
  git checkout -q "$sha"
  echo "requested_sha=$sha actual_sha=$(git rev-parse HEAD)" | tee "$RUN_DIR/$log"
  if [ "$(git rev-parse HEAD)" != "$sha" ]; then return 1; fi
  cargo build --release --locked -p reco-cli --features cuda >> "$RUN_DIR/$log" 2>&1
  local rc=$?
  cd "$RUN_DIR" || return 1
  return $rc
}

say "building merged blocking-poll base"
build_reco "$BASE_SHA" /tmp/reco_base build_base.log || { say "FATAL base build failed"; exit 4; }
say "building async handoff candidate"
build_reco "$ASYNC_SHA" /tmp/reco_async build_async.log || { say "FATAL async build failed"; exit 5; }
BASE_BIN=/tmp/reco_base/target/release/reco
ASYNC_BIN=/tmp/reco_async/target/release/reco

LENS_PROFILE_URL='https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json'
curl -fsSL "$LENS_PROFILE_URL" -o hero10_wide_16by9.json
RUST_LOG=warn "$BASE_BIN" calibrate left.mp4 right.mp4 \
  --left-profile hero10_wide_16by9.json \
  --right-profile hero10_wide_16by9.json \
  -o match.json > calibrate.log 2>&1
if [ $? -ne 0 ] || [ ! -s match.json ]; then say "FATAL calibration failed"; exit 6; fi

python3 - <<'PY'
import json
p='match.json'
m=json.load(open(p))
m['field_roi']={
 'left': [[0.1227,0.9611],[0.0573,0.6846],[0.1802,0.6285],[0.2645,0.5769],[0.4382,0.4864],[0.4988,0.4658],[0.5942,0.4474],[0.7835,0.4175],[0.9285,0.3785],[1.0,1.0],[0.1227,1.0]],
 'right': [[0.0391,0.4206],[0.0818,0.4101],[0.1839,0.4070],[0.2783,0.4070],[0.3448,0.4083],[0.4100,0.4161],[0.4684,0.4319],[0.6239,0.4801],[0.7368,0.5200],[0.7980,0.5465],[0.7454,0.9011],[0.7454,1.0],[0.0,1.0]]}
json.dump(m,open(p,'w'),indent=2)
PY

COMMON=(stitch left.mp4 right.mp4 -c match.json \
  --model "$MODEL" --tracking field --panner-preset broadcast \
  --lookahead 0.1 --detection-interval 1 --width 1920 --height 1080)
LOG_FILTER='warn,reco_core=info,reco_detect=info,reco_autocam=info'

run_case() {
  local label="$1" bin="$2" frames="$3" out="$4" log="$5" timeout_s="$6"
  local start end rc sec
  start=$(now)
  set +e
  timeout "${timeout_s}s" env RUST_LOG="$LOG_FILTER" stdbuf -oL -eL \
    "$bin" "${COMMON[@]}" --max-frames "$frames" -o "$out" --events "${label}.jsonl" \
    > "$log" 2>&1
  rc=$?
  set -e
  end=$(now); sec=$(elapsed "$start" "$end")
  echo "$label rc=$rc wall_s=$sec" | tee -a timing.log
  if [ "$rc" -ne 0 ]; then
    echo "$label runtime failed rc=$rc" | tee -a summary.log
    tail -80 "$log" >> summary.log
    return "$rc"
  fi
  local count
  count=$(ffprobe -v error -select_streams v:0 -count_frames -show_entries stream=nb_read_frames \
    -of default=nw=1:nk=1 "$out" 2>/dev/null || true)
  echo "$label frames=$count" | tee -a timing.log
  if [ "$count" != "$frames" ]; then
    echo "$label output frame count mismatch expected=$frames actual=${count:-missing}" | tee -a summary.log
    return 91
  fi
  return 0
}

say "warmup base"
run_case warm_base "$BASE_BIN" 30 warm_base.mp4 warm_base.log 90 || exit 10
say "warmup async"
run_case warm_async "$ASYNC_BIN" 30 warm_async.mp4 warm_async.log 90 || exit 11

say "timed A/B sequence: base -> async -> async -> base"
run_case base1 "$BASE_BIN" 180 base1.mp4 base1.log 150 || exit 12
run_case async1 "$ASYNC_BIN" 180 async1.mp4 async1.log 150 || exit 13
run_case async2 "$ASYNC_BIN" 180 async2.mp4 async2.log 150 || exit 14
run_case base2 "$BASE_BIN" 180 base2.mp4 base2.log 150 || exit 15

if grep -Eqi 'cuWaitExternalSemaphoresAsync:|completion semaphore wait sync:|ERROR_INVALID|CUDA_ERROR' async1.log async2.log; then
  say "OPTION1_CORRECT=FAIL semaphore/CUDA error found"
  grep -Ei 'cuWaitExternalSemaphoresAsync:|completion semaphore wait sync:|ERROR_INVALID|CUDA_ERROR' async1.log async2.log >> summary.log || true
  exit 16
fi
if ! grep -q 'GPU zero-copy (CUDA shared buffer/Vulkan)' async1.log || ! grep -q 'GPU zero-copy (CUDA shared buffer/Vulkan)' async2.log; then
  say "OPTION1_CORRECT=FAIL shared-buffer path marker missing"
  exit 17
fi

python3 - <<'PY' | tee ab_metrics.txt
import re
text=open('timing.log').read()
def secs(label):
    m=re.search(rf'^{label} rc=0 wall_s=([0-9.]+)$',text,re.M)
    if not m: raise SystemExit(f'missing timing for {label}')
    return float(m.group(1))
b1,b2=secs('base1'),secs('base2')
a1,a2=secs('async1'),secs('async2')
bavg=(b1+b2)/2; aavg=(a1+a2)/2
bfps=180/bavg; afps=180/aavg
speed=afps/bfps
print(f'base_run1_s={b1:.3f}')
print(f'base_run2_s={b2:.3f}')
print(f'base_avg_s={bavg:.3f}')
print(f'base_avg_fps={bfps:.3f}')
print(f'async_run1_s={a1:.3f}')
print(f'async_run2_s={a2:.3f}')
print(f'async_avg_s={aavg:.3f}')
print(f'async_avg_fps={afps:.3f}')
print(f'speedup_x={speed:.3f}')
print(f'improvement_pct={(speed-1)*100:.1f}')
PY
cat ab_metrics.txt >> summary.log

ffmpeg -loglevel error -y -i base1.mp4 -vf 'select=eq(n\,90)' -vsync 0 -frames:v 1 frames/base_frame90.png
ffmpeg -loglevel error -y -i async1.mp4 -vf 'select=eq(n\,90)' -vsync 0 -frames:v 1 frames/async_frame90.png

say "OPTION1_CORRECT=PASS"
python3 - <<'PY' >> summary.log
m={}
for line in open('ab_metrics.txt'):
    if '=' in line:
        k,v=line.strip().split('=',1); m[k]=float(v)
imp=m['improvement_pct']
if imp >= 2.0:
    print('OPTION1_PERF=PASS')
elif imp > -2.0:
    print('OPTION1_PERF=FLAT')
else:
    print('OPTION1_PERF=REGRESSION')
PY
say "OPTION1_RUNTIME=PASS"
exit 0
