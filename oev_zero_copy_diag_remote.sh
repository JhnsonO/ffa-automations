#!/usr/bin/env bash
# OEV -- zero-copy NV12 corruption diagnostic, executed INSIDE a throwaway
# RunPod pod (NOT the production network volume, NOT the baked image).
#
# Purpose: prove/disprove whether the CUDA decode -> shared-texture copy
# in reco-io's zero-copy path ever writes real pixel data, independent of
# anything wgpu/Vulkan does with that memory afterward. Uses the
# diag/zero-copy-frame0-readback branch (commit 79667aac, adds ZC_EXP4 Vulkan-side src+dst readback at the real VRAM-pool copy site) of
# JhnsonO/video-stitcher, which adds a single env-gated readback block to
# crates/reco-io/src/zero_copy.rs -- no other behavior change.
#
# Fresh cargo build (~90s, proven recipe from oev_populate_volume_remote.sh)
# -- deliberately NOT touching /runpod-volume, so this cannot desync the
# production network-volume manifest.
#
# Runs `reco stitch` WITHOUT --no-zero-copy (zero-copy path active, the
# same configuration that produced the corrupted followcam.mp4 in run
# 31557269688) with RECO_DEBUG_DUMP_FRAME=1 and RECO_DEBUG_DUMP_DIR set,
# so the decode thread's diagnostic block fires and dumps its readback.
#
# Diagnostic only -- no production script touched, no --no-zero-copy
# removed anywhere outside this throwaway script.
#
# Exit codes: 1=env sanity, 2=apt/rust setup, 3=reco-cli build,
#             4=benchmark pack missing, 5=calibrate/field_roi,
#             6=stitch invocation itself failed to run at all
# (stitch exiting non-zero due to detector-setup issues downstream of
# frame 0 is NOT fatal here -- the diagnostic evidence we need is
# emitted before that point, so this script tees logs either way and
# reports the diagnostic finding regardless of final stitch exit code.)

set -uo pipefail
cd /tmp/oev_run || exit 1

RECO_SHA="024ce664e1abc1b2223e04951f6797cc52c2e85c"
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

echo "=== build.log: apt deps + Rust toolchain (diagnostic branch $RECO_SHA) ===" | tee build.log
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

echo "=== build.log: cargo build --release -p reco-cli --features cuda (diag branch) ===" | tee -a build.log
echo "timing_reco_build_start=$(ts)" | tee -a timing.log
rm -rf /tmp/video-stitcher
git clone "$RECO_REPO" /tmp/video-stitcher 2>&1 | tee -a build.log
cd /tmp/video-stitcher || exit 3
git checkout "$RECO_SHA" 2>&1 | tee -a build.log
echo "video_stitcher_sha=$(git rev-parse HEAD)" | tee -a /tmp/oev_run/build.log

# This commit's Cargo.toml carries a [patch.crates-io] quartet pointing
# wgpu/wgpu-core/wgpu-hal/wgpu-types at the JhnsonO/wgpu fork (ZC_EXP5's
# initial_state backport) -- unlike the diagnostic branch's original base
# commit, which had no fork patch at all. Reusing the exact proven
# lockfile-update + hard resolution gate from
# oev_wgpu_hal_lockfile_update_probe_remote.sh (run 31841623350, PASSED)
# verbatim, so this real-fixture dispatch doesn't burn a full GoPro
# decode+calibrate+stitch cycle on the same already-solved
# crates.io-vs-fork version-split failure mode ticket 1c diagnosed.
echo "=== update.log: cargo update -p wgpu --precise 28.0.1 (lockfile-only, no fork edits) ===" | tee /tmp/oev_run/update.log
cargo update -p wgpu --precise 28.0.1 2>&1 | tee -a /tmp/oev_run/update.log
update_rc=${PIPESTATUS[0]}
if [ "$update_rc" -ne 0 ]; then
  echo "FATAL: cargo update -p wgpu --precise 28.0.1 failed (exit $update_rc)" | tee -a /tmp/oev_run/update.log
  exit 3
fi

EXPECTED_WGPU_REV="c8b6f2f00895210857f77f2a10fc1a32a80d5148"
WGPU_CRATES=("wgpu" "wgpu-core" "wgpu-hal" "wgpu-types")
echo "=== resolution.log: post-update quartet resolution gate (BEFORE any compile) ===" | tee /tmp/oev_run/resolution.log
ALL_UNIFIED=1
for CRATE in "${WGPU_CRATES[@]}"; do
  COUNT=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p' Cargo.lock | grep -c '^name =')
  SOURCE=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p && /^source/{print; exit}' Cargo.lock)
  echo "${CRATE}_instance_count=$COUNT" | tee -a /tmp/oev_run/resolution.log
  echo "${CRATE}_source_line=$SOURCE" | tee -a /tmp/oev_run/resolution.log
  if [ "$COUNT" -ne 1 ]; then
    echo "GATE FAIL: expected exactly 1 $CRATE instance in Cargo.lock, found $COUNT" | tee -a /tmp/oev_run/resolution.log
    ALL_UNIFIED=0
  fi
  if ! echo "$SOURCE" | grep -q "JhnsonO/wgpu" || ! echo "$SOURCE" | grep -q "$EXPECTED_WGPU_REV"; then
    echo "GATE FAIL: $CRATE source line does not reference the expected fork+rev. Got: $SOURCE" | tee -a /tmp/oev_run/resolution.log
    ALL_UNIFIED=0
  fi
done
if [ "$ALL_UNIFIED" -ne 1 ]; then
  echo "GATE: quartet NOT unified after cargo update -- stopping WITHOUT compiling or spending decode/render time. See resolution.log." | tee -a /tmp/oev_run/resolution.log
  exit 3
fi
echo "GATE PASS: all four wgpu-family crates resolve as a single unified instance from JhnsonO/wgpu@$EXPECTED_WGPU_REV." | tee -a /tmp/oev_run/resolution.log

cargo build --release -p reco-cli --features cuda 2>&1 | tee -a build.log
build_rc=${PIPESTATUS[0]}
echo "timing_reco_build_end=$(ts)" | tee -a timing.log
if [ "$build_rc" -ne 0 ]; then
  echo "FATAL: cargo build failed (exit $build_rc)" | tee -a build.log
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

echo "=== stitch.log: reco stitch (ZERO-COPY ACTIVE, no --no-zero-copy, diagnostic readback armed) ===" | tee stitch.log
echo "timing_render_start=$(ts)" | tee -a timing.log
mkdir -p /tmp/oev_run/diag_dump
export RECO_DEBUG_DUMP_FRAME=1
export RECO_DEBUG_DUMP_DIR=/tmp/oev_run/diag_dump
STITCH_ARGS=(stitch left.mp4 right.mp4 -c match.json -o followcam_diag.mp4
  --model "$MODEL_PATH"
  --tracking field
  --panner-preset broadcast
  --lookahead 1.5
  --detection-interval 1
  --events events_diag.jsonl
  --width 1920 --height 1080)
echo "reco stitch args (zero-copy path, --no-zero-copy deliberately omitted): ${STITCH_ARGS[*]}" | tee -a stitch.log
RUST_LOG=warn,reco_core=info stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log
stitch_rc=${PIPESTATUS[0]}
echo "timing_render_end=$(ts)" | tee -a timing.log
echo "reco stitch exit code: $stitch_rc (non-zero is EXPECTED/ACCEPTABLE for this diagnostic -- the readback evidence we need is emitted from the decode thread on frame 0, before/independent of whatever happens later in the job)" | tee -a stitch.log

echo "=== DIAG SUMMARY ===" | tee diag_summary.log
if grep -q "DIAG frame0 Y-plane readback" stitch.log; then
  grep "DIAG frame0 Y-plane readback\|DIAG dumped raw Y plane\|DIAG readback failed" stitch.log | tee -a diag_summary.log
  echo "CUDA-side diagnostic block FIRED (already-known-good baseline)." | tee -a diag_summary.log
else
  echo "CUDA-side diagnostic block DID NOT FIRE -- decode thread never reached frame 0 with RECO_DEBUG_DUMP_FRAME set (check stitch.log for an earlier fatal error, e.g. decoder open failure)." | tee -a diag_summary.log
fi

echo "--- ZC_EXP5: one-time layout transition (should fire 8x, once per shared texture, at setup, before any decode) ---" | tee -a diag_summary.log
ZC_EXP5_COUNT=$(grep -c "ZC_EXP5: transitioned imported VkImage" stitch.log || true)
echo "ZC_EXP5 transition fired ${ZC_EXP5_COUNT}x (expected 8: left/right x Y/UV x double-buffer)" | tee -a diag_summary.log
grep "ZC_EXP5" stitch.log | tee -a diag_summary.log || echo "(no ZC_EXP5 lines found)" | tee -a diag_summary.log

echo "--- ZC_EXP4: Vulkan-side readback -- THE decisive evidence for this experiment ---" | tee -a diag_summary.log
if grep -q "ZC_EXP4:" stitch.log; then
  grep "ZC_EXP4:" stitch.log | tee -a diag_summary.log
  # Decisive check: any *_vram_src line (the CUDA-shared source textures,
  # read via Vulkan's own copy path) with nonzero_bytes=0/... means the
  # hypothesis under test did NOT hold for that texture. Report per-plane,
  # don't collapse to a single pass/fail -- left/right or Y/UV could differ.
  echo "--- per-plane zero/nonzero verdict (source planes only, vram_src) ---" | tee -a diag_summary.log
  grep "ZC_EXP4:.*vram_src.*nonzero_bytes" stitch.log | while read -r line; do
    if echo "$line" | grep -qE "nonzero_bytes=0/"; then
      echo "STILL ZERO: $line" | tee -a diag_summary.log
    else
      echo "NONZERO (hypothesis supported): $line" | tee -a diag_summary.log
    fi
  done
else
  echo "ZC_EXP4 block DID NOT FIRE -- no readback evidence at all for this run. Check stitch.log for a fatal error before frame 0, or confirm RECO_DEBUG_DUMP_FRAME propagated." | tee -a diag_summary.log
fi

echo "=== All stages completed (diagnostic run -- non-zero stitch exit is not itself a failure) ===" | tee -a diag_summary.log
exit 0
