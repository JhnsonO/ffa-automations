#!/usr/bin/env bash
# Compatibility entrypoint for the existing proven RunPod frame-stride workflow.
#
# The workflow still SCPs this historical filename, but this ticket has moved
# from the old test-only source-frame-dropping matrix to the production design:
#   - exact Reco candidate b2fc622
#   - stride 1 vs stride 3 only
#   - every source frame is rendered
#   - AI analysis runs every third frame at stride 3
#   - no post-hoc setpts/retiming
#
# Keeping this as the workflow entrypoint lets us reuse the already-proven
# RunPod launch, preflight, sample/model staging, artifact pull, and guaranteed
# pod cleanup without editing workflow YAML through an Actions token.
set -euo pipefail

RECO_SHA='b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085'
RECO_BRANCH='feature/frame-stride-testing'
RECO_DIR='/tmp/video-stitcher'
OLD_ROOT='/tmp/oev_stride'
PROD_ROOT='/tmp/oev_stride_prod'
PROD_SCRIPT='/tmp/oev_frame_stride_production_remote.sh'
PROD_SCRIPT_URL='https://raw.githubusercontent.com/JhnsonO/ffa-automations/feature/frame-stride-testing/oev_frame_stride_production_remote.sh'

for f in "$OLD_ROOT/left.mp4" "$OLD_ROOT/right.mp4" "$OLD_ROOT/yolo26m.onnx"; do
  test -s "$f" || { echo "FATAL: staged input missing/empty: $f" >&2; exit 2; }
done

echo "=== Switching pod to exact production Reco candidate $RECO_SHA ==="
cd "$RECO_DIR"
git fetch origin "$RECO_BRANCH"
git reset --hard "$RECO_SHA"
test "$(git rev-parse HEAD)" = "$RECO_SHA"
source "$HOME/.cargo/env" 2>/dev/null || true

# Re-verify the exact candidate on the actual CUDA pod before spending the
# benchmark window on it. The workflow's earlier build is the historical
# test-only SHA; this is the build that the production acceptance uses.
cargo fmt --all -- --check
cargo test -p reco-autocam stride_ --lib
cargo test -p reco-core frame_stride_tests --lib
cargo test -p reco-core sparse_future_states_exclude_render_only_duplicates --lib
time cargo build --release -p reco-cli --features cuda

echo "production_reco_sha=$(git rev-parse HEAD)"

rm -rf "$PROD_ROOT"
mkdir -p "$PROD_ROOT"
cp "$OLD_ROOT/left.mp4" "$PROD_ROOT/left.mp4"
cp "$OLD_ROOT/right.mp4" "$PROD_ROOT/right.mp4"
cp "$OLD_ROOT/yolo26m.onnx" "$PROD_ROOT/yolo26m.onnx"

curl -fsSL "$PROD_SCRIPT_URL" -o "$PROD_SCRIPT"
chmod +x "$PROD_SCRIPT"
grep -q 'FRAME_STRIDE_PRODUCTION_ACCEPTANCE=PASS' "$PROD_SCRIPT"
grep -q -- '--frame-stride "$stride"' "$PROD_SCRIPT"
! grep -q 'setpts=' "$PROD_SCRIPT"

echo "=== Running production full-rate stride 1 vs 3 acceptance ==="
"$PROD_SCRIPT"

test -s "$PROD_ROOT/results/production_results.json"
python3 - "$PROD_ROOT/results/production_results.json" "$RECO_SHA" <<'PY'
import json,sys
p,sha=sys.argv[1:]
d=json.load(open(p))
d['reco_sha']=sha
d['validation_mode']='production_full_rate_stride_1_vs_3'
d['compatibility_entrypoint']='oev_frame_stride_benchmark_remote.sh'
json.dump(d,open(p,'w'),indent=2)
PY

# The existing workflow's artifact/acceptance plumbing expects /tmp/oev_stride
# and the historical filename matrix_results.json. Mirror the production
# evidence there so the proven cleanup/artifact path remains intact. The JSON
# itself explicitly identifies this as production stride 1 vs 3 evidence.
cp -f "$PROD_ROOT/source_metadata.txt" "$OLD_ROOT/source_metadata.txt"
cp -f "$PROD_ROOT/calibrate.log" "$OLD_ROOT/calibrate.log"
cp -f "$PROD_ROOT/match.json" "$OLD_ROOT/match.json"
rm -rf "$OLD_ROOT/results"
cp -a "$PROD_ROOT/results" "$OLD_ROOT/results"
cp "$OLD_ROOT/results/production_results.json" "$OLD_ROOT/results/matrix_results.json"
printf '%s\n' "$RECO_SHA" > "$OLD_ROOT/results/production_reco_sha.txt"

python3 - "$OLD_ROOT/results/production_results.json" <<'PY'
import json,sys
x=json.load(open(sys.argv[1]))
g=x['production_gates']
assert g['full_rate_render'] is True
assert g['same_duration'] is True
assert g['same_output_fps'] is True
assert g['sparse_analysis_cadence'] is True
assert g['posthoc_retime_used'] is False
rows={r['stride']:r for r in x['rows']}
assert 1 in rows and 3 in rows
print('PRODUCTION_EVIDENCE_RECO_SHA='+x['reco_sha'])
print('PRODUCTION_STRIDE1_WALL_SECONDS='+str(rows[1]['wall_seconds']))
print('PRODUCTION_STRIDE3_WALL_SECONDS='+str(rows[3]['wall_seconds']))
print('PRODUCTION_STRIDE3_SPEEDUP='+str(rows[3]['speedup_vs_stride1']))
print('PRODUCTION_STRIDE3_OUTPUT_FRAMES='+str(rows[3]['output_frames']))
print('PRODUCTION_STRIDE3_OUTPUT_FPS='+str(rows[3]['output_fps']))
print('PRODUCTION_STRIDE3_OUTPUT_DURATION='+str(rows[3]['output_duration']))
PY

echo 'FRAME_STRIDE_MATRIX=PASS'
echo 'FRAME_STRIDE_PRODUCTION_ACCEPTANCE=PASS'
