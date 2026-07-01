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
# Runs the dominant cost (v360 EAC→equirect remap + x264) on a synthetic source
# at the real output resolution, so a slow/contended host is rejected in <90s
# instead of after a 14GB download + partial encode. Hang-safe by construction:
# synthetic source, hard -t and -frames:v caps, timeout wrapper, progress to a
# file (no blocking pipe), and -f null (no disk write). This is why the earlier
# pre-download benchmark hung; those failure modes are all closed here.
BENCH_W=7680
BENCH_H=3840
PREFLIGHT_MIN=0.85     # lowered from 1.25 — 8K pipeline peaks ~0.56x on real vast.ai hardware
PREFLIGHT_LOG="${WORKDIR}/preflight.log"
log "--- Preflight benchmark (v360 ${BENCH_W}x${BENCH_H} + x264, 5s synthetic, floor ${PREFLIGHT_MIN}x) ---"
rm -f "${PREFLIGHT_LOG}"
set +e
timeout 90 ffmpeg -y -v error -nostdin \
  -f lavfi -i "testsrc2=size=${BENCH_W}x${BENCH_H}:rate=30" \
  -t 5 -frames:v 150 -an \
  -vf "v360=eac:e:interp=linear:w=${BENCH_W}:h=${BENCH_H},format=yuv420p" \
  -c:v libx264 -preset ultrafast -b:v 20M -threads 0 \
  -progress "${PREFLIGHT_LOG}" -f null - > /dev/null 2>&1
PF_RC=$?
set -e
PF_SPEED=$(grep -a "^speed=" "${PREFLIGHT_LOG}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d 'x ' || true)
log "  preflight exit=${PF_RC} speed=${PF_SPEED:-?}x"
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

# ── Upload to YouTube ─────────────────────────────────────────────────────────
log ""
log "--- Uploading to YouTube ---"

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

FILENAME = "${FILENAME}"
MEDIA_ID = "${MEDIA_ID}"
CAPTURED_AT = "${CAPTURED_AT}"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/drive",
]

import urllib.request as _urllib_req

creds_data = json.loads(CREDS_PATH.read_text())
token_data = json.loads(TOKEN_PATH.read_text())

# Determine client_id / client_secret — prefer token file, fall back to creds file
_client_id = token_data.get("client_id") or creds_data["installed"]["client_id"]
_client_secret = token_data.get("client_secret") or creds_data["installed"]["client_secret"]
_refresh_token = token_data["refresh_token"]
_token_uri = token_data.get("token_uri", "https://oauth2.googleapis.com/token")

# Always do a live token refresh — never rely on stored access token
print(f"[{datetime.now().strftime('%H:%M:%S')}] Refreshing YouTube access token...")
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

yt = build("youtube", "v3", credentials=creds)

try:
    dt = datetime.fromisoformat(CAPTURED_AT.replace("Z", "+00:00"))
    day_name = dt.strftime("%A")
    day = dt.day
    suffix = "th" if 11 <= day <= 13 else {1:"st",2:"nd",3:"rd"}.get(day%10,"th")
    date_str = dt.strftime(f"%-d{suffix} %B %Y")
except Exception:
    day_name = "Session"
    date_str = CAPTURED_AT[:10]

title = f"{day_name} Session | {date_str} | FFA Leicester | 360°"
description = (
    f"FFA Leicester — {day_name} session footage captured {date_str}.\\n"
    f"360° video — use a VR headset or drag to look around.\\n\\n"
    f"FFA_MEDIA_ID:{MEDIA_ID}\\n"
    f"FFA_FILENAME:{FILENAME}\\n"
    f"FFA_CAPTURED_AT:{CAPTURED_AT}\\n"
    f"FFA_360:true"
)

print(f"[{datetime.now().strftime('%H:%M:%S')}] Title: {title}")
print(f"[{datetime.now().strftime('%H:%M:%S')}] File size: {VIDEO_PATH.stat().st_size / 1e9:.2f} GB")

media = MediaFileUpload(
    str(VIDEO_PATH),
    mimetype="video/mp4",
    resumable=True,
    chunksize=10 * 1024 * 1024,
)

request = yt.videos().insert(
    part="snippet,status",
    body={
        "snippet": {
            "title": title,
            "description": description,
            "tags": ["FFA", "Football For All", "Leicester", "360", "football"],
            "categoryId": "17",
        },
        "status": {
            "privacyStatus": "unlisted",
        },
    },
    media_body=media,
)

yt_id = None
while yt_id is None:
    status, response = request.next_chunk()
    if status:
        pct = int(status.progress() * 100)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Upload progress: {pct}%")
    if response:
        yt_id = response["id"]

print(f"[{datetime.now().strftime('%H:%M:%S')}] Uploaded successfully: https://www.youtube.com/watch?v={yt_id}")
print(f"YT_ID={yt_id}")
Path("/tmp/yt_id.txt").write_text(yt_id)
PYEOF

YT_ID=$(cat /tmp/yt_id.txt 2>/dev/null || echo "")

if [ -z "${YT_ID}" ]; then
  log "ERROR: YouTube upload failed — no video ID returned"
  exit 1
fi

log ""
log "=== SUCCESS ==="
log "YouTube ID : ${YT_ID}"
log "URL        : https://www.youtube.com/watch?v=${YT_ID}"

# ── Update uploaded.db via GitHub API ─────────────────────────────────────────
log ""
log "--- Updating uploaded.db ---"

python3 - <<PYEOF
import json, base64, sqlite3, urllib.request, tempfile, os
from pathlib import Path
from datetime import datetime, timezone

TOKEN = "${GH_PAT}"
REPO = "${REPO}"
MEDIA_ID = "${MEDIA_ID}"
FILENAME = "${FILENAME}"
CAPTURED_AT = "${CAPTURED_AT}"
YT_ID = "${YT_ID}"

def gh_get(path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/{path}",
        headers={"Authorization": f"token {TOKEN}", "Accept": "application/vnd.github.v3+json"}
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

data = gh_get("contents/uploaded.db")
db_content = base64.b64decode(data["content"])
sha = data["sha"]

with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
    f.write(db_content)
    tmp_path = f.name

con = sqlite3.connect(tmp_path)
con.execute("""CREATE TABLE IF NOT EXISTS uploads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id TEXT UNIQUE,
    filename TEXT,
    captured_at TEXT,
    youtube_id TEXT,
    uploaded_at TEXT
)""")
con.execute(
    "INSERT OR REPLACE INTO uploads (media_id, filename, captured_at, youtube_id, uploaded_at) VALUES (?,?,?,?,?)",
    (MEDIA_ID, FILENAME, CAPTURED_AT, YT_ID, datetime.now(timezone.utc).isoformat())
)
con.commit()
con.close()

encoded = base64.b64encode(Path(tmp_path).read_bytes()).decode()
os.unlink(tmp_path)

payload = json.dumps({
    "message": f"chore: mark {FILENAME} uploaded (360) [skip ci]",
    "content": encoded,
    "sha": sha,
    "branch": "main"
}).encode()

req = urllib.request.Request(
    f"https://api.github.com/repos/{REPO}/contents/uploaded.db",
    data=payload, method="PUT",
    headers={"Authorization": f"token {TOKEN}", "Content-Type": "application/json"}
)
with urllib.request.urlopen(req) as r:
    result = json.loads(r.read())
print(f"uploaded.db committed: {result['commit']['sha']}")
PYEOF

log ""
log "--- Cleaning up ---"
# Record final sustained speed for offer-reputation capture by the workflow
FINAL_SPEED=$(grep -a "^speed=" "${PROGRESS}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d 'x ' || true)
echo "${FINAL_SPEED:-0}" > "/tmp/ffa360/SPEED" 2>/dev/null || true
# Write marker files OUTSIDE WORKDIR so rm -rf doesn't delete them before poll sees them
touch "/tmp/ffa360_DONE"
YT_URL_LINE="https://www.youtube.com/watch?v=${YT_ID:-unknown}"
echo "${YT_URL_LINE}" > "/tmp/ffa360_RESULT_URL" || true
# Also write inside WORKDIR for compatibility
touch "${WORKDIR}/DONE"
echo "${YT_URL_LINE}" > "${WORKDIR}/RESULT_URL" || true
log "Done. Instance will now terminate."
# Keep WORKDIR intact — instance is terminating anyway, no need to clean up


