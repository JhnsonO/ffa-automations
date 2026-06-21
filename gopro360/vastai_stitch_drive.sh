#!/usr/bin/env bash
# FFA GoPro MAX 360 — Vast.ai stitch + Google Drive upload script
# Runs on the Vast.ai instance. Called via SSH from GitHub Actions.
#
# Required env vars (passed via SSH):
#   SOURCE_URL        — GoPro CDN concat URL for the .360 file
#   MEDIA_ID          — GoPro media ID
#   FILENAME          — original .360 filename e.g. GS010404.360
#   CAPTURED_AT       — ISO8601 capture timestamp
#   YOUTUBE_CREDS     — JSON string of youtube_credentials.json (used for Drive auth)
#   YOUTUBE_TOKEN     — JSON string of youtube_token.json (used for Drive auth)
#   GH_PAT            — GitHub PAT for committing uploaded.db back
#   REPO              — GitHub repo e.g. JhnsonO/ffa-automations
#   TRANSCODE_BITRATE — FFmpeg output bitrate e.g. 20M (default: auto)
#   DRIVE_FOLDER_ID   — Google Drive folder ID to upload into

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
DRIVE_FOLDER_ID="${DRIVE_FOLDER_ID:-1gHW29JbvUWnbvJTCC0J8O7r-O1IPZAd8}"

log() {
  echo "[$(date -u +%H:%M:%S)] $*"
}

log "=== FFA GoPro 360 Stitch + Drive Upload ==="
log "File     : ${FILENAME}"
log "Media ID : ${MEDIA_ID}"
log "Captured : ${CAPTURED_AT}"
log "Bitrate  : ${BITRATE}"
log "Drive    : ${DRIVE_FOLDER_ID}"

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

log "--- Writing credentials ---"
echo "${YOUTUBE_CREDS}" > youtube_credentials.json
echo "${YOUTUBE_TOKEN}" > youtube_token.json
log "Credentials written"

# ── Generate seam-blend gradient mask ──────────────────────────────────────────
log "--- Generating seam blend mask ---"
python3 - <<'PY'
from PIL import Image
w, h = 64, 1344
img = Image.new("L", (w, h))
row = bytes(int(x * 255 / (w - 1)) for x in range(w))
data = row * h
img.putdata(list(data))
img.save("/tmp/ffa360/seam_mask.png")
PY
log "Seam mask generated: ${MASK_PNG}"

# ── FFmpeg filter chain ──────────────────────────────────────────────────────
FILTER_COMPLEX="\
[1:v]format=gray,split=4[mask1][mask2][mask3][mask4],\
[0:0]crop=624:1344:x=0:y=0,format=yuvj420p[left],\
[0:0]crop=624:1344:x=752:y=0,format=yuvj420p[right],\
[0:0]crop=64:1344:x=624:y=0,format=yuvj420p[segA],\
[0:0]crop=64:1344:x=688:y=0,format=yuvj420p[segB],\
[segA][segB][mask1]maskedmerge[crop],\
[crop]scale=96:1344[cropScaled],\
[left][cropScaled]hstack[leftAll],[leftAll][right]hstack[leftDone],\
[0:0]crop=1344:1344:1376:0[middle],\
[0:0]crop=624:1344:x=2720:y=0,format=yuvj420p[lRB],\
[0:0]crop=624:1344:x=3472:y=0,format=yuvj420p[rRB],\
[0:0]crop=64:1344:x=3344:y=0,format=yuvj420p[segARB],\
[0:0]crop=64:1344:x=3408:y=0,format=yuvj420p[segBRB],\
[segARB][segBRB][mask2]maskedmerge[cropRB],\
[cropRB]scale=96:1344[cropRBScaled],\
[lRB][cropRBScaled]hstack[rAll],[rAll][rRB]hstack[rBotDone],\
[leftDone][middle]hstack[lMid],[lMid][rBotDone]hstack[botComplete],\
[0:1]crop=624:1344:x=0:y=0,format=yuvj420p[flt],\
[0:1]crop=624:1344:x=752:y=0,format=yuvj420p[frt],\
[0:1]crop=64:1344:x=624:y=0,format=yuvj420p[segC],\
[0:1]crop=64:1344:x=688:y=0,format=yuvj420p[segD],\
[segC][segD][mask3]maskedmerge[ltc],\
[ltc]scale=96:1344[ltcScaled],\
[flt][ltcScaled]hstack[tlh],[tlh][frt]hstack[tlDone],\
[0:1]crop=1344:1344:1376:0[tMid],\
[0:1]crop=624:1344:x=2720:y=0,format=yuvj420p[tlRB],\
[0:1]crop=624:1344:x=3472:y=0,format=yuvj420p[trRB],\
[0:1]crop=64:1344:x=3344:y=0,format=yuvj420p[segE],\
[0:1]crop=64:1344:x=3408:y=0,format=yuvj420p[segF],\
[segE][segF][mask4]maskedmerge[tcRB],\
[tcRB]scale=96:1344[tcRBScaled],\
[tlRB][tcRBScaled]hstack[trAll],[trAll][trRB]hstack[trBotDone],\
[tlDone][tMid]hstack[tlMid],[tlMid][trBotDone]hstack[topComplete],\
[botComplete][topComplete]vstack[complete],\
[complete]v360=eac:e:interp=linear,crop=4032:2388:x=0:y=0[v]"

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

# ── Download source to local NVMe ───────────────────────────────────────────
LOCAL_SOURCE="${WORKDIR}/source.360"
log "--- HEAD request diagnostic ---"
HEAD_OUT=$(curl -sI --connect-timeout 15 --max-time 30 "${SOURCE_URL}" || true)
echo "$HEAD_OUT" | head -10
TOTAL_BYTES=$(echo "$HEAD_OUT" | grep -i '^content-length:' | tr -d '[:space:]\r' | cut -d: -f2 || echo 0)
TOTAL_MB=$(( (${TOTAL_BYTES:-0} + 1048575) / 1048576 ))
log "  Total file size: ${TOTAL_MB}MB"

SOURCE_MODE="${SOURCE_MODE:-gopro}"
log "Source mode: ${SOURCE_MODE}"

if [ "${SOURCE_MODE}" = "drive" ]; then
  log "--- Downloading from Google Drive (Drive API, authenticated) ---"
  # Extract the file ID from the SOURCE_URL query string
  DRIVE_FILE_ID=$(echo "${SOURCE_URL}" | grep -oP '(?<=id=)[^&]+')
  log "Drive file ID: ${DRIVE_FILE_ID}"
  # Use the Drive API with the existing OAuth token — bypasses all confirmation walls
  python3 - <<DRIVEEOF
import json, sys, urllib.request
from pathlib import Path
from datetime import datetime

CREDS_PATH = Path("${WORKDIR}/youtube_credentials.json")
TOKEN_PATH  = Path("${WORKDIR}/youtube_token.json")
FILE_ID     = "${DRIVE_FILE_ID}"
OUT_PATH    = "${LOCAL_SOURCE}"
TOTAL_MB    = ${TOTAL_MB:-0}

creds_data = json.loads(CREDS_PATH.read_text())
token_data  = json.loads(TOKEN_PATH.read_text())

client_id     = token_data.get("client_id")     or creds_data["installed"]["client_id"]
client_secret = token_data.get("client_secret") or creds_data["installed"]["client_secret"]
refresh_token = token_data["refresh_token"]
token_uri     = token_data.get("token_uri", "https://oauth2.googleapis.com/token")

# Refresh access token
print(f"[{datetime.now().strftime('%H:%M:%S')}] Refreshing token...")
resp = urllib.request.urlopen(urllib.request.Request(
    token_uri,
    data=json.dumps({"client_id": client_id, "client_secret": client_secret,
                     "refresh_token": refresh_token, "grant_type": "refresh_token"}).encode(),
    headers={"Content-Type": "application/json"}, method="POST"
))
tok = json.loads(resp.read())
if "access_token" not in tok:
    print(f"Token refresh failed: {tok}", file=sys.stderr)
    sys.exit(1)
access_token = tok["access_token"]
print(f"[{datetime.now().strftime('%H:%M:%S')}] Token OK")

# Stream download via Drive API alt=media
url = f"https://www.googleapis.com/drive/v3/files/{FILE_ID}?alt=media"
req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
chunk = 8 * 1024 * 1024  # 8MB chunks
downloaded = 0
start = datetime.now()
with urllib.request.urlopen(req) as r, open(OUT_PATH, "wb") as f:
    while True:
        buf = r.read(chunk)
        if not buf:
            break
        f.write(buf)
        downloaded += len(buf)
        mb = downloaded / 1e6
        elapsed = (datetime.now() - start).total_seconds()
        speed = mb / elapsed if elapsed > 0 else 0
        print(f"[{datetime.now().strftime('%H:%M:%S')}]   downloaded {mb:.0f}MB ({speed:.1f}MB/s)")
print(f"[{datetime.now().strftime('%H:%M:%S')}] Download complete: {downloaded/1e6:.0f}MB")
DRIVEEOF
  log "Drive download complete: $(du -m "${LOCAL_SOURCE}" | cut -f1)MB"
else
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
fi  # end SOURCE_MODE branch
SRC_SIZE_MB=$(du -m "${LOCAL_SOURCE}" | cut -f1)
log "Downloaded: ${SRC_SIZE_MB}MB -> ${LOCAL_SOURCE}"

TARGET_SPEED=4.0

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

MIN_SPEED=1.7
SPEED_CHECK_TICK=9
SPEED_CHECKED=0

TICK=0
while kill -0 "${FFMPEG_PID}" 2>/dev/null; do
  TICK=$((TICK+1))
  sleep 5
  fr=$(grep -a "^frame=" "${PROGRESS}" | tail -1 | cut -d= -f2 || true)
  fps=$(grep -a "^fps=" "${PROGRESS}" | tail -1 | cut -d= -f2 || true)
  ot=$(grep -a "^out_time=" "${PROGRESS}" | tail -1 | cut -d= -f2 || true)
  sp=$(grep -a "^speed=" "${PROGRESS}" | tail -1 | cut -d= -f2 || true)
  sp_num=$(echo "${sp:-0}" | tr -d 'x')
  vs_target=$(python3 -c "print(f'{${sp_num:-0}/${TARGET_SPEED}*100:.0f}%')" 2>/dev/null || echo "?")
  log "  frame=${fr:-0} fps=${fps:-0} t=${ot:-0:00:00} speed=${sp:-0}x (${vs_target} of ${TARGET_SPEED}x target)"

  if [ "${SPEED_CHECKED}" = "0" ] && [ "${TICK}" -ge "${SPEED_CHECK_TICK}" ]; then
    SPEED_CHECKED=1
    SPEED_OK=$(python3 -c "
sp = '${sp_num}'
try:
    v = float(sp)
    print('yes' if v >= ${MIN_SPEED} else 'no')
except:
    print('unknown')
" 2>/dev/null || echo "unknown")

    if [ "${SPEED_OK}" = "no" ]; then
      log "SPEED CHECK FAILED: ${sp:-0}x is below ${MIN_SPEED}x floor — killing encode and redispatching"
      kill "${FFMPEG_PID}" 2>/dev/null || true
      wait "${FFMPEG_PID}" 2>/dev/null || true
      echo "BENCHMARK_FAILED" > "${WORKDIR}/FAILED"
      cp "${WORKDIR}/FAILED" /tmp/ffa360/FAILED 2>/dev/null || true
      exit 1
    elif [ "${SPEED_OK}" = "unknown" ]; then
      log "SPEED CHECK: could not read speed (sp='${sp:-}') — continuing without check"
    else
      log "SPEED CHECK PASSED: ${sp:-0}x >= ${MIN_SPEED}x — continuing full encode"
    fi
  fi

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

log "--- FFmpeg tail (last 30 lines) ---"
tail -30 "${STDOUT}" | while IFS= read -r l; do log "  $l"; done

if [ ! -f "${OUTPUT_EQUIRECT}" ]; then
  log "ERROR: FFmpeg produced no output file (exit ${FFMPEG_EXIT})"
  exit 1
fi

SIZE_MB=$(du -m "${OUTPUT_EQUIRECT}" | cut -f1)
if [ "${SIZE_MB}" -lt 10 ]; then
  log "ERROR: Output suspiciously small (${SIZE_MB}MB, exit ${FFMPEG_EXIT}) — transcode likely failed"
  exit 1
fi

OUT_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 "${OUTPUT_EQUIRECT}" || echo "0")
MIN_DURATION=$(python3 -c "print(${TOTAL_DUR} * 0.9)")
DURATION_OK=$(python3 -c "exit(0 if float('${OUT_DURATION}') >= float('${MIN_DURATION}') else 1)" && echo yes || echo no)
if [ "${DURATION_OK}" = "no" ]; then
  log "ERROR: Output duration ${OUT_DURATION}s < 90% of source ${TOTAL_DUR}s — encode truncated"
  exit 1
fi

if [ "${FFMPEG_EXIT}" != "0" ]; then
  log "WARNING: FFmpeg exit code was ${FFMPEG_EXIT} but output looks complete (${SIZE_MB}MB, ${OUT_DURATION}s) — treating as success"
fi

log "Transcode complete: ${SIZE_MB}MB, duration ${OUT_DURATION}s"

rm -f "${LOCAL_SOURCE}"
log "Removed local source (${SRC_SIZE_MB}MB freed)"

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

# ── Upload to Google Drive ────────────────────────────────────────────────────
log ""
log "--- Uploading to Google Drive ---"

python3 - <<PYEOF
import json, sys, os
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, "${WORKDIR}")

import urllib.request as _urllib_req
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

CREDS_PATH = Path("${WORKDIR}/youtube_credentials.json")
TOKEN_PATH  = Path("${WORKDIR}/youtube_token.json")
VIDEO_PATH  = Path("${OUTPUT_FINAL}")
FOLDER_ID   = "${DRIVE_FOLDER_ID}"

FILENAME    = "${FILENAME}"
MEDIA_ID    = "${MEDIA_ID}"
CAPTURED_AT = "${CAPTURED_AT}"

SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/drive.file",
]

creds_data = json.loads(CREDS_PATH.read_text())
token_data  = json.loads(TOKEN_PATH.read_text())

_client_id     = token_data.get("client_id")     or creds_data["installed"]["client_id"]
_client_secret = token_data.get("client_secret") or creds_data["installed"]["client_secret"]
_refresh_token = token_data["refresh_token"]
_token_uri     = token_data.get("token_uri", "https://oauth2.googleapis.com/token")

print(f"[{datetime.now().strftime('%H:%M:%S')}] Refreshing Drive access token...")
_refresh_payload = json.dumps({
    "client_id":     _client_id,
    "client_secret": _client_secret,
    "refresh_token": _refresh_token,
    "grant_type":    "refresh_token",
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

try:
    dt = datetime.fromisoformat(CAPTURED_AT.replace("Z", "+00:00"))
    day_name = dt.strftime("%A")
    day = dt.day
    suffix = "th" if 11 <= day <= 13 else {1:"st",2:"nd",3:"rd"}.get(day%10,"th")
    date_str = dt.strftime(f"%-d{suffix} %B %Y")
except Exception:
    day_name = "Session"
    date_str = CAPTURED_AT[:10]

drive_filename = f"{day_name} Session - {date_str} - FFA Leicester - 360.mp4"

print(f"[{datetime.now().strftime('%H:%M:%S')}] Filename : {drive_filename}")
print(f"[{datetime.now().strftime('%H:%M:%S')}] File size: {VIDEO_PATH.stat().st_size / 1e9:.2f} GB")
print(f"[{datetime.now().strftime('%H:%M:%S')}] Folder ID: {FOLDER_ID}")

file_metadata = {
    "name":    drive_filename,
    "parents": [FOLDER_ID],
}

media = MediaFileUpload(
    str(VIDEO_PATH),
    mimetype="video/mp4",
    resumable=True,
    chunksize=50 * 1024 * 1024,  # 50MB chunks — Drive handles large files better than YT
)

request = drive.files().create(
    body=file_metadata,
    media_body=media,
    fields="id,name,size,webViewLink",
)

file_id = None
response = None
while response is None:
    status, response = request.next_chunk()
    if status:
        pct = int(status.progress() * 100)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Upload progress: {pct}%")

file_id  = response["id"]
view_url = response.get("webViewLink", f"https://drive.google.com/file/d/{file_id}/view")

print(f"[{datetime.now().strftime('%H:%M:%S')}] Uploaded successfully!")
print(f"[{datetime.now().strftime('%H:%M:%S')}] File ID : {file_id}")
print(f"[{datetime.now().strftime('%H:%M:%S')}] URL     : {view_url}")
print(f"DRIVE_FILE_ID={file_id}")
Path("/tmp/drive_file_id.txt").write_text(file_id)
Path("/tmp/drive_url.txt").write_text(view_url)
PYEOF

DRIVE_FILE_ID=$(cat /tmp/drive_file_id.txt 2>/dev/null || echo "")
DRIVE_URL=$(cat /tmp/drive_url.txt 2>/dev/null || echo "")

if [ -z "${DRIVE_FILE_ID}" ]; then
  log "ERROR: Drive upload failed — no file ID returned"
  exit 1
fi

log ""
log "=== SUCCESS ==="
log "Drive File ID : ${DRIVE_FILE_ID}"
log "URL           : ${DRIVE_URL}"

# ── Update uploaded.db via GitHub API ─────────────────────────────────────────
log ""
log "--- Updating uploaded.db ---"

python3 - <<PYEOF
import json, base64, sqlite3, urllib.request, tempfile, os
from pathlib import Path
from datetime import datetime, timezone

TOKEN       = "${GH_PAT}"
REPO        = "${REPO}"
MEDIA_ID    = "${MEDIA_ID}"
FILENAME    = "${FILENAME}"
CAPTURED_AT = "${CAPTURED_AT}"
DRIVE_ID    = "${DRIVE_FILE_ID}"

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
# Store Drive file ID in youtube_id column for compatibility
con.execute(
    "INSERT OR REPLACE INTO uploads (media_id, filename, captured_at, youtube_id, uploaded_at) VALUES (?,?,?,?,?)",
    (MEDIA_ID, FILENAME, CAPTURED_AT, f"drive:{DRIVE_ID}", datetime.now(timezone.utc).isoformat())
)
con.commit()
con.close()

encoded = base64.b64encode(Path(tmp_path).read_bytes()).decode()
os.unlink(tmp_path)

payload = json.dumps({
    "message": f"chore: mark {FILENAME} uploaded (360-drive) [skip ci]",
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
touch "/tmp/ffa360_DONE"
echo "${DRIVE_URL}" > "/tmp/ffa360_RESULT_URL" || true
touch "${WORKDIR}/DONE"
echo "${DRIVE_URL}" > "${WORKDIR}/RESULT_URL" || true
log "Done. Instance will now terminate."
