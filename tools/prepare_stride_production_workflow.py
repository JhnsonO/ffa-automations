from pathlib import Path
import sys

if len(sys.argv) != 2 or len(sys.argv[1]) != 40:
    raise SystemExit('usage: prepare_stride_production_workflow.py <40-char reco sha>')
sha = sys.argv[1]
p = Path('.github/workflows/oev-frame-stride-diag.yml')
t = p.read_text()

def rep(old, new, count=1):
    global t
    found=t.count(old)
    if found < count:
        raise SystemExit(f'expected >= {count}, found {found}: {old[:100]!r}')
    t=t.replace(old,new,count)

rep('name: OEV — Frame Stride Diagnostic', 'name: OEV — Production Frame Stride Acceptance')
rep('      - oev_frame_stride_benchmark_remote.sh', '      - oev_frame_stride_production_remote.sh')
rep('      RECO_SHA: c647cd101370887eabdf50059dd70eea3d9e730c', f'      RECO_SHA: {sha}')
rep('      - name: Build exact frame-stride Reco revision', '      - name: Build exact production frame-stride Reco revision')
old_build='''          $SSH "set -euo pipefail; cd /tmp/video-stitcher; git fetch origin '$RECO_BRANCH'; git reset --hard '$RECO_SHA'; test \\\"\\$(git rev-parse HEAD)\\\" = '$RECO_SHA'; source \\\"\\$HOME/.cargo/env\\\" 2>/dev/null || true; cargo fmt --all; cargo test -p reco-autocam stride_ --lib; time cargo build --release -p reco-cli --features cuda 2>&1 | tee /tmp/frame_stride_build.log; test \\\"\\${PIPESTATUS[0]}\\\" -eq 0; echo video-stitcher_sha=\\$(git rev-parse HEAD) > /tmp/frame_stride_versions.log"
'''
new_build='''          $SSH "set -euo pipefail; cd /tmp/video-stitcher; git fetch origin '$RECO_BRANCH'; git reset --hard '$RECO_SHA'; test \\\"\\$(git rev-parse HEAD)\\\" = '$RECO_SHA'; source \\\"\\$HOME/.cargo/env\\\" 2>/dev/null || true; cargo fmt --all -- --check; cargo test -p reco-autocam stride_ --lib; cargo test -p reco-core frame_stride_tests --lib; cargo test -p reco-core sparse_future_states_exclude_render_only_duplicates --lib; time cargo build --release -p reco-cli --features cuda 2>&1 | tee /tmp/frame_stride_build.log; test \\\"\\${PIPESTATUS[0]}\\\" -eq 0; echo video-stitcher_sha=\\$(git rev-parse HEAD) > /tmp/frame_stride_versions.log"
'''
rep(old_build,new_build)
# Production script uses a separate root; keep the old benchmark evidence untouched.
t=t.replace('/tmp/oev_stride', '/tmp/oev_stride_prod')
# Fix the actual network-volume sample layout: set/sample/file.
rep('''          SRC="/runpod-volume/oev-samples/$SAMPLE_SET_ID"
          LEFT_SRC="$SRC/${SAMPLE_ID}_left_${DURATION_S}s.mp4"
          RIGHT_SRC="$SRC/${SAMPLE_ID}_right_${DURATION_S}s.mp4"
''','''          SRC="/runpod-volume/oev-samples/$SAMPLE_SET_ID"
          SAMPLE_DIR="$SRC/$SAMPLE_ID"
          LEFT_SRC="$SAMPLE_DIR/${SAMPLE_ID}_left_${DURATION_S}s.mp4"
          RIGHT_SRC="$SAMPLE_DIR/${SAMPLE_ID}_right_${DURATION_S}s.mp4"
''')
rep("echo '--- sample directory ---'; ls -lah '$SRC' 2>&1 | head -40;", "echo '--- sample directory ---'; ls -lah '$SAMPLE_DIR' 2>&1 | head -40;")
rep('      - name: Run stride 1/2/3/4 matrix on same pod', '      - name: Run production stride 1 vs 3 acceptance on same pod')
rep('$SCP oev_frame_stride_benchmark_remote.sh root@$IP:/tmp/oev_frame_stride_benchmark_remote.sh', '$SCP oev_frame_stride_production_remote.sh root@$IP:/tmp/oev_frame_stride_production_remote.sh')
rep('chmod +x /tmp/oev_frame_stride_benchmark_remote.sh;', 'chmod +x /tmp/oev_frame_stride_production_remote.sh;')
rep('stdbuf -oL -eL /tmp/oev_frame_stride_benchmark_remote.sh', 'stdbuf -oL -eL /tmp/oev_frame_stride_production_remote.sh')
t=t.replace('matrix_results.json','production_results.json')
rep('name: oev-frame-stride-${{ github.run_id }}', 'name: oev-frame-stride-production-${{ github.run_id }}')
rep('Frame-stride matrix did not complete cleanly', 'Production frame-stride acceptance did not complete cleanly')
rep('Frame-stride diagnostic matrix completed and evidence artifact exists.', 'Production stride 1 vs 3 acceptance completed and evidence artifact exists.')
# Keep the historical workflow filename but make the artifact/pod labels unambiguous.
t=t.replace('oev-frame-stride-diag','oev-frame-stride-production')
p.write_text(t)
print(f'prepared production stride workflow for Reco {sha}')
