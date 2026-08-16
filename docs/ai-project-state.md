# FFA / OEV — Current Status (updated 16 Aug 2026)

One line per active or recently-closed track. Full detail is below, newest generally near the top but not guaranteed — check dates. Closed self-contained sagas (Clip Extractor, Flatcam, Runner failsafe) live in `docs/ai-project-state-archive.md`.

- **OEV zero-copy CUDA/Vulkan interop — RESOLVED, in production.** Shared-VkBuffer architecture merged (`video-stitcher@b2fc622...`), rolled out to standard FFA workflows. Direct shared-`VkImage` approach is a dead end — do not reopen without genuinely new evidence.
- **Frame-stride 3 — OPEN REGRESSION after multi-sample visual validation (16 Aug).** The full-rate sparse-analysis implementation is live in code, but it is **not product-accepted as the default**: `sample_01` 180s looked mostly watchable with slight misses, while `sample_02` 180s showed severe lag/near-static camera behaviour for large portions. Treat stride 1 v4 as the accepted quality reference until the cadence/timebase issue is resolved.
- **Panner-lag / ball-out-of-frame (`cluster_alpha`/EMA/lookahead) — OPEN, top product-quality priority.** Ball outside visible frame 51–57% of tracking time, model-independent. Not yet actioned.
- **YOLO26m vs YOLOv8n — YOLO26m preferred for current follow-cam experiments.** Prior A/B evidence showed roughly half the track-loss rate (~38 vs ~80 over 180s), higher confidence (~0.907 vs ~0.819), for ~7% more processing time. Keep visual product acceptance separate from detector metrics.
- **RunPod test allocator — HARDENED and proven; capacity/host compatibility remains variable.** Experiment path now excludes a GPU type after real NVDEC failure and falls through the widened pool; RTX 5090 is excluded from this experiment after a real Reco detector smoke failed with `cudaErrorNoKernelImageForDevice`. Capacity remains the main transient failure mode.
- **Network volume map — OPEN maintenance debt.** EU-RO-1 current; EUR-IS-1/US-IL-1 are stale caches; AP-JP-1's volume no longer exists on RunPod. `OEV_NETWORK_VOLUME_MAP` update blocked on a GitHub token permissions error (403).
- **OEV/Reco licensing (AGPL-3.0) — OPEN, commercial blocker before customer-facing launch.** Full review needed before OEV is sold; not a technical blocker for current testing. See "Frame-stride testing + production rollout — FINAL HANDOFF" below for detail.
- **P010 10-bit real-fixture test — not yet done.** Compiled/unit-tested only.
- **Standardized 180s sample validation — ACTIVE.** `sample_01` through `sample_05` have been rendered on the current stride-3 experiment path; `sample_02` exposed a severe visual regression. A same-sample stride-1 v4 control (`31939820386`) completed successfully and is ready for visual A/B. Internal processing-time variance across samples is the next measurement task.

---

## Frame-stride 3 multi-sample validation — REGRESSION FOUND; stride-1 control rendered (16 Aug 2026)

**Current verdict:** stride 3 is **not accepted for product-quality follow-cam yet**, despite the earlier 30s speed benchmark and `sample_01` 180s looking reasonably watchable. Multi-sample testing exposed a severe camera-response regression on `sample_02`. The accepted quality reference remains the v4 stride-1 behaviour from run `31913398625` (YOLO26m, FOV 44°, lookahead 1.5s, `cluster_alpha=0.08`, v3 trajectory hysteresis + v4 micro-damping).

**Implementation that is being tested:** Reco `b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085` keeps full-rate source rendering while running detector/tracker/panner only on analysis frames (`source_index % frame_stride == 0`), reuses `session.last_world_state.clone()` on render-only frames, and interpolates camera poses between sparse anchors. FFA main rollout commit `a729c5281ef1cb50046ce4ad721bdb1352ebf91e` enabled `--frame-stride 3` on the normal testing scripts. The earlier same-sample 30s benchmark (~2.737x faster stitch wall time) remains valid as a speed result; it is **not sufficient quality evidence**.

**Adapter/allocator fixes while getting the real 180s test to execute:** experiment branch `experiment/lookahead-ball-containment-01`. Bootstrap nested-quote bug fixed at `5c3d9309c8c605b090e3da0a8c17db311cf0c090`; the YOLO26 stride wrapper anchor was moved to the unique final base-execution boundary and validated before paid dispatch (fix lineage includes `21b3774`). These were harness defects, not Reco quality findings. The broadened allocator (`d3167ba9ca135c4a7bb53c24f77db703cec572f0`) proved GPU-type failover. RTX 5090 is excluded from this experiment because run `31918562049` passed Vulkan/CUDA/NVDEC/basic ORT but the real Reco detector smoke failed `cudaErrorNoKernelImageForDevice` on the YOLO QuickGelu kernel. Run `31918951122` then showed the remaining dominant failure mode: after rejecting a bad 4090, the compatible fallback types were out of capacity.

**Standardized 180s sample batch:** sample set `GX010197-seed1384188843`, same v4 camera settings + YOLO26m + lookahead 1.5 + alpha 0.08, with stride 3 as the intended cadence change. `sample_01` completed after capacity retries and Johnson judged it to have only a few slight misses and to be broadly watchable. The remaining batch all completed workflow-successfully from experiment SHA `f75abe28eb98b85ae749ee11d52b4aec2f68a75c`: `sample_02` run `31938283243`, `sample_03` `31938286396`, `sample_04` `31938289343`, `sample_05` `31938292331`. **Green workflow status is execution evidence only; visual quality remains the acceptance gate.**

**Decisive visual finding:** Johnson reviewed `sample_02` and reported that the camera struggled to keep up with the ball for large portions and that even at the start the camera barely moved. This is materially worse than the small misses in `sample_01` and is enough to reject the present stride-3 tuning pending the A/B control.

**Leading hypothesis — plausible, not yet proven by the control:** the accepted v4 limits/hysteresis are expressed per analysis update rather than in real time. Stride 3 drops meaningful panner updates from ~60Hz to ~20Hz but leaves values such as max yaw `0.75°/update`, confirmation `18` updates, and missing-ball hold `24` updates unchanged. In real-time terms that can turn roughly `45°/s` camera authority into `15°/s`, `18` updates from ~0.3s to ~0.9s, and `24` from ~0.4s to ~1.2s. That matches the observed slow-response failure shape, but do **not** call it proven until the same-sample stride-1 output is visually checked. The robust eventual fix should make limits/hysteresis time-aware/source-frame-aware rather than blindly multiplying every constant by 3.

**Clean same-sample control:** branch `experiment/sample02-v4-stride1-control`, head `6bc0857208b3d82a4f764a4f9658e42fdfc2057e`, run `31939820386`. It keeps the current b2fc Reco, broadened allocator, v4 stabilizer adaptation, YOLO26m, lookahead 1.5 and alpha 0.08, but does **not** inject `--frame-stride 3`. Workflow completed SUCCESS. **Visual comparison against stride-3 `sample_02` is still pending**; do not infer the product verdict from CI success alone.

**Processing-time variance — active investigation, do not conflate with pod setup:** for the four 180s batch runs, the outer `Upload + run sample-baseline test script` step lasted `6m17s` (`sample_02`), `18m26s` (`sample_03`), `13m05s` (`sample_04`), and `13m04s` (`sample_05`). This excludes allocator/bootstrap but is still only an outer step duration, **not yet the authoritative internal Reco processing time**. Next task is to pull each run's internal Reco/render timing, GPU model and FPS, normalize by the identical 180s source duration, and separate decode / YOLO / tracking+panner / stitch+render / encode where telemetry permits. Root-cause the ~3x spread; do not hand-wave it as different scenes being harder without log evidence.

**Immediate next actions:** (1) visually compare the completed stride-1 `sample_02` control with stride-3 `sample_02`; (2) finish the internal processing-time/GPU comparison for `sample_02`–`sample_05`; (3) if the control confirms cadence is the regression, make the v4 response/hysteresis time-aware so stride is a compute knob rather than changing the cameraman's real-time behaviour; (4) rerun the same multi-sample set before re-declaring stride 3 accepted.

## OEV zero-copy option 2 — shared CUDA/Vulkan buffers integrated; real stereo render correct; A/B measured (15 Aug 2026)

**Outcome: SUCCESS on a feature branch; production remains unchanged.** Linux decode now uses CUDA-VMM allocations imported by Vulkan as `VkBuffer` copy sources, one CUDA-signalled/Vulkan-waited binary semaphore per camera/slot, and one GPU-only buffer-to-ordinary-`VramPool`-texture submission for all four Y/UV planes. Existing render, replay, wgpu-detection fallback, lookahead and immediate-mode paths consume ordinary textures. CUDA detection still consumes the original shared-buffer pointers. There is no CPU pixel round-trip between NVDEC and the normal render textures.

**Exact branches/SHAs:**
- `JhnsonO/video-stitcher` branch `feature/linux-cuda-buffer-zero-copy`, head `61aced9687d2c441c48d11e29d3aa28df18b3beb` (implementation `f7e4dce9`, rustfmt `a055bb17`, corrected committed lock graph `61aced96`), based on production `53fe10f548d5767ad94ef66aeaedf2d8c7161f27`.
- `JhnsonO/wgpu` branch `feature/cuda-buffer-semaphore`, head `d74e00f2415e55c0f09a87b0497d66d8192a44bb`: minimal queue-wait backport parent `62e15ce1` plus `VK_KHR_external_semaphore_fd` adapter enablement. No old shared-image diagnostics are in this branch.
- `JhnsonO/ffa-automations` branch `agent/zc-buffer-copy-proof`; clean integration/control harness evidence is committed through `a20eedd3fca8562f886383fe585b4d2e87de340c`. No production workflow, Reco pin, network-volume binary/manifest, or `--no-zero-copy` setting was changed.

**Implementation scope (14 files, compile-gated against the production base):** workspace `Cargo.toml`/`Cargo.lock`; `reco-core` CUDA/Vulkan/zero-copy interop, session setup/frame processing/buffering/detection/VRAM pool; and `reco-io` SmartFileSource/decode threads. Linux NVDEC copies Y+UV to shared buffers, synchronizes, signals the slot semaphore, and sends the slot. Vulkan stages both camera waits at `TRANSFER`, copies left/right Y+UV into the acquired normal texture slot, and polls before binary-semaphore/slot reuse. Start-time skipped frames consume their semaphore signals with an empty waited submission. Both double-buffer slots, immediate one-slot texture pooling, lookahead lifetime, P010 byte widths, and error-path slot ownership are covered. Other OS paths and the CPU fallback remain present.

**Phase-1 destination-texture proof — VERIFIED byte-exact:** diagnostic Reco `acdcc61ece2f1d9bda453dea32ec3be10d34172a`; compile run `31887205847` / artifact `9247964269`; real fixture run `31888822303`, attempt 2 job `95022628358`, artifact `9248117131`, RTX 4090 / driver `570.195.03`. CUDA readback contained real football data (left Y mean 89.51, right 76.65); all four ordinary destination planes matched the CUDA dumps byte-for-byte; deterministic sentinels at offsets 0 / 4,147,200 / 8,290,560 matched in CUDA and the normal destination texture. The 180-frame render completed with normal football imagery and no green output. Wall-clock: 20.2s / 8.9 fps; session: 17.4s / 10.4 fps. Pod duration 281.5s; approximate cost $0.0579.

**Clean compile proof — VERIFIED:** run `31890863984`, job `95026811217`, artifact `9248925986`. Exact 14-file scope, unique wgpu quartet at `d74e00f2`, rustfmt, locked release `cargo check -p reco-cli --features cuda`, and targeted `nv12_and_p010_plane_pitches_are_copy_aligned` test all passed (1 passed, 0 failed). The two prior clean-gate attempts were not compiler evidence: `31890356726` stopped on rustfmt; `31890484385` stopped on a stale `reco-io -> ash` lock edge. Each was corrected independently before this pass.

**Clean real integration — VERIFIED:**
- Run `31892619238`, job `95031060826`, artifact `9249035815`, RTX 2000 Ada / driver `570.172.08`: start-time `0.2` + `lookahead=0` immediate smoke passed, including synchronized buffer copy and `GpuResident detection: CUDA path (TensorRT/ORT-CUDA)`. A 180-frame buffered stereo render completed at 3.3 fps wall-clock / 3.6 session fps. Four independently extracted checkpoints (frames 0/60/120/179) show normal football imagery, correct colour and no green/corruption. The workflow's final red status was only a false-negative grep for an immediate-only detection log at the separate buffered `detect_and_track_only` call site. Pod cost: approximately $0.0255.
- Run `31893154739`, attempt 2 job `95032753193`, artifact `9249237633`, NVIDIA L4 / driver `570.195.03`: immediate ownership/sync/CUDA-detection smoke passed; the clean 180-frame render passed at 41.4s / 4.4 fps wall-clock (36.8s / 4.9 session fps). Reported averages: shared-buffer wait+copy staging 1.7ms, stitch 0.1ms, readback 0.3ms, submit 0.1ms, encode 0.8ms. The identical same-pod `--no-zero-copy` workload completed 180 frames at 59.4s / 3.0 fps wall-clock (53.2s / 3.4 session fps): the new path was about 1.47x faster wall-clock. Buffered telemetry does not include lookahead-produce detection time; wall-clock is authoritative. Immediate telemetry measured CUDA-resident detection at 206.7ms average on this L4 and identified detection as the bottleneck. Approximate pod cost: $0.0535.
- The L4 baseline process exited 139 only after writing all 180 frames and its session summary. A final same-host regression control, run `31894036556`, job `95034455445`, artifact `9249368772`, RTX 4090 / driver `570.195.03`, compared the clean branch with exact production base `53fe10f5`. Both produced byte-count-valid 180-frame `--no-zero-copy` outputs at the same 3.9 fps wall-clock / 4.4 session fps. The clean branch exited 0; production base exited 139 with the CUDA-context teardown fingerprint. Therefore the option-2 branch did **not** regress the fallback and avoided the pre-existing/host-sensitive teardown failure in this control. Approximate cost: $0.0767.

**Performance interpretation:** on the same L4 workload the new path improves 3.0 -> 4.4 fps wall-clock (+47%). The RTX 4090 phase-1 render measured 8.9 wall-clock / 10.4 session fps versus the historical representative `--no-zero-copy` ~5.8 fps, but that comparison is cross-run. It remains below the historical ~19 fps corrupt direct-image path. The deliberate per-frame Vulkan completion poll makes binary-semaphore/slot reuse safe but is a likely throughput ceiling; do not remove it without a reverse completion signal or equivalent ownership proof.

**Remaining risks / recommendation:** the real fixtures exercised 8-bit NV12; P010 sizing/alignment compiled and unit-tested (3840-wide Y and UV rows are both 7,680 bytes) but still needs one real 10-bit fixture. macOS/Windows paths were cfg-isolated but not runtime-tested here. Recommend opening the implementation for review and running one P010 fixture plus, if a production-performance decision needs it, one clean-branch 4090 same-pod A/B. The architecture is viable and visually correct; do not return to shared-`VkImage` experiments. Do not merge or change the production Reco pin without Johnson's approval.

## OEV zero-copy NV12 corruption — EXP8 allocation-size A/B FALSIFIED; stop-loss reached (15 Aug 2026)

**Single-variable test:** `video-stitcher@58542bbb9b91560e533ccc846a5eb3bd6e9d9db4` (branch `diag/zc-exp8-image-alloc-size`, based directly on Avenue-2 `e810a04e`) changes only Vulkan image import `allocation_size(shared_mem.alloc_size as u64)` -> `allocation_size(mem_reqs.size)`.

**Valid hardware run:** ffa run `31876438503`, job `94992772328`, artifact `9244919570`, RTX 4090 / driver `570.195.03`. Hardened selection required Vulkan, CUDA init, NVDEC and ORT CUDA all PASS before accepting the pod. Reco built successfully and the real GoPro fixture decoded frame 0.

**Controls:** CUDA frame-0 data remained correct (`left Y mean=89.51`, `right Y mean=76.65`). The deterministic CUDA sentinel at offsets `0i`, `4,147,200`, `8,290,560` was present byte-exact in CUDA. The native Vulkan/wgpu readback control also passed byte-exact.

**Decisive result:** despite exact Vulkan allocation sizing, all imported image source planes remained all-zero (`left_y_vram_src 0/8,294,400`, `left_uv_vram_src 0/4,147,200`, right Y/UV similarly zero), and Vulkan saw `0/1024` sentinel bytes at all three offsets. **Allocation-size mismatch is falsified.**

**Infra notes:** compile-only run `31874496979` timed out during `cargo update` before Rust compilation and is not hypothesis evidence. The first two fixture attempts under `31875839603` had broken NVDEC (`CUDA_ERROR_NO_DEVICE`) and were inconclusive. The branch-only launcher was then hardened to reject NVDEC-broken hosts before download/build; attempt 3 above is the valid result.

**Evidence boundary / stop-loss:** CUDA producer, readback, wgpu initial-state/layout, semaphore sync, generic CUDA-VMM -> Vulkan OPAQUE_FD sharing (VkBuffer), external-image capability/dedicated-allocation checks, and allocation sizing have all been addressed. The unresolved defect is image-specific to the CUDA-created allocation being used as an imported linear `VkImage`. Stop parameter-by-parameter tweaking of that path absent new external evidence.

**Next bounded architecture:** CUDA/NVDEC -> shared external `VkBuffer` Y/UV -> CUDA signals external semaphore -> Vulkan waits -> GPU buffer-to-normal-texture copy -> existing render/tracking. First prove one Y plane byte-exact on a diagnostic branch, then a short real GoPro render before integrating Y+UV/double-buffering. No EXP8 Reco change is merged to production.

## OEV zero-copy NV12 corruption — EXP7: generic CUDA↔Vulkan OPAQUE_FD sharing PASSES; failure is image-specific — real hardware, decisive (15 Aug 2026)

**Correction to the immediately-following Avenue-2 entry:** run `31854089581` proved that the synchronized Vulkan **image** path did not observe a CUDA-written sentinel that CUDA itself read back byte-exact. It did **not** prove that CUDA and Vulkan cannot share the same backing allocation in general. EXP7 directly tests CUDA-VMM-export -> Vulkan-import with a `VkBuffer` and passes byte-exact, so the proven failure boundary is now specifically the imported **VkImage** path.

**Compile proof:** `JhnsonO/video-stitcher@9059470f01065d5336af8c94bac27a860156dfec` (`diag/zc-exp7-external-memory-caps`) compiled against `JhnsonO/wgpu@c8b6f2f00895210857f77f2a10fc1a32a80d5148` in run `31871577622`. Exact compile-proven sources: `vulkan.rs` 1476 lines / SHA-256 `4da792382d954f5ffe68865d5ae84db9e778c2f6e452917a7749149f50c41089`; `cuda.rs` 1312 lines / SHA-256 `e81e693071608caef213eb34e68335d064e3aded6c58214147f2b6be4ac303b7`.

**Real EXP7 run:** `31872745393`, `ffa-automations` branch `diag/zc-exp7-real-fixture`, head `3d51d19c414638c11f66d1a57d82705bef56f6d8`. NVIDIA preflight passed, Reco SHA `9059470f...`, wgpu resolution gate passed, EXP7 exited after two shared-texture probes before decode, artifact `oev-zero-copy-diag-31872745393` captured the evidence, and RunPod termination was confirmed.

**VkBuffer control: PASS byte-exact.** Vulkan 1.3.275. External-buffer capabilities for `TRANSFER_SRC|TRANSFER_DST + OPAQUE_FD`: `EXPORTABLE=true`, `IMPORTABLE=true`, `DEDICATED_ONLY=false`, OPAQUE_FD compatible. CUDA wrote the deterministic 1024-byte sentinel at offsets 0 / 524288 / 1047552, CUDA ground truth was byte-exact, CUDA signalled the external semaphore after synchronization, and the exact Vulkan transfer waited on it at `TRANSFER`. Vulkan returned `byte_exact=true` at all three offsets; `ZC_EXP7_BUF_ALIAS_RESULT=PASS`.

**Image capability queries: no obvious compatibility/dedicated-allocation red flag.**
- Y `R8_UNORM` LINEAR 3840x2160: `IMPORTABLE=true`, `DEDICATED_ONLY=false`, OPAQUE_FD compatible, `requiresDedicatedAllocation=false`, `prefersDedicatedAllocation=false`; Vulkan requirement 8,294,400 bytes / alignment 128 / `memoryTypeBits=0xf`; CUDA allocation 8,388,608.
- UV `R8G8_UNORM` LINEAR 1920x1080: same flags; Vulkan requirement 4,147,200 bytes / alignment 128 / `memoryTypeBits=0xf`; CUDA allocation 4,194,304.
`ZC_DIAG` still reports row pitch 3840, subresource offset 0, subresource size exactly pitch*height, and allocation size >= requirements. `EXP7_IMAGE_CAPABILITY_RED_FLAG=NO`.

**Important API correction:** do not use `vkGetMemoryFdPropertiesKHR` with `VK_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD_BIT`; VUID-00674 forbids it. The temporary draft using that query was removed before compile/hardware testing.

**Now ruled out unless contradictory evidence appears:** readback-mechanism failure; wgpu UNINITIALIZED/layout discard; missing CUDA->Vulkan semaphore synchronization; generic CUDA-VMM -> Vulkan OPAQUE_FD sharing failure; obvious external-image capability rejection / OPAQUE_FD incompatibility / dedicated-allocation requirement.

**Remaining failure boundary:** imported **linear VkImage** representation/binding/visibility semantics. One measured difference, not yet a root-cause claim, is allocation sizing: production imports CUDA's VMM granularity-rounded sizes (8,388,608 / 4,194,304) while Vulkan image requirements are smaller exact sizes (8,294,400 / 4,147,200). This is a bounded next experiment candidate, not a conclusion.

**Stop point:** EXP7 recorded; no production merge and no next hypothesis started.

## OEV zero-copy NV12 corruption — Avenue 1 CLOSED (readback mechanism sound), Avenue 2 FALSIFIED (physical aliasing does NOT hold) — real hardware evidence, decisive (15 Aug 2026)

[Historical diagnostic detail retained below unchanged from prior state-file revisions.]

## [ARCHIVED] Clip Extractor cookie/bot-check saga, Flatcam lens/venue work, Runner failsafe

Fully resolved, self-contained sagas (13 July – 2 Aug 2026). Moved to `docs/ai-project-state-archive.md` to keep this file focused on active work. Read the archive only if one of these areas regresses.

## Shared-buffer production rollout — VERIFIED (15 Aug 2026)

- `JhnsonO/video-stitcher` shared-buffer architecture is merged on `main` at `f27cbb6d0d65fcf9a11fb4d82d119ae214695318`.
- Linux production architecture: NVDEC -> CUDA-VMM shared VkBuffers -> CUDA external semaphore -> Vulkan wait -> GPU buffer-to-texture copy -> ordinary wgpu/VramPool textures. There is no CPU pixel round-trip; CUDA detection continues to consume the shared CUDA pointers directly.
- Correctness coverage before rollout: real GoPro NV12, real HEVC Main10/P010, immediate and buffered/lookahead paths, CUDA inference, and visual output all passed. The direct shared-VkImage architecture is closed and must not be reopened without genuinely new evidence.
- Exact merged Reco SHA `f27cbb6d...` was itself exercised on a healthy RTX 4090 in run `31898658490`: two 180-frame base renders completed exit 0 at 8.643 wall fps average and were visually correct.
- EU-RO-1 runtime volume `0hta9vhuue` was refreshed to `f27cbb6d...` successfully in run `31899987177`.
- Standard production-path canary run `31900275966` passed on EU-RO-1 using `sample_01`, 30 s, YOLO26m, lookahead 1.5, cluster_alpha 0.05. The normal FFA sample workflow passed its exact Reco SHA gate, shared-buffer/NVDEC/CUDA evidence gate, AI-tracking acceptance, Drive upload, and pod cleanup.
- The FFA production rollout pins standard RunPod workflows to `f27cbb6d...` and removes the functional `--no-zero-copy` argument from the normal follow-cam/sample scripts. `--no-zero-copy` remains available only as an explicit CLI fallback/diagnostic comparison.
- Option 1 (replace the per-frame CPU Vulkan completion poll with reverse completion semaphores) was fully implemented and hardware-tested but intentionally NOT merged: same-pod RTX 4090 B-A-A-B measured 8.643 fps vs 8.643 fps (0.0% improvement). PR `video-stitcher#4` was closed without merge.
- Product rollout decision: merge the validated FFA production defaults now; do not hold the working shared-buffer path behind optional network-volume cache maintenance.
- Next performance ticket: profile the current production path stage-by-stage on one healthy RTX 4090, then choose detector optimization vs broader pipeline scheduling from measured evidence. Do not revisit option 1 or direct shared-VkImage sharing.

## Full-rate frame stride 3 rollout — MERGED (15 Aug 2026)

- `JhnsonO/video-stitcher/main` advanced from `f27cbb6d0d65fcf9a11fb4d82d119ae214695318` to `b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085`. This adds `reco stitch --frame-stride N` with default `1`; upstream/default Reco behavior therefore remains unchanged unless explicitly enabled.
- Production stride semantics are full-rate output: every source frame is decoded/copied/rendered, while detector/tracker/panner state advances every Nth source frame. Camera poses are interpolated/smoothed between sparse anchors; 1.5s lookahead stays source-time correct. Shared-buffer CUDA/Vulkan zero-copy, NVDEC, CUDA detection, stereo sync, NV12/P010 and `--no-zero-copy` fallback were not changed.
- Hardware evidence on RTX 4090 / EU-RO-1 / YOLO26m / `sample_01` 30s: stride 1 rendered 1,784 frames in ~183.535s; stride 3 rendered the same 1,784 frames at 60000/1001 cadence in ~67.064s. That is ~2.737x faster / ~63.46% lower stitch wall time and approximate compute cost.
- FFA main commit `a729c5281ef1cb50046ce4ad721bdb1352ebf91e` pins the normal RunPod follow-cam, standard sample baseline, YOLO26 A/B and volume-populate build gate to Reco `b2fc622...`; the three normal follow-cam/sample remote scripts explicitly pass `--frame-stride 3`. Low-level diagnostics are not globally forced to stride 3.
- Quality choice remains stride 3 rather than stride 4: the prior same-sample matrix showed materially safer quality tails at stride 3. One retained full-rate stride-3 video is still desirable for final visual evidence when RunPod capacity is available; the first full-rate hardware run completed both renders but its harness failed afterward and did not retain the video.
- PR `JhnsonO/video-stitcher#5` is shown by GitHub as merged because the fork's `main` ref was fast-forwarded directly to its head commit. No CLA bot signature comment was posted.

## Frame-stride testing + production rollout — FINAL HANDOFF (16 Aug 2026)

**Historical rollout status — SUPERSEDED by the 16 Aug multi-sample regression entry near the top of this file.** The implementation is live on Reco/FFA `main`, but subsequent 180s visual validation showed that stride 3 does not yet preserve the accepted v4 camera behaviour on harder footage. Do not treat this section's original rollout verdict as current product acceptance.

### Live code / defaults

- `JhnsonO/video-stitcher/main` is `b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085`.
- Reco exposes `reco stitch --frame-stride N`; default is `1`, so generic/upstream Reco behaviour remains unchanged unless a caller opts in. Validated values are `1..=4`.
- Normal OEV RunPod follow-cam and sample test scripts explicitly pass `--frame-stride 3`.
- FFA rollout commit `a729c5281ef1cb50046ce4ad721bdb1352ebf91e` updated the Reco pin in the normal follow-cam workflow, sample-baseline workflow, YOLO26 A/B workflow and volume-populate build gate, plus added `--frame-stride 3` to the three normal remote render scripts. Low-level plumbing/zero-copy diagnostics are intentionally not globally forced to stride 3.
- Full validation remains available simply by running Reco with stride `1` / omitting the stride flag outside the FFA normal-test wrappers.

### What stride means in the final production implementation

- This is **not** detector interval, encode-only FPS reduction, or a shorter source window.
- Every source frame is still decoded/copied/rendered at the normal output cadence; only detector/tracker/panner AI state advances every Nth source frame.
- Camera poses between sparse AI decisions are interpolated inside Reco (shortest-path yaw plus linear pitch/FOV) and continue through the existing smoothing path, so the output video remains full-rate rather than a sparse ~20 fps render.
- Panner FPS/EMA time constants and ball coast duration are rebased for stride so tracker/panner timing remains tied to real source time.
- Lookahead remains source-time correct: e.g. 1.5 s at ~59.94 fps still represents ~90 source frames while stride 3 gives the panner ~30 future AI anchor states.
- Stereo pairing/sync, CUDA detection, NVDEC, NV12/P010, shared-buffer CUDA/Vulkan zero-copy and `--no-zero-copy` fallback were preserved. No `wgpu` change was required and the abandoned direct shared-`VkImage` path was not reopened.

### Original stride matrix — VERIFIED diagnostic evidence

GitHub Actions run `31904505904`, same healthy RTX 4090 / EU-RO-1 pod at `$0.74/hr`, YOLO26m, `sample_01` 30 s, exact experimental Reco candidate used for that matrix:

| stride | processed frames | wall time | speedup vs 1 | stitch cost | pan-yaw p90 | rapid-transition p90 |
|---|---:|---:|---:|---:|---:|---:|
| 1 | 1784 | 173.571 s | 1.00x | ~$0.03568 | baseline | baseline |
| 2 | 892 | 88.268 s | 1.97x | ~$0.01814 | ~0.92° | ~1.01° |
| 3 | 595 | 59.987 s | **2.89x** | **~$0.01233** | **~1.67°** | **~1.43°** |
| 4 | 446 | 46.014 s | 3.77x | ~$0.00946 | ~1.53° | ~1.94° |

- Stride 3 cut the experimental stitch wall time from ~173.6 s to ~60.0 s and stitch compute cost by ~65% while keeping the panner trajectory close enough for parameter iteration.
- Stride 4 was faster but had materially worse quality tails: ball angular divergence p95 was ~0.316 rad at stride 4 vs ~0.055 rad at stride 3, and maximum pan-yaw divergence was ~3.45° vs ~2.40°.
- Visual spot checks at ordinary and rapid-transition epochs showed stride 1/2/3 closely aligned; stride 4 had the largest framing deviations.
- The diagnostic harness proved the same real-time source interval was represented at every stride; the later production implementation improved on this experiment by rendering every source frame and interpolating between sparse AI anchors.

### Final full-rate production hardware evidence

Same-sample RTX 4090 / EU-RO-1 / YOLO26m acceptance evidence for final Reco `b2fc622...`:

- stride 1: **1,784 rendered output frames**, ~183.535 s wall.
- stride 3: **1,784 rendered output frames at normal 60000/1001 cadence**, ~67.064 s wall.
- Final production speedup: **~2.737x**, or ~63.46% lower stitch wall time.
- Approximate stitch compute at `$0.74/hr`: **~$0.03773 -> ~$0.01379**.
- Logs confirmed CUDA/NVDEC/ORT-CUDA/shared-buffer zero-copy remained active and there was no CPU pixel fallback or sparse-output-frame regression.
- The first full-rate acceptance harness completed both renders but failed in post-render measurement, so it did not retain the final video artifact. A retained full-rate stride-3 artifact is still useful on the next healthy allocation for final visual evidence, but this is **not a blocker** and there is no known stride-3 Reco crash/correctness failure.

### Standard testing policy from now on

- **FAST TEST: stride 3** — default for routine OEV tracker/panner/parameter iteration and standard RunPod sample tests.
- **FULL VALIDATION: stride 1** — use before final production/merge decisions, exact-behaviour comparisons, and final motion/encode-quality sign-off.
- **Stride 2** — optional cautious fast mode for especially transition-sensitive investigations.
- **Stride 4** — coarse smoke/directional checks only; do not use it as the default quality-testing mode.

### PR #5 / CLA / licensing note

- `JhnsonO/video-stitcher#5` is displayed by GitHub as merged because the fork's `main` ref was fast-forwarded directly to the PR's exact head commit `b2fc622...`; the PR merge button/action was not used.
- **No CLA Assistant signature comment was posted.** Do not sign or acknowledge contributor/legal terms on the owner's behalf through automation.
- The upstream `reco-project/video-stitcher` CONTRIBUTING text states that opening/submitting a PR grants the maintainer a perpetual, worldwide, royalty-free, irrevocable, non-exclusive licence to use/sublicense/relicense the contribution, including proprietary relicensing. Because that text says the agreement is tied to opening/submitting a PR rather than merely the bot signature, do **not** assume the missing bot signature proves there was no legal effect. PR #5 was fork-local, so applicability is not resolved here.
- Upstream Reco is AGPL-3.0 and its contributor terms contemplate dual commercial licensing. Before OEV is sold/customer-facing, perform a deliberate licensing/architecture review (and obtain qualified legal advice or a commercial licence if appropriate) rather than signing anything casually. This is a commercial/legal follow-up, **not a technical blocker for current testing**.

### Ticket closure / next engineering focus

- Frame-stride implementation, benchmark, production enablement and orchestration rollout are complete.
- No additional paid RunPod benchmark is required to choose the default; stride 3 has sufficient evidence over stride 4.
- Keep the retained full-rate stride-3 visual artifact as a low-priority evidence task when healthy capacity is naturally available.
- The outstanding product-quality priority remains the previously quantified panner lag / ball-out-of-frame problem (`cluster_alpha` / EMA / lookahead tuning). Future routine tuning runs should now benefit from the stride-3 fast-test path.

## Ball detection/reacquisition — native-crop recovery A/B modest; ROI/state bug confirmed; tiled reacquisition next (16 Aug 2026)

**Status / decision:** diagnosis complete; next architecture is not yet implemented in this entry. `JhnsonO/video-stitcher#6` remains draft and **must not merge as-is**. Current inspected state: `ffa-automations/main` was `347a3f83ac4a1b281bed2644488d22e1fca13df9` before this handoff update; `video-stitcher/main` is `b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085`; PR #6 head is `72d0ac1709a09f84a8e32ca7e4b9c792159edc50` on `agent/high-res-ball-roi-recovery`, based directly on that Reco main SHA. A fresh implementation chat independently inspected the same code and agreed with the architecture below. Its observation that the old FFA recovery experiment branch is 67 ahead / 32 behind current main means it should not be extended; use a fresh experiment branch.

**Experiment/control:** same `sample_02` 180s footage, stride/detection interval 1, YOLO26m, lookahead 1.5, same unrelated settings. Control = run `31939820386`, branch `experiment/sample02-v4-stride1-control`, SHA `6bc0857208b3d82a4f764a4f9658e42fdfc2057e`, artifact id `9262033662`. Recovery = run `31962717862`, branch `experiment/sample02-high-res-ball-recovery-01`, SHA `1d35010997452ac377e275586fed45fb0ba3ae95`, job `95203079378`, artifact `oev-sample-baseline-sample_02-180s-31962717862` / id `9268349547`. Recovery used predicted native-resolution crops with ratios `[0.5, 0.6666667, 0.8333333]`, `MAX_RECOVERY_MISSES=24`, source 3840×2160, existing 1920×1920 model input. The produced recovery `followcam.mp4` (~409MB) lacked a valid `moov` atom, so this A/B is event/log evidence rather than a valid visual comparison.

**Recovery telemetry:** attempts `[3408,869,651]`; hits `[2539,218,67]`; exhausted `584`; errors `0`; total `BALL_RECOVERY_HIT=2824`. Right `2214`, Left `610`; R1/L1 `2029/510`, R2/L2 `139/79`, R3/L3 `46/21`. Confidence mean/median fell with width: stage 1 ~`0.301/0.286`, stage 2 `0.208/0.170`, stage 3 `0.175/0.149`.

| metric | stride-1 control | native-crop recovery |
|---|---:|---:|
| raw frames with no ball | 4,892 | 4,617 |
| longest raw loss | 182 | 139 |
| world-state `None` | 1,451 | 1,182 |
| longest world-state loss | 158 | 116 |
| safe-zone violations | 3,085 | **3,179 (worse)** |

Both runs have 10,775 frames. Raw detections `8073→8507` (+434). There were 275 frames where control had no raw ball and recovery did; no frames moved opposite. World state improved on 301 frames and worsened on 32. But real tracking-state gain was small: `Tracking 3567→3666` (+99) while `Coasting 5757→5927` (+170). Of the 275 newly filled raw frames, only 55 became `Tracking`; 220 stayed `Coasting`. ~117 frames gained extra candidate(s) even though control already had a ball, adding duplicate/cross-camera confusion risk. Other acceptance signals: outside-frame tracking/coasting `971→937` slightly better, safe-zone violations `3085→3179` worse, longest outside streak `112→113`, guard activations `4368→4526`, ball-signal holds `1734→1857`, switches `29→28`. **Interpretation: most of the apparent gain is stale/uncertain holding, not genuine reacquisition.**

**Long-loss evidence:** major control losses included `10364–10545=182`, `9234–9380=147`, `9828–9959=132`, `773–896=124`, `565–672=108`, `470–563=94`. Recovery still had `9234–9372=139`, `9828–9959=132` identical, `773–896=124` identical, `575–672=98`, `470–563=94` identical, and split the 10.4k blackout into `10364–10446=83` + `10452–10545=94` rather than cleanly solving it. Example around frame 10447: weak Right recovery candidate conf ~0.159/yaw -0.302 while world remained stale Left `Coasting` around yaw +1.225/age ~142; frame 10451 Right ~0.160/yaw -0.601 while world was still stale Left. Internal recovery “hit” therefore does not mean the tracker reacquired the ball.

**Confirmed recovery-state/ROI ordering bug:** in `crates/reco-detect/src/detectors/ort_gpu.rs`, `CameraRecoveryState::observe()` updates velocity/center and resets misses. Any crop ball candidate currently increments the recovery hit, calls `observe()`, resets history, stops wider recovery and extends detections immediately. In `crates/reco-autocam/src/lib.rs`, high-res recovery is configured on the GPU detector before the detector is wrapped by field ROI. Therefore an out-of-field/spare-ball candidate can mutate trusted recovery state and terminate widening **before** the outer ROI rejects that same candidate. This is recovery-history poisoning; the field polygon itself is not the root bug.

**Confirmed dead-end:** local predicted crop recovery only runs while `misses < 24`; at stride 1/~60fps that is only ~0.4s. At 24 misses, predicted center becomes `None` and recovery stops until ordinary full-frame YOLO eventually sees the ball again. Real `sample_02` gaps last ~1–3s, so this design cannot solve the long blackout tails.

**Airborne/bouncing-ball ROI rule:** do **not** treat field ROI as a hard truth and do not simply enlarge it. A ball can project outside visible grass while physically above the pitch. Inside ROI = normal candidate. Outside ROI with no credible recent trajectory = reject/very low credibility. Outside ROI that connects convincingly to the recently trusted ball = keep briefly as **provisional**. Score with recent confirmed position, predicted position, velocity/direction, distance, temporal continuity, plausible acceleration/motion, whether it returns toward/into field, confidence, and competing candidates. Stationary spare balls should lose credibility; airborne match balls should remain associated with their prior track. A provisional candidate must **not** mutate trusted recovery state. Only sufficient validation may reset misses, update `last_center`/velocity, end the recovery search or influence the trusted main ball state.

**Tracker/cross-camera:** field tracker already raises max jump to ~0.8 rad and resets `last` on full loss. Do not loosen tracker gates just to improve metrics; current candidates are dirty. Player-anchor may theoretically reject an airborne ball far from players, but that is not proven as the current main bottleneck. Recovery state is per-camera (`[CameraRecoveryState;2]`) and cross-camera arbitration happens too late; a weak recovery candidate on one camera must not contaminate state while the other camera has a stronger credible ball.

**Agreed next architecture — key invariant: `candidate != trusted ball`.**
1. Split candidate generation from trusted state mutation. Detector may produce normal/local/tiled candidates; box found ≠ state commit.
2. Validate in a context-rich layer using ROI + recent trusted trajectory + confidence/motion + both cameras. Do not bury the field polygon inside the CUDA detector just to fix ordering.
3. Outside-ROI candidates are provisional only when trajectory-consistent; stationary/implausible ones die.
4. Cross-camera arbitration occurs before trusted commit.
5. Recovery is a state progression, not a dead end: `normal → predicted native crop → global tiled search → trusted reacquisition → normal`. Prediction expiry changes search mode; it must not abandon recovery.
6. Keep existing native predicted crops for short losses after fixing validation/state ordering.
7. After local recovery fails/expires, add fallback **native-resolution tiled global reacquisition**. Do not resize whole 3840×2160 into the same 1920 model; that preserves no meaningful extra ball detail. Start with ~2×2 overlapping native regions (derive exact geometry/aspect handling from preprocessing), feed each tile into existing detector, remap detections to native/camera coordinates, then run the same validator. Overlap protects boundaries. Fallback only, not continuous 4K inference; throttle later only if benchmark data requires.
8. Instrument candidate generated → provisional/rejected/accepted → trusted commit → actual transition back to `Tracking`, plus local/tiled attempts, tile/camera/confidence, cross-camera conflicts, reacquisition latency, and tiled inference time. `BALL_RECOVERY_HIT` is not success by itself.
9. Use a fresh FFA experiment branch from current main and a fresh stride-1 control from the same current harness; historical `31939820386` remains reference.
10. Re-test `sample_02` 180s, stride 1. Compare raw missing/longest loss, `Tracking`, `Coasting`, `None`/longest world loss, genuine lost→Tracking reacquisition count/latency, safe-zone violations, cross-camera conflicts/switches and wall-clock overhead. Success = materially shorter blackouts + more genuine `Tracking` without material false-tracking/safe-zone regression.

**If tiled native-resolution reacquisition still fails materially:** stop tracker tuning and move to a dedicated football detector/fine-tune on OEV hard cases (tiny ball, blur, occlusion, airborne, near feet, touchline, goal/net). YOLO+VLM remains a later exception-path concept, not the first next fix.

**Immediate handoff:** fresh implementation chat should establish `candidate != trusted ball` before adding stronger tiled search, keep work opt-in/draft, run a same-harness fresh control/treatment A/B, append exact implementation SHAs/branches/PR/run IDs/results to this state file, and **do not merge PR #6 as-is**.
