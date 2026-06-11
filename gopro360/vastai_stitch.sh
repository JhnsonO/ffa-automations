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

BITRATE="${TRANSCODE_BITRATE:-20M}"
WORKDIR="/tmp/ffa360"
OUTPUT_EQUIRECT="${WORKDIR}/output.equirect.mp4"
OUTPUT_FINAL="${WORKDIR}/output.final.mp4"

echo "=== FFA GoPro 360 Stitch + Upload ==="
echo "File     : ${FILENAME}"
echo "Media ID : ${MEDIA_ID}"
echo "Captured : ${CAPTURED_AT}"
echo "Bitrate  : ${BITRATE}"
echo ""

# ── Setup ─────────────────────────────────────────────────────────────────────
mkdir -p "${WORKDIR}"
cd "${WORKDIR}"

echo "--- Installing dependencies ---"
# ffmpeg and exiftool — use parallel install for speed
apt-get update -qq
apt-get install -y -qq --no-install-recommends ffmpeg libimage-exiftool-perl python3-pip > /dev/null &
APT_PID=$!

# pip deps in parallel while apt runs
pip install -q --break-system-packages google-auth google-auth-oauthlib google-api-python-client 2>/dev/null &
PIP_PID=$!

wait $APT_PID && echo "apt done" || { echo "apt failed"; exit 1; }
wait $PIP_PID && echo "pip done" || pip install -q google-auth google-auth-oauthlib google-api-python-client

echo "--- Writing YouTube credentials ---"
echo "${YOUTUBE_CREDS}" > youtube_credentials.json
echo "${YOUTUBE_TOKEN}" > youtube_token.json

# ── FFmpeg filter chain (devclef/gopro-max-video-tools approach) ───────────────
# GoPro MAX .360 CDN concat file has 2 streams: 0:0 (front), 0:1 (rear)
# Each stream is a 4096x1344 EAC tile. We blend seams, vstack, then v360=eac:equirect.
DIV=65

geq() {
  echo "geq=lum='if(between(X,0,64),(p((X+64),Y)*(((X+1))/${DIV}))+(p(X,Y)*(($DIV-((X+1)))/${DIV})),p(X,Y))':cb='if(between(X,0,64),(p((X+64),Y)*(((X+1))/${DIV}))+(p(X,Y)*(($DIV-((X+1)))/${DIV})),p(X,Y))':cr='if(between(X,0,64),(p((X+64),Y)*(((X+1))/${DIV}))+(p(X,Y)*(($DIV-((X+1)))/${DIV})),p(X,Y))':a='if(between(X,0,64),(p((X+64),Y)*(((X+1))/${DIV}))+(p(X,Y)*(($DIV-((X+1)))/${DIV})),p(X,Y))'"
}

G=$(geq)

FILTER_COMPLEX="\
[0:0]crop=128:1344:x=624:y=0,format=yuvj420p,${G}:interpolation=b,crop=64:1344:x=0:y=0,format=yuvj420p,scale=96:1344[crop],\
[0:0]crop=624:1344:x=0:y=0,format=yuvj420p[left],\
[0:0]crop=624:1344:x=752:y=0,format=yuvj420p[right],\
[left][crop]hstack[leftAll],[leftAll][right]hstack[leftDone],\
[0:0]crop=1344:1344:1376:0[middle],\
[0:0]crop=128:1344:x=3344:y=0,format=yuvj420p,${G}:interpolation=b,crop=64:1344:x=0:y=0,format=yuvj420p,scale=96:1344[cropRB],\
[0:0]crop=624:1344:x=2720:y=0,format=yuvj420p[lRB],\
[0:0]crop=624:1344:x=3472:y=0,format=yuvj420p[rRB],\
[lRB][cropRB]hstack[rAll],[rAll][rRB]hstack[rBotDone],\
[leftDone][middle]hstack[lMid],[lMid][rBotDone]hstack[botComplete],\
[0:1]crop=128:1344:x=624:y=0,format=yuvj420p,${G}:interpolation=n,crop=64:1344:x=0:y=0,format=yuvj420p,scale=96:1344[ltc],\
[0:1]crop=624:1344:x=0:y=0,format=yuvj420p[flt],\
[0:1]crop=624:1344:x=752:y=0,format=yuvj420p[frt],\
[flt][ltc]hstack[tlh],[tlh][frt]hstack[tlDone],\
[0:1]crop=1344:1344:1376:0[tMid],\
[0:1]crop=128:1344:x=3344:y=0,format=yuvj420p,${G}:interpolation=n,crop=64:1344:x=0:y=0,format=yuvj420p,scale=96:1344[tcRB],\
[0:1]crop=624:1344:x=2720:y=0,format=yuvj420p[tlRB],\
[0:1]crop=624:1344:x=3472:y=0,format=yuvj420p[trRB],\
[tlRB][tcRB]hstack[trAll],[trAll][trRB]hstack[trBotDone],\
[tlDone][tMid]hstack[tlMid],[tlMid][trBotDone]hstack[topComplete],\
[botComplete][topComplete]vstack[complete],\
[complete]v360=eac:e:interp=cubic,crop=4032:2388:x=0:y=0[v]"

# ── Transcode ─────────────────────────────────────────────────────────────────
echo ""
echo "--- Starting FFmpeg transcode (this will take a while) ---"
echo "Source: ${SOURCE_URL:0:80}..."

ffmpeg -y \
  -i "${SOURCE_URL}" \
  -filter_complex "${FILTER_COMPLEX}" \
  -map "[v]" \
  -map "0:a:0?" \
  -c:v h264_nvenc \
  -b:v "${BITRATE}" \
  -preset fast \
  -c:a aac \
  -ac 2 \
  -b:a 192k \
  -movflags +faststart \
  "${OUTPUT_EQUIRECT}" 2>&1 | grep -E "^(frame=|size=|time=|speed=|error|Error|WARNING|Stream)" || true

# Check output exists and has reasonable size
if [ ! -f "${OUTPUT_EQUIRECT}" ]; then
  echo "ERROR: FFmpeg produced no output file"
  exit 1
fi

SIZE_MB=$(du -m "${OUTPUT_EQUIRECT}" | cut -f1)
echo "Transcode complete: ${SIZE_MB}MB"

if [ "${SIZE_MB}" -lt 10 ]; then
  echo "ERROR: Output is suspiciously small (${SIZE_MB}MB) — transcode likely failed"
  exit 1
fi

# ── Inject 360° metadata ──────────────────────────────────────────────────────
echo ""
echo "--- Injecting 360° XMP metadata ---"
cp "${OUTPUT_EQUIRECT}" "${OUTPUT_FINAL}"
exiftool \
  -api LargeFileSupport=1 \
  -overwrite_original \
  -XMP-GSpherical:Spherical=true \
  -XMP-GSpherical:Stitched=true \
  -XMP-GSpherical:StitchingSoftware=FFmpeg \
  -XMP-GSpherical:ProjectionType=equirectangular \
  "${OUTPUT_FINAL}"

echo "Metadata injected"

# ── Upload to YouTube ─────────────────────────────────────────────────────────
echo ""
echo "--- Uploading to YouTube ---"

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

# Load credentials
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
    print("Token refreshed")

yt = build("youtube", "v3", credentials=creds)

# Build title and description
try:
    dt = datetime.fromisoformat(CAPTURED_AT.replace("Z", "+00:00"))
    day_name = dt.strftime("%A")
    date_str = dt.strftime("%-dst %B %Y").replace("1st", "1st").replace("2nd", "2nd").replace("3rd", "3rd")
    # Fix ordinal
    day = dt.day
    suffix = "th" if 11 <= day <= 13 else {1:"st",2:"nd",3:"rd"}.get(day%10,"th")
    date_str = dt.strftime(f"%-d{suffix} %B %Y")
except Exception:
    day_name = "Session"
    date_str = CAPTURED_AT[:10]

title = f"{day_name} Session | {date_str} | FFA Leicester | 360°"
description = (
    f"FFA Leicester — {day_name} session footage captured {date_str}.\n"
    f"360° video — use a VR headset or drag to look around.\n\n"
    f"FFA_MEDIA_ID:{MEDIA_ID}\n"
    f"FFA_FILENAME:{FILENAME}\n"
    f"FFA_CAPTURED_AT:{CAPTURED_AT}\n"
    f"FFA_360:true"
)

print(f"Title: {title}")
print(f"File size: {VIDEO_PATH.stat().st_size / 1e9:.2f} GB")

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
        if pct % 10 == 0:
            print(f"Upload progress: {pct}%")
    if response:
        yt_id = response["id"]

print(f"Uploaded successfully: https://www.youtube.com/watch?v={yt_id}")
print(f"YT_ID={yt_id}")

# Save yt_id to file so bash can pick it up
Path("/tmp/yt_id.txt").write_text(yt_id)
PYEOF

YT_ID=$(cat /tmp/yt_id.txt 2>/dev/null || echo "")

if [ -z "${YT_ID}" ]; then
  echo "ERROR: YouTube upload failed — no video ID returned"
  exit 1
fi

echo ""
echo "=== SUCCESS ==="
echo "YouTube ID : ${YT_ID}"
echo "URL        : https://www.youtube.com/watch?v=${YT_ID}"

# ── Update uploaded.db via GitHub API ─────────────────────────────────────────
echo ""
echo "--- Updating uploaded.db ---"

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

# Fetch current uploaded.db
data = gh_get("contents/uploaded.db")
db_content = base64.b64decode(data["content"])
sha = data["sha"]

# Write to temp file, update, re-encode
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

echo ""
echo "--- Cleaning up ---"
rm -rf "${WORKDIR}"

echo "Done. Instance will now terminate."
