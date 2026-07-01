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

# ── Candidate redesign: xstack-based seam assembly (ChatGPT proposal) ──────
# Same crop geometry, same maskedmerge blends, same mask assignments as
# SEAM_FILTER above (verified: width totals 5796px per eye, mask1->left seam,
# mask2->right-back seam, mask3/mask4->eye2 equivalents, matching original).
# Difference: replaces the serial hstack/hstack/hstack/vstack composition
# ladder with direct xstack placement into final position, to avoid repeated
# large intermediate-frame materialization. Also restores format=yuvj420p
# (moved upstream, once per eye, per the "move conversion upstream" idea —
# the version as given dropped it entirely, which would be a real deviation).
XSTACK_FILTER="\
[1:v]format=gray,split=4[m1][m2][m3][m4],\
[0:0]format=yuvj420p,split=9[e0a][e0b][e0c][e0d][e0e][e0f][e0g][e0h][e0i],\
[e0a]crop=897:1920:0:0[a0],\
[e0b]crop=92:1920:897:0[a1],\
[e0c]crop=92:1920:989:0[a2],\
[e0d]crop=897:1920:1081:0[a3],\
[e0e]crop=1932:1920:1978:0[a4],\
[e0f]crop=897:1920:3910:0[a5],\
[e0g]crop=92:1920:4807:0[a6],\
[e0h]crop=92:1920:4899:0[a7],\
[e0i]crop=897:1920:4991:0[a8],\
[a1][a2][m1]maskedmerge[ab],\
[ab]scale=138:1920[abs],\
[a6][a7][m2]maskedmerge[bb],\
[bb]scale=138:1920[bbs],\
[0:1]format=yuvj420p,split=9[e1a][e1b][e1c][e1d][e1e][e1f][e1g][e1h][e1i],\
[e1a]crop=897:1920:0:0[c0],\
[e1b]crop=92:1920:897:0[c1],\
[e1c]crop=92:1920:989:0[c2],\
[e1d]crop=897:1920:1081:0[c3],\
[e1e]crop=1932:1920:1978:0[c4],\
[e1f]crop=897:1920:3910:0[c5],\
[e1g]crop=92:1920:4807:0[c6],\
[e1h]crop=92:1920:4899:0[c7],\
[e1i]crop=897:1920:4991:0[c8],\
[c1][c2][m3]maskedmerge[cb],\
[cb]scale=138:1920[cbs],\
[c6][c7][m4]maskedmerge[db],\
[db]scale=138:1920[dbs],\
[a0][abs][a3][a4][a5][bbs][a8]xstack=inputs=7:layout=0_0|897_0|1035_0|1932_0|3864_0|4761_0|4899_0[row0],\
[c0][cbs][c3][c4][c5][dbs][c8]xstack=inputs=7:layout=0_0|897_0|1035_0|1932_0|3864_0|4761_0|4899_0[row1],\
[row0][row1]xstack=inputs=2:layout=0_0|0_1920[complete]"
XSTACK_FULL_FILTER="${XSTACK_FILTER},[complete]v360=eac:e:interp=linear:w=7680:h=3840[v]"


# ── Benchmark-only stream remap ──────────────────────────────────────────
# SEAM_FILTER/FULL_FILTER above use [0:0]/[0:1]/[1:v] because the REAL
# pipeline reads a single .360 file with two video streams + a separate
# mask file. For synthetic benchmarking we use three separate inputs
# (0=front testsrc2, 1=back testsrc2, 2=mask), so labels must be remapped:
#   [1:v] (mask, real)  -> [2:v] (mask, synthetic, 3rd input)
#   [0:0] (front, real) -> [0:v] (front, synthetic, 1st input)
#   [0:1] (back, real)  -> [1:v] (back, synthetic, 2nd input)
# Order matters: remap mask first to a placeholder before remapping [0:1],
# since [0:1]'s target [1:v] would otherwise collide with the original
# mask label. Done in python for safe literal (non-glob) replacement.
BENCH_SEAM_FILTER=$(python3 -c "
s = '''${SEAM_FILTER}'''
s = s.replace('[1:v]format=gray', '[MASKPLACEHOLDER]format=gray')
s = s.replace('[0:0]', '[0:v]')
s = s.replace('[0:1]', '[1:v]')
s = s.replace('[MASKPLACEHOLDER]', '[2:v]')
print(s)
")
BENCH_FULL_FILTER="${BENCH_SEAM_FILTER},[complete]v360=eac:e:interp=linear:w=7680:h=3840[v]"

BENCH_XSTACK_FILTER=$(python3 -c "
s = '''${XSTACK_FILTER}'''
s = s.replace('[1:v]format=gray', '[MASKPLACEHOLDER]format=gray')
s = s.replace('[0:0]', '[0:v]')
s = s.replace('[0:1]', '[1:v]')
s = s.replace('[MASKPLACEHOLDER]', '[2:v]')
print(s)
")
BENCH_XSTACK_FULL_FILTER="${BENCH_XSTACK_FILTER},[complete]v360=eac:e:interp=linear:w=7680:h=3840[v]"


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
  timeout $((RUN_SECONDS + 300)) bash -c "ffmpeg -y -v error -nostdin ${src_args} \
    -filter_complex \"${filter}\" \
    -map \"${maplabel}\" -an -t ${RUN_SECONDS} \
    -c:v libx264 -preset ultrafast -b:v 20M -threads 0 \
    -progress \"${plog}\" -f null - > /dev/null 2>\"${WORKDIR}/stderr_${label}.log\"" &
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
    log "  --- stderr for ${label} ---"
    cat "${WORKDIR}/stderr_${label}.log" 2>/dev/null | tail -30 | while IFS= read -r line; do log "  | ${line}"; done
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
  "${BENCH_SEAM_FILTER},[complete]format=yuv420p[v]" \
  "[v]"

# ── Benchmark 3: full graph (seam assembly + v360), matches real pipeline exactly ──
run_bench "full_graph" \
  "-f lavfi -i \"testsrc2=size=5888x1920:rate=30\" -f lavfi -i \"testsrc2=size=5888x1920:rate=30\" -i \"${MASK}\"" \
  "${BENCH_FULL_FILTER},[v]format=yuv420p[vout]" \
  "[vout]"

# ── Benchmark 4: xstack candidate, seam assembly alone, NO v360 ──
run_bench "seam_xstack" \
  "-f lavfi -i \"testsrc2=size=5888x1920:rate=30\" -f lavfi -i \"testsrc2=size=5888x1920:rate=30\" -i \"${MASK}\"" \
  "${BENCH_XSTACK_FILTER},[complete]format=yuv420p[v]" \
  "[v]"

# ── Benchmark 5: xstack candidate, full graph (seam + v360) ──
run_bench "full_graph_xstack" \
  "-f lavfi -i \"testsrc2=size=5888x1920:rate=30\" -f lavfi -i \"testsrc2=size=5888x1920:rate=30\" -i \"${MASK}\"" \
  "${BENCH_XSTACK_FULL_FILTER},[v]format=yuv420p[vout]" \
  "[vout]"

log ""
log "=== PROFILE SUMMARY ==="
V360=$(cat "${WORKDIR}/result_v360_alone.txt" 2>/dev/null || echo 0)
SEAM=$(cat "${WORKDIR}/result_seam_alone.txt" 2>/dev/null || echo 0)
FULL=$(cat "${WORKDIR}/result_full_graph.txt" 2>/dev/null || echo 0)
SEAM_XSTACK=$(cat "${WORKDIR}/result_seam_xstack.txt" 2>/dev/null || echo 0)
FULL_XSTACK=$(cat "${WORKDIR}/result_full_graph_xstack.txt" 2>/dev/null || echo 0)
log "v360 alone            : ${V360}x"
log "seam alone (current)  : ${SEAM}x"
log "full graph (current)  : ${FULL}x"
log "seam alone (xstack)   : ${SEAM_XSTACK}x"
log "full graph (xstack)   : ${FULL_XSTACK}x"
log "Done."
touch "${WORKDIR}/DONE"
