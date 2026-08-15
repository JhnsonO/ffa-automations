#!/usr/bin/env bash
# OEV -- zero-copy NV12 corruption diagnostic, executed INSIDE a throwaway
# RunPod pod (NOT the production network volume, NOT the baked image).
#
# Purpose: prove the option-2 production primitive with real footage:
# NVDEC -> CUDA-VMM shared VkBuffers -> external semaphore -> Vulkan/wgpu
# buffer-to-ordinary-texture copy. The script checks all four destination
# planes and a byte-exact sentinel, then records a short clean stereo render.
#
# Fresh cargo build (~90s, proven recipe from oev_populate_volume_remote.sh)
# -- deliberately NOT touching /runpod-volume, so this cannot desync the
# production network-volume manifest.
#
# Runs `reco stitch` WITHOUT --no-zero-copy. The first bounded invocation
# enables the existing diagnostics; only after that proof passes does a
# second 180-frame invocation run without sentinel mutation.
#
# Diagnostic only -- no production script touched, no --no-zero-copy
# removed anywhere outside this throwaway script.
#
# Exit codes: 1=env sanity, 2=apt/rust setup, 3=reco-cli build,
# 4=benchmark pack/model missing, 5=calibrate/GPU environment,
# 7=byte-exact buffer-to-texture proof failed, 8=short render failed.

set -uo pipefail
cd /tmp/oev_run || exit 1

RECO_SHA="acdcc61ece2f1d9bda453dea32ec3be10d34172a"
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

# The Reco branch pins the unified wgpu quartet to the semaphore-enabled
# fork. Resolve the lockfile before compiling and hard-gate all four crates.
echo "=== update.log: cargo update -p wgpu --precise 28.0.1 ===" | tee /tmp/oev_run/update.log
cargo update -p wgpu --precise 28.0.1 2>&1 | tee -a /tmp/oev_run/update.log
update_rc=${PIPESTATUS[0]}
if [ "$update_rc" -ne 0 ]; then
  echo "FATAL: cargo update failed (exit $update_rc)" | tee -a /tmp/oev_run/update.log
  exit 3
fi

EXPECTED_WGPU_REV="c8b6f2f00895210857f77f2a10fc1a32a80d5148"
for CRATE in wgpu wgpu-core wgpu-hal wgpu-types; do
  COUNT=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p' Cargo.lock | grep -c '^name =')
  SOURCE=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p && /^source/{print; exit}' Cargo.lock)
  echo "${CRATE}_instance_count=$COUNT" | tee -a /tmp/oev_run/update.log
  echo "${CRATE}_source_line=$SOURCE" | tee -a /tmp/oev_run/update.log
  if [ "$COUNT" -ne 1 ] || ! echo "$SOURCE" | grep -q "$EXPECTED_WGPU_REV"; then
    echo "FATAL: $CRATE is not uniquely pinned to the expected wgpu fork" | tee -a /tmp/oev_run/update.log
    exit 3
  fi
done
echo "WGPU RESOLUTION GATE PASS: $EXPECTED_WGPU_REV" | tee -a /tmp/oev_run/update.log
# Preserve Cargo's exact resolved lockfile for the clean production branch.
# This is an output artifact only; the diagnostic Reco checkout stays on the
# throwaway pod and no production pin is changed here.
cp Cargo.lock /tmp/oev_run/Cargo.lock.resolved

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
  --lookahead 0.1
  --detection-interval 1
  --events events_diag.jsonl
  --width 1920 --height 1080
  --max-frames 5)
echo "reco stitch args (zero-copy path, --no-zero-copy deliberately omitted): ${STITCH_ARGS[*]}" | tee -a stitch.log
RUST_LOG=warn,reco_core=info stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log
stitch_rc=${PIPESTATUS[0]}
echo "timing_render_end=$(ts)" | tee -a timing.log
echo "reco stitch exit code: $stitch_rc (byte-level evidence below remains authoritative; the clean short render must still pass afterward)" | tee -a stitch.log

echo "=== DIAG SUMMARY ===" | tee diag_summary.log
if grep -q "DIAG frame0 Y-plane readback" stitch.log; then
  grep "DIAG frame0 Y-plane readback\|DIAG dumped raw Y plane\|DIAG readback failed" stitch.log | tee -a diag_summary.log
  echo "CUDA-side diagnostic block FIRED." | tee -a diag_summary.log
else
  echo "CUDA-side diagnostic block DID NOT FIRE." | tee -a diag_summary.log
fi

proof_ok=1
if ! grep -q "ZC_BUFFER_COPY: synchronized shared VkBuffer -> normal VramPool textures complete" stitch.log; then
  echo "FAIL: synchronized buffer-to-texture completion marker missing" | tee -a diag_summary.log
  proof_ok=0
fi

DST_LINES=$(grep "ZC_EXP4:.*_vram_dst.*nonzero_bytes" stitch.log || true)
DST_COUNT=$(printf '%s\n' "$DST_LINES" | grep -c "ZC_EXP4:" || true)
echo "destination_readback_count=$DST_COUNT (expected 4)" | tee -a diag_summary.log
printf '%s\n' "$DST_LINES" | tee -a diag_summary.log
if [ "$DST_COUNT" -ne 4 ]; then
  echo "FAIL: did not capture all four normal destination textures" | tee -a diag_summary.log
  proof_ok=0
fi
if printf '%s\n' "$DST_LINES" | grep -qE 'nonzero_bytes=0/'; then
  echo "FAIL: at least one normal destination texture is all-zero" | tee -a diag_summary.log
  proof_ok=0
fi

python3 - <<'PYBUF' | tee -a diag_summary.log
import pathlib
import re

log = pathlib.Path('/tmp/oev_run/stitch.log').read_text(errors='replace')
match = re.search(
    r'ZC_EXP7: wrote 3x1024B sentinel .* byte_offsets=\[(\d+), (\d+), (\d+)\] y_pitch=(\d+) height=(\d+)',
    log,
)
if not match:
    print('BUFFER_TEXTURE_SENTINEL=FAIL (sentinel write log missing)')
    raise SystemExit(1)

offsets = [int(match.group(i)) for i in (1, 2, 3)]
pitch = int(match.group(4))
expected = bytes((j & 0xFF) ^ 0xC3 for j in range(1024))
dump_dir = pathlib.Path('/tmp/oev_run/diag_dump')

def exact(path, tag):
    if not path.exists():
        print(f'{tag}=FAIL (missing {path.name})')
        return False
    data = path.read_bytes()
    ok = True
    for offset in offsets:
        row, col = divmod(offset, pitch)
        tight_offset = row * 3840 + col
        same = data[tight_offset:tight_offset + 1024] == expected
        print(f'{tag} offset={offset} byte_exact={same}')
        ok &= same
    return ok

cuda_ok = exact(dump_dir / 'left_frame0_y_3840x2160.raw', 'CUDA_SENTINEL')
texture_ok = exact(dump_dir / 'left_y_vram_dst_3840x2160.raw', 'NORMAL_TEXTURE_SENTINEL')
print(f'BUFFER_TEXTURE_SENTINEL={"PASS" if cuda_ok and texture_ok else "FAIL"}')
raise SystemExit(0 if cuda_ok and texture_ok else 1)
PYBUF
sentinel_rc=${PIPESTATUS[0]}
if [ "$sentinel_rc" -ne 0 ]; then
  proof_ok=0
fi

if [ "$proof_ok" -eq 1 ]; then
  echo "ZC_BUFFER_TEXTURE_PROOF=PASS" | tee -a diag_summary.log
else
  echo "ZC_BUFFER_TEXTURE_PROOF=FAIL" | tee -a diag_summary.log
  exit 7
fi

# The byte-exact primitive has passed. Continue in the same already-paid-for
# pod with a short, clean stereo render (no diagnostic sentinel mutation) so
# the resulting MP4 can be inspected before any production integration.
unset RECO_DEBUG_DUMP_FRAME
unset RECO_DEBUG_DUMP_DIR
echo "=== visual.log: short stereo render after byte-exact proof ===" | tee visual.log
echo "timing_visual_start=$(ts)" | tee -a timing.log
VISUAL_ARGS=(stitch left.mp4 right.mp4 -c match.json -o followcam_buffer_visual.mp4
  --model "$MODEL_PATH"
  --tracking field
  --panner-preset broadcast
  --lookahead 0.1
  --detection-interval 1
  --events events_buffer_visual.jsonl
  --width 1920 --height 1080
  --max-frames 180)
echo "reco stitch args (short shared-buffer render): ${VISUAL_ARGS[*]}" | tee -a visual.log
RUST_LOG=warn,reco_core=info stdbuf -oL -eL "$RECO_BIN" "${VISUAL_ARGS[@]}" 2>&1 | tee -a visual.log
visual_rc=${PIPESTATUS[0]}
echo "timing_visual_end=$(ts)" | tee -a timing.log
echo "short stereo render exit code: $visual_rc" | tee -a visual.log

if [ "$visual_rc" -ne 0 ] || [ ! -s followcam_buffer_visual.mp4 ]; then
  echo "ZC_BUFFER_SHORT_RENDER=FAIL (rc=$visual_rc output_present=$([ -s followcam_buffer_visual.mp4 ] && echo yes || echo no))" | tee -a diag_summary.log
  exit 8
fi

if command -v ffprobe >/dev/null 2>&1; then
  ffprobe -v error -select_streams v:0 \
    -show_entries stream=codec_name,width,height,nb_frames,duration \
    -of default=noprint_wrappers=1 followcam_buffer_visual.mp4 \
    | tee -a diag_summary.log
fi
grep -E "Processed [0-9]+ frames|GpuResident detection|CUDAExecutionProvider|TensorRT" visual.log \
  | tail -20 | tee -a diag_summary.log || true
echo "ZC_BUFFER_SHORT_RENDER=COMPLETED_VISUAL_REVIEW_REQUIRED" | tee -a diag_summary.log
exit 0
