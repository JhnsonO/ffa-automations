#!/usr/bin/env bash
# FFA GoPro MAX 2 360 — Vast.ai stitch + upload script
# COPY of vastai_stitch.sh (MAX 1) with EAC tile geometry rescaled for
# MAX 2's per-stream resolution (5888x1920 vs MAX 1's 4096x1344).
# Scale factor R = 5888/4096 = 23/16 = 1.4375 applied to all x-axis crop
# widths/offsets; height substituted literally (1344 -> 1920) since it's
# directly known from ffprobe. Validated against GoPro's published "true
# 8K" stitched spec (3840 height) and ~25% EAC pixel savings — both match.
# VISUALLY VERIFIED on real MAX 2 clips via single-frame extraction in
# gopro360-test-frame.yml — clean seamless stitch, no jaggedness, no void.
# v360 output explicitly sized to w=7680:h=3840 (true 8K, matching GoPro's
# published stitched spec) — NOT the EAC canvas size (5796x2898), which was
# an earlier mistake that landed YouTube in its unsupported "in-between"
# 4K-8K dead zone and got silently downsampled to 4K.
# Runs on the Vast.ai instance. Called via SSH from GitHub Actions.
#
# Required env vars (passed via SSH):
#   SOURCE_URL        — GoPro CDN concat URL for the .360 file
#   MEDIA_ID          — GoPro media ID
#   FILENAME          — original .360 filename e.g. GS010404.360
#   CAPTURED_AT       — ISO8601 capture timestamp
#   YOUTUBE_CREDS     — JSON string of youtube_credentials.json
#   YOUTUBE_TOKEN     — JSON string of youtube_token.json
#   GH_PAT            — GitHub PAT for committing uploaded.db back
#   REPO              — GitHub repo e.g. JhnsonO/ffa-automations
#   TRANSCODE_BITRATE — FFmpeg output bitrate e.g. 20M (default: 20M)

set -euo pipefail

WORKDIR_EARLY="/tmp/ffa360"
mkdir -p "${WORKDIR_EARLY}"
rm -f "${WORKDIR_EARLY}/DONE" "${WORKDIR_EARLY}/FAILED"
trap 'code=$?; if [ $code -ne 0 ] && [ ! -s "${WORKDIR_EARLY}/FAILED" ]; then echo "FAILED:$code" > "${WORKDIR_EARLY}/FAILED"; fi' EXIT

BITRATE="${TRANSCODE_BITRATE:-auto}"
WORKDIR="/tmp/ffa360"
OUTPUT_EQUIRECT="${WORKDIR}/output.equirect.mp4"
OUTPUT_FINAL="${WORKDIR}/output.final.mp4"
MASK_PNG="${WORKDIR}/seam_mask.png"

log() {
  echo "[$(date -u +%H:%M:%S)] $*"
}

log "=== FFA GoPro 360 Stitch + Upload ==="
log "File     : ${FILENAME}"
log "Media ID : ${MEDIA_ID}"
log "Captured : ${CAPTURED_AT}"
log "Bitrate  : ${BITRATE}"

# ── Setup ─────────────────────────────────────────────────────────────────────
mkdir -p "${WORKDIR}"
cd "${WORKDIR}"

log "--- Installing dependencies ---"
apt-get update -qq
log "apt-get update done"

apt-get install -y -qq --no-install-recommends ffmpeg libimage-exiftool-perl python3-pip python3-pil > /dev/null &
APT_PID=$!

pip install -q --break-system-packages google-auth google-auth-oauthlib google-api-python-client 2>/dev/null &
PIP_PID=$!

wait $APT_PID && log "apt install done" || { log "apt install FAILED"; exit 1; }
wait $PIP_PID && log "pip install done" || { log "pip install (system) — trying without --break-system-packages"; pip install -q google-auth google-auth-oauthlib google-api-python-client; }

log "--- Writing YouTube credentials ---"
echo "${YOUTUBE_CREDS}" > youtube_credentials.json
echo "${YOUTUBE_TOKEN}" > youtube_token.json
log "Credentials written"

# ── Generate seam-blend gradient mask (once, cheap) ────────────────────────────
# 64px wide x 1344px tall, linear gradient 0 -> 255 left to right.
# Used with maskedmerge to replicate the soft seam blend that the old
# geq-based approach did per-pixel (and far too slowly for full-length video).
log "--- Generating seam blend mask ---"
python3 - <<'PY'
from PIL import Image
w, h = 92, 1920
img = Image.new("L", (w, h))
row = bytes(int(x * 255 / (w - 1)) for x in range(w))
data = row * h
img.putdata(list(data))
img.save("/tmp/ffa360/seam_mask.png")
PY
log "Seam mask generated: ${MASK_PNG}"

# ── FFmpeg filter chain ──────────────────────────────────────────────────────
# GoPro MAX .360 CDN concat file has 2 streams: 0:0 (front), 0:1 (rear)
# Each stream is a 4096x1344 EAC tile. We blend the seams using maskedmerge
# (fast, SIMD) instead of geq (slow, interpreted per-pixel) — same visual
# result, dramatically faster.
#
# For each seam: A = the "near" 64px strip, B = the wrapped 64px strip from
# the far side of the tile. maskedmerge(A,B,mask) blends A->B left-to-right.

FILTER_COMPLEX="\
[1:v]format=gray,split=4[mask1][mask2][mask3][mask4],\
[0:v:0]crop=897:1920:x=0:y=0,format=yuvj420p[left],\
[0:v:0]crop=897:1920:x=1081:y=0,format=yuvj420p[right],\
[0:v:0]crop=92:1920:x=897:y=0,format=yuvj420p[segA],\
[0:v:0]crop=92:1920:x=989:y=0,format=yuvj420p[segB],\
[segA][segB][mask1]maskedmerge[crop],\
[crop]scale=138:1920[cropScaled],\
[left][cropScaled]hstack[leftAll],[leftAll][right]hstack[leftDone],\
[0:v:0]crop=1932:1920:1978:0[middle],\
[0:v:0]crop=897:1920:x=3910:y=0,format=yuvj420p[lRB],\
[0:v:0]crop=897:1920:x=4991:y=0,format=yuvj420p[rRB],\
[0:v:0]crop=92:1920:x=4807:y=0,format=yuvj420p[segARB],\
[0:v:0]crop=92:1920:x=4899:y=0,format=yuvj420p[segBRB],\
[segARB][segBRB][mask2]maskedmerge[cropRB],\
[cropRB]scale=138:1920[cropRBScaled],\
[lRB][cropRBScaled]hstack[rAll],[rAll][rRB]hstack[rBotDone],\
[leftDone][middle]hstack[lMid],[lMid][rBotDone]hstack[botComplete],\
[0:v:1]crop=897:1920:x=0:y=0,format=yuvj420p[flt],\
[0:v:1]crop=897:1920:x=1081:y=0,format=yuvj420p[frt],\
[0:v:1]crop=92:1920:x=897:y=0,format=yuvj420p[segC],\
[0:v:1]crop=92:1920:x=989:y=0,format=yuvj420p[segD],\
[segC][segD][mask3]maskedmerge[ltc],\
[ltc]scale=138:1920[ltcScaled],\
[flt][ltcScaled]hstack[tlh],[tlh][frt]hstack[tlDone],\
[0:v:1]crop=1932:1920:1978:0[tMid],\
[0:v:1]crop=897:1920:x=3910:y=0,format=yuvj420p[tlRB],\
[0:v:1]crop=897:1920:x=4991:y=0,format=yuvj420p[trRB],\
[0:v:1]crop=92:1920:x=4807:y=0,format=yuvj420p[segE],\
[0:v:1]crop=92:1920:x=4899:y=0,format=yuvj420p[segF],\
[segE][segF][mask4]maskedmerge[tcRB],\
[tcRB]scale=138:1920[tcRBScaled],\
[tlRB][tcRBScaled]hstack[trAll],[trAll][trRB]hstack[trBotDone],\
[tlDone][tMid]hstack[tlMid],[tlMid][trBotDone]hstack[topComplete],\
[botComplete][topComplete]vstack[complete],\
[complete]v360=eac:e:interp=linear:w=7680:h=3840[v]"

# ── Probe source duration ──────────────────────────────────────────────────────
log ""
log "Source: ${SOURCE_URL:0:80}..."
log "--- GPU check ---"
nvidia-smi || log "WARNING: nvidia-smi not available"

log "--- Removing CUDA forward-compat libs (use host driver instead) ---"
rm -rf /usr/local/cuda/compat 2>/dev/null || true
ldconfig
log "ldconfig done"
TOTAL_DUR=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "${SOURCE_URL}")
log "Source duration: ${TOTAL_DUR}s"

if [ "${BITRATE}" = "auto" ]; then
  SRC_BPS=$(ffprobe -v error -show_entries format=bit_rate -of csv=p=0 "${SOURCE_URL}")
  if [ -n "${SRC_BPS}" ] && [ "${SRC_BPS}" != "N/A" ]; then
    BITRATE="${SRC_BPS}"
    log "Auto bitrate: using source bitrate ${BITRATE} bps ($(python3 -c "print(round(${BITRATE}/1e6,1))")Mbps)"
  else
    BITRATE="20M"
    log "Auto bitrate: source bit_rate unavailable, falling back to ${BITRATE}"
  fi
fi

NPROC=$(nproc)
log "--- Core count check ---"
log "  nproc: ${NPROC}"
if [ "${NPROC}" -lt 16 ]; then
  log "ERROR: only ${NPROC} cores allocated (need >=16 for target speed) — bad offer, aborting before download"
  exit 1
fi

# ── Preflight host-speed benchmark (BEFORE the multi-GB download) ────────────
# Runs v360 EAC→equirect remap + x264 on 60s of synthetic 8K content.
# v360 has a large one-off remap-table setup cost that dominates a 5s test
# but is negligible over a 52-min encode. We therefore:
#   - run for 60s of encode output
#   - ignore the first 30s (warmup)
#   - measure speed as delta out_time_us / wall_time over seconds 30–60
# This matches the sustained-monitor methodology and gives a fair prediction
# of throughput rather than startup overhead.
BENCH_W=7680
BENCH_H=3840
PREFLIGHT_MIN=0.55          # lowered from 0.85 — real measured sustained speed on best hardware (9950X) is ~0.63x post-warmup, so 0.85 rejected every host. 0.55 leaves margin below the real ceiling.
PREFLIGHT_WARMUP_US=30000000  # ignore first 30s of output (µs)
PREFLIGHT_LOG="${WORKDIR}/preflight.log"
log "--- Preflight benchmark (v360 ${BENCH_W}x${BENCH_H} + x264, 60s synthetic, 30s warm-up, floor ${PREFLIGHT_MIN}x) ---"
rm -f "${PREFLIGHT_LOG}"
set +e
timeout 300 ffmpeg -y -v error -nostdin \
  -f lavfi -i "testsrc2=size=${BENCH_W}x${BENCH_H}:rate=30" \
  -t 60 -an \
  -vf "v360=eac:e:interp=linear:w=${BENCH_W}:h=${BENCH_H},format=yuv420p" \
  -c:v libx264 -preset ultrafast -b:v 20M -threads 0 \
  -progress "${PREFLIGHT_LOG}" -f null - > /dev/null 2>&1 &
PF_PID=$!
set -e

SNAP_OT_US=""
SNAP_WALL_MS=""
PF_POLL=0
while kill -0 "${PF_PID}" 2>/dev/null; do
  sleep 5
  PF_POLL=$((PF_POLL + 5))
  OT_US=$(grep -a "^out_time_us=" "${PREFLIGHT_LOG}" 2>/dev/null | tail -1 | cut -d= -f2 || echo 0)
  OT_US=${OT_US:-0}
  SP=$(grep -a "^speed=" "${PREFLIGHT_LOG}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d 'x ' || echo "?")
  log "  [preflight ${PF_POLL}s wall] out_time_us=${OT_US} rolling_speed=${SP}x"
  if [ -z "${SNAP_OT_US}" ]; then
    PAST=$(python3 -c "print('yes' if int('${OT_US:-0}') >= ${PREFLIGHT_WARMUP_US} else 'no')" 2>/dev/null || echo "no")
    if [ "${PAST}" = "yes" ]; then
      SNAP_OT_US="${OT_US}"
      SNAP_WALL_MS=$(date +%s%3N)
      log "  [preflight] warmup complete — measurement window starts (snap_ot_us=${SNAP_OT_US})"
    fi
  fi
done

set +e
wait "${PF_PID}"
PF_RC=$?
set -e

FINAL_OT_US=$(grep -a "^out_time_us=" "${PREFLIGHT_LOG}" 2>/dev/null | tail -1 | cut -d= -f2 || echo 0)
FINAL_WALL_MS=$(date +%s%3N)

if [ -z "${SNAP_OT_US}" ]; then
  log "PREFLIGHT FAILED: encode never reached 30s output (host too slow to benchmark)"
  echo "0" > "${WORKDIR}/SPEED" 2>/dev/null || true
  cp "${WORKDIR}/SPEED" /tmp/ffa360/SPEED 2>/dev/null || true
  echo "BENCHMARK_FAILED" > "${WORKDIR}/FAILED"
  cp "${WORKDIR}/FAILED" /tmp/ffa360/FAILED 2>/dev/null || true
  exit 1
fi

PF_SPEED=$(python3 -c "
try:
    delta_ot = (int('${FINAL_OT_US}') - int('${SNAP_OT_US}')) / 1e6
    delta_w  = (int('${FINAL_WALL_MS}') - int('${SNAP_WALL_MS}')) / 1e3
    print(round(delta_ot / delta_w, 3) if delta_w > 0 and delta_ot > 0 else 0)
except Exception:
    print(0)
" 2>/dev/null || echo "0")

log "  preflight post-warmup speed=${PF_SPEED:-?}x (exit=${PF_RC})"
PF_OK=$(python3 -c "
try:
    print('yes' if float('${PF_SPEED:-0}') >= ${PREFLIGHT_MIN} else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

if [ "${PF_RC}" = "124" ] || [ "${PF_OK}" != "yes" ]; then
  log "PREFLIGHT FAILED: ${PF_SPEED:-0}x < ${PREFLIGHT_MIN}x (rc=${PF_RC}) — rejecting host before download"
  echo "${PF_SPEED:-0}" > "${WORKDIR}/SPEED" 2>/dev/null || true
  cp "${WORKDIR}/SPEED" /tmp/ffa360/SPEED 2>/dev/null || true
  echo "BENCHMARK_FAILED" > "${WORKDIR}/FAILED"
  cp "${WORKDIR}/FAILED" /tmp/ffa360/FAILED 2>/dev/null || true
  exit 1
fi
log "PREFLIGHT PASSED: ${PF_SPEED}x >= ${PREFLIGHT_MIN}x — proceeding to download"
echo "${PF_SPEED}" > "${WORKDIR}/SPEED" 2>/dev/null || true
cp "${WORKDIR}/SPEED" /tmp/ffa360/SPEED 2>/dev/null || true


# ── Download source to local NVMe ───────────────────────────────────────────
# Avoids N parallel remote seeks against the GoPro CDN (unreliable/slow);
# local -ss seeks are instant and frame-accurate.
LOCAL_SOURCE="${WORKDIR}/source.360"
log "--- HEAD request diagnostic ---"
HEAD_OUT=$(curl -sI --connect-timeout 15 --max-time 30 "${SOURCE_URL}" || true)
echo "$HEAD_OUT" | head -10
TOTAL_BYTES=$(echo "$HEAD_OUT" | grep -i '^content-length:' | tr -d '[:space:]\r' | cut -d: -f2 || echo 0)
TOTAL_MB=$(( (${TOTAL_BYTES:-0} + 1048575) / 1048576 ))
log "  Total file size: ${TOTAL_MB}MB"

log "--- Installing aria2 ---"
apt-get install -y -qq aria2 > /dev/null

log "--- Downloading source to local disk (aria2c, 16 connections) ---"
aria2c \
  --out="source.360" \
  --dir="${WORKDIR}" \
  --split=16 \
  --max-connection-per-server=16 \
  --min-split-size=10M \
  --connect-timeout=30 \
  --timeout=60 \
  --max-tries=10 \
  --retry-wait=5 \
  --max-overall-download-limit=0 \
  --file-allocation=none \
  --allow-overwrite=true \
  --console-log-level=warn \
  --summary-interval=0 \
  "${SOURCE_URL}" > "${WORKDIR}/download.log" 2>&1 &
DL_PID=$!
STALL_TICKS=0
LAST_SZ=0
DL_START=$(date +%s)
while kill -0 "${DL_PID}" 2>/dev/null; do
  sleep 5
  SZ=$(du -m "${LOCAL_SOURCE}" 2>/dev/null | cut -f1 || echo 0)
  NOW=$(date +%s)
  ELAPSED=$(( NOW - DL_START ))
  if [ "${SZ}" -gt 0 ] && [ "${ELAPSED}" -gt 0 ] && [ "${TOTAL_MB:-0}" -gt 0 ]; then
    SPEED_MBS=$(( SZ / ELAPSED ))
    REMAINING_MB=$(( TOTAL_MB - SZ ))
    if [ "${SPEED_MBS}" -gt 0 ]; then
      ETA_SEC=$(( REMAINING_MB / SPEED_MBS ))
      ETA_MIN=$(( ETA_SEC / 60 ))
      ETA_S=$(( ETA_SEC % 60 ))
      log "  downloading... ${SZ}/${TOTAL_MB}MB (${SPEED_MBS}MB/s, ETA ${ETA_MIN}m${ETA_S}s)"
    else
      log "  downloading... ${SZ}/${TOTAL_MB}MB"
    fi
  else
    log "  downloading... ${SZ}MB so far"
  fi
  if [ "${SZ}" -eq "${LAST_SZ}" ]; then
    STALL_TICKS=$((STALL_TICKS+1))
  else
    STALL_TICKS=0
  fi
  LAST_SZ="${SZ}"
  if [ "${STALL_TICKS}" -ge 12 ]; then
    log "ERROR: download stalled for 60s at ${SZ}MB — killing and retrying on next run"
    kill "${DL_PID}" 2>/dev/null || true
    exit 1
  fi
done
wait "${DL_PID}" || { log "ERROR: aria2c download failed:"; tail -20 "${WORKDIR}/download.log"; exit 1; }
SRC_SIZE_MB=$(du -m "${LOCAL_SOURCE}" | cut -f1)
log "Downloaded: ${SRC_SIZE_MB}MB -> ${LOCAL_SOURCE}"

TARGET_SPEED=2.87

log "--- System info ---"
free -m | sed 's/^/  /' | while read -r l; do log "$l"; done
log "  ffmpeg: $(ffmpeg -version | head -1)"
log "Target: ${TARGET_SPEED}x realtime (i.e. ${TOTAL_DUR}s source in $(python3 -c "print(round(${TOTAL_DUR}/${TARGET_SPEED}))")s)"

PROGRESS="${WORKDIR}/progress.log"
STDOUT="${WORKDIR}/ffmpeg_stdout.log"

log "--- Starting single-pass encode (libx264, ${NPROC} threads) ---"
ENCODE_DUR_ARGS=()
if [ -n "${TEST_DURATION_SEC:-}" ]; then
  log "TEST MODE: limiting encode to first ${TEST_DURATION_SEC}s"
  ENCODE_DUR_ARGS=(-t "${TEST_DURATION_SEC}")
fi
stdbuf -oL -eL ffmpeg -y -v info \
  -i "${LOCAL_SOURCE}" \
  -i "${MASK_PNG}" \
  -filter_complex_threads "${NPROC}" \
  -filter_complex "${FILTER_COMPLEX}" \
  -map "[v]" \
  -map "0:a:0?" \
  "${ENCODE_DUR_ARGS[@]}" \
  -c:v libx264 -preset ultrafast -b:v "${BITRATE}" -threads 0 \
  -c:a aac -ac 2 -b:a 192k \
  -movflags +faststart \
  -progress "${PROGRESS}" \
  "${OUTPUT_EQUIRECT}" > "${STDOUT}" 2>&1 &
FFMPEG_PID=$!

MIN_SPEED=1.3          # sustained floor — below this on 3 consecutive windows → reject and redispatch
ABORT_MIN=1.15         # = MIN_SPEED - 0.15; instantaneous floor per sampling window
WARMUP_TICKS=12        # ~60s warm-up before sustained checks begin (tick = 5s)
SAMPLE_EVERY=6         # evaluate instantaneous speed every ~30s
CONSEC_SLOW=0          # consecutive sub-ABORT_MIN windows
PREV_OT_US=0
PREV_WALL=$(date +%s)

TICK=0
while kill -0 "${FFMPEG_PID}" 2>/dev/null; do
  TICK=$((TICK+1))
  sleep 5
  fr=$(grep -a "^frame=" "${PROGRESS}" | tail -1 | cut -d= -f2 || true)
  fps=$(grep -a "^fps=" "${PROGRESS}" | tail -1 | cut -d= -f2 || true)
  ot=$(grep -a "^out_time=" "${PROGRESS}" | tail -1 | cut -d= -f2 || true)
  ot_us=$(grep -a "^out_time_us=" "${PROGRESS}" | tail -1 | cut -d= -f2 || true)
  sp=$(grep -a "^speed=" "${PROGRESS}" | tail -1 | cut -d= -f2 || true)
  sp_num=$(echo "${sp:-0}" | tr -d 'x')
  vs_target=$(python3 -c "print(f'{${sp_num:-0}/${TARGET_SPEED}*100:.0f}%')" 2>/dev/null || echo "?")
  log "  frame=${fr:-0} fps=${fps:-0} t=${ot:-0:00:00} speed=${sp:-0}x (${vs_target} of ${TARGET_SPEED}x target)"
  echo "${sp_num:-0}" > "${WORKDIR}/SPEED" 2>/dev/null || true

  # ── Sustained-speed monitor: abort after 3 consecutive slow windows ───────
  # Catches hosts that start fast then collapse (observed: 9950X3D 1.8x→0.7x).
  if [ "${TICK}" -ge "${WARMUP_TICKS}" ] && [ $((TICK % SAMPLE_EVERY)) -eq 0 ]; then
    NOW_WALL=$(date +%s)
    INST=$(python3 -c "
try:
    dot = (${ot_us:-0} - ${PREV_OT_US}) / 1e6
    dw  = ${NOW_WALL} - ${PREV_WALL}
    print(round(dot/dw, 3) if dw > 0 else 0)
except Exception:
    print(0)
" 2>/dev/null || echo 0)
    PREV_OT_US="${ot_us:-0}"
    PREV_WALL="${NOW_WALL}"
    BELOW=$(python3 -c "print('yes' if float('${INST:-0}') < ${ABORT_MIN} else 'no')" 2>/dev/null || echo "no")
    if [ "${BELOW}" = "yes" ]; then
      CONSEC_SLOW=$((CONSEC_SLOW+1))
      log "  SUSTAINED CHECK: instantaneous ${INST}x < ${ABORT_MIN}x (${CONSEC_SLOW}/3 slow windows)"
    else
      [ "${CONSEC_SLOW}" -gt 0 ] && log "  SUSTAINED CHECK: instantaneous ${INST}x recovered — resetting"
      CONSEC_SLOW=0
    fi
    if [ "${CONSEC_SLOW}" -ge 3 ]; then
      log "SUSTAINED CHECK FAILED: 3 consecutive windows below ${ABORT_MIN}x — killing encode and redispatching"
      kill "${FFMPEG_PID}" 2>/dev/null || true
      wait "${FFMPEG_PID}" 2>/dev/null || true
      echo "${INST:-0}" > "${WORKDIR}/SPEED" 2>/dev/null || true
      cp "${WORKDIR}/SPEED" /tmp/ffa360/SPEED 2>/dev/null || true
      echo "BENCHMARK_FAILED" > "${WORKDIR}/FAILED"
      cp "${WORKDIR}/FAILED" /tmp/ffa360/FAILED 2>/dev/null || true
      exit 1
    fi
  fi
  # ──────────────────────────────────────────────────────────────────────────

  if [ $((TICK % 6)) -eq 0 ]; then
    log "  -- ps snapshot --"
    ps -eo pid,pcpu,nlwp,comm | grep -i ffmpeg | sed 's/^/    /' | while read -r l; do log "$l"; done
  fi
done

set +e
wait "${FFMPEG_PID}"
FFMPEG_EXIT=$?
set -e
log "FFmpeg exited with code ${FFMPEG_EXIT}"

# Always dump the last 30 lines of FFmpeg stderr so we can diagnose failures
log "--- FFmpeg tail (last 30 lines) ---"
tail -30 "${STDOUT}" | while IFS= read -r l; do log "  $l"; done

# Don't trust exit code alone — libx264 occasionally exits non-zero on
# successful encodes (e.g. SIGPIPE on the progress pipe, or muxing overhead
# warnings). Check the output file exists and has a sensible duration instead.
if [ ! -f "${OUTPUT_EQUIRECT}" ]; then
  log "ERROR: FFmpeg produced no output file (exit ${FFMPEG_EXIT})"
  exit 1
fi

SIZE_MB=$(du -m "${OUTPUT_EQUIRECT}" | cut -f1)
if [ "${SIZE_MB}" -lt 10 ]; then
  log "ERROR: Output suspiciously small (${SIZE_MB}MB, exit ${FFMPEG_EXIT}) — transcode likely failed"
  exit 1
fi

# Duration check against source — catches truncated encodes
OUT_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "${OUTPUT_EQUIRECT}" || echo "0")
MIN_DURATION=$(python3 -c "print(${TOTAL_DUR} * 0.9)")
DURATION_OK=$(python3 -c "exit(0 if float('${OUT_DURATION}') >= float('${MIN_DURATION}') else 1)" && echo yes || echo no)
if [ "${DURATION_OK}" = "no" ]; then
  log "ERROR: Output duration ${OUT_DURATION}s < 90% of source ${TOTAL_DUR}s (exit ${FFMPEG_EXIT}) — encode truncated"
  exit 1
fi

if [ "${FFMPEG_EXIT}" != "0" ]; then
  log "WARNING: FFmpeg exit code was ${FFMPEG_EXIT} but output looks complete (${SIZE_MB}MB, ${OUT_DURATION}s) — treating as success"
fi

log "Transcode complete: ${SIZE_MB}MB, duration ${OUT_DURATION}s"

# Free up disk space — source file no longer needed, and the metadata
# injection step used to `cp` the output (briefly doubling disk usage,
# which caused "No space left on device" on a 60GB disk).
rm -f "${LOCAL_SOURCE}"
log "Removed local source (${SRC_SIZE_MB}MB freed)"

if [ "${SIZE_MB}" -lt 10 ]; then
  log "ERROR: Output is suspiciously small (${SIZE_MB}MB) — transcode likely failed"
  exit 1
fi

# Duration check: compare output duration against expected source duration.
# Catches truncated reads (e.g. CDN connection drop) that still produce a
# valid, non-tiny MP4 but only cover a fraction of the source.
OUT_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "${OUTPUT_EQUIRECT}")
if [ -n "${EXPECTED_DURATION_SEC:-}" ]; then
  MIN_DURATION=$(python3 -c "print(${EXPECTED_DURATION_SEC} * 0.9)")
  if python3 -c "exit(0 if float('${OUT_DURATION}') >= float('${MIN_DURATION}') else 1)"; then
    log "Duration check OK: ${OUT_DURATION}s (expected >= ${MIN_DURATION}s)"
  else
    log "ERROR: Output duration ${OUT_DURATION}s is less than 90% of expected ${EXPECTED_DURATION_SEC}s — source likely truncated (CDN drop)"
    exit 1
  fi
else
  log "Output duration: ${OUT_DURATION}s (EXPECTED_DURATION_SEC not provided — skipping duration check)"
fi

# ── Inject 360° metadata ──────────────────────────────────────────────────────
log ""
log "--- Injecting 360° XMP metadata ---"
mv "${OUTPUT_EQUIRECT}" "${OUTPUT_FINAL}"
exiftool \
  -api LargeFileSupport=1 \
  -overwrite_original \
  -XMP-GSpherical:Spherical=true \
  -XMP-GSpherical:Stitched=true \
  -XMP-GSpherical:StitchingSoftware=FFmpeg \
  -XMP-GSpherical:ProjectionType=equirectangular \
  "${OUTPUT_FINAL}" > /dev/null
log "Metadata injected"

# ── Upload chapter to Google Drive Inbox ────────────────────────────────────
log ""
log "--- Uploading chapter to Drive Inbox ---"

python3 - <<PYEOF
import json, sys, os
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, "${WORKDIR}")

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

CREDS_PATH = Path("${WORKDIR}/youtube_credentials.json")
TOKEN_PATH = Path("${WORKDIR}/youtube_token.json")
VIDEO_PATH = Path("${OUTPUT_FINAL}")

SESSION_PREFIX = "${SESSION_PREFIX}"
CHAPTER_NUM    = "${CHAPTER_NUM}"      # already zero-padded, e.g. "01"
TOTAL_CHAPTERS = "${TOTAL_CHAPTERS}"
MEDIA_ID       = "${MEDIA_ID}"
FILENAME       = "${FILENAME}"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/drive",
]

import urllib.request as _urllib_req

creds_data = json.loads(CREDS_PATH.read_text())
token_data = json.loads(TOKEN_PATH.read_text())

_client_id = token_data.get("client_id") or creds_data["installed"]["client_id"]
_client_secret = token_data.get("client_secret") or creds_data["installed"]["client_secret"]
_refresh_token = token_data["refresh_token"]
_token_uri = token_data.get("token_uri", "https://oauth2.googleapis.com/token")

print(f"[{datetime.now().strftime('%H:%M:%S')}] Refreshing Drive access token...")
_refresh_payload = json.dumps({
    "client_id": _client_id,
    "client_secret": _client_secret,
    "refresh_token": _refresh_token,
    "grant_type": "refresh_token",
}).encode()
_req = _urllib_req.Request(
    _token_uri,
    data=_refresh_payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
with _urllib_req.urlopen(_req) as _resp:
    _tok = json.loads(_resp.read())

if "access_token" not in _tok:
    raise RuntimeError(f"Token refresh failed: {_tok}")

print(f"[{datetime.now().strftime('%H:%M:%S')}] Token refreshed OK (expires_in={_tok.get('expires_in')}s)")

creds = Credentials(
    token=_tok["access_token"],
    refresh_token=_refresh_token,
    token_uri=_token_uri,
    client_id=_client_id,
    client_secret=_client_secret,
    scopes=SCOPES,
)

drive = build("drive", "v3", credentials=creds)

def find_or_create_folder(name, parent_id=None):
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = drive.files().list(q=q, fields="files(id,name)", pageSize=10).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    folder = drive.files().create(body=meta, fields="id").execute()
    return folder["id"]

# Root/Inbox structure kept separate from XbotGo's own "XbotGo/Inbox" tree
root_id = find_or_create_folder("FFA360")
inbox_id = find_or_create_folder("Inbox", parent_id=root_id)

# Zero-padded chapter number ensures correct string-sort order downstream
# e.g. 0419_ch01.mp4, 0419_ch02.mp4, 0419_ch03.mp4
drive_filename = f"{SESSION_PREFIX}_ch{CHAPTER_NUM}.mp4"

print(f"[{datetime.now().strftime('%H:%M:%S')}] Uploading as: {drive_filename}")
print(f"[{datetime.now().strftime('%H:%M:%S')}] File size: {VIDEO_PATH.stat().st_size / 1e9:.2f} GB")

media = MediaFileUpload(
    str(VIDEO_PATH),
    mimetype="video/mp4",
    resumable=True,
    chunksize=10 * 1024 * 1024,
)

request = drive.files().create(
    body={
        "name": drive_filename,
        "parents": [inbox_id],
        "description": (
            f"FFA_MEDIA_ID:{MEDIA_ID};FFA_FILENAME:{FILENAME};"
            f"FFA_SESSION:{SESSION_PREFIX};FFA_CHAPTER:{CHAPTER_NUM};"
            f"FFA_TOTAL_CHAPTERS:{TOTAL_CHAPTERS}"
        ),
    },
    media_body=media,
    fields="id",
)

file_id = None
while file_id is None:
    status, response = request.next_chunk()
    if status:
        pct = int(status.progress() * 100)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Upload progress: {pct}%")
    if response:
        file_id = response["id"]

print(f"[{datetime.now().strftime('%H:%M:%S')}] Uploaded to Drive Inbox: {drive_filename} (file_id={file_id})")
print(f"DRIVE_FILE_ID={file_id}")
Path("/tmp/drive_file_id.txt").write_text(file_id)
Path("/tmp/drive_filename.txt").write_text(drive_filename)
PYEOF

DRIVE_FILE_ID=$(cat /tmp/drive_file_id.txt 2>/dev/null || echo "")
DRIVE_FILENAME=$(cat /tmp/drive_filename.txt 2>/dev/null || echo "")

if [ -z "${DRIVE_FILE_ID}" ]; then
  log "ERROR: Drive upload failed — no file ID returned"
  exit 1
fi

log ""
log "=== SUCCESS ==="
log "Drive filename : ${DRIVE_FILENAME}"
log "Drive file ID  : ${DRIVE_FILE_ID}"
log "(Final concat + YouTube upload happens later, once all chapters are in the Inbox)"

log ""
log "--- Cleaning up ---"
# Record final sustained speed for offer-reputation capture by the workflow
FINAL_SPEED=$(grep -a "^speed=" "${PROGRESS}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d 'x ' || true)
echo "${FINAL_SPEED:-0}" > "/tmp/ffa360/SPEED" 2>/dev/null || true
# Write marker files OUTSIDE WORKDIR so rm -rf doesn't delete them before poll sees them
touch "/tmp/ffa360_DONE"
RESULT_LINE="drive:${DRIVE_FILENAME:-unknown}:${DRIVE_FILE_ID:-unknown}"
echo "${RESULT_LINE}" > "/tmp/ffa360_RESULT_URL" || true
# Also write inside WORKDIR for compatibility
touch "${WORKDIR}/DONE"
echo "${RESULT_LINE}" > "${WORKDIR}/RESULT_URL" || true
log "Done. Instance will now terminate."
# Keep WORKDIR intact — instance is terminating anyway, no need to clean up


