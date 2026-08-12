# OEV Test Runtime v1
#
# Versioned, reproducible experiment environment for the OEV/reco-cli
# follow-cam pipeline. Bakes in everything that runpod_bootstrap.sh
# otherwise rebuilds from scratch every dispatch (apt build deps, Rust
# toolchain, reco-cli compiled at the pinned production SHA, and
# pre-exported 1920px YOLO26 ONNX models) so a RunPod experiment pod can
# skip straight to inference.
#
# CPU-detector-only by design: Reco's GPU-resident detector
# (OrtGpuDetector) is only reachable when the pipeline runs with
# --zero-copy, which is disabled in production due to an unresolved
# NV12->RGB chroma-plane corruption bug (see docs/ai-project-state.md).
# This image does not change that -- CpuYoloDetector remains the
# detector that actually runs. Fixing the zero-copy path is a separate,
# later Reco ticket.
#
# Do NOT tag this `latest`. Tag format: v1-reco-<short-sha>.

FROM runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404

ARG RECO_SHA=53fe10f548d5767ad94ef66aeaedf2d8c7161f27
ARG RECO_REPO=https://github.com/JhnsonO/video-stitcher
ARG ULTRALYTICS_VERSION=8.4.118
ENV DEBIAN_FRONTEND=noninteractive

# ---------------------------------------------------------------------
# 1. Build/runtime apt dependencies.
#    Verbatim from runpod_bootstrap.sh section 2: this base does NOT
#    ship pkg-config, libclang, or ffmpeg -dev headers, but DOES ship a
#    working ffmpeg 6.1.1 runtime already.
# ---------------------------------------------------------------------
RUN apt-get update -qq && apt-get install -y -qq --no-install-recommends \
      git curl build-essential pkg-config cmake \
      clang libclang-dev \
      libavcodec-dev libavformat-dev libavutil-dev libswscale-dev libavfilter-dev \
      vulkan-tools libvulkan1 \
      aria2 \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version | head -1

# ---------------------------------------------------------------------
# 2. Rust toolchain (build-time only; matches runpod_bootstrap.sh section 3).
# ---------------------------------------------------------------------
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
ENV PATH="/root/.cargo/bin:${PATH}"
RUN rustc --version

# ---------------------------------------------------------------------
# 3. CUDA userspace: cudart only, no driver package.
#    Verbatim fallback chain from runpod_bootstrap.sh section 4 -- never
#    the `cuda-runtime` meta-package (pulls a libnvidia-compute driver
#    package and can conflict with whatever driver the RunPod host
#    actually has at container-run time).
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
# 4. Build reco-cli at the pinned production SHA, --features cuda.
#    Build-time only -- no physical GPU required to compile: ort-sys
#    downloads a prebuilt ONNX Runtime binary (cuDNN is a runtime dlopen
#    dependency of that binary, not a compile-time link), and nvcc
#    (invoked by reco-detect's build.rs for the CUDA preprocessing
#    kernels) targets a compute-capability list, not a live device.
# ---------------------------------------------------------------------
WORKDIR /opt/video-stitcher
RUN git clone "${RECO_REPO}" . \
    && git checkout "${RECO_SHA}" \
    && echo "video_stitcher_sha=$(git rev-parse HEAD)" > /etc/oev_build_manifest.env

RUN --mount=type=cache,target=/root/.cargo/registry \
    --mount=type=cache,target=/opt/video-stitcher/target \
    cargo build --release -p reco-cli --features cuda \
    && mkdir -p /opt/oev-runtime/bin \
    && cp target/release/reco /opt/oev-runtime/bin/reco \
    && find target/release -maxdepth 1 -iname 'libonnxruntime*.so*' -exec cp {} /opt/oev-runtime/bin/ \; \
    && /opt/oev-runtime/bin/reco --version > /etc/oev_reco_version.txt

ENV PATH="/opt/oev-runtime/bin:${PATH}"
ENV LD_LIBRARY_PATH="/opt/oev-runtime/bin:${LD_LIBRARY_PATH}"

# ---------------------------------------------------------------------
# 5. Pre-export YOLO26 s/m/l/x @ 1920px ONNX models. No nms=True /
#    end2end=False -- YOLO26's default one-to-one NMS-free export head
#    already produces the (1,300,6) layout Reco's CpuYoloDetector
#    expects (confirmed this session against the yolov8n.onnx baseline
#    layout). Deterministic filenames, sha256 recorded in the manifest.
# ---------------------------------------------------------------------
WORKDIR /opt/oev-runtime/models
RUN python3 -m venv /opt/oev-runtime/yolo-venv \
    && /opt/oev-runtime/yolo-venv/bin/pip install -q --upgrade pip \
    && /opt/oev-runtime/yolo-venv/bin/pip install -q "ultralytics==${ULTRALYTICS_VERSION}" onnxruntime-gpu

RUN set -e; \
    for size in s m l x; do \
      /opt/oev-runtime/yolo-venv/bin/yolo export model="yolo26${size}.pt" format=onnx imgsz=1920 \
        project=/opt/oev-runtime/models name="yolo26${size}" exist_ok=True; \
      mv "/opt/oev-runtime/models/yolo26${size}/yolo26${size}.onnx" "/opt/oev-runtime/models/yolo26${size}.onnx"; \
      rm -rf "/opt/oev-runtime/models/yolo26${size}"; \
      python3 -c "import onnx; m=onnx.load('/opt/oev-runtime/models/yolo26${size}.onnx'); \
inp=m.graph.input[0].type.tensor_type.shape.dim; out=m.graph.output[0].type.tensor_type.shape.dim; \
print('yolo26${size} input=', [d.dim_value or d.dim_param for d in inp], 'output=', [d.dim_value or d.dim_param for d in out])"; \
    done

RUN cd /opt/oev-runtime/models && sha256sum yolo26*.onnx > /opt/oev-runtime/models/models.sha256

# ---------------------------------------------------------------------
# 6. Build metadata manifest baked into the image.
# ---------------------------------------------------------------------
RUN { \
      echo "{"; \
      echo "  \"reco_sha\": \"${RECO_SHA}\","; \
      echo "  \"ultralytics_version\": \"${ULTRALYTICS_VERSION}\","; \
      echo "  \"onnxruntime_gpu_version\": \"$(/opt/oev-runtime/yolo-venv/bin/python3 -c 'import onnxruntime; print(onnxruntime.__version__)')\","; \
      echo "  \"base_image\": \"runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404\","; \
      echo "  \"build_timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\","; \
      echo "  \"reco_version_string\": \"$(cat /etc/oev_reco_version.txt | tr -d '\n')\","; \
      echo "  \"models_sha256\": \"$(cat /opt/oev-runtime/models/models.sha256 | tr '\n' ';')\""; \
      echo "}"; \
    } > /opt/oev-runtime/manifest.json \
    && cat /opt/oev-runtime/manifest.json

WORKDIR /opt/oev-runtime
