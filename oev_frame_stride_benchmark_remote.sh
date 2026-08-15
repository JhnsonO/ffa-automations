#!/usr/bin/env bash
# Testing-only OEV full-pipeline frame-stride benchmark.
# One preflighted pod, one calibrated 30s stereo sample, strides 1/2/3/4.
set -euo pipefail

ROOT=/tmp/oev_stride
RECO=/tmp/video-stitcher/target/release/reco
MODEL="$ROOT/yolo26m.onnx"
mkdir -p "$ROOT/results"
cd "$ROOT"
for f in left.mp4 right.mp4 "$MODEL"; do
  test -s "$f" || { echo "FATAL: required input missing/empty: $f" >&2; exit 2; }
done
test -x "$RECO" || { echo "FATAL: Reco binary missing: $RECO" >&2; exit 2; }

SOURCE_FPS=$(ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of default=nw=1:nk=1 left.mp4)
SOURCE_FRAMES=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1:nk=1 left.mp4)
RIGHT_FRAMES=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1:nk=1 right.mp4)
SOURCE_DURATION=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 left.mp4)
if [ -z "$SOURCE_FRAMES" ] || [ "$SOURCE_FRAMES" = N/A ] || [ -z "$RIGHT_FRAMES" ] || [ "$RIGHT_FRAMES" = N/A ]; then
  echo "FATAL: ffprobe could not count source frames" >&2; exit 2
fi
if [ "$SOURCE_FRAMES" -ne "$RIGHT_FRAMES" ]; then
  echo "FATAL: staged stereo container counts differ: left=$SOURCE_FRAMES right=$RIGHT_FRAMES" >&2; exit 2
fi
cat > source_metadata.txt <<META
source_fps=$SOURCE_FPS
container_source_frames=$SOURCE_FRAMES
source_duration=$SOURCE_DURATION
right_container_frames=$RIGHT_FRAMES
META
cat source_metadata.txt

LENS_PROFILE_URL='https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json'
curl -fsSL "$LENS_PROFILE_URL" -o hero10_wide_16by9.json
stdbuf -oL -eL "$RECO" calibrate left.mp4 right.mp4 \
  --left-profile hero10_wide_16by9.json --right-profile hero10_wide_16by9.json \
  -o match.json 2>&1 | tee calibrate.log
python3 - <<'PY'
import json
p='match.json'; m=json.load(open(p))
m['field_roi']={
 'left':[[0.1227,0.9611],[0.0573,0.6846],[0.1802,0.6285],[0.2645,0.5769],[0.4382,0.4864],[0.4988,0.4658],[0.5942,0.4474],[0.7835,0.4175],[0.9285,0.3785],[1.0,1.0],[0.1227,1.0]],
 'right':[[0.0391,0.4206],[0.0818,0.4101],[0.1839,0.4070],[0.2783,0.4070],[0.3448,0.4083],[0.4100,0.4161],[0.4684,0.4319],[0.6239,0.4801],[0.7368,0.5200],[0.7980,0.5465],[0.7454,0.9011],[0.7454,1.0],[0.0,1.0]],
}
json.dump(m,open(p,'w'),indent=2)
PY

CONTROL_FRAMES=""
for stride in 1 2 3 4; do
  out="$ROOT/results/stride_${stride}"; mkdir -p "$out"
  lookahead=$(python3 - "$stride" <<'PY'
import sys
print(f"{1.5/int(sys.argv[1]):.6f}")
PY
)
  echo "=== STRIDE $stride: container_source=$SOURCE_FRAMES, lookahead_arg=${lookahead}s ===" | tee "$out/benchmark.log"
  start_ns=$(date +%s%N)
  set +e
  RECO_TEST_FRAME_STRIDE="$stride" stdbuf -oL -eL "$RECO" stitch left.mp4 right.mp4 \
    -c match.json -o "$out/raw.mp4" --model "$MODEL" --tracking field \
    --panner-preset broadcast --lookahead "$lookahead" --detection-interval 1 \
    --events "$out/events.jsonl" --width 1920 --height 1080 2>&1 | tee "$out/stitch.log"
  rc=${PIPESTATUS[0]}; set -e
  end_ns=$(date +%s%N)
  wall_s=$(python3 - "$start_ns" "$end_ns" <<'PY'
import sys
print(f"{(int(sys.argv[2])-int(sys.argv[1]))/1e9:.6f}")
PY
)
  echo "wall_seconds=$wall_s" | tee -a "$out/benchmark.log"
  echo "lookahead_arg_seconds=$lookahead" | tee -a "$out/benchmark.log"
  [ "$rc" -eq 0 ] || { echo "FATAL: stride $stride Reco exit $rc" >&2; exit 10; }
  test -s "$out/raw.mp4" && test -s "$out/events.jsonl" || { echo "FATAL: stride $stride output missing" >&2; exit 11; }

  grep -q 'Autocam: tracking enabled' "$out/stitch.log" || { echo "FATAL: stride $stride tracking not active" >&2; exit 12; }
  grep -qiE 'GPU zero-copy|zero-copy|zero copy' "$out/stitch.log" || { echo "FATAL: stride $stride zero-copy evidence missing" >&2; exit 12; }
  grep -qiE 'NVDEC.*CUDA|NVDEC \(CUDA\)|cuvid' "$out/stitch.log" || { echo "FATAL: stride $stride NVDEC evidence missing" >&2; exit 12; }
  grep -q 'CUDAExecutionProvider' "$out/stitch.log" || { echo "FATAL: stride $stride CUDA EP evidence missing" >&2; exit 12; }
  ! grep -q 'No execution providers from session options registered successfully' "$out/stitch.log" || { echo "FATAL: stride $stride CUDA EP fallback" >&2; exit 12; }
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
    n += e.get('kind') == 'world_state'
print(n)
PY
)
  if [ "$stride" -eq 1 ]; then
    CONTROL_FRAMES="$processed"
    echo "represented_paired_frames=$CONTROL_FRAMES" >> source_metadata.txt
    expected="$CONTROL_FRAMES"
  else
    expected=$(( (CONTROL_FRAMES + stride - 1) / stride ))
  fi
  echo "represented_paired_frames=$CONTROL_FRAMES" | tee -a "$out/benchmark.log"
  echo "processed_frames=$processed" | tee -a "$out/benchmark.log"
  echo "expected_processed_frames=$expected" | tee -a "$out/benchmark.log"
  if [ "$processed" -lt $(( expected - 1 )) ] || [ "$processed" -gt $(( expected + 1 )) ]; then
    echo "FATAL: stride $stride processed $processed; expected ~$expected from stride-1 paired control $CONTROL_FRAMES" >&2; exit 13
  fi

  # Outside measured window: sparse output is retimed to represented match time.
  ffmpeg -y -v error -i "$out/raw.mp4" -vf "setpts=${stride}*PTS" -fps_mode vfr \
    -c:v h264_nvenc -preset p1 -an "$out/realtime.mp4"
  raw_dur=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$out/raw.mp4")
  real_dur=$(ffprobe -v error -show_entries format=duration -of default=nw=1:nk=1 "$out/realtime.mp4")
  echo "raw_output_duration=$raw_dur" | tee -a "$out/benchmark.log"
  echo "realtime_output_duration=$real_dur" | tee -a "$out/benchmark.log"
  mkdir -p "$out/stills"
  for t in 3 9 15 24; do ffmpeg -y -v error -ss "$t" -i "$out/realtime.mp4" -frames:v 1 "$out/stills/t${t}.jpg"; done
done

python3 - <<'PY'
import json,math,re,statistics
from pathlib import Path
root=Path('/tmp/oev_stride/results'); source={}
for line in open('/tmp/oev_stride/source_metadata.txt'):
    if '=' in line:
        k,v=line.strip().split('=',1); source[k]=v
represented=int(source['represented_paired_frames'])

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
    if not xs:return None
    ys=sorted(xs); return ys[max(0,min(len(ys)-1,math.ceil(p*len(ys))-1))]

def wall(s):
    t=(root/f'stride_{s}'/'benchmark.log').read_text(); return float(re.search(r'wall_seconds=([0-9.]+)',t).group(1))

def summary(s):
    t=(root/f'stride_{s}'/'stitch.log').read_text(errors='replace'); out={}
    for key,pat,cast in [
      ('summary_frames',r'Frames:\s+(\d+) processed',int),('session_duration_s',r'Duration:\s+([0-9.]+)s',float),('session_avg_fps',r'FPS:\s+([0-9.]+) avg',float)]:
        m=re.search(pat,t); out[key]=cast(m.group(1)) if m else None
    for key,label in [('decode_ms','Decode'),('stitch_ms','Stitch'),('readback_ms','Readback'),('submit_ms','Submit'),('encode_ms','Encode'),('total_ms','Total')]:
        m=re.search(rf'{label}:\s+([0-9.]+) ms',t); out[key]=float(m.group(1)) if m else None
    return out

base_w,base_p=load_events(1); base_wall=wall(1); rows=[]
for stride in (1,2,3,4):
    worlds,pans=load_events(stride); ball_err=[]; pan_err=[]; fov_err=[]; rapid=[]
    bpresent=spresent=lost_vs=track=coast=lost=missing=0
    base_steps=[]
    for i in sorted(worlds):
        bi=i*stride
        if bi in base_p and bi-stride in base_p and base_p[bi].get('yaw') is not None and base_p[bi-stride].get('yaw') is not None:
            base_steps.append(abs(base_p[bi]['yaw']-base_p[bi-stride]['yaw']))
    rapid_threshold=pct(base_steps,.90) or 0.0
    for i,w in worlds.items():
        bi=i*stride; bw=base_w.get(bi); bb=(bw or {}).get('ball'); sb=w.get('ball')
        bpresent += bb is not None; spresent += sb is not None; lost_vs += bb is not None and sb is None
        if sb is None: missing+=1
        else:
            st=(sb.get('state') or '').lower(); track+=st=='tracking'; coast+=st=='coasting'; lost+=st=='lost'
        if bb is not None and sb is not None:
            ball_err.append(math.hypot(float(sb['yaw'])-float(bb['yaw']),float(sb['pitch'])-float(bb['pitch'])))
        sp=pans.get(i); bp=base_p.get(bi)
        if sp and bp and sp.get('yaw') is not None and bp.get('yaw') is not None:
            pe=abs(float(sp['yaw'])-float(bp['yaw'])); pan_err.append(pe)
            if i>0 and bi-stride in base_p and base_p[bi-stride].get('yaw') is not None and abs(float(bp['yaw'])-float(base_p[bi-stride]['yaw']))>=rapid_threshold: rapid.append(pe)
            if sp.get('fov_degrees') is not None and bp.get('fov_degrees') is not None: fov_err.append(abs(float(sp['fov_degrees'])-float(bp['fov_degrees'])))
    w=wall(stride); n=len(worlds)
    rows.append({
      'stride':stride,'source_frames_represented':represented,'processed_frames':n,'wall_seconds':w,
      'effective_processed_fps':n/w,'speedup_vs_stride1':base_wall/w,
      'ball_mean_delta_rad':statistics.fmean(ball_err) if ball_err else None,'ball_p90_delta_rad':pct(ball_err,.90),
      'ball_baseline_present_samples':bpresent,'ball_sparse_present_samples':spresent,'lost_vs_baseline_count':lost_vs,
      'lost_vs_baseline_pct':100*lost_vs/bpresent if bpresent else None,'tracking_frames':track,'coasting_frames':coast,'lost_frames':lost,'missing_ball_frames':missing,
      'pan_mean_yaw_delta_rad':statistics.fmean(pan_err) if pan_err else None,'pan_p90_yaw_delta_rad':pct(pan_err,.90),
      'rapid_transition_p90_yaw_delta_rad':pct(rapid,.90),'fov_mean_delta_deg':statistics.fmean(fov_err) if fov_err else None,**summary(stride)})
json.dump({'source':source,'rows':rows},open(root/'matrix_results.json','w'),indent=2)
keys=list(rows[0]); f=open(root/'matrix_results.tsv','w'); f.write('\t'.join(keys)+'\n')
for r in rows:f.write('\t'.join('' if r[k] is None else str(r[k]) for k in keys)+'\n')
f.close(); print(json.dumps(rows,indent=2))
PY

echo 'FRAME_STRIDE_MATRIX=PASS'
