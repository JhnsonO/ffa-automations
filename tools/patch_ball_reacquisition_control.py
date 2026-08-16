from pathlib import Path

RECO_SHA = "160c1aac8a59f83b9c6ccdfcc57b69bd8a598432"
RECO_BRANCH = "agent/high-res-ball-roi-recovery"

p = Path('.github/workflows/oev-runpod-sample-baseline.yml')
t = p.read_text()
old = "EXPECTED_RECO_SHA='b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085'"
new = f"EXPECTED_RECO_SHA='{RECO_SHA}'"
if t.count(old) != 1:
    raise SystemExit(f'workflow Reco gate match count={t.count(old)}')
p.write_text(t.replace(old, new, 1))

p = Path('runpod_bootstrap.sh')
t = p.read_text()
anchor = '''fi
REPO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)
log_version "video-stitcher_sha" "$REPO_SHA"
'''
replacement = f'''fi
git -C "$WORKDIR" fetch origin {RECO_BRANCH} || fail "git fetch of recovery branch failed" 3
git -C "$WORKDIR" reset --hard {RECO_SHA} || fail "git reset to recovery revision failed" 3
REPO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)
log_version "video-stitcher_sha" "$REPO_SHA"
'''
if t.count(anchor) != 1:
    raise SystemExit(f'bootstrap revision anchor match count={t.count(anchor)}')
p.write_text(t.replace(anchor, replacement, 1))

# Control: same Reco revision and accepted stride-1 v4 cadence, recovery disabled.
p = Path('runpod_sample_baseline_yolo26_remote.sh')
t = p.read_text()
old = '''  --detection-interval 1
  --frame-stride 3
  --events events.jsonl'''
new = '''  --detection-interval 1
  --events events.jsonl'''
if t.count(old) != 1:
    raise SystemExit(f'runner stride anchor match count={t.count(old)}')
p.write_text(t.replace(old, new, 1))
print(f'patched control harness for Reco {RECO_SHA}')
