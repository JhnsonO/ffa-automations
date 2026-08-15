#!/usr/bin/env bash
# Compile-only gate for the Linux CUDA-shared-VkBuffer -> normal-texture path.
# No footage, model, network volume, production pin, or production workflow.
set -euo pipefail
cd /tmp/oev_run

RECO_REPO="https://github.com/JhnsonO/video-stitcher"
RECO_SHA="acdcc61ece2f1d9bda453dea32ec3be10d34172a"
RECO_BASE="9059470f01065d5336af8c94bac27a860156dfec"
EXPECTED_WGPU_REV="c8b6f2f00895210857f77f2a10fc1a32a80d5148"

ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
rm -f build.log resolution.log timing.log diag_summary.log

echo "=== shared-buffer copy compile proof ===" | tee build.log
echo "timing_setup_start=$(ts)" | tee timing.log
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  git curl build-essential pkg-config cmake clang libclang-dev \
  libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libavfilter-dev \
  ca-certificates 2>&1 | tee -a build.log
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y 2>&1 | tee -a build.log
export PATH="/root/.cargo/bin:${PATH}"
rustc --version | tee -a build.log
cargo --version | tee -a build.log
echo "timing_setup_end=$(ts)" | tee -a timing.log

rm -rf /tmp/video-stitcher
git clone -q "$RECO_REPO" /tmp/video-stitcher
cd /tmp/video-stitcher
git checkout -q "$RECO_SHA"
ACTUAL=$(git rev-parse HEAD)
test "$ACTUAL" = "$RECO_SHA"
echo "video_stitcher_sha=$ACTUAL" | tee -a /tmp/oev_run/build.log

EXPECTED_FILES=$(printf '%s\n' \
  crates/reco-core/src/interop/cuda.rs \
  crates/reco-core/src/interop/vulkan.rs \
  crates/reco-core/src/session/frame_processing.rs \
  crates/reco-core/src/session/mod.rs \
  crates/reco-core/src/session/run_loop.rs \
  crates/reco-core/src/session/vram_pool.rs \
  crates/reco-core/src/session/zero_copy_linux.rs \
  crates/reco-io/src/smart_source.rs | sort)
ACTUAL_FILES=$(git diff --name-only "$RECO_BASE" "$RECO_SHA" | sort)
diff -u <(printf '%s\n' "$EXPECTED_FILES") <(printf '%s\n' "$ACTUAL_FILES")
grep -qF 'pub fn create_shared_buffer(' crates/reco-core/src/interop/vulkan.rs
grep -qF 'Buffer::from_raw_managed' crates/reco-core/src/interop/vulkan.rs
grep -qF 'copy_buffer_to_texture(' crates/reco-core/src/session/vram_pool.rs
grep -qF 'pool.copy_from_buffers(' crates/reco-core/src/session/frame_processing.rs
if grep -qF 'get_memory_fd_properties' crates/reco-core/src/interop/vulkan.rs; then
  echo 'FATAL: forbidden vkGetMemoryFdPropertiesKHR-equivalent call found for OPAQUE_FD' | tee -a /tmp/oev_run/build.log
  exit 10
fi
echo 'SCOPE GATE PASS: expected eight production-shaped buffer-copy/sync files only.' | tee -a /tmp/oev_run/build.log

echo "=== cargo update / wgpu resolution ===" | tee /tmp/oev_run/resolution.log
cargo update -p wgpu --precise 28.0.1 2>&1 | tee -a /tmp/oev_run/resolution.log
for crate in wgpu wgpu-core wgpu-hal wgpu-types; do
  SOURCE_LINES=$(awk -v name="\"$crate\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p && /^source = /{print}' Cargo.lock)
  printf '%s_source=%s\n' "$crate" "$SOURCE_LINES" | tee -a /tmp/oev_run/resolution.log
  SOURCE_COUNT=$(printf '%s\n' "$SOURCE_LINES" | grep -c "$EXPECTED_WGPU_REV" || true)
  test "$SOURCE_COUNT" -eq 1 || { echo "FATAL: $crate not pinned to expected wgpu fork" | tee -a /tmp/oev_run/resolution.log; exit 11; }
done
echo 'RESOLUTION GATE PASS' | tee -a /tmp/oev_run/resolution.log

echo "timing_fmt_start=$(ts)" | tee -a /tmp/oev_run/timing.log
cargo fmt --all -- --check 2>&1 | tee -a /tmp/oev_run/build.log
echo "timing_fmt_end=$(ts)" | tee -a /tmp/oev_run/timing.log

echo "=== cargo check -p reco-cli --features cuda ===" | tee -a /tmp/oev_run/build.log
echo "timing_check_start=$(ts)" | tee -a /tmp/oev_run/timing.log
cargo check --release -p reco-cli --features cuda 2>&1 | tee -a /tmp/oev_run/build.log
echo "timing_check_end=$(ts)" | tee -a /tmp/oev_run/timing.log

{
  echo 'shared-buffer compile proof: PASS'
  echo "video-stitcher SHA: $ACTUAL"
  echo "wgpu quartet: $EXPECTED_WGPU_REV"
  echo 'cargo fmt: PASS'
  echo 'cargo check: PASS'
} | tee /tmp/oev_run/diag_summary.log
