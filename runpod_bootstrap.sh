#!/usr/bin/env bash
# TEST-ONLY bootstrap wrapper for containment v2 + persistent dormant-object memory.
#
# Starts from the exact validated good containment-v2 base. Production
# video-stitcher/main remains untouched. The active BallTracker's scoring,
# jump/coast/reacquisition behaviour is not changed by this experiment.
set -euo pipefail

BASE_AUTOMATIONS_SHA="b043ef9fca4d15e6fa1379dda10c366f94046993"
PANNER_PATCH_SHA="fae991246d2d893b3207973b5652a0f5fd19e23e"
DORMANT_PATCH_SHA="4f1b427fa8a3d64e9ea99952b51f97e5f9e8b0bf"
SOURCE_BRANCH="test/oev-dormant-object-memory-02"
SOURCE_REPO="/tmp/ffa-automations-source"
BASE_BOOTSTRAP="/tmp/runpod_bootstrap_validated.sh"
PANNER_PATCHER="/tmp/apply_ball_containment.py"
DORMANT_PATCHER="/tmp/apply_dormant_object_memory.py"
WORKDIR="/tmp/video-stitcher"
VERSIONS_LOG="/tmp/runpod_bootstrap_versions.log"

command -v git >/dev/null 2>&1 || {
  echo "[dormant_object_test] FATAL: git missing from RunPod base image" >&2
  exit 2
}
rm -rf "$SOURCE_REPO"
echo "[dormant_object_test] Fetching experiment sources via git transport (no raw.githubusercontent.com)..."
git clone --filter=blob:none --no-checkout https://github.com/JhnsonO/ffa-automations.git "$SOURCE_REPO"
git -C "$SOURCE_REPO" fetch --depth=1 origin "$BASE_AUTOMATIONS_SHA"
git -C "$SOURCE_REPO" fetch --depth=1 origin "$PANNER_PATCH_SHA"
git -C "$SOURCE_REPO" fetch --depth=20 origin "$SOURCE_BRANCH"

git -C "$SOURCE_REPO" show "${BASE_AUTOMATIONS_SHA}:runpod_bootstrap.sh" > "$BASE_BOOTSTRAP"
git -C "$SOURCE_REPO" show "${PANNER_PATCH_SHA}:experiments/apply_ball_containment.py" > "$PANNER_PATCHER"
git -C "$SOURCE_REPO" show "${DORMANT_PATCH_SHA}:experiments/apply_dormant_object_memory.py" > "$DORMANT_PATCHER"
test -s "$BASE_BOOTSTRAP"
test -s "$PANNER_PATCHER"
test -s "$DORMANT_PATCHER"
chmod +x "$BASE_BOOTSTRAP"

echo "[dormant_object_test] Running exact validated bootstrap from ffa-automations ${BASE_AUTOMATIONS_SHA}"
bash "$BASE_BOOTSTRAP"

BASE_RECO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)
if [ "$BASE_RECO_SHA" != "c8b0d74b537d192c7de8d2856de64620a82830cf" ]; then
  echo "[dormant_object_test] FATAL: expected bridging Reco c8b0d74..., got $BASE_RECO_SHA" >&2
  exit 3
fi

python3 "$PANNER_PATCHER"
python3 "$DORMANT_PATCHER"
git -C "$WORKDIR" diff --check

echo "[dormant_object_test] Reco source diff follows (test-only):"
git -C "$WORKDIR" diff -- \
  crates/reco-autocam/src/panners/field.rs \
  crates/reco-autocam/src/trackers/ball.rs \
  crates/reco-autocam/src/trackers/mod.rs \
  crates/reco-autocam/src/trackers/dormant_ball.rs \
  | tee /tmp/dormant_object_source.diff

echo "ball_containment_experiment=true" >> "$VERSIONS_LOG"
echo "ball_containment_version=v2_future_ball_anticipation" >> "$VERSIONS_LOG"
echo "ball_containment_patcher_commit=$PANNER_PATCH_SHA" >> "$VERSIONS_LOG"
echo "dormant_object_memory_experiment=true" >> "$VERSIONS_LOG"
echo "dormant_object_memory_version=v2_persistent_identity_memory" >> "$VERSIONS_LOG"
echo "dormant_object_memory_patcher_commit=$DORMANT_PATCH_SHA" >> "$VERSIONS_LOG"
echo "dormant_object_memory_base_reco_sha=$BASE_RECO_SHA" >> "$VERSIONS_LOG"
echo "dormant_object_memory_active_tracker_changes=none" >> "$VERSIONS_LOG"
echo "dormant_object_memory_dirty_tree=true" >> "$VERSIONS_LOG"

source "$HOME/.cargo/env" 2>/dev/null || true
cd "$WORKDIR"
echo "[dormant_object_test] Running reco-autocam library tests..."
time cargo test -p reco-autocam --lib 2>&1 | tee /tmp/dormant_object_tests.log
TEST_RC=${PIPESTATUS[0]}
if [ "$TEST_RC" -ne 0 ]; then
  echo "[dormant_object_test] FATAL: reco-autocam tests failed (exit $TEST_RC)" >&2
  exit 3
fi

echo "[dormant_object_test] Rebuilding reco-cli --release --features cuda..."
time cargo build --release -p reco-cli --features cuda 2>&1 | tee /tmp/dormant_object_build.log
BUILD_RC=${PIPESTATUS[0]}
if [ "$BUILD_RC" -ne 0 ]; then
  echo "[dormant_object_test] FATAL: patched reco-cli build failed (exit $BUILD_RC)" >&2
  exit 3
fi

test -x "$WORKDIR/target/release/reco"
echo "[dormant_object_test] DORMANT_OBJECT_TEST_BUILD=PASS version=v2_persistent_identity_memory base_reco=$BASE_RECO_SHA"
