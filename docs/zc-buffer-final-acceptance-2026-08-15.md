# OEV option-2 final acceptance — 2026-08-15

Implementation under test remained frozen at:

- `JhnsonO/video-stitcher@61aced9687d2c441c48d11e29d3aa28df18b3beb`
- `JhnsonO/wgpu@d74e00f2415e55c0f09a87b0497d66d8192a44bb`

No production Reco pin or production workflow was changed.

## Final acceptance run

GitHub Actions run `31895422159`, artifact `9249712146`.

Accepted first-attempt RunPod host only after hard preflight:

- GPU: NVIDIA GeForce RTX 4090
- Driver: 570.195.03
- Vulkan: PASS
- CUDA init: PASS
- NVDEC: PASS
- ORT CUDA EP: PASS
- Price: $0.74/hr
- Pod cleanup: HTTP 204 confirmed
- Approximate pod cost: ~$0.055

## Test 1 — P010 / 10-bit runtime: PASS

A 3-second HEVC Main10 3840x2160 fixture was generated from the real GoPro benchmark frames on the accepted 4090 pod. Both files were independently probed before Reco:

- codec: HEVC
- profile: Main 10
- pixel format: yuv420p10le
- dimensions: 3840x2160

Reco then exercised the Linux option-2 path end-to-end:

- shared Y buffers: `R16Unorm`, pitch 7680, size 16,588,800 bytes
- shared UV buffers: `Rg16Unorm`, pitch 7680, size 8,294,400 bytes
- `OrtGpuDetector ... 10bit=true`
- `GpuResident detection: CUDA path (TensorRT/ORT-CUDA)`
- Decode summary: `GPU zero-copy (CUDA shared buffer/Vulkan)`
- 60/60 output frames completed
- Runtime: 7.3 s, 8.2 reported fps

Validation frames 0, 30 and 59 were extracted and independently inspected: normal football imagery, correct colour, no green/corruption.

**Verdict: P010 runtime risk is cleared for the option-2 architecture.**

## Test 2 — RTX 4090 same-pod A/B: performance PASS; fallback teardown remains host-sensitive

Exact same pod, Reco SHA, source videos, calibration, YOLO model, tracking mode, lookahead, detection interval, output size and 180-frame workload. Only `--no-zero-copy` differed.

### Option 2

- process exit: 0
- 180/180 frames completed
- wall: 20.347 s
- wall fps: 8.847
- Reco session: 180 frames in 19.7 s; session average 10.4 fps
- CUDA shared-buffer/Vulkan path active
- ORT CUDA EP active
- validation frame 90 visually normal

### `--no-zero-copy`

- 180/180 frames and session summary completed before teardown
- wall: 33.463 s
- wall fps: 5.379
- Reco session: 180 frames in 33.1 s; session average 6.1 fps
- validation frame 90 visually normal and consistent with option-2 output
- process exit: 139 only after output completion, with the known CUDA/FFmpeg context teardown fingerprint (`cuCtxPopCurrent`, `cuCtxPushCurrent`, `cuMemFree` failures)

### A/B result

- speedup: 1.645x
- wall-clock improvement: +64.5%

The workflow is red because its intentionally strict evaluator required `--no-zero-copy` exit code 0. The A/B performance measurement itself remains valid because both 180-frame outputs and summaries completed before the fallback teardown crash.

This does **not** overturn run `31894036556`, which previously compared the clean option-2 branch against the exact production base and established that the fallback teardown crash is pre-existing / host-sensitive rather than introduced by option 2. This run demonstrates that the host-sensitive teardown can still occur on the clean branch on another 4090 host even at driver 570.195.03.

## Recommendation

The two requested pre-merge option-2 acceptance questions are answered:

1. Real P010/10-bit runtime path: **PASS**.
2. RTX4090 same-pod performance A/B: **PASS for correctness/performance evidence**, option 2 is ~64.5% faster wall-clock (8.85 vs 5.38 fps).

Option 2 itself is suitable to proceed to code review / merge consideration. The `--no-zero-copy` teardown instability should remain a separate reliability ticket; do not conflate that post-output fallback teardown issue with the shared-buffer implementation.
