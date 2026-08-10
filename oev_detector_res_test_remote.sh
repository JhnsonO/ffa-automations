#!/usr/bin/env bash
# Runs ON the Vast.ai instance (uploaded + executed via SSH by
# oev-detector-res-test.yml). Isolated from both the M1 cylindrical
# pipeline (oev_reco_stitch_remote.sh) and the full follow-cam test
# (oev_followcam_test_remote.sh) -- this is a narrow detector-only A/B
# diagnostic: does YOLO input resolution (640 baseline already have,
# now 1280 and 1920) change ball-detection rate on the same 20s window?
#
# Only --model changes between the two stitch calls in this script.
# Everything else (calibration, footage, window, tracking=field,
# panner-preset=broadcast, lookahead=1.5, detection-interval=1,
# --no-zero-copy, and Reco's own hardcoded field-mode conf_thresh=0.1)
# is held fixed, matching run 31427290826 (the 640 baseline) exactly,
# per Johnson's explicit correction: field mode does NOT force
# conf_thresh=0.25 (that's ball-mode-only); the real value, confirmed
# in run 31427290826's own stitch.log, is conf_thresh=0.1.
#
# Expects, in /tmp/oev_run/:
#   left.mp4, right.mp4   (the full clip pair, already downloaded)
#
# Produces, in /tmp/oev_run/:
#   env.log                  - toolchain/driver/YOLO-export versions
#   build.log                - cargo build output
#   calibrate.log             - reco calibrate output (once, shared)
#   stitch_1280.log           - reco stitch output, 1280 model
#   stitch_1920.log           - reco stitch output, 1920 model
#   timing_summary.txt        - measured fps/runtime per resolution + confirmed loaded input size
#   match.json                - calibration result
#   yolov8n_1280.onnx, yolov8n_1920.onnx
#   events_1280.jsonl, events_1920.jsonl
#   followcam_1280.mp4, followcam_1920.mp4   (small, diagnostic only, not for review)
#   left_window.mp4, right_window.mp4        (stream-copy trim of the same 7-27s
#                                              window, for local still-frame /
#                                              visual-overlay diagnostics afterward)
#
# Exit codes: 1=env/build failure, 2=calibrate failure,
# 3=1280 export failure, 4=1920 export failure,
# 5=1280 stitch failure, 6=1920 stitch failure.

set -uo pipefail
cd /tmp/oev_run

# Fixed test window, chosen from the 640 baseline's own events.jsonl as
# the richest 20s stretch of real ball detections in the clip (86 of 90
# total ball-hit frames across the whole 45s clip fall in this window).
START_SEC="${START_SEC:-7}"
END_SEC="${END_SEC:-27}"

# --- GitHub Release blob cache (shared with the other two scripts;
# same repo/tag, same reco-cli binary cache key by fork SHA) ---
GH_TOKEN="${GH_TOKEN:-}"
GH_CACHE_REPO="JhnsonO/ffa-automations"
GH_CACHE_TAG="oev-build-cache"

gh_cache_release_id() {
  [ -z "$GH_TOKEN" ] && return 1
  curl -s -H "Authorization: token $GH_TOKEN" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases/tags/$GH_CACHE_TAG" \
    | jq -r 'if .id then .id else empty end'
}

gh_cache_download() {
  local release_id="$1" name="$2" outpath="$3" asset_id
  asset_id=$(curl -s -H "Authorization: token $GH_TOKEN" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases/$release_id/assets" \
    | jq -r --arg n "$name" '.[] | select(.name==$n) | .id')
  [ -z "$asset_id" ] && return 1
  curl -sL -H "Authorization: token $GH_TOKEN" -H "Accept: application/octet-stream" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases/assets/$asset_id" -o "$outpath"
  [ -s "$outpath" ]
}

echo "=== env.log: toolchain + GPU info ===" | tee env.log
{
  echo "--- nvidia-smi ---"
  nvidia-smi || echo "nvidia-smi not available"
} 2>&1 | tee -a env.log

echo "=== Installing system deps ===" | tee -a env.log
stdbuf -oL -eL apt-get update 2>&1 | tee -a env.log
stdbuf -oL -eL apt-get install -y --no-install-recommends \
  git build-essential pkg-config libssl-dev cmake clang ca-certificates wget jq \
  mesa-vulkan-drivers vulkan-tools libvulkan1 ffmpeg \
  libavutil-dev libavcodec-dev libavformat-dev libswscale-dev \
  libavdevice-dev libavfilter-dev libswresample-dev \
  python3 python3-pip python3-venv 2>&1 | tee -a env.log

# --- NVIDIA userspace Vulkan library extraction (EGL ICD fix, verbatim
# logic from the other two scripts) -- Vulkan render (wgpu) still needs
# the host's real GPU even though decode/detection are CPU this run. ---
{
  echo "--- NVIDIA driver library discovery ---"
  NVIDIA_LIB=$(find / -xdev -name 'libGLX_nvidia.so.[0-9]*.[0-9]*' 2>/dev/null | head -1)
  if [ -n "$NVIDIA_LIB" ]; then
    echo "NVIDIA_LIB=$NVIDIA_LIB"
    DRIVER_VER=$(basename "$NVIDIA_LIB" | sed -E 's/^libGLX_nvidia\.so\.//')
    DRIVER_SERIES=$(echo "$DRIVER_VER" | cut -d. -f1)
    if [ -n "$DRIVER_SERIES" ]; then
      NVIDIA_PKG=$(apt-cache search "^libnvidia-gl-${DRIVER_SERIES}" 2>/dev/null | sort -V | tail -1 | awk '{print $1}')
      if [ -n "$NVIDIA_PKG" ]; then
        mkdir -p /tmp/nvidia-extract
        apt-get download "$NVIDIA_PKG" -o Dir::Cache::Archives=/tmp/nvidia-extract 2>&1
        DEB_FILE=$(find /tmp/nvidia-extract -name '*.deb' | head -1)
        if [ -n "$DEB_FILE" ]; then
          dpkg-deb -x "$DEB_FILE" /
          ldconfig
        fi
      fi
    fi
  else
    echo "No libGLX_nvidia.so.<version> file found — cannot diagnose further"
  fi
} 2>&1 | tee -a env.log

echo "=== Installing Rust toolchain ===" | tee -a env.log
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | stdbuf -oL -eL sh -s -- -y --default-toolchain stable 2>&1 | tee -a env.log
source "$HOME/.cargo/env"

echo "=== Installing ultralytics + exporting YOLOv8n ONNX at 1280 and 1920 (NMS baked in) ===" | tee -a env.log
# 640 already covered by run 31427290826 -- not re-exported, not re-run.
python3 -m venv /tmp/yolo-venv 2>&1 | tee -a env.log
source /tmp/yolo-venv/bin/activate
pip install -q ultralytics 2>&1 | tee -a env.log
for RES in 1280 1920; do
  echo "--- exporting imgsz=$RES ---" | tee -a env.log
  rm -f yolov8n.onnx yolov8n.pt
  yolo export model=yolov8n.pt format=onnx imgsz=$RES nms=True 2>&1 | tee -a env.log
  export_rc=${PIPESTATUS[0]}
  if [ "$export_rc" -ne 0 ] || [ ! -s /tmp/oev_run/yolov8n.onnx ]; then
    echo "FATAL: ultralytics export failed at imgsz=$RES (exit $export_rc)" | tee -a env.log
    deactivate
    exit $([ "$RES" = "1280" ] && echo 3 || echo 4)
  fi
  mv yolov8n.onnx "yolov8n_${RES}.onnx"
  echo "Model ready: yolov8n_${RES}.onnx ($(du -h yolov8n_${RES}.onnx | cut -f1))" | tee -a env.log
done
deactivate

echo "=== build.log: cloning + building reco-cli ===" | tee build.log
export CARGO_NET_RETRY=2
export CARGO_HTTP_TIMEOUT=15
git clone --depth 1 https://github.com/JhnsonO/video-stitcher.git /tmp/reco-src 2>&1 | tee -a build.log
clone_rc=${PIPESTATUS[0]}
if [ "$clone_rc" -ne 0 ]; then
  echo "FATAL: git clone failed (exit $clone_rc), see build.log" | tee -a build.log
  exit 1
fi
cd /tmp/reco-src
RECO_SHA=$(git rev-parse HEAD)
echo "JhnsonO/video-stitcher HEAD: $RECO_SHA" | tee -a build.log
RECO_BIN="/tmp/reco-src/target/release/reco"
BIN_CACHE_ASSET="reco-cli-${RECO_SHA}.tar.gz"
bin_cache_hit=0
if [ -n "$GH_TOKEN" ]; then
  BIN_RELEASE_ID=$(gh_cache_release_id)
  if [ -n "$BIN_RELEASE_ID" ] && gh_cache_download "$BIN_RELEASE_ID" "$BIN_CACHE_ASSET" /tmp/reco-cli-cache.tar.gz; then
    echo "reco-cli binary cache HIT ($BIN_CACHE_ASSET) — skipping cargo build" | tee -a build.log
    mkdir -p "$(dirname "$RECO_BIN")"
    tar -xzf /tmp/reco-cli-cache.tar.gz -C "$(dirname "$RECO_BIN")"
    chmod +x "$RECO_BIN"
    bin_cache_hit=1
  fi
fi
if [ "$bin_cache_hit" -eq 0 ]; then
  echo "reco-cli binary cache MISS ($BIN_CACHE_ASSET, or no GH_TOKEN) — building from source" | tee -a build.log
  timeout 1200 stdbuf -oL -eL cargo build --release -p reco-cli -v 2>&1 | tee -a /tmp/oev_run/build.log
  build_rc=${PIPESTATUS[0]}
  if [ "$build_rc" -ne 0 ]; then
    echo "FATAL: cargo build failed or timed out (exit $build_rc), see build.log" | tee -a /tmp/oev_run/build.log
    exit 1
  fi
  if [ ! -x "$RECO_BIN" ]; then
    echo "FATAL: build reported success but binary not found at $RECO_BIN" | tee -a /tmp/oev_run/build.log
    exit 1
  fi
fi
echo "Build OK: $RECO_BIN" | tee -a /tmp/oev_run/build.log
cd /tmp/oev_run

echo "=== calibrate.log: reco calibrate (once, shared across both resolution runs) ===" | tee calibrate.log
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

echo "=== Trimming raw camera pair to the test window (stream-copy, for local visual diagnostics) ===" | tee -a env.log
ffmpeg -y -ss "$START_SEC" -t $((END_SEC - START_SEC)) -i left.mp4 -c copy left_window.mp4 2>&1 | tee -a env.log
ffmpeg -y -ss "$START_SEC" -t $((END_SEC - START_SEC)) -i right.mp4 -c copy right_window.mp4 2>&1 | tee -a env.log

echo "" > timing_summary.txt
echo "=== Detector A/B test: same window (${START_SEC}s-${END_SEC}s), same everything except --model ===" | tee -a timing_summary.txt

run_stitch_test() {
  local RES="$1" EXIT_ON_FAIL="$2"
  echo "=== stitch_${RES}.log: reco stitch, YOLOv8n @ ${RES} ===" | tee "stitch_${RES}.log"
  STITCH_ARGS=(stitch left.mp4 right.mp4 -c match.json -o "followcam_${RES}.mp4"
    --model "yolov8n_${RES}.onnx"
    --tracking field
    --panner-preset broadcast
    --lookahead 1.5
    --detection-interval 1
    --events "events_${RES}.jsonl"
    --no-zero-copy
    --start-time "$START_SEC" --end-time "$END_SEC"
    --width 640 --height 360)
  echo "reco stitch args: ${STITCH_ARGS[*]}" | tee -a "stitch_${RES}.log"
  stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a "stitch_${RES}.log"
  local stitch_rc=${PIPESTATUS[0]}
  if [ "$stitch_rc" -ne 0 ]; then
    echo "FATAL: reco stitch failed at ${RES} (exit $stitch_rc), see stitch_${RES}.log" | tee -a "stitch_${RES}.log"
    exit "$EXIT_ON_FAIL"
  fi
  {
    echo "--- ${RES} ---"
    grep "CpuYoloDetector loaded" "stitch_${RES}.log" || echo "WARNING: no 'CpuYoloDetector loaded' line found -- input size NOT confirmed"
    grep "^Done: " "stitch_${RES}.log" || echo "WARNING: no final 'Done:' summary line found"
    echo ""
  } | tee -a timing_summary.txt
}

run_stitch_test 1280 5
run_stitch_test 1920 6

echo "=== timing_summary.txt (final) ===" 
cat timing_summary.txt

echo "=== All stages completed ==="
