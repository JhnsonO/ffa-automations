#!/usr/bin/env bash
# Idempotent environment bootstrap for running the real production reco-cli
# (JhnsonO/video-stitcher fork, --features cuda) on a RunPod pod.
#
# Target base image (environment contract -- see check below):
#   runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404
#   Ubuntu 24.04, CUDA 12.8.1 userspace, driver observed 580.159.04 in
#   validation session (2026-08-12). This is the ONLY base this script is
#   validated against. If run on a different image, the environment
#   contract check below fails loudly rather than silently limping on.
#
# What this script deliberately does NOT do:
#   - Does not install, upgrade, or replace the NVIDIA host driver, or any
#     package that would pull one in as a dependency (see cudart step).
#   - Does not build or publish a custom Docker image -- that is scoped as
#     a later hardening ticket, once the real unseen follow-cam validation
#     has passed against this bootstrap-script approach.
#   - Does not fetch real production footage, credentials, or secrets. The
#     Reco CUDA smoke test at the end uses a small self-generated synthetic
#     clip pair, matching the same design philosophy as
#     oev_gpu_preflight.sh's own synthetic NVDEC/ORT checks -- it proves
#     the real reco-cli binary's Vulkan/NVDEC/ORT-CUDA-EP stack initialises
#     and runs, not that a specific piece of footage stitches correctly.
#
# Idempotency: every step below either checks for existing state before
# acting, or is itself a no-op-safe operation (apt-get install on an
# already-installed package, rustup install with an existing toolchain,
# git clone into an existing directory is skipped and the repo is fetched
# instead). Safe to re-run after a partial failure or on pod restart.
#
# Usage:
#   ./runpod_bootstrap.sh
# Exit codes:
#   0  = bootstrap + smoke test fully passed
#   1  = environment contract check failed (wrong base image / driver class)
#   2  = a required install step failed
#   3  = reco-cli build failed
#   4  = Reco CUDA smoke test failed (Vulkan / NVDEC / CUDA EP did not pass)

set -uo pipefail

REPO_URL="https://github.com/JhnsonO/video-stitcher.git"
WORKDIR="/tmp/video-stitcher"
LOG_PREFIX="[runpod_bootstrap]"
VERSIONS_LOG="/tmp/runpod_bootstrap_versions.log"
: > "$VERSIONS_LOG"

log() { echo "$LOG_PREFIX $*"; }
log_version() { echo "$1=$2" | tee -a "$VERSIONS_LOG"; }
fail() { log "FATAL: $*"; exit "${2:-1}"; }

# ---------------------------------------------------------------------------
# 0. Environment contract check -- fail loudly if this isn't the validated
#    base, rather than silently attempting an unvalidated recipe.
# ---------------------------------------------------------------------------
log "Checking environment contract..."

if [ ! -f /etc/os-release ]; then
  fail "cannot read /etc/os-release -- cannot verify base image" 1
fi
. /etc/os-release
if [ "${VERSION_ID:-}" != "24.04" ]; then
  fail "expected Ubuntu 24.04, found VERSION_ID='${VERSION_ID:-unknown}'. This script is only validated against runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404. Refusing to proceed on an unvalidated base -- if the RunPod template has changed, this script needs re-validation, not a silent attempt." 1
fi
log_version "os_version_id" "$VERSION_ID"

if ! command -v nvidia-smi >/dev/null 2>&1; then
  fail "nvidia-smi not found -- no NVIDIA driver visible in this container" 1
fi
DRIVER_VERSION=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>&1 | head -1)
GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>&1 | head -1)
if [ -z "$DRIVER_VERSION" ]; then
  fail "nvidia-smi ran but returned no driver version -- GPU not properly passed through" 1
fi
log_version "gpu_name" "$GPU_NAME"
log_version "driver_version" "$DRIVER_VERSION"
DRIVER_MAJOR="${DRIVER_VERSION%%.*}"
if [ "$DRIVER_MAJOR" -lt 550 ] 2>/dev/null; then
  fail "driver $DRIVER_VERSION is older than any CUDA-12-class driver this recipe was validated against (550+). Refusing to guess a compatible ORT_CUDA_VERSION." 1
fi
log "Environment contract OK: Ubuntu $VERSION_ID, driver $DRIVER_VERSION ($GPU_NAME)"

# ---------------------------------------------------------------------------
# 1. Inventory what the base image already provides before installing
#    anything -- do not blindly recreate packages the base already ships.
# ---------------------------------------------------------------------------
log "Inventorying base image..."
for bin in ffmpeg python3 pip3 git curl rustc pkg-config clang; do
  if command -v "$bin" >/dev/null 2>&1; then
    VER=$("$bin" --version 2>&1 | head -1)
    log_version "preexisting_$bin" "$VER"
  else
    log_version "preexisting_$bin" "NOT_PRESENT"
  fi
done

# ---------------------------------------------------------------------------
# 2. Build/runtime apt dependencies. Confirmed via validation session: this
#    base does NOT ship pkg-config, libclang, or the ffmpeg -dev headers,
#    but DOES ship a working ffmpeg 6.1.1 runtime already. apt-get install
#    on already-satisfied packages is a safe no-op, so no pre-check needed
#    here beyond the inventory logging above.
# ---------------------------------------------------------------------------
log "Installing build dependencies..."
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq || fail "apt-get update failed -- check network/repo config" 2
apt-get install -y -qq --no-install-recommends \
  git curl build-essential pkg-config cmake \
  libavutil-dev libavcodec-dev libavformat-dev libswscale-dev \
  libavdevice-dev libavfilter-dev libswresample-dev \
  libssl-dev libclang-dev clang \
  vulkan-tools mesa-vulkan-drivers \
  || fail "apt-get install of build dependencies failed" 2
log "Build dependencies installed."

FFMPEG_VER=$(ffmpeg -version 2>&1 | head -1)
log_version "ffmpeg_after_install" "$FFMPEG_VER"
if ! ffmpeg -version 2>&1 | head -1 | grep -qE 'ffmpeg version (4\.[3-9]|[5-9]\.|[1-9][0-9]\.)'; then
  fail "FFmpeg version too old after install ('$FFMPEG_VER') -- reco-io needs 4.3+ (Pixel::VAAPI). This is the exact failure class that blocked the Ubuntu 20.04 desktop-image attempt." 2
fi

# ---------------------------------------------------------------------------
# 3. Rust toolchain (idempotent: rustup itself no-ops if already installed;
#    skip network call entirely if rustc already present and working).
# ---------------------------------------------------------------------------
if command -v rustc >/dev/null 2>&1 && rustc --version >/dev/null 2>&1; then
  log "Rust toolchain already present, skipping rustup install."
else
  log "Installing Rust toolchain..."
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
    || fail "rustup install failed" 2
fi
source "$HOME/.cargo/env" 2>/dev/null || true
command -v rustc >/dev/null 2>&1 || fail "rustc not on PATH after install" 2
log_version "rustc" "$(rustc --version)"

# ---------------------------------------------------------------------------
# 4. CUDA userspace: runtime libs ONLY. Deliberately install cuda-cudart
#    (a specific version-suffixed package), never the unversioned
#    cuda-runtime meta-package -- that meta-package depends on
#    libnvidia-compute-<ver>, which is a DRIVER package. Installing it on a
#    host whose actual driver is older/newer causes apt to either refuse
#    (confirmed this session: cuda-runtime-12-8 demanded
#    libnvidia-compute-570>=570.211.01 against an actual 570.195.03 driver)
#    or, worse, attempt to actually change the driver -- explicitly
#    forbidden by this script's contract.
# ---------------------------------------------------------------------------
log "Installing CUDA 12 runtime (cudart only, no driver package)..."
CUDART_INSTALLED=""
for ver in 12-8 12-9 12-6 12-5; do
  if apt-get install -y -qq "cuda-cudart-${ver}" --no-install-recommends 2>/tmp/cudart_err.log; then
    CUDART_INSTALLED="cuda-cudart-${ver}"
    break
  fi
done
if [ -z "$CUDART_INSTALLED" ]; then
  cat /tmp/cudart_err.log
  fail "could not install any cuda-cudart-12-* variant. See apt error above -- do NOT fall back to the cuda-runtime meta-package, it pulls a driver package." 2
fi
log_version "cuda_cudart_package" "$CUDART_INSTALLED"

CUDA_LIB_DIR=$(find /usr/local -maxdepth 1 -type d -name "cuda-12.*" 2>/dev/null | sort -V | tail -1)
if [ -z "$CUDA_LIB_DIR" ]; then
  fail "cuda-cudart installed but /usr/local/cuda-12.* not found -- cannot locate lib64" 2
fi
log "CUDA lib dir: $CUDA_LIB_DIR"

log "Installing cuDNN 9 for CUDA 12..."
if ! apt-get install -y -qq libcudnn9-cuda-12 2>/tmp/cudnn_err.log; then
  if ! apt-get install -y -qq cudnn9-cuda-12 2>>/tmp/cudnn_err.log; then
    cat /tmp/cudnn_err.log
    fail "could not install libcudnn9-cuda-12 or cudnn9-cuda-12" 2
  fi
fi
CUDNN_VER=$(dpkg -l | grep -E 'libcudnn9-cuda-12|cudnn9-cuda-12' | awk '{print $3}' | head -1)
log_version "cudnn_package_version" "${CUDNN_VER:-unknown}"

export ORT_CUDA_VERSION=12
export LD_LIBRARY_PATH="${CUDA_LIB_DIR}/lib64:${LD_LIBRARY_PATH:-}"
log_version "ORT_CUDA_VERSION" "$ORT_CUDA_VERSION"

# ---------------------------------------------------------------------------
# 5. EGL ICD override -- forces the real NVIDIA Vulkan driver instead of
#    any GLX-frontend or llvmpipe software fallback. Proven identical
#    across every host tested this project (Vast.ai and RunPod, desktop
#    and headless).
# ---------------------------------------------------------------------------
EGL_ICD_PATH="/tmp/nvidia_egl_icd.json"
EGL_LIB=$(find /usr/lib/x86_64-linux-gnu /usr/lib -iname "libEGL_nvidia.so.0" 2>/dev/null | head -1)
if [ -z "$EGL_LIB" ]; then
  fail "libEGL_nvidia.so.0 not found -- cannot construct Vulkan ICD override. NVIDIA driver may not be properly passed into this container." 2
fi
cat > "$EGL_ICD_PATH" <<JSONEOF
{
    "file_format_version" : "1.0.1",
    "ICD": {
        "library_path": "libEGL_nvidia.so.0",
        "api_version" : "1.4.312"
    }
}
JSONEOF
export VK_DRIVER_FILES="$EGL_ICD_PATH"
export VK_ICD_FILENAMES="$EGL_ICD_PATH"
log "EGL ICD override written to $EGL_ICD_PATH"

VULKAN_CHECK=$(env -u DISPLAY vulkaninfo 2>&1 | grep -iE 'deviceName|deviceType' | head -4)
if ! echo "$VULKAN_CHECK" | grep -q 'DISCRETE_GPU'; then
  fail "Vulkan check after EGL ICD override did not report a DISCRETE_GPU device. Output: $VULKAN_CHECK" 2
fi
log "Vulkan confirmed: $(echo "$VULKAN_CHECK" | tr '\n' ' ')"

# ---------------------------------------------------------------------------
# 6. Build reco-cli with the cuda feature. Idempotent: cargo itself skips
#    recompiling unchanged crates; re-running this script after a source
#    update just picks up the diff.
# ---------------------------------------------------------------------------
log "Fetching/building reco-cli..."
if [ -d "$WORKDIR/.git" ]; then
  log "Existing clone found at $WORKDIR, fetching latest instead of re-cloning."
  git -C "$WORKDIR" fetch origin || fail "git fetch failed in existing clone" 3
  git -C "$WORKDIR" reset --hard origin/main || fail "git reset to origin/main failed" 3
else
  rm -rf "$WORKDIR"
  git clone "$REPO_URL" "$WORKDIR" || fail "git clone failed" 3
fi
REPO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)
log_version "video-stitcher_sha" "$REPO_SHA"

cd "$WORKDIR"
time cargo build --release -p reco-cli --features cuda 2>&1 | tee /tmp/runpod_bootstrap_build.log
BUILD_EXIT=${PIPESTATUS[0]}
if [ "$BUILD_EXIT" -ne 0 ]; then
  fail "cargo build failed (exit $BUILD_EXIT). See /tmp/runpod_bootstrap_build.log" 3
fi

RECO_BIN="$WORKDIR/target/release/reco"
if [ ! -x "$RECO_BIN" ]; then
  fail "build reported success but binary not found at $RECO_BIN" 3
fi
log "reco-cli built: $($RECO_BIN --version 2>&1)"

# Preserve companion ONNX Runtime .so files alongside the binary -- the
# cuda13 cache-bug class of failure on Vast.ai (companion .so missing from
# a cached archive, causing silent CPU fallback) is exactly what this
# logging step exists to make visible, not to cache here (no cache in this
# script -- caching is workflow-level concern, deferred to the RunPod
# workflow integration ticket).
COMPANION_SOS=$(find "$WORKDIR/target/release" -maxdepth 1 -iname "*.so*" 2>/dev/null)
log "Companion .so files alongside reco binary:"
echo "$COMPANION_SOS" | tee -a "$VERSIONS_LOG"

# ---------------------------------------------------------------------------
# 7. Standard 4-check preflight (Vulkan / CUDA driver API / NVDEC / ORT CUDA
#    EP), reused from runpod_gpu_preflight.sh so this script and the
#    standalone preflight never drift apart. Then the real Reco CUDA smoke
#    path: the actual reco-cli binary against a small self-generated
#    synthetic stereo pair, proving the production Vulkan/NVDEC/ORT-CUDA-EP
#    stack initialises for real -- not a Python proxy for it.
# ---------------------------------------------------------------------------
PREFLIGHT_SCRIPT="$(dirname "$0")/runpod_gpu_preflight.sh"
if [ -f "$PREFLIGHT_SCRIPT" ]; then
  log "Running standard 4-check preflight..."
  if ! bash "$PREFLIGHT_SCRIPT"; then
    fail "standard preflight (Vulkan/CUDA/NVDEC/ORT) failed -- see output above" 4
  fi
else
  log "WARNING: runpod_gpu_preflight.sh not found alongside this script -- skipping standard 4-check preflight, proceeding straight to Reco smoke test. Run runpod_gpu_preflight.sh separately to get the full check."
fi

log "Running real Reco CUDA smoke test (synthetic clip pair, actual reco-cli binary)..."
SMOKE_DIR="/tmp/runpod_smoke"
rm -rf "$SMOKE_DIR"
mkdir -p "$SMOKE_DIR"
cd "$SMOKE_DIR"

# Two short synthetic clips with distinct patterns so calibrate's AKAZE
# feature matcher has real (if arbitrary) structure to detect -- a single
# flat color would produce zero features and fail calibrate for reasons
# unrelated to GPU/CUDA health.
ffmpeg -y -loglevel error -f lavfi -i "testsrc2=size=1280x720:rate=30:duration=2" -c:v libx264 -pix_fmt yuv420p smoke_left.mp4
ffmpeg -y -loglevel error -f lavfi -i "mandelbrot=size=1280x720:rate=30" -t 2 -c:v libx264 -pix_fmt yuv420p smoke_right.mp4

# No lens profile available for synthetic footage and no real GoPro
# telemetry to auto-detect from -- this smoke test intentionally does not
# validate calibrate's lens-profile logic (that needs real footage, kept
# out of a generic bootstrap script per the no-secrets/no-large-download
# constraint). It validates the GPU/CUDA/Vulkan/ORT stack only.
"$RECO_BIN" calibrate --no-auto-imu --frames 1 smoke_left.mp4 smoke_right.mp4 \
  --output smoke_match.json 2>&1 | tee smoke_calibrate.log
CALIB_EXIT=${PIPESTATUS[0]}
if [ "$CALIB_EXIT" -ne 0 ] || [ ! -f smoke_match.json ]; then
  fail "Reco smoke calibrate failed (exit $CALIB_EXIT) or produced no match.json. This does not necessarily mean the GPU/CUDA stack is broken -- calibrate on synthetic footage can fail for feature-matching reasons unrelated to GPU health. See smoke_calibrate.log." 4
fi

"$RECO_BIN" stitch --calibration smoke_match.json --width 640 --height 360 \
  --max-frames 20 -o smoke_output.mp4 smoke_left.mp4 smoke_right.mp4 2>&1 | tee smoke_stitch.log
STITCH_EXIT=${PIPESTATUS[0]}

if [ "$STITCH_EXIT" -ne 0 ]; then
  fail "Reco smoke stitch failed (exit $STITCH_EXIT). See smoke_stitch.log" 4
fi
if ! grep -qE 'Selected GPU: NVIDIA' smoke_stitch.log; then
  fail "Reco smoke stitch did not report a real NVIDIA GPU selected -- see smoke_stitch.log for the actual device line" 4
fi
if grep -qiE 'llvmpipe' smoke_stitch.log; then
  fail "Reco smoke stitch fell back to llvmpipe (software Vulkan) -- GPU passthrough is not healthy on this pod" 4
fi
if [ ! -s smoke_output.mp4 ]; then
  fail "Reco smoke stitch reported success but smoke_output.mp4 is missing or empty" 4
fi

log "Reco CUDA smoke test PASSED: real GPU selected, output produced."
log "Versions summary written to $VERSIONS_LOG"
log "Bootstrap complete."
exit 0
