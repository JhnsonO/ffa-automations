#!/usr/bin/env bash
# FFA MAX2 filter-graph profiler — isolates v360 cost vs seam-assembly cost.
# Not part of the production pipeline. Synthetic sources only, no download.
set -euo pipefail

WORKDIR="/tmp/ffa_profile"
mkdir -p "${WORKDIR}"
cd "${WORKDIR}"

log() { echo "[$(date -u +%H:%M:%S)] $*"; }

log "=== FFA Filter Graph Profiler ==="
NPROC=$(nproc)
log "nproc: ${NPROC}"

log "--- Installing deps ---"
apt-get update -qq
apt-get install -y -qq --no-install-recommends ffmpeg python3-pil > /dev/null
log "deps installed"

log "--- Generating seam blend mask ---"
python3 - <<'PY'
from PIL import Image
w, h = 92, 1920
img = Image.new("L", (w, h))
row = bytes(int(x * 255 / (w - 1)) for x in range(w))
img.putdata(list(row * h))
img.save("/tmp/ffa_profile/seam_mask.png")
PY
log "mask generated"

MASK="/tmp/ffa_profile/seam_mask.png"

# Real geometry (from vastai_stitch_max2.sh):
#   per-eye EAC tile input: 5888x1920
#   seam-assembled canvas ("complete") before v360: 5796x3840
#   final v360 output: 7680x3840
SEAM_FILTER="\
[1:v]format=gray,split=4[mask1][mask2][mask3][mask4],\
[0:0]crop=897:1920:x=0:y=0,format=yuvj420p[left],\
[0:0]crop=897:1920:x=1081:y=0,format=yuvj420p[right],\
[0:0]crop=92:1920:x=897:y=0,format=yuvj420p[segA],\
[0:0]crop=92:1920:x=989:y=0,format=yuvj420p[segB],\
[segA][segB][mask1]maskedmerge[crop],\
[crop]scale=138:1920[cropScaled],\
[left][cropScaled]hstack[leftAll],[leftAll][right]hstack[leftDone],\
[0:0]crop=1932:1920:1978:0[middle],\
[0:0]crop=897:1920:x=3910:y=0,format=yuvj420p[lRB],\
[0:0]crop=897:1920:x=4991:y=0,format=yuvj420p[rRB],\
[0:0]crop=92:1920:x=4807:y=0,format=yuvj420p[segARB],\
[0:0]crop=92:1920:x=4899:y=0,format=yuvj420p[segBRB],\
[segARB][segBRB][mask2]maskedmerge[cropRB],\
[cropRB]scale=138:1920[cropRBScaled],\
[lRB][cropRBScaled]hstack[rAll],[rAll][rRB]hstack[rBotDone],\
[leftDone][middle]hstack[lMid],[lMid][rBotDone]hstack[botComplete],\
[0:1]crop=897:1920:x=0:y=0,format=yuvj420p[flt],\
[0:1]crop=897:1920:x=1081:y=0,format=yuvj420p[frt],\
[0:1]crop=92:1920:x=897:y=0,format=yuvj420p[segC],\
[0:1]crop=92:1920:x=989:y=0,format=yuvj420p[segD],\
[segC][segD][mask3]maskedmerge[ltc],\
[ltc]scale=138:1920[ltcScaled],\
[flt][ltcScaled]hstack[tlh],[tlh][frt]hstack[tlDone],\
[0:1]crop=1932:1920:1978:0[tMid],\
[0:1]crop=897:1920:x=3910:y=0,format=yuvj420p[tlRB],\
[0:1]crop=897:1920:x=4991:y=0,format=yuvj420p[trRB],\
[0:1]crop=92:1920:x=4807:y=0,format=yuvj420p[segE],\
[0:1]crop=92:1920:x=4899:y=0,format=yuvj420p[segF],\
[segE][segF][mask4]maskedmerge[tcRB],\
[tcRB]scale=138:1920[tcRBScaled],\
[tlRB][tcRBScaled]hstack[trAll],[trAll][trRB]hstack[trBotDone],\
[tlDone][tMid]hstack[tlMid],[tlMid][trBotDone]hstack[topComplete],\
[botComplete][topComplete]vstack[complete]"

FULL_FILTER="${SEAM_FILTER},[complete]v360=eac:e:interp=linear:w=7680:h=3840[v]"

RUN_SECONDS=90
WARMUP_US=30000000

# run_bench <label> <ffmpeg-source-args...> -- <filter> <map-label>
run_bench() {
  local label="$1"; shift
  local src_args="$1"; shift
  local filter="$1"; shift
  local maplabel="$1"; shift

  local plog="${WORKDIR}/progress_${label}.log"
  rm -f "${plog}"
  log "--- Benchmark: ${label} (${RUN_SECONDS}s run, ${WARMUP_US}us warmup) ---"

  set +e
  timeout $((RUN_SECONDS + 60)) bash -c "ffmpeg -y -v error -nostdin ${src_args} \
    -filter_complex \"${filter}\" \
    -map \"${maplabel}\" -an -t ${RUN_SECONDS} \
    -c:v libx264 -preset ultrafast -b:v 20M -threads 0 \
    -progress \"${plog}\" -f null - > /dev/null 2>&1" &
  local pid=$!
  set -e

  local snap_ot="" snap_wall=""
  local poll=0
  while kill -0 "${pid}" 2>/dev/null; do
    sleep 5
    poll=$((poll + 5))
    local ot
    ot=$(grep -a "^out_time_us=" "${plog}" 2>/dev/null | tail -1 | cut -d= -f2 || echo 0)
    ot=${ot:-0}
    local sp
    sp=$(grep -a "^speed=" "${plog}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d 'x ' || echo "?")
    log "  [${label} ${poll}s] out_time_us=${ot} rolling_speed=${sp}x"
    if [ -z "${snap_ot}" ]; then
      local past
      past=$(python3 -c "print('yes' if int('${ot:-0}') >= ${WARMUP_US} else 'no')" 2>/dev/null || echo "no")
      if [ "${past}" = "yes" ]; then
        snap_ot="${ot}"
        snap_wall=$(date +%s%3N)
        log "  [${label}] warmup complete — measuring from here"
      fi
    fi
  done
  set +e
  wait "${pid}"
  set -e

  local final_ot final_wall
  final_ot=$(grep -a "^out_time_us=" "${plog}" 2>/dev/null | tail -1 | cut -d= -f2 || echo 0)
  final_wall=$(date +%s%3N)

  if [ -z "${snap_ot}" ]; then
    log "RESULT ${label}: FAILED — never reached warmup threshold"
    echo "0" > "${WORKDIR}/result_${label}.txt"
    return
  fi

  local speed
  speed=$(python3 -c "
try:
    dot = (int('${final_ot}') - int('${snap_ot}')) / 1e6
    dw  = (int('${final_wall}') - int('${snap_wall}')) / 1e3
    print(round(dot/dw, 3) if dw > 0 and dot > 0 else 0)
except Exception:
    print(0)
" 2>/dev/null || echo 0)

  log "RESULT ${label}: sustained speed = ${speed}x (post-warmup)"
  echo "${speed}" > "${WORKDIR}/result_${label}.txt"
}

# ── Benchmark 1: v360 alone, real intermediate size 5796x3840 -> 7680x3840 ──
run_bench "v360_alone" \
  "-f lavfi -i \"testsrc2=size=5796x3840:rate=30\"" \
  "[0:v]v360=eac:e:interp=linear:w=7680:h=3840,format=yuv420p[v]" \
  "[v]"

# ── Benchmark 2: seam assembly alone, NO v360, real per-eye size 5888x1920 x2 ──
run_bench "seam_alone" \
  "-f lavfi -i \"testsrc2=size=5888x1920:rate=30\" -f lavfi -i \"testsrc2=size=5888x1920:rate=30\" -i \"${MASK}\"" \
  "${SEAM_FILTER},[complete]format=yuv420p[v]" \
  "[v]"

# ── Benchmark 3: full graph (seam assembly + v360), matches real pipeline exactly ──
run_bench "full_graph" \
  "-f lavfi -i \"testsrc2=size=5888x1920:rate=30\" -f lavfi -i \"testsrc2=size=5888x1920:rate=30\" -i \"${MASK}\"" \
  "${FULL_FILTER},[v]format=yuv420p[vout]" \
  "[vout]"

log ""
log "=== PROFILE SUMMARY ==="
V360=$(cat "${WORKDIR}/result_v360_alone.txt" 2>/dev/null || echo 0)
SEAM=$(cat "${WORKDIR}/result_seam_alone.txt" 2>/dev/null || echo 0)
FULL=$(cat "${WORKDIR}/result_full_graph.txt" 2>/dev/null || echo 0)
log "v360 alone     : ${V360}x"
log "seam alone     : ${SEAM}x"
log "full graph     : ${FULL}x"
log "Done."
touch "${WORKDIR}/DONE"
