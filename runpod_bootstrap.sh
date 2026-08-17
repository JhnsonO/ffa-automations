#!/usr/bin/env bash
# TEST-ONLY bootstrap wrapper for OEV ball-containment experiment.
#
# Runs the exact validated main bootstrap first (including its normal GPU/CUDA
# smoke test), then applies the committed experiment patch to the checked-out
# Reco c8b0d74 tree, runs reco-autocam unit tests, and rebuilds reco-cli.
# video-stitcher/main is NOT modified by this experiment.
set -euo pipefail

BASE_AUTOMATIONS_SHA="b043ef9fca4d15e6fa1379dda10c366f94046993"
PATCH_SCRIPT_SHA="cfd57b5b27a303adcd6bc635d24f9d5d0a01cb84"
BASE_BOOTSTRAP="/tmp/runpod_bootstrap_validated.sh"
PATCHER="/tmp/apply_ball_containment.py"
WORKDIR="/tmp/video-stitcher"
VERSIONS_LOG="/tmp/runpod_bootstrap_versions.log"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${BASE_AUTOMATIONS_SHA}/runpod_bootstrap.sh" \
  -o "$BASE_BOOTSTRAP"
chmod +x "$BASE_BOOTSTRAP"

echo "[containment_test] Running exact validated bootstrap from ffa-automations ${BASE_AUTOMATIONS_SHA}"
bash "$BASE_BOOTSTRAP"

BASE_RECO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)
if [ "$BASE_RECO_SHA" != "c8b0d74b537d192c7de8d2856de64620a82830cf" ]; then
  echo "[containment_test] FATAL: expected bridging Reco c8b0d74..., got $BASE_RECO_SHA" >&2
  exit 3
fi

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${PATCH_SCRIPT_SHA}/experiments/apply_ball_containment.py" \
  -o "$PATCHER"
test -s "$PATCHER"
echo "[containment_test] patcher_sha256=$(sha256sum "$PATCHER" | awk '{print $1}')"

python3 "$PATCHER"
git -C "$WORKDIR" diff --check

echo "[containment_test] Reco source diff follows (test-only, not committed to video-stitcher/main):"
git -C "$WORKDIR" diff -- crates/reco-autocam/src/panners/field.rs | tee /tmp/ball_containment_source.diff

echo "ball_containment_experiment=true" >> "$VERSIONS_LOG"
echo "ball_containment_base_reco_sha=$BASE_RECO_SHA" >> "$VERSIONS_LOG"
echo "ball_containment_patcher_commit=$PATCH_SCRIPT_SHA" >> "$VERSIONS_LOG"
echo "ball_containment_dirty_tree=true" >> "$VERSIONS_LOG"

# Compile/test the affected panner before spending time on the 180s live render.
source "$HOME/.cargo/env" 2>/dev/null || true
cd "$WORKDIR"
echo "[containment_test] Running reco-autocam library tests..."
time cargo test -p reco-autocam --lib 2>&1 | tee /tmp/ball_containment_tests.log
TEST_RC=${PIPESTATUS[0]}
if [ "$TEST_RC" -ne 0 ]; then
  echo "[containment_test] FATAL: reco-autocam tests failed (exit $TEST_RC)" >&2
  exit 3
fi

echo "[containment_test] Rebuilding reco-cli --release --features cuda with containment patch..."
time cargo build --release -p reco-cli --features cuda 2>&1 | tee /tmp/ball_containment_build.log
BUILD_RC=${PIPESTATUS[0]}
if [ "$BUILD_RC" -ne 0 ]; then
  echo "[containment_test] FATAL: patched reco-cli build failed (exit $BUILD_RC)" >&2
  exit 3
fi

test -x "$WORKDIR/target/release/reco"
echo "[containment_test] BALL_CONTAINMENT_TEST_BUILD=PASS base_reco=$BASE_RECO_SHA"
