#!/usr/bin/env bash
# Runs ON the Vast.ai instance (uploaded + executed via SSH by
# oev-followcam-test.yml). Isolated from oev_reco_stitch_remote.sh
# (the M1 cylindrical-panorama pipeline, frozen) -- this is a separate
# experiment: does Reco's existing field follow-cam (person+ball
# detection -> FieldPanner, broadcast preset) produce a real AI-driven
# camera on our real HERO10 pair, using the normal l-shape/perspective
# stitch path (NOT cylindrical-stereo).
#
# Updated to use the proven GPU CUDA detector path (--features cuda +
# cuDNN 9 + ORT_CUDA_VERSION=13, confirmed working in
# oev_gpu_detector_test_remote.sh sessions 11/12/15) at 1920 YOLO
# export resolution (confirmed the practical resolution ceiling --
# 57.2% ball recall vs 7.2% at 640, session 9/10/14) -- both were
# proven on a 20s diagnostic window only, this is the first run that
# actually produces a real follow-cam clip with this combination.
# Still --no-zero-copy (CPU decode, GPU inference only) -- decode/
# zero-copy is a separate, still-unstarted track.
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

gh_cache_release_id_or_create() {
  local id
  id=$(gh_cache_release_id)
  if [ -n "$id" ]; then echo "$id"; return 0; fi
  curl -s -X POST -H "Authorization: token $GH_TOKEN" -H "Content-Type: application/json" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases" \
    -d "{\"tag_name\":\"$GH_CACHE_TAG\",\"name\":\"OEV build cache (auto-managed)\",\"body\":\"Binary/package cache for OEV Vast.ai scripts. Safe to delete - will be recreated on the next run.\",\"prerelease\":true}" \
    | jq -r '.id // empty'
}

gh_cache_upload() {
  local release_id="$1" filepath="$2" name="$3" existing_id
  [ -z "$release_id" ] && { echo "no release id, skipping cache upload of $name"; return 1; }
  existing_id=$(curl -s -H "Authorization: token $GH_TOKEN" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases/$release_id/assets" \
    | jq -r --arg n "$name" '.[] | select(.name==$n) | .id')
  if [ -n "$existing_id" ]; then
    curl -s -X DELETE -H "Authorization: token $GH_TOKEN" \
      "https://api.github.com/repos/$GH_CACHE_REPO/releases/assets/$existing_id" > /dev/null
  fi
  curl -s -X POST -H "Authorization: token $GH_TOKEN" -H "Content-Type: application/gzip" \
    --data-binary @"$filepath" \
    "https://uploads.github.com/repos/$GH_CACHE_REPO/releases/$release_id/assets?name=$name" > /dev/null
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

# --- Random 15-20s test segment (Johnson's request, 2026-08-11): the
# downloaded clip pair is the full ~45s trimmed clip; a random sub-window
# is picked here (not hand-chosen) so this GPU-cache-verification test
# isn't testing a cherry-picked "easy" moment. Purely a segment-length
# change, not a panner/tracker/detector change -- left.mp4/right.mp4 are
# overwritten in place so the rest of the script is unaffected. ---
echo "=== Selecting random 15-20s test segment ===" | tee segment.log
FULL_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 left.mp4)
FULL_DURATION_INT=$(printf '%.0f' "${FULL_DURATION:-0}")
SEG_DURATION=$(( (RANDOM % 6) + 15 ))
MAX_START=$(( FULL_DURATION_INT - SEG_DURATION ))
if [ "$MAX_START" -lt 0 ]; then MAX_START=0; fi
SEG_START=$(( RANDOM % (MAX_START + 1) ))
echo "Full clip duration: ${FULL_DURATION_INT}s. Random segment: start=${SEG_START}s duration=${SEG_DURATION}s" | tee -a segment.log
mv left.mp4 left_full.mp4
mv right.mp4 right_full.mp4
ffmpeg -y -ss "$SEG_START" -i left_full.mp4 -t "$SEG_DURATION" -c copy left.mp4 2>&1 | tee -a segment.log
ffmpeg -y -ss "$SEG_START" -i right_full.mp4 -t "$SEG_DURATION" -c copy right.mp4 2>&1 | tee -a segment.log
if [ ! -s left.mp4 ] || [ ! -s right.mp4 ]; then
  echo "FATAL: random segment trim failed, left.mp4/right.mp4 missing/empty" | tee -a segment.log
  exit 1
fi
echo "Segment trim OK: left.mp4/right.mp4 now ~${SEG_DURATION}s starting at ${SEG_START}s" | tee -a segment.log

# --- CUDA runtime install (verbatim from oev_gpu_detector_test_remote.sh,
# itself verbatim from oev_reco_stitch_remote.sh -- proven working in
# sessions 11/12/15). Needed for real now: ort/cuda's execution
# provider dlopens libcudart etc. at runtime. ---
echo "=== Installing CUDA runtime (plain ubuntu image has no CUDA libs by default) ===" | tee -a env.log
{
  wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb -O /tmp/cuda-keyring.deb \
    && dpkg -i /tmp/cuda-keyring.deb \
    && apt-get update
} 2>&1 | tee -a env.log

HOST_CUDA_MAX=$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -1)
echo "Host driver max-supported CUDA version: ${HOST_CUDA_MAX:-unknown (nvidia-smi parse failed)}" | tee -a env.log

CUDA_CACHE_ASSET="cuda-runtime-debs.tar.gz"
cuda_cache_hit=0
if [ -n "$GH_TOKEN" ]; then
  CUDA_RELEASE_ID=$(gh_cache_release_id)
  if [ -n "$CUDA_RELEASE_ID" ] && gh_cache_download "$CUDA_RELEASE_ID" "$CUDA_CACHE_ASSET" /tmp/cuda-runtime-debs.tar.gz; then
    echo "CUDA runtime cache HIT ($CUDA_CACHE_ASSET) — installing from cached .debs" | tee -a env.log
    mkdir -p /tmp/cuda-cache && tar -xzf /tmp/cuda-runtime-debs.tar.gz -C /tmp/cuda-cache
    { dpkg -i /tmp/cuda-cache/*.deb || apt-get install -f -y; } 2>&1 | tee -a env.log
    cuda_cache_hit=1
  fi
fi
if [ "$cuda_cache_hit" -eq 0 ]; then
  echo "CUDA runtime cache MISS (or no GH_TOKEN) — installing via apt" | tee -a env.log
  mkdir -p /tmp/cuda-cache
  {
    ( apt-get install -y --no-install-recommends --download-only -o "Dir::Cache::Archives=/tmp/cuda-cache" cuda-runtime \
        || { echo "unversioned cuda-runtime not found, searching repo for a compatible versioned package..."; \
             if [ -n "$HOST_CUDA_MAX" ]; then \
               CUDA_PKG=$(apt-cache search '^cuda-runtime-[0-9]' | awk '{print $1}' \
                 | sed -E 's/cuda-runtime-([0-9]+)-([0-9]+)/\1.\2 &/' \
                 | sort -V \
                 | awk -v max="$HOST_CUDA_MAX" '{split(max,m,"."); split($1,v,"."); if ((v[1]<m[1]) || (v[1]==m[1] && v[2]<=m[2])) last=$2} END{print last}'); \
               echo "Highest cuda-runtime-X-Y <= host max ($HOST_CUDA_MAX): ${CUDA_PKG:-none found}"; \
             else \
               echo "No host CUDA max known — falling back to newest available"; \
               CUDA_PKG=$(apt-cache search '^cuda-runtime-[0-9]' | sort -V | tail -1 | awk '{print $1}'); \
             fi; \
             if [ -n "$CUDA_PKG" ]; then \
               echo "Installing: $CUDA_PKG"; \
               apt-get install -y --no-install-recommends --download-only -o "Dir::Cache::Archives=/tmp/cuda-cache" "$CUDA_PKG"; \
             else \
               echo "No compatible cuda-runtime-* package found in repo"; \
               exit 1; \
             fi; } ) \
      && { dpkg -i /tmp/cuda-cache/*.deb || apt-get install -f -y; } \
      || echo "CUDA runtime install failed — see above for the actual error"
  } 2>&1 | tee -a env.log
  if [ -n "$GH_TOKEN" ] && ls /tmp/cuda-cache/*.deb >/dev/null 2>&1; then
    echo "Caching downloaded CUDA runtime .debs for next run..." | tee -a env.log
    tar -czf /tmp/cuda-runtime-debs.tar.gz -C /tmp/cuda-cache .
    CUDA_RELEASE_ID=$(gh_cache_release_id_or_create)
    gh_cache_upload "$CUDA_RELEASE_ID" /tmp/cuda-runtime-debs.tar.gz "$CUDA_CACHE_ASSET" 2>&1 | tee -a env.log \
      || echo "CUDA runtime cache upload failed (non-fatal)" | tee -a env.log
  fi
fi
ldconfig
{
  echo "--- libcudart / libcublas presence after install ---"
  ldconfig -p | grep -iE 'libcudart|libcublas' || echo "WARNING: no libcudart/libcublas found after CUDA runtime install"
} 2>&1 | tee -a env.log

# --- cuDNN 9 (ort's CUDA EP requires it; cuda-runtime meta-package
# does not include it -- root-caused in session 11). Package name for
# CUDA 13.x on Ubuntu 24.04 via the NVIDIA apt repo is libcudnn9-cuda-13. ---
echo "=== Installing cuDNN 9 (for CUDA 13) ===" | tee -a env.log
{
  apt-get install -y --no-install-recommends libcudnn9-cuda-13 \
    || { echo "libcudnn9-cuda-13 not found, trying unversioned cudnn9-cuda-13 meta-package"; \
         apt-get install -y --no-install-recommends cudnn9-cuda-13; }
} 2>&1 | tee -a env.log
ldconfig
{
  echo "--- libcudnn presence after install ---"
  ldconfig -p | grep -i libcudnn || echo "FATAL-LOOKING: no libcudnn found after install attempt"
} 2>&1 | tee -a env.log

# ort's rc.12 release auto-detects CUDA version but can guess wrong --
# force it explicitly since we know this instance: CUDA 13.2. Must be
# set before cargo build (ort-sys's build script downloads/links the
# matching prebuilt binary at build time).
export ORT_CUDA_VERSION=13
echo "ORT_CUDA_VERSION=$ORT_CUDA_VERSION (forced explicitly, not auto-detected)" | tee -a env.log

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

echo "=== Installing ultralytics + exporting YOLOv8n ONNX at 1920 (NMS baked in, proven resolution per session 9/10/14) ===" | tee -a env.log
# Proof-run only per Johnson's explicit direction: unpinned `pip install
# ultralytics` is acceptable here, NOT for anything production/recurring.
# reco-detect's ORT CPU backend requires an ONNX model with built-in NMS
# (see reco-detect README); `nms=True` on export is what ultralytics
# calls this. Standard COCO classes cover both person (0) and sports
# ball (32), which is everything --tracking field needs.
python3 -m venv /tmp/yolo-venv 2>&1 | tee -a env.log
source /tmp/yolo-venv/bin/activate
pip install -q ultralytics 2>&1 | tee -a env.log
yolo export model=yolov8n.pt format=onnx imgsz=1920 nms=True 2>&1 | tee -a env.log
export_rc=${PIPESTATUS[0]}
deactivate
if [ "$export_rc" -ne 0 ] || [ ! -s /tmp/oev_run/yolov8n.onnx ]; then
  echo "FATAL: ultralytics export failed or yolov8n.onnx missing (exit $export_rc)" | tee -a env.log
  exit 1
fi
echo "YOLO model ready: /tmp/oev_run/yolov8n.onnx ($(du -h /tmp/oev_run/yolov8n.onnx | cut -f1))" | tee -a env.log

echo "=== build.log: cloning + building reco-cli (--features cuda) ===" | tee build.log
# Same fork. Now built WITH --features cuda (default autocam,ort + cuda)
# to get GPU-accelerated inference via ort's CUDA EP, per the proven
# recipe from oev_gpu_detector_test_remote.sh (sessions 11/12/15).
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
echo "JhnsonO/video-stitcher HEAD: $RECO_SHA" | tee -a /tmp/oev_run/build.log
RECO_BIN="/tmp/reco-src/target/release/reco"
RECO_BIN_DIR="$(dirname "$RECO_BIN")"
# CUDA execution provider's companion shared lib -- confirmed missing after
# a cache-HIT in run 31494793310 (2026-08-11), causing a silent CPU
# fallback (docs/ai-project-state.md cont.19/20). Hard-verified below
# regardless of which path (cache-restore or fresh build) produced the
# binary, so this can never recur silently.
CUDA_SO_NAME="libonnxruntime_providers_shared.so"
# Same cache key as oev_gpu_detector_test_remote.sh's cuda build
# ("reco-cli-cuda13-<sha>") -- deliberately shared: a rebuild triggered
# by either script benefits the other, since the binary is identical
# for the same source SHA + ORT_CUDA_VERSION=13. Distinct from the
# CPU-only "reco-cli-<sha>" key used by the M1 pipeline.
BIN_CACHE_ASSET="reco-cli-cuda13-${RECO_SHA}.tar.gz"
bin_cache_hit=0
if [ -n "$GH_TOKEN" ]; then
  BIN_RELEASE_ID=$(gh_cache_release_id)
  if [ -n "$BIN_RELEASE_ID" ] && gh_cache_download "$BIN_RELEASE_ID" "$BIN_CACHE_ASSET" /tmp/reco-cli-cache.tar.gz; then
    echo "reco-cli (cuda) binary cache HIT ($BIN_CACHE_ASSET) — skipping cargo build" | tee -a /tmp/oev_run/build.log
    mkdir -p "$RECO_BIN_DIR"
    tar -xzf /tmp/reco-cli-cache.tar.gz -C "$RECO_BIN_DIR"
    chmod +x "$RECO_BIN"
    if [ -f "$RECO_BIN_DIR/$CUDA_SO_NAME" ]; then
      bin_cache_hit=1
    else
      echo "Cached binary is MISSING $CUDA_SO_NAME (stale/incomplete cache) — discarding cache, forcing a real build" | tee -a /tmp/oev_run/build.log
    fi
  fi
fi
if [ "$bin_cache_hit" -eq 0 ]; then
  echo "reco-cli (cuda) binary cache MISS ($BIN_CACHE_ASSET, or no GH_TOKEN, or cache invalid) — building from source" | tee -a /tmp/oev_run/build.log
  timeout 1800 stdbuf -oL -eL cargo build --release -p reco-cli --features cuda -v 2>&1 | tee -a /tmp/oev_run/build.log
  build_rc=${PIPESTATUS[0]}
  if [ "$build_rc" -eq 124 ]; then
    echo "FATAL: cargo build timed out after 30min (likely slow-network host), see build.log" | tee -a /tmp/oev_run/build.log
    exit 1
  elif [ "$build_rc" -ne 0 ]; then
    echo "FATAL: cargo build --features cuda failed (exit $build_rc), see build.log" | tee -a /tmp/oev_run/build.log
    exit 1
  fi
  if [ ! -x "$RECO_BIN" ]; then
    echo "FATAL: build reported success but binary not found at $RECO_BIN" | tee -a /tmp/oev_run/build.log
    exit 1
  fi
  if [ -n "$GH_TOKEN" ]; then
    # Cache the binary AND any companion shared libraries next to it
    # (e.g. libonnxruntime_providers_shared.so) -- the cont.15 fix, so
    # this script's own cache writes stay correct too, not just the
    # detector-test script's.
    CACHE_SO_FILES=$(find "$RECO_BIN_DIR" -maxdepth 1 -name '*.so*' -printf '%f\n' 2>/dev/null)
    echo "Caching reco-cli (cuda) binary + companion .so files for next run: $(basename "$RECO_BIN") $CACHE_SO_FILES" | tee -a /tmp/oev_run/build.log
    tar -czf /tmp/reco-cli-cuda-cache.tar.gz -C "$RECO_BIN_DIR" "$(basename "$RECO_BIN")" $CACHE_SO_FILES
    BIN_RELEASE_ID=$(gh_cache_release_id_or_create)
    gh_cache_upload "$BIN_RELEASE_ID" /tmp/reco-cli-cuda-cache.tar.gz "$BIN_CACHE_ASSET" 2>&1 | tee -a /tmp/oev_run/build.log \
      || echo "Binary cache upload failed (non-fatal)" | tee -a /tmp/oev_run/build.log
  fi
fi
# Hard verification regardless of path taken -- this is the actual guarantee
# Johnson asked for: never silently proceed to stitch on a CPU-fallback
# ONNXRuntime session.
if [ ! -f "$RECO_BIN_DIR/$CUDA_SO_NAME" ]; then
  echo "FATAL: $CUDA_SO_NAME missing next to $RECO_BIN after build/cache-restore -- would silently fall back to CPU. Aborting." | tee -a /tmp/oev_run/build.log
  exit 1
fi
echo "Build OK: $RECO_BIN (CUDA provider companion lib confirmed present)" | tee -a /tmp/oev_run/build.log
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

# Fixed St Margaret's field ROI (prototype, single venue/camera setup).
# Injected into match.json after calibrate, before stitch, so reco stitch's
# already-existing field_roi auto-load (reco-cli/src/stitch.rs) filters out
# detections from the neighbouring pitch. No new CLI flag, no ROI-editor
# invocation, no venue-preset framework -- coordinates below are the fixed
# prototype polygon Johnson marked on the calibrate-stills screenshots for
# this exact clip pair/camera setup (docs/ai-project-state.md, cont.18).
echo "Injecting St Margaret's field_roi into match.json" | tee -a calibrate.log
python3 - <<'PYROI'
import json

with open("match.json") as f:
    match = json.load(f)

match["field_roi"] = {
    "left": [
        [0.1227, 0.9611],
        [0.0573, 0.6846],
        [0.1802, 0.6285],
        [0.2645, 0.5769],
        [0.4382, 0.4864],
        [0.4988, 0.4658],
        [0.5942, 0.4474],
        [0.7835, 0.4175],
        [0.9285, 0.3785],
        [1.0000, 1.0000],
        [0.1227, 1.0000],
    ],
    "right": [
        [0.0391, 0.4206],
        [0.0818, 0.4101],
        [0.1839, 0.4070],
        [0.2783, 0.4070],
        [0.3448, 0.4083],
        [0.4100, 0.4161],
        [0.4684, 0.4319],
        [0.6239, 0.4801],
        [0.7368, 0.5200],
        [0.7980, 0.5465],
        [0.7454, 0.9011],
        [0.7454, 1.0000],
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
if ! python3 -c "
import json, sys
m = json.load(open('match.json'))
roi = m.get('field_roi')
assert roi and isinstance(roi.get('left'), list) and len(roi['left']) > 0, 'field_roi.left missing/empty'
assert isinstance(roi.get('right'), list) and len(roi['right']) > 0, 'field_roi.right missing/empty'
" 2>>calibrate.log; then
  echo "FATAL: match.json field_roi validation failed after injection" | tee -a calibrate.log
  exit 2
fi
echo "field_roi validated in match.json (left/right polygons present)" | tee -a calibrate.log

echo "=== stitch.log: reco stitch (field follow-cam, l-shape, --features cuda build, --no-zero-copy) ===" | tee stitch.log
# Exact flag set agreed with Johnson: normal perspective (l-shape,
# default) projection, NOT cylindrical -- follow-cam uses Reco's
# standard virtual-camera path, panorama distortion is irrelevant here.
# --no-zero-copy still forces CPU decode + CPU letterbox, but with
# --features cuda + cuDNN + ORT_CUDA_VERSION=13 the YOLO inference
# itself now runs on GPU (isolates "does GPU inference help" from the
# separate, still out-of-scope NVDEC zero-copy question).
# --detection-interval 1 (no frame-skipping -- that's a separate,
# explicitly deferred optimisation track, not part of this ticket).
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

echo "=== GPU/decode summary (Vulkan render device; CUDA EP for detection, decode still CPU/--no-zero-copy) ===" | tee -a env.log
{
  echo "--- Vulkan/GPU device selected (calibrate + stitch) ---"
  grep -iE 'Selected GPU|deviceName|llvmpipe|RTX|GeForce' calibrate.log stitch.log 2>&1 || echo "no GPU-selection lines found"
  echo "--- Decode backend / zero-copy status (expect CPU this run, --no-zero-copy) ---"
  grep -iE 'zero-copy|decode_backend|NVDEC|cuvid|hwaccel' calibrate.log stitch.log 2>&1 || echo "no decode-backend lines found"
  echo "--- CUDA EP status (informational -- the acceptance gate above already requires real detections+movement regardless of backend, this just confirms whether they came from GPU or a silent CPU fallback) ---"
  if grep -qE "No execution providers from session options registered successfully" stitch.log; then
    echo "CUDA EP FALLBACK WARNING PRESENT -- detection almost certainly ran on CPU despite --features cuda build. Not fatal to this run (acceptance already passed on detection/movement), but treat 'proven GPU 1920' claims about THIS run's speed with caution."
  elif grep -qE "ORT: CUDA execution provider enabled" stitch.log; then
    echo "CUDA EP enabled log line found, no fallback warning -- detection likely ran on GPU."
  else
    echo "Neither CUDA EP log line found -- inconclusive, check build.log for whether --features cuda actually took effect."
  fi
} 2>&1 | tee -a env.log

echo "=== All stages completed ==="
