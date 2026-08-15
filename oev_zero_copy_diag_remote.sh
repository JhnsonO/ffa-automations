#!/usr/bin/env bash
# OEV -- bounded --no-zero-copy teardown regression control, executed
# INSIDE a throwaway RunPod pod (NOT the production volume binary/image).
#
# Purpose: compare the post-output --no-zero-copy teardown behavior of the
# clean option-2 branch against its exact production base on one GPU host.
# Runtime run 31893154739 already completed all 180 frames for both the new
# path and --no-zero-copy, but the latter segfaulted during CUDA teardown.
#
# Fresh cargo build (~90s, proven recipe from oev_populate_volume_remote.sh)
# -- deliberately NOT touching /runpod-volume, so this cannot desync the
# production network-volume manifest.
#
# Runs the identical 180-frame --no-zero-copy workload first at the clean
# branch SHA and then at the production base SHA. Both outputs must be complete
# before exit-code/fingerprint comparison is accepted as regression evidence.
#
# Diagnostic only -- no production script touched, no --no-zero-copy
# removed anywhere outside this throwaway script.
#
# Exit codes: 1=env sanity, 2=apt/rust setup, 3=clean reco build,
# 4=benchmark pack/model missing, 5=calibrate/GPU environment,
# 12=production-base build, 13=branch regression, 14=inconclusive control.

set -uo pipefail
cd /tmp/oev_run || exit 1
RUN_DIR="/tmp/oev_run"

RECO_SHA="61aced9687d2c441c48d11e29d3aa28df18b3beb"
RECO_BASE="53fe10f548d5767ad94ef66aeaedf2d8c7161f27"
RECO_REPO="https://github.com/JhnsonO/video-stitcher"
MODEL_PATH="/runpod-volume/oev-runtime/models/yolo26m.onnx"

ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }

[ -f left.mp4 ] || { echo "FATAL: left.mp4 (benchmark pack) not present" | tee -a timing.log; exit 4; }
[ -f right.mp4 ] || { echo "FATAL: right.mp4 (benchmark pack) not present" | tee -a timing.log; exit 4; }
# Read-only reuse of the already-populated EU-RO-1 network volume for the
# YOLO model only. This script never writes to /runpod-volume -- the
# volume's reco binary and manifest are untouched; only its model file is
# read, so the production volume state cannot be affected by this run.
[ -f "$MODEL_PATH" ] || { echo "FATAL: $MODEL_PATH not found -- expected network volume mounted read access to existing populated model" | tee -a timing.log; exit 4; }

echo "=== build.log: apt deps + Rust toolchain (clean branch $RECO_SHA) ===" | tee build.log
echo "timing_setup_start=$(ts)" | tee -a timing.log
apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    git curl build-essential pkg-config cmake \
    clang libclang-dev \
    libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libavfilter-dev \
    ca-certificates 2>&1 | tee -a build.log
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "FATAL: apt-get install failed" | tee -a build.log
  exit 2
fi

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y 2>&1 | tee -a build.log
export PATH="/root/.cargo/bin:${PATH}"
rustc --version 2>&1 | tee -a build.log
echo "timing_setup_end=$(ts)" | tee -a timing.log

echo "=== build.log: cargo build --release --locked -p reco-cli --features cuda ===" | tee -a build.log
echo "timing_reco_build_start=$(ts)" | tee -a timing.log
rm -rf /tmp/video-stitcher
git clone "$RECO_REPO" /tmp/video-stitcher 2>&1 | tee -a build.log
cd /tmp/video-stitcher || exit 3
git checkout "$RECO_SHA" 2>&1 | tee -a "$RUN_DIR/build.log"
echo "video_stitcher_sha=$(git rev-parse HEAD)" | tee -a "$RUN_DIR/build.log"

# The clean branch commits the exact unified semaphore-enabled wgpu
# resolution. Inspect it without mutating the lockfile.
echo "=== update.log: locked wgpu resolution ===" | tee "$RUN_DIR/update.log"
EXPECTED_WGPU_REV="d74e00f2415e55c0f09a87b0497d66d8192a44bb"
for CRATE in wgpu wgpu-core wgpu-hal wgpu-types; do
  COUNT=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p' Cargo.lock | grep -c '^name =')
  SOURCE=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p && /^source/{print; exit}' Cargo.lock)
  echo "${CRATE}_instance_count=$COUNT" | tee -a "$RUN_DIR/update.log"
  echo "${CRATE}_source_line=$SOURCE" | tee -a "$RUN_DIR/update.log"
  if [ "$COUNT" -ne 1 ] || ! echo "$SOURCE" | grep -q "$EXPECTED_WGPU_REV"; then
    echo "FATAL: $CRATE is not uniquely pinned to the expected wgpu fork" | tee -a "$RUN_DIR/update.log"
    exit 3
  fi
done
echo "WGPU RESOLUTION GATE PASS: $EXPECTED_WGPU_REV" | tee -a "$RUN_DIR/update.log"
cp Cargo.lock "$RUN_DIR/Cargo.lock.resolved"

cargo build --release --locked -p reco-cli --features cuda 2>&1 | tee -a "$RUN_DIR/build.log"
build_rc=${PIPESTATUS[0]}
echo "timing_reco_build_end=$(ts)" | tee -a timing.log
if [ "$build_rc" -ne 0 ]; then
  echo "FATAL: cargo build failed (exit $build_rc)" | tee -a "$RUN_DIR/build.log"
  exit 3
fi

RECO_BIN="/tmp/video-stitcher/target/release/reco"
"$RECO_BIN" --version 2>&1 | tee /tmp/oev_run/reco_version.txt
cd /tmp/oev_run || exit 1

echo "=== gpu_env.log: NVIDIA Vulkan runtime reconstruction (harness fix, cycle 2) ===" | tee gpu_env.log
# Reuses the exact proven mechanism from runpod_bootstrap.sh (frozen script)
# -- do not hand-roll a new ICD setup. This diagnostic script previously
# skipped this step entirely, which let wgpu/Vulkan silently fall back to
# llvmpipe (software) while CUDA allocated from the real NVIDIA GPU --
# root cause of cycle 1's ERROR_INVALID_EXTERNAL_HANDLE, not a genuine
# CUDA/Vulkan external-handle bug.
CUDA_LIB_DIR=$(find /usr/local -maxdepth 1 -type d -name "cuda-12.*" 2>/dev/null | sort -V | tail -1)
if [ -z "$CUDA_LIB_DIR" ]; then
  echo "FATAL: no /usr/local/cuda-12.* dir found" | tee -a gpu_env.log
  exit 5
fi
export LD_LIBRARY_PATH="${CUDA_LIB_DIR}/lib64:${LD_LIBRARY_PATH:-}"
echo "CUDA lib dir: $CUDA_LIB_DIR" | tee -a gpu_env.log

EGL_ICD_PATH="/tmp/nvidia_egl_icd.json"
if [ -f "$EGL_ICD_PATH" ]; then
  echo "$EGL_ICD_PATH already present, reusing" | tee -a gpu_env.log
else
  EGL_LIB=$(find /usr/lib/x86_64-linux-gnu /usr/lib -iname "libEGL_nvidia.so.0" 2>/dev/null | head -1)
  if [ -z "$EGL_LIB" ]; then
    echo "FATAL: libEGL_nvidia.so.0 not found -- cannot construct Vulkan ICD override. NVIDIA driver may not be properly passed into this container." | tee -a gpu_env.log
    exit 5
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
  echo "EGL ICD override written to $EGL_ICD_PATH" | tee -a gpu_env.log
fi
export VK_DRIVER_FILES="$EGL_ICD_PATH"
export VK_ICD_FILENAMES="$EGL_ICD_PATH"
unset DISPLAY

VULKAN_CHECK=$(env -u DISPLAY vulkaninfo 2>&1 | grep -iE 'deviceName|deviceType' | head -4)
echo "$VULKAN_CHECK" | tee -a gpu_env.log
if ! grep -q 'DISCRETE_GPU' <<< "$VULKAN_CHECK"; then
  echo "FATAL: Vulkan check after EGL ICD override did not report a DISCRETE_GPU device -- aborting before calibrate/stitch. Output: $VULKAN_CHECK" | tee -a gpu_env.log
  exit 5
fi
echo "Vulkan confirmed NVIDIA discrete GPU: $(echo "$VULKAN_CHECK" | tr '\n' ' ')" | tee -a gpu_env.log

echo "=== calibrate.log: reco calibrate ===" | tee calibrate.log
echo "timing_calibrate_start=$(ts)" | tee -a timing.log
LENS_PROFILE_URL="https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json"
curl -fsSL "$LENS_PROFILE_URL" -o hero10_wide_16by9.json
if [ ! -s hero10_wide_16by9.json ]; then
  echo "FATAL: failed to download lens profile from $LENS_PROFILE_URL" | tee -a calibrate.log
  exit 5
fi
stdbuf -oL -eL "$RECO_BIN" calibrate left.mp4 right.mp4 \
  --left-profile hero10_wide_16by9.json \
  --right-profile hero10_wide_16by9.json \
  -o match.json 2>&1 | tee -a calibrate.log
calibrate_rc=${PIPESTATUS[0]}
if [ "$calibrate_rc" -ne 0 ] || [ ! -f match.json ]; then
  echo "FATAL: reco calibrate failed (exit $calibrate_rc) or match.json missing" | tee -a calibrate.log
  exit 5
fi
echo "Calibrate OK: match.json written" | tee -a calibrate.log

if grep -qi "Selected GPU: llvmpipe" calibrate.log; then
  echo "FATAL: reco calibrate selected llvmpipe (software Vulkan), not the NVIDIA GPU -- aborting before stitch. Any zero-copy result under this condition is not valid evidence for the CUDA/Vulkan import hypothesis." | tee -a calibrate.log
  exit 5
fi
if ! grep -qi "Selected GPU:" calibrate.log; then
  echo "FATAL: could not find a \"Selected GPU:\" line in calibrate.log -- cannot confirm which Vulkan device reco used, aborting rather than guessing." | tee -a calibrate.log
  exit 5
fi
echo "Vulkan device gate: $(grep -i 'Selected GPU:' calibrate.log | head -1)" | tee -a calibrate.log

# Same St Margaret's field ROI as production, reproduced verbatim from
# oev_test_runtime_benchmark_remote.sh -- do not re-derive. Not load-bearing
# for this diagnostic (tracking accuracy is irrelevant here) but keeps the
# invocation identical to the real production shape.
python3 - <<'PYROI'
import json

with open("match.json") as f:
    match = json.load(f)

match["field_roi"] = {
    "left": [
        [0.1227, 0.9611], [0.0573, 0.6846], [0.1802, 0.6285],
        [0.2645, 0.5769], [0.4382, 0.4864], [0.4988, 0.4658],
        [0.5942, 0.4474], [0.7835, 0.4175], [0.9285, 0.3785],
        [1.0000, 1.0000], [0.1227, 1.0000],
    ],
    "right": [
        [0.0391, 0.4206], [0.0818, 0.4101], [0.1839, 0.4070],
        [0.2783, 0.4070], [0.3448, 0.4083], [0.4100, 0.4161],
        [0.4684, 0.4319], [0.6239, 0.4801], [0.7368, 0.5200],
        [0.7980, 0.5465], [0.7454, 0.9011], [0.7454, 1.0000],
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
  exit 5
fi
echo "timing_calibrate_end=$(ts)" | tee -a timing.log

LOG_FILTER="warn,reco_core=info,reco_autocam=info,reco_detect=info"
echo "=== NO-ZERO-COPY TEARDOWN REGRESSION CONTROL ===" | tee diag_summary.log

validate_complete_output() {
  local output="$1"
  local log_file="$2"
  [ -s "$output" ] || return 1
  grep -q "Done: 180 frames" "$log_file" || return 1
  grep -q "Frames:    180 processed" "$log_file" || return 1
  local frames
  frames=$(ffprobe -v error -select_streams v:0 -show_entries stream=nb_frames \
    -of default=noprint_wrappers=1:nokey=1 "$output")
  [ "$frames" = "180" ]
}

# First reproduce on the clean option-2 branch. This is deliberately the same
# workload that completed before rc=139 in runtime attempt 31893154739.
echo "=== branch_nozero.log: clean branch --no-zero-copy ===" | tee branch_nozero.log
echo "timing_branch_nozero_start=$(ts)" | tee -a timing.log
BRANCH_ARGS=(stitch left.mp4 right.mp4 -c match.json -o followcam_branch_nozero.mp4
  --model "$MODEL_PATH"
  --tracking field
  --panner-preset broadcast
  --lookahead 0.1
  --detection-interval 1
  --events events_branch_nozero.jsonl
  --no-zero-copy
  --width 1920 --height 1080
  --max-frames 180)
RUST_LOG="$LOG_FILTER" stdbuf -oL -eL "$RECO_BIN" "${BRANCH_ARGS[@]}" 2>&1 | tee -a branch_nozero.log
branch_rc=${PIPESTATUS[0]}
echo "timing_branch_nozero_end=$(ts)" | tee -a timing.log
if validate_complete_output followcam_branch_nozero.mp4 branch_nozero.log; then
  echo "NO_ZERO_COPY_CLEAN_COMPLETE=PASS" | tee -a diag_summary.log
else
  echo "NO_ZERO_COPY_CLEAN_COMPLETE=FAIL (rc=$branch_rc)" | tee -a diag_summary.log
  exit 14
fi

# Build the unchanged production base on the same pod, with the same global
# Cargo cache but a separate source/target tree. No production ref is mutated.
echo "=== base_build.log: production base $RECO_BASE ===" | tee base_build.log
echo "timing_base_build_start=$(ts)" | tee -a timing.log
rm -rf /tmp/video-stitcher-base
git clone "$RECO_REPO" /tmp/video-stitcher-base 2>&1 | tee -a base_build.log
cd /tmp/video-stitcher-base || exit 12
git checkout "$RECO_BASE" 2>&1 | tee -a "$RUN_DIR/base_build.log"
test "$(git rev-parse HEAD)" = "$RECO_BASE" || exit 12
cargo build --release --locked -p reco-cli --features cuda 2>&1 | tee -a "$RUN_DIR/base_build.log"
base_build_rc=${PIPESTATUS[0]}
echo "timing_base_build_end=$(ts)" | tee -a "$RUN_DIR/timing.log"
if [ "$base_build_rc" -ne 0 ]; then
  echo "NO_ZERO_COPY_BASE_BUILD=FAIL (rc=$base_build_rc)" | tee -a "$RUN_DIR/diag_summary.log"
  exit 12
fi
echo "NO_ZERO_COPY_BASE_BUILD=PASS" | tee -a "$RUN_DIR/diag_summary.log"

BASE_BIN="/tmp/video-stitcher-base/target/release/reco"
cd "$RUN_DIR" || exit 1
echo "=== base_nozero.log: production base --no-zero-copy ===" | tee base_nozero.log
echo "timing_base_nozero_start=$(ts)" | tee -a timing.log
BASE_ARGS=(stitch left.mp4 right.mp4 -c match.json -o followcam_base_nozero.mp4
  --model "$MODEL_PATH"
  --tracking field
  --panner-preset broadcast
  --lookahead 0.1
  --detection-interval 1
  --events events_base_nozero.jsonl
  --no-zero-copy
  --width 1920 --height 1080
  --max-frames 180)
RUST_LOG="$LOG_FILTER" stdbuf -oL -eL "$BASE_BIN" "${BASE_ARGS[@]}" 2>&1 | tee -a base_nozero.log
base_rc=${PIPESTATUS[0]}
echo "timing_base_nozero_end=$(ts)" | tee -a timing.log
if validate_complete_output followcam_base_nozero.mp4 base_nozero.log; then
  echo "NO_ZERO_COPY_BASE_COMPLETE=PASS" | tee -a diag_summary.log
else
  echo "NO_ZERO_COPY_BASE_COMPLETE=FAIL (rc=$base_rc)" | tee -a diag_summary.log
  exit 14
fi

{
  echo "clean_branch_rc=$branch_rc"
  echo "production_base_rc=$base_rc"
  echo "--- CLEAN BRANCH SESSION SUMMARY ---"
  grep -A30 -- "--- Session Summary ---" branch_nozero.log | tail -31
  echo "--- PRODUCTION BASE SESSION SUMMARY ---"
  grep -A30 -- "--- Session Summary ---" base_nozero.log | tail -31
  echo "--- CLEAN BRANCH TEARDOWN ---"
  tail -12 branch_nozero.log
  echo "--- PRODUCTION BASE TEARDOWN ---"
  tail -12 base_nozero.log
} | tee -a diag_summary.log

if [ "$branch_rc" -eq 0 ]; then
  echo "NO_ZERO_COPY_REGRESSION_CONTROL=PASS (clean branch exited normally)" | tee -a diag_summary.log
  exit 0
fi

if [ "$base_rc" -eq "$branch_rc" ] \
  && grep -q "cuCtxPopCurrent" branch_nozero.log \
  && grep -q "cuCtxPopCurrent" base_nozero.log; then
  echo "NO_ZERO_COPY_REGRESSION_CONTROL=PASS (same rc/fingerprint on production base)" | tee -a diag_summary.log
  exit 0
fi

if [ "$base_rc" -eq 0 ]; then
  echo "NO_ZERO_COPY_REGRESSION_CONTROL=FAIL (clean branch regression)" | tee -a diag_summary.log
  exit 13
fi

echo "NO_ZERO_COPY_REGRESSION_CONTROL=INCONCLUSIVE (different teardown failures)" | tee -a diag_summary.log
exit 14
