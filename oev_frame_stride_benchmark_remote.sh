#!/usr/bin/env bash
# Compatibility entrypoint for the existing proven RunPod frame-stride workflow.
# Actual validation: exact Reco b2fc622, full-rate stride 1 vs stride 3, no retime.
set -Eeuo pipefail

RECO_SHA='b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085'
RECO_BRANCH='feature/frame-stride-testing'
RECO_DIR='/tmp/video-stitcher'
OLD_ROOT='/tmp/oev_stride'
PROD_ROOT='/tmp/oev_stride_prod'
PROD_SCRIPT='/tmp/oev_frame_stride_production_remote.sh'
PROD_SCRIPT_URL='https://raw.githubusercontent.com/JhnsonO/ffa-automations/feature/frame-stride-testing/oev_frame_stride_production_remote.sh'

mirror_evidence() {
  mkdir -p "$OLD_ROOT"
  for f in source_metadata.txt calibrate.log match.json; do
    [ -f "$PROD_ROOT/$f" ] && cp -f "$PROD_ROOT/$f" "$OLD_ROOT/$f" || true
  done
  if [ -d "$PROD_ROOT/results" ]; then
    rm -rf "$OLD_ROOT/results"
    cp -a "$PROD_ROOT/results" "$OLD_ROOT/results"
    printf '%s\n' "$RECO_SHA" > "$OLD_ROOT/results/production_reco_sha.txt"
    if [ -s "$OLD_ROOT/results/production_results.json" ]; then
      cp "$OLD_ROOT/results/production_results.json" "$OLD_ROOT/results/matrix_results.json"
    else
      # Existing workflow acceptance/artifact plumbing expects this historical
      # name. A partial marker keeps diagnostics in the artifact without ever
      # pretending the production gates passed.
      python3 - "$OLD_ROOT/results/matrix_results.json" "$RECO_SHA" <<'PY'
import json,sys
p,sha=sys.argv[1:]
json.dump({'status':'partial_or_failed','reco_sha':sha,'validation_mode':'production_full_rate_stride_1_vs_3'},open(p,'w'),indent=2)
PY
    fi
  fi
}
trap mirror_evidence EXIT

for f in "$OLD_ROOT/left.mp4" "$OLD_ROOT/right.mp4" "$OLD_ROOT/yolo26m.onnx"; do
  test -s "$f" || { echo "FATAL: staged input missing/empty: $f" >&2; exit 2; }
done

echo "=== Switching pod to exact production Reco candidate $RECO_SHA ==="
cd "$RECO_DIR"
git fetch origin "$RECO_BRANCH"
git reset --hard "$RECO_SHA"
test "$(git rev-parse HEAD)" = "$RECO_SHA"
source "$HOME/.cargo/env" 2>/dev/null || true
cargo fmt --all -- --check
cargo test -p reco-autocam stride_ --lib
cargo test -p reco-core frame_stride_tests --lib
cargo test -p reco-core sparse_future_states_exclude_render_only_duplicates --lib
time cargo build --release -p reco-cli --features cuda
printf 'production_reco_sha=%s\n' "$(git rev-parse HEAD)"

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
set +e
"$PROD_SCRIPT"
prod_rc=$?
set -e
mirror_evidence
[ "$prod_rc" -eq 0 ] || { echo "FATAL: production acceptance script exit=$prod_rc" >&2; exit "$prod_rc"; }

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
mirror_evidence

python3 - "$OLD_ROOT/results/production_results.json" <<'PY'
import json,sys
x=json.load(open(sys.argv[1])); g=x['production_gates']; rows={r['stride']:r for r in x['rows']}
for key in ('full_rate_render','same_duration','same_output_fps','sparse_analysis_cadence','full_rate_pose_presentation'):
    assert g[key] is True, key
assert g['posthoc_retime_used'] is False
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
