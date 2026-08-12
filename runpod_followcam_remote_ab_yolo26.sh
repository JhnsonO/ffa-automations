#!/usr/bin/env bash
# Runs ON a RunPod pod (uploaded + executed via SSH by
# oev-runpod-followcam.yml), AFTER runpod_bootstrap.sh has already
# produced a working --features cuda reco-cli build at
# /tmp/video-stitcher/target/release/reco and runpod_gpu_preflight.sh has
# already confirmed PREFLIGHT_RESULT=PASS on this pod.
#
# This script does NOT install any CUDA runtime, cuDNN, Rust toolchain,
# or rebuild reco-cli -- all of that is runpod_bootstrap.sh's job and is
# already done by the time this script runs. Redoing it here would mean
# building against the Vast script's CUDA-13-tuned install logic on top
# of this environment's already-proven CUDA 12.8 contract -- exactly the
# "wrong CUDA version on top of bootstrap's output" mistake this ticket
# exists to avoid (docs/ai-project-state.md, Task 4 spec).
#
# Segment-selection, calibrate, field_roi injection, and the tracking-
# acceptance check below are reused verbatim from
# oev_followcam_test_remote.sh (the Vast.ai equivalent).
#
# --no-zero-copy IS PASSED HERE, matching Vast, as of 2026-08-12. This is
# an interim correction, not the original intent: run 31557269688 proved
# full zero-copy (--no-zero-copy omitted) executes cleanly per every log
# signal (tracking, CUDAExecutionProvider, zero-copy engaged) but
# actually produces a corrupted followcam.mp4 -- a solid green band,
# confirmed on visual review, consistent with an NV12->RGB chroma-plane
# bug in reco-cli's zero-copy encode path that had never been exercised
# end-to-end before that run. Run 31558373625, identical script except
# --no-zero-copy added back, produced a clean, correct followcam.mp4 --
# isolating the corruption to zero-copy specifically (not geometry,
# flags, or match.json). Do not remove --no-zero-copy again until the
# zero-copy NV12 bug is actually fixed and re-verified in the
# JhnsonO/video-stitcher fork. The RunPod-specific zero-copy acceptance
# check below is left in place for when that fix lands and this flag is
# removed again -- it is currently dead code on this path since
# --no-zero-copy means those log lines will never appear, but a
# following "no such text found" acceptance failure on THIS path with
# --no-zero-copy still present would itself indicate the flag silently
# stopped applying and is worth investigating if seen.
#
# Deliberately does NOT pass --allow-no-tracking: if Reco can't
# initialize tracking, the run must fail loudly, not silently produce a
# plain static stitch that looks like a follow-cam but isn't one.
#
# Expects, in /tmp/oev_run/:
#   left.mp4, right.mp4   (the FULL original left/right GoPro source clips,
#                          already downloaded by the workflow)
#
# Produces, in /tmp/oev_run/:
#   segment.log     - full-source filenames + chosen start/duration for the
#                      synchronised test segment cut from the full originals
#   calibrate.log   - reco calibrate output + field_roi injection
#   stitch.log       - reco stitch output
#   acceptance.log   - tracking + zero-copy acceptance check output
#   match.json        - calibration result (present even if stitch fails)
#   events.jsonl      - pipeline event trace (present if stitch ran)
#   followcam.mp4     - follow-cam output (only if stitch succeeds)
#
# Exit codes: 1=segment-selection failure, 2=calibrate/field_roi failure,
# 3=stitch command failure, 4=stitch reported success but output missing,
# 5=acceptance failure (tracking not confirmed active, OR -- RunPod-
# specific -- zero-copy/NVDEC/CUDAExecutionProvider evidence missing from
# stitch.log).

set -uo pipefail
cd /tmp/oev_run

RECO_BIN="/tmp/video-stitcher/target/release/reco"
if [ ! -x "$RECO_BIN" ]; then
  echo "FATAL: $RECO_BIN not found or not executable -- runpod_bootstrap.sh must run and succeed before this script" | tee -a segment.log
  exit 1
fi

# --- Synchronised 15-20s test segment from the FULL original source
# clips (verbatim logic from oev_followcam_test_remote.sh). left.mp4/
# right.mp4 as downloaded by the workflow ARE the full original left/
# right GoPro recordings (resolved from the OEV Drive root folder, not
# Trimmed/) -- one random start timestamp + one random 15-20s duration is
# applied identically to both, bounded by the shorter of the two full
# clips so the segment is guaranteed to exist in both. left.mp4/right.mp4
# are overwritten in place so the rest of the script is unaffected. ---
echo "=== AB TEST: PINNED segment (yolo26m vs yolov8n baseline run 31596442940), NOT randomly selected ===" | tee segment.log
LEFT_SOURCE_NAME="${LEFT_CLIP:-left.mp4}"
RIGHT_SOURCE_NAME="${RIGHT_CLIP:-right.mp4}"
LEFT_FULL_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 left.mp4)
RIGHT_FULL_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 right.mp4)
LEFT_FULL_DURATION_INT=$(printf '%.0f' "${LEFT_FULL_DURATION:-0}")
RIGHT_FULL_DURATION_INT=$(printf '%.0f' "${RIGHT_FULL_DURATION:-0}")
if [ "$LEFT_FULL_DURATION_INT" -le 0 ] || [ "$RIGHT_FULL_DURATION_INT" -le 0 ]; then
  echo "FATAL: could not determine duration of left.mp4/right.mp4 (left=${LEFT_FULL_DURATION_INT}s right=${RIGHT_FULL_DURATION_INT}s) -- are these the full source clips?" | tee -a segment.log
  exit 1
fi
OVERLAP_DURATION_INT=$LEFT_FULL_DURATION_INT
if [ "$RIGHT_FULL_DURATION_INT" -lt "$OVERLAP_DURATION_INT" ]; then
  OVERLAP_DURATION_INT=$RIGHT_FULL_DURATION_INT
fi
# Pinned to match baseline run 31596442940's segment.log exactly (start=2274s
# duration=19s on GX010197.MP4/GX010173.MP4) -- deliberately NOT $RANDOM, so
# model quality is the only variable under test. Bounds check kept as a
# defensive guard only (should always pass given these are the same source
# files the baseline run used).
SEG_DURATION=19
SEG_START=2274
if [ "$SEG_DURATION" -gt "$OVERLAP_DURATION_INT" ]; then
  echo "FATAL: overlapping duration (${OVERLAP_DURATION_INT}s) shorter than pinned segment length (${SEG_DURATION}s) -- left=${LEFT_FULL_DURATION_INT}s right=${RIGHT_FULL_DURATION_INT}s" | tee -a segment.log
  exit 1
fi
MAX_START=$(( OVERLAP_DURATION_INT - SEG_DURATION ))
if [ "$MAX_START" -lt 0 ]; then MAX_START=0; fi
if [ "$SEG_START" -gt "$MAX_START" ]; then
  echo "FATAL: pinned SEG_START=${SEG_START}s exceeds this run's max valid start (${MAX_START}s) -- source clip durations differ from baseline run 31596442940, cannot reproduce the exact same segment" | tee -a segment.log
  exit 1
fi
{
  echo "Left full source:  ${LEFT_SOURCE_NAME} (duration ${LEFT_FULL_DURATION_INT}s)"
  echo "Right full source: ${RIGHT_SOURCE_NAME} (duration ${RIGHT_FULL_DURATION_INT}s)"
  echo "Overlapping valid duration: ${OVERLAP_DURATION_INT}s"
  echo "PINNED segment (matches baseline run 31596442940): start=${SEG_START}s duration=${SEG_DURATION}s (same start+duration applied to both cameras)"
} | tee -a segment.log
mv left.mp4 left_full.mp4
mv right.mp4 right_full.mp4
ffmpeg -y -ss "$SEG_START" -i left_full.mp4 -t "$SEG_DURATION" -c copy left.mp4 2>&1 | tee -a segment.log
ffmpeg -y -ss "$SEG_START" -i right_full.mp4 -t "$SEG_DURATION" -c copy right.mp4 2>&1 | tee -a segment.log
if [ ! -s left.mp4 ] || [ ! -s right.mp4 ]; then
  echo "FATAL: synchronised segment trim failed, left.mp4/right.mp4 missing/empty" | tee -a segment.log
  exit 1
fi
echo "Segment trim OK: left.mp4/right.mp4 now ~${SEG_DURATION}s starting at ${SEG_START}s (from full originals)" | tee -a segment.log

# --- AB TEST: YOLO model is yolo26m (candidate), NOT yolov8n (baseline).
# Only this model name/export line differs from the production script --
# tracking flags, resolution, ROI, panner, lookahead, detection-interval
# all remain byte-identical below. Verified via Ultralytics docs before
# this test: YOLO26's default export head (one-to-one, NMS-free) produces
# the same (1, 300, 6) xyxy+conf+cls ONNX output shape that the baseline's
# `nms=True` YOLOv8 export produces (confirmed from baseline run
# 31596442940's segment.log: "output shape(s) (1, 300, 6)") -- so no
# end2end=False/decode change is needed on the Reco side; `nms=True` is
# NOT passed here since it is not the equivalent flag for YOLO26 (that
# flag pairs with end2end=False on YOLO26, a different, non-default path).
YOLO_MODEL=""
for candidate in /tmp/oev_run/yolo26m.onnx /tmp/video-stitcher/yolo26m.onnx /tmp/yolo26m.onnx; do
  if [ -s "$candidate" ]; then
    YOLO_MODEL="$candidate"
    break
  fi
done
if [ -z "$YOLO_MODEL" ]; then
  echo "No existing yolo26m.onnx found on this pod -- exporting a fresh one (1920px, default one-to-one NMS-free head, matching the proven follow-cam resolution)" | tee -a segment.log
  python3 -m venv /tmp/yolo-venv 2>&1 | tee -a segment.log
  source /tmp/yolo-venv/bin/activate
  pip install -q -U ultralytics 2>&1 | tee -a segment.log
  ( cd /tmp/oev_run && yolo export model=yolo26m.pt format=onnx imgsz=1920 ) 2>&1 | tee -a segment.log
  deactivate
  if [ ! -s /tmp/oev_run/yolo26m.onnx ]; then
    echo "FATAL: yolo26m.onnx export failed and no existing model found" | tee -a segment.log
    exit 1
  fi
  YOLO_MODEL="/tmp/oev_run/yolo26m.onnx"
fi
echo "Using YOLO model: $YOLO_MODEL" | tee -a segment.log
if [ "$YOLO_MODEL" != "/tmp/oev_run/yolo26m.onnx" ]; then
  cp "$YOLO_MODEL" /tmp/oev_run/yolo26m.onnx
fi

echo "=== calibrate.log: reco calibrate ===" | tee calibrate.log
# Same pinned Hero10 Wide profile as the Vast follow-cam script (both
# cameras are GoPro Hero 10, Wide mode; auto-detect is known to fail on
# this footage's telemetry -- see docs/ai-project-state.md).
LENS_PROFILE_URL="https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json"
echo "Downloading lens profile: $LENS_PROFILE_URL" | tee -a calibrate.log
curl -fsSL "$LENS_PROFILE_URL" -o hero10_wide_16by9.json
if [ ! -s hero10_wide_16by9.json ]; then
  echo "FATAL: failed to download lens profile from $LENS_PROFILE_URL" | tee -a calibrate.log
  exit 2
fi
stdbuf -oL -eL "$RECO_BIN" calibrate left.mp4 right.mp4 \
  --left-profile hero10_wide_16by9.json \
  --right-profile hero10_wide_16by9.json \
  -o match.json 2>&1 | tee -a calibrate.log
calibrate_rc=${PIPESTATUS[0]}
if [ "$calibrate_rc" -ne 0 ]; then
  echo "FATAL: reco calibrate failed (exit $calibrate_rc), see calibrate.log" | tee -a calibrate.log
  exit 2
fi
if [ ! -f match.json ]; then
  echo "FATAL: calibrate reported success but match.json missing" | tee -a calibrate.log
  exit 2
fi
echo "Calibrate OK: match.json written" | tee -a calibrate.log

# Fixed St Margaret's field ROI (verbatim from oev_followcam_test_remote.sh
# -- same prototype polygon Johnson marked on the calibrate-stills
# screenshots for this exact clip pair/camera setup). Injected into
# match.json after calibrate, before stitch, so reco stitch's already-
# existing field_roi auto-load filters out detections from the
# neighbouring pitch.
echo "Injecting St Margaret's field_roi into match.json" | tee -a calibrate.log
python3 - <<'PYROI'
import json

with open("match.json") as f:
    match = json.load(f)

match["field_roi"] = {
    "left": [
        [0.1227, 0.9611],
        [0.0573, 0.6846],
        [0.1802, 0.6285],
        [0.2645, 0.5769],
        [0.4382, 0.4864],
        [0.4988, 0.4658],
        [0.5942, 0.4474],
        [0.7835, 0.4175],
        [0.9285, 0.3785],
        [1.0000, 1.0000],
        [0.1227, 1.0000],
    ],
    "right": [
        [0.0391, 0.4206],
        [0.0818, 0.4101],
        [0.1839, 0.4070],
        [0.2783, 0.4070],
        [0.3448, 0.4083],
        [0.4100, 0.4161],
        [0.4684, 0.4319],
        [0.6239, 0.4801],
        [0.7368, 0.5200],
        [0.7980, 0.5465],
        [0.7454, 0.9011],
        [0.7454, 1.0000],
        [0.0000, 1.0000],
    ],
}

with open("match.json", "w") as f:
    json.dump(match, f, indent=2)

assert len(match["field_roi"]["left"]) == 11
assert len(match["field_roi"]["right"]) == 13
print("field_roi injected: left=%d pts, right=%d pts" % (
    len(match["field_roi"]["left"]), len(match["field_roi"]["right"])))
PYROI
if [ $? -ne 0 ]; then
  echo "FATAL: field_roi injection into match.json failed" | tee -a calibrate.log
  exit 2
fi
if ! python3 -c "
import json, sys
m = json.load(open('match.json'))
roi = m.get('field_roi')
assert roi and isinstance(roi.get('left'), list) and len(roi['left']) > 0, 'field_roi.left missing/empty'
assert isinstance(roi.get('right'), list) and len(roi['right']) > 0, 'field_roi.right missing/empty'
" 2>>calibrate.log; then
  echo "FATAL: match.json field_roi validation failed after injection" | tee -a calibrate.log
  exit 2
fi
echo "field_roi validated in match.json (left/right polygons present)" | tee -a calibrate.log

echo "=== stitch.log: reco stitch (field follow-cam, l-shape, --no-zero-copy interim) ===" | tee stitch.log
# Same flag set agreed with Johnson as the Vast script: normal
# perspective (l-shape, default) projection, NOT cylindrical.
# --detection-interval 1 (no frame-skipping, out of scope for this
# ticket). Deliberately NO --allow-no-tracking: a tracking-init failure
# must fail this run loudly, not silently degrade to a static stitch.
# --no-zero-copy: see the file-header note (2026-08-12) -- zero-copy
# proved to produce a corrupted green-band output on this pod
# (run 31557269688), isolated to the zero-copy path specifically via A/B
# against run 31558373625. This is now the production setting, matching
# Vast, until the underlying reco-cli bug is fixed and re-verified.
STITCH_ARGS=(stitch left.mp4 right.mp4 -c match.json -o followcam.mp4
  --model yolo26m.onnx
  --tracking field
  --panner-preset broadcast
  --lookahead 1.5
  --detection-interval 1
  --events events.jsonl
  --no-zero-copy
  --width 1920 --height 1080)
echo "reco stitch args: ${STITCH_ARGS[*]}" | tee -a stitch.log
stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log
stitch_rc=${PIPESTATUS[0]}
if [ "$stitch_rc" -ne 0 ]; then
  echo "FATAL: reco stitch failed (exit $stitch_rc), see stitch.log (match.json/yolo26m.onnx are still valid)" | tee -a stitch.log
  exit 3
fi
if [ ! -f followcam.mp4 ]; then
  echo "FATAL: stitch reported success but followcam.mp4 missing" | tee -a stitch.log
  exit 4
fi
echo "Stitch OK: followcam.mp4 written" | tee -a stitch.log

echo "=== acceptance.log: verifying AI-driven follow-cam (not a static stitch) ===" | tee acceptance.log
python3 - <<'PY' 2>&1 | tee -a acceptance.log
import json, sys

accept_fail = False

try:
    stitch_log = open('stitch.log').read()
except FileNotFoundError:
    print("FAIL: stitch.log missing")
    sys.exit(1)

if "Autocam: tracking enabled" not in stitch_log:
    print("FAIL: 'Autocam: tracking enabled' not found in stitch.log -- tracking did not initialize")
    accept_fail = True
else:
    print("OK: 'Autocam: tracking enabled' found in stitch.log")

try:
    lines = open('events.jsonl').read().splitlines()
except FileNotFoundError:
    print("FAIL: events.jsonl missing")
    accept_fail = True
    lines = []

detections_with_hits = 0
pan_yaws = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    if ev.get('kind') == 'detections_raw' and ev.get('detections'):
        detections_with_hits += 1
    if ev.get('kind') == 'pan_decision':
        pose = ev.get('pose') or {}
        yaw = pose.get('yaw')
        if yaw is not None:
            pan_yaws.append(yaw)

print(f"Total event lines: {len(lines)}")
print(f"detections_raw events with >=1 detection: {detections_with_hits}")
print(f"pan_decision events with a yaw value: {len(pan_yaws)}")

if detections_with_hits == 0:
    print("FAIL: no detections_raw event contained any detection -- detector produced nothing")
    accept_fail = True

if len(pan_yaws) < 2:
    print("FAIL: fewer than 2 pan_decision events with a pose -- can't judge camera movement")
    accept_fail = True
else:
    yaw_spread = max(pan_yaws) - min(pan_yaws)
    print(f"pan_decision yaw spread (radians): {yaw_spread}")
    if yaw_spread < 1e-4:
        print("FAIL: pan_decision yaw never changes -- camera is static, not AI-driven")
        accept_fail = True
    else:
        print("OK: pan_decision yaw shows real movement")

if accept_fail:
    sys.exit(1)
print("ACCEPTANCE (tracking): PASS")
PY
tracking_accept_rc=${PIPESTATUS[0]}
if [ "$tracking_accept_rc" -ne 0 ]; then
  echo "FATAL: follow-cam tracking acceptance check FAILED -- see acceptance.log. followcam.mp4 exists but is NOT confirmed AI-driven." | tee -a acceptance.log
  exit 5
fi

# --- RunPod-specific: zero-copy evidence check. Only runs when
# --no-zero-copy is NOT in STITCH_ARGS -- i.e. only when zero-copy is
# actually expected to be active. As of 2026-08-12, --no-zero-copy IS in
# STITCH_ARGS (interim production setting, see file header), so this
# whole block is skipped on this path; it's left in place, unmodified,
# for whenever the reco-cli zero-copy bug is fixed and --no-zero-copy is
# removed again -- do not delete this block to "clean up" while
# --no-zero-copy is the active setting. ---
if printf '%s\n' "${STITCH_ARGS[@]}" | grep -qx -- '--no-zero-copy'; then
  echo "=== --no-zero-copy is active this run -- skipping zero-copy evidence check (expected, not applicable) ===" | tee -a acceptance.log
else
  echo "=== Verifying full zero-copy path actually engaged (RunPod-specific, no Vast equivalent) ===" | tee -a acceptance.log
  zero_copy_fail=0
  if grep -qiE 'zero-copy|zero copy' stitch.log; then
    echo "OK: zero-copy log line found" | tee -a acceptance.log
  else
    echo "FAIL: no zero-copy log line found in stitch.log" | tee -a acceptance.log
    zero_copy_fail=1
  fi
  if grep -qiE 'NVDEC.*CUDA|NVDEC \(CUDA\)|cuvid' stitch.log; then
    echo "OK: GPU decode (NVDEC/CUDA) log line found" | tee -a acceptance.log
  else
    echo "FAIL: no NVDEC/CUDA decode log line found in stitch.log" | tee -a acceptance.log
    zero_copy_fail=1
  fi
  if grep -qE "No execution providers from session options registered successfully" stitch.log; then
    echo "FAIL: CUDA EP fallback warning present in stitch.log -- detection likely ran on CPU" | tee -a acceptance.log
    zero_copy_fail=1
  elif grep -qiE 'CUDAExecutionProvider' stitch.log; then
    echo "OK: CUDAExecutionProvider log line found, no fallback warning" | tee -a acceptance.log
  else
    echo "FAIL: no CUDAExecutionProvider log line found in stitch.log" | tee -a acceptance.log
    zero_copy_fail=1
  fi
  if [ "$zero_copy_fail" -ne 0 ]; then
    echo "FATAL: zero-copy acceptance check FAILED -- see acceptance.log. Tracking passed but full zero-copy is NOT confirmed active on this run." | tee -a acceptance.log
    exit 5
  fi
  echo "Acceptance OK: zero-copy (GPU decode + CUDA inference) confirmed engaged" | tee -a acceptance.log
fi
echo "Acceptance OK: AI tracking confirmed active with real detections + camera movement" | tee -a acceptance.log

echo "=== All stages completed ==="
