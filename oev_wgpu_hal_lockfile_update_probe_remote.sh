#!/usr/bin/env bash
# OEV EXP7 compile proof only. Reconstructs the exact reviewed diagnostic
# source payload on top of video-stitcher e810a04e, verifies byte hashes,
# unifies the pinned wgpu fork in Cargo.lock, then runs cargo check.
# No render, no decode, no production merge, no network volume.
set -euo pipefail
cd /tmp/oev_run

RECO_REPO="https://github.com/JhnsonO/video-stitcher"
RECO_SHA="e810a04ee29452b3cd6647cc98875033a2e0d1a0"
FFA_REPO="https://github.com/JhnsonO/ffa-automations"
FFA_BRANCH="diag/zc-exp7-compile-proof"
EXPECTED_WGPU_REV="c8b6f2f00895210857f77f2a10fc1a32a80d5148"
EXPECTED_VULKAN_SHA256="3e21c4cbfa6b14ed0924f1038c033c844f24326ca514486b0c0a693fa84ea4c7"
EXPECTED_CUDA_SHA256="e81e693071608caef213eb34e68335d064e3aded6c58214147f2b6be4ac303b7"

rm -f build.log update.log resolution.log diag_summary.log timing.log

ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
echo "=== EXP7 compile proof: environment ===" | tee build.log
echo "timing_setup_start=$(ts)" | tee timing.log
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  git curl build-essential pkg-config cmake clang libclang-dev \
  libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libavfilter-dev \
  ca-certificates 2>&1 | tee -a build.log
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y 2>&1 | tee -a build.log
export PATH="/root/.cargo/bin:${PATH}"
rustc --version | tee -a build.log
echo "timing_setup_end=$(ts)" | tee -a timing.log

echo "=== Clone exact source + compile payload ===" | tee -a build.log
rm -rf /tmp/video-stitcher /tmp/ffa-exp7
git clone -q "$RECO_REPO" /tmp/video-stitcher
git -C /tmp/video-stitcher checkout -q "$RECO_SHA"
ACTUAL_RECO_SHA=$(git -C /tmp/video-stitcher rev-parse HEAD)
[ "$ACTUAL_RECO_SHA" = "$RECO_SHA" ] || { echo "FATAL: Reco SHA mismatch" | tee -a build.log; exit 11; }

git clone -q --single-branch --branch "$FFA_BRANCH" "$FFA_REPO" /tmp/ffa-exp7
PAYLOAD=/tmp/ffa-exp7/exp7_payload
VULKAN=/tmp/video-stitcher/crates/reco-core/src/interop/vulkan.rs
CUDA=/tmp/video-stitcher/crates/reco-core/src/interop/cuda.rs

cat "$PAYLOAD"/vulkan.part00 "$PAYLOAD"/vulkan.part01 "$PAYLOAD"/vulkan.part02 \
    "$PAYLOAD"/vulkan.part03 "$PAYLOAD"/vulkan.part04 > "$VULKAN"

python3 - "$CUDA" "$PAYLOAD/cuda_destroy_wrapper.txt" <<'PY'
from pathlib import Path
import sys
cuda = Path(sys.argv[1])
wrapper = Path(sys.argv[2]).read_text()
s = cuda.read_text()
marker = '/// Query (free, total) device memory in bytes via the CUDA driver.\n'
if 'pub fn cuda_destroy_external_semaphore(' in s:
    raise SystemExit('FATAL: destroy wrapper already present in base; refusing double insert')
if s.count(marker) != 1:
    raise SystemExit(f'FATAL: CUDA insertion marker count={s.count(marker)}, expected 1')
s = s.replace(marker, wrapper + marker, 1)
cuda.write_text(s)
PY

VULKAN_SHA=$(sha256sum "$VULKAN" | awk '{print $1}')
CUDA_SHA=$(sha256sum "$CUDA" | awk '{print $1}')
VULKAN_LINES=$(wc -l < "$VULKAN")
CUDA_LINES=$(wc -l < "$CUDA")
{
  echo "reco_sha=$ACTUAL_RECO_SHA"
  echo "vulkan_lines=$VULKAN_LINES vulkan_sha256=$VULKAN_SHA"
  echo "cuda_lines=$CUDA_LINES cuda_sha256=$CUDA_SHA"
} | tee -a build.log

[ "$VULKAN_LINES" -eq 1472 ] || { echo "FATAL: vulkan line count != 1472" | tee -a build.log; exit 12; }
[ "$VULKAN_SHA" = "$EXPECTED_VULKAN_SHA256" ] || { echo "FATAL: vulkan payload hash mismatch" | tee -a build.log; exit 13; }
[ "$CUDA_SHA" = "$EXPECTED_CUDA_SHA256" ] || { echo "FATAL: cuda payload hash mismatch" | tee -a build.log; exit 14; }
if grep -Eq 'zc_exp7_fd_memory_type_bits|ZC_EXP7_FD_IMG|eligible_bits|fd_bits|get_memory_fd_properties' "$VULKAN"; then
  echo "FATAL: forbidden reverted fd-query code present" | tee -a build.log
  exit 15
fi
for marker in ZC_EXP7_API DEDICATED_ONLY cuda_signal_external_semaphore wait_dst_stage_mask ZC_EXP7_ABORT_AFTER cuda_destroy_external_semaphore; do
  grep -q "$marker" "$VULKAN" "$CUDA" || { echo "FATAL: required marker missing: $marker" | tee -a build.log; exit 16; }
done
echo "PAYLOAD GATE PASS: exact reviewed EXP7 files reconstructed." | tee -a build.log

cd /tmp/video-stitcher
echo "=== Cargo lockfile unification ===" | tee update.log
echo "timing_update_start=$(ts)" | tee -a /tmp/oev_run/timing.log
cargo update -p wgpu --precise 28.0.1 2>&1 | tee -a /tmp/oev_run/update.log
echo "timing_update_end=$(ts)" | tee -a /tmp/oev_run/timing.log

{
  echo "=== wgpu source resolution ==="
  for crate in wgpu wgpu-core wgpu-hal wgpu-types; do
    echo "--- $crate ---"
    cargo tree -i "$crate" 2>&1 || cargo tree -p "$crate" 2>&1
  done
  echo "--- Cargo.lock expected rev occurrences ---"
  grep -n "$EXPECTED_WGPU_REV" Cargo.lock || true
} | tee resolution.log
COUNT=$(grep -c "$EXPECTED_WGPU_REV" Cargo.lock || true)
[ "$COUNT" -ge 4 ] || { echo "FATAL: expected wgpu fork rev not present for quartet (count=$COUNT)" | tee -a resolution.log; exit 17; }
if cargo tree -d 2>&1 | grep -Eq '^wgpu(-core|-hal|-types)? v'; then
  echo "FATAL: duplicate wgpu-family crates remain after cargo update" | tee -a resolution.log
  cargo tree -d 2>&1 | tee -a resolution.log
  exit 18
fi
echo "RESOLUTION GATE PASS" | tee -a resolution.log

echo "=== cargo check -p reco-cli --features cuda ===" | tee -a /tmp/oev_run/build.log
echo "timing_check_start=$(ts)" | tee -a /tmp/oev_run/timing.log
set +e
cargo check --release -p reco-cli --features cuda 2>&1 | tee -a /tmp/oev_run/build.log
RC=${PIPESTATUS[0]}
set -e
echo "timing_check_end=$(ts)" | tee -a /tmp/oev_run/timing.log
echo "cargo_check_exit_code=$RC" | tee -a /tmp/oev_run/build.log

{
  echo "=== EXP7 COMPILE PROOF SUMMARY ==="
  echo "video-stitcher base: $ACTUAL_RECO_SHA"
  echo "vulkan.rs: lines=$VULKAN_LINES sha256=$VULKAN_SHA"
  echo "cuda.rs: lines=$CUDA_LINES sha256=$CUDA_SHA"
  echo "forbidden OPAQUE_FD fd-properties probe: ABSENT"
  echo "wgpu fork rev: $EXPECTED_WGPU_REV"
  echo "cargo check exit: $RC"
  if [ "$RC" -eq 0 ]; then echo "VERDICT: PASS"; else echo "VERDICT: FAIL"; fi
} | tee diag_summary.log

exit "$RC"
