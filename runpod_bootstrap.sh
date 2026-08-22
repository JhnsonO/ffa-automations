#!/usr/bin/env bash
# TEST-ONLY bootstrap wrapper for OEV containment v2 + dormant-ball registry v2.
#
# Runs the exact validated main bootstrap, then applies only:
#   1) containment v2 future-ball anticipation; and
#   2) Dynamic Dormant-Ball Registry v2 + media-time/reacquisition guards.
# No forced stationary challenger or camera-side filtering is applied.
# video-stitcher/main remains untouched.
set -euo pipefail

BASE_AUTOMATIONS_SHA="b043ef9fca4d15e6fa1379dda10c366f94046993"
PANNER_PATCH_SHA="fae991246d2d893b3207973b5652a0f5fd19e23e"
DORMANT_PATCH_SHA="c5c51c689a18b764fd839b4259c08806a7188b8d"
BASE_BOOTSTRAP="/tmp/runpod_bootstrap_validated.sh"
PANNER_PATCHER="/tmp/apply_ball_containment.py"
DORMANT_PATCHER="/tmp/apply_dormant_ball_registry_v2.py"
WORKDIR="/tmp/video-stitcher"
VERSIONS_LOG="/tmp/runpod_bootstrap_versions.log"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${BASE_AUTOMATIONS_SHA}/runpod_bootstrap.sh" \
  -o "$BASE_BOOTSTRAP"
chmod +x "$BASE_BOOTSTRAP"

echo "[dormant_ball_v2] Running exact validated bootstrap from ffa-automations ${BASE_AUTOMATIONS_SHA}"
bash "$BASE_BOOTSTRAP"

BASE_RECO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)
if [ "$BASE_RECO_SHA" != "c8b0d74b537d192c7de8d2856de64620a82830cf" ]; then
  echo "[dormant_ball_v2] FATAL: expected bridging Reco c8b0d74..., got $BASE_RECO_SHA" >&2
  exit 3
fi

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${PANNER_PATCH_SHA}/experiments/apply_ball_containment.py" \
  -o "$PANNER_PATCHER"
curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${DORMANT_PATCH_SHA}/experiments/apply_dormant_ball_registry_v2.py" \
  -o "$DORMANT_PATCHER"
test -s "$PANNER_PATCHER"
test -s "$DORMANT_PATCHER"
echo "[dormant_ball_v2] panner_patcher_sha256=$(sha256sum "$PANNER_PATCHER" | awk '{print $1}')"
echo "[dormant_ball_v2] dormant_patcher_sha256=$(sha256sum "$DORMANT_PATCHER" | awk '{print $1}')"

python3 "$PANNER_PATCHER"
python3 "$DORMANT_PATCHER"
git -C "$WORKDIR" diff --check

echo "[dormant_ball_v2] Reco source diff follows (test-only):"
git -C "$WORKDIR" diff -- \
  crates/reco-autocam/src/panners/field.rs \
  crates/reco-autocam/src/trackers/ball.rs \
  crates/reco-core/src/session/run_loop.rs \
  | tee /tmp/dormant_ball_v2_source.diff

echo "ball_containment_experiment=true" >> "$VERSIONS_LOG"
echo "ball_containment_version=v2_future_ball_anticipation" >> "$VERSIONS_LOG"
echo "ball_containment_patcher_commit=$PANNER_PATCH_SHA" >> "$VERSIONS_LOG"
echo "dormant_ball_registry_experiment=true" >> "$VERSIONS_LOG"
echo "dormant_ball_registry_version=v2_media_time_persistent_dormant_reacquire_guard" >> "$VERSIONS_LOG"
echo "dormant_ball_registry_patcher_commit=$DORMANT_PATCH_SHA" >> "$VERSIONS_LOG"
echo "dormant_ball_registry_media_time=true" >> "$VERSIONS_LOG"
echo "dormant_ball_registry_forced_switches=false" >> "$VERSIONS_LOG"
echo "dormant_ball_registry_confidence_bypass=false" >> "$VERSIONS_LOG"
echo "dormant_ball_registry_weak_reacquire_confirmation=3" >> "$VERSIONS_LOG"
echo "dormant_ball_registry_base_reco_sha=$BASE_RECO_SHA" >> "$VERSIONS_LOG"
echo "dormant_ball_registry_dirty_tree=true" >> "$VERSIONS_LOG"

source "$HOME/.cargo/env" 2>/dev/null || true
cd "$WORKDIR"
echo "[dormant_ball_v2] Running reco-autocam library tests before render..."
time cargo test -p reco-autocam --lib 2>&1 | tee /tmp/dormant_ball_v2_tests.log
TEST_RC=${PIPESTATUS[0]}
if [ "$TEST_RC" -ne 0 ]; then
  echo "[dormant_ball_v2] FATAL: reco-autocam tests failed (exit $TEST_RC)" >&2
  exit 3
fi

echo "[dormant_ball_v2] Rebuilding reco-cli --release --features cuda..."
time cargo build --release -p reco-cli --features cuda 2>&1 | tee /tmp/dormant_ball_v2_build.log
BUILD_RC=${PIPESTATUS[0]}
if [ "$BUILD_RC" -ne 0 ]; then
  echo "[dormant_ball_v2] FATAL: patched reco-cli build failed (exit $BUILD_RC)" >&2
  exit 3
fi

test -x "$WORKDIR/target/release/reco"
echo "[dormant_ball_v2] DORMANT_BALL_V2_TEST_BUILD=PASS version=v2_media_time_persistent_dormant_reacquire_guard base_reco=$BASE_RECO_SHA"
