#!/usr/bin/env bash
# GPU-health preflight for RunPod pods running the validated
# runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404 base (or a driver-
# compatible successor -- see the environment contract check).
#
# This is a SEPARATE script from oev_gpu_preflight.sh, not a modification
# of it. oev_gpu_preflight.sh remains the Vast.ai preflight, targeting
# arbitrary marketplace hosts with unpredictable driver/package state and
# CUDA-13-class assumptions (ORT_CUDA_VERSION=13, libcudnn9-cuda-13). This
# script targets one specific, pinned, controlled RunPod base image with
# CUDA-12-class assumptions (ORT_CUDA_VERSION=12, libcudnn9-cuda-12).
# Merging them would either break Vast.ai compatibility or force RunPod to
# inherit Vast-specific host-variance workarounds it doesn't need.
#
# Differences from oev_gpu_preflight.sh, all confirmed necessary against
# the real RunPod base image in the 2026-08-12 validation session:
#   - apt-get install calls prefixed with the caller's own privilege
#     (this base runs as root already; sudo is not assumed present or
#     required -- checked below rather than assumed either way)
#   - vulkaninfo called WITHOUT --summary: confirmed elsewhere that some
#     RunPod-adjacent images ship a vulkan-tools old enough to not support
#     that flag; using plain vulkaninfo + grep works on every version
#     tested and is a strict superset of --summary's output
#   - ORT CUDA EP check pinned to onnxruntime-gpu matching this image's
#     Python (not capped at the CUDA-11-era build the old Kasm desktop
#     image forced due to Python 3.8)
#
# Pass criteria (all four required, same shape as oev_gpu_preflight.sh):
#   1. EGL Vulkan ICD sees a real discrete NVIDIA GPU (not llvmpipe).
#   2. Bare CUDA Driver API: cuInit(0) succeeds, cuDeviceGetCount >= 1.
#   3. Minimal NVDEC decode smoke test succeeds.
#   4. Real ONNX Runtime CUDA execution provider smoke test: a genuine
#      InferenceSession is created and a real inference call executed,
#      with the active provider confirmed CUDA -- not just present in the
#      available-providers list. This distinction has caught real CPU
#      fallback bugs elsewhere in this project (Vast.ai cuda13 cache-bug
#      session) and is not weakened here.
#
# This preflight does NOT accept nvidia-smi GPU utilization alone as an
# acceptance signal for any check -- utilization sampling is useful
# corroborating evidence (used manually in the validation session) but is
# not a pass/fail criterion here, since it says nothing about which
# execution provider is active without the explicit checks above.
#
# NVENC is deliberately NOT tested here, matching oev_gpu_preflight.sh --
# not relevant to the driver-version-conflict class of problem this script
# guards against, and the production stitch flags already accept a
# libx264 software-encode fallback where needed.
#
# Output contract (same shape as oev_gpu_preflight.sh, for tooling reuse):
#   PREFLIGHT_GPU_MODEL=<nvidia-smi reported name, or "unknown">
#   PREFLIGHT_DRIVER_VERSION=<nvidia-smi reported driver version, or "unknown">
#   PREFLIGHT_VULKAN=PASS|FAIL
#   PREFLIGHT_CUDA_INIT=PASS|FAIL
#   PREFLIGHT_NVDEC=PASS|FAIL
#   PREFLIGHT_ORT_CUDA=PASS|FAIL
#   PREFLIGHT_RESULT=PASS|FAIL
# Every line above is always printed exactly once, in this order, as the
# LAST seven lines of output, regardless of where a check fails.

set -uo pipefail

GPU_MODEL="unknown"
DRIVER_VERSION="unknown"
VULKAN_RESULT="FAIL"
CUDA_INIT_RESULT="FAIL"
NVDEC_RESULT="FAIL"
ORT_CUDA_RESULT="FAIL"

# --- Environment contract check: fail loudly, don't silently attempt an
# unvalidated combination. This mirrors runpod_bootstrap.sh's own check so
# the preflight can be run standalone (e.g. before a workflow decides
# whether to proceed to the expensive build/render steps) without having
# run the bootstrap script first. ---
if [ -f /etc/os-release ]; then
  . /etc/os-release
  if [ "${VERSION_ID:-}" != "24.04" ]; then
    echo "FATAL: expected Ubuntu 24.04 (runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404 or driver-compatible successor), found VERSION_ID='${VERSION_ID:-unknown}'. Refusing to run RunPod-specific checks against an unvalidated base -- use oev_gpu_preflight.sh for Vast.ai/other hosts instead."
    echo "PREFLIGHT_GPU_MODEL=$GPU_MODEL"
    echo "PREFLIGHT_DRIVER_VERSION=$DRIVER_VERSION"
    echo "PREFLIGHT_VULKAN=$VULKAN_RESULT"
    echo "PREFLIGHT_CUDA_INIT=$CUDA_INIT_RESULT"
    echo "PREFLIGHT_NVDEC=$NVDEC_RESULT"
    echo "PREFLIGHT_ORT_CUDA=$ORT_CUDA_RESULT"
    echo "PREFLIGHT_RESULT=FAIL"
    exit 1
  fi
else
  echo "FATAL: /etc/os-release not found -- cannot verify base image contract."
  echo "PREFLIGHT_GPU_MODEL=$GPU_MODEL"
  echo "PREFLIGHT_DRIVER_VERSION=$DRIVER_VERSION"
  echo "PREFLIGHT_VULKAN=$VULKAN_RESULT"
  echo "PREFLIGHT_CUDA_INIT=$CUDA_INIT_RESULT"
  echo "PREFLIGHT_NVDEC=$NVDEC_RESULT"
  echo "PREFLIGHT_ORT_CUDA=$ORT_CUDA_RESULT"
  echo "PREFLIGHT_RESULT=FAIL"
  exit 1
fi

# --- GPU model / driver version (logging only) ---
SMI_OUT=$(nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>&1)
if [ $? -eq 0 ] && [ -n "$SMI_OUT" ]; then
  GPU_MODEL=$(echo "$SMI_OUT" | head -1 | cut -d',' -f1 | sed 's/^ *//;s/ *$//')
  DRIVER_VERSION=$(echo "$SMI_OUT" | head -1 | cut -d',' -f2 | sed 's/^ *//;s/ *$//')
fi
echo "nvidia-smi: model=$GPU_MODEL driver=$DRIVER_VERSION"

# --- Privilege detection: this base runs as root by default (confirmed:
# `whoami` -> root in validation session), but don't assume -- use sudo
# only if not already root and sudo exists. ---
AS_ROOT=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    AS_ROOT="sudo"
  else
    echo "WARNING: not root and no sudo available -- package installs below may fail."
  fi
fi

# --- Minimal deps for this preflight only ---
export DEBIAN_FRONTEND=noninteractive
$AS_ROOT apt-get update -qq >/dev/null 2>&1
$AS_ROOT apt-get install -y -qq --no-install-recommends \
  vulkan-tools mesa-vulkan-drivers ffmpeg python3 python3-pip ca-certificates wget >/dev/null 2>&1

# --- 1. EGL ICD Vulkan check. Deliberately does NOT use `vulkaninfo
# --summary` -- confirmed this flag is unsupported on some vulkan-tools
# builds encountered in this project (old versions print full --help and
# exit nonzero when passed an unrecognised flag, which looks identical to
# "no device lines found" if only grepping stdout, silently masking a
# real failure). Plain `vulkaninfo` is a strict superset and works
# everywhere --summary does. ---
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
  VULKAN_OUT=$(env -u DISPLAY vulkaninfo 2>&1)
  if echo "$VULKAN_OUT" | grep -q 'deviceType.*DISCRETE_GPU'; then
    VULKAN_RESULT="PASS"
  fi
  echo "--- vulkaninfo relevant lines ---"
  echo "$VULKAN_OUT" | grep -iE 'deviceName|deviceType|driverID|driverName|driverInfo' || echo "(no device lines found)"
else
  echo "libEGL_nvidia.so.0 not found on this host -- cannot attempt Vulkan check"
fi

# --- 2. Bare CUDA Driver API ---
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

# --- 3. Minimal NVDEC decode smoke test ---
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

# --- 4. Real ONNX Runtime CUDA EP smoke test.
#
# This RunPod environment's contract is fixed at CUDA 12 / ORT_CUDA_VERSION=12
# (see runpod_bootstrap.sh and the environment contract check above -- this
# script only runs at all on the validated Ubuntu 24.04 base). The pin
# below is therefore DETERMINISTIC, not inferred from driver text.
#
# Bug fixed here (2026-08-12 correction): the previous version of this
# script tried to extract a "host CUDA version" by regexing digits out of
# $SMI_OUT -- but $SMI_OUT only ever contains `name,driver_version`
# (see the nvidia-smi query above: --query-gpu=name,driver_version), it
# has NEVER contained a CUDA version field. That regex was silently
# parsing the DRIVER number instead (e.g. a driver "580.159.04" could get
# read as CUDA major "580", wrongly forcing the >=13 unpinned-package
# branch). Never infer ORT's required CUDA build from driver version text.
ORT_GPU_PIN="onnxruntime-gpu==1.20.2"
echo "ORT package pin: $ORT_GPU_PIN (fixed -- this preflight's environment contract is CUDA 12 / ORT_CUDA_VERSION=12, not inferred from driver text)"

# Diagnostics only, never used for the pin decision above: parse the
# actual "CUDA Version:" field from nvidia-smi's own header output (a
# different field entirely from driver_version -- this is the max CUDA
# API the driver advertises support for, which nvidia-smi does print but
# --query-gpu=driver_version does not expose).
HOST_CUDA_API_DIAG=$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' | head -1)
echo "Diagnostic only (not used for pin decision): ${HOST_CUDA_API_DIAG:-not found in nvidia-smi output}"
pip3 install -q --no-input onnx "$ORT_GPU_PIN" >/tmp/preflight_ort_install.log 2>&1
ORT_OUT=$(python3 - <<'PYEOF' 2>&1
import sys
try:
    import onnx
    from onnx import helper, TensorProto
except Exception as e:
    print(f"FATAL: onnx import failed: {e}")
    sys.exit(0)
try:
    import onnxruntime as ort
except Exception as e:
    print(f"FATAL: onnxruntime-gpu import failed: {e}")
    sys.exit(0)

x = helper.make_tensor_value_info('x', TensorProto.FLOAT, [1, 4])
y = helper.make_tensor_value_info('y', TensorProto.FLOAT, [1, 4])
add_node = helper.make_node('Add', ['x', 'x'], ['sum'])
relu_node = helper.make_node('Relu', ['sum'], ['y'])
graph = helper.make_graph([add_node, relu_node], 'preflight_smoke', [x], [y])
model = helper.make_model(graph, producer_name='runpod_gpu_preflight')
model.opset_import[0].version = 13
model.ir_version = 9
onnx.checker.check_model(model)
model_path = '/tmp/preflight_smoke.onnx'
onnx.save(model, model_path)

if 'CUDAExecutionProvider' not in ort.get_available_providers():
    print(f"FATAL: CUDAExecutionProvider not in available providers: {ort.get_available_providers()}")
    sys.exit(0)

try:
    sess = ort.InferenceSession(model_path, providers=['CUDAExecutionProvider'])
except Exception as e:
    print(f"FATAL: CUDAExecutionProvider session creation raised: {e}")
    sys.exit(0)

active = sess.get_providers()
if not active or active[0] != 'CUDAExecutionProvider':
    print(f"FATAL: session did not register CUDAExecutionProvider as active (got {active}) -- would silently fall back to CPU")
    sys.exit(0)

import numpy as np
try:
    out = sess.run(None, {'x': np.ones((1, 4), dtype=np.float32)})
except Exception as e:
    print(f"FATAL: real inference execution on CUDA EP raised: {e}")
    sys.exit(0)

print(f"ORT_CUDA_SMOKE_TEST PASSED -- active_providers={active} output={out[0].tolist()}")
PYEOF
)
echo "--- ORT CUDA EP smoke test ---"
cat /tmp/preflight_ort_install.log 2>/dev/null | tail -5
echo "$ORT_OUT"
if echo "$ORT_OUT" | grep -q '^ORT_CUDA_SMOKE_TEST PASSED'; then
  ORT_CUDA_RESULT="PASS"
fi

OVERALL="FAIL"
if [ "$VULKAN_RESULT" = "PASS" ] && [ "$CUDA_INIT_RESULT" = "PASS" ] && [ "$NVDEC_RESULT" = "PASS" ] && [ "$ORT_CUDA_RESULT" = "PASS" ]; then
  OVERALL="PASS"
fi

echo "PREFLIGHT_GPU_MODEL=$GPU_MODEL"
echo "PREFLIGHT_DRIVER_VERSION=$DRIVER_VERSION"
echo "PREFLIGHT_VULKAN=$VULKAN_RESULT"
echo "PREFLIGHT_CUDA_INIT=$CUDA_INIT_RESULT"
echo "PREFLIGHT_NVDEC=$NVDEC_RESULT"
echo "PREFLIGHT_ORT_CUDA=$ORT_CUDA_RESULT"
echo "PREFLIGHT_RESULT=$OVERALL"

if [ "$OVERALL" != "PASS" ]; then
  exit 1
fi
exit 0
