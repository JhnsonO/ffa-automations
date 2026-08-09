#!/usr/bin/env bash
# Runs ON the Vast.ai instance (uploaded + executed via SSH by
# oev-reco-stitch.yml). Builds reco-cli from source, then runs
# reco calibrate + reco stitch against a pre-downloaded clip pair.
#
# Every stage writes its own log file so a failure at any point leaves
# an inspectable artifact behind, rather than a single opaque pass/fail.
# All long-running commands are piped through `tee -a` (not `>>`) so
# output streams live to the SSH/Actions log AND is captured reliably —
# a plain `>>` redirect can be lost if the session ends before the
# buffer flushes, which is what happened on the first run of this script.
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
} 2>&1 | tee -a env.log

echo "=== Installing system deps ===" | tee -a env.log
stdbuf -oL -eL apt-get update 2>&1 | tee -a env.log
stdbuf -oL -eL apt-get install -y --no-install-recommends \
  git build-essential pkg-config libssl-dev cmake clang ca-certificates wget \
  mesa-vulkan-drivers vulkan-tools libvulkan1 ffmpeg \
  libavutil-dev libavcodec-dev libavformat-dev libswscale-dev \
  libavdevice-dev libavfilter-dev libswresample-dev 2>&1 | tee -a env.log

echo "=== Installing CUDA runtime (plain ubuntu image has no CUDA libs by default) ===" | tee -a env.log
{
  wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb -O /tmp/cuda-keyring.deb \
    && dpkg -i /tmp/cuda-keyring.deb \
    && apt-get update \
    && ( apt-get install -y --no-install-recommends cuda-runtime \
         || { echo "unversioned cuda-runtime not found, searching repo for a versioned package..."; \
              CUDA_PKG=$(apt-cache search '^cuda-runtime-[0-9]' | sort -V | tail -1 | awk '{print $1}'); \
              if [ -n "$CUDA_PKG" ]; then \
                echo "Found: $CUDA_PKG"; \
                apt-get install -y --no-install-recommends "$CUDA_PKG"; \
              else \
                echo "No cuda-runtime-* package found in repo either"; \
                exit 1; \
              fi; } ) \
    || echo "CUDA runtime install failed — see above for the actual error"
} 2>&1 | tee -a env.log

echo "=== GPU/Vulkan diagnostic dump ===" | tee -a env.log
{
  echo "--- NVIDIA env vars seen by container init (PID 1) ---"
  cat /proc/1/environ | tr '\0' '\n' | grep -i NVIDIA || echo "no NVIDIA env vars found in container init env"
  echo "--- /dev/nvidia* device nodes ---"
  ls -la /dev/nvidia* 2>&1 || echo "no /dev/nvidia* device nodes found"
  echo "--- Vulkan ICD manifest dirs ---"
  ls -la /usr/share/vulkan/icd.d/ /etc/vulkan/icd.d/ 2>&1 || echo "no vulkan icd.d dirs found"
  echo "--- libGLX_nvidia.so search ---"
  find / -iname "libGLX_nvidia.so*" 2>/dev/null || echo "libGLX_nvidia.so not found anywhere on filesystem"
} 2>&1 | tee -a env.log

echo "=== Refreshing dynamic linker cache (Vast bind-mounts NVIDIA libs post-boot; ldconfig may never have run) ===" | tee -a env.log
{
  ldconfig
  echo "--- vulkaninfo summary (post-ldconfig) ---"
  vulkaninfo --summary 2>&1 | head -30 || echo "vulkaninfo still failed after ldconfig"
} 2>&1 | tee -a env.log

echo "=== Diagnosing + fixing missing NVIDIA driver helper libraries ===" | tee -a env.log
{
  # Vast bind-mounts libGLX_nvidia.so directly rather than using the
  # nvidia-container-toolkit hook (confirmed: NVIDIA_VISIBLE_DEVICES reads
  # back as 'void' inside the container). The earlier apt-get-install
  # approach failed with dpkg "Invalid cross-device link" errors — dpkg's
  # atomic-replace-via-hardlink logic doesn't work across Vast's bind
  # mounts. apt-get download + dpkg-deb -x sidesteps dpkg's install
  # machinery entirely and just extracts the .deb's file contents.
  NVIDIA_LIB=$(find /usr/lib/x86_64-linux-gnu /usr/lib -iname "libGLX_nvidia.so.*.*.*" 2>/dev/null | head -1)
  echo "NVIDIA_LIB=${NVIDIA_LIB:-not found}"
  if [ -n "$NVIDIA_LIB" ]; then
    echo "--- ldd on $NVIDIA_LIB (before fix) ---"
    ldd "$NVIDIA_LIB" 2>&1 || echo "ldd failed to run"
    DRIVER_VER=$(basename "$NVIDIA_LIB" | grep -oE '[0-9]+' | head -1)
    echo "Detected driver series: ${DRIVER_VER:-unknown}"
    if [ -n "$DRIVER_VER" ]; then
      mkdir -p /tmp/nvidia-extract && cd /tmp/nvidia-extract
      for PKG in "libnvidia-gl-${DRIVER_VER}" "libnvidia-compute-${DRIVER_VER}"; do
        echo "Downloading $PKG..."
        apt-get download "$PKG" 2>&1 || echo "$PKG not available or download failed"
      done
      echo "Downloaded $(ls -1 *.deb 2>/dev/null | wc -l) .deb file(s)"
      for DEB in *.deb; do
        [ -f "$DEB" ] || continue
        echo "Extracting $DEB via dpkg-deb -x..."
        dpkg-deb -x "$DEB" /tmp/nvidia-extract/root 2>&1 || echo "extraction failed for $DEB"
      done
      if [ -d /tmp/nvidia-extract/root/usr/lib/x86_64-linux-gnu ]; then
        echo "Copying extracted .so files into /usr/lib/x86_64-linux-gnu/ ..."
        cp -av /tmp/nvidia-extract/root/usr/lib/x86_64-linux-gnu/*.so* /usr/lib/x86_64-linux-gnu/ 2>&1 || echo "copy step found nothing to copy"
      else
        echo "No extracted library directory found — nothing to copy"
      fi
      cd /tmp/oev_run
      ldconfig
      echo "--- ldd on $NVIDIA_LIB (after fix) ---"
      ldd "$NVIDIA_LIB" 2>&1 || echo "ldd failed to run"
      echo "--- vulkaninfo summary (after manual extract) ---"
      vulkaninfo --summary 2>&1 | head -30 || echo "vulkaninfo still failed after manual extract"
    fi
  else
    echo "No libGLX_nvidia.so.<version> file found — cannot diagnose further"
  fi
} 2>&1 | tee -a env.log

echo "=== Installing Rust toolchain ===" | tee -a env.log
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | stdbuf -oL -eL sh -s -- -y --default-toolchain stable 2>&1 | tee -a env.log
source "$HOME/.cargo/env"
{
  echo "--- rustc/cargo (post-install) ---"
  rustc --version
  cargo --version
  echo "--- vulkaninfo summary ---"
  vulkaninfo --summary 2>&1 | head -30 || echo "vulkaninfo failed"
} 2>&1 | tee -a env.log

echo "=== build.log: cloning + building reco-cli ===" | tee build.log
git clone --depth 1 https://github.com/reco-project/video-stitcher.git /tmp/reco-src 2>&1 | tee -a build.log
clone_rc=${PIPESTATUS[0]}
if [ "$clone_rc" -ne 0 ]; then
  echo "FATAL: git clone failed (exit $clone_rc), see build.log" | tee -a build.log
  exit 1
fi
cd /tmp/reco-src
stdbuf -oL -eL cargo build --release -p reco-cli -v 2>&1 | tee -a /tmp/oev_run/build.log
build_rc=${PIPESTATUS[0]}
if [ "$build_rc" -ne 0 ]; then
  echo "FATAL: cargo build failed (exit $build_rc), see build.log" | tee -a /tmp/oev_run/build.log
  exit 1
fi
RECO_BIN="/tmp/reco-src/target/release/reco"
if [ ! -x "$RECO_BIN" ]; then
  echo "FATAL: build reported success but binary not found at $RECO_BIN" | tee -a /tmp/oev_run/build.log
  exit 1
fi
echo "Build OK: $RECO_BIN" | tee -a /tmp/oev_run/build.log
echo "--- reco stitch --help (checking for a software/CPU fallback flag) ---" | tee -a /tmp/oev_run/build.log
"$RECO_BIN" stitch --help 2>&1 | tee -a /tmp/oev_run/build.log || echo "reco stitch --help failed" | tee -a /tmp/oev_run/build.log
cd /tmp/oev_run

echo "=== calibrate.log: reco calibrate ===" | tee calibrate.log
stdbuf -oL -eL "$RECO_BIN" calibrate left.mp4 right.mp4 -o match.json 2>&1 | tee -a calibrate.log
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

echo "=== stitch.log: reco stitch ===" | tee stitch.log
stdbuf -oL -eL "$RECO_BIN" stitch left.mp4 right.mp4 -c match.json -o panorama.mp4 2>&1 | tee -a stitch.log
stitch_rc=${PIPESTATUS[0]}
if [ "$stitch_rc" -ne 0 ]; then
  echo "FATAL: reco stitch failed (exit $stitch_rc), see stitch.log (match.json is still valid, calibration succeeded)" | tee -a stitch.log
  exit 3
fi
if [ ! -f panorama.mp4 ]; then
  echo "FATAL: stitch reported success but panorama.mp4 missing" | tee -a stitch.log
  exit 3
fi
echo "Stitch OK: panorama.mp4 written" | tee -a stitch.log

echo "=== All stages completed ==="
