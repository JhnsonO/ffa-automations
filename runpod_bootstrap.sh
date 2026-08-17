#!/usr/bin/env bash
# TEST-ONLY bootstrap wrapper for OEV containment + active-ball validation.
#
# Runs the exact validated main bootstrap first (including its normal GPU/CUDA
# smoke test), then applies the committed experiment patches to the checked-out
# Reco c8b0d74 tree, runs reco-autocam unit tests, and rebuilds reco-cli.
# video-stitcher/main is NOT modified by this experiment.
set -euo pipefail

BASE_AUTOMATIONS_SHA="b043ef9fca4d15e6fa1379dda10c366f94046993"
PANNER_PATCH_SHA="fae991246d2d893b3207973b5652a0f5fd19e23e"
ACTIVE_BALL_PATCH_SHA="ac3c3448eae3a38f90bf693cb5dd30b02342ddd9"
BASE_BOOTSTRAP="/tmp/runpod_bootstrap_validated.sh"
PANNER_PATCHER="/tmp/apply_ball_containment.py"
ACTIVE_BALL_PATCHER="/tmp/apply_active_ball_filter.py"
WORKDIR="/tmp/video-stitcher"
VERSIONS_LOG="/tmp/runpod_bootstrap_versions.log"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${BASE_AUTOMATIONS_SHA}/runpod_bootstrap.sh" \
  -o "$BASE_BOOTSTRAP"
chmod +x "$BASE_BOOTSTRAP"

echo "[active_ball_test] Running exact validated bootstrap from ffa-automations ${BASE_AUTOMATIONS_SHA}"
bash "$BASE_BOOTSTRAP"

BASE_RECO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)
if [ "$BASE_RECO_SHA" != "c8b0d74b537d192c7de8d2856de64620a82830cf" ]; then
  echo "[active_ball_test] FATAL: expected bridging Reco c8b0d74..., got $BASE_RECO_SHA" >&2
  exit 3
fi

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${PANNER_PATCH_SHA}/experiments/apply_ball_containment.py" \
  -o "$PANNER_PATCHER"
curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${ACTIVE_BALL_PATCH_SHA}/experiments/apply_active_ball_filter.py" \
  -o "$ACTIVE_BALL_PATCHER"
test -s "$PANNER_PATCHER"
test -s "$ACTIVE_BALL_PATCHER"
echo "[active_ball_test] panner_patcher_sha256=$(sha256sum "$PANNER_PATCHER" | awk '{print $1}')"
echo "[active_ball_test] active_ball_patcher_sha256=$(sha256sum "$ACTIVE_BALL_PATCHER" | awk '{print $1}')"

python3 "$PANNER_PATCHER"
python3 "$ACTIVE_BALL_PATCHER"
git -C "$WORKDIR" diff --check

echo "[active_ball_test] Reco source diff follows (test-only, not committed to video-stitcher/main):"
git -C "$WORKDIR" diff -- \
  crates/reco-autocam/src/panners/field.rs \
  crates/reco-autocam/src/trackers/ball.rs \
  | tee /tmp/active_ball_source.diff

echo "ball_containment_experiment=true" >> "$VERSIONS_LOG"
echo "ball_containment_version=v2_future_ball_anticipation" >> "$VERSIONS_LOG"
echo "ball_containment_patcher_commit=$PANNER_PATCH_SHA" >> "$VERSIONS_LOG"
echo "active_ball_validation_experiment=true" >> "$VERSIONS_LOG"
echo "active_ball_validation_version=v1_stationary_and_anchor_guard" >> "$VERSIONS_LOG"
echo "active_ball_validation_patcher_commit=$ACTIVE_BALL_PATCH_SHA" >> "$VERSIONS_LOG"
echo "active_ball_validation_base_reco_sha=$BASE_RECO_SHA" >> "$VERSIONS_LOG"
echo "active_ball_validation_dirty_tree=true" >> "$VERSIONS_LOG"

# Compile/test the affected panner + ball tracker before spending time on the
# 180s live render. The new active-ball regression tests live in reco-autocam.
source "$HOME/.cargo/env" 2>/dev/null || true
cd "$WORKDIR"
echo "[active_ball_test] Running reco-autocam library tests..."
time cargo test -p reco-autocam --lib 2>&1 | tee /tmp/active_ball_tests.log
TEST_RC=${PIPESTATUS[0]}
if [ "$TEST_RC" -ne 0 ]; then
  echo "[active_ball_test] FATAL: reco-autocam tests failed (exit $TEST_RC)" >&2
  exit 3
fi

echo "[active_ball_test] Rebuilding reco-cli --release --features cuda with experiment patches..."
time cargo build --release -p reco-cli --features cuda 2>&1 | tee /tmp/active_ball_build.log
BUILD_RC=${PIPESTATUS[0]}
if [ "$BUILD_RC" -ne 0 ]; then
  echo "[active_ball_test] FATAL: patched reco-cli build failed (exit $BUILD_RC)" >&2
  exit 3
fi

test -x "$WORKDIR/target/release/reco"
echo "[active_ball_test] ACTIVE_BALL_TEST_BUILD=PASS version=v1_stationary_and_anchor_guard base_reco=$BASE_RECO_SHA"
