#!/usr/bin/env bash
# EXP8 compile proof only: exact Avenue-2 diagnostic baseline plus ONE
# behavioral change in vulkan.rs: image VkMemoryAllocateInfo::allocationSize
# uses Vulkan mem_reqs.size instead of CUDA VMM granularity-rounded size.
set -euo pipefail
cd /tmp/oev_run

RECO_REPO="https://github.com/JhnsonO/video-stitcher"
RECO_SHA="58542bbb9b91560e533ccc846a5eb3bd6e9d9db4"
EXPECTED_WGPU_REV="c8b6f2f00895210857f77f2a10fc1a32a80d5148"

ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
rm -f build.log resolution.log timing.log

echo "=== EXP8 compile proof ===" | tee build.log
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

rm -rf /tmp/video-stitcher
git clone -q "$RECO_REPO" /tmp/video-stitcher
cd /tmp/video-stitcher
git checkout -q "$RECO_SHA"
ACTUAL=$(git rev-parse HEAD)
test "$ACTUAL" = "$RECO_SHA"
echo "video_stitcher_sha=$ACTUAL" | tee -a /tmp/oev_run/build.log

# Hard scope gate: exactly one +1/-1 file vs the Avenue-2 baseline.
git diff --exit-code e810a04ee29452b3cd6647cc98875033a2e0d1a0 -- . ':!crates/reco-core/src/interop/vulkan.rs'
NUMSTAT=$(git diff --numstat e810a04ee29452b3cd6647cc98875033a2e0d1a0 -- crates/reco-core/src/interop/vulkan.rs)
echo "scope_numstat=$NUMSTAT" | tee -a /tmp/oev_run/build.log
test "$(echo "$NUMSTAT" | awk '{print $1}')" -eq 1
test "$(echo "$NUMSTAT" | awk '{print $2}')" -eq 1
grep -qF '.allocation_size(mem_reqs.size)' crates/reco-core/src/interop/vulkan.rs
if grep -qF '.allocation_size(shared_mem.alloc_size as u64)' crates/reco-core/src/interop/vulkan.rs; then
  echo 'FATAL: old image allocation size call still present' | tee -a /tmp/oev_run/build.log
  exit 10
fi
echo 'SCOPE GATE PASS: one-line allocation-size A/B only.' | tee -a /tmp/oev_run/build.log

echo "=== cargo update / wgpu resolution ===" | tee /tmp/oev_run/resolution.log
cargo update -p wgpu --precise 28.0.1 2>&1 | tee -a /tmp/oev_run/resolution.log
for crate in wgpu wgpu-core wgpu-hal wgpu-types; do
  cargo tree -p "$crate" 2>&1 | head -12 | tee -a /tmp/oev_run/resolution.log
  SOURCE_COUNT=$(awk -v name="\"$crate\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p && /^source = /{print}' Cargo.lock | grep -c "$EXPECTED_WGPU_REV" || true)
  test "$SOURCE_COUNT" -eq 1 || { echo "FATAL: $crate not pinned to expected wgpu fork" | tee -a /tmp/oev_run/resolution.log; exit 11; }
done
echo 'RESOLUTION GATE PASS' | tee -a /tmp/oev_run/resolution.log

echo "=== cargo check -p reco-cli --features cuda ===" | tee -a /tmp/oev_run/build.log
echo "timing_check_start=$(ts)" | tee -a /tmp/oev_run/timing.log
cargo check --release -p reco-cli --features cuda 2>&1 | tee -a /tmp/oev_run/build.log
echo "timing_check_end=$(ts)" | tee -a /tmp/oev_run/timing.log
echo 'EXP8_COMPILE_PROOF=PASS' | tee -a /tmp/oev_run/build.log
