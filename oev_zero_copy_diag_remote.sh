#!/usr/bin/env bash
# OEV EXP7 — external-memory import semantics diagnostic.
# Diagnostic branch only. Reconstructs the exact compile-proven EXP7 source,
# builds it on a throwaway RunPod pod, then runs only far enough to collect:
#   A) exact external IMAGE capability flags,
#   B) VkMemoryDedicatedRequirements,
#   C) CUDA-VMM -> Vulkan VkBuffer OPAQUE_FD sentinel alias control.
# ZC_EXP7_ABORT_AFTER=2 exits before real decode/render work once Y+UV shared
# textures for the first source have been probed.
set -uo pipefail
cd /tmp/oev_run || exit 1

RECO_REPO="https://github.com/JhnsonO/video-stitcher"
RECO_SHA="e810a04ee29452b3cd6647cc98875033a2e0d1a0"
FFA_REPO="https://github.com/JhnsonO/ffa-automations"
FFA_SHA="6cdbb96c9fd6a244860b9e1e8fb4b35ee4c0121a"
EXPECTED_WGPU_REV="c8b6f2f00895210857f77f2a10fc1a32a80d5148"
EXPECTED_VULKAN_SHA256="4da792382d954f5ffe68865d5ae84db9e778c2f6e452917a7749149f50c41089"
EXPECTED_CUDA_SHA256="e81e693071608caef213eb34e68335d064e3aded6c58214147f2b6be4ac303b7"
MODEL_PATH="/runpod-volume/oev-runtime/models/yolo26m.onnx"

ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
: > timing.log

[ -s left.mp4 ] || { echo "FATAL: left.mp4 missing"; exit 4; }
[ -s right.mp4 ] || { echo "FATAL: right.mp4 missing"; exit 4; }
[ -s "$MODEL_PATH" ] || { echo "FATAL: model missing: $MODEL_PATH"; exit 4; }

echo "=== EXP7 build/setup ===" | tee build.log
echo "timing_setup_start=$(ts)" | tee -a timing.log
apt-get update -qq && apt-get install -y -qq --no-install-recommends \
  git curl build-essential pkg-config cmake clang libclang-dev \
  libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libavfilter-dev \
  ca-certificates 2>&1 | tee -a build.log
if [ "${PIPESTATUS[0]}" -ne 0 ]; then exit 2; fi
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y 2>&1 | tee -a build.log
export PATH="/root/.cargo/bin:${PATH}"
rustc --version | tee -a build.log
echo "timing_setup_end=$(ts)" | tee -a timing.log

echo "=== Reconstruct exact compile-proven EXP7 source ===" | tee -a build.log
rm -rf /tmp/video-stitcher /tmp/ffa-exp7
git clone -q "$RECO_REPO" /tmp/video-stitcher || exit 3
git -C /tmp/video-stitcher checkout -q "$RECO_SHA" || exit 3
git clone -q "$FFA_REPO" /tmp/ffa-exp7 || exit 3
git -C /tmp/ffa-exp7 checkout -q "$FFA_SHA" || exit 3

PAYLOAD=/tmp/ffa-exp7/exp7_payload
VULKAN=/tmp/video-stitcher/crates/reco-core/src/interop/vulkan.rs
CUDA=/tmp/video-stitcher/crates/reco-core/src/interop/cuda.rs
cat "$PAYLOAD"/vulkan.part00 "$PAYLOAD"/vulkan.part01 "$PAYLOAD"/vulkan.part02 \
    "$PAYLOAD"/vulkan.part03 "$PAYLOAD"/vulkan.part04 > "$VULKAN"
python3 - "$CUDA" "$PAYLOAD/cuda_destroy_wrapper.txt" <<'PY'
from pathlib import Path
import sys
cuda = Path(sys.argv[1]); wrapper = Path(sys.argv[2]).read_text(); s = cuda.read_text()
marker = '/// Query (free, total) device memory in bytes via the CUDA driver.\n'
if 'pub fn cuda_destroy_external_semaphore(' in s:
    raise SystemExit('FATAL: wrapper already present')
if s.count(marker) != 1:
    raise SystemExit(f'FATAL: insertion marker count={s.count(marker)}')
cuda.write_text(s.replace(marker, wrapper + marker, 1))
PY

VULKAN_SHA=$(sha256sum "$VULKAN" | awk '{print $1}')
CUDA_SHA=$(sha256sum "$CUDA" | awk '{print $1}')
VULKAN_LINES=$(wc -l < "$VULKAN")
CUDA_LINES=$(wc -l < "$CUDA")
{
  echo "reco_sha=$(git -C /tmp/video-stitcher rev-parse HEAD)"
  echo "ffa_payload_sha=$(git -C /tmp/ffa-exp7 rev-parse HEAD)"
  echo "vulkan_lines=$VULKAN_LINES vulkan_sha256=$VULKAN_SHA"
  echo "cuda_lines=$CUDA_LINES cuda_sha256=$CUDA_SHA"
} | tee -a build.log
[ "$VULKAN_LINES" -eq 1476 ] || { echo "FATAL: vulkan line count"; exit 3; }
[ "$VULKAN_SHA" = "$EXPECTED_VULKAN_SHA256" ] || { echo "FATAL: vulkan hash mismatch"; exit 3; }
[ "$CUDA_SHA" = "$EXPECTED_CUDA_SHA256" ] || { echo "FATAL: cuda hash mismatch"; exit 3; }
if grep -Eq 'zc_exp7_fd_memory_type_bits|ZC_EXP7_FD_IMG|eligible_bits|fd_bits|get_memory_fd_properties' "$VULKAN"; then
  echo "FATAL: forbidden OPAQUE_FD fd-properties probe present"; exit 3
fi
echo "PAYLOAD GATE PASS" | tee -a build.log

cd /tmp/video-stitcher || exit 3
echo "timing_cargo_update_start=$(ts)" | tee -a /tmp/oev_run/timing.log
cargo update -p wgpu --precise 28.0.1 2>&1 | tee -a /tmp/oev_run/build.log
if [ "${PIPESTATUS[0]}" -ne 0 ]; then exit 3; fi
echo "timing_cargo_update_end=$(ts)" | tee -a /tmp/oev_run/timing.log
COUNT=$(grep -c "$EXPECTED_WGPU_REV" Cargo.lock || true)
[ "$COUNT" -ge 4 ] || { echo "FATAL: wgpu quartet not pinned (count=$COUNT)" | tee -a /tmp/oev_run/build.log; exit 3; }
echo "RESOLUTION GATE PASS: expected wgpu rev occurrences=$COUNT" | tee -a /tmp/oev_run/build.log

echo "timing_reco_build_start=$(ts)" | tee -a /tmp/oev_run/timing.log
cargo build --release -p reco-cli --features cuda 2>&1 | tee -a /tmp/oev_run/build.log
BUILD_RC=${PIPESTATUS[0]}
echo "timing_reco_build_end=$(ts)" | tee -a /tmp/oev_run/timing.log
[ "$BUILD_RC" -eq 0 ] || { echo "FATAL: cargo build exit $BUILD_RC"; exit 3; }
RECO_BIN=/tmp/video-stitcher/target/release/reco
"$RECO_BIN" --version 2>&1 | tee /tmp/oev_run/reco_version.txt
cd /tmp/oev_run || exit 1

# Proven NVIDIA Vulkan ICD reconstruction from the earlier zero-copy harness.
echo "=== NVIDIA Vulkan device gate ===" | tee gpu_env.log
CUDA_LIB_DIR=$(find /usr/local -maxdepth 1 -type d -name 'cuda-12.*' 2>/dev/null | sort -V | tail -1)
[ -n "$CUDA_LIB_DIR" ] || { echo "FATAL: CUDA lib dir missing" | tee -a gpu_env.log; exit 5; }
export LD_LIBRARY_PATH="${CUDA_LIB_DIR}/lib64:${LD_LIBRARY_PATH:-}"
EGL_ICD_PATH=/tmp/nvidia_egl_icd.json
cat > "$EGL_ICD_PATH" <<'JSONEOF'
{
  "file_format_version" : "1.0.1",
  "ICD": { "library_path": "libEGL_nvidia.so.0", "api_version" : "1.4.312" }
}
JSONEOF
export VK_DRIVER_FILES="$EGL_ICD_PATH"
export VK_ICD_FILENAMES="$EGL_ICD_PATH"
unset DISPLAY
VULKAN_CHECK=$(env -u DISPLAY vulkaninfo 2>&1 | grep -iE 'deviceName|deviceType' | head -4)
echo "$VULKAN_CHECK" | tee -a gpu_env.log
if ! grep -q 'DISCRETE_GPU' <<< "$VULKAN_CHECK"; then
  echo "FATAL: discrete NVIDIA Vulkan GPU not confirmed" | tee -a gpu_env.log; exit 5
fi

# Calibrate only to produce a valid match.json for the standard stitch entrypoint.
echo "=== calibrate ===" | tee calibrate.log
LENS_PROFILE_URL='https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json'
curl -fsSL "$LENS_PROFILE_URL" -o hero10_wide_16by9.json || exit 5
RUST_LOG=warn,reco_core=info stdbuf -oL -eL "$RECO_BIN" calibrate left.mp4 right.mp4 \
  --left-profile hero10_wide_16by9.json --right-profile hero10_wide_16by9.json \
  -o match.json 2>&1 | tee -a calibrate.log
CAL_RC=${PIPESTATUS[0]}
[ "$CAL_RC" -eq 0 ] && [ -s match.json ] || { echo "FATAL: calibrate failed"; exit 5; }
if grep -qi 'Selected GPU: llvmpipe' calibrate.log; then echo "FATAL: llvmpipe"; exit 5; fi
if ! grep -qi 'Selected GPU:' calibrate.log; then echo "FATAL: no Selected GPU line"; exit 5; fi

python3 - <<'PYROI'
import json
p='match.json'; m=json.load(open(p))
m['field_roi']={
 'left':[[0.1227,0.9611],[0.0573,0.6846],[0.1802,0.6285],[0.2645,0.5769],[0.4382,0.4864],[0.4988,0.4658],[0.5942,0.4474],[0.7835,0.4175],[0.9285,0.3785],[1.0,1.0],[0.1227,1.0]],
 'right':[[0.0391,0.4206],[0.0818,0.4101],[0.1839,0.4070],[0.2783,0.4070],[0.3448,0.4083],[0.4100,0.4161],[0.4684,0.4319],[0.6239,0.4801],[0.7368,0.5200],[0.7980,0.5465],[0.7454,0.9011],[0.7454,1.0],[0.0,1.0]]}
json.dump(m,open(p,'w'),indent=2)
PYROI

# EXP7 itself. The abort gate intentionally exits after the first source's
# Y+UV shared textures are created, before frame decode/render/encode.
echo "=== EXP7 hardware diagnostic ===" | tee stitch.log
export ZC_EXP7=1
export ZC_EXP7_ABORT_AFTER=2
STITCH_ARGS=(stitch left.mp4 right.mp4 -c match.json -o followcam_diag.mp4
  --model "$MODEL_PATH" --tracking field --panner-preset broadcast --lookahead 1.5
  --detection-interval 1 --events events_diag.jsonl --width 1920 --height 1080)
echo "ZC_EXP7=1 ZC_EXP7_ABORT_AFTER=2 ${STITCH_ARGS[*]}" | tee -a stitch.log
echo "timing_exp7_start=$(ts)" | tee -a timing.log
RUST_LOG=warn,reco_core=info stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log
STITCH_RC=${PIPESTATUS[0]}
echo "timing_exp7_end=$(ts)" | tee -a timing.log
echo "EXP7 process exit=$STITCH_RC" | tee -a stitch.log

# Evidence gate. CONFIG_UNSUPPORTED/SKIPPED are valid classifications; only
# missing evidence or HARNESS_ERROR makes the experiment invalid.
echo "=== EXP7 DIAG SUMMARY ===" | tee diag_summary.log
for pat in 'ZC_EXP7_API:' 'ZC_EXP7_IMG:' 'ZC_EXP7_REQ2:' 'ZC_EXP7_BUF_CAPS:' 'ZC_EXP7_BUF_ALIAS_RESULT=' 'ZC_EXP7_COMPLETE:'; do
  echo "--- $pat ---" | tee -a diag_summary.log
  grep -F "$pat" stitch.log | tee -a diag_summary.log || true
done
IMG_COUNT=$(grep -c 'ZC_EXP7_IMG:' stitch.log || true)
REQ_COUNT=$(grep -c 'ZC_EXP7_REQ2:' stitch.log || true)
BUF_RESULT=$(grep 'ZC_EXP7_BUF_ALIAS_RESULT=' stitch.log | tail -1 || true)
if grep -q 'ZC_EXP7_BUF_ALIAS_RESULT=HARNESS_ERROR' stitch.log; then
  echo "EXP7 INVALID: buffer control HARNESS_ERROR. Do not classify aliasing." | tee -a diag_summary.log
  exit 7
fi
if [ "$IMG_COUNT" -lt 2 ] || [ "$REQ_COUNT" -lt 2 ] || [ -z "$BUF_RESULT" ] || ! grep -q 'ZC_EXP7_COMPLETE:' stitch.log; then
  echo "EXP7 INVALID: required evidence incomplete (IMG=$IMG_COUNT REQ2=$REQ_COUNT BUF=${BUF_RESULT:-missing})." | tee -a diag_summary.log
  exit 7
fi
echo "Diagnostic block FIRED — EXP7 evidence complete." | tee -a diag_summary.log
echo "EXP7_VALID=1 IMG_COUNT=$IMG_COUNT REQ2_COUNT=$REQ_COUNT" | tee -a diag_summary.log
echo "$BUF_RESULT" | tee -a diag_summary.log
exit 0
