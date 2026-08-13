#!/usr/bin/env bash
# OEV Test Runtime -- populate-volume build script.
#
# Runs INSIDE a throwaway RunPod pod that has the persistent network
# volume attached at $VOLUME_MOUNT (default /runpod-volume). Builds
# reco-cli at the pinned SHA (--features cuda) and pre-exports YOLO26
# s/m/l/x @1920 ONNX models, writing everything to
# $VOLUME_MOUNT/oev-runtime/{bin,models}, plus a manifest.json -- the
# exact same build this pipeline used to bake into the (now-retired,
# fully-baked) Docker image. WHAT gets built is unchanged; only WHERE
# it's written is different (volume instead of image layers).
#
# Idempotent: if manifest.json already exists on the volume and matches
# the requested RECO_SHA + ULTRALYTICS_VERSION, and FORCE_REBUILD is not
# "true", the build is skipped entirely.
#
# Exit codes: 1=env/arg sanity, 2=already-current (not an error, just an
#             early clean exit), 3=apt/rust setup failure, 4=reco-cli
#             build failure, 5=yolo export failure, 6=manifest write
#             failure.

set -uo pipefail

: "${RECO_SHA:?RECO_SHA must be set}"
: "${RECO_REPO:?RECO_REPO must be set}"
: "${ULTRALYTICS_VERSION:?ULTRALYTICS_VERSION must be set}"
: "${VOLUME_MOUNT:=/runpod-volume}"
: "${FORCE_REBUILD:=false}"

RUNTIME_DIR="${VOLUME_MOUNT}/oev-runtime"
BIN_DIR="${RUNTIME_DIR}/bin"
MODELS_DIR="${RUNTIME_DIR}/models"
MANIFEST="${RUNTIME_DIR}/manifest.json"

ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
mkdir -p "$BIN_DIR" "$MODELS_DIR"

echo "timing_populate_start=$(ts)" | tee -a timing.log

# --- Idempotency check ---
if [ -f "$MANIFEST" ] && [ "$FORCE_REBUILD" != "true" ]; then
  existing_sha=$(python3 -c "import json; print(json.load(open('$MANIFEST')).get('reco_sha',''))" 2>/dev/null || echo "")
  existing_ultra=$(python3 -c "import json; print(json.load(open('$MANIFEST')).get('ultralytics_version',''))" 2>/dev/null || echo "")
  if [ "$existing_sha" = "$RECO_SHA" ] && [ "$existing_ultra" = "$ULTRALYTICS_VERSION" ] \
     && [ -x "${BIN_DIR}/reco" ] && [ -f "${MODELS_DIR}/models.sha256" ]; then
    echo "Volume already current for reco_sha=$RECO_SHA ultralytics_version=$ULTRALYTICS_VERSION -- skipping build." | tee -a timing.log
    echo "timing_populate_end=$(ts)" | tee -a timing.log
    exit 2
  fi
  echo "Volume manifest present but stale (existing reco_sha=$existing_sha, existing_ultra=$existing_ultra) -- rebuilding." | tee -a timing.log
fi

echo "=== build.log: apt deps + Rust toolchain ===" | tee build.log
echo "timing_setup_start=$(ts)" | tee -a timing.log

apt-get update -qq && apt-get install -y -qq --no-install-recommends \
    git curl build-essential pkg-config cmake \
    clang libclang-dev \
    libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libavfilter-dev \
    ca-certificates 2>&1 | tee -a build.log
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  echo "FATAL: apt-get install failed" | tee -a build.log
  exit 3
fi

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y 2>&1 | tee -a build.log
export PATH="/root/.cargo/bin:${PATH}"
rustc --version 2>&1 | tee -a build.log
echo "timing_setup_end=$(ts)" | tee -a timing.log

echo "=== build.log: cargo build --release -p reco-cli --features cuda ===" | tee -a build.log
echo "timing_reco_build_start=$(ts)" | tee -a timing.log
rm -rf /tmp/video-stitcher
git clone "$RECO_REPO" /tmp/video-stitcher 2>&1 | tee -a build.log
cd /tmp/video-stitcher || exit 4
git checkout "$RECO_SHA" 2>&1 | tee -a build.log
echo "video_stitcher_sha=$(git rev-parse HEAD)" > /tmp/oev_build_manifest.env

cargo build --release -p reco-cli --features cuda 2>&1 | tee -a build.log
build_rc=${PIPESTATUS[0]}
echo "timing_reco_build_end=$(ts)" | tee -a timing.log
if [ "$build_rc" -ne 0 ]; then
  echo "FATAL: cargo build failed (exit $build_rc)" | tee -a build.log
  exit 4
fi

cp target/release/reco "$BIN_DIR/reco"
find target/release -maxdepth 1 -iname 'libonnxruntime*.so*' -exec cp {} "$BIN_DIR/" \;
"$BIN_DIR/reco" --version > /tmp/oev_reco_version.txt 2>&1
echo "reco build OK: $(cat /tmp/oev_reco_version.txt)" | tee -a build.log

echo "=== build.log: YOLO26 s/m/l/x @1920 ONNX export ===" | tee -a build.log
echo "timing_yolo_export_start=$(ts)" | tee -a timing.log
python3 -m venv /tmp/yolo-venv
/tmp/yolo-venv/bin/pip install -q --upgrade pip
/tmp/yolo-venv/bin/pip install -q "ultralytics==${ULTRALYTICS_VERSION}" onnxruntime-gpu 2>&1 | tee -a build.log

cd "$MODELS_DIR" || exit 5
export_fail=0
for size in s m l x; do
  /tmp/yolo-venv/bin/yolo export model="yolo26${size}.pt" format=onnx imgsz=1920 2>&1 | tee -a build.log
  if [ ! -f "${MODELS_DIR}/yolo26${size}.onnx" ]; then
    echo "FATAL: yolo26${size}.onnx not found after export" | tee -a build.log
    export_fail=1
    break
  fi
done
echo "timing_yolo_export_end=$(ts)" | tee -a timing.log
if [ "$export_fail" -ne 0 ]; then
  exit 5
fi

sha256sum "${MODELS_DIR}"/yolo26*.onnx > "${MODELS_DIR}/models.sha256"

echo "=== manifest.json ===" | tee -a build.log
onnxruntime_gpu_version=$(/tmp/yolo-venv/bin/python3 -c 'import onnxruntime; print(onnxruntime.__version__)')
{
  echo "{"
  echo "  \"reco_sha\": \"${RECO_SHA}\","
  echo "  \"ultralytics_version\": \"${ULTRALYTICS_VERSION}\","
  echo "  \"onnxruntime_gpu_version\": \"${onnxruntime_gpu_version}\","
  echo "  \"base_image\": \"runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404\","
  echo "  \"build_timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","
  echo "  \"reco_version_string\": \"$(cat /tmp/oev_reco_version.txt | tr -d '\n')\","
  echo "  \"models_sha256\": \"$(cat "${MODELS_DIR}/models.sha256" | tr '\n' ';')\""
  echo "}"
} > "$MANIFEST"

if [ ! -f "$MANIFEST" ]; then
  echo "FATAL: manifest.json write failed" | tee -a build.log
  exit 6
fi
cat "$MANIFEST" | tee -a build.log
echo "timing_populate_end=$(ts)" | tee -a timing.log
echo "Populate-volume OK: reco + 4 models + manifest written to $RUNTIME_DIR"
