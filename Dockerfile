# OEV Test Runtime v1 -- runtime-lite image
#
# Small, fast-pulling inference-only image. Heavy build/model assets
# (compiled reco-cli binary, YOLO26 ONNX models, build manifest) live on
# a persistent RunPod Network Volume instead of image layers -- see
# .github/workflows/oev-populate-volume.yml, which builds those assets
# on a throwaway pod and writes them to the volume mount path
# (/runpod-volume/oev-runtime, kept in sync with oev-test-runtime-benchmark.yml
# and oev_test_runtime_benchmark_remote.sh).
#
# This is a WHERE-things-are-built-and-stored change only -- WHAT gets
# built (pinned reco-cli SHA, YOLO26 s/m/l/x @1920 export parameters) is
# unchanged from the prior fully-baked image and is now owned by the
# populate-volume workflow instead of this Dockerfile.
#
# CPU-detector-only by design, same as before -- Reco's GPU-resident
# detector (OrtGpuDetector) is only reachable with --zero-copy, which
# stays disabled in production due to the unresolved NV12->RGB
# chroma-plane corruption bug (see docs/ai-project-state.md). This image
# does not change that.
#
# Do NOT tag this `latest`. Tag format: v1-lite-<short-sha-of-this-file>.

FROM runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404

ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------
# 1. Inference-time apt dependencies only.
#    ffmpeg runtime (6.1.1) already ships in this base image. curl is
#    needed at runtime for the lens-profile fetch the benchmark/followcam
#    remote scripts perform against raw.githubusercontent.com. Everything
#    build-only (build-essential, clang/libclang-dev, cmake, pkg-config,
#    ffmpeg -dev headers, the Rust toolchain, git+cargo-build) has been
#    removed -- those now run once, on the volume-populate pod, not on
#    every pull of this image.
# ---------------------------------------------------------------------
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      vulkan-tools libvulkan1 \
      aria2 \
      curl \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version | head -1

# ---------------------------------------------------------------------
# 2. CUDA userspace: cudart only, no driver package.
#    Verbatim, unchanged from the prior image -- same fallback chain
#    from runpod_bootstrap.sh section 4. Never the `cuda-runtime`
#    meta-package (pulls a libnvidia-compute driver package and can
#    conflict with whatever driver the RunPod host actually has at
#    container-run time).
# ---------------------------------------------------------------------
RUN set -e; \
    for ver in 12-8 12-9 12-6 12-5; do \
      if apt-get update -qq && apt-get install -y -qq "cuda-cudart-${ver}" --no-install-recommends; then \
        echo "CUDART_INSTALLED=cuda-cudart-${ver}" > /etc/oev_cudart_version; \
        break; \
      fi; \
    done; \
    test -f /etc/oev_cudart_version || { echo "FATAL: could not install any cuda-cudart-12-* variant"; exit 1; }; \
    rm -rf /var/lib/apt/lists/*
ENV LD_LIBRARY_PATH="/usr/local/cuda-12.8/lib64:${LD_LIBRARY_PATH}"

# ---------------------------------------------------------------------
# 3. Volume mount point + runtime PATH/LD_LIBRARY_PATH.
#    /runpod-volume/oev-runtime is where oev-populate-volume.yml writes
#    reco/models/manifest.json (via the pod's networkVolumeId +
#    volumeMountPath=/runpod-volume). Directory is created empty here so
#    the image is self-documenting about the expected layout even before
#    a volume is attached; the actual binary/models only exist once a
#    volume is mounted at container-run time.
# ---------------------------------------------------------------------
RUN mkdir -p /runpod-volume/oev-runtime/bin /runpod-volume/oev-runtime/models
ENV PATH="/runpod-volume/oev-runtime/bin:${PATH}"
ENV LD_LIBRARY_PATH="/runpod-volume/oev-runtime/bin:${LD_LIBRARY_PATH}"

WORKDIR /runpod-volume/oev-runtime
