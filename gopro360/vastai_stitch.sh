#!/usr/bin/env bash
# FFA GoPro MAX 360 — Vast.ai stitch + upload script
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
trap 'code=$?; if [ $code -ne 0 ]; then echo "$code" > "${WORKDIR_EARLY}/FAILED"; fi' EXIT

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
w, h = 64, 1344
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

# ── Download source to local NVMe ───────────────────────────────────────────
# Avoids N parallel remote seeks against the GoPro CDN (unreliable/slow);
# local -ss seeks are instant and frame-accurate.
LOCAL_SOURCE="${WORKDIR}/source.360"
log "--- HEAD request diagnostic ---"
curl -sI --connect-timeout 15 --max-time 30 "${SOURCE_URL}" | head -10 || log "HEAD request failed"

log "--- Downloading source to local disk ---"
curl -L --fail --retry 3 --retry-delay 5 \
  --connect-timeout 30 --max-time 1800 --speed-time 30 --speed-limit 1024 \
  -o "${LOCAL_SOURCE}" "${SOURCE_URL}" > "${WORKDIR}/download.log" 2>&1 &
DL_PID=$!
while kill -0 "${DL_PID}" 2>/dev/null; do
  sleep 5
  SZ=$(du -m "${LOCAL_SOURCE}" 2>/dev/null | cut -f1 || echo 0)
  log "  downloading... ${SZ}MB so far"
done
wait "${DL_PID}" || { log "ERROR: download failed:"; tail -20 "${WORKDIR}/download.log"; exit 1; }
SRC_SIZE_MB=$(du -m "${LOCAL_SOURCE}" | cut -f1)
log "Downloaded: ${SRC_SIZE_MB}MB -> ${LOCAL_SOURCE}"

NPROC=$(nproc)
TARGET_SPEED=4.0

log "--- System info ---"
log "  nproc: ${NPROC}"
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
if [ "${FFMPEG_EXIT}" != "0" ]; then
  log "ERROR: FFmpeg failed — last 30 lines:"
  tail -30 "${STDOUT}"
  exit 1
fi

# Check output exists and has reasonable size
if [ ! -f "${OUTPUT_EQUIRECT}" ]; then
  log "ERROR: FFmpeg produced no output file"
  exit 1
fi

SIZE_MB=$(du -m "${OUTPUT_EQUIRECT}" | cut -f1)
log "Transcode complete: ${SIZE_MB}MB"

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
from google.auth.transport.requests import Request
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
]

creds_data = json.loads(CREDS_PATH.read_text())
token_data = json.loads(TOKEN_PATH.read_text())

creds = Credentials(
    token=token_data.get("token"),
    refresh_token=token_data.get("refresh_token"),
    token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
    client_id=creds_data["installed"]["client_id"],
    client_secret=creds_data["installed"]["client_secret"],
    scopes=SCOPES,
)

if creds.expired and creds.refresh_token:
    creds.refresh(Request())
    TOKEN_PATH.write_text(json.dumps({
        "token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_uri": creds.token_uri,
    }))
    print(f"[{datetime.now().strftime('%H:%M:%S')}] Token refreshed")

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
touch "${WORKDIR}/DONE"
YT_URL_LINE="https://www.youtube.com/watch?v=${YT_ID:-unknown}"
echo "${YT_URL_LINE}" > "${WORKDIR}/RESULT_URL" || true
log "Done. Instance will now terminate."
# Note: cleanup of WORKDIR intentionally happens AFTER marker files are written
# so the polling step can see DONE before the directory disappears.
sleep 5
rm -rf "${WORKDIR}"
