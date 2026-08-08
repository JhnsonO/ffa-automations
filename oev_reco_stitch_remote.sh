#!/usr/bin/env bash
# Runs ON the Vast.ai instance (uploaded + executed via SSH by
# oev-reco-stitch.yml). Builds reco-cli from source, then runs
# reco calibrate + reco stitch against a pre-downloaded clip pair.
#
# Every stage writes its own log file so a failure at any point leaves
# an inspectable artifact behind, rather than a single opaque pass/fail.
#
# Expects, in /tmp/oev_run/:
#   left.mp4, right.mp4   (the clip pair, already downloaded)
#
# Produces, in /tmp/oev_run/:
#   env.log       - toolchain/driver versions, always written first
#   build.log     - cargo build output
#   calibrate.log - reco calibrate output
#   stitch.log     - reco stitch output
#   match.json     - calibration result (present even if stitch fails)
#   panorama.mp4    - stitched output (only if stitch succeeds)

set -uo pipefail
cd /tmp/oev_run

echo "=== env.log: toolchain + GPU info ===" | tee env.log
{
  echo "--- nvidia-smi ---"
  nvidia-smi || echo "nvidia-smi not available"
  echo "--- rustc/cargo (pre-install check) ---"
  rustc --version 2>&1 || echo "rustc not yet installed"
  cargo --version 2>&1 || echo "cargo not yet installed"
} >> env.log 2>&1

echo "=== Installing system deps ===" | tee -a env.log
stdbuf -oL -eL apt-get update >> env.log 2>&1
stdbuf -oL -eL apt-get install -y --no-install-recommends \
  git build-essential pkg-config libssl-dev cmake clang \
  mesa-vulkan-drivers vulkan-tools libvulkan1 ffmpeg >> env.log 2>&1

echo "=== Installing Rust toolchain ===" | tee -a env.log
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | stdbuf -oL -eL sh -s -- -y --default-toolchain stable >> env.log 2>&1
source "$HOME/.cargo/env"
{
  echo "--- rustc/cargo (post-install) ---"
  rustc --version
  cargo --version
  echo "--- vulkaninfo summary ---"
  vulkaninfo --summary 2>&1 | head -30 || echo "vulkaninfo failed"
} >> env.log 2>&1

echo "=== build.log: cloning + building reco-cli ===" | tee build.log
if ! git clone --depth 1 https://github.com/reco-project/video-stitcher.git /tmp/reco-src >> build.log 2>&1; then
  echo "FATAL: git clone failed, see build.log" | tee -a build.log
  exit 1
fi
cd /tmp/reco-src
if ! stdbuf -oL -eL cargo build --release -p reco-cli -v >> build.log 2>&1; then
  echo "FATAL: cargo build failed, see build.log" | tee -a build.log
  exit 1
fi
RECO_BIN="/tmp/reco-src/target/release/reco"
if [ ! -x "$RECO_BIN" ]; then
  echo "FATAL: build reported success but binary not found at $RECO_BIN" | tee -a build.log
  exit 1
fi
echo "Build OK: $RECO_BIN" | tee -a build.log
cd /tmp/oev_run

echo "=== calibrate.log: reco calibrate ===" | tee calibrate.log
if ! stdbuf -oL -eL "$RECO_BIN" calibrate left.mp4 right.mp4 -o match.json >> calibrate.log 2>&1; then
  echo "FATAL: reco calibrate failed, see calibrate.log" | tee -a calibrate.log
  exit 2
fi
if [ ! -f match.json ]; then
  echo "FATAL: calibrate reported success but match.json missing" | tee -a calibrate.log
  exit 2
fi
echo "Calibrate OK: match.json written" | tee -a calibrate.log

echo "=== stitch.log: reco stitch ===" | tee stitch.log
if ! stdbuf -oL -eL "$RECO_BIN" stitch left.mp4 right.mp4 -c match.json -o panorama.mp4 >> stitch.log 2>&1; then
  echo "FATAL: reco stitch failed, see stitch.log (match.json is still valid, calibration succeeded)" | tee -a stitch.log
  exit 3
fi
if [ ! -f panorama.mp4 ]; then
  echo "FATAL: stitch reported success but panorama.mp4 missing" | tee -a stitch.log
  exit 3
fi
echo "Stitch OK: panorama.mp4 written" | tee -a stitch.log

echo "=== All stages completed ==="
