#!/usr/bin/env bash
# Runs ON the Vast.ai instance (uploaded + executed via SSH by
# oev-gpu-detector-test.yml). Isolated from the M1 pipeline, the
# follow-cam test, and the CPU-only detector-resolution A/B script --
# this is a single, narrow test: how fast is 1920 YOLO inference when
# the ORT session actually gets a CUDA execution provider, vs. the CPU
# ORT baseline already measured (1.0 fps in run 31434114262)?
#
# Deliberately changes ONE thing vs. that run: reco-cli is built with
# `--features cuda` (chains reco-cli/cuda -> reco-autocam/cuda ->
# reco-detect/cuda -> ort/cuda, confirmed by reading all three
# Cargo.toml files this session). --no-zero-copy is KEPT (CPU decode +
# CPU letterbox/preprocess, same as before) -- this does NOT touch the
# NVDEC zero-copy / OrtGpuDetector / NPP path at all. CpuYoloDetector
# still calls the same create_ort_session() as before; the only change
# is that this build now has the `#[cfg(feature = "cuda")]` block
# compiled in, so create_ort_session will actually attempt the CUDA EP
# before falling back to CPU. This isolates the inference backend only
# -- decode and preprocess stay exactly as they were.
#
# Does NOT assume the CUDA EP actually engaged just because the run
# was faster: greps stitch.log for the literal ort_session.rs log
# lines ("ORT: CUDA execution provider enabled" vs "ORT: CUDA EP
# failed (...), falling back to CPU") and independently samples
# nvidia-smi GPU utilization during the run, so a silent CPU fallback
# can't be mistaken for a GPU result.
#
# RETEST (run 31466295819 diagnosis): the first attempt's "enabled"
# log line was a false positive -- ort's own internal warning one line
# later ("No execution providers from session options registered
# successfully; may fall back to CPU") showed CUDA never actually
# registered, and GPU utilization sat at ~0% almost the entire run.
# Root cause: cuDNN was never installed (the `cuda-runtime` apt
# meta-package does not include it, and ort's CUDA EP requires it).
# This version adds libcudnn9-cuda-13 and forces ORT_CUDA_VERSION=13
# explicitly (ort rc.12 ships both CUDA 12 and 13 prebuilt binaries
# and can guess wrong when auto-detecting). The acceptance check below
# is also tightened to require ALL THREE of: no fallback warning, GPU
# utilization sustained (not just a brief blip), and throughput
# materially above the 1.0 fps CPU baseline -- not just the presence
# of the "enabled" log line.
#
# Expects, in /tmp/oev_run/:
#   left.mp4, right.mp4   (the full clip pair, already downloaded)
#
# Produces, in /tmp/oev_run/:
#   env.log, build.log, calibrate.log
#   stitch_1920_cuda.log      - reco stitch output
#   gpu_util.log              - nvidia-smi sampled every 5s during the stitch run
#   result_summary.txt        - CUDA EP status + confirmed input size + measured fps, in one place
#   match.json, yolov8n_1920.onnx
#   events_1920_cuda.jsonl
#   followcam_1920_cuda.mp4   (small, diagnostic only)
#
# Exit codes: 1=env/CUDA-runtime/build failure, 2=calibrate failure,
# 3=YOLO export failure, 4=stitch failure.

set -uo pipefail
cd /tmp/oev_run

START_SEC="${START_SEC:-7}"
END_SEC="${END_SEC:-27}"

# --- GitHub Release blob cache. Shared cache repo/tag with the other
# three scripts, but the reco-cli binary asset name is deliberately
# DIFFERENT here (reco-cli-cuda-<sha> vs reco-cli-<sha>) -- a
# cuda-featured binary must never silently become what the
# CPU-only-default scripts pull from the shared cache key. The
# CUDA-runtime .deb cache key IS shared/reused as-is: those .debs are
# pinned to a CUDA toolkit release, not to reco build features, so
# reuse is safe and intended (same reasoning already documented in
# oev_reco_stitch_remote.sh). ---
GH_TOKEN="${GH_TOKEN:-}"
GH_CACHE_REPO="JhnsonO/ffa-automations"
GH_CACHE_TAG="oev-build-cache"

gh_cache_release_id() {
  [ -z "$GH_TOKEN" ] && return 1
  curl -s -H "Authorization: token $GH_TOKEN" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases/tags/$GH_CACHE_TAG" \
    | jq -r 'if .id then .id else empty end'
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
} 2>&1 | tee -a env.log

echo "=== Installing system deps ===" | tee -a env.log
stdbuf -oL -eL apt-get update 2>&1 | tee -a env.log
stdbuf -oL -eL apt-get install -y --no-install-recommends \
  git build-essential pkg-config libssl-dev cmake clang ca-certificates wget jq \
  mesa-vulkan-drivers vulkan-tools libvulkan1 ffmpeg \
  libavutil-dev libavcodec-dev libavformat-dev libswscale-dev \
  libavdevice-dev libavfilter-dev libswresample-dev \
  python3 python3-pip python3-venv 2>&1 | tee -a env.log

# --- CUDA runtime install (verbatim from oev_reco_stitch_remote.sh --
# needed now for real: ort/cuda's execution provider dlopens libcudart
# etc. at runtime, and this build is the first one in the repo that
# actually needs them to be present, not just diagnosed). ---
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

# --- cuDNN 9 (missing in the previous run -- confirmed root cause of
# the silent CUDA EP fallback: ort's CUDA EP requires cuDNN, and the
# `cuda-runtime` apt meta-package does not include it). Package name
# for CUDA 13.x on Ubuntu 24.04 via the NVIDIA apt repo (cuda-keyring,
# already added above) is libcudnn9-cuda-13. ---
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

# ort's rc.12 release ships both CUDA 12 and CUDA 13 prebuilt binaries
# and auto-detects which to use, but can guess wrong (esp. with only
# one CUDA version installed, per ort's own docs). Force it explicitly
# since we know exactly what's on this instance: CUDA 13.2. This must
# be set before `cargo build` (ort-sys's build script downloads/links
# the matching prebuilt binary at build time), and we also export it
# for the stitch run itself in case anything re-checks it at runtime.
export ORT_CUDA_VERSION=13
echo "ORT_CUDA_VERSION=$ORT_CUDA_VERSION (forced explicitly, not auto-detected)" | tee -a env.log

# --- NVIDIA userspace Vulkan library extraction (EGL ICD fix,
# verbatim from the other scripts) -- Vulkan render (wgpu) still needs
# this regardless of the detector backend. ---
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

echo "=== Installing ultralytics + exporting YOLOv8n ONNX at 1920 (NMS baked in) ===" | tee -a env.log
python3 -m venv /tmp/yolo-venv 2>&1 | tee -a env.log
source /tmp/yolo-venv/bin/activate
pip install -q ultralytics 2>&1 | tee -a env.log
yolo export model=yolov8n.pt format=onnx imgsz=1920 nms=True 2>&1 | tee -a env.log
export_rc=${PIPESTATUS[0]}
deactivate
if [ "$export_rc" -ne 0 ] || [ ! -s /tmp/oev_run/yolov8n.onnx ]; then
  echo "FATAL: ultralytics export failed (exit $export_rc)" | tee -a env.log
  exit 3
fi
mv yolov8n.onnx yolov8n_1920.onnx
echo "Model ready: yolov8n_1920.onnx ($(du -h yolov8n_1920.onnx | cut -f1))" | tee -a env.log

echo "=== build.log: cloning + building reco-cli --features cuda ===" | tee build.log
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
# Deliberately distinct cache key from BOTH the CPU-only scripts'
# "reco-cli-<sha>" asset AND the previous cuda attempt's
# "reco-cli-cuda-<sha>" asset (run 31466295819, built with
# ORT_CUDA_VERSION unset/auto-detected). This run forces
# ORT_CUDA_VERSION=13 explicitly per Johnson's instruction to remove
# auto-detection ambiguity entirely -- reusing the old cuda cache here
# would silently skip that and defeat the point of this retest.
BIN_CACHE_ASSET="reco-cli-cuda13-${RECO_SHA}.tar.gz"
bin_cache_hit=0
if [ -n "$GH_TOKEN" ]; then
  BIN_RELEASE_ID=$(gh_cache_release_id)
  if [ -n "$BIN_RELEASE_ID" ] && gh_cache_download "$BIN_RELEASE_ID" "$BIN_CACHE_ASSET" /tmp/reco-cli-cache.tar.gz; then
    echo "reco-cli (cuda) binary cache HIT ($BIN_CACHE_ASSET) — skipping cargo build" | tee -a build.log
    mkdir -p "$(dirname "$RECO_BIN")"
    tar -xzf /tmp/reco-cli-cache.tar.gz -C "$(dirname "$RECO_BIN")"
    chmod +x "$RECO_BIN"
    bin_cache_hit=1
  fi
fi
if [ "$bin_cache_hit" -eq 0 ]; then
  echo "reco-cli (cuda) binary cache MISS ($BIN_CACHE_ASSET, or no GH_TOKEN) — building from source" | tee -a build.log
  timeout 1800 stdbuf -oL -eL cargo build --release -p reco-cli --features cuda -v 2>&1 | tee -a /tmp/oev_run/build.log
  build_rc=${PIPESTATUS[0]}
  if [ "$build_rc" -ne 0 ]; then
    echo "FATAL: cargo build --features cuda failed or timed out (exit $build_rc), see build.log" | tee -a /tmp/oev_run/build.log
    exit 1
  fi
  if [ ! -x "$RECO_BIN" ]; then
    echo "FATAL: build reported success but binary not found at $RECO_BIN" | tee -a /tmp/oev_run/build.log
    exit 1
  fi
  if [ -n "$GH_TOKEN" ]; then
    echo "Caching reco-cli (cuda) binary for next run..." | tee -a /tmp/oev_run/build.log
    tar -czf /tmp/reco-cli-cuda-cache.tar.gz -C "$(dirname "$RECO_BIN")" "$(basename "$RECO_BIN")"
    BIN_RELEASE_ID=$(gh_cache_release_id_or_create)
    gh_cache_upload "$BIN_RELEASE_ID" /tmp/reco-cli-cuda-cache.tar.gz "$BIN_CACHE_ASSET" 2>&1 | tee -a /tmp/oev_run/build.log \
      || echo "Binary cache upload failed (non-fatal)" | tee -a /tmp/oev_run/build.log
  fi
fi
echo "Build OK: $RECO_BIN" | tee -a /tmp/oev_run/build.log
cd /tmp/oev_run

echo "=== calibrate.log: reco calibrate ===" | tee calibrate.log
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

echo "=== stitch_1920_cuda.log: reco stitch, YOLOv8n @ 1920, --features cuda build, --no-zero-copy (CPU decode, GPU inference if CUDA EP engages) ===" | tee stitch_1920_cuda.log
STITCH_ARGS=(stitch left.mp4 right.mp4 -c match.json -o followcam_1920_cuda.mp4
  --model yolov8n_1920.onnx
  --tracking field
  --panner-preset broadcast
  --lookahead 1.5
  --detection-interval 1
  --events events_1920_cuda.jsonl
  --no-zero-copy
  --start-time "$START_SEC" --end-time "$END_SEC"
  --width 640 --height 360)
echo "reco stitch args: ${STITCH_ARGS[*]}" | tee -a stitch_1920_cuda.log

# Independent confirmation the GPU was actually doing work, not just a
# faster wall-clock number: sample nvidia-smi every 5s for the
# duration of the stitch call.
( while true; do date -Iseconds; nvidia-smi --query-gpu=utilization.gpu,utilization.memory,memory.used,memory.total --format=csv,noheader; sleep 5; done > gpu_util.log 2>&1 ) &
GPU_SAMPLER_PID=$!

stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch_1920_cuda.log
stitch_rc=${PIPESTATUS[0]}

kill "$GPU_SAMPLER_PID" 2>/dev/null || true

if [ "$stitch_rc" -ne 0 ]; then
  echo "FATAL: reco stitch failed (exit $stitch_rc), see stitch_1920_cuda.log" | tee -a stitch_1920_cuda.log
  exit 4
fi
echo "Stitch OK: followcam_1920_cuda.mp4 written" | tee -a stitch_1920_cuda.log

echo "=== result_summary.txt ===" | tee result_summary.txt
{
  echo "--- CUDA execution provider status (from ort_session.rs AND ort's own internal EP-registration check) ---"
  echo "Prior run (31466295819) showed this builder-success line is NOT sufficient proof on its own --"
  echo "ort logs its own separate warning one line later if registration actually failed. Check both:"
  grep -E "ORT: CUDA execution provider enabled|ORT: CUDA EP failed" stitch_1920_cuda.log \
    || echo "NEITHER log line found -- ort/cuda EP code path may not have been reached at all, check build.log for --features cuda"
  FALLBACK_WARNING=$(grep -c "No execution providers from session options registered successfully" stitch_1920_cuda.log || true)
  if [ "${FALLBACK_WARNING:-0}" -gt 0 ]; then
    echo "FALLBACK WARNING PRESENT ($FALLBACK_WARNING times) -- CUDA EP did NOT actually register. This alone fails the test regardless of speed."
  else
    echo "No provider-registration fallback warning found -- EP registration signal is clean."
  fi
  echo ""
  echo "--- Confirmed loaded model (input size + conf_thresh) ---"
  grep "CpuYoloDetector loaded" stitch_1920_cuda.log || echo "WARNING: no 'CpuYoloDetector loaded' line found"
  echo ""
  echo "--- Measured throughput ---"
  DONE_LINE=$(grep "^Done: " stitch_1920_cuda.log || echo "")
  echo "${DONE_LINE:-WARNING: no final Done: summary line found}"
  MEASURED_FPS=$(echo "$DONE_LINE" | grep -oE '\([0-9.]+ fps\)' | grep -oE '[0-9.]+' || echo "0")
  echo ""
  echo "--- GPU utilization: full distribution, not just peak (prior run's 27% peak was a brief blip, not sustained) ---"
  if [ -f gpu_util.log ]; then
    # Structured key=value output on ITS OWN LINE per field -- the previous
    # version packed everything into one sentence and extracted fields with
    # `grep -oE '[0-9]+'`, which also matched the literal "10" inside the
    # label text "samples_above_10pct", corrupting the count and silently
    # breaking the verdict below (run 31470468751 genuinely passed but was
    # misreported as FAIL because of this). One value per line removes the
    # ambiguity instead of relying on a smarter regex.
    awk -F',' 'NR%2==0{gsub(/[^0-9.]/,"",$1); n++; sum+=$1+0; if($1+0>max)max=$1+0; if($1+0>10)above10++} END{if(n>0){printf "gpu_util_samples %d\n", n; printf "gpu_util_mean_pct %.1f\n", sum/n; printf "gpu_util_peak_pct %.1f\n", max; printf "gpu_util_samples_above_10pct %d\n", above10} else print "gpu_util_samples 0"}' gpu_util.log > /tmp/gpu_util_stats.txt
    cat /tmp/gpu_util_stats.txt
    TOTAL_SAMPLES=$(awk '$1=="gpu_util_samples"{print $2}' /tmp/gpu_util_stats.txt)
    SAMPLES_ABOVE_10=$(awk '$1=="gpu_util_samples_above_10pct"{print $2}' /tmp/gpu_util_stats.txt)
    TOTAL_SAMPLES="${TOTAL_SAMPLES:-0}"
    SAMPLES_ABOVE_10="${SAMPLES_ABOVE_10:-0}"
  else
    echo "gpu_util.log missing"
    SAMPLES_ABOVE_10=0
    TOTAL_SAMPLES=0
  fi
  echo ""
  echo "--- VERDICT (per Johnson's explicit acceptance bar: no fallback warning AND sustained GPU util AND throughput materially beats 1.0 fps CPU baseline) ---"
  if [ "$TOTAL_SAMPLES" -gt 0 ]; then
    SUSTAINED=$(awk -v a="$SAMPLES_ABOVE_10" -v t="$TOTAL_SAMPLES" 'BEGIN{print (100*a/t >= 50) ? 1 : 0}')
  else
    SUSTAINED=0
  fi
  MATERIALLY_FASTER=$(awk -v fps="${MEASURED_FPS:-0}" 'BEGIN{print (fps > 2.0) ? 1 : 0}')
  if [ "${FALLBACK_WARNING:-0}" -eq 0 ] && [ "$SUSTAINED" -eq 1 ] && [ "$MATERIALLY_FASTER" -eq 1 ]; then
    echo "PASS: CUDA EP registered cleanly, GPU utilization sustained ($SAMPLES_ABOVE_10/$TOTAL_SAMPLES samples above 10%), measured ${MEASURED_FPS:-0} fps materially beats 1.0 fps CPU baseline."
  else
    echo "FAIL: at least one condition not met -- do not treat this run as 'CUDA works'. fallback_warning_count=${FALLBACK_WARNING:-0}, sustained_util=$SUSTAINED ($SAMPLES_ABOVE_10/$TOTAL_SAMPLES), measured_fps=${MEASURED_FPS:-0}"
  fi
} | tee -a result_summary.txt

echo "=== All stages completed ==="
