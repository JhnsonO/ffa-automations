#!/usr/bin/env bash
# OEV — populate-volume-samples build script.
#
# Runs INSIDE a throwaway RunPod pod that has a persistent network volume
# attached at $VOLUME_MOUNT (default /runpod-volume). Downloads some or
# all of a sample dataset (produced by oev-sample-dataset-prep.yml) from
# the OEV Drive Samples/<sample-set-id>/ folder and writes it to
# $VOLUME_MOUNT/oev-samples/<sample-set-id>/, so benchmark/test runs on
# that datacenter's volume can read the small sample clips directly
# instead of downloading the full source per run.
#
# SAMPLES_FILTER (optional, comma-separated e.g. "sample_01,sample_02"):
# only the listed sample_NN subfolders are downloaded. Leave unset/empty
# to stage the whole set. manifest.json (small, describes all 5 samples
# regardless of filter) is always downloaded/refreshed.
#
# Idempotent per sample: each requested sample_NN folder is skipped if it
# already exists locally with at least one file, unless FORCE_REPOPULATE
# is "true". Exits 2 (clean, not an error) only if every requested sample
# was already present and nothing needed downloading.
#
# Does NOT touch reco-cli/models population (oev_populate_volume_remote.sh)
# or the round-robin datacenter-selection logic used elsewhere -- this
# script only stages video files onto whichever volume it's already
# attached to.
#
# Exit codes: 1=env/arg sanity, 2=already-current (not an error, just an
#             early clean exit, nothing downloaded), 3=Drive auth/list
#             failure, 4=download failure, 5=manifest mismatch/missing
#             after download.

set -uo pipefail

: "${SAMPLE_SET_ID:?SAMPLE_SET_ID must be set}"
: "${YOUTUBE_TOKEN:?YOUTUBE_TOKEN must be set}"
: "${YOUTUBE_CREDENTIALS:?YOUTUBE_CREDENTIALS must be set}"
: "${VOLUME_MOUNT:=/runpod-volume}"
: "${FORCE_REPOPULATE:=false}"
: "${SAMPLES_FILTER:=}"

OEV_DRIVE_FOLDER_ID="18Y8hI_S29BMeg5FEoxlaqoGy1DsQ7GKJ"
SAMPLES_ROOT="${VOLUME_MOUNT}/oev-samples/${SAMPLE_SET_ID}"
MANIFEST="${SAMPLES_ROOT}/manifest.json"

ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
mkdir -p "$SAMPLES_ROOT"

echo "timing_populate_samples_start=$(ts)" | tee -a timing.log
echo "samples_filter='${SAMPLES_FILTER}' (empty means: all samples)" | tee -a timing.log

echo "$YOUTUBE_TOKEN" > /tmp/youtube_token.json
echo "$YOUTUBE_CREDENTIALS" > /tmp/youtube_credentials.json

echo "=== download.log: listing + downloading Samples/${SAMPLE_SET_ID}/ ===" | tee download.log
SAMPLES_ROOT="$SAMPLES_ROOT" SAMPLE_SET_ID="$SAMPLE_SET_ID" OEV_DRIVE_FOLDER_ID="$OEV_DRIVE_FOLDER_ID" \
  SAMPLES_FILTER="$SAMPLES_FILTER" FORCE_REPOPULATE="$FORCE_REPOPULATE" \
  python3 -u - << 'PY' 2>&1 | tee -a download.log
import json
import os
import sys
import urllib.parse
import urllib.request as req
from pathlib import Path

OEV_DRIVE_FOLDER_ID = os.environ['OEV_DRIVE_FOLDER_ID']
SAMPLE_SET_ID = os.environ['SAMPLE_SET_ID']
SAMPLES_ROOT = Path(os.environ['SAMPLES_ROOT'])
FORCE_REPOPULATE = os.environ.get('FORCE_REPOPULATE', 'false') == 'true'
SAMPLES_FILTER = [s.strip() for s in os.environ.get('SAMPLES_FILTER', '').split(',') if s.strip()]

token_data = json.loads(open('/tmp/youtube_token.json').read())
creds_data = json.loads(open('/tmp/youtube_credentials.json').read())
payload = json.dumps({
    'client_id': token_data.get('client_id') or creds_data['installed']['client_id'],
    'client_secret': token_data.get('client_secret') or creds_data['installed']['client_secret'],
    'refresh_token': token_data['refresh_token'],
    'grant_type': 'refresh_token',
}).encode()
resp = req.urlopen(req.Request(
    'https://oauth2.googleapis.com/token', data=payload,
    headers={'Content-Type': 'application/json'}, method='POST',
))
token = json.loads(resp.read())
if 'access_token' not in token:
    print(f'FATAL: token refresh failed: {token}')
    sys.exit(1)
access_token = token['access_token']
auth_headers = {'Authorization': f'Bearer {access_token}'}


def list_children(parent_id):
    url = 'https://www.googleapis.com/drive/v3/files?' + urllib.parse.urlencode({
        'q': f"'{parent_id}' in parents and trashed=false",
        'fields': 'files(id,name,mimeType)',
        'supportsAllDrives': 'true',
        'includeItemsFromAllDrives': 'true',
        'pageSize': '1000',
    })
    resp = req.urlopen(req.Request(url, headers=auth_headers))
    return json.loads(resp.read()).get('files', [])


def find_child_folder(parent_id, name):
    for f in list_children(parent_id):
        if f['mimeType'] == 'application/vnd.google-apps.folder' and f['name'] == name:
            return f['id']
    return None


def download_file(file_id, dest: Path):
    url = f'https://www.googleapis.com/drive/v3/files/{file_id}?alt=media'
    resp = req.urlopen(req.Request(url, headers=auth_headers))
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, 'wb') as out:
        while True:
            chunk = resp.read(4 * 1024 * 1024)
            if not chunk:
                break
            out.write(chunk)


samples_folder_id = find_child_folder(OEV_DRIVE_FOLDER_ID, 'Samples')
if not samples_folder_id:
    print("FATAL: 'Samples' folder not found under OEV Drive root")
    sys.exit(1)
set_folder_id = find_child_folder(samples_folder_id, SAMPLE_SET_ID)
if not set_folder_id:
    print(f"FATAL: sample set '{SAMPLE_SET_ID}' not found under Drive Samples/")
    sys.exit(1)

entries = list_children(set_folder_id)

# manifest.json (and any other top-level file) always downloaded fresh --
# small, describes the whole set regardless of filter.
top_level_files = [e for e in entries if e['mimeType'] != 'application/vnd.google-apps.folder']
sample_folders = [e for e in entries if e['mimeType'] == 'application/vnd.google-apps.folder']

if SAMPLES_FILTER:
    wanted_names = set(SAMPLES_FILTER)
    missing = wanted_names - {f['name'] for f in sample_folders}
    if missing:
        print(f"FATAL: requested sample(s) not found in Drive set: {sorted(missing)}")
        sys.exit(1)
    sample_folders = [f for f in sample_folders if f['name'] in wanted_names]

print(f"Target samples this run: {[f['name'] for f in sample_folders] or '(none -- filter matched nothing)'}")

count = 0
for entry in top_level_files:
    dest = SAMPLES_ROOT / entry['name']
    print(f'Downloading {dest} (manifest/top-level file, always refreshed)')
    download_file(entry['id'], dest)
    count += 1

skipped = []
for folder in sample_folders:
    local_dir = SAMPLES_ROOT / folder['name']
    already_present = local_dir.is_dir() and any(local_dir.iterdir())
    if already_present and not FORCE_REPOPULATE:
        print(f"SKIP {folder['name']}: already present at {local_dir}")
        skipped.append(folder['name'])
        continue
    for entry in list_children(folder['id']):
        dest = local_dir / entry['name']
        print(f'Downloading {dest}')
        download_file(entry['id'], dest)
        count += 1

print(f'Downloaded {count} file(s) to {SAMPLES_ROOT}; skipped (already present) {skipped}')
PY
DOWNLOAD_RC=${PIPESTATUS[0]}
if [ "$DOWNLOAD_RC" -ne 0 ]; then
  echo "FATAL: sample dataset download failed (exit $DOWNLOAD_RC)" | tee -a download.log
  exit 4
fi

if [ ! -f "$MANIFEST" ]; then
  echo "FATAL: manifest.json not present at $MANIFEST after download" | tee -a download.log
  exit 5
fi
downloaded_set_id=$(python3 -c "import json; print(json.load(open('$MANIFEST')).get('sample_set_id',''))" 2>/dev/null || echo "")
if [ "$downloaded_set_id" != "$SAMPLE_SET_ID" ]; then
  echo "FATAL: downloaded manifest sample_set_id='$downloaded_set_id' does not match requested '$SAMPLE_SET_ID'" | tee -a download.log
  exit 5
fi

echo "timing_populate_samples_end=$(ts)" | tee -a timing.log
if grep -q '^Downloaded 0 file' download.log 2>/dev/null; then
  echo "Populate-volume-samples: nothing new to download (all requested samples already present)."
  exit 2
fi
echo "Populate-volume-samples OK: sample set $SAMPLE_SET_ID (filter='${SAMPLES_FILTER}') written to $SAMPLES_ROOT"
