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

# --- GitHub Release blob cache (no-op throughout if GH_TOKEN is unset) ---
# Used to cache two things across runs, keyed so a stale cache can never be
# served silently:
#   1. The generic CUDA runtime .debs (~1.1GB) - these are pinned to
#      whatever version our script resolves (cuda-runtime, or the latest
#      cuda-runtime-N), NOT to the host's GPU driver, so they're identical
#      run to run until NVIDIA ships a new CUDA release.
#   2. The compiled reco-cli binary, keyed by the exact git SHA of
#      reco-project/video-stitcher - a source change naturally produces a
#      new cache key rather than serving a stale binary.
# The host-specific libnvidia-gl/-compute driver extraction below is
# deliberately NOT cached - see docs/ai-project-state.md for why baking in
# a fixed driver version previously caused the exact class of bug that took
# 8+ debug cycles to fix.
GH_TOKEN="${GH_TOKEN:-}"
GH_CACHE_REPO="JhnsonO/ffa-automations"
GH_CACHE_TAG="oev-build-cache"

gh_cache_release_id() {
  [ -z "$GH_TOKEN" ] && return 1
  curl -s -H "Authorization: token $GH_TOKEN" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases/tags/$GH_CACHE_TAG" \
    | jq -r 'if .id then .id else empty end'
}

gh_cache_release_id_or_create() {
  local id
  id=$(gh_cache_release_id)
  if [ -n "$id" ]; then echo "$id"; return 0; fi
  curl -s -X POST -H "Authorization: token $GH_TOKEN" -H "Content-Type: application/json" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases" \
    -d "{\"tag_name\":\"$GH_CACHE_TAG\",\"name\":\"OEV build cache (auto-managed)\",\"body\":\"Binary/package cache for oev-reco-stitch.yml. Safe to delete - will be recreated on the next run.\",\"prerelease\":true}" \
    | jq -r '.id // empty'
}

gh_cache_download() {
  local release_id="$1" name="$2" outpath="$3" asset_id
  asset_id=$(curl -s -H "Authorization: token $GH_TOKEN" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases/$release_id/assets" \
    | jq -r --arg n "$name" '.[] | select(.name==$n) | .id')
  [ -z "$asset_id" ] && return 1
  curl -sL -H "Authorization: token $GH_TOKEN" -H "Accept: application/octet-stream" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases/assets/$asset_id" -o "$outpath"
  [ -s "$outpath" ]
}

gh_cache_upload() {
  local release_id="$1" filepath="$2" name="$3" existing_id
  [ -z "$release_id" ] && { echo "no release id, skipping cache upload of $name"; return 1; }
  existing_id=$(curl -s -H "Authorization: token $GH_TOKEN" \
    "https://api.github.com/repos/$GH_CACHE_REPO/releases/$release_id/assets" \
    | jq -r --arg n "$name" '.[] | select(.name==$n) | .id')
  if [ -n "$existing_id" ]; then
    curl -s -X DELETE -H "Authorization: token $GH_TOKEN" \
      "https://api.github.com/repos/$GH_CACHE_REPO/releases/assets/$existing_id" > /dev/null
  fi
  curl -s -X POST -H "Authorization: token $GH_TOKEN" -H "Content-Type: application/gzip" \
    --data-binary @"$filepath" \
    "https://uploads.github.com/repos/$GH_CACHE_REPO/releases/$release_id/assets?name=$name" > /dev/null
}

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
  git build-essential pkg-config libssl-dev cmake clang ca-certificates wget jq \
  mesa-vulkan-drivers vulkan-tools libvulkan1 ffmpeg \
  libavutil-dev libavcodec-dev libavformat-dev libswscale-dev \
  libavdevice-dev libavfilter-dev libswresample-dev 2>&1 | tee -a env.log

echo "=== Installing CUDA runtime (plain ubuntu image has no CUDA libs by default) ===" | tee -a env.log
{
  wget -q https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb -O /tmp/cuda-keyring.deb \
    && dpkg -i /tmp/cuda-keyring.deb \
    && apt-get update
} 2>&1 | tee -a env.log

# Host driver's nvidia-smi "CUDA Version" is the *maximum* CUDA toolkit the
# installed driver supports, not a version to match exactly. Installing a
# newer toolkit than the driver supports fails at runtime (cuInit ->
# CUDA_ERROR_COMPAT_NOT_SUPPORTED_ON_DEVICE) even though the install itself
# succeeds. Pick the highest available cuda-runtime-X-Y package that does not
# exceed this max, instead of always taking the newest package in the repo.
HOST_CUDA_MAX=$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9]+\.[0-9]+' | grep -oE '[0-9]+\.[0-9]+' | head -1)
echo "Host driver max-supported CUDA version: ${HOST_CUDA_MAX:-unknown (nvidia-smi parse failed)}" | tee -a env.log

CUDA_CACHE_ASSET="cuda-runtime-debs.tar.gz"
cuda_cache_hit=0
if [ -n "$GH_TOKEN" ]; then
  CUDA_RELEASE_ID=$(gh_cache_release_id)
  if [ -n "$CUDA_RELEASE_ID" ] && gh_cache_download "$CUDA_RELEASE_ID" "$CUDA_CACHE_ASSET" /tmp/cuda-runtime-debs.tar.gz; then
    echo "CUDA runtime cache HIT ($CUDA_CACHE_ASSET) — installing from cached .debs, skipping ~1.1GB download" | tee -a env.log
    mkdir -p /tmp/cuda-cache && tar -xzf /tmp/cuda-runtime-debs.tar.gz -C /tmp/cuda-cache
    { dpkg -i /tmp/cuda-cache/*.deb || apt-get install -f -y; } 2>&1 | tee -a env.log
    cuda_cache_hit=1
  fi
fi
if [ "$cuda_cache_hit" -eq 0 ]; then
  echo "CUDA runtime cache MISS (or no GH_TOKEN) — installing via apt" | tee -a env.log
  mkdir -p /tmp/cuda-cache
  {
    ( apt-get install -y --no-install-recommends --download-only -o "Dir::Cache::Archives=/tmp/cuda-cache" cuda-runtime \
        || { echo "unversioned cuda-runtime not found, searching repo for a compatible versioned package..."; \
             if [ -n "$HOST_CUDA_MAX" ]; then \
               CUDA_PKG=$(apt-cache search '^cuda-runtime-[0-9]' | awk '{print $1}' \
                 | sed -E 's/cuda-runtime-([0-9]+)-([0-9]+)/\1.\2 &/' \
                 | sort -V \
                 | awk -v max="$HOST_CUDA_MAX" '{split(max,m,"."); split($1,v,"."); if ((v[1]<m[1]) || (v[1]==m[1] && v[2]<=m[2])) last=$2} END{print last}'); \
               echo "Highest cuda-runtime-X-Y <= host max ($HOST_CUDA_MAX): ${CUDA_PKG:-none found}"; \
             else \
               echo "No host CUDA max known — falling back to newest available (pre-fix behavior)"; \
               CUDA_PKG=$(apt-cache search '^cuda-runtime-[0-9]' | sort -V | tail -1 | awk '{print $1}'); \
             fi; \
             if [ -n "$CUDA_PKG" ]; then \
               echo "Installing: $CUDA_PKG"; \
               apt-get install -y --no-install-recommends --download-only -o "Dir::Cache::Archives=/tmp/cuda-cache" "$CUDA_PKG"; \
             else \
               echo "No compatible cuda-runtime-* package found in repo"; \
               exit 1; \
             fi; } ) \
      && { dpkg -i /tmp/cuda-cache/*.deb || apt-get install -f -y; } \
      || echo "CUDA runtime install failed — see above for the actual error"
  } 2>&1 | tee -a env.log
  if [ -n "$GH_TOKEN" ] && ls /tmp/cuda-cache/*.deb >/dev/null 2>&1; then
    echo "Caching downloaded CUDA runtime .debs for next run..." | tee -a env.log
    tar -czf /tmp/cuda-runtime-debs.tar.gz -C /tmp/cuda-cache .
    CUDA_RELEASE_ID=$(gh_cache_release_id_or_create)
    gh_cache_upload "$CUDA_RELEASE_ID" /tmp/cuda-runtime-debs.tar.gz "$CUDA_CACHE_ASSET" 2>&1 | tee -a env.log \
      || echo "CUDA runtime cache upload failed (non-fatal)" | tee -a env.log
  fi
fi

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
  echo "--- vulkaninfo summary (post-ldconfig, pristine stack) ---"
  vulkaninfo --summary 2>&1 | head -30 || echo "vulkaninfo still failed after ldconfig"
  echo "--- vulkaninfo with VK_LOADER_DEBUG=all (isolating the real vkCreateInstance failure) ---"
  VK_LOADER_DEBUG=all vulkaninfo --summary 2>&1 | tail -60 || echo "vulkaninfo (loader debug) failed"
  echo "--- Vulkan ICD manifest contents ---"
  for f in /usr/share/vulkan/icd.d/*.json /etc/vulkan/icd.d/*.json; do
    [ -f "$f" ] && { echo "-- $f --"; cat "$f"; }
  done 2>&1
} 2>&1 | tee -a env.log

echo "=== Diagnosing NVIDIA driver helper libraries (extract only what's actually missing) ===" | tee -a env.log
{
  # Vast bind-mounts libGLX_nvidia.so directly rather than using the
  # nvidia-container-toolkit hook (confirmed: NVIDIA_VISIBLE_DEVICES reads
  # back as 'void' inside the container). Prior version of this script
  # unconditionally downloaded the latest driver-series package and
  # overwrote the entire NVIDIA lib set -- this DESTROYS an already-correct,
  # exact-version-matched bind-mounted stack (confirmed: apt only serves the
  # latest X.Y.Z patch for a series, not the host's exact patch version, so
  # a blanket overwrite silently downgrades/upgrades libs that were already
  # right and only breaks the version match). Fix: check what ldd actually
  # reports missing first; only fetch + copy those specific missing files.
  # Never overwrite an existing library file.
  NVIDIA_LIB=$(find /usr/lib/x86_64-linux-gnu /usr/lib -iname "libGLX_nvidia.so.[0-9]*.[0-9]*" 2>/dev/null | head -1)
  echo "NVIDIA_LIB=${NVIDIA_LIB:-not found}"
  if [ -n "$NVIDIA_LIB" ]; then
    echo "--- ldd on $NVIDIA_LIB (pristine, pre-fix) ---"
    LDD_OUT=$(ldd "$NVIDIA_LIB" 2>&1)
    echo "$LDD_OUT"
    MISSING_LIBS=$(echo "$LDD_OUT" | awk '/not found/ {print $1}')
    if [ -z "$MISSING_LIBS" ]; then
      echo "All dependencies of $NVIDIA_LIB already resolve -- stack is complete, nothing to extract."
    else
      echo "Missing dependencies detected: $MISSING_LIBS"
      DRIVER_VER=$(basename "$NVIDIA_LIB" | sed -E 's/^libGLX_nvidia\.so\.//')
      echo "Exact host driver version: ${DRIVER_VER:-unknown}"
      DRIVER_SERIES=$(echo "$DRIVER_VER" | grep -oE '^[0-9]+')
      if [ -n "$DRIVER_SERIES" ]; then
        mkdir -p /tmp/nvidia-extract && cd /tmp/nvidia-extract
        for PKG in "libnvidia-gl-${DRIVER_SERIES}" "libnvidia-compute-${DRIVER_SERIES}"; do
          echo "Downloading $PKG (series-level package, exact patch not available via apt)..."
          apt-get download "$PKG" 2>&1 || echo "$PKG not available or download failed"
        done
        echo "Downloaded $(ls -1 *.deb 2>/dev/null | wc -l) .deb file(s)"
        for DEB in *.deb; do
          [ -f "$DEB" ] || continue
          echo "Extracting $DEB via dpkg-deb -x..."
          dpkg-deb -x "$DEB" /tmp/nvidia-extract/root 2>&1 || echo "extraction failed for $DEB"
        done
        COPIED=0
        for MISSING in $MISSING_LIBS; do
          SRC=$(find /tmp/nvidia-extract/root -iname "$MISSING" 2>/dev/null | head -1)
          if [ -n "$SRC" ]; then
            echo "Copying only the missing file: $SRC -> /usr/lib/x86_64-linux-gnu/$MISSING (does not touch present libs)"
            cp -av "$SRC" "/usr/lib/x86_64-linux-gnu/$MISSING" 2>&1 && COPIED=$((COPIED+1))
          else
            echo "WARNING: $MISSING not found in extracted package either (series ${DRIVER_SERIES} apt package may not carry it) -- leaving unresolved"
          fi
        done
        echo "Copied $COPIED of $(echo "$MISSING_LIBS" | wc -w) missing file(s). All pre-existing libs left untouched."
        cd /tmp/oev_run
        ldconfig
        echo "--- ldd on $NVIDIA_LIB (after targeted fix) ---"
        ldd "$NVIDIA_LIB" 2>&1 || echo "ldd failed to run"
      else
        echo "Could not parse driver series from version '$DRIVER_VER' -- skipping extraction"
      fi
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
# Fail-fast network config: a genuinely bad host (slow route to crates.io/GitHub)
# should abort quickly rather than burn 30-60+ min retrying — cheaper to let the
# workflow redispatch onto a different Vast.ai offer than to wait it out here.
export CARGO_NET_RETRY=2
export CARGO_HTTP_TIMEOUT=15
git clone --depth 1 https://github.com/JhnsonO/video-stitcher.git /tmp/reco-src 2>&1 | tee -a build.log
clone_rc=${PIPESTATUS[0]}
if [ "$clone_rc" -ne 0 ]; then
  echo "FATAL: git clone failed (exit $clone_rc), see build.log" | tee -a build.log
  exit 1
fi
cd /tmp/reco-src
RECO_SHA=$(git rev-parse HEAD)
echo "JhnsonO/video-stitcher HEAD: $RECO_SHA" | tee -a build.log
RECO_BIN="/tmp/reco-src/target/release/reco"
BIN_CACHE_ASSET="reco-cli-${RECO_SHA}.tar.gz"
bin_cache_hit=0
if [ -n "$GH_TOKEN" ]; then
  BIN_RELEASE_ID=$(gh_cache_release_id)
  if [ -n "$BIN_RELEASE_ID" ] && gh_cache_download "$BIN_RELEASE_ID" "$BIN_CACHE_ASSET" /tmp/reco-cli-cache.tar.gz; then
    echo "reco-cli binary cache HIT ($BIN_CACHE_ASSET) — skipping cargo build" | tee -a build.log
    mkdir -p "$(dirname "$RECO_BIN")"
    tar -xzf /tmp/reco-cli-cache.tar.gz -C "$(dirname "$RECO_BIN")"
    chmod +x "$RECO_BIN"
    bin_cache_hit=1
  fi
fi
if [ "$bin_cache_hit" -eq 0 ]; then
  echo "reco-cli binary cache MISS ($BIN_CACHE_ASSET, or no GH_TOKEN) — building from source" | tee -a build.log
  # Hard wall-clock cap on the whole build (deps fetch + compile). Prior successful
  # builds completed in ~10 min; 20 min gives headroom for a normal run while still
  # aborting a stalled/slow-network host well before it burns an hour.
  timeout 1200 stdbuf -oL -eL cargo build --release -p reco-cli -v 2>&1 | tee -a /tmp/oev_run/build.log
  build_rc=${PIPESTATUS[0]}
  if [ "$build_rc" -eq 124 ]; then
    echo "FATAL: cargo build timed out after 20min (likely slow-network host), see build.log" | tee -a /tmp/oev_run/build.log
    exit 1
  elif [ "$build_rc" -ne 0 ]; then
    echo "FATAL: cargo build failed (exit $build_rc), see build.log" | tee -a /tmp/oev_run/build.log
    exit 1
  fi
  if [ ! -x "$RECO_BIN" ]; then
    echo "FATAL: build reported success but binary not found at $RECO_BIN" | tee -a /tmp/oev_run/build.log
    exit 1
  fi
  if [ -n "$GH_TOKEN" ]; then
    echo "Caching compiled reco-cli binary for next run..." | tee -a build.log
    tar -czf /tmp/reco-cli-cache.tar.gz -C "$(dirname "$RECO_BIN")" "$(basename "$RECO_BIN")"
    BIN_RELEASE_ID=$(gh_cache_release_id_or_create)
    gh_cache_upload "$BIN_RELEASE_ID" /tmp/reco-cli-cache.tar.gz "$BIN_CACHE_ASSET" 2>&1 | tee -a build.log \
      || echo "reco-cli binary cache upload failed (non-fatal)" | tee -a build.log
  fi
fi
echo "Build OK: $RECO_BIN" | tee -a /tmp/oev_run/build.log
echo "--- reco stitch --help (checking for a software/CPU fallback flag) ---" | tee -a /tmp/oev_run/build.log
"$RECO_BIN" stitch --help 2>&1 | tee -a /tmp/oev_run/build.log || echo "reco stitch --help failed" | tee -a /tmp/oev_run/build.log
cd /tmp/oev_run

echo "=== calibrate.log: reco calibrate ===" | tee calibrate.log
# Lens auto-detect matches by camera-model string from GPMF telemetry, which
# GoPro doesn't always expose in a form reco recognizes; when it can't match,
# it silently falls back to a generic Mobius 4K profile with the wrong
# distortion model (confirmed via calibrate.log on run 31322462730 -
# see docs/ai-project-state.md). Both cameras are GoPro Hero 10, Wide mode,
# so pin the correct Gyroflow profile explicitly instead of relying on
# auto-detect.
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

echo "=== stitch.log: reco stitch ===" | tee -a stitch.log
# Cylindrical FOV/resolution knobs are optional env vars (set by
# oev-reco-stitch.yml workflow_dispatch inputs); blank = reco-cli default
# for that flag. See docs/ai-project-state.md for the M1 gap-artifact
# finding that motivated exposing these instead of hardcoding 7680x1080.
STITCH_ARGS=(stitch left.mp4 right.mp4 -c match.json -o panorama.mp4
  --width "${OUT_WIDTH:-7680}" --height "${OUT_HEIGHT:-1080}"
  --no-zero-copy --projection cylindrical-stereo)
[ -n "${YAW_SPAN_DEG:-}" ] && STITCH_ARGS+=(--yaw-span-deg "$YAW_SPAN_DEG")
[ -n "${VERTICAL_FOV_DEG:-}" ] && STITCH_ARGS+=(--vertical-fov-deg "$VERTICAL_FOV_DEG")
[ -n "${YAW_CENTER_DEG:-}" ] && STITCH_ARGS+=(--yaw-center-deg "$YAW_CENTER_DEG")
[ -n "${PITCH_CENTER_DEG:-}" ] && STITCH_ARGS+=(--pitch-center-deg "$PITCH_CENTER_DEG")
[ -n "${MAX_FRAMES:-}" ] && STITCH_ARGS+=(--max-frames "$MAX_FRAMES")
echo "reco stitch args: ${STITCH_ARGS[*]}" | tee -a stitch.log
stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log
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

