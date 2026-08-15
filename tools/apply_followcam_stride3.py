from pathlib import Path
p=Path('runpod_followcam_remote.sh')
t=p.read_text()
old='''# --detection-interval 1 (no frame-skipping, out of scope for this
# ticket). Deliberately NO --allow-no-tracking: a tracking-init failure
# must fail this run loudly, not silently degrade to a static stitch.
'''
new='''# --detection-interval 1 keeps detector calls on every sparse ANALYSIS frame.
# --frame-stride 3 is the production cadence: AI/tracker/panner decisions run
# at ~20 Hz on 59.94fps source footage while Reco still renders every source
# frame at the normal output FPS. Deliberately NO --allow-no-tracking: a
# tracking-init failure must fail this run loudly, not silently degrade.
'''
if t.count(old)!=1: raise SystemExit(f'comment match count={t.count(old)}')
t=t.replace(old,new,1)
old='''  --lookahead 1.5
  --detection-interval 1
  --events events.jsonl
'''
new='''  --lookahead 1.5
  --detection-interval 1
  --frame-stride 3
  --events events.jsonl
'''
if t.count(old)!=1: raise SystemExit(f'arg match count={t.count(old)}')
t=t.replace(old,new,1)
p.write_text(t)
print('wired normal RunPod follow-cam to --frame-stride 3')
