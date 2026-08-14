#!/usr/bin/env bash
# OEV -- ticket 1b: compile-only proof that a patched wgpu-hal (fork
# JhnsonO/wgpu@62e15ce1, minimal backport of upstream #9461 onto v28.0.1)
# resolves and compiles inside Reco's workspace, and that
# Queue::add_wait_semaphore type-checks through the existing
# gpu.queue.as_hal::<Vulkan>() access pattern already used in
# interop/vulkan.rs.
#
# Compile/integration proof ONLY:
#   - no CUDA external semaphore implementation
#   - no zero-copy behavioral change
#   - no render run (no benchmark pack, no model, no `reco stitch`)
#   - no GoPro processing
#   - no production merge
#
# Branch under test: diag/wgpu-hal-patch-compile-proof (video-stitcher),
# off diag/zero-copy-frame0-readback @ 79667aac. Two files changed vs
# that parent: Cargo.toml ([patch.crates-io] wgpu-hal source redirect
# only, no version bump) and interop/vulkan.rs (one new, never-called,
# #[allow(dead_code)] probe function).
#
# Exit codes: 1=env sanity, 2=apt/rust setup, 3=cargo check failed,
#             4=lockfile did not resolve the expected patched source,
#             5=more than one wgpu-hal 28.0.1 instance in the graph

set -uo pipefail
cd /tmp/oev_run || exit 1

RECO_REPO="https://github.com/JhnsonO/video-stitcher"
RECO_BRANCH="diag/wgpu-hal-patch-compile-proof"
EXPECTED_RECO_SHA="9497b5af8c48b14196946a1dbc819e22f38b5222"
EXPECTED_WGPU_HAL_REV="62e15ce1cc94929235d27b59962abb511622fb4e"

ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }

echo "=== build.log: apt deps + Rust toolchain ===" | tee build.log
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

echo "=== build.log: clone + checkout compile-proof branch ===" | tee -a build.log
rm -rf /tmp/video-stitcher
git clone "$RECO_REPO" /tmp/video-stitcher 2>&1 | tee -a build.log
cd /tmp/video-stitcher || exit 1
git checkout "$RECO_BRANCH" 2>&1 | tee -a build.log
ACTUAL_RECO_SHA=$(git rev-parse HEAD)
echo "video_stitcher_sha=$ACTUAL_RECO_SHA" | tee -a /tmp/oev_run/build.log
if [ "$ACTUAL_RECO_SHA" != "$EXPECTED_RECO_SHA" ]; then
  echo "FATAL: checked-out SHA ($ACTUAL_RECO_SHA) != expected ($EXPECTED_RECO_SHA) -- branch moved under us, aborting rather than testing the wrong commit." | tee -a /tmp/oev_run/build.log
  exit 1
fi

echo "=== build.log: cargo check -p reco-cli --features cuda (patched wgpu-hal) ===" | tee -a build.log
echo "timing_check_start=$(ts)" | tee -a timing.log
cargo check --release -p reco-cli --features cuda 2>&1 | tee -a build.log
check_rc=${PIPESTATUS[0]}
echo "timing_check_end=$(ts)" | tee -a timing.log
if [ "$check_rc" -ne 0 ]; then
  echo "FATAL: cargo check failed (exit $check_rc)" | tee -a build.log
  exit 3
fi
echo "cargo check: PASS" | tee -a /tmp/oev_run/build.log

echo "=== resolution.log: Cargo.lock evidence ===" | tee /tmp/oev_run/resolution.log
{
  echo "--- wgpu-hal package block(s) in Cargo.lock ---"
  awk '/^\[\[package\]\]$/{p=0} /name = "wgpu-hal"/{p=1} p' Cargo.lock
  echo
  echo "--- instance count (expect exactly 1) ---"
  grep -c 'name = "wgpu-hal"' Cargo.lock
  echo
  echo "--- cargo tree -i wgpu-hal (resolution path) ---"
  cargo tree -i wgpu-hal 2>&1
} | tee -a /tmp/oev_run/resolution.log

WGPU_HAL_COUNT=$(grep -c 'name = "wgpu-hal"' Cargo.lock)
WGPU_HAL_VERSION=$(awk '/^\[\[package\]\]$/{p=0} /name = "wgpu-hal"/{p=1} p && /^version/{print; exit}' Cargo.lock | sed -E 's/.*"(.*)".*/\1/')
WGPU_HAL_SOURCE=$(awk '/^\[\[package\]\]$/{p=0} /name = "wgpu-hal"/{p=1} p && /^source/{print; exit}' Cargo.lock)

echo "wgpu_hal_instance_count=$WGPU_HAL_COUNT" | tee -a /tmp/oev_run/resolution.log
echo "wgpu_hal_version=$WGPU_HAL_VERSION" | tee -a /tmp/oev_run/resolution.log
echo "wgpu_hal_source_line=$WGPU_HAL_SOURCE" | tee -a /tmp/oev_run/resolution.log

if [ "$WGPU_HAL_COUNT" -ne 1 ]; then
  echo "FATAL: expected exactly 1 wgpu-hal instance in Cargo.lock, found $WGPU_HAL_COUNT" | tee -a /tmp/oev_run/resolution.log
  exit 5
fi
if [ "$WGPU_HAL_VERSION" != "28.0.1" ]; then
  echo "FATAL: resolved wgpu-hal version is '$WGPU_HAL_VERSION', expected 28.0.1" | tee -a /tmp/oev_run/resolution.log
  exit 4
fi
if ! echo "$WGPU_HAL_SOURCE" | grep -q "JhnsonO/wgpu" || ! echo "$WGPU_HAL_SOURCE" | grep -q "$EXPECTED_WGPU_HAL_REV"; then
  echo "FATAL: wgpu-hal source line does not reference the expected fork+rev. Got: $WGPU_HAL_SOURCE" | tee -a /tmp/oev_run/resolution.log
  exit 4
fi

echo "=== DIAG SUMMARY ===" | tee /tmp/oev_run/diag_summary.log
{
  echo "cargo check: PASS"
  echo "wgpu-hal instances: $WGPU_HAL_COUNT (expected 1)"
  echo "wgpu-hal version: $WGPU_HAL_VERSION (expected 28.0.1)"
  echo "wgpu-hal source: $WGPU_HAL_SOURCE"
  echo "add_wait_semaphore type-checks: YES (cargo check would have failed at the probe function otherwise)"
  echo "video-stitcher SHA under test: $ACTUAL_RECO_SHA"
} | tee -a /tmp/oev_run/diag_summary.log

echo "=== 1b compile proof complete ==="
exit 0
