from pathlib import Path
import sys

if len(sys.argv) != 2 or len(sys.argv[1]) != 40:
    raise SystemExit('usage: prepare_stride_production_workflow_v2.py <40-char reco sha>')
sha=sys.argv[1]
p=Path('.github/workflows/oev-frame-stride-diag.yml')
t=p.read_text()

def one(old,new):
    global t
    n=t.count(old)
    if n != 1:
        raise SystemExit(f'expected exactly one match ({n}): {old[:120]!r}')
    t=t.replace(old,new,1)

one('name: OEV — Frame Stride Diagnostic','name: OEV — Production Frame Stride Acceptance')
one('      - oev_frame_stride_benchmark_remote.sh','      - oev_frame_stride_production_remote.sh')
one('      RECO_SHA: c647cd101370887eabdf50059dd70eea3d9e730c',f'      RECO_SHA: {sha}')
one('      - name: Build exact frame-stride Reco revision','      - name: Build exact production frame-stride Reco revision')
one('cargo fmt --all; cargo test -p reco-autocam stride_ --lib; time cargo build --release -p reco-cli --features cuda',
    'cargo fmt --all -- --check; cargo test -p reco-autocam stride_ --lib; cargo test -p reco-core frame_stride_tests --lib; cargo test -p reco-core sparse_future_states_exclude_render_only_duplicates --lib; time cargo build --release -p reco-cli --features cuda')
# Production harness has its own root. Global replacement is intentional only in this diagnostic workflow.
t=t.replace('/tmp/oev_stride','/tmp/oev_stride_prod')
one('''          SRC="/runpod-volume/oev-samples/$SAMPLE_SET_ID"
          LEFT_SRC="$SRC/${SAMPLE_ID}_left_${DURATION_S}s.mp4"
          RIGHT_SRC="$SRC/${SAMPLE_ID}_right_${DURATION_S}s.mp4"
''','''          SRC="/runpod-volume/oev-samples/$SAMPLE_SET_ID"
          SAMPLE_DIR="$SRC/$SAMPLE_ID"
          LEFT_SRC="$SAMPLE_DIR/${SAMPLE_ID}_left_${DURATION_S}s.mp4"
          RIGHT_SRC="$SAMPLE_DIR/${SAMPLE_ID}_right_${DURATION_S}s.mp4"
''')
one("echo '--- sample directory ---'; ls -lah '$SRC' 2>&1 | head -40;", "echo '--- sample directory ---'; ls -lah '$SAMPLE_DIR' 2>&1 | head -40;")
one('      - name: Run stride 1/2/3/4 matrix on same pod','      - name: Run production stride 1 vs 3 acceptance on same pod')
one('$SCP oev_frame_stride_benchmark_remote.sh root@$IP:/tmp/oev_frame_stride_benchmark_remote.sh', '$SCP oev_frame_stride_production_remote.sh root@$IP:/tmp/oev_frame_stride_production_remote.sh')
one('chmod +x /tmp/oev_frame_stride_benchmark_remote.sh;', 'chmod +x /tmp/oev_frame_stride_production_remote.sh;')
one('stdbuf -oL -eL /tmp/oev_frame_stride_benchmark_remote.sh','stdbuf -oL -eL /tmp/oev_frame_stride_production_remote.sh')
t=t.replace('matrix_results.json','production_results.json')
one('name: oev-frame-stride-${{ github.run_id }}','name: oev-frame-stride-production-${{ github.run_id }}')
one('Frame-stride matrix did not complete cleanly','Production frame-stride acceptance did not complete cleanly')
one('Frame-stride diagnostic matrix completed and evidence artifact exists.','Production stride 1 vs 3 acceptance completed and evidence artifact exists.')
t=t.replace('oev-frame-stride-diag','oev-frame-stride-production')
# Sanity gates: no legacy test-only control/old remote script should be referenced.
if 'oev_frame_stride_benchmark_remote.sh' in t:
    raise SystemExit('legacy remote benchmark reference remains')
if f'RECO_SHA: {sha}' not in t:
    raise SystemExit('exact Reco SHA not pinned')
if 'production_results.json' not in t:
    raise SystemExit('production result acceptance not wired')
p.write_text(t)
print(f'production workflow prepared for {sha}')
