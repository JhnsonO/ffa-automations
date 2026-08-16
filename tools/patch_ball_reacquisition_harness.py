from pathlib import Path

RECO_SHA = "160c1aac8a59f83b9c6ccdfcc57b69bd8a598432"
RECO_BRANCH = "agent/high-res-ball-roi-recovery"

# Pin the baseline workflow's bootstrap revision gate to the exact PR head.
p = Path('.github/workflows/oev-runpod-sample-baseline.yml')
t = p.read_text()
old = "EXPECTED_RECO_SHA='b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085'"
new = f"EXPECTED_RECO_SHA='{RECO_SHA}'"
if t.count(old) != 1:
    raise SystemExit(f'workflow Reco gate match count={t.count(old)}')
p.write_text(t.replace(old, new, 1))

# Keep the standard bootstrap, but force the exact draft-PR revision after
# clone/fetch so the paid run cannot silently execute main instead.
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

# Treatment: accepted stride-1 v4 cadence, high-resolution recovery enabled.
p = Path('runpod_sample_baseline_yolo26_remote.sh')
t = p.read_text()
old = '''  --detection-interval 1
  --frame-stride 3
  --events events.jsonl'''
new = '''  --detection-interval 1
  --high-res-ball-recovery
  --events events.jsonl'''
if t.count(old) != 1:
    raise SystemExit(f'runner stride/recovery anchor match count={t.count(old)}')
t = t.replace(old, new, 1)
t = t.replace(
    '# --detection-interval 1 (no frame-skipping, out of scope for this\n# ticket).',
    '# --detection-interval 1 and no --frame-stride flag: treatment is stride 1.\n# --high-res-ball-recovery is the only detector/recovery treatment flag.',
    1,
)
p.write_text(t)

print(f'patched treatment harness for Reco {RECO_SHA}')
# Retry marker: use GH_PAT-backed checkout so workflow-file updates can be pushed.
