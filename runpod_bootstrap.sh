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
#     Reco CUDA smoke test at the end derives a left/right clip pair from a
#     single deterministic synthetic source (guaranteed feature overlap by
#     construction, not probabilistic), and exercises the actual --model
#     detector path with a small real YOLO ONNX export -- it proves the
#     real reco-cli binary's Vulkan/NVDEC/CUDAExecutionProvider/
#     OrtGpuDetector stack initialises and runs for real, not just that
#     the renderer/decode path works without a detector attached.
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
# Check against the already-captured $FFMPEG_VER via a here-string, not a
# second "ffmpeg -version | head -1 | grep -q" pipe. Same SIGPIPE-under-
# pipefail false-negative class documented 12 Aug 2026 (vulkaninfo/CUDA/
# NVDEC/ORT checks): head -1 closing its read end early can SIGPIPE the
# still-writing ffmpeg process on a long real build-config output, and
# with pipefail that flips this check to FATAL even when the version is
# genuinely fine. Re-checking the value already in hand avoids the
# second invocation (and the pipe) entirely rather than relying on a
# race between head's read and ffmpeg's write completing.
if ! grep -qE 'ffmpeg version (4\.[3-9]|[5-9]\.|[1-9][0-9]\.)' <<< "$FFMPEG_VER"; then
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

# cuDNN: do NOT assume a specific apt package is required. Confirmed this
# session that libcudnn9-cuda-12 can fail to install (apt exit 100) on this
# exact base image while the real production reco-cli run still registered
# CUDAExecutionProvider and completed real inference successfully --
# meaning the base image (which ships PyTorch + its own bundled CUDA
# userspace libs, commonly under a Python site-packages nvidia/ tree)
# already provides what's actually needed at runtime. Installing a package
# "because it seemed like it should be needed" without evidence is exactly
# the mistake this section previously made.
#
# Correct order: inventory what's already resolvable BEFORE attempting any
# install, and only install if evidence (ldd on the real built .so, once
# it exists) shows something is genuinely missing. The build step below
# does not require cuDNN at build time (ort-sys downloads a prebuilt
# onnxruntime binary; cuDNN is a dlopen'd runtime dependency of that
# binary, not a compile-time link), so cuDNN resolution is deferred until
# after the build produces the real libonnxruntime_providers_cuda.so to
# inspect.
log "Inventorying pre-existing CUDA userspace libraries (cudnn/cublas)..."
EXISTING_CUDNN=$(find / -xdev -iname "libcudnn*.so*" 2>/dev/null | grep -v '^/proc')
EXISTING_CUBLAS=$(find / -xdev -iname "libcublas*.so*" 2>/dev/null | grep -v '^/proc')
log "Pre-existing libcudnn*: ${EXISTING_CUDNN:-none found}"
log "Pre-existing libcublas*: ${EXISTING_CUBLAS:-none found}"
echo "preexisting_libcudnn_paths=${EXISTING_CUDNN:-none}" >> "$VERSIONS_LOG"
echo "preexisting_libcublas_paths=${EXISTING_CUBLAS:-none}" >> "$VERSIONS_LOG"

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
if ! grep -q 'DISCRETE_GPU' <<< "$VULKAN_CHECK"; then
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
git -C "$WORKDIR" fetch origin agent/high-res-ball-roi-recovery || fail "git fetch of recovery branch failed" 3
git -C "$WORKDIR" reset --hard de0160580c3c69cc9007194d9347ca0db2f98e8c || fail "git reset to recovery revision failed" 3
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
# 6b. Evidence-based cuDNN/cublas resolution. Inspect the REAL runtime
# dependencies of the actual built libonnxruntime_providers_cuda.so via
# ldd, rather than assuming a package is needed. Only install something if
# ldd shows an actual "=> not found" entry -- never install speculatively.
# ---------------------------------------------------------------------------
CUDA_PROVIDER_SO=$(find "$WORKDIR/target/release" -maxdepth 1 -iname "libonnxruntime_providers_cuda.so" 2>/dev/null | head -1)
if [ -z "$CUDA_PROVIDER_SO" ]; then
  log "WARNING: libonnxruntime_providers_cuda.so not found alongside reco binary -- cannot inventory its dependencies. This may itself indicate a problem (see companion .so list above); proceeding to the smoke test, which will fail loudly if CUDA EP cannot load."
else
  log "Inspecting real runtime dependencies of $CUDA_PROVIDER_SO..."
  LDD_OUT=$(ldd "$CUDA_PROVIDER_SO" 2>&1)
  echo "$LDD_OUT" | tee -a "$VERSIONS_LOG"
  MISSING_LIBS=$(echo "$LDD_OUT" | grep "not found" | awk '{print $1}' | sort -u)

  if [ -z "$MISSING_LIBS" ]; then
    log "All shared library dependencies of libonnxruntime_providers_cuda.so already resolve -- no cuDNN/cublas package install needed. Base image already provides what's required (confirmed by ldd, not assumed)."
  else
    log "ldd reports missing libraries: $MISSING_LIBS"
    for lib in $MISSING_LIBS; do
      case "$lib" in
        libcudnn*)
          log "Missing $lib -- attempting libcudnn9-cuda-12 (evidence: ldd, not speculation)"
          if apt-get install -y -qq libcudnn9-cuda-12 2>/tmp/cudnn_err.log || apt-get install -y -qq cudnn9-cuda-12 2>>/tmp/cudnn_err.log; then
            log "cuDNN package install succeeded."
          else
            log "WARNING: cuDNN package install failed (see /tmp/cudnn_err.log). Proceeding anyway -- the smoke test below is the real acceptance signal, not this install's exit code. A failed apt install here does not necessarily mean the environment is broken, per this session's own evidence (production run succeeded despite this exact package failing on this exact base image previously)."
            cat /tmp/cudnn_err.log
          fi
          ;;
        libcublas*)
          log "Missing $lib -- this should have come from cuda-cudart-12-8/cuda-libraries; re-checking cuda lib dir is on LD_LIBRARY_PATH ($LD_LIBRARY_PATH)"
          ;;
        *)
          log "Missing $lib -- no known fix in this script; smoke test will surface whether this is fatal"
          ;;
      esac
    done
    # Re-check after any install attempts, log the final state either way.
    LDD_RECHECK=$(ldd "$CUDA_PROVIDER_SO" 2>&1)
    STILL_MISSING=$(echo "$LDD_RECHECK" | grep "not found")
    if [ -n "$STILL_MISSING" ]; then
      log "After install attempts, still unresolved: $STILL_MISSING -- this will be caught by the Reco CUDA smoke test below if it's actually fatal."
    else
      log "All dependencies resolved after install attempts."
    fi
  fi
fi

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

log "Running real Reco CUDA smoke test (deterministic calibratable fixture + real --model detector path)..."
SMOKE_DIR="/tmp/runpod_smoke"
rm -rf "$SMOKE_DIR"
mkdir -p "$SMOKE_DIR"
cd "$SMOKE_DIR"

# --- Deterministic, guaranteed-calibratable fixture ---
# Generate ONE patterned source, then derive left/right by cropping two
# overlapping windows of it (same underlying pixels, shifted). This
# guarantees shared AKAZE features exist by construction -- unlike two
# unrelated generators (e.g. testsrc2 + mandelbrot), which may or may not
# share enough structure for calibrate to find matches, making that
# earlier version's pass/fail probabilistic rather than deterministic.
SRC_W=2560
SRC_H=720
CROP_W=1280
CROP_H=720
LEFT_X=0
RIGHT_X=640   # 640px overlap out of 1280px crop width = 50% shared region

ffmpeg -y -loglevel error -f lavfi -i "testsrc2=size=${SRC_W}x${SRC_H}:rate=30:duration=2" \
  -c:v libx264 -pix_fmt yuv420p smoke_source.mp4
ffmpeg -y -loglevel error -i smoke_source.mp4 \
  -vf "crop=${CROP_W}:${CROP_H}:${LEFT_X}:0" -c:v libx264 -pix_fmt yuv420p smoke_left.mp4
ffmpeg -y -loglevel error -i smoke_source.mp4 \
  -vf "crop=${CROP_W}:${CROP_H}:${RIGHT_X}:0" -c:v libx264 -pix_fmt yuv420p smoke_right.mp4

# No lens profile available for synthetic footage and no real GoPro
# telemetry to auto-detect from -- this smoke test intentionally does not
# validate calibrate's lens-profile logic (that needs real footage, kept
# out of a generic bootstrap script per the no-secrets/no-large-download
# constraint). It validates the GPU/CUDA/Vulkan/ORT/detector stack.
"$RECO_BIN" calibrate --no-auto-imu --frames 1 smoke_left.mp4 smoke_right.mp4 \
  --output smoke_match.json 2>&1 | tee smoke_calibrate.log
CALIB_EXIT=${PIPESTATUS[0]}
if [ "$CALIB_EXIT" -ne 0 ] || [ ! -f smoke_match.json ]; then
  fail "Reco smoke calibrate failed (exit $CALIB_EXIT) or produced no match.json against a DETERMINISTIC overlapping fixture -- this is a real failure, not a probabilistic feature-matching miss (the fixture is constructed to guarantee shared features). See smoke_calibrate.log." 4
fi
if ! grep -qE 'matched points|frame pairs produced matches' smoke_calibrate.log; then
  fail "Reco smoke calibrate exited 0 but log shows no evidence of real feature matching -- see smoke_calibrate.log" 4
fi

# --- Small real ONNX YOLO model, same code path as production (NMS
# built in, input size read from the model itself) so the smoke test
# actually exercises OrtGpuDetector / CUDAExecutionProvider, not just the
# stitch renderer. Uses a small imgsz (320, vs production's 1920) purely
# for smoke-test speed -- this validates the CODE PATH and CUDA EP
# registration, not production detection quality at production
# resolution. Idempotent: skips re-export if the model already exists. ---
SMOKE_MODEL="$SMOKE_DIR/smoke_yolov8n.onnx"
if [ ! -f "$SMOKE_MODEL" ]; then
  log "Exporting small smoke-test YOLO model..."
  SMOKE_VENV="/tmp/runpod_smoke_venv"
  if [ ! -d "$SMOKE_VENV" ]; then
    python3 -m venv "$SMOKE_VENV" || fail "failed to create venv for smoke model export" 4
  fi
  # shellcheck disable=SC1091
  source "$SMOKE_VENV/bin/activate"
  pip install -q ultralytics >/tmp/smoke_ultralytics_install.log 2>&1 \
    || fail "pip install ultralytics failed for smoke model export -- see /tmp/smoke_ultralytics_install.log" 4
  yolo export model=yolov8n.pt format=onnx nms=True imgsz=320 project="$SMOKE_DIR" >/tmp/smoke_yolo_export.log 2>&1
  EXPORT_EXIT=$?
  deactivate
  FOUND_ONNX=$(find "$SMOKE_DIR" /tmp -maxdepth 3 -iname "yolov8n.onnx" -newer /tmp/smoke_ultralytics_install.log 2>/dev/null | head -1)
  if [ "$EXPORT_EXIT" -ne 0 ] || [ -z "$FOUND_ONNX" ]; then
    cat /tmp/smoke_yolo_export.log
    fail "YOLO ONNX export for smoke test failed -- see /tmp/smoke_yolo_export.log" 4
  fi
  cp "$FOUND_ONNX" "$SMOKE_MODEL"
  log "Smoke model exported to $SMOKE_MODEL"
else
  log "Smoke model already present at $SMOKE_MODEL, skipping re-export."
fi

"$RECO_BIN" stitch --calibration smoke_match.json --width 640 --height 360 \
  --model "$SMOKE_MODEL" --tracking field --detection-interval 1 --lookahead 0 \
  --max-frames 20 -o smoke_output.mp4 smoke_left.mp4 smoke_right.mp4 2>&1 | tee smoke_stitch.log
STITCH_EXIT=${PIPESTATUS[0]}

if [ "$STITCH_EXIT" -ne 0 ]; then
  fail "Reco smoke stitch (with --model) failed (exit $STITCH_EXIT). See smoke_stitch.log" 4
fi

# Every one of these is required -- no partial credit. A missing
# CUDAExecutionProvider or a llvmpipe/CPU-fallback signature means the
# smoke test does NOT pass, even if stitch exits 0 and produces a file.
SMOKE_FAIL_REASONS=""
grep -q "Selected GPU: NVIDIA" smoke_stitch.log || SMOKE_FAIL_REASONS="${SMOKE_FAIL_REASONS}no real NVIDIA GPU selected; "
grep -qi "llvmpipe" smoke_stitch.log && SMOKE_FAIL_REASONS="${SMOKE_FAIL_REASONS}llvmpipe (software Vulkan) detected; "
grep -q "Successfully registered \`CUDAExecutionProvider\`" smoke_stitch.log || SMOKE_FAIL_REASONS="${SMOKE_FAIL_REASONS}CUDAExecutionProvider registration not confirmed; "
grep -q "ORT: CUDA execution provider enabled" smoke_stitch.log || SMOKE_FAIL_REASONS="${SMOKE_FAIL_REASONS}'ORT: CUDA execution provider enabled' not logged; "
grep -qE "OrtGpuDetector.*warmup inference complete" smoke_stitch.log || SMOKE_FAIL_REASONS="${SMOKE_FAIL_REASONS}OrtGpuDetector warmup inference not confirmed; "
grep -qi "falling back to CPU" smoke_stitch.log && SMOKE_FAIL_REASONS="${SMOKE_FAIL_REASONS}explicit CPU fallback logged; "
grep -qE "NVDEC \(CUDA\)" smoke_stitch.log || SMOKE_FAIL_REASONS="${SMOKE_FAIL_REASONS}NVDEC zero-copy decode not confirmed; "
[ -s smoke_output.mp4 ] || SMOKE_FAIL_REASONS="${SMOKE_FAIL_REASONS}smoke_output.mp4 missing or empty; "

if [ -n "$SMOKE_FAIL_REASONS" ]; then
  fail "Reco CUDA smoke test FAILED acceptance: $SMOKE_FAIL_REASONS See smoke_stitch.log for full detail." 4
fi

log "Reco CUDA smoke test PASSED: real GPU selected, CUDAExecutionProvider registered, OrtGpuDetector warmup completed, NVDEC zero-copy confirmed, no CPU fallback, output produced."
log "Versions summary written to $VERSIONS_LOG"
log "Bootstrap complete."
exit 0
