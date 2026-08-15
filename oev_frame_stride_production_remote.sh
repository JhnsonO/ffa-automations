#!/usr/bin/env bash
# OEV production frame-stride acceptance benchmark.
# Same 30s stereo sample, same pod/GPU/model/settings, stride 1 vs stride 3.
# Production invariant: AI may run sparsely, but EVERY source frame is rendered.
# No post-hoc retiming is allowed in this harness.
set -Eeuo pipefail
trap 'rc=$?; echo "HARNESS_ERR line=$LINENO rc=$rc cmd=$BASH_COMMAND" >&2' ERR

ROOT=/tmp/oev_stride_prod
RECO=/tmp/video-stitcher/target/release/reco
MODEL="$ROOT/yolo26m.onnx"
mkdir -p "$ROOT/results"
cd "$ROOT"

fatal() { echo "FATAL: $*" >&2; exit 20; }
require_log() {
  local label="$1" pattern="$2" file="$3"
  if grep -qiE "$pattern" "$file"; then
    echo "GATE PASS: $label"
  else
    fatal "$label evidence missing in $file"
  fi
}
probe_value() {
  local label="$1"; shift
  local value
  if ! value=$(ffprobe -v error "$@"); then
    fatal "ffprobe failed for $label"
  fi
  value=$(printf '%s' "$value" | tr -d '\r\n')
  [ -n "$value" ] && [ "$value" != N/A ] || fatal "ffprobe returned empty/N/A for $label"
  printf '%s' "$value"
}

for f in left.mp4 right.mp4 "$MODEL"; do
  test -s "$f" || fatal "required input missing/empty: $f"
done
test -x "$RECO" || fatal "Reco binary missing: $RECO"

SOURCE_FPS=$(probe_value source_fps -select_streams v:0 -show_entries stream=avg_frame_rate -of default=nw=1:nk=1 left.mp4)
SOURCE_FRAMES=$(probe_value left_source_frames -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1:nk=1 left.mp4)
RIGHT_FRAMES=$(probe_value right_source_frames -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1:nk=1 right.mp4)
SOURCE_DURATION=$(probe_value source_duration -show_entries format=duration -of default=nw=1:nk=1 left.mp4)
[ "$SOURCE_FRAMES" -eq "$RIGHT_FRAMES" ] || fatal "stereo source counts differ: left=$SOURCE_FRAMES right=$RIGHT_FRAMES"
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

for stride in 1 3; do
  out="$ROOT/results/stride_${stride}"
  mkdir -p "$out/stills"
  echo "=== PRODUCTION STRIDE $stride: source=$SOURCE_FRAMES, lookahead=1.5s ===" | tee "$out/benchmark.log"
  start_ns=$(date +%s%N)
  set +e
  stdbuf -oL -eL "$RECO" stitch left.mp4 right.mp4 \
    -c match.json -o "$out/output.mp4" --model "$MODEL" --tracking field \
    --panner-preset broadcast --lookahead 1.5 --detection-interval 1 \
    --frame-stride "$stride" --events "$out/events.jsonl" \
    --width 1920 --height 1080 2>&1 | tee "$out/stitch.log"
  rc=${PIPESTATUS[0]}
  set -e
  end_ns=$(date +%s%N)
  wall_s=$(python3 - "$start_ns" "$end_ns" <<'PY'
import sys
print(f"{(int(sys.argv[2])-int(sys.argv[1]))/1e9:.6f}")
PY
)
  echo "wall_seconds=$wall_s" | tee -a "$out/benchmark.log"
  [ "$rc" -eq 0 ] || fatal "stride $stride Reco exit $rc"
  test -s "$out/output.mp4" || fatal "stride $stride output.mp4 missing/empty"
  test -s "$out/events.jsonl" || fatal "stride $stride events.jsonl missing/empty"

  require_log "stride $stride tracking" 'Autocam: tracking enabled' "$out/stitch.log"
  require_log "stride $stride zero-copy" 'GPU zero-copy|zero-copy|zero copy' "$out/stitch.log"
  require_log "stride $stride NVDEC" 'NVDEC.*CUDA|NVDEC \(CUDA\)|cuvid' "$out/stitch.log"
  require_log "stride $stride CUDA EP" 'CUDAExecutionProvider' "$out/stitch.log"
  if grep -q 'No execution providers from session options registered successfully' "$out/stitch.log"; then
    fatal "stride $stride CUDA EP fell back"
  fi
  echo "GATE PASS: stride $stride CUDA EP no-fallback"
  if [ "$stride" -eq 3 ]; then
    require_log "production stride CLI" 'Frame stride: analyze 1/3, render every source frame' "$out/stitch.log"
    require_log "production sparse-analysis loop" 'analyze every 3 frames' "$out/stitch.log"
  fi

  echo "POSTCHECK: probing stride $stride output"
  output_frames=$(probe_value "stride_${stride}_output_frames" -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nw=1:nk=1 "$out/output.mp4")
  output_fps=$(probe_value "stride_${stride}_output_fps" -select_streams v:0 -show_entries stream=avg_frame_rate -of default=nw=1:nk=1 "$out/output.mp4")
  output_duration=$(probe_value "stride_${stride}_output_duration" -show_entries format=duration -of default=nw=1:nk=1 "$out/output.mp4")
  read -r world_events pan_events pose_events < <(python3 - "$out/events.jsonl" <<'PY'
import json,sys
counts={'world_state':0,'pan_decision':0,'pose_presented':0}
for line in open(sys.argv[1]):
    try: e=json.loads(line)
    except Exception: continue
    k=e.get('kind')
    if k in counts: counts[k]+=1
print(counts['world_state'], counts['pan_decision'], counts['pose_presented'])
PY
)
  echo "POSTCHECK stride=$stride output_frames=$output_frames output_fps=$output_fps output_duration=$output_duration world_state_events=$world_events pan_decision_events=$pan_events pose_presented_events=$pose_events"
  cat >> "$out/benchmark.log" <<META
output_frames=$output_frames
output_fps=$output_fps
output_duration=$output_duration
world_state_events=$world_events
pan_decision_events=$pan_events
pose_presented_events=$pose_events
META

  # Stills are useful visual evidence, but a thumbnail extraction problem must
  # not invalidate the mechanical full-rate production gates.
  for t in 3 9 15 24 27; do
    if ffmpeg -y -v error -ss "$t" -i "$out/output.mp4" -frames:v 1 "$out/stills/t${t}.jpg"; then
      echo "STILL PASS: stride=$stride t=${t}s"
    else
      echo "STILL WARN: stride=$stride t=${t}s extraction failed" >&2
    fi
  done
done

echo "POSTCHECK: evaluating production gates"
python3 - <<'PY'
import json,math,statistics
from fractions import Fraction
from pathlib import Path
root=Path('/tmp/oev_stride_prod/results')
source={}
for line in open('/tmp/oev_stride_prod/source_metadata.txt'):
    if '=' in line:
        k,v=line.strip().split('=',1); source[k]=v

def parse_log(s):
    vals={}
    for line in (root/f'stride_{s}'/'benchmark.log').read_text().splitlines():
        if '=' in line:
            k,v=line.split('=',1); vals[k]=v
    return vals

def load_events(s):
    worlds={}; pans={}; poses={}
    for line in open(root/f'stride_{s}'/'events.jsonl'):
        try:e=json.loads(line)
        except Exception:continue
        i=e.get('frame_index')
        if i is None: continue
        if e.get('kind')=='world_state': worlds[int(i)]=e
        elif e.get('kind')=='pan_decision': pans[int(i)]=e.get('pose') or {}
        elif e.get('kind')=='pose_presented': poses[int(i)]=e.get('pose') or {}
    return worlds,pans,poses

def pct(xs,p):
    if not xs:return None
    ys=sorted(xs); return ys[max(0,min(len(ys)-1,math.ceil(p*len(ys))-1))]

logs={s:parse_log(s) for s in (1,3)}
rows=[]
for s in (1,3):
    x=logs[s]
    rows.append({
        'stride':s,
        'wall_seconds':float(x['wall_seconds']),
        'output_frames':int(x['output_frames']),
        'output_fps':x['output_fps'],
        'output_duration':float(x['output_duration']),
        'world_state_events':int(x['world_state_events']),
        'pan_decision_events':int(x['pan_decision_events']),
        'pose_presented_events':int(x['pose_presented_events']),
    })
base=rows[0]; fast=rows[1]
fast['speedup_vs_stride1']=base['wall_seconds']/fast['wall_seconds']
fast['wall_reduction_pct']=100*(1-fast['wall_seconds']/base['wall_seconds'])
fast['output_frame_ratio_vs_stride1']=fast['output_frames']/base['output_frames']
fast['duration_delta_s_vs_stride1']=fast['output_duration']-base['output_duration']

bw,bp,_=load_events(1); sw,sp,_=load_events(3)
ball_err=[]; pan_err=[]; fov_err=[]; lost_vs=0; baseline_ball=0
for i,w in sw.items():
    b=bw.get(i*3)
    if not b: continue
    bb=b.get('ball'); sb=w.get('ball')
    baseline_ball += bb is not None
    lost_vs += bb is not None and sb is None
    if bb is not None and sb is not None:
        ball_err.append(math.hypot(float(sb['yaw'])-float(bb['yaw']),float(sb['pitch'])-float(bb['pitch'])))
for i,p in sp.items():
    b=bp.get(i*3)
    if not b or p.get('yaw') is None or b.get('yaw') is None: continue
    dy=(float(p['yaw'])-float(b['yaw'])+math.pi)%(2*math.pi)-math.pi
    pan_err.append(abs(dy))
    if p.get('fov_degrees') is not None and b.get('fov_degrees') is not None:
        fov_err.append(abs(float(p['fov_degrees'])-float(b['fov_degrees'])))
quality={
    'ball_mean_delta_rad':statistics.fmean(ball_err) if ball_err else None,
    'ball_p90_delta_rad':pct(ball_err,.90),
    'ball_p95_delta_rad':pct(ball_err,.95),
    'lost_vs_baseline_count':lost_vs,
    'lost_vs_baseline_pct':100*lost_vs/baseline_ball if baseline_ball else None,
    'pan_mean_yaw_delta_rad':statistics.fmean(pan_err) if pan_err else None,
    'pan_p90_yaw_delta_rad':pct(pan_err,.90),
    'pan_p95_yaw_delta_rad':pct(pan_err,.95),
    'pan_max_yaw_delta_rad':max(pan_err) if pan_err else None,
    'fov_mean_delta_deg':statistics.fmean(fov_err) if fov_err else None,
}

gates={}
gates['full_rate_render']=fast['output_frame_ratio_vs_stride1'] >= 0.995
gates['same_duration']=abs(fast['duration_delta_s_vs_stride1']) <= 0.10
gates['same_output_fps']=Fraction(fast['output_fps']) == Fraction(base['output_fps'])
analysis_ratio=fast['world_state_events']/max(1,base['world_state_events'])
gates['sparse_analysis_cadence']=0.30 <= analysis_ratio <= 0.37
gates['posthoc_retime_used']=False
# PosePresented is emitted on render/output cadence and should stay full-rate.
pose_ratio=fast['pose_presented_events']/max(1,base['pose_presented_events'])
gates['full_rate_pose_presentation']=pose_ratio >= 0.995

result={
    'source':source,
    'rows':rows,
    'analysis_event_ratio_stride3_vs_stride1':analysis_ratio,
    'pose_event_ratio_stride3_vs_stride1':pose_ratio,
    'quality_stride3_vs_stride1':quality,
    'production_gates':gates,
}
json.dump(result,open(root/'production_results.json','w'),indent=2)
print(json.dumps(result,indent=2))
failed=[k for k,v in gates.items() if k!='posthoc_retime_used' and v is not True]
if failed:
    raise SystemExit('FAIL production gates: '+', '.join(failed))
PY

echo 'FRAME_STRIDE_PRODUCTION_ACCEPTANCE=PASS'
