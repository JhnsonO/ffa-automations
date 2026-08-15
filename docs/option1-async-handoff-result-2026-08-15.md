# OEV option 1 — async CUDA/Vulkan slot handoff result (15 Aug 2026)

## Verdict

**Performance hypothesis falsified on the representative RTX 4090 workload. Do not merge option 1 for performance.**

The reverse-semaphore design is functionally correct, but replacing the per-frame CPU Vulkan completion poll produced **0.0% measured wall-clock improvement** in a same-pod symmetric A/B. The extra synchronization/lifetime complexity is therefore not justified for the current workload.

## Source under test

Merged blocking-poll base:
- `JhnsonO/video-stitcher@f27cbb6d0d65fcf9a11fb4d82d119ae214695318`

Async handoff candidate:
- branch `feature/async-vulkan-buffer-completion`
- clean head `a5de6a23294eea91e0f52831ae4f3686ec619be1`
- implementation commit `b21cc5d515bb24aa989cd5223893da81d1fd2b26`

The clean branch differs from the merged base in exactly 9 source files. The implementation adds a Vulkan->CUDA completion semaphore per camera/decode slot:

`CUDA writes -> signal ready -> Vulkan waits/copies -> signal completion -> CUDA waits before slot reuse`

The session/render thread no longer performs a per-frame blocking Vulkan `device.poll(wait_indefinitely())`. A single Vulkan-idle lifetime fence remains at teardown.

## Compile proof

`video-stitcher` Actions run `31897778027`:
- `cargo check --release --locked -p reco-cli --features cuda`: PASS
- targeted `nv12_and_p010_plane_pitches_are_copy_aligned` test: PASS
- rustfmt / diff checks: PASS

Earlier red compile-harness attempts were not source failures: one lacked FFmpeg development headers, and one invoked the targeted test with an invalid `reco-core` feature flag. Source was unchanged for those infrastructure corrections.

## Hardware A/B — VERIFIED

`ffa-automations` run `31898658490`, job `95045758884`, artifact `9250554971`.

Accepted host after hard preflight:
- GPU: NVIDIA GeForce RTX 4090
- driver: 570.195.03
- Vulkan: PASS
- CUDA init: PASS
- NVDEC: PASS
- ORT CUDA: PASS
- RunPod price: $0.74/hr
- pod termination: HTTP 204 confirmed

Inputs were the exact proven Google Drive benchmark pair:
- `left_2274_19s.mp4`
- `right_2274_19s.mp4`
- HEVC 3840x2160, 60000/1001 fps

Both exact Reco SHAs were freshly built on the same accepted pod.

### Warmup

- base, 30 frames: 6.317709 s, exit 0
- async, 30 frames: 6.117777 s, exit 0

### Symmetric timed sequence

Order was deliberately `base -> async -> async -> base` to reduce cache/thermal/order bias. Each run rendered exactly 180 frames and exited 0.

- base run 1: 20.804287 s
- async run 1: 20.814920 s
- async run 2: 20.836115 s
- base run 2: 20.848745 s

Aggregated:
- base average: **20.827 s / 8.643 fps**
- async average: **20.826 s / 8.643 fps**
- speedup: **1.000x**
- improvement: **0.0%**

Harness verdicts:
- `OPTION1_CORRECT=PASS`
- `OPTION1_PERF=FLAT`
- `OPTION1_RUNTIME=PASS`

No `cuWaitExternalSemaphoresAsync`, completion-wait, CUDA, or invalid-external-handle error was found in the candidate runs. Repeated double-buffer slot reuse completed without deadlock.

## Visual validation

Artifact SHA-256: `27998ee9f3beef8721b4692ff32632407cf10ed492d2f079126db07ea34e1cba`.

Frame 90 was extracted from the first base and first async 180-frame outputs. Both were independently inspected: normal football imagery, correct colour, no green/corruption, and no obvious visual difference between the two paths.

## Interpretation

The previous assumption that the conservative per-frame Vulkan completion poll was a meaningful throughput ceiling is **not supported by this representative 4090 test**. Removing it safely changes synchronization architecture but does not improve end-to-end wall-clock throughput.

This does not prove the poll can never matter under a different workload; it proves that it is not worth adding this complexity as a performance optimization for the workload we currently care about.

## Recommendation

- Keep merged shared-buffer architecture `f27cbb6d...` as the production candidate.
- Do **not** merge draft video-stitcher PR #4 for performance.
- Close/park option 1 with this evidence retained for reference.
- Move the optimization effort to a measured downstream bottleneck instead of further Vulkan synchronization work. A bounded 4090 stage profile should choose between broader pipeline scheduling and detector optimization; earlier L4 telemetry already showed detector inference as a major cost center.
