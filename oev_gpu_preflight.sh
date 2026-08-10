#!/usr/bin/env bash
# GPU-health preflight, run via SSH against a freshly-launched Vast.ai
# instance BEFORE it is accepted into the offer-selection loop -- i.e.
# before any clip download, Rust build, calibrate, or stitch work happens.
#
# Purpose: Vast host GPU-passthrough completeness is inconsistent (confirmed
# across multiple ffa-automations dispatches -- see docs/ai-project-state.md,
# session "ROOT CAUSE CONFIRMED" and the follow-up NVDEC/NVENC diagnostics).
# Some hosts render/decode cleanly on real GPU; others fail with
# CUDA_ERROR_NO_DEVICE despite an apparently healthy driver. This script
# gives the offer loop a fast, cheap way to detect a bad host and move on
# to the next offer instead of discovering the problem 10+ minutes into a
# full build/calibrate/stitch run.
#
# Pass criteria (all three required):
#   1. EGL Vulkan ICD sees a real discrete NVIDIA GPU (not llvmpipe).
#   2. Bare CUDA Driver API: cuInit(0) succeeds, cuDeviceGetCount >= 1.
#   3. Minimal NVDEC decode smoke test succeeds (CUDA hwaccel decode of a
#      tiny self-generated synthetic clip -- no dependency on GoPro clip
#      download, which happens later and is a separate, larger download).
#
# NVENC is deliberately NOT tested here and NOT a pass criterion -- NVENC
# failure on Vast is a confirmed structural GeForce/consumer-GPU-in-container
# limitation, not a host-health signal (see project state doc). Software
# encode (libx264) is the expected, accepted path regardless of host.
#
# Output contract (parsed by the offer-loop caller):
#   PREFLIGHT_GPU_MODEL=<nvidia-smi reported name, or "unknown">
#   PREFLIGHT_DRIVER_VERSION=<nvidia-smi reported driver version, or "unknown">
#   PREFLIGHT_VULKAN=PASS|FAIL
#   PREFLIGHT_CUDA_INIT=PASS|FAIL
#   PREFLIGHT_NVDEC=PASS|FAIL
#   PREFLIGHT_RESULT=PASS|FAIL
# Every line above is always printed exactly once, in this order, as the
# LAST six lines of output, regardless of where a check fails, so the
# caller can reliably parse from the tail even if earlier steps error out.

set -uo pipefail

GPU_MODEL="unknown"
DRIVER_VERSION="unknown"
VULKAN_RESULT="FAIL"
CUDA_INIT_RESULT="FAIL"
NVDEC_RESULT="FAIL"

# --- GPU model / driver version (for logging, not a pass/fail check itself) ---
SMI_OUT=$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>&1)
if [ $? -eq 0 ] && [ -n "$SMI_OUT" ]; then
  GPU_MODEL=$(echo "$SMI_OUT" | head -1 | cut -d',' -f1 | sed 's/^ *//;s/ *$//')
  DRIVER_VERSION=$(echo "$SMI_OUT" | head -1 | cut -d',' -f2 | sed 's/^ *//;s/ *$//')
fi
echo "nvidia-smi: model=$GPU_MODEL driver=$DRIVER_VERSION"

# --- Minimal deps for this preflight only (cheap vs. the full CUDA toolkit +
# Rust toolchain + cargo build that a real run needs -- apt install here is
# the acceptable cost of testing before the expensive work, not free, but
# far smaller than what it's gating) ---
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null 2>&1
apt-get install -y -qq --no-install-recommends \
  vulkan-tools mesa-vulkan-drivers ffmpeg python3 ca-certificates wget >/dev/null 2>&1

# --- 1. Permanent EGL ICD fix (same as production script -- GLX frontend
# fails on headless containers per NVIDIA's own docs; confirmed universal
# across every host tested this session) ---
EGL_LIB=$(find /usr/lib/x86_64-linux-gnu /usr/lib -iname "libEGL_nvidia.so.0" 2>/dev/null | head -1)
if [ -n "$EGL_LIB" ]; then
  NVIDIA_EGL_ICD=/tmp/nvidia_egl_icd_preflight.json
  cat > "$NVIDIA_EGL_ICD" <<JSONEOF
{
    "file_format_version" : "1.0.1",
    "ICD": {
        "library_path": "libEGL_nvidia.so.0",
        "api_version" : "1.4.312"
    }
}
JSONEOF
  export VK_DRIVER_FILES="$NVIDIA_EGL_ICD"
  export VK_ICD_FILENAMES="$NVIDIA_EGL_ICD"
  VULKAN_OUT=$(vulkaninfo --summary 2>&1)
  if echo "$VULKAN_OUT" | grep -q 'deviceType.*DISCRETE_GPU'; then
    VULKAN_RESULT="PASS"
  fi
  echo "--- vulkaninfo relevant lines ---"
  echo "$VULKAN_OUT" | grep -iE 'deviceName|deviceType|driverID' || echo "(no device lines found)"
else
  echo "libEGL_nvidia.so.0 not found on this host -- cannot even attempt Vulkan check"
fi

# --- 2. Bare CUDA Driver API (dlopen libcuda.so.1, NOT libcudart) ---
CUDA_PROBE_OUT=$(python3 - <<'PYEOF' 2>&1
import ctypes, sys
try:
    lib = ctypes.CDLL("libcuda.so.1")
except OSError as e:
    print(f"dlopen libcuda.so.1: FAILED -- {e}")
    sys.exit(0)
rc = lib.cuInit(0)
print(f"cuInit(0) -> CUresult {rc}")
if rc != 0:
    sys.exit(0)
count = ctypes.c_int(0)
rc = lib.cuDeviceGetCount(ctypes.byref(count))
print(f"cuDeviceGetCount -> CUresult {rc}, count={count.value}")
PYEOF
)
echo "--- CUDA driver API probe ---"
echo "$CUDA_PROBE_OUT"
if echo "$CUDA_PROBE_OUT" | grep -q 'cuInit(0) -> CUresult 0' && \
   echo "$CUDA_PROBE_OUT" | grep -qE 'cuDeviceGetCount -> CUresult 0, count=[1-9]'; then
  CUDA_INIT_RESULT="PASS"
fi

# --- 3. Minimal NVDEC decode smoke test (self-generated synthetic clip --
# no dependency on GoPro clip download, which hasn't happened yet at this
# point in the workflow) ---
SYNTH_CLIP=/tmp/preflight_synth.mp4
ffmpeg -y -loglevel error -f lavfi -i "testsrc2=size=640x360:rate=30:duration=1" \
  -c:v libx264 -pix_fmt yuv420p "$SYNTH_CLIP" >/dev/null 2>&1
if [ -f "$SYNTH_CLIP" ]; then
  NVDEC_OUT=$(ffmpeg -y -hwaccel cuda -hwaccel_output_format cuda -i "$SYNTH_CLIP" -frames:v 10 -f null - 2>&1)
  echo "--- NVDEC decode smoke test (tail) ---"
  echo "$NVDEC_OUT" | tail -15
  if echo "$NVDEC_OUT" | grep -qE 'frame=\s*10' && ! echo "$NVDEC_OUT" | grep -qiE 'CUDA_ERROR|hwaccel initialisation returned error|No such file'; then
    NVDEC_RESULT="PASS"
  fi
else
  echo "Failed to generate synthetic test clip -- cannot run NVDEC test"
fi

OVERALL="FAIL"
if [ "$VULKAN_RESULT" = "PASS" ] && [ "$CUDA_INIT_RESULT" = "PASS" ] && [ "$NVDEC_RESULT" = "PASS" ]; then
  OVERALL="PASS"
fi

echo "PREFLIGHT_GPU_MODEL=$GPU_MODEL"
echo "PREFLIGHT_DRIVER_VERSION=$DRIVER_VERSION"
echo "PREFLIGHT_VULKAN=$VULKAN_RESULT"
echo "PREFLIGHT_CUDA_INIT=$CUDA_INIT_RESULT"
echo "PREFLIGHT_NVDEC=$NVDEC_RESULT"
echo "PREFLIGHT_RESULT=$OVERALL"
