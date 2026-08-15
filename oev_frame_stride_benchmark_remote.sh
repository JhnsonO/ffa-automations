#!/usr/bin/env bash
# Testing-only OEV full-pipeline frame-stride benchmark.
# Runs on one already-preflighted RunPod pod after runpod_bootstrap.sh.
# It expects the exact 30s sample pair and yolo26m model to already exist
# under /tmp/oev_stride/. Calibration is performed once, then strides
# 1/2/3/4 run on the same GPU with identical settings.
#
# RECO_TEST_FRAME_STRIDE is intentionally the only Reco behavior switch.
# Detection interval remains 1: every *processed* sparse frame is detected.
# Lookahead seconds passed to Reco are divided by stride because StitchJob
# converts seconds to source-FPS frame count before the source stride is
# applied. This preserves ~1.5 seconds of represented match time.
set -euo pipefail

ROOT=/tmp/oev_stride
RECO=/tmp/video-stitcher/target/release/reco
MODEL="$ROOT/yolo26m.onnx"
SOURCE_LOOKAHEAD=1.5
mkdir -p "$ROOT/results"
cd "$ROOT"

for f in left.mp4 right.mp4 "$MODEL"; do
  test -s "$f" || { echo "FATAL: required input missing/empty: $f" >&2; exit 2; }
done
test -x "$RECO" || { echo "FATAL: Reco binary missing: $RECO" >&2; exit 2; }

# Source truth used for represented-frame accounting.
SOURCE_FPS=$(ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of default=nw=1:nk=1 left.mp4)
SOURCE_FRAMES=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1:nk=1 left.mp4)
RIGHT_FRAMES=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1:nk=1 right.mp4)
SOURCE_DURATION=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 left.mp4)
if [ -z "$SOURCE_FRAMES" ] || [ "$SOURCE_FRAMES" = N/A ] || [ -z "$RIGHT_FRAMES" ] || [ "$RIGHT_FRAMES" = N/A ]; then
  echo "FATAL: ffprobe could not count source frames" >&2
  exit 2
fi
if [ "$SOURCE_FRAMES" -ne "$RIGHT_FRAMES" ]; then
  echo "FATAL: staged stereo sample frame counts differ: left=$SOURCE_FRAMES right=$RIGHT_FRAMES" >&2
  exit 2
fi
cat > source_metadata.txt <<META
source_fps=$SOURCE_FPS
source_frames=$SOURCE_FRAMES
source_duration=$SOURCE_DURATION
right_frames=$RIGHT_FRAMES
META
cat source_metadata.txt

# Same pinned Hero10 Wide profile + St Margaret's field ROI as the existing
# sample-baseline harness. Calibrate once so the four stride runs differ only
# by the stride/timing adaptation under test.
LENS_PROFILE_URL='https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json'
curl -fsSL "$LENS_PROFILE_URL" -o hero10_wide_16by9.json
stdbuf -oL -eL "$RECO" calibrate left.mp4 right.mp4 \
  --left-profile hero10_wide_16by9.json \
  --right-profile hero10_wide_16by9.json \
  -o match.json 2>&1 | tee calibrate.log
python3 - <<'PY'
import json
p='match.json'
m=json.load(open(p))
m['field_roi']={
 'left':[[0.1227,0.9611],[0.0573,0.6846],[0.1802,0.6285],[0.2645,0.5769],[0.4382,0.4864],[0.4988,0.4658],[0.5942,0.4474],[0.7835,0.4175],[0.9285,0.3785],[1.0,1.0],[0.1227,1.0]],
 'right':[[0.0391,0.4206],[0.0818,0.4101],[0.1839,0.4070],[0.2783,0.4070],[0.3448,0.4083],[0.4100,0.4161],[0.4684,0.4319],[0.6239,0.4801],[0.7368,0.5200],[0.7980,0.5465],[0.7454,0.9011],[0.7454,1.0],[0.0,1.0]],
}
json.dump(m,open(p,'w'),indent=2)
PY

# Measure only reco stitch. Retiming/stills/analysis happen afterwards and are
# excluded from per-stride wall time/cost.
for stride in 1 2 3 4; do
  out="$ROOT/results/stride_${stride}"
  mkdir -p "$out"
  lookahead=$(python3 - "$stride" <<'PY'
import sys
print(f"{1.5/int(sys.argv[1]):.6f}")
PY
)
  echo "=== STRIDE $stride: represented source=$SOURCE_FRAMES frames, lookahead_arg=${lookahead}s ===" | tee "$out/benchmark.log"
  start_ns=$(date +%s%N)
  set +e
  RECO_TEST_FRAME_STRIDE="$stride" stdbuf -oL -eL "$RECO" stitch left.mp4 right.mp4 \
    -c match.json -o "$out/raw.mp4" \
    --model "$MODEL" \
    --tracking field \
    --panner-preset broadcast \
    --lookahead "$lookahead" \
    --detection-interval 1 \
    --events "$out/events.jsonl" \
    --width 1920 --height 1080 \
    2>&1 | tee "$out/stitch.log"
  rc=${PIPESTATUS[0]}
  set -e
  end_ns=$(date +%s%N)
  wall_s=$(python3 - "$start_ns" "$end_ns" <<'PY'
import sys
print(f"{(int(sys.argv[2])-int(sys.argv[1]))/1e9:.6f}")
PY
)
  echo "wall_seconds=$wall_s" | tee -a "$out/benchmark.log"
  echo "lookahead_arg_seconds=$lookahead" | tee -a "$out/benchmark.log"
  if [ "$rc" -ne 0 ]; then
    echo "FATAL: stride $stride Reco exit $rc" | tee -a "$out/benchmark.log"
    exit 10
  fi
  test -s "$out/raw.mp4" && test -s "$out/events.jsonl" || { echo "FATAL: stride $stride output missing" >&2; exit 11; }

  # Acceptance evidence: AI active, shared-buffer zero-copy, NVDEC/CUDA EP.
  grep -q 'Autocam: tracking enabled' "$out/stitch.log" || { echo "FATAL: stride $stride tracking not active" >&2; exit 12; }
  grep -qiE 'GPU zero-copy|zero-copy|zero copy' "$out/stitch.log" || { echo "FATAL: stride $stride zero-copy evidence missing" >&2; exit 12; }
  grep -qiE 'NVDEC.*CUDA|NVDEC \(CUDA\)|cuvid' "$out/stitch.log" || { echo "FATAL: stride $stride NVDEC evidence missing" >&2; exit 12; }
  grep -q 'CUDAExecutionProvider' "$out/stitch.log" || { echo "FATAL: stride $stride CUDA EP evidence missing" >&2; exit 12; }
  if grep -q 'No execution providers from session options registered successfully' "$out/stitch.log"; then
    echo "FATAL: stride $stride CUDA EP fallback detected" >&2
    exit 12
  fi
  if [ "$stride" -gt 1 ]; then
    grep -q "RECO_TEST_FRAME_STRIDE=$stride" "$out/stitch.log" || { echo "FATAL: stride $stride source-stride log missing" >&2; exit 12; }
    grep -q "stride=$stride" "$out/stitch.log" || { echo "FATAL: stride $stride autocam timing log missing" >&2; exit 12; }
  fi

  processed=$(python3 - "$out/events.jsonl" <<'PY'
import json,sys
n=0
for line in open(sys.argv[1]):
    try: e=json.loads(line)
    except Exception: continue
    if e.get('kind')=='world_state': n+=1
print(n)
PY
)
  expected=$(( (SOURCE_FRAMES + stride - 1) / stride ))
  echo "processed_frames=$processed" | tee -a "$out/benchmark.log"
  echo "expected_processed_frames=$expected" | tee -a "$out/benchmark.log"
  if [ "$processed" -lt $(( expected - 1 )) ] || [ "$processed" -gt $(( expected + 1 )) ]; then
    echo "FATAL: stride $stride processed $processed frames; expected ~${expected} from source $SOURCE_FRAMES" >&2
    exit 13
  fi

  # Build a real-time visual diagnostic outside the measured stitch window.
  # Sparse frames are re-timestamped by stride so playback spans the same match time.
  ffmpeg -y -v error -i "$out/raw.mp4" -vf "setpts=${stride}*PTS" -fps_mode vfr \
    -c:v h264_nvenc -preset p1 -an "$out/realtime.mp4"
  raw_dur=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$out/raw.mp4")
  real_dur=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$out/realtime.mp4")
  echo "raw_output_duration=$raw_dur" | tee -a "$out/benchmark.log"
  echo "realtime_output_duration=$real_dur" | tee -a "$out/benchmark.log"

  # Same real-time positions from each output for later visual inspection.
  mkdir -p "$out/stills"
  for t in 3 9 15 24; do
    ffmpeg -y -v error -ss "$t" -i "$out/realtime.mp4" -frames:v 1 "$out/stills/t${t}.jpg"
  done
done

# Compare each sparse result against stride 1 at its corresponding original
# source-frame position: sparse processed index i maps to baseline i*stride.
python3 - <<'PY'
import json, math, os, re, statistics
from pathlib import Path
root=Path('/tmp/oev_stride/results')
source={}
for line in open('/tmp/oev_stride/source_metadata.txt'):
    if '=' in line:
        k,v=line.strip().split('=',1); source[k]=v
source_frames=int(source['source_frames'])

def load_events(stride):
    worlds={}; pans={}
    for line in open(root/f'stride_{stride}'/'events.jsonl'):
        try: e=json.loads(line)
        except Exception: continue
        i=e.get('frame_index')
        if e.get('kind')=='world_state': worlds[i]=e
        elif e.get('kind')=='pan_decision': pans[i]=e.get('pose') or {}
    return worlds,pans

def pct(xs,p):
    if not xs: return None
    ys=sorted(xs); idx=max(0,min(len(ys)-1,math.ceil(p*len(ys))-1)); return ys[idx]

def parse_wall(stride):
    text=(root/f'stride_{stride}'/'benchmark.log').read_text()
    return float(re.search(r'wall_seconds=([0-9.]+)',text).group(1))

def parse_summary(stride):
    text=(root/f'stride_{stride}'/'stitch.log').read_text(errors='replace')
    out={}
    m=re.search(r'Frames:\s+(\d+) processed',text); out['summary_frames']=int(m.group(1)) if m else None
    m=re.search(r'Duration:\s+([0-9.]+)s',text); out['session_duration_s']=float(m.group(1)) if m else None
    m=re.search(r'FPS:\s+([0-9.]+) avg',text); out['session_avg_fps']=float(m.group(1)) if m else None
    for key,label in [('decode_ms','Decode'),('stitch_ms','Stitch'),('readback_ms','Readback'),('submit_ms','Submit'),('encode_ms','Encode'),('total_ms','Total')]:
        m=re.search(rf'{label}:\s+([0-9.]+) ms',text)
        out[key]=float(m.group(1)) if m else None
    return out

base_w,base_p=load_events(1)
base_wall=parse_wall(1)
rows=[]
for stride in (1,2,3,4):
    worlds,pans=load_events(stride)
    ball_err=[]; pan_yaw_err=[]; pan_fov_err=[]
    baseline_ball_present=0; sparse_ball_present=0; lost_vs_baseline=0
    track=coast=lost=missing=0
    rapid_pan_err=[]
    # Baseline rapid-transition threshold from matched baseline yaw step sizes.
    matched_base_indices=[i*stride for i in sorted(worlds)]
    base_steps=[]
    for bi in matched_base_indices:
        if bi in base_p and bi-stride in base_p and 'yaw' in base_p[bi] and 'yaw' in base_p[bi-stride]:
            base_steps.append(abs(base_p[bi]['yaw']-base_p[bi-stride]['yaw']))
    rapid_threshold=pct(base_steps,0.90) or 0.0
    for i,w in worlds.items():
        bi=i*stride
        bw=base_w.get(bi)
        bball=(bw or {}).get('ball')
        sball=w.get('ball')
        if bball is not None: baseline_ball_present+=1
        if sball is not None: sparse_ball_present+=1
        if bball is not None and sball is None: lost_vs_baseline+=1
        if sball is None:
            missing+=1
        else:
            state=(sball.get('state') or '').lower()
            if state=='tracking': track+=1
            elif state=='coasting': coast+=1
            elif state=='lost': lost+=1
        if bball is not None and sball is not None:
            ball_err.append(math.hypot(float(sball['yaw'])-float(bball['yaw']), float(sball['pitch'])-float(bball['pitch'])))
        sp=pans.get(i); bp=base_p.get(bi)
        if sp and bp and sp.get('yaw') is not None and bp.get('yaw') is not None:
            pe=abs(float(sp['yaw'])-float(bp['yaw'])); pan_yaw_err.append(pe)
            if i>0 and bi-stride in base_p and base_p[bi-stride].get('yaw') is not None:
                if abs(float(bp['yaw'])-float(base_p[bi-stride]['yaw'])) >= rapid_threshold:
                    rapid_pan_err.append(pe)
            if sp.get('fov_degrees') is not None and bp.get('fov_degrees') is not None:
                pan_fov_err.append(abs(float(sp['fov_degrees'])-float(bp['fov_degrees'])))
    wall=parse_wall(stride); summ=parse_summary(stride)
    n=len(worlds)
    row={
      'stride':stride,'source_frames_represented':source_frames,'processed_frames':n,
      'wall_seconds':wall,'effective_processed_fps':n/wall if wall else None,
      'speedup_vs_stride1':base_wall/wall if wall else None,
      'ball_mean_delta_rad':statistics.fmean(ball_err) if ball_err else None,
      'ball_p90_delta_rad':pct(ball_err,0.90),
      'ball_baseline_present_samples':baseline_ball_present,
      'ball_sparse_present_samples':sparse_ball_present,
      'lost_vs_baseline_count':lost_vs_baseline,
      'lost_vs_baseline_pct':100*lost_vs_baseline/baseline_ball_present if baseline_ball_present else None,
      'tracking_frames':track,'coasting_frames':coast,'lost_frames':lost,'missing_ball_frames':missing,
      'pan_mean_yaw_delta_rad':statistics.fmean(pan_yaw_err) if pan_yaw_err else None,
      'pan_p90_yaw_delta_rad':pct(pan_yaw_err,0.90),
      'rapid_transition_p90_yaw_delta_rad':pct(rapid_pan_err,0.90),
      'fov_mean_delta_deg':statistics.fmean(pan_fov_err) if pan_fov_err else None,
      **summ,
    }
    rows.append(row)
json.dump({'source':source,'rows':rows},open(root/'matrix_results.json','w'),indent=2)
with open(root/'matrix_results.tsv','w') as f:
    keys=list(rows[0]); f.write('\t'.join(keys)+'\n')
    for r in rows: f.write('\t'.join('' if r[k] is None else str(r[k]) for k in keys)+'\n')
print(json.dumps(rows,indent=2))
PY

echo 'FRAME_STRIDE_MATRIX=PASS'
