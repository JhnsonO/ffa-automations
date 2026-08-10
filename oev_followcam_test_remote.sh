#!/usr/bin/env bash
# Runs ON the Vast.ai instance (uploaded + executed via SSH by
# oev-followcam-test.yml). Isolated from oev_reco_stitch_remote.sh
# (the M1 cylindrical-panorama pipeline, frozen) -- this is a separate
# experiment: does Reco's existing field follow-cam (person+ball
# detection -> FieldPanner, broadcast preset) produce a real AI-driven
# camera on our real HERO10 pair, using the normal l-shape/perspective
# stitch path (NOT cylindrical-stereo).
#
# Deliberately uses --no-zero-copy (CPU decode + CPU ORT detection) to
# remove the CUDA/TensorRT GPU-detector question entirely for this
# first test -- Vulkan GPU is still required for the stitch render
# itself (wgpu), just not for decode/detection this run.
#
# Deliberately does NOT pass --allow-no-tracking: if Reco can't
# initialize tracking, the run must fail loudly, not silently produce
# a plain static stitch that looks like a follow-cam but isn't one.
#
# Expects, in /tmp/oev_run/:
#   left.mp4, right.mp4   (the clip pair, already downloaded)
#
# Produces, in /tmp/oev_run/:
#   env.log         - toolchain/driver/YOLO-export versions
#   build.log       - cargo build output
#   calibrate.log   - reco calibrate output
#   stitch.log      - reco stitch output
#   acceptance.log  - AI-driven-camera acceptance check output
#   match.json      - calibration result (present even if stitch fails)
#   yolov8n.onnx    - exported detection model (present even if stitch fails)
#   events.jsonl    - pipeline event trace (present if stitch ran)
#   followcam.mp4   - follow-cam output (only if stitch succeeds)
#
# Exit codes: 1=env/build/model-export failure, 2=calibrate failure,
# 3=stitch command failure, 4=stitch reported success but output missing,
# 5=stitch succeeded but the AI-driven-camera acceptance check failed.

set -uo pipefail
cd /tmp/oev_run

# --- GitHub Release blob cache (no-op throughout if GH_TOKEN is unset) ---
# Same cache as oev_reco_stitch_remote.sh, same tag/repo -- the compiled
# reco-cli binary is identical regardless of which script invokes it
# (keyed by fork git SHA), so this run benefits from a cache the M1
# pipeline already warmed, and vice versa. CUDA-runtime .deb cache is
# also shared for the same reason, even though this run's stitch path
# doesn't need CUDA (--no-zero-copy) -- harmless to leave wired.
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
  echo "--- rustc/cargo (pre-install check) ---"
  rustc --version 2>&1 || echo "rustc not yet installed"
  cargo --version 2>&1 || echo "cargo not yet installed"
} 2>&1 | tee -a env.log

echo "=== Installing system deps ===" | tee -a env.log
stdbuf -oL -eL apt-get update 2>&1 | tee -a env.log
stdbuf -oL -eL apt-get install -y --no-install-recommends \
  git build-essential pkg-config libssl-dev cmake clang ca-certificates wget jq \
  mesa-vulkan-drivers vulkan-tools libvulkan1 ffmpeg \
  libavutil-dev libavcodec-dev libavformat-dev libswscale-dev \
  libavdevice-dev libavfilter-dev libswresample-dev \
  python3 python3-pip python3-venv 2>&1 | tee -a env.log

# --- NVIDIA userspace Vulkan library extraction (EGL ICD fix) ---
# Verbatim logic from oev_reco_stitch_remote.sh (fix a430de7/680d5be):
# Vulkan render (wgpu) still needs the host's real GPU even though
# decode/detection are forced to CPU this run. Glob matches real driver
# filenames like libGLX_nvidia.so.570.144 (2 dot-segments), not just
# 3-segment forms.
{
  echo "--- NVIDIA driver library discovery ---"
  NVIDIA_LIB=$(find / -xdev -name 'libGLX_nvidia.so.[0-9]*.[0-9]*' 2>/dev/null | head -1)
  if [ -n "$NVIDIA_LIB" ]; then
    echo "NVIDIA_LIB=$NVIDIA_LIB"
    ldd "$NVIDIA_LIB" 2>&1 || echo "ldd failed on $NVIDIA_LIB"
    DRIVER_VER=$(basename "$NVIDIA_LIB" | sed -E 's/^libGLX_nvidia\.so\.//')
    DRIVER_SERIES=$(echo "$DRIVER_VER" | cut -d. -f1)
    if [ -n "$DRIVER_SERIES" ]; then
      echo "Detected driver series: $DRIVER_SERIES (full: $DRIVER_VER)"
      NVIDIA_PKG=$(apt-cache search "^libnvidia-gl-${DRIVER_SERIES}" 2>/dev/null | sort -V | tail -1 | awk '{print $1}')
      if [ -n "$NVIDIA_PKG" ]; then
        echo "Extracting $NVIDIA_PKG (dpkg-deb -x, sidesteps dpkg atomic-replace cross-device-link failures)"
        mkdir -p /tmp/nvidia-extract
        apt-get download "$NVIDIA_PKG" -o Dir::Cache::Archives=/tmp/nvidia-extract 2>&1
        DEB_FILE=$(find /tmp/nvidia-extract -name '*.deb' | head -1)
        if [ -n "$DEB_FILE" ]; then
          dpkg-deb -x "$DEB_FILE" /
          ldconfig
        fi
      else
        echo "No libnvidia-gl-${DRIVER_SERIES} package found in repo"
      fi
    fi
  else
    echo "No libGLX_nvidia.so.<version> file found — cannot diagnose further"
  fi
} 2>&1 | tee -a env.log

echo "=== Installing Rust toolchain ===" | tee -a env.log
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | stdbuf -oL -eL sh -s -- -y --default-toolchain stable 2>&1 | tee -a env.log
source "$HOME/.cargo/env"
{
  echo "--- rustc/cargo (post-install) ---"
  rustc --version
  cargo --version
  echo "--- vulkaninfo summary ---"
  vulkaninfo --summary 2>&1 | head -30 || echo "vulkaninfo failed"
} 2>&1 | tee -a env.log

echo "=== Installing ultralytics + exporting YOLOv8n ONNX (NMS baked in) ===" | tee -a env.log
# Proof-run only per Johnson's explicit direction: unpinned `pip install
# ultralytics` is acceptable here, NOT for anything production/recurring.
# reco-detect's ORT CPU backend requires an ONNX model with built-in NMS
# (see reco-detect README); `nms=True` on export is what ultralytics
# calls this. Standard COCO classes cover both person (0) and sports
# ball (32), which is everything --tracking field needs.
python3 -m venv /tmp/yolo-venv 2>&1 | tee -a env.log
source /tmp/yolo-venv/bin/activate
pip install -q ultralytics 2>&1 | tee -a env.log
yolo export model=yolov8n.pt format=onnx nms=True 2>&1 | tee -a env.log
export_rc=${PIPESTATUS[0]}
deactivate
if [ "$export_rc" -ne 0 ] || [ ! -s /tmp/oev_run/yolov8n.onnx ]; then
  echo "FATAL: ultralytics export failed or yolov8n.onnx missing (exit $export_rc)" | tee -a env.log
  exit 1
fi
echo "YOLO model ready: /tmp/oev_run/yolov8n.onnx ($(du -h /tmp/oev_run/yolov8n.onnx | cut -f1))" | tee -a env.log

echo "=== build.log: cloning + building reco-cli ===" | tee build.log
# Same fork, same default features (autocam + ort already on by default --
# no --features flags needed for follow-cam, confirmed by reading
# reco-cli/Cargo.toml and reco-autocam/Cargo.toml before this ticket).
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
  if [ "$build_rc" -eq 124 ]; then
    echo "FATAL: cargo build timed out after 20min (likely slow-network host), see build.log" | tee -a /tmp/oev_run/build.log
    exit 1
  elif [ "$build_rc" -ne 0 ]; then
    echo "FATAL: cargo build failed (exit $build_rc), see build.log" | tee -a /tmp/oev_run/build.log
    exit 1
  fi
  if [ ! -x "$RECO_BIN" ]; then
    echo "FATAL: build reported success but binary not found at $RECO_BIN" | tee -a /tmp/oev_run/build.log
    exit 1
  fi
  # Binary cache upload intentionally NOT duplicated here -- the M1
  # pipeline's script already populates this cache key on its own runs,
  # and this script only needs to consume it, not race an upload against
  # a concurrent M1 run.
fi
echo "Build OK: $RECO_BIN" | tee -a /tmp/oev_run/build.log
cd /tmp/oev_run

echo "=== calibrate.log: reco calibrate ===" | tee calibrate.log
# Same pinned Hero10 Wide profile as the M1 pipeline (both cameras are
# GoPro Hero 10, Wide mode; auto-detect is known to fail on this
# footage's telemetry -- see docs/ai-project-state.md).
LENS_PROFILE_URL="https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json"
echo "Downloading lens profile: $LENS_PROFILE_URL" | tee -a calibrate.log
curl -fsSL "$LENS_PROFILE_URL" -o hero10_wide_16by9.json
if [ ! -s hero10_wide_16by9.json ]; then
  echo "FATAL: failed to download lens profile from $LENS_PROFILE_URL" | tee -a calibrate.log
  exit 2
fi
RECO_BIN="/tmp/reco-src/target/release/reco"
stdbuf -oL -eL "$RECO_BIN" calibrate left.mp4 right.mp4 \
  --left-profile hero10_wide_16by9.json \
  --right-profile hero10_wide_16by9.json \
  -o match.json 2>&1 | tee -a calibrate.log
calibrate_rc=${PIPESTATUS[0]}
if [ "$calibrate_rc" -ne 0 ]; then
  echo "FATAL: reco calibrate failed (exit $calibrate_rc), see calibrate.log" | tee -a calibrate.log
  exit 2
fi
if [ ! -f match.json ]; then
  echo "FATAL: calibrate reported success but match.json missing" | tee -a calibrate.log
  exit 2
fi
echo "Calibrate OK: match.json written" | tee -a calibrate.log

echo "=== stitch.log: reco stitch (field follow-cam, l-shape, no-zero-copy) ===" | tee stitch.log
# Exact flag set agreed with Johnson: normal perspective (l-shape,
# default) projection, NOT cylindrical -- follow-cam uses Reco's
# standard virtual-camera path, panorama distortion is irrelevant here.
# --no-zero-copy forces CPU decode + CPU ORT detection, removing the
# CUDA/TensorRT GPU-detector question for this first proof run.
# Deliberately NO --allow-no-tracking: a tracking-init failure must
# fail this run loudly, not silently degrade to a static stitch.
STITCH_ARGS=(stitch left.mp4 right.mp4 -c match.json -o followcam.mp4
  --model yolov8n.onnx
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
if [ "$stitch_rc" -ne 0 ]; then
  echo "FATAL: reco stitch failed (exit $stitch_rc), see stitch.log (match.json/yolov8n.onnx are still valid)" | tee -a stitch.log
  exit 3
fi
if [ ! -f followcam.mp4 ]; then
  echo "FATAL: stitch reported success but followcam.mp4 missing" | tee -a stitch.log
  exit 4
fi
echo "Stitch OK: followcam.mp4 written" | tee -a stitch.log

echo "=== acceptance.log: verifying AI-driven follow-cam (not a static stitch) ===" | tee acceptance.log
python3 - <<'PY' 2>&1 | tee -a acceptance.log
import json, sys

accept_fail = False

try:
    stitch_log = open('stitch.log').read()
except FileNotFoundError:
    print("FAIL: stitch.log missing")
    sys.exit(1)

if "Autocam: tracking enabled" not in stitch_log:
    print("FAIL: 'Autocam: tracking enabled' not found in stitch.log -- tracking did not initialize")
    accept_fail = True
else:
    print("OK: 'Autocam: tracking enabled' found in stitch.log")

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
    print("FAIL: no detections_raw event contained any detection -- detector produced nothing")
    accept_fail = True

if len(pan_yaws) < 2:
    print("FAIL: fewer than 2 pan_decision events with a pose -- can't judge camera movement")
    accept_fail = True
else:
    yaw_spread = max(pan_yaws) - min(pan_yaws)
    print(f"pan_decision yaw spread (radians): {yaw_spread}")
    if yaw_spread < 1e-4:
        print("FAIL: pan_decision yaw never changes -- camera is static, not AI-driven")
        accept_fail = True
    else:
        print("OK: pan_decision yaw shows real movement")

if accept_fail:
    sys.exit(1)
print("ACCEPTANCE: PASS")
PY
accept_rc=${PIPESTATUS[0]}
if [ "$accept_rc" -ne 0 ]; then
  echo "FATAL: follow-cam acceptance check FAILED -- see acceptance.log. followcam.mp4 exists but is NOT confirmed AI-driven." | tee -a acceptance.log
  exit 5
fi
echo "Acceptance OK: AI tracking confirmed active with real detections + camera movement" | tee -a acceptance.log

echo "=== GPU/decode summary (Vulkan render device; decode/detection forced CPU this run) ===" | tee -a env.log
{
  echo "--- Vulkan/GPU device selected (calibrate + stitch) ---"
  grep -iE 'Selected GPU|deviceName|llvmpipe|RTX|GeForce' calibrate.log stitch.log 2>&1 || echo "no GPU-selection lines found"
  echo "--- Decode backend / zero-copy status (expect CPU this run, --no-zero-copy) ---"
  grep -iE 'zero-copy|decode_backend|NVDEC|cuvid|hwaccel' calibrate.log stitch.log 2>&1 || echo "no decode-backend lines found"
} 2>&1 | tee -a env.log

echo "=== All stages completed ==="
