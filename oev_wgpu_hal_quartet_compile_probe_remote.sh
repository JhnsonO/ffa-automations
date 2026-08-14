#!/usr/bin/env bash
# OEV -- ticket 1c: compile-only proof for the quartet-patch strategy.
#
# 1b patched wgpu-hal's SOURCE alone (wgpu/wgpu-core/wgpu-types stayed on
# crates.io) and cargo check failed with 101 errors -- rustc E0308 "two
# different versions of crate `wgpu_types` are being used", because the
# forked wgpu-hal pulls its own workspace-locked wgpu-types from the fork
# monorepo, distinct from the crates.io copy wgpu-core uses. This script
# tests the fix: patch all four tightly-coupled crates (wgpu, wgpu-core,
# wgpu-hal, wgpu-types) to the same fork/rev, so only one wgpu-types
# instance exists in the graph.
#
# Compile/integration proof ONLY:
#   - no CUDA external semaphore implementation
#   - no zero-copy behavioral change
#   - no render run (no benchmark pack, no model, no `reco stitch`)
#   - no GoPro processing
#   - no production merge
#
# Stricter acceptance criteria for this run (vs 1b):
#   1. All four wgpu crates resolve from JhnsonO/wgpu@<rev> in Cargo.lock.
#   2. `cargo tree -d` shows no duplicate wgpu-types (or wgpu-core/
#      wgpu-hal/wgpu) source split.
#   3. The add_wait_semaphore() probe call compiles (implied by a clean
#      cargo check, since it's the only call site).
#   4. cargo check exit code is exactly 0 -- not "looks plausible".
#   5. This script's own exit code is the source of truth for the
#      workflow's pass/fail evaluation, not a grep of a log file that
#      may or may not have been pulled back.
#
# Exit codes: 1=env sanity, 2=apt/rust setup, 3=cargo check failed,
#             4=lockfile did not resolve the expected patched source for
#               one or more of the four crates, or a resolved version
#               changed unexpectedly, 5=duplicate source detected via
#               `cargo tree -d`

set -uo pipefail
cd /tmp/oev_run || exit 1

RECO_REPO="https://github.com/JhnsonO/video-stitcher"
RECO_BRANCH="diag/wgpu-hal-patch-compile-proof"
EXPECTED_RECO_SHA="9df865dc96ae0e61f66af1005218098d972bbcd0"
EXPECTED_WGPU_REV="62e15ce1cc94929235d27b59962abb511622fb4e"
WGPU_CRATES=("wgpu" "wgpu-core" "wgpu-hal" "wgpu-types")

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

echo "=== build.log: clone + checkout compile-proof branch (1c commit) ===" | tee -a build.log
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

echo "=== build.log: cargo check -p reco-cli --features cuda (quartet-patched wgpu family) ===" | tee -a build.log
echo "timing_check_start=$(ts)" | tee -a timing.log
cargo check --release -p reco-cli --features cuda 2>&1 | tee -a build.log
check_rc=${PIPESTATUS[0]}
echo "timing_check_end=$(ts)" | tee -a timing.log
echo "cargo_check_exit_code=$check_rc" | tee -a /tmp/oev_run/build.log
if [ "$check_rc" -ne 0 ]; then
  echo "FATAL: cargo check failed (exit $check_rc)" | tee -a build.log
  exit 3
fi
echo "cargo check: PASS (exit 0)" | tee -a /tmp/oev_run/build.log

echo "=== resolution.log: Cargo.lock evidence for all four wgpu crates ===" | tee /tmp/oev_run/resolution.log
ALL_RESOLVED_OK=1
for CRATE in "${WGPU_CRATES[@]}"; do
  {
    echo "--- $CRATE package block(s) in Cargo.lock ---"
    awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p' Cargo.lock
    echo
  } | tee -a /tmp/oev_run/resolution.log

  COUNT=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p' Cargo.lock | grep -c '^name =')
  VERSION=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p && /^version/{print; exit}' Cargo.lock | sed -E 's/.*"(.*)".*/\1/')
  SOURCE=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p && /^source/{print; exit}' Cargo.lock)

  echo "${CRATE}_instance_count=$COUNT" | tee -a /tmp/oev_run/resolution.log
  echo "${CRATE}_version=$VERSION" | tee -a /tmp/oev_run/resolution.log
  echo "${CRATE}_source_line=$SOURCE" | tee -a /tmp/oev_run/resolution.log

  if [ "$COUNT" -ne 1 ]; then
    echo "FATAL: expected exactly 1 $CRATE instance in Cargo.lock, found $COUNT" | tee -a /tmp/oev_run/resolution.log
    ALL_RESOLVED_OK=0
  fi
  if ! echo "$SOURCE" | grep -q "JhnsonO/wgpu" || ! echo "$SOURCE" | grep -q "$EXPECTED_WGPU_REV"; then
    echo "FATAL: $CRATE source line does not reference the expected fork+rev. Got: $SOURCE" | tee -a /tmp/oev_run/resolution.log
    ALL_RESOLVED_OK=0
  fi
done

echo "=== resolution.log: cargo tree -d (duplicate-source check) ===" | tee -a /tmp/oev_run/resolution.log
DUP_OUTPUT=$(cargo tree -d 2>&1)
echo "$DUP_OUTPUT" | tee -a /tmp/oev_run/resolution.log
DUP_WGPU_HITS=$(echo "$DUP_OUTPUT" | grep -iE '^wgpu(-core|-hal|-types)? v' || true)
if [ -n "$DUP_WGPU_HITS" ]; then
  echo "FATAL: cargo tree -d shows duplicate wgpu-family crates:" | tee -a /tmp/oev_run/resolution.log
  echo "$DUP_WGPU_HITS" | tee -a /tmp/oev_run/resolution.log
  ALL_RESOLVED_OK=0
else
  echo "No duplicate wgpu-family crates found by cargo tree -d." | tee -a /tmp/oev_run/resolution.log
fi

if [ "$ALL_RESOLVED_OK" -ne 1 ]; then
  echo "=== DIAG SUMMARY ===" | tee /tmp/oev_run/diag_summary.log
  echo "cargo check: PASS but resolution/duplicate checks FAILED -- see resolution.log" | tee -a /tmp/oev_run/diag_summary.log
  exit 5
fi

echo "=== DIAG SUMMARY ===" | tee /tmp/oev_run/diag_summary.log
{
  echo "cargo check: PASS (exit 0)"
  echo "quartet resolution: all four wgpu crates from JhnsonO/wgpu@$EXPECTED_WGPU_REV, 1 instance each"
  echo "cargo tree -d: no duplicate wgpu-family crates"
  echo "add_wait_semaphore type-checks: YES (cargo check would have failed at the probe function otherwise)"
  echo "video-stitcher SHA under test: $ACTUAL_RECO_SHA"
} | tee -a /tmp/oev_run/diag_summary.log

echo "=== 1c compile proof complete: PASS ==="
exit 0
