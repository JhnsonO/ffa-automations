#!/usr/bin/env bash
# OEV -- ticket 1d: test whether 1c's "patch `wgpu` was not used in the
# crate graph" warning was a Cargo.lock selection issue rather than a
# genuine version-string incompatibility.
#
# 1c (run 31813040183) kept the quartet [patch.crates-io] (wgpu,
# wgpu-core, wgpu-hal, wgpu-types all pointed at JhnsonO/wgpu@62e15ce1)
# but a fresh `cargo check` alone left the top-level `wgpu` crate
# resolved from crates.io 28.0.0 (unpatched) while wgpu-core/wgpu-hal
# resolved from the git fork 28.0.1 -- a pairing that was never actually
# published together, producing ~40 type errors.
#
# Cargo's own unused-patch diagnostic says this can happen when the
# patch version differs from what's already selected, and the fix is
# `cargo update` to move the lockfile onto the patched candidate. This
# script tests exactly that, with NO changes to the fork's source or
# version metadata -- if this alone unifies the graph, we don't need a
# fifth fork commit pinning wgpu's version to 28.0.0.
#
# Hard gate: resolution is verified with `cargo tree` BEFORE compiling.
# If the quartet isn't unified post-`cargo update`, this script stops
# immediately without running cargo check at all.
#
# Compile/integration proof ONLY:
#   - no CUDA external semaphore implementation
#   - no zero-copy behavioral change
#   - no render run (no benchmark pack, no model, no `reco stitch`)
#   - no GoPro processing
#   - no production merge
#   - no fork source/version changes
#
# Exit codes: 1=env sanity, 2=apt/rust setup, 3=cargo check failed,
#             6=cargo update failed, 7=post-update resolution gate
#             failed (quartet still split -- stops before compiling)

set -uo pipefail
cd /tmp/oev_run || exit 1

RECO_REPO="https://github.com/JhnsonO/video-stitcher"
RECO_BRANCH="diag/wgpu-hal-patch-compile-proof"
EXPECTED_RECO_SHA="d44df777fbfba19284e2d7d9506ba5dcb8eab91f"
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

echo "=== build.log: clone + checkout compile-proof branch (unchanged since 1c) ===" | tee -a build.log
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

echo "=== update.log: cargo update -p wgpu --precise 28.0.1 (lockfile-only, no fork edits) ===" | tee /tmp/oev_run/update.log
echo "timing_update_start=$(ts)" | tee -a timing.log
cargo update -p wgpu --precise 28.0.1 2>&1 | tee -a /tmp/oev_run/update.log
update_rc=${PIPESTATUS[0]}
echo "timing_update_end=$(ts)" | tee -a timing.log
if [ "$update_rc" -ne 0 ]; then
  echo "FATAL: cargo update -p wgpu --precise 28.0.1 failed (exit $update_rc)" | tee -a /tmp/oev_run/update.log
  exit 6
fi

echo "=== resolution.log: post-update quartet resolution gate (BEFORE any compile) ===" | tee /tmp/oev_run/resolution.log
ALL_UNIFIED=1
for CRATE in "${WGPU_CRATES[@]}"; do
  {
    echo "--- cargo tree -p $CRATE (or -i for non-root deps) ---"
  } | tee -a /tmp/oev_run/resolution.log
  if [ "$CRATE" = "wgpu" ]; then
    TREE_OUT=$(cargo tree -p wgpu 2>&1)
  else
    TREE_OUT=$(cargo tree -i "$CRATE" 2>&1)
  fi
  echo "$TREE_OUT" | tee -a /tmp/oev_run/resolution.log

  # Cargo.lock is the ground truth for source; parse it directly too.
  COUNT=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p' Cargo.lock | grep -c '^name =')
  VERSION=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p && /^version/{print; exit}' Cargo.lock | sed -E 's/.*"(.*)".*/\1/')
  SOURCE=$(awk -v name="\"$CRATE\"" '/^\[\[package\]\]$/{p=0} $0 ~ ("name = " name) {p=1} p && /^source/{print; exit}' Cargo.lock)

  echo "${CRATE}_instance_count=$COUNT" | tee -a /tmp/oev_run/resolution.log
  echo "${CRATE}_version=$VERSION" | tee -a /tmp/oev_run/resolution.log
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

echo "--- cargo tree -d (duplicate-source check) ---" | tee -a /tmp/oev_run/resolution.log
DUP_OUTPUT=$(cargo tree -d 2>&1)
echo "$DUP_OUTPUT" | tee -a /tmp/oev_run/resolution.log
DUP_WGPU_HITS=$(echo "$DUP_OUTPUT" | grep -iE '^wgpu(-core|-hal|-types)? v' || true)
if [ -n "$DUP_WGPU_HITS" ]; then
  echo "GATE FAIL: cargo tree -d shows duplicate wgpu-family crates:" | tee -a /tmp/oev_run/resolution.log
  echo "$DUP_WGPU_HITS" | tee -a /tmp/oev_run/resolution.log
  ALL_UNIFIED=0
fi

if [ "$ALL_UNIFIED" -ne 1 ]; then
  echo "=== DIAG SUMMARY ===" | tee /tmp/oev_run/diag_summary.log
  echo "GATE: quartet NOT unified after cargo update -p wgpu --precise 28.0.1 -- stopping WITHOUT compiling, per hard gate. See resolution.log." | tee -a /tmp/oev_run/diag_summary.log
  exit 7
fi
echo "GATE PASS: all four wgpu-family crates resolve as a single unified instance from JhnsonO/wgpu@$EXPECTED_WGPU_REV." | tee -a /tmp/oev_run/resolution.log

echo "=== build.log: cargo check -p reco-cli --features cuda (post cargo-update, gate passed) ===" | tee -a build.log
echo "timing_check_start=$(ts)" | tee -a timing.log
cargo check --release -p reco-cli --features cuda 2>&1 | tee -a build.log
check_rc=${PIPESTATUS[0]}
echo "timing_check_end=$(ts)" | tee -a timing.log
echo "cargo_check_exit_code=$check_rc" | tee -a /tmp/oev_run/build.log
if [ "$check_rc" -ne 0 ]; then
  echo "FATAL: cargo check failed (exit $check_rc)" | tee -a build.log
  echo "=== DIAG SUMMARY ===" | tee /tmp/oev_run/diag_summary.log
  echo "GATE PASS (quartet unified) but cargo check FAILED (exit $check_rc) -- see build.log." | tee -a /tmp/oev_run/diag_summary.log
  exit 3
fi
echo "cargo check: PASS (exit 0)" | tee -a /tmp/oev_run/build.log

echo "=== DIAG SUMMARY ===" | tee /tmp/oev_run/diag_summary.log
{
  echo "cargo update -p wgpu --precise 28.0.1: succeeded"
  echo "post-update resolution gate: PASS -- all four wgpu-family crates unified from JhnsonO/wgpu@$EXPECTED_WGPU_REV"
  echo "cargo check: PASS (exit 0)"
  echo "add_wait_semaphore type-checks: YES (cargo check would have failed at the probe function otherwise)"
  echo "video-stitcher SHA under test: $ACTUAL_RECO_SHA"
  echo "no fork source/version changes were made"
} | tee -a /tmp/oev_run/diag_summary.log

echo "=== 1d compile proof complete: PASS ==="
exit 0
