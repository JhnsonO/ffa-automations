## OEV Test Runtime v1 — Benchmark Pack Prep CLOSED green; Benchmark run IN FLIGHT; tiered minDownloadMbps + stale-comment fix merged, not yet dispatched (13 Aug 2026)

**Secrets-in-layers work is fully closed (confirmed this session, not reopened):** merge `4e7a9140e9894822b3d8fc81c4272831400b0b22`, standalone scan run `31685815975` green across all 47 layers, dir-mode Gitleaks scan merged.

**`oev-benchmark-pack-prep.yml` — CLOSED, green.** Two real bugs found and fixed via verified diffs:
1. `516b552` — runner never installed `ffmpeg`/`ffprobe` (only `aria2` for the download step); extract/verify steps hit `command not found`. Fixed by adding `ffmpeg` to the existing apt-get line.
2. `32c272a` — equivalence check compared `ffprobe -show_entries format=duration` (container duration, includes AAC audio-tail overhang past the video's `-c copy` cut) against a reference number that was actually read off ffmpeg's own encode-progress line (video-stream duration), not ffprobe. Different metrics, always would have mismatched. Fixed by switching the check to `-select_streams v:0 -show_entries stream=duration` so it measures the same thing the reference value represents. Root-caused by pulling `segment.log` from the actual reference run (`31608010277`'s artifact) and confirming its `18.99s` came from ffmpeg's `time=` line, not ffprobe.
- Confirmed green: run `31691178053`, all 8 steps passed, benchmark pack uploaded to `Benchmarks/oev-test-runtime-v1/` in Drive.
- **3 fix/redispatch-cycle budget for the "close the benchmark" ticket was spent across these two fixes plus the network-timeout fix below (3 total). Do not treat pack-prep as needing further cycles — it's done.**

**`oev-test-runtime-benchmark.yml` — IN PROGRESS, real infra finding + two improvements merged, not yet redispatched:**
- **Image size confirmed via GHCR manifest query:** `ghcr.io/jhnsono/oev-test-runtime:v1-reco-53fe10f5` is 47 layers, `19,662,285,233` bytes (~19.7GB) compressed. This is why pulls are slow — not a broken pipeline, a genuinely large image.
- Run `31692246338` (before the network-timeout fix): failed — all 3 internal pod-allocation attempts hit `wait_for_network`'s old `300s` (5min) ceiling while still mid-image-pull. Root-caused via job log (`pod ... never reported a public IP + SSH mapping within 300s`) cross-referenced against user-supplied RunPod console screenshots showing `"still fetching image"` for the entire window — confirmed same event, not a guess.
- **Fix `a94d651`:** raised `wait_for_network`'s default `timeout_s` 300→1200 and job `timeout-minutes` 60→90 to give the large pull a real chance to complete and to actually measure download time (a stated deliverable) rather than truncating it.
- Redispatched as run `31694920927` (started `2026-08-13T11:18:32Z`) using the fixed timeout. **Still in progress as of this entry.** User-supplied logs show attempt 1 timed out at ~1200s as expected, attempt 2 began ~`11:38:51Z`, also slow-pulling the same image — two consecutive hosts both slow on the same 19.7GB pull, not a one-host fluke.
- **Fix `9cebd1e` (this session, NOT YET dispatched — merged to main only, current run 31694920927 unaffected since GH Actions already snapshotted the script at dispatch time):**
  1. **Tiered `minDownloadMbps` fallback**, mirroring the same start-strict-then-relax pattern the GPU fallback list uses: attempt 1 requires ≥800Mbps, attempt 2 ≥400Mbps, attempt 3 no floor (take whatever's available rather than striking out entirely). Confirmed via RunPod's official REST v1 docs that `minDownloadMbps` is a real, supported `POST /pods` field. Implemented as `MIN_DOWNLOAD_MBPS_BY_ATTEMPT = [800, 400, None]` indexed by attempt number, passed into `create_pod()`.
  2. **Corrected a stale/false code comment.** The workflow claimed the multi-GPU-fallback-list + `gpuTypePriority:'custom'` request shape was a "confirmed NVDEC-breaking" pattern, and used that as the reason this workflow only requests a single RTX 4090. That theory was falsified by run `31590998085` (same single-type request, NVDEC still failed) — the real, later-confirmed pattern is **per-host/per-driver**: certain RunPod hosts on driver `570.158.01` have NVDEC broken at the hardware/driver level regardless of request shape. `oev-runpod-followcam.yml` already restored its 4-GPU fallback list on this corrected understanding; `oev-test-runtime-benchmark.yml` never did. Comment corrected to reflect this; **the single-GPU behavior itself was deliberately left unchanged** (restoring the fallback list here is flagged as a legitimate separate future improvement, not bundled into this fix).

**Not yet done, next chat/session:**
1. Check run `31694920927` completion (`DISPATCHED — UNVERIFIED` as of this entry). If green: pull `timing.json`, confirm no build/export/apt-get occurred, confirm calibration/tracking/detection/pan acceptance checks passed, compare against ~40min/44GB old baseline, produce ADOPT/DO-NOT-ADOPT verdict.
2. If red (e.g. exhausts all 3 internal pod-allocation attempts due to slow hosts): the tiered-`minDownloadMbps` fix (`9cebd1e`) is already merged and ready — redispatch with it. This would be a fresh fix-cycle budget (the prior 3-cycle budget was for the pack-prep + timeout fixes, already spent).
3. Optional, explicitly not yet decided: whether to restore the 4-GPU fallback list (4090/A5000/3090/L4 + `gpuTypePriority:'custom'`) on this benchmark workflow to match `oev-runpod-followcam.yml`'s corrected, current understanding of the NVDEC issue. Not required for this ticket — flagged for awareness only.

## OEV Test Runtime v1 — IMAGE PUBLISHED, verification step needs a fix (12 Aug 2026, run `31619934141`)

**Cycle 3 (run `31619934141`): image build + GHCR push SUCCEEDED.** Confirmed real artifact:
- `image_ref`: `ghcr.io/jhnsono/oev-test-runtime:v1-reco-53fe10f5`
- `digest`: `sha256:98cd20a52c92f69329e25fac96a61b8a735475b11b4877b82a03c1b28b29fb76`

**Run still shows overall `failure`** because the *separate* "No-secrets-in-layers check" step failed downstream of the successful push -- root cause confirmed from log: it is NOT a detected secret. The step's `docker pull "$IMAGE_REF"` (re-downloading the image on the same runner, right after building it, to inspect `docker history`) ran the `ubuntu-latest` runner out of disk: `failed to register layer: ... no space left on device`, `Free space left: 0 MB`. Grep for secret patterns never executed. This is a resource-sizing bug in the verification step, not evidence of a leaked credential.

**Needed fix (not yet made -- next chat):** the build-and-push step already has the image loaded locally in the same buildx session; re-pulling it from GHCR afterward is redundant and doubles peak disk usage on a runner that's already tight after a CUDA-toolkit + Rust + PyTorch-base image build. Options: (a) run `docker history` against the buildx-produced local image directly instead of re-pulling, (b) add a `docker builder prune`/`docker system prune -f` step between build and the secrets check, (c) skip the local-image secrets check entirely and instead inspect via `crane` / GHCR manifest+config API (no full layer pull needed for `docker history`-equivalent info). (a) is simplest and cheapest -- prefer that first.

**Debug budget note:** this finding came from continuing to poll/diagnose an already-succeeded build, not from a 4th fix-and-redispatch cycle -- no new dispatch was made this turn. The 3-cycle build budget from this session is still considered used; the fix above should be scoped as its own small, separate dispatch (arguably cycle 1 of a fresh budget, since the actual image-build logic is now proven correct).

**Not yet done, next chat/session:**
1. Fix the no-secrets-check step (see options above), push, verify diff, merge, redispatch build workflow -- should be fast since Docker layer caching means only that step re-runs in substance (full rebuild will still occur since `cache-from: type=gha` was not proven to hit across these cycles -- confirm/ignore, low stakes either way).
2. Once a run goes fully green: dispatch `oev-benchmark-pack-prep.yml` (CPU-only) -> dispatch `oev-test-runtime-benchmark.yml` with `image_ref=ghcr.io/jhnsono/oev-test-runtime:v1-reco-53fe10f5` -> pull `timing.json` -> compare against ~40min/44GB old-path baseline and YOLO26m A/B telemetry (run `31608010277`) -> final deliverable (tag+digest, timing breakdown, adoption verdict).
3. The image itself (build/push logic, Reco pin, YOLO26 export x4, manifest) is proven working as of `9ffd2eb` -- do not re-litigate that logic, only fix the post-push verification step.

## OEV Test Runtime v1 — build cycle 3 dispatched, debug budget exhausted this chat (12 Aug 2026, run `31619934141`, fix `9ffd2eb`)

**Cycle 2 (run `31618729675`) FAILED, root-caused, NOT a GPU/build-environment issue:** YOLO export itself succeeded (`yolo26s.onnx` written, 37.8MB, confirmed in log). The failure was the shape-verification line using the system `python3` instead of `/opt/oev-runtime/yolo-venv/bin/python3` -- `onnx` package is only installed in the venv, so `ModuleNotFoundError: No module named 'onnx'`. One-line fix, diff verified (Dockerfile: 1 line changed), merged `9ffd2eb`.

**Cycle 3 dispatched:** run `31619934141` -- https://github.com/JhnsonO/ffa-automations/actions/runs/31619934141. 2 polls used, still `in_progress` past 9+ minutes with no failure surfaced (further than either prior cycle got before failing). NOT YET VERIFIED COMPLETE.

**Debug budget exhausted for this ticket this chat (3 dispatch cycles used: run `31617406326` fail, `31618729675` fail, `31619934141` pending).** Per the debug-budget rule, do NOT attempt a cycle-4 fix in this chat even if `31619934141` also fails -- check the run result in a fresh chat/message and diagnose from there.

**Not yet done, next chat/session:**
1. Check run `31619934141` completion first action. If green: pull tag/digest from "Record digest"/"Smoke-check manifest" step logs, confirm no-secrets-check passed -- proceed to benchmark-pack-prep + isolated RunPod benchmark.
2. If red: fresh diagnose/fix/dispatch cycle (new budget), starting from the exact failing step's log -- do not re-guess, pull the actual error window via the Actions API before editing the Dockerfile again.
3. Both prior failures (cycle 1: wrong export output path, cycle 2: wrong python interpreter) were self-inflicted Dockerfile authoring mistakes, not environment/GPU/architecture problems -- the underlying design (CPU-only build runner, baked reco+models, GHCR publish) has not been challenged by any failure so far.

## OEV Test Runtime v1 — build cycle 2 in progress, cycle 1 root-caused (12 Aug 2026, run `31618729675`, fix `8f1aeda`)

**Cycle 1 (run `31617406326`) FAILED, root-caused, NOT a GPU/build-environment issue:** `cargo build --release -p reco-cli --features cuda` completed successfully on the standard `ubuntu-latest` runner in 5m01s with zero device-related errors -- this is now mechanical confirmation that the CUDA-feature reco build does NOT require a physical GPU to compile (Correction 1 from the audit holds). The actual failure was downstream and self-inflicted: the Dockerfile's YOLO26 export loop passed `project=.../ name=...` to `yolo export`, then tried to `mv` the output from a `<name>/<model>.onnx` subdirectory that `yolo export` never creates -- `export` (unlike `train`) writes the `.onnx` directly into the working directory (confirmed in the log: `Results saved to /opt/oev-runtime/models/yolo26s.onnx`). Fixed by removing `project=`/`name=`/`mv`/`rm -rf` and just running `yolo export model=... format=onnx imgsz=1920` from within `/opt/oev-runtime/models` (matches the exact pattern already proven working in the AB-test run `31608010277`'s `segment.log`).

**Fix pushed to `feat/oev-test-runtime-v1`, diff verified (Dockerfile: +2/-4, only file changed), merged to `main` (`8f1aeda`).**

**Cycle 2 dispatched:** run `31618729675` -- https://github.com/JhnsonO/ffa-automations/actions/runs/31618729675. 2 polls used this session (~9.5min elapsed total), still `in_progress` at "Build and push" -- past the point cycle 1 failed at, no failure evidence yet. NOT YET VERIFIED COMPLETE. This is dispatch/debug cycle 2 of 3 for this ticket.

**Not yet done, next check:**
1. Check run `31618729675` completion. If green: pull tag/digest from "Record digest" + "Smoke-check manifest" step logs, confirm no-secrets-check passed.
2. If red: cycle 3 of 3 -- diagnose from the run log (`gh.sh logs` / job log) before redispatching; if that also fails, stop and hand off per debug budget.
3. Then: dispatch `oev-benchmark-pack-prep.yml` (CPU-only) -> dispatch `oev-test-runtime-benchmark.yml` with the confirmed image ref -> pull `timing.json` -> compare against ~40min/44GB old-path baseline and YOLO26m A/B telemetry (run `31608010277`) -> final deliverable (tag+digest, timing breakdown, adoption verdict).

## OEV Test Runtime v1 — build DISPATCHED, in progress (12 Aug 2026, run `31617406326`, merge `6982b52`)

**Goal:** versioned GHCR image baking pinned reco-cli + pre-exported YOLO26 s/m/l/x@1920 so experiments skip the ~40min bootstrap/44GB-download tax. CPU-detector-only by design (see GPU-detector finding below).

**GPU-detector finding (audited before build, no code change made):** `reco-autocam/src/lib.rs::setup_autocam` gates `OrtGpuDetector` selection on a single `use_zero_copy = source_is_gpu_resident` boolean. Production runs `--no-zero-copy` (required, unresolved NV12->RGB chroma-plane corruption bug), which deterministically routes to `CpuYoloDetector` at the `!use_zero_copy` branch -- not a missing invocation, a direct architectural consequence. Fixing this is a separate future Reco ticket; explicitly out of scope here, `--no-zero-copy` untouched.

**Built, merged to `main` via `feat/oev-test-runtime-v1` (diff verified before merge: 5 files added, 0 modified, 0 behind main):**
- `Dockerfile` -- based on `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404`; apt/rust/cudart install block copied from `runpod_bootstrap.sh` sections 1-4; builds reco-cli at pinned SHA `53fe10f548d5767ad94ef66aeaedf2d8c7161f27` with `--features cuda`; exports YOLO26 s/m/l/x @1920 (no `nms=True`, matches confirmed `(1,300,6)` layout); bakes `manifest.json` (reco SHA, ultralytics/onnxruntime versions, model sha256s, build timestamp). Tag `v1-reco-53fe10f5`, never `latest`.
- `.github/workflows/oev-test-runtime-build.yml` -- builds/publishes to `ghcr.io/jhnsono/oev-test-runtime` on a standard (non-GPU) `ubuntu-latest` runner, per the corrected assumption that CUDA compilation does not require a live GPU device (ort-sys uses a prebuilt ONNX Runtime binary; nvcc targets a compute-capability list, not a device). Includes a `docker history --no-trunc` secret-pattern check and a manifest/model-checksum/reco-version smoke check.
- `.github/workflows/oev-benchmark-pack-prep.yml` -- one-time, CPU-only (ffmpeg stream-copy, no GPU): extracts the pinned 19s segment using the exact command form from `runpod_followcam_remote.sh` (`ffmpeg -ss 2274 -i <src> -t 19 -c copy`) against the same `GX010197.MP4`/`GX010173.MP4` sources, verifies output duration/size against reference values pulled from the successful A/B run `31608010277`'s `segment.log` (right.mp4 ~18.99s / ~143554kB), then uploads to a new `Benchmarks/oev-test-runtime-v1/` Drive subfolder (reuses existing OAuth/upload/verify pattern, does not invent new storage).
- `.github/workflows/oev-test-runtime-benchmark.yml` -- isolated RunPod dispatch using the new image (`imageName` pointed at the GHCR tag instead of stock `runpod/pytorch`); launch/preflight-retry and terminate steps copied verbatim (mechanics unchanged) from `oev-runpod-followcam.yml`, with `gpuTypeIds` narrowed to single `NVIDIA GeForce RTX 4090` (no `gpuTypePriority`) per the already-documented NVDEC-breaking request-shape finding. Downloads the small benchmark pack (not the 44GB full source) from Drive. Produces `timing.json` with the full wall-clock breakdown requested (pod-requested to network to SSH/preflight, image-acceptance-check, benchmark-download, calibrate, render, total). Image acceptance checks (nvidia-smi, manifest, model checksums, reco --version, onnxruntime providers) run before any render.
- `oev_test_runtime_benchmark_remote.sh` (new, not frozen) -- runs inside the baked-image pod: calibrate (same lens profile URL) + same St Margaret's field_roi injection + stitch with `--model yolo26m.onnx --tracking field --panner-preset broadcast --lookahead 1.5 --detection-interval 1 --no-zero-copy --width 1920 --height 1080`, same acceptance checks as production (tracking-enabled line, >=1 detection, pan-yaw spread) PLUS a mechanical grep across its own logs confirming zero `cargo build`/`yolo export`/`apt-get install` occurred.

**Frozen files: untouched.** `oev-runpod-followcam.yml`, `runpod_bootstrap.sh`, `runpod_gpu_preflight.sh`, `runpod_followcam_remote.sh` -- confirmed via diff, 0 modifications.

**Dispatch status:** build workflow dispatched (HTTP 204) as run `31617406326` on `main`. 2 polls used this session, both showed `in_progress` at the "Build and push" step (apt+rustup+cargo build+YOLO26 x4 export -- genuinely long on a standard runner, no evidence of failure). NOT YET VERIFIED COMPLETE.

**Not yet done, next chat/session:**
1. Check run `31617406326` completion -- https://github.com/JhnsonO/ffa-automations/actions/runs/31617406326. If green: confirm published tag + digest from the run log (Record digest / Smoke-check steps), confirm no-secrets-check passed.
2. If red: this is dispatch/debug cycle 1 of 3 for this specific ticket (fresh budget, separate from the now-closed A/B-test session budget) -- diagnose from the run log before re-dispatching.
3. Dispatch `.github/workflows/oev-benchmark-pack-prep.yml` (CPU-only, cheap) to produce and verify the Drive benchmark pack.
4. Dispatch `.github/workflows/oev-test-runtime-benchmark.yml` with the confirmed image ref, pull `timing.json`, compare against the ~40min/44GB old-path baseline and the YOLO26m A/B telemetry from run `31608010277`.
5. Produce final deliverable: image tag+digest, old/new wall-clock breakdown, adoption verdict.

## AB TEST — yolo26m vs yolov8n follow-cam comparison, isolated path (12 Aug 2026, run `31607161708` DISPATCHED — UNVERIFIED, branch `experiment/yolo26-ab-test`)

**Goal:** does YOLO26m materially improve ball-detection/follow-cam quality over YOLOv8n at the same 1920 input, all else equal. One-shot experiment, not a production change.

**Model verification (done before dispatch, not guessed):** `yolo26m.pt` confirmed current (Ultralytics, Sep 2025). Baseline's `yolov8n.onnx` is exported with `nms=True`, giving `(1, 300, 6)` xyxy+conf+cls (confirmed from baseline run `31596442940` segment.log, not assumed). YOLO26's **default** export head (one-to-one, NMS-free) also outputs `(1, 300, 6)` in the same layout -- so `yolo export model=yolo26m.pt format=onnx imgsz=1920` (no `nms=True`/`end2end=False` needed) is format-compatible with Reco's `CpuYoloDetector` with zero decode changes.

**Segment pinned (not random):** start=2274s, duration=19s, `GX010197.MP4`/`GX010173.MP4`, matches baseline run `31596442940` exactly (source: that run's `segment.log`).

**Baseline telemetry reconciled and locked in** (from run `31596442940`'s `events.jsonl`, definitions below -- use identically for the candidate run, do not redefine per-run):
- "Locked" = ball world_state in `{Tracking, Coasting}` (Coasting is active prediction-hold during a missed frame, not a loss -- Lost/no-ball-frame is the only true break). Earlier session mistakenly required unbroken raw `Tracking` only, which structurally can never sustain past ~9 frames given Coasting flicker -- that was the bug, now fixed.
- "Sustained lock" = first locked run &ge;1.0s. Baseline: **first sustained lock at frame 511 = 8.53s, duration 1.18s** (matches Johnson's independent check).
- First raw ball detection: frame 236 = 3.94s.
- Total raw ball detections: 142; frames with &ge;1 ball detection: 119/1136 = 10.5%.
- Detector gaps: 28, longest 3.94s, total gap time 16.97s.
- Ball state acquisitions (transitions into Tracking): 19; losses (out of Tracking): 27.
- Longest non-Tracking run: 3.94s (frames 0-235, i.e. before first detection).
- Player detections: 17,244 raw, 100% frame coverage (1136/1136).
- Processing: 274s, RTX 4090, $0.0563. Detection runs on **CPU** (`reco_detect::detectors::cpu`), not GPU -- GPU is NVDEC/encode only. YOLO26m (medium) vs YOLOv8n (nano) on CPU inference is expected to be meaningfully slower; not yet measured.

**Isolated test path (frozen production files untouched):**
- `runpod_followcam_remote_ab_yolo26.sh` -- branch `experiment/yolo26-ab-test` ONLY, never `main`. Copy of frozen `runpod_followcam_remote.sh` with exactly 2 functional diffs: (1) segment pinned to 2274/19 instead of `$RANDOM`, (2) model `yolov8n.pt`/`yolov8n.onnx` &rarr; `yolo26m.pt`/`yolo26m.onnx` (export flags adjusted: drop `nms=True`, not equivalent for YOLO26). Tracking/panner/lookahead/detection-interval/resolution/ROI/`--no-zero-copy` byte-identical to production. Diff self-verified before push.
- `.github/workflows/oev-runpod-followcam-ab-yolo26.yml` -- **new file, on `main`** (commit `6aa1671`), required only because GitHub's `workflow_dispatch` API requires the workflow file to exist on the default branch to be dispatchable at all (confirmed: this applies to the Actions UI too, not just the API). Does not modify `oev-runpod-followcam.yml` or any other existing file (diffed and confirmed: pod launch/preflight/retry/terminate steps, lines ~1-450 and ~640-720, untouched verbatim). Its checkout step pulls actual content from whatever ref it's dispatched against, so the real experimental script content still lives only on the branch. Differences from production workflow: points scp/ssh exec at the AB script filename, distinct artifact name `oev-runpod-followcam-AB-YOLO26M-<run_id>`, Drive upload step disabled (`if: false` -- one-off test, don't pollute the shared OEV Drive Followcam/ folder), acceptance gate no longer requires Drive verification.

**Dispatch history this session:** first dispatch attempt returned HTTP 400 (workflow file had just been pushed, GitHub hadn't indexed it yet) but apparently queued anyway; a retry produced a second concurrent run. Both hit RunPod `HTTP 500 "There are no instances currently available"` on all 3 pod-create attempts (transient capacity on the same GPU pool the production workflow already uses -- not a script/workflow bug). Zero pods were created on either failed run (`No pod IDs recorded`), so no double-spend occurred. Single clean re-dispatch: run `31607161708`, queued cleanly, one status check confirms it's past pod launch (`in_progress`) -- not polling further, awaiting completion.

**Dispatch attempts, this session (budget: 3 diagnose/dispatch cycles):**
1. `31607161708` (superseded by duplicate below) / `31607077409` -- both FAILED at pod-create: RunPod `HTTP 500 "There are no instances currently available"` on all 3 attempts, transient capacity on the standard 4090/A5000/3090/L4 pool. Zero pods created, zero cost.
2. `31607161708` (clean redispatch) -- FAILED at bootstrap: preflight PASSED clean (host `203.57.40.217`, US-IL-1, RTX 4090, driver `570.195.03`), then SSH connection timed out ~2min into `runpod_bootstrap.sh` (frozen, unrelated to the AB script -- AB script never ran). Matches the already-documented SSH-drop signature from the earlier RunPod NVDEC diagnosis session. Pod terminated cleanly (HTTP 204 confirmed), no orphan, ~2min uptime cost.
3. `31608010277` -- **DISPATCHED — UNVERIFIED**, in progress, past the bootstrap point where attempt 2 failed. This is dispatch/debug cycle 3 of 3 for this session -- next chat should pick up from here if this also fails.

**Not yet done:** confirm run `31608010277` completes; pull `events.jsonl`/`stitch.log`/`run_metadata.txt`, apply the same locked/sustained-lock definitions above, build the A/B table, visual review of `followcam.mp4`, verdict (clear improvement / marginal / no improvement / regression).

## RunPod NVDEC pod-retry pipeline — GREEN END-TO-END, first full followcam.mp4 produced (12 Aug 2026, run `31596442940`, commit `3cc8fc8`)

**Cycle 3 (fix + redispatch only, per explicit budget correction -- cycle 2 was invalidated by an implementation bug, not a real test):** one-line fix, `.github/workflows/oev-runpod-followcam.yml` -- added `?includeMachine=true` to the pod-status poll URL used by the retry loop. No other change; GPU order/config untouched throughout cycles 2-3, as directed, to keep datacenter steering isolated as the only variable under test.

**Run `31596442940`: ALL 14 steps green, first time the full RunPod pipeline has gone end-to-end.**
- Attempt 1/3: pod `q8hitfxww7xt10`, host `69.145.85.73`, machine `fwz8suyls228` (machineId field-path fix from the prior cycle now confirmed working -- real value, not `unknown`), driver `570.195.03`, GPU `NVIDIA GeForce RTX 4090` -- **NVDEC PASS on the first attempt.** Datacenter-exclusion steering was never exercised (no failure occurred to trigger it) -- the retry infrastructure is proven, but the steering logic itself remains functionally unverified since no run has yet needed it to actually escape a bad host mid-run.
- **Known gap, non-blocking:** `data_center_id` is still `unknown` even with `includeMachine=true` added -- the fix resolved `machineId` but not `machine.dataCenterId` for this response. Not investigated further since it didn't block this run; would need investigation before the exclusion-by-datacenter logic could be trusted to actually fire in a future failing run.
- Bootstrap, Reco-SHA gate (`53fe10f548d5767ad94ef66aeaedf2d8c7161f27`), Drive download, and the real production follow-cam script all passed. Confirmed from `stitch.log`: `Autocam: tracking enabled` with the real `yolov8n.onnx` model (distinct from bootstrap's own internal smoke-test line earlier in the log, which uses a separate smoke model and is expected/unrelated).
- Drive upload verified: `followcam.mp4` -> Drive file `1lEhQrZPqemQLTgIWgW4rxrb8PXHaCCP_` (42,668,782 bytes, size-matched), `events.jsonl` -> Drive file `1K-yjQVMp_gmkRy1UmaQQpGuUIH0YM9rh`. Both in the OEV Drive `Followcam/` folder.
- Pod `q8hitfxww7xt10` terminated cleanly (Terminate step: success).

**Not yet done -- the actual next step, no more infrastructure work needed first:** Johnson visually reviews the produced `followcam.mp4` (Drive file `1lEhQrZPqemQLTgIWgW4rxrb8PXHaCCP_`, or the run's GitHub artifact `oev-runpod-followcam-31596442940`) against the standard product question -- does it acquire the ball and produce good camerawork on this segment. A green CI run proves execution only, not product acceptance (standing rule, restated deliberately -- see the earlier corrected entry above where a green run masked a corrupted-output bug).

**Retry-loop infrastructure status:** built and merged over 3 sessions/cycles (`1788f9d` retry loop, `812f61c` datacenter steering, `3cc8fc8` includeMachine fix). Mechanically proven for the happy path (clean 3-attempt failure handling in the two earlier runs, clean 1-attempt success here, no orphaned pods across any of the 3 dispatches).

**Active risk, not closed:** the datacenter-exclusion fallback is unverified and, as currently observed, non-functional -- `data_center_id` reads `unknown` even with `includeMachine=true` added, which means if a future run fails NVDEC on attempt 1, the exclusion set stays empty and attempt 2 will retry with no steering applied at all (identical behavior to the plain retry loop from cycle 1, which re-hit the same bad host 3/3 times). Do not treat this as done. Re-open only if a future run actually fails attempt 1: check whether `data_center_id` populates that time, and only debug the fallback then -- no further infra work now.

## RunPod NVDEC pod-recreate retry loop — MERGED + DISPATCHED, execution verified, product goal NOT achieved: RunPod re-allocated the SAME bad host all 3 attempts (12 Aug 2026, run `31593541439`, commit `1788f9d`)

**Built (Claude direct build, explicit routing override "You do it"):** `.github/workflows/oev-runpod-followcam.yml` — "Launch RunPod GPU pod" step now loops up to 3 pod-allocation attempts (create → wait for network → wait for SSH → preflight; NVDEC fail → delete pod → retry). Only the passing attempt's pod_id/ip/port/gpu_type/cost_per_hr is published downstream. All-3-fail hard-fails the step. "Terminate RunPod pod(s)" now sweeps every pod ID created this run (tracking file written the instant each pod is created), not just the winner. Per-attempt evidence (`pod_selection_summary.txt`, `preflight_attempt_N.txt`: pod_id, host IP, machine_id, GPU, driver version, result) pulled into the run artifact — no blacklist logic added, evidence-only as directed. Merged to `main` via feature branch `fix/runpod-nvdec-preflight-retry` (commit `ff81c13` → merge `1788f9d`). Frozen files untouched (`runpod_gpu_preflight.sh`, `runpod_bootstrap.sh`, `runpod_followcam_remote.sh`).

**Run `31593541439`: retry-loop mechanics fully verified working, but did not solve the underlying problem.** All 3 attempts:
- Attempt 1: pod `744f6igce7y6ny`, host `103.196.86.125`, driver `570.195.03` → NVDEC FAIL, deleted cleanly (attempt-level delete, not final sweep).
- Attempt 2: pod `pzmg7y69kmldfg`, **same host `103.196.86.125`**, same driver → NVDEC FAIL, deleted cleanly.
- Attempt 3: pod `ggq2k1xh29ewkc`, **same host `103.196.86.125`** again, same driver → NVDEC FAIL, deleted cleanly.
- Step correctly hard-failed after 3/3 (`##[error]All 3 pod allocation attempts failed...`). Final "Terminate RunPod pod(s)" sweep step: `success` — confirmed no orphans (all 3 already individually deleted in-loop; sweep is a no-op safety net here, working as designed).

**New finding, decision-relevant:** RunPod's Secure Cloud 4090 allocator handed this account the exact same physical host (`103.196.86.125`) on all 3 consecutive create calls despite deleting the pod between each — this account/region is currently pinned to (or heavily weighted toward) one specific bad box. Delete-and-recreate alone cannot escape a sticky-host allocator; a plain retry loop is not sufficient on its own right now.

**Also new: this host+driver combination (`103.196.86.125`, driver `570.195.03`) fails NVDEC, while the earlier PASS host (`103.196.86.137`, same driver `570.195.03`) passed twice.** This rules out driver-version alone as the signature (two different hosts, identical driver string, opposite NVDEC outcomes) — confirms the problem is genuinely per-physical-host, not per-driver-version. Do not build a driver-version blacklist off this data.

**Gap found, not yet fixed:** `machine_id` field extraction (`field(machine, 'id', 'machineId')`) returned `unknown` on all 3 attempts — the RunPod pod-info payload doesn't expose machine identity under those key names (or the retry loop's `info` isn't the right object). Host IP is the only reliable evidence of physical-host identity right now. Needs a live payload inspection (print the full `machine` dict once) before any host-exclusion feature can be built, since that would need a real, stable host/machine identifier to exclude on.

**Not yet done — real next candidates:**
1. Inspect a live RunPod `GET /pods/{id}` response's full `machine` object to find the actual field name(s) available (if any) for physical host identity, before designing exclusion logic around it.
2. If RunPod's create API supports it, request exclusion of a specific host/machine on the next create call within the same retry loop (needs the field from #1 first).
3. Alternative not requiring new RunPod API surface: on repeated same-host NVDEC failures within one retry loop, deliberately vary `gpuTypeIds`/priority order attempt-to-attempt (e.g. try A5000 first on attempt 2) to see if that steers the allocator to a different host — cheap to test, no new dependency on undocumented fields.
4. Simplest fallback if neither works: widen `MAX_ATTEMPTS` isn't the fix (sticky-host, not random-host, problem) — do not just raise the retry count without addressing host stickiness first.

**Debug cycle 1 of 3 this session (retry-loop cycle) used. Handing decision to Johnson: which of #1-#3 to pursue next.**

## RunPod NVDEC — NVIDIA_DRIVER_CAPABILITIES isolation REJECTED without dispatch (12 Aug 2026, fresh chat, cycle not spent)

**Hypothesis considered:** unset `NVIDIA_DRIVER_CAPABILITIES` (missing `video`) causes the per-host NVDEC failures, fix = explicitly request `compute,utility,video` on pod create.

**Rejected on existing evidence, no new dispatch needed:** all 3 recent `fix/runpod-nvdec-diagnosis` runs (`31587433143` PASS, `31587964973` PASS, `31590998085` FAIL) ran identical code with `NVIDIA_DRIVER_CAPABILITIES` unset in every case (confirmed printed by `runpod_gpu_preflight.sh`, unchanged since added). Unset passed clean on 2/3 hosts (`libnvcuvid.so` present, NVDEC working) and failed only on the host running driver `570.158.01`. Since the capability declaration was constant across both PASS and FAIL runs, it cannot explain a failure that appears on only one host/driver — setting it explicitly would very likely be an inert env-var change, not a real fix. Consistent with RunPod's own docs, which describe them handling GPU passthrough themselves rather than via the standard `nvidia-container-toolkit` env-var gate this variable normally controls.

**Do not re-open this hypothesis** without new evidence that ties capability declaration (not host/driver) to pass/fail outcome.

**Live lead, unchanged:** per-host/per-driver NVDEC failure (see entry below) — `PREFLIGHT_DRIVER_VERSION` logging + watching for a `570.158.01` pattern, or pod-recreate-on-NVDEC-fail mitigation, are the next real candidates.

## RunPod NVDEC — CORRECTION: prior "root cause" was wrong, real pattern is per-host, not request-shape (12 Aug 2026, run `31590998085`)

**The two-entries-ago "root cause confirmed" conclusion (gpuTypeIds-multi + gpuTypePriority=custom breaks NVDEC) is FALSIFIED by this run.** Third dispatch used the exact same single-type config that passed clean twice (`gpuTypeIds: ['NVIDIA GeForce RTX 4090']`, no `gpuTypePriority`) and got `PREFLIGHT_NVDEC=FAIL` / `CUDA_ERROR_NO_DEVICE` — the identical signature from the original 4 multi-type failures. Same request shape, different outcome. The request-shape theory was built on only 2 data points and was wrong; flagging this explicitly so a future session doesn't trust that entry's conclusion.

**Actual pattern across all 3 `fix/runpod-nvdec-diagnosis` dispatches, by host:**
- Run `31587433143`: host `103.196.86.137`, driver `570.195.03` → NVDEC PASS
- Run `31587964973`: host `103.196.86.137` (same host again), driver `570.195.03` → NVDEC PASS (SSH dropped later at bootstrap, unrelated)
- Run `31590998085`: host `47.47.180.35` (different), driver `570.158.01` (different point release) → NVDEC FAIL, `libnvcuvid.so.570.158.01` present and correctly matched to the driver (not a naming/version-mismatch at the file level) -- yet `cuvidGetDecoderCaps` still returns `CUDA_ERROR_NO_DEVICE`.

**Working hypothesis (not yet confirmed):** certain RunPod secure-cloud 4090 hosts -- specifically ones running driver `570.158.01` or similar -- have NVDEC genuinely broken at the host/driver level, independent of container config. This is close to the very first hypothesis from earlier today, now with real per-host evidence instead of price-based inference (the price-correlation theory from the original session was also wrong -- $0.74/hr showed up on both a passing and failing host, it's just the standard secure-cloud 4090 rate, not a marker of a bad pool).

**Not yet done / next candidate steps (untried):**
1. Log `PREFLIGHT_DRIVER_VERSION` as a first-class signal going forward -- already printed, just needs to be watched. If a pattern emerges (all `570.158.01`-driver hosts fail, all `570.195.03` hosts pass), that's a concrete, actionable driver-version block-list.
2. RunPod's pod-create API may support excluding specific machine IDs or requesting a minimum driver version -- not yet researched.
3. Simplest short-term mitigation: on `PREFLIGHT_NVDEC=FAIL`, terminate and retry the pod create once or twice within the same workflow run (cheap -- preflight fails fast, before the expensive bootstrap/build step) rather than failing the whole run and requiring a manual re-dispatch.
4. The SSH-drop issue from the previous entry (same-host, run `31587433143`/`31587964973`) is now a red herring for "systemic" -- it only repeated because both those runs happened to land on the same host by chance, not because of a workflow bug. Downgrade its priority; may not be a real recurring issue at all, watch for it rather than actively fixing.

**Debug budget exhausted this session (3 cycles: 2 confirms + this falsification). Handing off.**

## RunPod SSH-drop confirmed reproducible on same host (12 Aug 2026, run `31587964973`)

**NVDEC fix confirmed 2/2:** second dispatch on `fix/runpod-nvdec-diagnosis` (single-type `['NVIDIA GeForce RTX 4090']`, no `gpuTypePriority`) again passed `PREFLIGHT_RESULT=PASS` clean. The root-cause diagnosis from the previous entry (multi-`gpuTypeIds` + `gpuTypePriority: custom` breaks NVDEC) is now backed by 2 clean single-type passes vs 4 failed multi-type dispatches — confident, not provisional.

**New issue is a SAME-HOST SSH drop, not random flakiness:** both this run and the prior one (`31587433143`) landed on the identical pod IP `103.196.86.137`, SSH reachable through preflight, then unreachable ~2min into bootstrap (`ssh: connect to host 103.196.86.137 ... Connection timed out`, repeated retries, exit 255). Same signature both times, same IP both times. This points at RunPod's Secure Cloud 4090 allocator repeatedly handing this account the same specific box, and that box's networking degrading once the build step puts it under sustained CPU/IO load — not "any random host is occasionally bad." Cleanup/termination confirmed successful both times, no leaked pods, no cost overrun risk.

**Not yet done:** no fix attempted for the SSH-drop issue itself — 2 debug cycles this session both confirmed NVDEC, neither got past bootstrap to test the rest of the pipeline. Candidate next steps (untried): (a) explicitly exclude/avoid that host if RunPod's API supports it, (b) try a different single GPU type (A5000 or 3090) to see if it's specific to whichever box gets assigned for 4090 specifically in this account/region, (c) add SSH `ServerAliveInterval`/retry-with-reconnect resilience to the bootstrap upload step so a transient drop doesn't kill the whole run, (d) just retry — if a 3rd dispatch lands a different host and completes cleanly, this was a one-host problem, not systemic.

**Still outstanding from previous entry, unaffected by this:** fallback GPU list (4090/A5000/3090/L4) is still narrowed to single-4090 on `fix/runpod-nvdec-diagnosis`, not yet merged to main, not yet restored/re-tested with `gpuTypePriority` dropped.

## RunPod GPU fallback pool — NVDEC regression ROOT-CAUSED, new SSH-drop issue found (12 Aug 2026, branch `fix/runpod-nvdec-diagnosis`)

**Root cause confirmed (not a GPU pool/host issue as previously suspected):** `28c0d4379b` (single `gpuTypeIds: ['NVIDIA L4']`) ran clean end-to-end. The very next commit, `102bbc6809`/merge `9a140b9624`, changed to `gpuTypeIds: [4090, A5000, 3090, L4]` + `gpuTypePriority: 'custom'` — every dispatch since then (4 runs) failed `PREFLIGHT_NVDEC` identically. Isolation dispatch (`31587433143`, single-type `['NVIDIA GeForce RTX 4090']`, no `gpuTypePriority`) passed clean: `PREFLIGHT_RESULT=PASS`, all 4 sub-checks PASS, `libnvcuvid.so.570.195.03` present and correctly symlinked. **Conclusion: the `gpuTypeIds`(multiple) + `gpuTypePriority: custom` request shape itself is what breaks NVDEC on RunPod's allocator — not the GPU class.** Do not re-add the 4-GPU fallback list with `gpuTypePriority: custom` without re-testing that combination specifically (untested: multi-id list WITHOUT `gpuTypePriority`, to isolate whether it's the priority field or having >1 candidate at all).

**Fix @ `947aa409` (workflow) + `cff67000` (preflight), branch `fix/runpod-nvdec-diagnosis`, NOT YET MERGED to main:**
- `oev-runpod-followcam.yml`: `gpuTypeIds` narrowed to single `['NVIDIA GeForce RTX 4090']` (temporary diagnostic state — restore fallback list, re-tested, before calling this done). Fixed the `gpu_type=unknown` observability gap: the polling `GET /pods/{id}` call was missing `?includeMachine=true` (an opt-in REST param, not a timing/race issue as first assumed) — `machine` is always empty without it. Confirmed fixed: run `31587433143` resolved `gpu_type=NVIDIA GeForce RTX 4090` correctly.
- `runpod_gpu_preflight.sh`: added NVDEC diagnostics (prints `NVIDIA_DRIVER_CAPABILITIES`, searches for `libnvcuvid.so*`, checks `libcuda.so.1`) — additive only, does not change PASS/FAIL logic. Confirmed on this run: `NVIDIA_DRIVER_CAPABILITIES` is unset on RunPod's base image and that's fine — `libnvcuvid.so` is still present/linked regardless (RunPod's own container runtime doesn't gate it behind that env var, unlike raw `nvidia-docker`). No env-var fix needed for NVDEC itself.

**New, unrelated issue surfaced same run:** bootstrap step died at `ssh: connect to host ... Connection timed out` / `scp: Connection closed`, exit 255 (not one of `runpod_bootstrap.sh`'s own exit codes 1-4) — SSH connection to the pod dropped ~2 min into the bootstrap step, cause not yet diagnosed (transient single-host network blip vs. a systemic keepalive/timeout problem — one data point, not enough to tell). Pod cleanup/termination confirmed successful despite the failure (no leaked pod).

**Next:** re-dispatch `fix/runpod-nvdec-diagnosis` once more to see if the SSH drop repeats (systemic) or was one bad host (transient). If clean, decide fallback-list restoration approach (drop `gpuTypePriority` entirely, or re-test it alone) and merge to main.

## Clip Extractor — new "Clip Errors" tab: cross-tab error index (13 July 2026, merge `835cb9c`)

**Why:** Johnson spotted scattered false-positive errors across tabs (e.g. a 10s clip on the Monday Camera B tab wrongly flagged `Skipped: clip too long (46.4 mins)`) and wanted one place to see every clip currently in an error state instead of hunting tab by tab. This ticket only builds the index — it does NOT fix the underlying false-positive duration/parse bug, which is a separate, not-yet-started ticket.

**Fix @ `c3754f7` (Claude-authored, merged to main via `835cb9c`, feature branch `feat/clip-errors-tab` deleted after merge):** `sheet_manager.py` only, additive.
- New `CLIP_ERRORS_TAB = "Clip Errors"` constant, excluded from the video-tab loop alongside Index/Add Video/Clips Tracker.
- `ensure_clip_errors_tab()` — same create-if-missing pattern as `ensure_clips_tracker_tab()`; header `Video | Row | Start | End | Name | Status`.
- `_reconcile_clip_errors(sheets_svc, spreadsheet_id, tab_names, tab_gids)` — runs at the end of every `process_clips()` pass (1 batchGet across all video tabs' `A6:E` + 1 clear + 1 write). Full overwrite each run rather than incremental, so it always reflects current sheet state as clips get fixed/retried. Any row with Status starting `Error:` or `Skipped:` gets a row here, with a `HYPERLINK("#gid={gid}&range=A{row}", tab_name)` formula that jumps straight to the offending row in its source tab (same `#gid=` pattern the Index tab already uses, extended with `&range=` for a direct row jump).
- `process_clips()` now builds a `tab_gids` map from the same `meta` call it already makes (no extra API cost) and calls the new reconcile function after the Clips Tracker one; completion log line now also reports error-row count.
- Nothing else touched — diff verified clean before merge (additive only, no changes to sheet-writing, Drive, encode, credentials, or bot-check logic).

**Not yet dispatched for verification** — the earlier clip-extractor run (`29288376802`, from the bot-check breaker fix) was still `in_progress` at merge time, so no test dispatch was queued behind it to avoid delaying that run's own verification. This will self-verify on the next natural `process_clips()` run (scheduled or manual) since the reconcile now runs every pass — check the "Clip Errors" tab exists with correct rows/links next time either run is inspected.

**Next:** once a process-clips run completes with this code, confirm the Clip Errors tab was created, contains one row per current Error/Skipped clip, and each row's link correctly jumps to the right tab+row.

## Clip Extractor — bot-check circuit breaker fixed to be run-scoped, not per-tab (13 July 2026, merge `3a9d595`)

**Bug (found in a prior session):** `consecutive_botchecks` was a local variable inside `_process_tab()`, resetting to 0 at the start of every tab. In a real run touching 8 tabs the breaker tripped 8 separate times (5 failures each) instead of once — 44 wasted bot-check hits instead of ~5, defeating the purpose of backing off once IP-throttling is detected.

**Fix @ `f780896` (Claude-authored, merged to main via `3a9d595`, feature branch `fix/botcheck-breaker-run-scoped` deleted after merge):** `sheet_manager.py` only. `process_clips()` now creates one `botcheck_state = {"count": 0, "tripped": False}` dict for the whole run and passes it into every `_process_tab()` call. `_process_tab()` takes `botcheck_state` as a new required param and, as its very first action (before any Sheets reads), returns 0 immediately if `tripped` is already True — so a tripped breaker skips all remaining tabs entirely, with zero further `_fetch_clip_section` calls. Inside the per-clip loop, all `consecutive_botchecks` reads/writes were replaced with `botcheck_state["count"]`/`botcheck_state["tripped"]`; hitting 5 sets `tripped = True` and prints one stop message, then `break`s the current tab loop as before. Untouched per spec: sheet-writing logic, Drive upload logic, `_reencode_clip`, credential handling, `_classify_ytdlp_error`, `MAX_CLIP_SECONDS`, cookies-first logic, sleep/throttle flags, the Processing/Link-empty self-heal pickup.

**Diff verified before merge** via `compare/main...fix/botcheck-breaker-run-scoped`: exactly the expected 6 hunks in `sheet_manager.py`, no other files touched.

**Verification run `29288376802`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/29288376802 — still `in_progress` after 2 polls (poll budget exhausted this session). Head commit `3a9d595`.

**Next:** once the run completes, pull the log and confirm the breaker message ("Stopping early: N consecutive bot-checks...") appears **at most once** across the whole run, and that any tabs after a trip log "Skipping — bot-check circuit breaker already tripped this run" with no clip-fetch attempts. If it never trips this run (i.e. no bot-checks occurred at all, e.g. cookies now valid per the 13 July Chrome fix above), that is inconclusive for this specific fix and does not confirm or deny the run-scoping — would need a run that actually hits bot-checks to fully verify.



## Clip Extractor cookie saga — RESOLVED: Chrome restarted, cookies persisting again (13 July 2026)

**Gate cleared — Johnson gave the go and re-logged in.** The dead root Chrome process was killed and relaunched fresh on Xvfb `:99` with the `chrome-ffa` profile via a new self-healing cron watchdog; Johnson signed into YouTube once via VNC. Cookie persistence is now confirmed working.

**Verification (conclusive):** root's `/root/.config/chrome-ffa/Default/Cookies` is now **36864 bytes, mtime 2026-07-13 15:54:59** (written at Johnson's fresh login) — versus the dead 4096-byte schema-0 shell frozen at 2026-06-28 21:31:58 that caused the two-week outage. The old file was backed up to `Cookies.broken.<ts>` before relaunch. Chrome confirmed alive via remote-debug port (`Chrome/149.0.7827.53` on `http://localhost:9222`).

**Root cause of the failed first relaunch attempt (fixed):** Chrome refuses to run as root without `--no-sandbox` (`zygote_host_impl_linux.cc:101`). The original long-lived process had it; the watchdog now includes `--no-sandbox --disable-dev-shm-usage`.

**Watchdog now installed on the VM (persistent, VM-side state — no repo footprint):**
- Script: `/root/chrome_ffa_watchdog.sh` (relaunches Chrome on `:99`, profile `/root/.config/chrome-ffa`, `--no-sandbox`, remote-debug port 9222, opens youtube.com; no-ops if already running; clears stale `Singleton*` locks; inherits Xvfb `:99` auth if any).
- Root crontab: `* * * * *` + `@reboot sleep 25` → same self-healing pattern as the x11vnc watchdog. If Chrome ever dies again, check `crontab -l` / `/tmp/chrome_ffa_watchdog.cron.log` before rebuilding.
- Xvfb `:99` runs as root: `Xvfb :99 -screen 0 1280x800x24` (untouched since 5 June).
- The remote-debug port also gives a disk-independent way to pull live cookies from browser memory in future (immune to another disk wipe).

**Temp workflows used for this and then deleted** (`chrome-relaunch.yml`, `cookie-check.yml`) — no permanent repo footprint; all persistent state is VM-side (script + crontab).

**Clip-extractor: DISPATCHED — UNVERIFIED** (run `29264421618`, https://github.com/JhnsonO/ffa-automations/actions/runs/29264421618). Working through the ~2-week pending-clip backlog. **Next:** inspect that run + the FFA Clips sheet Status column for real "Done" links (not bot-check text) and confirm the Clips Tracker backfill from `967aad9`. If a fresh login still bot-checks, the Vultr IP itself may need rotating (no evidence for that yet).

**Superseded below:** the 12 July "awaiting go" / "session revoked" / earlier root-cause sections are historical — the disk-cleanup-broke-persistence diagnosis in them is correct, but their "not yet executed / needs manual re-login" status is now done.


## Clip Extractor cookie saga — [SUPERSEDED 13 July, see top section] fix proposed, awaiting go (12 July 2026)

**Definitive finding:** root's live Chrome process (pid alive since 5 June, `/root/.config/chrome-ffa`) has a permanently broken on-disk cookie persistence, NOT a stale-login problem. Evidence: `Safe Browsing Cookies` file in the same Default/ dir updated **today 22:23** (Chrome is alive and actively writing files), but the actual `Cookies` file (site logins) has not changed by a single byte since **2026-06-28 21:31:58** — through multiple fresh VNC logins tonight. Johnson identified the trigger: a Vultr disk-space-clearing pass around that date almost certainly deleted the Cookies file while Chrome held it open; Chrome has been holding a dead/orphaned file handle ever since and can never re-establish persistence without a process restart. No further diagnostic needed — this is conclusive.

**Why every workaround attempted tonight failed:** `9cb3c6a`'s sync step and the pre-existing root rsync cron both correctly copy whatever is in the Cookies file — but the source itself never receives new writes, so both faithfully sync an empty shell forever, regardless of login state.

**Fix proposed to Johnson, NOT YET EXECUTED (needs explicit go, kills the current Chrome session):**
1. Kill the current broken root Chrome process; delete the dead Cookies file.
2. Install a cron watchdog (same self-healing pattern as the x11vnc watchdog already running) that keeps Chrome alive on display `:99` with the `chrome-ffa` profile, plus a remote-debug port — giving a second, disk-independent way to pull live cookies from browser memory in future (immune to another disk wipe).
3. Chrome relaunches fresh on Johnson's VNC screen within ~1 minute; a clean process will persist cookies normally.
4. **Johnson logs in one more time** — last time needed, since persistence will then actually function; `9cb3c6a` sync + existing rsync cron pick it up automatically from then on.

**Immediate next action when resumed:** get Johnson's go, then execute step 1–2 above (new workflow dispatch to kill+relaunch Chrome via cron, mirroring the x11vnc watchdog installation pattern from earlier this session), confirm Chrome comes back up on :99, have Johnson log in once, then dispatch clip-extractor and verify real downloads succeed (Status column shows real "Done" links, not bot-check text) and Clips Tracker backfills per `967aad9`.

**All temporary diagnostic workflows from tonight deleted** (vnc-diagnose, vnc-authcheck, vnc-readonly, vnc-cookiecheck, vnc-postlogin). VNC access itself (x11vnc on :99, cron watchdog, password `58869612`) is confirmed working and self-healing independently of the Chrome cookie issue — that part is fully resolved.


## Clip Extractor / VNC saga — true root cause: cookies wiped during a Vultr disk-cleanup pass on 28 June (12 July 2026)

**Confirmed via direct inspection:** root's live Cookies file (`/root/.config/chrome-ffa/Default/Cookies`) is schema-0/empty, mtime frozen at exactly **2026-06-28 21:31:58**, unchanged even after Johnson's fresh VNC login attempts tonight — the browser LOOKS logged in because the open tab is stale/cached, not because a real session exists on disk. **Johnson identified the actual trigger: a disk-space-clearing pass on the Vultr VM around that date almost certainly deleted/truncated the Cookies file along with genuine cache/log files** (it's indistinguishable from disposable cache data to a generic cleanup). Not a Google-side revocation — this explains the abrupt wipe far better (no partial/invalidated-cookie trace, just gone).

**Also discovered (unrelated, pre-existing):** a root crontab entry already existed (`rsync -a /root/.config/chrome-ffa/ /home/runner/.config/chrome-ffa/ && chown ...`, ~every 15 min) built specifically to solve the same runner-read-permission problem `9cb3c6a`'s sync step addresses — redundant mechanisms now, both harmless to leave in place. Last seen firing 21:15/21:30 on 28 June in journalctl; not confirmed whether it's still active today (not re-checked, not urgent).

**Outstanding action (Johnson, whenever convenient, no rush):** in the VNC session, hard-refresh or open a fresh tab to youtube.com (not the existing stale one) and complete a real sign-in as `footffa@gmail.com`. Once genuine cookies are written, both `9cb3c6a`'s workflow sync step and the pre-existing rsync cron will pick them up automatically — no further manual steps needed after that.

**Recommendation for future VM disk cleanups:** exclude `~/.config/chrome-ffa` (both root's and any synced copies) from cache/disk-space sweeps — it holds live login state, not disposable cache.

**All temporary diagnostic workflows from this investigation deleted** (`vnc-diagnose`, `vnc-authcheck`, `vnc-readonly`, `vnc-cookiecheck`). No permanent repo footprint from tonight's VNC/cookie troubleshooting beyond `9cb3c6a` and the earlier sheet_manager.py fixes.


## VNC password correction (12 July 2026)

**Original password `ffa92762` (mixed letters+digits) was rejected on Johnson's device** — server log confirmed genuine `password check failed` on real connection attempts from his IP, ruling out any plumbing/path bug. Regenerated as all-digit to remove any mobile-keyboard autocapitalization risk. **Current password: `58869612`**, stored at `~/.vnc/passwd` on the VM (mtime 22:01 12 July), confirmed via read-only check to be what the live cron-respawned x11vnc instance is actually using.

**Process note:** two intermediate passwords (`45793261`, then `58869612`) were generated while diagnosing — only `58869612` is current/correct. All temporary diagnostic/mutating workflows used for this (`vnc-diagnose`, `vnc-authcheck`, `vnc-readonly`) have been deleted; no permanent workflow files remain from this VNC troubleshooting, all state lives on the VM (crontab + passwd file).


## VNC access restored + made self-healing (12 July 2026)

**Root cause:** no VNC server process existed on the VM at all (x11vnc/vncserver both absent from `ps aux`, port 5900 not listening) — whatever served it originally likely died with the manual terminal session that started it, since it was never a persistent service. Firewall was NOT the issue (`5900/tcp ALLOW Anywhere` already open); logged UFW blocks were UDP probe packets from Johnson's phone, irrelevant to the actual TCP VNC protocol.

**Fix (VM-side, not a repo code change):** confirmed `Xvfb :99` (the virtual display Chrome renders to) has been running since 5 June, untouched. Started `x11vnc -display :99 -rfbport 5900` pointed at that existing display — does not touch/restart Chrome or the X server. Generated a fresh VNC password (`ffa92762`, stored at `~/.vnc/passwd` on the VM) since none existed on disk. Installed a self-healing cron watchdog (`* * * * *` + `@reboot`, pattern copied from the existing working `ffa-labeling-tunnel` watchdog already in this VM's crontab) that restarts x11vnc if it ever dies again — confirmed working: the first x11vnc instance was killed by the Actions runner's own orphan-process cleanup at job-end (expected, same mechanism that blocked the cloudflared tunnel previously), but the cron-spawned replacement (different PID, spawned outside any job's process tree) survived a subsequent job's cleanup untouched.

**Verified:** port 5900 listening (x11vnc), reachable per Johnson's next message. No workflow files added — all changes are VM-side state (crontab + `~/.vnc/passwd` + `~/x11vnc_watchdog.sh`), done via temporary diagnostic workflows that were deleted after use (no permanent repo footprint).

**Note for future sessions:** if VNC ever stops working again, check `crontab -l` on the VM for the `x11vnc_watchdog.sh` lines before assuming it needs rebuilding — the watchdog should already self-heal; investigate why the watchdog itself died (e.g. VM reboot without the `@reboot` line firing correctly) rather than repeating this whole diagnosis.


## Clip Extractor — corrected diagnosis: session genuinely revoked ~28 June, not just a path bug (12 July 2026, run 29209210035 cancelled by Johnson)

**9cb3c6a's path/sync fix is correct and stays** — verified the sync step runs and copies successfully every time. But the "no valid cookie DB" check still failed after sync, so root's *actual* live file (not the copy) was inspected directly via sudo: it is **itself** a 4096-byte, schema-0, empty SQLite shell, last written **28 June 21:31 UTC** — identical state to the old broken runner-owned copy. No `-wal`/`-shm` sidecars exist either (checked; not a WAL-mode red herring).

**Revised root cause:** the Chrome window has been sitting on youtube.com since 5 June looking alive, but the session itself was invalidated — almost certainly Google revoking it, plausibly triggered by the sustained bot-flagged automated traffic once the outage began. 28 June lines up closely with when the run history's failure streak actually starts. The path/ownership bug (`b3a7d62`/`9cb3c6a`) was real and worth fixing, but is not what caused the two-week outage by itself — the session dying is.

**No further code fix possible here** — a live authenticated browser is required at least once to re-establish a session; this cannot be routed around programmatically. Johnson needs to reopen the existing Chrome window on the Vultr VM (however he originally accessed it — VNC/remote desktop) and log into YouTube again **in that same window/profile** (not a new one). Once live, the `9cb3c6a` sync step picks up fresh cookies automatically on the next scheduled run — no manual export/paste needed, and this was the one-time exception to "built the VM so I wouldn't have to do this," not a recurring requirement.

**Run `29209210035` was cancelled by Johnson mid-run** (still showing bot-check on both Chrome-profile and secret-cookie-file paths — consistent with this diagnosis, secret cookies are stale too).

**Next:** Johnson re-authenticates the VM's Chrome session; then dispatch clip-extractor.yml and verify real downloads succeed + tracker backfills. If a fresh login still gets bot-checked immediately, the VM's IP itself may need rotating — not yet a confirmed issue, no evidence for it either way.


## Clip Extractor — root cause found + fixed: profile path/user mismatch, not credential expiry (12 July 2026, `9cb3c6a`)

**Real root cause (via self-hosted diagnostic dispatch, since removed):** the workflow's `CHROME_PROFILE_PATH` pointed at `/home/runner/.config/chrome-ffa` — an empty, schema-0 SQLite shell (owned by `runner`, never had cookies). The actual live, logged-in-since-5-June Chrome session (still open on youtube.com) runs as `root` at `/root/.config/chrome-ffa`, unreadable by the `runner` user due to path/ownership mismatch — not because cookies expired. This was very likely wrong since initial setup, not a regression.

**Fix @ `9cb3c6a`:** new step in `clip-extractor.yml`, "Sync live YouTube cookie profile", runs before Process pending clips. Confirmed the runner has passwordless sudo (`sudo -n test -r ...` succeeded). Step does `sudo cp` of `Local State` + `Default/Cookies(-journal)` from root's live profile into `$HOME/.cache/yt-chrome-sync`, `chown`s to `runner`, tightens perms. `CHROME_PROFILE_PATH` for the process step repointed at that synced copy. `continue-on-error: true` on the sync step so a sudo/copy hiccup falls back gracefully to the `_chrome_profile_usable()` check already shipped in `b3a7d62` — no change needed in `sheet_manager.py` for this. Runs every 6h, so the copy is always near-live; no manual cookie export/re-login required as long as the root Chrome session stays signed in.

**Run `29209210035`: DISPATCHED — UNVERIFIED** (in_progress at both allowed polls, not failed — likely working through the 2-week backlog of pending clips). https://github.com/JhnsonO/ffa-automations/actions/runs/29209210035

**Next:** check run 29209210035 outcome + sheet Status column (should show real downloads succeeding, not bot-check flags, if this worked) + Clips Tracker backfill from the `967aad9` reconcile pass. If bot-check flags still appear, the root Chrome session itself may have been logged out server-side — that would be the one scenario still requiring a manual re-login via the existing `yt-cookie-refresh.html` helper.


## Clip Extractor — tracker reconcile shipped; first fix run GREEN (12 July 2026, `967aad9`)

**Run `29208205545` (post-fix verification) COMPLETED SUCCESS** — the b3a7d62/8c558e3 fixes executed cleanly end-to-end. Whether clips actually downloaded (cookies working) vs got classified error flags is not yet inspected — check the sheet Status column.

**Clips Tracker root cause:** rows only appended after full clip success (so 2 weeks of nothing was expected), plus a real gap — per-clip append ran AFTER the Done/link writes, so 429 mid-run crashes left Done clips missing from the tracker; numbering used len(col A) and breaks on manual edits.

**Fix @ `967aad9` (Claude-authored, Johnson chose link-match):** per-clip `_append_to_clips_tracker` removed, replaced with `_reconcile_clips_tracker()` at end of every process-clips run — 1 tracker read (FORMULA render) + 1 batchGet across all video tabs; any row with a Drive link absent from the tracker (matched by URL) is backfilled in one append; numbering = max existing # + 1. Special tabs (Add Video, Clips Tracker) now explicitly excluded from processing. Mock-tested: backfill, dedupe vs manual/renamed rows, plain-URL links, numbering.

**Run `29208745479`: DISPATCHED — UNVERIFIED** (reconcile + backfill exercise). https://github.com/JhnsonO/ffa-automations/actions/runs/29208745479

**Next:** inspect run 29208745479 log tail ("Tracker reconcile: N missing row(s) backfilled") + Johnson eyeballs the tracker tab. Cookie-staleness question from previous section still open pending sheet inspection.


## Clip Extractor — failure diagnosis + fix shipped (12 July 2026, `b3a7d62`/`8c558e3`)

**Diagnosis (runs inspected 28 June–12 July):** extractor produced zero clips for ~2 weeks. Every yt-dlp download failed: (1) cookie-less attempt hits YouTube bot-check (Vultr IP flagged); (2) cookie fallback dead — Chrome profile at `/home/runner/.config/chrome-ffa` has unreadable cookie DB (`no such table: meta`), and the workflow's `yt_cookies.txt` was never read by the script (env not passed); (3) poison row `Third_miss` (Thursday 4th June tab) has reversed timestamps 01:01:41→00:01:02, negative duration bypassed the 90s guard, retried every run; (4) accumulated per-tab reads tripped Sheets 60-reads/min quota → unhandled 429 killed runs mid-scan (only difference between red and green runs). Separate 28–30 June phase: checkout EACCES on stale .git refs on VM, self-cleared.

**Fix shipped to main (Claude-authored per Johnson's explicit "no codex" instruction):** `sheet_manager.py` @ `b3a7d62` — classified failure reasons written to Status col with last-try timestamp (bot-check / cookie-profile-unreadable / unavailable / private / no-1080p / rate-limited), `end<=start` validation before download, `_execute_with_backoff` (429 exponential backoff) on all process-clips-path Sheets calls, `_chrome_profile_usable()` sqlite check gates the chrome cookie source. `.github/workflows/clip-extractor.yml` @ `8c558e3` — one line: `YOUTUBE_COOKIES_FILE` env passed to process step (fixes dead cookie fallback). Retry semantics unchanged: rows retry while Link empty, so backlog self-redoes once downloads work.

**Verification run `29208205545`: DISPATCHED — UNVERIFIED** (in_progress at last poll). https://github.com/JhnsonO/ffa-automations/actions/runs/29208205545

**Open risks:** (1) `YOUTUBE_COOKIES` secret may itself be stale — if the run still shows bot-check flags in the sheet, secret needs refresh; (2) Chrome profile on Vultr VM needs manual repair/refresh regardless; (3) `Third_miss` timestamps must be corrected in the sheet by a human — it will now flag "end before start" instead of failing silently.


## Flatcam — lens strength + venue mask RESOLVED (9 July 2026, later still, `5d335a2`/`6d7d3f9`)

**Correction strength confirmed by Johnson: raw (0.0), deferred not final.** `flatcam/lens_profiles.json` MSV profile fixed: `distortion_correction_strength: 0.0`, `calibration_status: "deferred"`. Note: live `main` had drifted to `f90d967`'s `strength=1.0/fov=170`, self-described in its own commit/notes as "visually_tuned" — this was a live contradiction against this state file's own record of Johnson rejecting that render as over-corrected. Resolved in favour of this file's human-verified record; `f90d967`'s self-assessment was wrong. Flag for future sessions: don't trust a commit's own notes over Johnson's actual recorded verdict when they conflict.

**Venue mask written:** `flatcam/venues/st_margarets_msv.json`, the 24-point polygon Johnson approved (raw frame-pixel space, 3840x2160). Since strength=0.0 is a verified true identity map in `undistort.py` (`map_x=xs, map_y=ys` exactly when `s=0`), raw space = undistorted space here, so the approved points were written directly, no transform needed.

**Not yet done:** `render_segment_flat.py` re-run with both fixes — needs real MSV footage (`GX010424 copy.mp4` / `GX010424.MP4`), which is not present in this session's sandbox and was never committed to the repo (local-only per last session's note). Re-source from Drive (`footffa@gmail.com`, id `1xfr5gvMeYtkyVs1DdqU3GROcuVUt6BvQ`) or get a fresh clip from Johnson before this can run — CPU-only local pipeline, no workflow/dispatch needed once footage is available.

**Next gate:** get real footage, run `render_segment_flat.py --profile gopro_max2_msv_4k60 --venue flatcam/venues/st_margarets_msv.json`, visual sign-off from Johnson before calling flatcam done.

## Flatcam — pan-only v1 (9 July 2026, merge `e3b0f296`)

**FOLLOW-mode zoom removed per Johnson's request.** `flatcam/follow_camera_flat.py` FOLLOW mode now uses `self._wide_size()` for crop dimensions (same as WIDE_FALLBACK) instead of a fixed 0.55x zoom — crop size is constant across all modes, only `cx`/`cy` pan. Verified: 3s real-footage re-render (`GX010424`, frames ~128-131s) shows crop_w/crop_h constant at 3840x2160 across all 180 frames, both FOLLOW and WIDE_FALLBACK modes. Zoom deferred to v2, not deleted.

**First real-footage render also verified today** (pre-pan-only): 3s segment, output valid, FOLLOW mode engaged correctly on real footage for the first time.

**Next gate:** Johnson visual sign-off on pan-only render. If approved, flatcam v1 (pan-only, raw lens correction) is done — zoom tuning is a separate future task, not started.

## Flatcam — EDGE_MARGIN locked at 0.80 (10 July 2026, `1d13c2ad`)

**Johnson tested 0.9 / 0.85 / 0.80 / 0.75 renders on real footage and locked 0.80.** Constant crop-in (both modes, still pan-only) to reduce visible lens-edge distortion. Proper distortion correction (undistort calibration) explicitly DEFERRED by Johnson — do not revisit until he raises it. Intermediate commits: 0.9 @ `224373d2`, 0.85 @ `43dc2e88`.

**Flatcam v1 config now locked:** raw lens (strength 0.0), pan-only FSM, EDGE_MARGIN 0.80. All verified on GX010424 real footage (Drive id `1xfr5gvMeYtkyVs1DdqU3GROcuVUt6BvQ`).

## Flatcam — full-clip render workflow built, merged, dispatched (10 July 2026)

`.github/workflows/flatcam-render.yml` added: CPU-only Vast.ai `workflow_dispatch`, Vast lifecycle mechanics copied verbatim from `playcam-poc.yml` (`428ac208`) — only the offer query adapted (no GPU fields; `cpu_cores>=16`, `cpu_ram>=32768MB`, `disk_space>=60`). Drive download reuses the existing YOUTUBE_TOKEN/YOUTUBE_CREDENTIALS oauth-refresh pattern verbatim. Runs `render_segment_flat.py --input source.mp4 --profile gopro_max2_msv_4k60 --venue flatcam/venues/st_margarets_msv.json --output full_render.mp4 --csv-out full_render.csv` on the full downloaded clip (script has no trim flags, so no windowing — matches full-clip requirement). No frozen files touched; diff was a single new file, 288 additions.

**Run 1 (`29073069722`) FAILED** — instance launched fine (AMD EPYC 7502, 64 cores), but `Wait for SSH` timed out after 18 attempts. Cause: launch step used `image: python:3.11-slim` for the CPU-only offer instead of the proven Vast image — that generic image has no `sshd` installed, so Vast's SSH runtype never came up. Fixed @ `c2320fee`: image reverted to the exact proven `pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime` from `playcam-poc.yml` (CPU-only offer query unchanged; the image itself was not "legitimately script-specific" — only the offer query was meant to be adapted, that was the actual mistake).

**Run 2 (`29073246636`) SUCCEEDED** — all steps green, instance terminated cleanly, no leak. Artifact `flatcam-full-render-29073246636` (575.7 MB) uploaded, containing `full_render.mp4` + `full_render.csv` for the full 174s GX010424 clip. Run: https://github.com/JhnsonO/ffa-automations/actions/runs/29073246636

A green run proves execution only, not product quality — the actual gate is Johnson watching the full render.

**Next gate:** Johnson downloads and watches the full render (`flatcam-full-render-29073246636`, run https://github.com/JhnsonO/ffa-automations/actions/runs/29073246636) for: pan smoothness across real play, FSM behaviour during stoppages (WIDE_FALLBACK transitions), whether 0.80 margin holds up pitch-wide. That visual sign-off is the actual product gate.

## Flatcam — full-clip visual sign-off: PASSED WITH TWO OPEN ISSUES (10 July 2026)

**Johnson's verdict, watching the full 174s render: "not bad AT ALL... needs tweaking but pretty good."** Flatcam v1 (raw lens, pan-only, EDGE_MARGIN 0.80) is directionally validated on a full real clip, not yet production-final. Two issues flagged, neither fixed yet — no code touched since `c2320fee`.

**1. Camera lags when the ball goes to the far side.** Diagnosed (not yet confirmed against data): the pipeline tracks MOG2 motion-centroid concentration, not the ball itself — `action_centroid.py` finds where movement mass is clustered, `follow_camera_flat.py`'s FSM (FOLLOW_T 0.45 / WIDE_T 0.30 / HYSTERESIS_S 1.5, untouched defaults) reacts to that score. When the ball outruns the player cluster, the centroid lags behind the actual ball position — this is a structural property of the motion-mass approach, not obviously a threshold bug. **Not yet done:** pull `full_render.csv` (in the run-2 artifact) and correlate mode/score/cx/cy against the far-side moments Johnson noticed, to see whether it's a threshold/hysteresis tuning issue or a deeper approach limitation.

**2. Lens curve/distortion is noticeable.** Expected given `distortion_correction_strength: 0.0` (raw passthrough) on the MSV profile — the profile's own note says revisit only if a real render visibly shows edge warping, which is now the case. Two knobs, not to be conflated:
   - EDGE_MARGIN (crop-in) is already at its tested ceiling — Johnson tried 0.9/0.85/0.80/0.75 and picked 0.80 over 0.75, so "zoom in more" via this knob re-litigates a call already made on real footage.
   - `distortion_correction_strength` is the actual unexplored lever: 0.0 (now, raw) and 1.0 (rejected 9 July as over-corrected/edge-stretching) are the only two points tried. Nothing in between (e.g. 0.3–0.5) has been rendered or judged.

**Handover note:** Johnson wants to interrogate the process itself in the next chat before deciding how to proceed on either issue — treat this as open for discussion, not a green light to pick a correction-strength value or touch FSM constants. No do-not-touch rule has been lifted; Johnson raising the topics unlocks discussion, not unilateral changes.

Plan after both issues are resolved and re-verified:
1. Live-match test — record a real FFA session on Max 2 flat mode, run pipeline end-to-end, judge production quality.
2. Only after that passes: decide flatcam's relationship to playcam/360 pipeline (replace vs complement), revisit dynamic zoom v2.

Do NOT: pick a new distortion_correction_strength value, re-tune FSM constants, or dispatch a new render without Johnson's explicit go. Scope discipline per CLAUDE.md.

## Flatcam — lens distortion comparison stills workflow built, merged, dispatched (10 July 2026)

`.github/workflows/flatcam-stills.yml` + `flatcam/lens_stills.py` added (merge `9ebda929`, feature branch `flatcam-lens-stills`). Addresses issue #2 (lens curve) from the full-clip sign-off: `distortion_correction_strength` has only been tried at 0.0 (current) and 1.0 (rejected 9 July). No full render, no Vast.ai instance — standard GitHub-hosted runner only. Pulls one frame via ffmpeg from the same Drive source (`GX010424 copy.mp4`, id `1xfr5gvMeYtkyVs1DdqU3GROcuVUt6BvQ`, default timestamp `00:01:00`, arbitrary per Johnson — stills don't move through the video so timing doesn't matter), runs it through `undistort_frame()` at six strengths (0.0/0.25/0.4/0.55/0.7/1.0) with the override applied in-memory only, then center-crops each to 3072x1728 (3840x2160 x EDGE_MARGIN 0.80, matching `follow_camera_flat.py`'s production crop). Uploads only the 6 JPGs as the artifact. `flatcam/lens_profiles.json` itself not touched. Diff verified before merge: 2 files added, 0 modified, 150 lines.

**Run `29077900462` SUCCEEDED** — all steps green. Artifact `flatcam-lens-stills-29077900462` (5.85 MB, 6 JPGs) uploaded. Run: https://github.com/JhnsonO/ffa-automations/actions/runs/29077900462.

**SUPERSEDED (10 July 2026, later session):** this stills track was never reviewed and is discarded by Johnson's explicit decision, in favour of the bounded strength-segment visual test below. Do not resurrect the still-frame gate or ask Johnson to review `29077900462`. `lens_profiles.json` remains untouched either way — no do-not-touch rule lifted by this task.

## Flatcam — full calibration/rectilinear-renderer design (10 July 2026) — ARCHIVED AS FALLBACK, NOT ACTIVE

A multi-session design (Claude/ChatGPT/Johnson, static-frame lens calibration + rectilinear FOV renderer for a stronger dewarp than the strength-knob architecture: Gate 0 two-stage stability check (RANSAC homography frame-motion vs non-rigid residual, held-out static features), division-model warp (`k1,k2` bounded, monotonic) jointly fitted with camera pose against measured pitch geometry, full Monte Carlo uncertainty propagation (joint resample of correspondences + survey inputs, 200 resamples), primitive-level held-out validation, locked numeric thresholds (reprojection RMS ≤3.0px, anisotropy floor + directional-bias test, ≥25% held-out coverage), camera/venue file separation, provisional/mount-scoped naming. **Explicitly paused by Johnson as over-scoped for the current decision.** No code written. Kept here only as a reference design if a genuine camera-global rectilinear calibration is ever pursued — do not resume without Johnson explicitly re-opening it.

## Flatcam — strength segment A/B visual test built, merged (10 July 2026, later session, merge `895fcd2`)

**Replaces the stills track and the full calibration design (both above) as the active decision path.** Bounded ticket: does a mid-range `distortion_correction_strength` look better than raw (0.0) on a real followcam segment, judged by eye — no absolute calibration, no yaw, no pose fitting.

Built directly by Claude per Johnson's explicit routing override for this ticket (normally Codex writes, Claude verifies — CLAUDE.md three-AI split unchanged as default; this was a one-off exception due to renderer-boundary nuance).

**Files added, frozen files untouched** (`action_centroid.py`, `follow_camera_flat.py`, `render_segment_flat.py`, `undistort.py`, `lens_profiles.json`, `flatcam/venues/st_margarets_msv.json` all unmodified):
- `flatcam/strength_segment_test.py` — trims a segment (ffmpeg), computes the camera path ONCE by running the unmodified production renderer at the profile's on-disk strength (asserts it's 0.0 — the verified raw-space identity condition — or aborts rather than silently changing semantics); that run's own output is the strength-0.0 baseline. For each additional strength, overrides `distortion_correction_strength` on the loaded profile dict in memory only (never touches `lens_profiles.json` on disk) and replays the same segment.
- Framing method (revised twice this session after review): raw crop box's left/right/top/bottom edge-midpoints are inverted through the exact per-strength mapping (obtained via the public `undistort_frame()`, not a reimplemented formula) — horizontal span and centre are exact by construction. An earlier centre-point local-gradient approximation was tried and rejected: measured up to −64% span error and +219px centre bias under this warp.
- Diagnostics logged per frame, per strength, in `camera_path_s{XXX}.csv`: `v_cover` (vertical scene coverage vs the fixed 16:9 output aspect — verified 0.997–1.004 across strengths 0.1/0.3/0.5, i.e. vertical framing holds); `corner_err_px` (max distance between the raw box's true inverted corners and the assumed rectangle's corners — NOT zero, grows with strength: ~2.2% of crop width at 0.1, ~8.2% at 0.3, ~16.8% at 0.5. This is a genuine geometric property of fitting a rectangle to a non-conformal radial warp, not an implementation bug — centre framing is exact, corner/peripheral content diverges more at higher strengths, consistent with the known edge-stretching behaviour that got strength=1.0 rejected on 9 July. Logged as an inspectable diagnostic; no auto-gate applied).
- Timing: `timings.json` — path-compute (decode+detector+FSM+baseline render, one production-renderer pass) and per-strength replay render logged separately; encode is interleaved with render (VideoWriter), so `encode_s` is null by design rather than restructuring frozen code to split it.
- Output MP4s burned-in top-left strength label so review clips can't be confused.
- `.github/workflows/flatcam-strength-test.yml` — CPU-only Vast.ai `workflow_dispatch`; lifecycle/SSH/deps/Drive-oauth blocks are a verbatim string-substitution build off `flatcam-render.yml` (36 changed lines total: name, inputs incl. `start_sec`/`end_sec`/`strengths`, code-upload list, run command, artifact copy). YAML-validated. Inputs default `start_sec=115`, `end_sec=145`, `strengths=0.0,0.3,0.5` (dispatch this session used `0.0,0.1,0.2,0.3,0.5`).

**Verified locally** (synthetic 4K60-class clip + real St Margaret's venue polygon, not yet on real GX010424 footage): scene target preserved across strengths on extracted frames; inversion residual ≤1.5px; `v_cover` and `corner_err_px` behave as described above at 0.1/0.3/0.5.

**Merged to main:** `895fcd2` (2 files, +620/-0, feature branch `flatcam-strength-segment-test`). Follow-up fix `bb62ec9` (direct to main): `trim_segment` switched from libx264 re-encode to stream-copy — the Vast.ai CPU image's ffmpeg (4.3, conda build) rejects `-preset` outright; copy sidesteps codec options and is faster. Not a frozen-file change.

**Dispatch history:** run `29091492838` failed at SSH wait (infra flake, instance never answered within 90s, clean termination, no leak) → redispatched, run `29091788470` reached the actual script and failed on the `-preset` defect above → fixed, redispatched, run `29102830220` **SUCCEEDED**, all 13 steps green.

**Run `29102830220` — real GX010424 footage, 115–145s segment, strengths 0.0/0.1/0.2/0.3/0.5.** Artifact `flatcam-strength-test-29102830220` (417.3 MB): 5× labelled MP4 (each 29.996s, duration-matched, non-corrupt), 4× per-strength CSV, `timings.json`. Diagnostics transferred from synthetic test as expected: `v_cover` 0.988–1.011 across all strengths (vertical framing holds); `corner_err_px` grows with strength as predicted — max 2.3% of crop width at 0.1, 5.0% at 0.2, 8.2% at 0.3, 16.4% at 0.5. Timing: path-compute (production renderer, one pass, decode+detector+FSM+baseline render) 237s; each replay render ~70–72s; label pass 16s. Total wall time this run ≈ 6–7 min of instance time.

**Next gate:** Johnson watches the 5 renders in `flatcam-strength-test-29102830220` and judges: (1) pitch lines/fences straighter, (2) players look natural, (3) no visible pan acceleration/distortion near edges, (4) runtime acceptable (all four are visual/subjective calls — no numeric threshold applies here). Corner-drift caveat above applies most at 0.3–0.5, worth a deliberate look at frame edges on those two. Verdict decides: pick a strength for `lens_profiles.json` (still frozen, needs explicit approval to touch) vs reopen the archived rectilinear-renderer design vs stay at 0.0.

## Runner failsafe — GitHub-hosted fallback for GoPro pipeline (2 Aug 2026)

**Trigger:** `vultr-ffa` self-hosted runner offline since 28 Jul (second outage in a month, first was ~30 Jun). 18-run Actions backlog. GoPro Cloud Scanner (self-hosted, hourly) is the dispatcher for GoPro Upload (already `ubuntu-latest`, unaffected) — scanner being down is what stalled a week of session uploads, not the uploader itself.

**Merged to main** `afa9ff6c` (feature branch `feature/runner-failsafe`, built directly in this session per explicit routing override, not Codex):
- `gopro-scanner.yml` — added `runner` workflow_dispatch choice input (`self-hosted` default / `ubuntu-latest`), `runs-on: ${{ inputs.runner || 'self-hosted' }}`. +9/-1.
- `runner-failsafe.yml` (new) — hourly cron `15 * * * *`: checks `vultr-ffa` status via API; if offline and no scanner run `in_progress`, cancels queued scanner runs and dispatches scanner with `runner=ubuntu-latest`. Uses existing `GH_PAT` secret, no new secrets.

**Defect found post-merge — NOT WORKING, unresolved:** every `ubuntu-latest` fallback dispatch (manual: run `30718071559`; failsafe-triggered: `30719755839`, `30720767893`) sits in status `pending` with **zero jobs ever created** (`/actions/runs/{id}/jobs` → `total_count: 0`), then gets cancelled by the *next* hourly failsafe pass as "stale queued," which redispatches a fresh one — a self-perpetuating cancel/redispatch loop that never actually scans. Confirmed NOT a billing/capacity issue: repo is public (`private: false`), `actions/permissions` shows `enabled: true, allowed_actions: all`. `pending` (distinct from `queued`) points to a run being blocked pre-job-assignment — likely a repo/org Actions setting (e.g. required approval for `workflow_dispatch`) not visible via REST API with this PAT's scope, or a concurrency-group interaction. **Unconfirmed — needs Settings → Actions → General checked in the GitHub UI**, specifically "Require approval for workflow runs" and the fork/first-time-contributor approval settings, plus whether `gopro-scanner`'s `concurrency: group: gopro-scanner, cancel-in-progress: false` is somehow blocking despite no run showing `in_progress`.

**Current live impact:** `runner-failsafe.yml` is active in production (hourly cron) and is currently looping — cancelling and redispatching a scanner run every hour with no effect. It is not causing damage (no compute cost, no data risk) but is not fixing the backlog either. Backlog as of this session: still 0 successful GoPro Upload runs since 27 Jul 02:01 UTC.

**Not yet started:** Clip Extractor and XbotGo pipelines have no failsafe — assessed non-portable to `ubuntu-latest` (Clip Extractor needs the VM's live Chrome cookie profile; XbotGo needs local SQLite `xbotgo.db`) and were intentionally excluded from this ticket's scope.


## Runner failsafe consolidated (2 Aug 2026) — loop fixed, real blocker found

**Merged to main** `319edac3` (branch `fix/consolidate-runner-fallback`, built directly, not Codex): `gopro-scanner.yml` restructured into two jobs in one file — `check-runner` (always `ubuntu-latest`, checks `vultr-ffa` status via API, outputs the target runner) → `scan` (`needs: check-runner`, `runs-on: ${{ needs.check-runner.outputs.runner }}`). Standalone `runner-failsafe.yml` deleted (logic absorbed). `workflow_dispatch` `runner` input changed to `auto`/`self-hosted`/`ubuntu-latest` (was `self-hosted`/`ubuntu-latest`).

**Confirmed working:** dispatch `30750571938` — both jobs `completed`/`success` on `ubuntu-latest`, no more cancel-loop (previous two-file design self-cancelled every hour, see prior section above — 8+ failed loop iterations before this fix).

**New blocker found, unresolved:** the scan itself failed cleanly with `401 Unauthorized` from GoPro's API — cookies expired (unsurprising after a week with no successful scan to refresh them). Cookie refresh happens via `cookie-refresh.yml`, which is **also `runs-on: self-hosted`** (needs a live browser login, same category of VM-dependency as Clip Extractor's Chrome profile — not portable to GitHub-hosted without storing GoPro login credentials in Actions secrets and building a headless-login flow, which is a materially bigger, credential-sensitive task not yet scoped or approved).

**Net effect:** the GitHub-hosted fallback is now structurally correct, but the GoPro pipeline (scan → upload) cannot fully recover until either (a) `vultr-ffa` comes back online and successfully refreshes cookies, or (b) Johnson explicitly approves scoping a cookie-refresh fallback (bigger, credential-sensitive ticket). Backlog as of this session: still 0 successful GoPro Upload runs since 27 Jul 02:01 UTC.

## OEV — GoPro Cloud → Drive uploader + trim pipeline (6–8 Aug 2026)

**New files, merged to main:**
- `oev_drive_uploader.py` (`3dac978993`, updated `9f1866253e`) — downloads a GoPro Cloud video by `media_id` or exact filename, uploads directly to OEV Drive folder (`18Y8hI_S29BMeg5FEoxlaqoGy1DsQ7GKJ`), skips if same-named file already present. Reuses `gopro_uploader.py`'s auth/download helpers via import, no duplication. No YouTube step, no `uploaded.db`. Downloads to `/mnt/oevdata` when present, falls back to `gu.DOWNLOAD_DIR` otherwise.
- `.github/workflows/oev-drive-upload.yml` (`340c17bb64`, switched to self-hosted `c42ef2d7d9`) — `workflow_dispatch` with `media_id`/`gopro_filename` inputs, `runs-on: [self-hosted, vultr]`. Same GoPro-cookie-validate/refresh-dispatch pattern as `gopro-upload.yml`.
- `oev_trim_clip.py` (`147c1a5694`) + `.github/workflows/oev-trim-clip.yml` (`4f14e6b903`) — pulls a named clip already in the OEV Drive folder, `ffmpeg -ss <offset> -t <duration> -c copy` trim (defaults 10:00 offset / 45s duration), uploads trimmed result to a `Trimmed/` subfolder (auto-created) inside the OEV Drive folder. Runs on `vultr-ffa`, scratch space `/mnt/oevdata`.
- `.github/workflows/cookie-refresh.yml` fix (`9762ddb209`): YouTube-token commit retry changed from flat 3s×5 to exponential backoff (`2**attempt`, matches the proven `uploaded.db` pattern) — was 409-conflicting on every single run since 20 Jun due to high commit volume on `main`. Not yet verified against a live cookie-refresh run.

**Infra:** `vultr-ffa` had only 7.3G free on its 52G root disk (cause not fully diagnosed — `_work`, chrome profile, downloads dirs didn't account for the ~42G used). Attached a pre-existing 40GB Vultr Block Storage volume (region-matched to `vultr-ffa`, Atlanta) instead of debugging further: partitioned (`vdb1`, GPT), `mkfs.ext4`, mounted at `/mnt/oevdata`, `UUID=346edaa2-8b9d-4807-9cba-4d26811adc25` in `/etc/fstab`, owned `runner:runner`. Second identical 40GB volume still sits unattached/spare.

**Context:** this work happened during a live GitHub Actions platform incident (started 6 Aug ~15:22 UTC) — hosted-runner (`ubuntu-latest`) jobs were stuck `queued` with zero jobs assigned. Switching the OEV workflows to the self-hosted `vultr-ffa` runner successfully bypassed it, since self-hosted runner dispatch went through even while hosted-runner assignment was stuck.

**Verified runs — uploads (`oev-drive-upload.yml`):**
- `GX010197.MP4` — **venue label corrected 2026-08-10: this is St Margaret's, not Aylestone** (Johnson confirmed; see M1 cylindrical-stereo section below — the "Aylestone avoids St Margaret's sun/exposure issue" reasoning elsewhere in this doc was therefore built on a mislabeled pair. Not yet reconciled with the earlier exposure-mismatch note; if `reco calibrate` quality on this pair still looks fine despite being St Margaret's, the exposure issue may be less universal than first thought, or specific to a different camera angle at that venue). First attempt (`31128612979`) stuck in GitHub's hosted-runner queue (platform incident), abandoned. Retry on `vultr-ffa` (`31129084980`) failed — `/mnt/oevdata` was `root`-owned, `runner` user got permission denied. Fixed with `chown -R runner:runner /mnt/oevdata`. Redispatch (`31129133117`) succeeded.
- `GX010198.MP4`, `GX010175.MP4` — venue unconfirmed after the correction above; treat as St Margaret's pending Johnson confirmation, not Aylestone. Both succeeded first try (`31150601739`, `31150604634`), post permission fix.
- `GX010173.MP4` — St Margaret's (corrected), the pair to `GX010197`. Never went through this uploader (only `GX010197` was dispatched from that pair, per explicit "try 1 for now"), but was confirmed already present in the OEV Drive folder when the trim workflow ran against it (see below) — uploaded some other way, not tracked here.

**Verified runs — trims (`oev-trim-clip.yml`), all 10:00 offset / 45s duration, all `completed success`:**
`GX010197.MP4` → `31270018554`, `GX010173.MP4` → `31270020775`, `GX010198.MP4` → `31270022821`, `GX010175.MP4` → `31270024820`. Output: `trimmed_GX0101xx.MP4` × 4 in `Trimmed/` subfolder of the OEV Drive folder. **Not yet visually reviewed by Johnson.**

**Known factor, venue-specific (not a general OEV rig issue):** at St Margaret's, one camera faces the sun and the other faces away, causing independent auto-exposure mismatch between the two feeds — this was the likely cause of the earlier weak `reco calibrate` match quality (8–19 features/frame) on that footage. Aylestone reshoots (this session's 4 files) don't have this issue per Johnson. Not yet a formal repo issue; worth filing when someone picks up rig/exposure work, with fixed-exposure (Protune) or preprocessing histogram-matching as candidate fixes.

**Decision this session:** `reco calibrate` (CPU, AKAZE-based, no GPU dependency per repo's own architecture docs) and `reco stitch` (GPU-first, `wgpu` rendering engine) will be run together on Vast.ai rather than splitting calibrate off onto `vultr-ffa`/GitHub-hosted — explicit call to avoid pipeline complexity, even though calibrate alone could run without GPU.

**Superseded below** — see "OEV — reco-cli build attempt on Vast.ai GPU" for what happened when this was actually attempted.

## OEV — reco-cli build attempt on Vast.ai GPU: 3 issues found, blocked on FFmpeg/ffmpeg-next version mismatch (8 Aug 2026)

**New files, merged to main** (feature branch `feature/oev-reco-stitch`, built directly by Claude per explicit routing override, not Codex):
- `oev_reco_stitch_remote.sh` — bash script uploaded to and run on the Vast.ai instance: builds `reco-cli` from source (git clone + `cargo build --release -p reco-cli`), then runs `reco calibrate` + `reco stitch` against a pre-downloaded clip pair. Every stage writes its own log (`env.log`/`build.log`/`calibrate.log`/`stitch.log`).
- `.github/workflows/oev-reco-stitch.yml` — Vast.ai GPU `workflow_dispatch` (`left_clip`/`right_clip` inputs, default `trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`). Lifecycle block (launch-retry/offer-selection/delete_instance) copied verbatim from `playcam-poc.yml` per CLAUDE.md; offer query relaxed vs playcam (`gpu_ram>=4096`, `cpu_cores>=8`, `cpu_ram>=8192`) since reco calibrate/stitch is far lighter than YOLO. Resolves clip file IDs in the OEV Drive `Trimmed/` folder, downloads via aria2c, runs the remote script, pulls back all logs regardless of outcome (uploaded as run artifact `oev-reco-stitch-{run_id}`), uploads `panorama.mp4`+`match.json` to a new `Stitched/` Drive folder only if stitch succeeded.

**Issue 1 — logging was silently lost (fixed, commit `d8c13c9e43`):** original script used `>> file 2>&1` redirects, which don't reliably flush before the SSH session ends — the first run (`31273809221`) failed at the cargo build step but `build.log` only captured one line ("Cloning into..."), the actual compiler error was never written anywhere. Fixed by switching every long-running command to `2>&1 | tee -a file` (exit codes still correct — `pipefail` already set). This fix is validated: subsequent runs captured full build output including real errors.

**Issue 2 — missing FFmpeg dev packages (fixed, commit `f21f1bd496`):** with logging fixed, run `31275779314` showed the real error — `ffmpeg-sys-next` (a `reco-cli` dependency) failed because `libavutil.pc` wasn't found; the `ffmpeg` apt package installs runtime libs only, not the `-dev`/pkg-config files needed to build against. Added `libavutil-dev libavcodec-dev libavformat-dev libswscale-dev libavdevice-dev libavfilter-dev libswresample-dev` to the apt install list — this exactly matches the prerequisite list in reco-project's own README, confirming the fix is correct per upstream docs.

**Issue 3 — BLOCKED, not a config problem: `Pixel::VAAPI` missing from `ffmpeg-next`'s enum (run `31276633754`, unresolved):** with issues 1–2 fixed, the build got much further — all of `wgpu`/`naga`/`reco-core` compiled successfully, then failed compiling `reco-io` with 6 instances of `error[E0599]: no variant or associated item named 'VAAPI' found for enum 'Pixel'` (in `crates/reco-io/src/ffmpeg/encoder.rs` and `hw_upload.rs`). This is a version/API mismatch between what `reco-io`'s VAAPI hardware-encode path expects from the `ffmpeg-next` crate and what's actually available when `ffmpeg-next` is built (via bindgen) against this image's FFmpeg libraries — the base image (`pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime`) ships Ubuntu 20.04's FFmpeg 4.2.7, likely older/different from whatever FFmpeg version reco-project's own CI builds and tests against. Not fixable by installing more packages — this is a genuine toolchain/dependency compatibility wall.

**RESOLVED (8 Aug 2026, fresh chat): base image swap.** Checked `reco-io/Cargo.toml` on `reco-project/video-stitcher` — no feature flag to disable VAAPI; it's unconditional inside the default `ffmpeg` feature (`ffmpeg-next = "8"`), so path 2 (feature flag) is dead. Path 1 confirmed: their CI pins FFmpeg n7.1.4 (Windows) / builds on `ubuntu-latest` (24.04 → FFmpeg 6.1); `AV_PIX_FMT_VAAPI` only became a real enum member (not a `#define` alias) in FFmpeg 5.0, so Ubuntu 20.04 (4.2.7, the old base image) and 22.04 (4.4.2) both fail — 24.04 is the floor.

**Fix @ `e9fda96` (Claude-authored direct build, explicit routing override, merged to main via `4fa3e22`, feature branch `fix/oev-reco-stitch-ubuntu24-image` deleted after merge):** `.github/workflows/oev-reco-stitch.yml` only, single-line change — Vast.ai launch `'image'` field: `pytorch/pytorch:2.1.0-cuda11.8-cudnn8-runtime` → `nvidia/cuda:12.4.1-devel-ubuntu24.04`. Diff verified against `main` before merge: exactly 1 file, 1 hunk. `oev_reco_stitch_remote.sh` untouched — it already carries the Vulkan packages (`mesa-vulkan-drivers vulkan-tools libvulkan1`) and a `vulkaninfo --summary` line in `env.log`, added in an earlier session; this was mistakenly re-proposed as new work before checking the live file, then corrected.

**Verification run `31277876978`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31277876978 — head commit `4fa3e22`. Inputs: `left_clip=trimmed_GX010197.MP4`, `right_clip=trimmed_GX010173.MP4`.

**Known open risk, not yet evidenced either way:** `reco stitch` renders via `wgpu` (Vulkan), not CUDA. Dropping the PyTorch base image is fine since nothing depends on it, but whether the Vast.ai host's NVIDIA ICD is exposed into a plain CUDA-devel container is unconfirmed — the `vulkaninfo --summary` line in `env.log` will show this on the same run rather than requiring a separate cycle.

**Run `31277876978`: FAILED — new issue, not the VAAPI wall.** Launch step failed: 4 of 5 tried offers stuck in `status=loading` for the full 5-minute/30-poll window, never reached `running`; 1 offer got instant `HTTP 400 Bad Request`. 43 eligible offers existed, so this wasn't a supply problem. Root cause: `nvidia/cuda:12.4.1-devel-ubuntu24.04` (~9GB) is far less commonly pre-cached across Vast.ai's host fleet than the original `pytorch/pytorch` image, so hosts had to pull it cold and blew past the poll window before the build step was ever reached.

**Fix @ `a875b03` (Claude-authored direct build, explicit routing override, merged to main via `926035a`, feature branch `fix/oev-reco-stitch-plain-ubuntu-image` deleted after merge):** `.github/workflows/oev-reco-stitch.yml` only, single-line change again — `'image'` field `nvidia/cuda:12.4.1-devel-ubuntu24.04` → `ubuntu:24.04`. Reasoning: the build never uses CUDA toolkit (no `nvcc`, no CUDA compile step in `oev_reco_stitch_remote.sh`) — GPU access comes from Vast.ai's host driver passthrough regardless of base image, so a plain, tiny, near-universally-cached Ubuntu 24.04 image should still provide FFmpeg 6.1 via apt while avoiding the cold-pull stall. Diff verified against `main` before merge: exactly 1 file, 1 hunk.

**Open risk, unverified either way:** whether Vast.ai's NVIDIA/Vulkan driver injection works against a non-`nvidia/*`-labeled base image on this host config. The existing `nvidia-smi` and `vulkaninfo --summary` lines in `env.log` will show this on the same run.

**Verification run `31279245133`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31279245133 — head commit `926035a`. Inputs: `left_clip=trimmed_GX010197.MP4`, `right_clip=trimmed_GX010173.MP4`.

**Run `31279245133`: PARTIAL SUCCESS — build + calibrate PASSED, stitch FAILED with a new (third) root cause.** Launch reached `running` fast (plain `ubuntu:24.04` launch-stall risk did not materialize). `nvidia-smi` succeeded in `env.log` (`Driver Version: 580.159.03, CUDA Version: 13.0` — host driver visible). `cargo build` completed clean through `reco-io`/`reco-cli` in ~10 min — **VAAPI wall is confirmed fully resolved**, no `Pixel::VAAPI` errors. `vulkaninfo --summary` **did fail** (`Could not get 'vkCreateInstance' via 'vk_icdGetInstanceProcAddr' for ICD libGLX_nvidia.so.0`; `XDG_RUNTIME_DIR is invalid or not set`) but this did not block calibrate.

`reco calibrate` **PASSED**, `GX010197`/`GX010173` pair: 138 total matched points across 2/2 frame pairs, confidence 100.0%, median_error 0.000104, angular_error 0.44. `match.json` written. (Frame extraction logged non-fatal CUDA decode fallback: `CUDA_ERROR_NO_DEVICE` on hevc hwaccel init, fell back to `llvmpipe` CPU Vulkan renderer successfully — calibrate doesn't need real GPU per repo's own architecture docs, confirmed in practice here.)

`reco stitch` **FAILED (exit 3)**: `Error: source: source init (left.mp4): left[0] Y: CUDA error 201 in cudaGetDevice`. Root cause: unlike calibrate, stitch's video source-init path hard-requires a working CUDA runtime (`cudaGetDevice`) with no software fallback. Plain `ubuntu:24.04` has no CUDA runtime libraries at all (only the host driver is visible via `nvidia-smi`, which talks to the kernel driver directly and doesn't need `libcudart`) — so this is a direct consequence of the previous fix (dropping the CUDA image to solve the cold-pull stall also dropped CUDA runtime entirely). `panorama.mp4` not produced; `match.json` from calibrate is valid and preserved in artifact `oev-reco-stitch-31279245133` (85KB, 5 files: env/build/calibrate/stitch logs + match.json).

**Fix @ `72ca07c` (Claude-authored direct build, explicit routing override, merged to main via `1b612ad`, feature branch `fix/oev-reco-stitch-cuda-runtime-image` deleted after merge):** `.github/workflows/oev-reco-stitch.yml` only, single-line change — `'image'` field `ubuntu:24.04` → `nvidia/cuda:12.4.1-runtime-ubuntu24.04`. Diff verified against `main` before merge: exactly 1 file, 1 hunk.

**Verification run `31281123397`: FAILED — all 5 offers rejected the same way as the `-devel-` attempt.** 4 of 5 offers stuck in `status=loading` for the full 5-min/30-poll window; 1 offer (`31323635`) got instant `HTTP 400 Bad Request`. **Conclusion: the `-runtime-` vs `-devel-` size difference was not the deciding factor** — any `nvidia/cuda:*-ubuntu24.04`-tagged image appears poorly cached across Vast.ai's host fleet, regardless of variant size. Plain `ubuntu:24.04` and `pytorch/pytorch` both launch fast; anything tagged `nvidia/cuda` does not, on this fleet, at this time.

**Fix @ `518d025` (workflow) + `22dc64a` (script), Claude-authored direct build, explicit routing override, merged to main via `ac467b2`, feature branch `fix/oev-reco-stitch-cuda-apt-install` deleted after merge): stop chasing pre-built images — install CUDA runtime via apt instead.** `.github/workflows/oev-reco-stitch.yml`: image reverted to `ubuntu:24.04` (known fast/reliable launch). `oev_reco_stitch_remote.sh`: added a step after the existing system-deps apt install — downloads NVIDIA's `cuda-keyring_1.1-1_all.deb` for `ubuntu2404`, `dpkg -i`, `apt-get update`, `apt-get install cuda-runtime-12-4` (non-fatal on failure, logged to `env.log` either way so a failure here is visible without blocking the rest of the run). Also added `ca-certificates wget` to the existing apt list (needed for the `wget` fetch of the keyring `.deb`). This keeps the fast-launching image and does the CUDA-specific work *after* the instance is already running (inside the script, already `tee`'d/logged), sidestepping the image-pull-time bottleneck entirely. Diff verified against `main` before merge: exactly 2 files, 2 hunks, nothing else touched.

**Verification run `31282877306`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31282877306 — head commit `ac467b2`. Inputs: `left_clip=trimmed_GX010197.MP4`, `right_clip=trimmed_GX010173.MP4`.

**Verification run `31282877306`: FAILED — apt package name wrong, same `cudaGetDevice` error as a result.** Launch was fast (plain `ubuntu:24.04`, confirmed reliable). Build succeeded (VAAPI wall remains resolved). But `env.log` shows: `E: Unable to locate package cuda-runtime-12-4` — the `cuda-runtime-12-4` package no longer exists in NVIDIA's Ubuntu 24.04 repo (their published package versions have moved on since that number was chosen to loosely match the earlier devel image tag; NVIDIA's own web index now shows CUDA 13.x-numbered packages for `ubuntu2404`). Because the install step was written to fail non-fatally (by design, so it wouldn't block the rest of the run and would be visible in the log instead), the script continued anyway with no CUDA runtime present — so `reco stitch` hit the identical `Error: source: source init (left.mp4): left[0] Y: CUDA error 201 in cudaGetDevice` as run `31279245133`. Calibrate not re-confirmed this run (log inspection stopped once the same stitch failure was identified) but build/launch health confirmed clean.

**Fix @ `6c2f321` (Claude-authored direct build, explicit routing override, merged to main via `1d9d629`, feature branch `fix/oev-reco-stitch-cuda-pkg-resolve` deleted after merge): stop hardcoding a CUDA package version — resolve it dynamically.** `oev_reco_stitch_remote.sh` only. Tries the unversioned `cuda-runtime` meta-package first; if that 404s, falls back to `apt-cache search '^cuda-runtime-[0-9]'`, `sort -V`, install the highest match found live in the repo. Diff verified against `main` before merge: exactly 1 file, 1 hunk.

**Verification run `31283387937`: FAILED — unrelated transient infra flake, not a code/config issue.** Launch succeeded fast (RTX 4080, 16GB, plain `ubuntu:24.04`). But the "Wait for SSH" step (18 tries × ~7s ≈ 2.5 min) never got a response — Vast.ai reported the instance `running` before its SSH daemon was actually reachable. No log content beyond repeated `SSH not ready (n/18)` — ordinary cloud cold-start flakiness, not caused by any recent change. Nothing to fix; redispatch is the correct response.

**Storage/caching question (Johnson): still explicitly deferred until a run succeeds.** See above — build-once/cache-binary is the right shape, not a Vast.ai storage volume, but do not implement until `panorama.mp4` is actually produced by a successful run.

**Verification run `31283699393`: FAILED — same `cudaGetDevice` error, but this run FALSIFIES the "missing library" theory.** SSH came up fine this time (RTX 3090, 24GB — pure infra roll of the dice from the previous flake, unrelated). Dynamic CUDA package resolution **worked exactly as designed**: unversioned `cuda-runtime` 404'd as expected, fell back to `apt-cache search`, found and installed `cuda-runtime-13-3` cleanly (`cuda-cudart-13-3`, `cuda-libraries-13-3`, etc. all set up without error — confirms NVIDIA's repo has moved to CUDA 13.x numbering as of Aug 2026). Calibrate **PASSED again** (100% confidence, same quality as the first successful calibrate). But `reco stitch` hit the **identical** `Error: source: source init (left.mp4): left[0] Y: CUDA error 201 in cudaGetDevice`.

**Re-diagnosis: root cause was never a missing library.** With `libcudart` now genuinely present and correctly installed, the exact same failure persisting means the container simply isn't being granted real GPU device access at the container-runtime level — `nvidia-smi` succeeding is not sufficient evidence of this (it's a simpler query tool), and CUDA context creation (error 201 = `CUDA_ERROR_INVALID_CONTEXT` in the CUDA driver API numbering) requires actual device-file/driver-library injection into the container that `nvidia-smi` doesn't require. NVIDIA's own official `nvidia/cuda:*` images set `NVIDIA_VISIBLE_DEVICES` and `NVIDIA_DRIVER_CAPABILITIES` as `ENV` directives baked into the image, which is what told the Vast.ai host's container runtime what to inject — this is likely *why* those images (when they did launch) got further than plain images on the GPU-access front, even though they failed differently (VAAPI/launch-stall). Plain `ubuntu:24.04` has no such `ENV` metadata, and the workflow's own launch request has an `'env': {}` field — currently empty — that Vast.ai's launch API supports for passing exactly this kind of container environment/capability config.

**Fix @ (pending), `.github/workflows/oev-reco-stitch.yml` only: populate the launch request's `env` field.** Add `NVIDIA_VISIBLE_DEVICES: all` and `NVIDIA_DRIVER_CAPABILITIES: all` to the `env: {}` dict already present in the Vast.ai launch request. No image swap — keeps the fast/reliable `ubuntu:24.04` base and the now-working dynamic CUDA package install; this should give the container the device access it's currently missing.

**Fix @ `541790f` (Claude-authored direct build, explicit routing override, merged to main via `d340542`, feature branch `fix/oev-reco-stitch-nvidia-env-vars` deleted after merge): populated the launch request's `env` field.** `.github/workflows/oev-reco-stitch.yml` only. Added `NVIDIA_VISIBLE_DEVICES: all` and `NVIDIA_DRIVER_CAPABILITIES: all` to the previously-empty `'env': {}` dict in the Vast.ai launch request. Diff verified against `main` before merge: exactly 1 file, 1 hunk.

**Verification run `31297428378`: FAILED — unrelated SSH cold-start flake, same pattern as before, unverified fix.** Redispatched with no code change, same head commit `d340542`.

**Verification run `31298065184`: FAILED — env var fix did NOT resolve the issue.** SSH came up fine. CUDA package install worked cleanly again (`cuda-runtime-13-3`). But **identical error persists**: `Error: source: source init (left.mp4): left[0] Y: CUDA error 201 in cudaGetDevice`. Critically, `reco_core::gpu` log line shows `Selected GPU: llvmpipe (LLVM 20.1.2, 256 bits) (Vulkan)` — the *software* CPU Vulkan renderer, not the actual NVIDIA GPU — meaning real GPU device access still isn't reaching the container even with `NVIDIA_VISIBLE_DEVICES`/`NVIDIA_DRIVER_CAPABILITIES` set in the launch request's `env` field.

**Docs check (9 Aug 2026, fresh chat): `env` field confirmed to be the right mechanism — the risk above is RESOLVED as a non-issue.** Vast.ai's own API reference confirms instance-creation `env` is a plain JSON dict of key-value environment variables (port mappings use `-p ...` keys valued `"1"`), which is exactly the shape used in the launch request. So `NVIDIA_VISIBLE_DEVICES`/`NVIDIA_DRIVER_CAPABILITIES` were reaching Vast's API correctly — the "wrong field" theory is dead.

**Re-diagnosis, evidence-based:** `NVIDIA_DRIVER_CAPABILITIES` controls which driver libraries get *mounted* into the container per-capability — `utility` (nvidia-smi/NVML) is the default when unset, `compute` is needed for CUDA, `graphics` for OpenGL/Vulkan, `video` for hw video codec. The prior run's exact error (`Could not get 'vkCreateInstance' via 'vk_icdGetInstanceProcAddr' for ICD libGLX_nvidia.so.0`) is the canonical signature of the Vulkan ICD manifest being present but the matching NVIDIA GL driver library (`libnvidia-gl-<version>`, must match host driver series — host is `580.159.03` per earlier `nvidia-smi` output) not being installed inside the container. This explains every symptom together: `nvidia-smi` works (utility only needs the base driver, not graphics libs), Vulkan/CUDA don't (graphics/compute libs never landed in the image).

**Fix @ `81d60a3` (Claude-authored direct build, explicit routing override, merged to main via `acd4fce`, feature branch `fix/oev-reco-stitch-gpu-diag` deleted after merge):** `oev_reco_stitch_remote.sh` only, additive, inserted after the existing CUDA-runtime install block and before the Rust install block. Two new non-fatal blocks, same `tee -a env.log` logging convention as the rest of the file:
1. Diagnostic dump — `/proc/1/environ` grepped for `NVIDIA` (confirms whether our env vars actually reached the container's init env), `ls /dev/nvidia*` (device-node presence), Vulkan ICD manifest dirs, and a filesystem-wide `libGLX_nvidia.so*` search.
2. Candidate fix — searches the already-added CUDA apt repo for the latest `libnvidia-gl-*` package (same `apt-cache search | sort -V | tail -1` idiom as the existing CUDA-runtime block), installs it plus the matching `libnvidia-decode-<version>` if found, logs whichever version actually got installed (or that none was found).
Diff verified against `main` before merge: exactly 1 file, 32 additions, 0 deletions — purely additive, nothing else touched.

**Run `31305380382`: FAILED — infra flake (SSH connection dropped mid-run, unrelated to code).** Redispatched.

**Run `31306284887`: FAILED — `ldconfig` fix falsified.** Diagnostic dump captured cleanly: `NVIDIA_VISIBLE_DEVICES` read back as `void` inside the container (confirms Vast bind-mounts NVIDIA files directly rather than using the standard nvidia-container-toolkit hook — that hook is what normally resolves `NVIDIA_VISIBLE_DEVICES`/`NVIDIA_DRIVER_CAPABILITIES` into actual device injection). `vulkaninfo` re-checked immediately after `ldconfig` — identical `vk_icdGetInstanceProcAddr` failure. The library file (`libGLX_nvidia.so.610.43.02` that run) was already in a path `ldconfig` indexes by default, so a stale cache was never the real problem. Theory dead.

**Fix @ `dfc571c`/merge `f74ef16` (Claude-authored direct build, explicit routing override): replaced the failing `apt-get install libnvidia-gl-*` approach with `apt-get download` + `dpkg-deb -x` manual extraction**, plus an `ldd` diagnostic on the actual `libGLX_nvidia.so.<ver>` file before/after. Rationale: the earlier `apt-get install` attempts were failing on `dpkg: unable to make backup link ... Invalid cross-device link` — dpkg's atomic-replace-via-hardlink logic doesn't work across Vast's bind mounts. `dpkg-deb -x` sidesteps dpkg's install machinery entirely. Also added a `reco stitch --help` capture right after build succeeds, to check for a software-decode flag (there isn't one — see below).

**Run `31307564057`: FAILED — infra flake (SSH connection dropped ~4 min into the script, unrelated to code).** Redispatched.

**Run `31308807543`: SUCCESS — M1 clip produced.** https://github.com/JhnsonO/ffa-automations/actions/runs/31308807543 — head commit `f74ef16`, artifact `oev-reco-stitch-31308807543` (12.9MB). The `ldd` diagnostic revealed the "missing helper library" theory was also wrong: every dependency of `libGLX_nvidia.so.580.126.20` (this run's host) was already resolving correctly *before* the fix ran. So the Vulkan/graphics-library question and the actual blocker were never the same thing. `vulkaninfo` still failed all three checks this run (`llvmpipe` throughout) — but **`reco stitch` completed anyway**, on CPU rendering: 2,696 frames in 62.0s (43.5 fps) → `panorama.mp4` written, uploaded to OEV Drive `Stitched/` folder: `stitched_trimmed_GX010197.MP4` → https://drive.google.com/file/d/1iiMuUe9ZVMd9lZpjwKBp1pVXvfufJSiB (+ `match_trimmed_GX010197.MP4.json`). Best available explanation: the earlier `cudaGetDevice` hard-failure (exit 3) was fixed by the driver package finally landing correctly via `apt-get download`+extract (likely `libcuda.so` from `libnvidia-compute-<ver>`, distinct from the Vulkan/GL question) — not by anything related to Vulkan/`llvmpipe`, which remains unresolved but is evidently not blocking. **GPU-access debugging is DONE for M1 purposes** — pipeline works end-to-end via CPU render fallback. Real-GPU Vulkan rendering (for speed) is a separate, lower-priority follow-up, not a blocker.

**Visual defect found in the produced panorama (Johnson, reviewing `stitched_trimmed_GX010197.MP4`):** two black wedge-shaped gaps at the seam (top-center and bottom-center), plus the extreme edges of both camera views are not captured in the output. Root cause found directly in the calibrate log for this run: lens auto-detect failed — `telemetry extraction failed: telemetry parse error: Unsupported file format` — and silently fell back to a **generic Mobius 4K action-cam lens profile** (chosen only because the resolution matched 3840x2160), not a GoPro profile. Wrong distortion/FOV model → bad seam warp + clipped edges.

**Two-part fix, both scoped this session:**
1. **Immediate workaround** (not yet applied — needs Johnson's camera info, now have it: **GoPro Hero 10, Wide mode**): `reco calibrate` supports `--left-profile <path.json> --right-profile <path.json>` (confirmed in `crates/reco-cli/src/calibrate.rs`) to bypass auto-detect with an explicit Gyroflow-format lens profile from the embedded database (9,794 profiles / 1,687 cameras). GoPro FOV modes are explicitly modeled in `crates/reco-control/src/gopro/constants.rs`: `VideoLens { Wide=0, Linear=4, SuperView=3, Narrow=2 }` — confirms "wrong lens mode" was the right instinct. **Not yet done:** find/confirm the exact Hero 10 Wide-mode profile filename in the lens database and wire `--left-profile`/`--right-profile` into `oev_reco_stitch_remote.sh`'s calibrate call.
2. **Root-cause fix @ `85aaf03`/merge `29d58db` (Claude-authored direct build): `oev_trim_clip.py` — added `-map 0 -copy_unknown` to the ffmpeg trim command.** Previously `-c copy` with no `-map` let ffmpeg's default stream selection silently drop GoPro's GPMF telemetry track (stored as a `data` stream, not auto-selected) — this is exactly why auto-detect's telemetry read failed on every trimmed clip. Diff verified: 1 file, 1 hunk. **UNVERIFIED** — needs a fresh trim (`oev-trim-clip.yml`) + calibrate run to confirm telemetry now parses and auto-detect picks a real GoPro profile without needing the manual `--left-profile` workaround at all.

**Also noted, not yet acted on:** `crates/reco-control/src/gopro/constants.rs` has a comment "HyperSmooth stabilization. Must be OFF for stereo stitching." — worth confirming HyperSmooth was off during filming; if not, that's a second contributing factor independent of the lens-profile fix.

**Next (fresh chat — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Re-run `oev-trim-clip.yml` on `GX010197`/`GX010173` (picks up the `-map 0` fix), then re-run `oev-reco-stitch.yml` on the freshly-trimmed pair. Check calibrate.log: does lens auto-detect now find a real GoPro Hero 10 profile instead of falling back to Mobius?
2. If auto-detect still fails: fall back to the manual `--left-profile`/`--right-profile` workaround — need to find the correct Hero 10 + Wide-mode profile filename in the embedded lens database (may need a quick `reco calibrate --help` or a lens-database listing command; not yet checked) and wire it into `oev_reco_stitch_remote.sh`.
3. Once seam/edges look correct: Johnson visual sign-off on `GX010197`/`GX010173`, then repeat on second pair (`GX010198`/`GX010175`) as a repeatability check = M1 gate cleared.
4. Confirm whether HyperSmooth was on during filming (Johnson to check) — OFF is required per the tool's own doc comment.
5. Deferred, lower priority: real-GPU Vulkan rendering is still broken (stuck on `llvmpipe`/CPU) — works but slower. Investigate only if speed becomes a real constraint; not a blocker for M1.

---

## Session update — 9 Aug 2026

**Re-trim of GX010197/GX010173 (to exercise the `85aaf03` GPMF fix) surfaced a new bug: `-map 0` also pulls in the GoPro `tmcd` timecode track (`codec_type: data`, `codec_name: none`), which the mp4 muxer can't write a tag for — aborts the whole header write before `gpmd` is ever copied.** Confirmed via ffmpeg stderr on runs `31312612675`/`31314072064`: `Could not find tag for codec none in stream #2, codec not currently supported in container`.

**Fix attempt 1 (`46acf02`, merged): CSV-based tmcd exclusion — BROKEN, confirmed by test.** Parsed `ffprobe -of csv=p=0` output by column position to detect and exclude the tmcd stream. Re-run (`31314072064`) showed `excluded_indices` stayed empty (no "Excluding tmcd" log line, ffmpeg still mapped all 4 streams, same failure as before). Root cause of the parse failure not fully diagnosed — moved straight to a more robust approach rather than debugging CSV parsing further.

**Fix attempt 2 (`3a7f2d8`, merged): JSON-based tmcd exclusion — NOT YET VERIFIED.** Switched `ffprobe` to `-of json` + `json.loads()` keyed lookup (`codec_type`/`codec_tag_string`), eliminating column-order ambiguity. Also now logs ffprobe failures/non-JSON output explicitly instead of silently falling through to zero exclusions. No run has exercised this code yet.

**Separate, unrelated issue found and fixed: `/mnt/oevdata` (40GB scratch volume) filled up (27GB used, 11GB free) with orphaned source downloads from failed runs**, since `oev_trim_clip.py` only unlinked `src_path`/`trimmed_path` on full success. This caused one run (`31312611858`) to fail with `OSError: No space left on device` during download — unrelated to the tmcd bug.
- Added `.github/workflows/vultr-oevdata-cleanup.yml` (manual dispatch, clears `*.MP4` at `/mnt/oevdata` root) — run once (`31313979394`), confirmed 27GB → 24KB used.
- `oev_trim_clip.py` (part of `46acf02`): wrapped download/trim/upload in `try/finally` so scratch files are cleaned up on any failure going forward, not just success.

**Run history this session (chronological):**
- `31312611858` (GX010197, pre-fix code) — failure, disk full (stale infra issue, not code)
- `31312612675` (GX010173, pre-fix code) — failure, tmcd bug (this is what surfaced the bug)
- `31313455134`/`31313455968` — cancelled (superseded by cleanup reordering)
- `31313979394` (cleanup workflow) — success, freed 27GB
- `31314072064` (GX010197, `46acf02` CSV-fix) — failure, tmcd bug persisted (CSV parsing didn't detect the stream)
- `31314072874` (GX010173, `46acf02` CSV-fix) — status not confirmed at session end (was `in_progress`, not polled further)

**Debug budget (3 cycles) reached this session — stopping here per protocol.**

---

## Session update — 9 Aug 2026, later session

**JSON-based tmcd fix (`3a7f2d8`) CONFIRMED WORKING.** Re-ran `oev-trim-clip.yml` against `main` (head `9cb2ecc` at dispatch time): runs `31314897953`, `31314901933`, `31314908439` all `completed success`, all three logged `Excluding tmcd stream(s) from map: ['2']`. (Three runs not two — an early `gh.sh dispatch` call reported HTTP 400 but the request went through anyway, so `GX010197` was trimmed twice; harmless duplicate, no cleanup needed.) `docs/ai-project-state.md` updated same session via `9cb2ecc` is now superseded by this section for the tmcd-fix status.

**`oev-reco-stitch.yml` run `31319038358` on the freshly-trimmed pair: FAILED — network stall, not a code/GPU defect.** Launch, SSH, downloads, system-deps, CUDA-runtime install, and NVIDIA driver-library extraction all succeeded as in prior runs (same `libnvidia-gpucomp.so.580.178.04` vs host driver `580.159.03` Vulkan mismatch persists — still not blocking, unchanged from before). Rust toolchain install alone took ~20 min (normally fast) — first sign of a bad-network host. Then `cargo build`'s `av1-avif`/`avif-sample-images` submodule fetch took 16+ min, followed by ~30 min of `argmin` crate download retries against crates.io, all failing with `[28] Timeout was reached (Operation too slow. Less than 10 bytes/sec transferred)`, until cargo gave up after exhausting retries. Instance CPU sat at 0.06% the whole time (confirmed via Vast.ai console) — idle on network I/O, not compute. Total instance time ≈67 min, cost ≈16¢. No code or dependency defect; this specific Vast.ai host had a bad route to crates.io/GitHub.

**Fix @ `012c4dd` (Claude-authored direct build, explicit routing override, pushed directly to main — not Codex): fail-fast network handling.** `oev_reco_stitch_remote.sh` only, purely additive (diff verified: 4 hunks, nothing else touched). Sets `CARGO_NET_RETRY=2` / `CARGO_HTTP_TIMEOUT=15` before the build, and wraps `cargo build` in `timeout 1200` (20 min) with a distinct `FATAL: cargo build timed out after 20min (likely slow-network host)` message on exit 124, separate from the existing generic build-failure message. Rationale: prior successful builds finished in ~10 min, so 20 min gives headroom for a normal run while capping a stalled host at ~20 min instead of 60+. On timeout, the run fails and can be cheaply redispatched onto a different Vast.ai offer rather than waiting out a bad host.

**Verification run `31322462730`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31322462730 — head commit `012c4dd`. Inputs: `left_clip=trimmed_GX010197.MP4`, `right_clip=trimmed_GX010173.MP4`. Status at session end: `in_progress`, not polled to completion.

**New fact worth carrying forward:** the one successful stitch (`31308807543`) confirmed via direct `ffprobe` on the downloaded `panorama.mp4` — output is **1920×1080 @ ~59.94fps**, despite both camera inputs being 4K. Frame rate is preserved; **resolution is downscaled to 1080p**, not combined into a wider 4K+ frame as might be assumed. Not yet checked whether `reco stitch` has an output-resolution flag (`reco stitch --help` not yet run) or whether 1080p is a hard ceiling of the current render path.

**Cost/speed estimate (from the one successful run, single data point):** CPU-fallback (`llvmpipe`) render speed measured at 43.5 fps for 4K60 source. Extrapolated: ~83 min of pure stitch time per 1 hour of 60fps footage, plus ~10 min fixed build overhead (build now cached-adjacent via the fail-fast fix, not literally cached) → roughly 1.5–1.7 hours of Vast.ai instance time per 1 hour of stitched footage at current (CPU-fallback) speed. Real-GPU Vulkan rendering remains unresolved and would likely be faster, but no measured number exists for it.

**Not yet done, deferred (Johnson's explicit "not now, maybe in an hour"):** binary-caching the compiled `reco-cli` build (e.g. to a GitHub Release, keyed by git SHA) to skip the ~10 min `cargo build` on every run. Explicitly NOT extending this to the CUDA runtime/driver library steps — those must stay dynamic since they need to match whatever Vast.ai host is assigned per-run; caching a fixed driver version risks reintroducing the exact class of bug that took 8+ cycles to fix.

**Run `31322462730`: SUCCESS (workflow), but visual output WORSE — Johnson confirmed via screenshot: black seam wedges bigger, more of both camera edges clipped than the first `31308807543` run.** Root cause found in `calibrate.log`: the tmcd/GPMF fix worked as far as it goes — telemetry now parses (`9088 gyro, 9088 accel samples, 2760 quaternions`) — but **lens auto-detect is a separate lookup keyed on camera-model identification, not on telemetry parsing succeeding.** Log shows `telemetry: GoPro unknown ... lens profile: none, FOV: unknown` — GPMF's camera-model string itself isn't coming through — so `detect_profile()`'s primary path (camera ID → database lookup) never had a model to match on, and it fell through to the same `no camera match for 3840x2160` → generic Mobius fallback as before. The tmcd fix and the lens-profile problem were always two separate bugs; fixing telemetry parsing was necessary but never going to fix lens ID.

**Fix @ `5710bff` (Claude-authored direct build, explicit routing override, pushed directly to main — user instructed "make the changes yourself"): pinned explicit `--left-profile`/`--right-profile` in `oev_reco_stitch_remote.sh`, bypassing auto-detect entirely.** Diff verified: 1 file, 18 additions, 1 deletion, purely additive to the calibrate call.
- Profile identified: `reco`'s embedded lens database is built from the public `gyroflow/lens_profiles` GitHub repo (confirmed via `scripts/convert-gyroflow-profiles.py` in `reco-project/video-stitcher`). Found and verified `GoPro/GoPro_HERO10 Black_Wide_16by9.json` — `calib_dimension: 3840x2160`, matches both the camera (GoPro Hero 10) and mode (Wide) Johnson confirmed, and the actual clip resolution.
- Script now `curl`s that profile directly from `raw.githubusercontent.com` into `/tmp/oev_run/hero10_wide_16by9.json` on the Vast.ai host at calibrate time (no binary/JSON asset added to the repo — smallest safe diff), then passes it via `--left-profile`/`--right-profile` to `reco calibrate` (same profile both sides — same camera/mode both cameras).
- **UNVERIFIED** — needs a stitch run against the already-trimmed pair (`trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`) to confirm calibrate picks up the pinned profile (log should show no "falling back to GENERIC" warning) and that the seam/edges in `panorama.mp4` look correct.

**Verification run `31323968907`: DISPATCHED — UNVERIFIED at time of build-cache work below.** https://github.com/JhnsonO/ffa-automations/actions/runs/31323968907 — head commit `5710bff` (pinned lens profile, before the cache changes below). Inputs: `left_clip=trimmed_GX010197.MP4`, `right_clip=trimmed_GX010173.MP4`. Was still `in_progress` on the CUDA-runtime apt-install step (~20+ min in — same step now cached, see below) last checked.

## Build cache (CUDA runtime + reco-cli binary) — 9 Aug 2026

**Implemented @ `b635545` (`oev_reco_stitch_remote.sh`) + `4a17c6d` (`.github/workflows/oev-reco-stitch.yml`), Claude-authored direct build, explicit routing override (user: "Do it directly"). Diff verified: 139/28 and 9/4 lines respectively, both purely additive/scoped to the intended change.**

Two things now cached across runs in a GitHub Release (`JhnsonO/ffa-automations`, tag `oev-build-cache`, auto-created on first upload):
1. **Generic CUDA runtime `.deb`s (~1.1GB, `cuda-runtime-debs.tar.gz`)** — this bundle is pinned to whatever version the script resolves (`cuda-runtime`, or the latest `cuda-runtime-N` found in NVIDIA's repo), **not** to the host's GPU driver, so it's identical run-to-run until NVIDIA ships a new CUDA release. Confirmed via the two apt-get lines: this is a separate install from the host-specific `libnvidia-gl-<DRIVER_VER>`/`libnvidia-compute-<DRIVER_VER>` extraction a few steps later, which **remains uncached and dynamic** per prior explicit decision (baking in a fixed driver version previously caused the exact bug class that took 8+ debug cycles to fix).
2. **Compiled `reco-cli` binary**, keyed by the exact git SHA of `reco-project/video-stitcher` HEAD at clone time (`reco-cli-<sha>.tar.gz`) — a source change naturally produces a new cache key, so a stale binary can never be served silently.

Mechanics: plain `curl` + `jq` against the GitHub REST API (release lookup/create, asset list/download/delete-then-upload) — no `gh` CLI needed. `GH_TOKEN` (mapped from `secrets.GITHUB_TOKEN` in the workflow, `contents: write` permission now granted at the workflow level) is forwarded into the SSH session; every caching code path is a no-op if `GH_TOKEN` is unset, so the script degrades gracefully to always-build/always-install if the token is ever missing.

**UNVERIFIED** — no run has exercised this code yet. Cache will be empty on the first run after this change (guaranteed miss → builds + populates cache normally), so the real test is whether the **second** run after this shows `cache HIT` in `env.log`/`build.log` and skips the download/build.

**Lens-profile fix (`5710bff`) CONFIRMED WORKING via run `31323968907` (completed success, head commit `13892db9` = docs-only commit directly after `5710bff`, so behaviourally identical code).** `calibrate.log` shows no Mobius-fallback warning: 180 matched points, confidence 100%, `median_error=0.000035`, `cameraAxisOffset=0.2388`, `intersect=0.5396`. This run predates the `b635545`/`4a17c6d` build-cache commits (dispatched before they landed), so the cache path is still unverified by any run — carry forward.

**Verification run `31328297333`: FAILED.** `stitch.log`: `Error: source: source init (left.mp4): left[0] Y: CUDA error 201 in cudaGetDevice`, exit 1. Calibrate succeeded first (same 100%/180-match result as before), crash is in `SmartFileSource::open`'s video-source init, before any trajectory/viewport code runs — confirmed unrelated to the `3d440ab` diff itself.

**Redispatch `31329362682` (same commit `3d440ab`, no code change, testing bad-host hypothesis): FAILED identically.** Same `cudaGetDevice error 201` at the same point. Ruled out both "bad host" and "upstream reco drift": both failing runs used the exact same `reco-project/video-stitcher` HEAD (`cd3bf434b9`, unchanged since 6 Aug — no upstream commits landed in the window), and the second run's build-cache logged a binary cache HIT on the first run's build, so both ran the literal same compiled binary on different Vast.ai hosts.

**Root cause: `calibrate` and `stitch` diverge in hwaccel-failure handling.** Both failing runs show `calibrate` also hitting HEVC/CUDA hwaccel setup failures (`[hevc @ ...] Failed setup for format cuda: hwaccel initialisation returned error`) — but `calibrate`'s frame-extraction path absorbs this and degrades to software decode, same as it always has. `stitch`'s `SmartFileSource::open` GPU-capability probe doesn't have that same graceful fallback for this specific failure — it calls `cudaGetDevice`, gets a hard error, and the whole process exits instead of degrading to CPU decode (which is what the one prior successful run, `31323968907`, happened to land on anyway via `Zero-copy disabled: decode_backend=software (capable=false)... Using CPU upload path`). Whether the probe crashes vs. degrades looks host/timing-sensitive, not tied to our diff or to reco's source changing.

**Fix @ `0b79303` (`oev_reco_stitch_remote.sh` only, Claude-authored direct build, explicit routing override, user: "Yup"): adds `--no-zero-copy` to the stitch invocation.** This is an existing, documented reco flag ("Force CPU video decode instead of GPU zero-copy") that sidesteps the crashing probe entirely rather than relying on it to degrade gracefully. Diff verified: 1 line changed, nothing else touched.

**Verification run `31330697894`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31330697894 — head commit `0b79303`. Same inputs (`trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`). This is debug cycle 2 of 3 for this session (cycle 1 = the bad-host redispatch test, which was informative but didn't fix it).

**Retry `31332310874` (same commit `0b79303`, no code change — the failure above was pure transient network flakiness, not the fix): SUCCESS.** `--no-zero-copy` confirmed working — no `cudaGetDevice` crash, stitch completed. Johnson visual check on `panorama.mp4` (58°, 3840×1440, zero-wedge trajectory): near-zero wedges (tiny sliver visible at top — confirms true safe ceiling is a hair under 58°, consistent with earlier calibration-derived estimate), but horizontal coverage insufficient — left goal area cropped out of frame. Step 1 of the agreed test sequence (docs above) completed: 58° is not enough.

**Scope correction on `CylindricalProjection`:** re-confirmed by re-reading `reco-core/src/projection/mod.rs` — it is explicitly single-camera/mono (own test: `"cylindrical projection consumes exactly one camera"`). It is NOT a two-camera stitcher and can't be "wired in" as a drop-in fix for the L-shape stitch path. Any cylindrical two-camera panorama would be new implementation work (reprojecting each calibrated camera plane onto a shared cylinder + blending), using the existing `Projection` trait as the plug-in point and `CylindricalProjection`'s shader as a reference, not an integration of existing code. Revise any earlier framing that suggested otherwise.

**Revised plan (Johnson, prototype-first — skip the 75°/3840×1440 half-step, go straight to a much wider zero-code test before considering any reco fork):** `reco`'s stitch is fundamentally sound — it builds the full combined scene from both cameras correctly, then only *displays* a narrow perspective-camera slice of it (`ViewportConfig::default()` fov=75°, output aspect controls how much of that slice is shown). The two raw camera feeds already have ~160° of real combined horizontal coverage with overlap around midfield — cameras/mount/calibration are not the problem. Before building any cylindrical projection, test whether just requesting a far wider *aspect* at the existing fixed 75° vfov (no trajectory, no code changes) already recovers effectively all of it: `7680×1080` → ~159° horizontal (vs 8192 cap), matching the full available coverage. Expected: heavy perspective edge-stretch (~5.5× at the far edges, well beyond the ~2× threshold flagged earlier as a tracking-accuracy risk) — deliberately untested until now, since the point is to look at the real image and then test the tracker on it rather than reason abstractly about acceptable distortion. If both goals are visible and edge-stretch doesn't break tracking → done, no reco fork needed. If pitch is fully visible but tracking fails at the edges → that failure mode is what justifies building a real two-camera cylindrical projection (new work, needs a fork of `reco-project/video-stitcher`, which is currently cloned fresh from upstream on every run with no fork existing).

**Change @ `b6b136b` (`oev_reco_stitch_remote.sh` only, Claude-authored direct build, explicit routing override): removes `--trajectory`/CSV entirely (back to plain stitch, fixed 75° default) and widens output to `--width 7680 --height 1080`** (keeps `--no-zero-copy`, still needed regardless of trajectory). Diff verified: trajectory CSV block removed, one flag line changed, nothing else touched.

**Verification run `31334476831`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31334476831 — head commit `b6b136b`. Same clip pair. Next: Johnson visual check — do both goals appear in frame? If yes, proceed straight to a tracking test on this output before any further reco work.

**Run `31334476831`: SUCCESS, fastest run to date (11m35s — reco-cli binary cache HIT saved the ~10min build; CUDA runtime step happened to hit a fast host/mirror this time, ~2min vs ~15min elsewhere — CUDA runtime cache itself is still broken, see fix below, still outstanding). Visual result: both goals in frame** (Johnson confirmed on scrub-through) — the ~159° horizontal at 7680×1080 does reach full pitch coverage. **But edge distortion is severe and disqualifying.** Pulled the actual frames from the run artifact (not just the Drive copy) and did a direct crop comparison: a player near the left edge is visibly squashed wide/flat (limbs blurred into torso, clearly abnormal proportions) versus the same player type at frame-center looking completely normal. Matches the predicted ~5.5× edge stretch at this aspect/FOV combination. This is the decisive negative result Johnson's test sequence was designed to produce.

## CONCLUSION — perspective-FOV testing is DONE, do not resume it

**Root cause, final form:** `reco`'s current `stitch` output is a flat perspective-camera view of the correctly-stitched 3D scene (`ViewportConfig` fov_degrees + standard tan-frustum projection, see `render/viewport.rs`/`projection/virtual_camera.rs`). It is NOT a panoramic/cylindrical projection — despite superficially resembling one at narrow FOV. Widening the perspective FOV to reach full pitch coverage (~160°) necessarily produces severe secant-law edge stretch; this is mathematically inherent to perspective projection at wide FOV, not a bug or a tunable parameter. **Cameras, mount, and calibration are all confirmed fine — do not revisit them.** The problem is entirely `reco`'s output/projection mode.

**Decision: build a two-camera cylindrical projection in `reco`, matching the ActionStitch reference visual/approach.** `reco-core/src/projection/mod.rs` already has a `Projection` trait (the intended plug-in point, `Box<dyn Projection>`) and a `CylindricalProjection` — but that existing one is single-camera/mono (confirmed via its own test: `"cylindrical projection consumes exactly one camera"`), so it is a reference for the cylinder math/shader approach, not a drop-in fix. The real work is a new two-camera cylindrical projection: reproject each calibrated camera plane onto a shared cylinder (using the same `MatchCalibration`/`PlaneLayout`/`CameraParams` data `reco calibrate` already produces correctly) and blend in the overlap zone.

**Not yet resolved: whether ActionStitch's own software is automatable (CLI/API) or is a manual/consumer app.** If manual-only, it's a visual reference for judging our cylindrical output, not a viable production path for OEV's unattended pipeline — worth a quick check before treating "just use ActionStitch" as a real option.

**Scope note:** this is genuine architecture/implementation work (new Rust + WGSL in a repo we don't currently fork — every run still clones fresh from `reco-project/video-stitcher` upstream with no fork existing), not a config/flag change. Belongs in a proper dev loop (fork first, then design + Codex implementation + iteration against real footage), not further one-off direct-push commits in a mobile chat session.

**Outstanding, unrelated to the above, still worth fixing whenever convenient:** `gh_cache_upload()` in `oev_reco_stitch_remote.sh` uses `curl --data-binary @"$filepath"`, which buffers the whole file in memory. Works fine for the small `reco-cli` binary (cache HIT confirmed working across multiple runs) but OOMs on the ~1.1GB CUDA runtime `.deb` bundle every time (`curl: option --data-binary: out of memory`) — that cache has never actually populated, so every run still pays the full CUDA-runtime `apt-get install` cost (~2–15min, host-dependent). Fix: swap to `-T "$filepath"` (streaming upload) in that one function, used at both call sites (line ~144 CUDA runtime, line ~275 reco-cli binary).

**Next (fresh chat — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Confirm whether ActionStitch has any automatable interface (quick check, not a blocker either way).
2. Fork `reco-project/video-stitcher` (small, unblocks iterating on projection code — currently every workflow run clones fresh from upstream with no fork).
3. Design the two-camera cylindrical projection: new struct implementing the `Projection` trait, reprojecting both calibrated camera planes onto a shared cylinder + overlap blend, using `CylindricalProjection`'s existing shader/cylinder math as reference and `MatchCalibration` as the data contract. Given the scope, consider drafting this design on Opus before routing implementation through Codex as normal (frozen files + data contracts as hard constraints, Codex writes to a feature branch, Claude verifies the diff).
4. Once a working two-camera cylindrical build exists: point the OEV stitch pipeline at it, re-run the same visual test (does the whole pitch fit with acceptable — not zero — distortion), then move to the tracking test that was deferred this session.
5. Fix `gh_cache_upload()`'s `--data-binary` → `-T` OOM bug whenever convenient (see above) — small, independent of the projection work.
6. Deferred, lower priority: real-GPU Vulkan rendering still broken (`libnvidia-gpucomp.so` version mismatch) — not a blocker for any of the above, CPU/software render works fine.

## Session update — 9 Aug 2026, later session: black-wedge root cause diagnosed, not a calibration/rig problem

**Root cause confirmed by reading `reco` source directly (`reco-core/src/{calibration,projection,render}`) and reproducing the defect in an offline geometry simulation using the real pinned Hero10 Wide KB4 intrinsics + Johnson's calibration (`intersect=0.540`, `cameraAxisOffset=0.239`):**
- `cameraAxisOffset`/`intersect` are positional/overlap parameters of `reco`'s 2-plane L-shape rig model, not angles — `13.7°` was never a real quantity. Calibration and physical mount (ActionStitch HERO10 90° mount) are NOT the problem; do not touch either.
- Plain `reco stitch` (no panner) renders one fixed perspective camera at `ViewportConfig::default()` — hardcoded `fov_degrees: 75.0`, no CLI flag to change it. Source vertical coverage from this rig is only ±29° at the seam / ±33° off-seam, so the 75° (±37.5°) request exceeds real coverage at the seam → the black wedges. Horizontally, ~160° of real overlap-corrected coverage exists, but default 16:9 only shows 107.5° of it — most left/right footage is being discarded, not lost to any defect.
- **Important gotcha, confirmed by reading `frame_processing.rs::director_position()`:** any panner (`--trajectory` or `--model`) makes reco auto-clamp `fov_degrees` to `coverage.max_fov_degrees()` (~58° for this calibration, aspect-independent) on every frame, regardless of the value supplied. `reco stitch --help` confirms there is no standalone `--fov` flag. Net effect: **only two vfov regimes are reachable without patching `reco`** — `--trajectory` → clamped to ~58° (zero-wedge), or no panner → fixed 75° (small wedges, ~95% coverage, ~2.3x edge stretch at wide aspect). Nothing in between (e.g. 65°/70°) is reachable via CLI today.
- `CylindricalProjection` already exists in `reco-core/src/projection/mod.rs` (single-camera mono projection, ActionStitch-matching defaults) but is not wired into the 2-camera `StitchJob`/CLI stitch path. Relevant if the perspective-renderer approach hits a wall — extends an existing trait rather than a fresh rewrite.
- OEV requirement reframed (Johnson, agreed): not "zero black pixels" — "the whole playable pitch visible with enough resolution for tracking; invalid sky/foreground can be cropped." Wedge tolerance is acceptable if the pitch itself is intact.

**Agreed test sequence (cheapest-first, no code/mount/calibration changes unless both steps below fall short):**
1. `--trajectory` fixed pose, fov=58 (auto-clamped anyway), wide 3840×1440 output → zero-wedge, ~114° horizontal. If whole pitch visible → done, M1-adjacent visual gate clears, no further changes needed.
2. If not enough: plain stitch, no `--trajectory`, same 3840×1440 → fixed 75°, ~128° horizontal, small wedges, ~2.3x edge stretch. If whole pitch visible and distortion acceptable → also done, still zero reco code changes.
3. Only if both 1 and 2 fall short (58° too narrow AND 75° too distorted/ugly): fork `reco-project/video-stitcher` (currently cloned fresh from upstream every run, no fork exists) — either add a real `--fov` override or wire in the existing `CylindricalProjection`.

**Fix @ `3d440ab` (`oev_reco_stitch_remote.sh` only, Claude-authored direct build, explicit routing override, user: "You do it"): implements step 1 of the test sequence.** Diff verified: 2 lines removed / 9 added, isolated to the calibrate→stitch handoff block; build-cache, NVIDIA/Vulkan diagnostics, and lens-profile pin all untouched. Writes `trajectory.csv` (`frame,yaw,pitch,fov` header + one row `0,0.0,0.0,58`) before the stitch call, then invokes `reco stitch ... --trajectory trajectory.csv --width 3840 --height 1440`. One row is sufficient — `FilePanner` holds the frame-0 pose for the whole clip.

**Verification run `31328297333`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31328297333 — head commit `3d440ab5`. Inputs: `left_clip=trimmed_GX010197.MP4`, `right_clip=trimmed_GX010173.MP4` (workflow defaults, unchanged). Also the first real exercise of the `b635545`/`4a17c6d` build-cache path (still unverified going into this run) — a cache-related failure here would be an unrelated confound to rule out before blaming the trajectory/width/height change.

**Next (fresh chat — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Check run `31328297333`: `env.log`/`build.log` for cache HIT/MISS + success (first real test of the build-cache path), `calibrate.log` for no Mobius fallback (expect same as `31323968907`), `stitch.log` for `Stitch OK`.
2. Pull `panorama.mp4`, Johnson visual check against step 1 of the test sequence above: does the whole playable pitch fit in the zero-wedge 3840×1440/58° frame?
   - Yes → M1 visual gate effectively cleared on this axis; move to repeatability check on `GX010198`/`GX010175`.
   - No → dispatch step 2 (plain stitch, no `--trajectory`, same 3840×1440, accepts 75°/small wedges) before considering any fork of `reco`.
3. Once seam/edges look correct: Johnson visual sign-off on `GX010197`/`GX010173`, then repeat on `GX010198`/`GX010175` as a repeatability check = M1 gate cleared.
4. Confirm whether HyperSmooth was on during filming (Johnson to check) — OFF required per the tool's own doc comment.
5. Deferred, lower priority: real-GPU Vulkan rendering still broken (`libnvidia-gpucomp.so` version mismatch, 580.178.04 vs host 580.159.03) — works but slower via CPU fallback, not a blocker for M1.


## 2026-08-10 session: OEV M1 cylindrical-stereo projection — geometry solved

**Venue label correction:** the `GX010197`/`GX010173`/`GX010198`/`GX010175` pairs used throughout M1 testing are St Margaret's, not Aylestone as labelled earlier in this doc (Johnson correction, 2026-08-10). See strikethrough notes inline above. Not yet reconciled with the earlier "Aylestone avoids St Margaret's sun-exposure calibration issue" reasoning — calibration on this pair has consistently been strong (100% confidence, 150-180 matched points across sessions), so either that issue doesn't affect this specific pair/angle, or is otherwise not currently manifesting. Worth a fresh look if calibration quality regresses.

**Context:** M1 gate's disqualifying finding from the prior session (perspective-FOV testing) was that `reco`'s flat-perspective output mode gnomonically stretches wide FOV (~5.5× at the edges) — cameras/mount/calibration were fine. Decision made: build a cylindrical (yaw-linear) reprojection instead of tuning FOV further.

**Fork + core cylindrical rendering (PR #1):** `JhnsonO/video-stitcher` forked from `reco-project/video-stitcher` pinned at `cd3bf434b92c678a5585cd9be2330eda49782a8a`. Branch `feat/cylindrical-stereo-projection` → merged to fork `main` @ `df07a86f02416f09811cd41ca6db5aa8407fa13a`. Adds `CylindricalStereoProjection`/`CylindricalStereoProjectionConfig`, `cylindrical_stereo.wgsl` (full-screen per-pixel analytic ray/plane intersection — not iterative ray-marching — reusing the KB4 distortion + YUV/color-transfer blocks from `fisheye.wgsl` verbatim), a real `CylindricalRenderer` (bind groups, pipeline, `render()`), and `--projection <l-shape|cylindrical-stereo>` CLI flag wired all the way through `StitchJob`/`StitchSession`/`StitchCore`/`StitchPipeline`. First Codex pass shipped a renderer stub (math helpers + tests, no actual wgpu pipeline, flag validated but not dispatched) — caught in review before merge, second pass fixed it correctly. Frozen files (`fisheye.wgsl`, `renderer.rs`, `scene.rs`, `stitch_renderer.rs`, `planes.rs`, `coverage.rs`, `virtual_camera.rs`, `reco-calibrate/`, `reco-autocam/`) untouched throughout.

**Tunable FOV (PR #2):** merged to fork `main` @ `ba04bb4b5c0b5431fdea39732159f9da522ccd02`. Exposes `--yaw-span-deg`/`--vertical-fov-deg`/`--yaw-center-deg` (all optional, defaulting to the prior hardcoded 180°/70°/0°) through `StitchJob` builder setters into `CylindricalStereoProjectionConfig`.

**Root-cause finding (black-gap/pinch defect):** first real stitch (`ffa-automations` run `31338591795`, default 180°/70°/yaw-center-0°, 7680×1080) produced a severe hourglass-pinch artifact with a large black wedge — visually alarming but confirmed NOT a code bug. Reimplemented the shader's ray/plane/KB4 math in Python from the run's actual `match.json` calibration and reproduced the exact artifact shape, proving the math is correct — the defaults simply requested more combined FOV than this two-camera rig's real coverage envelope supports gap-free. Further simulation found:
- Right camera's modeled coverage hard-caps at **~+57° to +61° yaw** (checked across every pitch — this is a genuine calibration/physical-FOV ceiling, not recoverable via config).
- Left camera's modeled coverage is clean and gap-free out to **~−146.5° yaw at pitch 0** — far more headroom than initially used.
- The original "biggest gap-free rectangle" approach (yaw_center ≈ −17°, span 146.5°) over-indexed on avoiding black pixels rather than on capturing the actual playable pitch, and silently truncated ~50° of perfectly good left-camera coverage. Corrected per Johnson's direction: pin the right edge at the real ceiling, extend the left edge to use the confirmed headroom, accept trivial black at extremities.

**`ffa-automations` workflow parameterization:** `oev-reco-stitch.yml` gained `yaw_span_deg`/`vertical_fov_deg`/`yaw_center_deg`/`out_width`/`out_height`/`max_frames`/`run_label` `workflow_dispatch` inputs (commit `ecf43e4`), threaded via SSH env vars into `oev_reco_stitch_remote.sh` (commit `3b3ef67`), which also now points its `reco-cli` build at the fork instead of upstream (commit `24761d8`). `run_label` suffixes the `Stitched/` Drive filename so multiple candidate runs against the same clip pair don't collide.

**Candidates tested (600-frame/~10s previews against `trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`):**
- `ref146` (146.5°/50°/−17°, 6592×2256) — no-black reference; repeatedly hit Vast.ai infra flakes (SSH-never-came-up), never got a clean completion, superseded before revisiting.
- `wide155` (155°/47°/−17°, 6976×2112) — run `31341569727`, succeeded. Right goal clearly visible, proportions sane, small right-edge wedge as expected — but **left goal missing**, exposing the left-coverage-truncation issue above.
- **`fullcover178` (178°/42°/−31°, 8016×1888) — run `31343444338`, succeeded. Both goals visible, sane proportions throughout, only a trivial black wedge at the far-right edge (matches the confirmed real right-camera ceiling, not fixable via config).** Simulated gap severity for this window: ≤1% worst-row gap. Visually spot-checked one frame (t=5s) — looks like the geometry problem is solved, pending Johnson's full review of the 10s preview clip.

**Infra notes (unrelated to code):** this session hit repeated transient Vast.ai flakes — 3× "SSH never came up" within the 90s post-`running` window on different offers/hosts, 1× `git clone` TLS handshake failure. All resolved by blind redispatch (consistent with the workflow's existing "cheaper to redispatch than wait out a bad host" philosophy) — none were caused by the config/code changes made this session.

**M1 visual gate: likely cleared on geometry, pending full-clip review.** Not yet done: (1) Johnson's full review of the `fullcover178` preview (only a single frame spot-checked so far); (2) a full-length (non-`--max-frames`-capped) stitch at 178°/42°/−31°/8016×1888 once the preview is approved, as the actual M1 sign-off artifact; (3) repeatability check on the second clip pair if required.

**Next steps after M1 sign-off (Johnson's direction):** quality/bitrate pass, and start the follow-cam/tracking track in parallel. Also worth a future look: `reco`'s own `coverage.rs` ("no-black" viewport-bounds utilities, currently only wired to the old perspective-panning path) could potentially auto-derive this window instead of the hand-derived constants above, which are specific to this rig's current physical mount/calibration and would need re-deriving if the rig is re-aimed or recalibrated.

## 2026-08-10 session (cont.): pitch_center_rad added; final M1 framing config locked in

**`pitch_center_rad` feature (PR #3):** `video-stitcher` merge to fork `main` @ `53fe10f548d5767ad94ef66aeaedf2d8c7161f27` (direct Claude-authored build, user: "Don't leave this to codex, you make the changes" — explicit routing override for this change only). Adds a vertical-offset control mirroring the existing `yaw_center_rad` pattern end-to-end: `CylindricalStereoProjectionConfig.pitch_center_rad` (default `0.0`) → new padded `pitch` uniform slot in both the Rust `Uniforms` struct (`cylindrical_renderer.rs`) and the WGSL shader (`cylindrical_stereo.wgsl`, `fs_cylindrical_stereo` adds it to the computed pitch angle) → `StitchJob::pitch_center_rad()` builder → `reco-cli --pitch-center-deg` flag. CPU-side `output_uv_to_yaw_pitch`/`yaw_pitch_to_output_uv` helpers in `cylindrical_renderer.rs` updated to stay consistent. Default `0.0` preserves prior (horizon-centered) behavior unless the flag is passed.

**`ffa-automations` wiring:** `oev-reco-stitch.yml` gained `pitch_center_deg` `workflow_dispatch` input (commit `2584 4c0`), threaded via `PITCH_CENTER_DEG` env var into `oev_reco_stitch_remote.sh` → `--pitch-center-deg` (commit `7094c6c`).

**Framing iteration (all against `trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`, 300-frame previews, `yaw_span=178°`/`vertical_fov=42°` held constant throughout):**
- `pitch_center=-8°` tested once and never revisited — solved the excess-sky problem in one step (Johnson: no complaint on vertical framing after this).
- `yaw_center` walked from the `fullcover178` baseline (-31°) to trade excess right-side fence for left-corner coverage: -31→-34 ("extra metre, not enough") →-42 ("basically there, just a bit more") →-48 ("still need some more") →-56 ("overshot a tiny bit") →**-54, confirmed "Perfect" (Johnson, run `31360135030`).**
- Right-edge yaw at -54° ≈ 35°, well clear of the confirmed ~57–61° optical ceiling — no black-wedge risk at this config.

**Locked-in M1 framing config: `yaw_span_deg=178, vertical_fov_deg=42, yaw_center_deg=-54, pitch_center_deg=-8`.** This is the confirmed-good panorama crop window; use for the full-length (non-capped) M1 sign-off stitch and as the default going forward unless the rig is re-mounted or recalibrated.

**Anomaly noted, not yet root-caused:** one preview run (`31355736849`, yaw=-42) completed all 300 frames, wrote a fully valid `panorama.mp4` (confirmed via ffprobe: correct dims/duration/codecs), and printed its full session summary — then the process segfaulted (exit 139) during cleanup, after output was already flushed. That run's GPU backend had fallen back to `llvmpipe` (software Vulkan) instead of the host's actual GPU — plausible driver-cleanup-path cause, but unconfirmed. The workflow treats any non-zero exit as fatal (skips the Drive upload step), so the artifact was only recoverable via the GitHub Actions run artifact, not the Stitched/ Drive folder. Not blocking — did not recur on subsequent runs — but worth a fix if it happens again: (a) don't treat exit 139 as fatal when `panorama.mp4` exists and is valid, and/or (b) find why this host fell back to `llvmpipe` when others in this session used real GPU decode.

**Infra notes (unrelated to code):** this session's dispatch loop hit 3 more Vast.ai "SSH never came up" flakes (90s window, `Wait for SSH` step) on top of the ones logged in the prior entry — all resolved by blind redispatch, consistent with existing philosophy. One flake was confirmed via log inspection to be a proxy-propagation delay (Vast.ai instance API reports `running` before its SSH reverse-proxy is actually reachable), not a bad-offer pattern — worth considering bumping the 18×5s retry budget in `oev-reco-stitch.yml`'s `Wait for SSH` step if this keeps recurring.

**Next (fresh chat — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Dispatch the full-length (no `max_frames` cap) stitch at the locked-in config (`178°/42°/-54°/-8°`, 8016×1888 or whatever resolution is chosen for sign-off) as the actual M1 sign-off artifact.
2. Repeatability check on the second clip pair (`GX010198`/`GX010175`) at the same config, if required before formally clearing M1.
3. Quality/bitrate pass once M1 is signed off, then start the follow-cam/tracking track in parallel (per Johnson's prior direction).
4. If the `exit 139`/`llvmpipe` anomaly recurs: investigate the workflow's exit-code handling and/or why that host didn't get real GPU access.

## 2026-08-10 session (cont. 2): GPU issue root-caused — NOT an anomaly, affects every run including the approved M1 config

**Correction to prior entry:** the `llvmpipe` fallback noted as a one-off anomaly on run `31355736849` is not an anomaly — confirmed via job logs that the approved M1 run (`31360135030`) *also* ran entirely on `llvmpipe` (software Vulkan), not real GPU. Every run checked this session used software rendering. The M1 framing/geometry sign-off is unaffected (framing is correct regardless of render backend), but no run to date has verified GPU-accelerated rendering actually works.

**Bug #1 (fixed, confirmed): version-glob mismatch in `oev_reco_stitch_remote.sh`.** The GPU diagnostic/fix block's `find` glob (`libGLX_nvidia.so.*.*.*`, expects 3 dot-segments) never matched real driver filenames like `libGLX_nvidia.so.570.144` or `.580.65.06` (2 segments) — so the block always hit "NVIDIA_LIB=not found" and bailed before running `ldd`, meaning **no run has ever actually diagnosed the real blocker until this session**. Fixed on branch `fix/oev-glx-glob-diag` (commit `a430de7`): glob changed to `libGLX_nvidia.so.[0-9]*.[0-9]*`. Confirmed working — `ldd` now runs successfully.

**Bug #2 (found, NOT fixed): NVIDIA userspace library version mismatch.** With bug #1 fixed, `ldd` reveals the real blocker: Vast bind-mounts the host's actual driver lib (`libGLX_nvidia.so.580.65.06` on this host — driver version varies per-host/offer). The script's fallback fix (`apt-get download libnvidia-gl-580` / `libnvidia-compute-580`) pulls whatever NVIDIA's apt repo currently serves as "580.x" — which is the latest patch (`580.178.04`), not the host's exact version. Result: extracted `libnvidia-glcore.so` now needs `libnvidia-gpucomp.so.580.178.04`, which doesn't exist anywhere (host only has `580.65.06` components). Vulkan ICD load still fails → `llvmpipe` fallback persists. CUDA fails separately for the same root shape (`cuInit` → `CUDA_ERROR_COMPAT_NOT_SUPPORTED_ON_DEVICE` — installed CUDA toolkit doesn't match host driver either).

**Real fix needed (not yet attempted):** apt only serves the latest patch per major driver series, so exact-version matching isn't available that way. Options: (a) search more broadly across Vast's own bind-mounted paths for an already-present, correctly-versioned `libnvidia-gpucomp.so` (Vast mounts *some* driver components directly — worth checking if this one is just in an unsearched path rather than genuinely absent); (b) fetch the exact `.run` driver installer from NVIDIA's driver archive by detected version and extract userspace libs from that instead of apt. This is a version-pinning problem, not a missing-package problem — likely needs a debug session with live SSH access (Opus-level) rather than blind script edits.

**Cost this session:** 3 dispatches on `fix/oev-glx-glob-diag` — 2 hit Vast.ai SSH flakes (clean terminations, negligible cost, unrelated to the fix), 1 succeeded and produced the above diagnosis. Total spend: low (a few cents).

**Do not merge `fix/oev-glx-glob-diag` to main yet** — it's a correct and useful diagnostic fix (keep it) but doesn't resolve GPU acceleration on its own. Full-length M1 sign-off stitch can still proceed on CPU/llvmpipe if Johnson wants to unblock M1 now — it'll just be slower per-run; the GPU fix is a separate, still-open track.

**Next (fresh chat — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. GPU fix: live-SSH debug session to find/pin the correct-version NVIDIA userspace libs (see Bug #2 above). Opus-level — architecture/version-pinning problem, not a quick script edit.
2. Decide: dispatch the uncapped M1 sign-off stitch now on CPU/llvmpipe (slower, works today) vs. wait for the GPU fix (faster once solved, timeline uncertain).
3. Once GPU or M1-sign-off path is chosen: repeatability check on `GX010198`/`GX010175` if required.
4. Orthogonal, can run in parallel any time: VR180/spherical-video metadata injection on existing CPU-rendered output, to make panorama viewable/pannable on YouTube.

## 2026-08-10 session (cont. 3): GPU EGL fix shipped + preflight built; validation in progress

**Bug #2 resolved via architecture change, not version-pinning:** rather than chasing exact NVIDIA userspace-lib version matches (prior entry's Bug #2), the fix moved the Vulkan ICD from GLX-based loading to EGL-based loading. Confirmed to work universally across 5+ hosts regardless of driver version — supersedes the version-pinning approach entirely. Production fix on `fix/oev-glx-glob-diag` @ `680d5be`.

**GPU-health preflight added (`7e74ade`):** Vulkan check + bare CUDA driver-API check + NVDEC smoke test, folded into Vast.ai offer acceptance. Bad hosts are now auto-rejected/terminated before expensive work runs, rather than discovered after the fact.

**NVENC confirmed structurally unfixable:** GeForce-in-container blocks NVENC; not a bug, not pursued further (consistent with existing NVENC-unusable-on-Vast.ai finding elsewhere in this doc).

**CUDA-runtime `dpkg-deb -x` fix shelved:** unnecessary now that the EGL fix resolves Vulkan ICD loading directly.

**RTX PRO/Blackwell excluded from offer pool (`6418af5`):** 2/2 tested hosts failed Vulkan preflight on Blackwell — excluded rather than debugged further.

**Logging gap fixed (`8efa49c`, `6418af5`):** raw preflight output was previously discarded (only pass/fail retained); now full output lands in the Actions log per offer, and a `run_metadata.txt` (instance/offer/GPU/driver) is included in pulled-back artifacts.

**Validation status:**
- First pre-merge validation attempt: 0/8 offers passed — but confirmed this was the preflight correctly rejecting every bad host, not a bad host slipping through undetected.
- Diagnostic dispatch `31388616000`: 1/1 pass, driver `550.144.03` — clean baseline captured.
- Diagnostic dispatch `31390996988` (commit `6418af5`): **DISPATCHED — UNVERIFIED**, still `in_progress` as of this entry (checked once, in progress). Purpose: catch a `580.x`/`595.x` host for a real diff against the `550.144.03` baseline. https://github.com/JhnsonO/ffa-automations/actions/runs/31390996988

**Driver correlation theory (not confirmed):** working drivers bracket a `580–595` suspected regression window (`550.x` and `610.x` confirmed working) — suggestive only, small n either side.

**Branch `fix/oev-glx-glob-diag` still not merged to `main`.**

**Next (fresh chat — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Check outcome of run `31390996988` (one more check only, per policy) — either result (pass or fail) is informative; stop investigating the driver-correlation question after this one either way.
2. Regardless of that result: dispatch one real non-`diag_only` preview validation (300 frames, locked config `yaw_span=178°, vertical_fov=42°, yaw_center=-54°, pitch_center=-8°`) and confirm: calibrate+stitch both use real Vulkan GPU (not the preflight's synthetic test), source decode uses real NVDEC, output is a valid panorama, encode correctly falls back to `libx264`.
3. Only after that passes: merge `fix/oev-glx-glob-diag` to `main`.
4. Orthogonal, can run in parallel any time: VR180/spherical-video metadata injection on existing CPU-rendered output.

## 2026-08-10 session (cont. 4): known-good tiered selection + merge to main; real M1 sign-off validation dispatched

**Tiered known-good selection shipped (`feat/oev-known-good-selection`, on top of `fix/oev-glx-glob-diag`):** replaced flat known-good list with `KNOWN_GOOD_MACHINES` dict tagging each machine `'cheap'` (tried before the normal pool) or `'fallback'` (tried only if the normal pool is exhausted). Ranking: cheap known-good → normal ≥0.995-reliability pool (preflight as before) → fallback known-good, cheapest-first within each tier. Preflight remains mandatory for every tier, including known-good. No driver-version allow/block logic added (per explicit instruction). Reliability threshold raised 0.98 → 0.995 (live pool check: 38→29 eligible pre-session, no availability collapse). `machine_id` now logged on every offer/preflight/output line and in `run_metadata.txt`.

**Seeded known-good machines (from this session's own diag runs):**
- `machine_id=7213` → `'cheap'`, RTX 3090, ~$0.35/hr, confirmed PASS (run `31396647812`, driver `535.161.08`).
- `machine_id=2750` → `'fallback'`, A100-SXM4-40GB, ~$1.19/hr, confirmed PASS (run `31392655465`, driver `580.82.09`).

**Diag validation runs this session (all `diag_only`, empty→partial known-good list at time of run):**
- `31388616000`: driver `550.144.03` → PASS (baseline).
- `31390996988`: driver `595.71.05` → PASS. Breaks the suspected `580–595` driver regression-window theory (in-range driver passed clean) — confirms the earlier decision not to hard-block by driver line was correct. Theory retired, not pursued further per instruction.
- `31392655465`: empty known-good list, normal-pool fallback path only. 12 offers tried (2 failed on `nvdec` specifically, Vulkan/CUDA otherwise clean) before `machine_id=2750` passed — this run is what surfaced 2750 as a fallback candidate.
- `31396647812`: fallback-tier seeded but not yet exercised (normal pool passed on try 3, `machine_id=7213`, before reaching fallback) — surfaced 7213 as the cheap-tier candidate.

**Both branches merged to `main` (commit `cecf5dbd`):** merge done as an explicit two-parent commit via the Git Data API (blob→tree→commit→ref), not a plain GitHub merge, because `docs/ai-project-state.md` conflicted (this file had been updated directly on `main` mid-session while the branches were in flight). Conflict resolved by keeping `main`'s current version of this doc and taking the three code files (`.github/workflows/oev-reco-stitch.yml`, `oev_gpu_preflight.sh`, `oev_reco_stitch_remote.sh`) verbatim from `feat/oev-known-good-selection`'s tip. Verified post-merge: workflow on `main` byte-matches the feat-branch tip; no doc content lost.

**Real (non-`diag_only`) M1 sign-off validation dispatched from `main`:** run `31397771578`, 300-frame preview, locked config `yaw_span_deg=178, vertical_fov_deg=42, yaw_center_deg=-54, pitch_center_deg=-8`, `run_label=m1-signoff-preview`. **DISPATCHED — UNVERIFIED.** This is the actual acceptance check: confirm calibrate+stitch use real Vulkan GPU (not preflight's synthetic test), source decode uses real NVDEC, output is a valid panorama, encode correctly falls back to `libx264`. If this passes, Johnson's direction is to call Vast/GPU infrastructure "production-ready enough for now" and stop selection/preflight tuning unless production runs show a real problem.

**Next (fresh chat — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Check outcome of run `31397771578` (one check only). Verify in the logs: real Vulkan device used (not `llvmpipe`), NVDEC decode confirmed (not CPU fallback), `panorama.mp4` valid (ffprobe dims/duration/codecs), encode backend is `libx264` as expected.
2. If it passes: infrastructure work is done per Johnson's direction — no further Vast/selection/preflight tuning unless a production run surfaces a real problem.
3. If it fails: this is a new debug cycle (budget resets in a fresh chat) — do not reopen the driver-correlation or selection-tiering questions, which are both closed per this session's findings.
4. Orthogonal, can run in parallel any time: VR180/spherical-video metadata injection on existing CPU-rendered output.

## 2026-08-10 session (cont. 5): first M1 sign-off attempt crashed on a real bug; fixed, re-dispatched

**Run `31397771578` (first real M1 sign-off attempt) failed -- not a preflight rejection, a crash.** Offer `35580411` (RTX 3090, machine_id=78080) reached `running`, SSH came up, but the preflight script itself hung and hit its 180s timeout. The `except subprocess.TimeoutExpired` handler in the launch step had a real bug: `exc.stdout`/`exc.stderr` are always `bytes` on a timeout even when `text=True` was passed to `subprocess.run()` (decoding only happens on successful completion) -- the handler tried to concatenate `bytes` with `str` and raised `TypeError`, crashing the whole launch step (exit 1) instead of gracefully rejecting the offer and trying the next one.

**Consequence:** because the crash happened before `instance_id` was written to `$GITHUB_OUTPUT`, the "Pull back logs" cleanup step ran with `INSTANCE_ID` empty and never attempted `delete_instance()` -- the Vast instance (offer `35580411`, `ssh8.vast.ai:17958`) was left running with no automated cleanup. **Manually terminated by Johnson.**

**Fixed on `main` directly (commit `03169c8`):** the `TimeoutExpired` handler now checks `isinstance(exc.stdout, bytes)` and decodes before concatenating. Scoped to just this one except block -- diff-verified against main before push.

**Re-dispatched:** run `31399426095`, same real 300-frame M1 sign-off config (`178°/42°/-54°/-8°`, `run_label=m1-signoff-preview-2`) from `main`. **DISPATCHED — UNVERIFIED.**

**Debug budget:** 1 of 3 diagnose→fix→dispatch cycles used this chat.

**Next (fresh chat if budget exhausted -- mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Check outcome of run `31399426095`. If it fails again, this is diagnose→fix→dispatch cycle 2 -- do not reopen driver-correlation or selection-tiering (closed), only debug the actual new failure.
2. If it passes: verify in logs -- real Vulkan device (not `llvmpipe`), real NVDEC decode (not CPU fallback), valid `panorama.mp4` (ffprobe dims/duration/codecs), `libx264` encode fallback as expected. If all confirmed, Johnson's direction is to call Vast/GPU infrastructure "production-ready enough for now" and stop tuning unless a production run surfaces a real problem.
3. Orthogonal, can run in parallel any time: VR180/spherical-video metadata injection on existing CPU-rendered output.

## 2026-08-10 session (cont. 6): M1 GPU sign-off PASSED — Vast/GPU infrastructure track closed for now

**Run `31399426095`: SUCCESS.** Real 300-frame M1 sign-off preview from `main`, locked config `yaw_span=178°, vertical_fov=42°, yaw_center=-54°, pitch_center=-8°`. All four acceptance criteria confirmed from job logs:
- Vulkan: `Selected GPU: NVIDIA GeForce RTX 4080 (Vulkan)` -- real GPU, not `llvmpipe`.
- NVDEC: `left decoder: NVDEC (CUDA)` / `right decoder: NVDEC (CUDA)`, both 3840x2160 sources -- real hardware decode confirmed.
- Encode: `h264_nvenc: No capable devices found` -> clean fallback to `libx264 (software)`, as expected (consistent with confirmed structural NVENC-on-Vast limitation).
- Output: `Stitch OK: panorama.mp4 written`, 300/300 frames, 7680x1080, 24.2fps. Instance `47379398` (offer, machine_id=112749, RTX 4080, driver 595.71.05, normal pool -- neither known-good tier was exercised) cleaned up correctly.

**Minor non-blocking observation:** log also shows `Force CPU decode: zero-copy disabled by --no-zero-copy` / session summary `Decode: CPU upload`, despite NVDEC hardware decode being confirmed on both streams. This is `reco-cli`'s zero-copy *pipeline* mode (GPU-resident frame path end-to-end) being off, distinct from whether NVDEC hardware did the decode (it did). Not a failure against any of the four sign-off criteria. Worth revisiting only if a future perf pass specifically targets zero-copy throughput.

**Per Johnson's explicit direction: Vast/GPU selection infrastructure is now "production-ready enough for now." Stop tuning preflight, driver correlation, or offer selection unless a production run surfaces a real problem.**

**Session totals:** `fix/oev-glx-glob-diag` + `feat/oev-known-good-selection` both merged to `main` (merge commit `cecf5dbd`). One real production bug found and fixed on `main` directly (`03169c8`, TimeoutExpired bytes/str crash -- also caused an orphaned Vast instance on the first attempt, run `31397771578`, manually terminated by Johnson). Known-good tiers seeded: `machine_id=7213` (cheap, RTX 3090, ~$0.35/hr), `machine_id=2750` (fallback, A100-SXM4-40GB, ~$1.19/hr) -- neither tier has been exercised by a passing run yet (all passes so far landed in the normal pool), so they remain unverified-in-practice but logically sound and diff-reviewed.

**Next (fresh chat -- mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. GPU/selection infrastructure track is CLOSED. Do not resume driver-correlation, preflight tuning, or known-good list curation unless a real production run shows a concrete problem.
2. Orthogonal, can run in parallel any time: VR180/spherical-video metadata injection on existing CPU-rendered output.
3. Otherwise: next roadmap item per Johnson's prior direction is the follow-cam/tracking track, or a quality/bitrate pass on the OEV stitch output.

## 2026-08-10 session (cont. 7): follow-cam track started — first real test dispatched

**New track, per Johnson's routing doc: test Reco's existing field follow-cam (person+ball -> FieldPanner, broadcast preset) on real HERO10 footage before designing any new tracker.** Panorama/M1 work is done and untouched this session.

**Source findings (read directly from `JhnsonO/video-stitcher` before writing anything):**
- `reco-cli` default features are `["autocam", "ort"]` — follow-cam needs **no build-flag changes**, the existing `cargo build --release -p reco-cli` already includes AI tracking.
- `reco stitch` flags confirmed: `--projection l-shape` (default, not cylindrical), `--calibration`, `--model <onnx>`, `--tracking field` (default), `--panner-preset broadcast|action|frame_all`, `--lookahead` (default 1.5), `--detection-interval` (default 1), `--events <jsonl>`, `--no-zero-copy` (forces CPU decode + CPU ORT detection, sidesteps CUDA/TensorRT entirely), `--allow-no-tracking` (silently continues without tracking — deliberately NOT used this test).
- `reco-detect`'s ORT backend requires an ONNX model with **built-in NMS**. Sourced via `ultralytics` at runtime: `yolo export model=yolov8n.pt format=onnx nms=True` (unpinned `pip install ultralytics` — acceptable for this one proof run per Johnson, not for anything recurring).
- Pipeline event JSONL schema confirmed by reading `reco-core/src/detect/pipeline_event.rs` directly (not guessed): `serde(tag="kind", rename_all="snake_case")` — variants include `detections_raw` (has a `detections` array), `pan_decision` (has a `pose` object with `yaw`/`pitch`/`fov_degrees` in radians/degrees). Used to build an evidence-based acceptance check rather than just checking the job exit code.

**New files, merged to `main` (commit `9c0bb7b`, feature branch `feature/oev-followcam-test-m1` deleted after merge, built directly by Claude per explicit routing override — Johnson: "No need for codex actually, you do it"). Diff verified before merge: exactly 2 files added, 0 modified, 918 lines total — no M1/cylindrical files touched.**
- `oev_followcam_test_remote.sh` — isolated from `oev_reco_stitch_remote.sh` (M1, frozen). Same env/apt/NVIDIA-driver-extraction/Rust-install/reco-cli-build blocks (build unchanged, so this reuses the same GH Release binary cache keyed by fork SHA). New: ultralytics venv + YOLOv8n ONNX export step. Same pinned Hero10-Wide-mode calibrate step as M1 (auto-detect known to fail on this footage's telemetry). Stitch invocation is the exact flag set Johnson specified: `--projection l-shape --model yolov8n.onnx --tracking field --panner-preset broadcast --lookahead 1.5 --detection-interval 1 --events events.jsonl --no-zero-copy --width 1920 --height 1080` — **no `--allow-no-tracking`**, so a tracking-init failure fails the run instead of silently degrading to a static stitch. New acceptance-check block (exit 5 if it fails, distinct from exit 1/2/3/4 for env/calibrate/stitch/missing-output failures): parses `stitch.log` for the literal `"Autocam: tracking enabled"` string, and parses `events.jsonl` to confirm at least one `detections_raw` event had a real detection AND at least 2 `pan_decision` events exist with a non-zero yaw spread (i.e. the camera actually moved, not just initialized).
- `.github/workflows/oev-followcam-test.yml` — Vast.ai GPU lifecycle (launch-retry/preflight/offer-selection/`delete_instance`) copied verbatim from `oev-reco-stitch.yml` per CLAUDE.md, only the offer-acceptance/download/run/upload steps adapted. New "Evaluate acceptance" step explicitly fails the job (`exit 1`) if the remote script's exit code was non-zero — so **a red run means the acceptance check failed, even if `followcam.mp4` was produced** (this was Johnson's explicit requirement). Drive upload (only on remote exit 0) goes to a new `Followcam/` subfolder of the OEV Drive folder, uploading `followcam.mp4` + `events.jsonl`, separate from `Stitched/`.

**Verification run `31427290826`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31427290826 — head commit `9c0bb7b`. Inputs: `left_clip=trimmed_GX010197.MP4`, `right_clip=trimmed_GX010173.MP4` (same pair used throughout M1 — calibration already proven strong on this pair, no new variables). This is a full (non-preview-capped) run since neither the CLI nor this script exposes a frame cap for `stitch` without `--trajectory`; slower than M1 previews due to `--no-zero-copy` CPU decode/detection, cost/runtime not yet measured.

**Next (fresh chat if needed — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Check outcome of run `31427290826`. If the job is green: pull `followcam.mp4` + `acceptance.log` + `events.jsonl` from the artifact or `Followcam/` Drive folder, and Johnson reviews against the 7 acceptance criteria in the original follow-cam test brief (follows action, handles camera-to-camera handoff, lookahead anticipation, no twitchiness, broadcast-like zoom, JSONL failure attribution, nothing important off-screen).
2. If the job is red: check which stage failed (build/calibrate/stitch/acceptance, distinguishable by exit code 1-5) via the pulled logs before redispatching — this is debug cycle 1 of 3 for this session if a fix is needed.
3. If this first CPU-decode/CPU-detection test looks promising: test #2 restores NVDEC + builds the CUDA/TensorRT detector path (Johnson's explicit next step, not started).
4. Orthogonal, unrelated, can run in parallel any time: VR180/spherical-video metadata injection on existing M1 CPU-rendered panorama output (still not started).

## 2026-08-10 session (cont. 8): follow-cam quality issue diagnosed → detector-resolution A/B test dispatched

**Follow-cam visual verdict on run `31427290826` (Johnson): "floating in the middle, not finding the ball" for the first ~20s.**

**Diagnosis from `events.jsonl` (not guessed — parsed directly):**
- Ball (`class_id=32`) detected in only 90/2696 frames (3.3%) across the whole 45s clip.
- Player detections abundant (~19/frame) but raw cluster is wide/noisy — e.g. frame 600 spans yaw −0.87 to +1.49 rad (~134°), consistent with the known "duplicate detections across camera overlap, not deduped" limitation and/or sideline subs being picked up as persons.
- Root mechanism, read from `FieldPannerConfig` source: `broadcast` preset only overrides `ball_weight` (0.20); `cluster_alpha` (0.012, very slow EMA) and `dead_zone_rad` (0.20 rad ≈ 11.5°) are both shared defaults. That combination, fighting a noisy/duplicate-heavy raw cluster, is why the aim can't accumulate enough net displacement to clear the dead zone for a long stretch.
- `PannerDebug` events (the enum variant with `cluster_yaw`/`target_yaw` etc.) were never emitted (0 in the file) despite `--events` being passed — a gap in this version of `FieldPanner`, not our config; limits how precisely this can be diagnosed from JSONL alone.

**Before touching the panner at all, Johnson redirected to a more basic question: is 640×640 YOLO input starving the detector of the 4K detail we already have?** Confirmed from source (`create_ort_session` in `reco-detect/src/ort_session.rs`): `input_size` is read live from the ONNX model's own BCHW input shape, not hardcoded — a model exported at 1280/1920 makes Reco preprocess at that size automatically. `CpuYoloDetector` logs `"CpuYoloDetector loaded: input={w}x{h}, {n} classes, conf_thresh={c}"` on load, which is the ground-truth confirmation line for each test.

**Correction (Johnson) to an earlier claim in this doc/chat: field-mode confidence threshold is NOT 0.25.** 0.25 is `stitch.rs`'s hardcoded value for `tracking=ball` mode only. For `tracking=field` (what every follow-cam test has used), the real value — confirmed directly in run `31427290826`'s own `stitch.log` — is **`conf_thresh=0.1`**. The A/B/C resolution test is unaffected by this (confidence threshold isn't touched either way), but any future note referencing "0.25" for the field-mode default is wrong and should be corrected to 0.1.

**Test window, mined from run `31427290826`'s own `events.jsonl`:** frames 420–1619 (**t=7.0s–27.0s**) contain 86 of the clip's 90 total ball-hit frames — the richest 20s stretch, so a fair test of "resolution vs. genuinely missed" rather than "ball wasn't in frame."

**New files, merged to `main` (commit `2d95877`, feature branch `feature/oev-detector-res-test` deleted after merge, built directly by Claude, explicit routing override). Diff verified before merge: exactly 2 files added, 0 modified, 758 lines — no M1/follow-cam files touched.**
- `oev_detector_res_test_remote.sh` — isolated script. Reuses the shared GH-Release binary cache (same `reco-cli` fork SHA → cache HIT expected) and the same pinned Hero10-Wide calibrate step. **Only exports/runs 1280 and 1920** — 640 is NOT re-run; run `31427290826` is the 640 baseline (Johnson's explicit cost-saving call). Two `reco stitch` calls, identical flags to the 640 baseline (`--tracking field --panner-preset broadcast --lookahead 1.5 --detection-interval 1 --no-zero-copy`, small `--width 640 --height 360` output since the video itself isn't the deliverable) plus `--start-time 7 --end-time 27` to window to the same clip section, only `--model` differing. After each run, greps `stitch_{res}.log` for the `CpuYoloDetector loaded` line (confirms actual input size + conf_thresh, doesn't trust the filename) and the final `Done:` fps summary, written to `timing_summary.txt`. Also stream-copy trims `left.mp4`/`right.mp4` to the same 7–27s window (`left_window.mp4`/`right_window.mp4`) so Claude can build the visual box-overlay diagnostic locally afterward without re-dispatching.
- `.github/workflows/oev-detector-res-test.yml` — same Vast.ai lifecycle verbatim. `start_sec`/`end_sec` exposed as dispatch inputs (default 7/27). No Drive upload (throwaway diagnostic, not a product deliverable) — artifact only. "Report outcome" step is a warning, not a hard failure, since a partial result (e.g. 1280 succeeds, 1920 times out) is still useful data, not a wasted run.

**Verification run `31434114262`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31434114262 — head commit `2d95877`.

**Cost/runtime NOT yet known — explicitly flagged by Johnson as unreliable to estimate in advance.** 640 baseline measured 6.3fps avg on CPU decode+detect over the full 45s/2696-frame clip; 1920 has 9× the inference pixels of 640 and this is still CPU (ORT) inference (`--no-zero-copy`, deliberately, to isolate resolution from the CUDA/TensorRT detector-backend question). `timing_summary.txt` is designed to report actual measured fps per resolution so the next decision (is 1920 economically viable at all) is evidence-based, not estimated.

**Next (fresh chat if needed — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Check outcome of run `31434114262`. Expect this to run considerably longer than prior tests, especially the 1920 leg — do not assume a stall just because it's slow; check `timing_summary.txt`/logs for actual progress before redispatching.
2. Once both `events_1280.jsonl` and `events_1920.jsonl` exist: compute the same stats already computed for the 640 baseline (frames tested, %frames with ≥1 ball, total ball detections, confidence distribution, Left/Right split) and compare against 640's numbers (already in this doc's prior session-8 section / derivable from run `31427290826`'s artifact).
3. Build the visual diagnostic locally: extract stills from `left_window.mp4`/`right_window.mp4` at a handful of shared sample frames, overlay each resolution's *actual* reported detections (including the existing 640 baseline's `events.jsonl`, already downloaded locally) on copies of the same stills, tile into one composite comparison image.
4. Classify the result into regime A (resolution is the main problem — keep pushing resolution/optimize inference), B (resolution helps only modestly — generic COCO YOLOv8n is probably fundamentally weak for small fast football-ball detection; next step would be a football-specific detector or tiled inference), or C (resolution doesn't matter — stop here, look at ball perception another way). Report this classification with the numbers, do not just dump stats.
5. Per Johnson's explicit constraints on this ticket: no FieldPanner tuning, no player dedup, no new tracker architecture, no panorama/cylindrical work — those all wait until the detector-resolution question is answered.

## 2026-08-10 session (cont. 9): detector-resolution A/B result — REGIME A, resolution is the main problem

**Run `31434114262`: SUCCESS.** Both `CpuYoloDetector loaded` log lines confirmed actual input size + conf_thresh (not trusted from filename): `input=1280x1280, conf_thresh=0.1` and `input=1920x1920, conf_thresh=0.1`.

**Same 1199-frame window (t=7–27s of `trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`), same everything except `--model`:**

| resolution | frames w/ ≥1 ball | % | total ball detections | conf median | Left/Right |
|---|---|---|---|---|---|
| 640 (baseline, run `31427290826`) | 86/1199 | 7.2% | 92 | 0.40 | 81/11 |
| 1280 | 204/1199 | 17.0% | 229 | 0.48 | 189/40 |
| 1920 | 686/1199 | 57.2% | 880 | 0.36 | 789/91 |

**Classification: Regime A — resolution is the main problem.** Matches the shape of the example Johnson gave for "keep pushing resolution." Confirmed visually, not just from counts: a 3-frame composite (`detector_resolution_comparison.png`, delivered to the person, built from each resolution's *actual* reported detections drawn onto real camera stills, not reimplemented inference) shows frame 823 where 640 and 1280 miss a small real ball entirely and 1920 catches it clearly, alongside a frame where all three agree (confidence drops slightly as resolution rises, box just tightens) and a frame where none of the three catch anything (genuine occlusion/motion-blur miss, not a resolution artifact — useful negative control).

**Cost reality, measured not estimated (Johnson's explicit ask):** 1280 = 558.1s for 1199 frames (2.1 fps). 1920 = 1210.0s for 1199 frames (**1.0 fps** — over 20 minutes of compute for 20 seconds of footage) — all on CPU ORT (`--no-zero-copy`, deliberate, to isolate resolution from the CUDA/TensorRT backend question). **1920 on CPU is not economically viable for a real pipeline as-is.**

**Decision point surfaced, not resolved this session:** resolution clearly works and should keep being pushed, but doing so on CPU inference is dead-ended. The previously-deferred test #2 (restore NVDEC + build the CUDA/TensorRT detector path, `--zero-copy` / without `--no-zero-copy`) is no longer just "the next planned test" — it's now the actual blocker to using higher-resolution detection at all economically.

**Files/artifacts:** run `31434114262` artifact `oev-detector-res-test-31434114262` (309MB — includes `left_window.mp4`/`right_window.mp4`, the raw 7–27s window, useful for any further visual diagnostics without re-dispatching). `detector_resolution_comparison.png` built and delivered locally this session (not committed to the repo — a one-off diagnostic image, not a pipeline artifact).

**Per Johnson's explicit scope constraints, none of the following happened this session and remain open:** no FieldPanner tuning (the "floating in the middle" panner-side finding from session-8 is still just diagnosed, not fixed), no player-detection dedup across the camera overlap (also still just flagged, not implemented), no new tracker architecture, no panorama/cylindrical work.

**Next (fresh chat if needed — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Decide + scope the CUDA/TensorRT detector build (test #2). `reco-cli`'s `cuda`/`tensorrt` Cargo features exist but have never been built or tested in this repo's Vast.ai pipeline — this is real new work (build flags, driver/toolkit setup on the instance, `--zero-copy` decode path), not a config tweak like the last two tickets.
2. Once GPU detection is viable, re-run a resolution test (or go straight to 1920, given how clean this result was) with `--zero-copy` to get a real fps number before deciding whether 1920 becomes the standing default for follow-cam.
3. Only after the detector question is fully settled: revisit the session-8 FieldPanner finding (`cluster_alpha`/`dead_zone_rad` vs. a noisy/duplicate-heavy raw cluster) — a much stronger ball signal from higher-resolution detection may substantially change how much panner tuning is even needed, so tuning before this would risk tuning around the wrong problem.

## 2026-08-10 session (cont. 10): correction — 1920 not ruled out, GPU (ORT CUDA EP) test dispatched

**Johnson correction to the previous entry's framing: the 1.0fps number for 1920 was a CPU-only benchmark, not a verdict on 1920's viability.** 57.2% vs 7.2% ball recall is too large a win to discard over a CPU number. The only metric that matters for choosing final resolution is ball recall × real GPU inference speed × cost — not CPU fps. 1920 stays the leading candidate; 2560 is now also on the table if 1920→GPU still shows recall climbing.

**Source investigation (read, not guessed) that shaped this test:**
- `reco-cli`'s `cuda` feature chains cleanly: `reco-cli/cuda → reco-autocam/cuda → reco-detect/cuda → ort/cuda` (confirmed by reading all three `Cargo.toml`s directly).
- `create_ort_session()` (shared by both `CpuYoloDetector` and `OrtGpuDetector`) is where the CUDA EP attempt actually lives (`#[cfg(feature = "cuda")] ... ort::ep::CUDA::default()`), gated purely on this cargo feature — **not** on `--zero-copy`/`--no-zero-copy`. This means building with `--features cuda` and keeping `--no-zero-copy` gets GPU-accelerated *inference* while leaving decode/letterbox on CPU exactly as before — the smallest possible change to isolate "does GPU inference help" from the much larger and separately-scoped NVDEC zero-copy / `OrtGpuDetector` / NPP question (that path is Linux-only, needs NPP, and is what the earlier `stitch.rs` error message — `"Build with --features tensorrt for GPU detection, or use CPU decode"` — was pointing at for *that* path specifically, not this one).
- Reused the CUDA-runtime `.deb` install block verbatim from `oev_reco_stitch_remote.sh` (same cache key, safe to share — pinned to CUDA toolkit release, not to reco build features).

**New file, merged to `main` (commit `2ee0981`, feature branch `feature/oev-gpu-detector-test` deleted after merge, built directly by Claude). Diff verified before merge: exactly 2 files added, 0 modified, 865 lines — no other OEV pipeline touched.**
- `oev_gpu_detector_test_remote.sh` — isolated script. Builds `reco-cli` with `--features cuda` (default `autocam,ort` + `cuda`), caching that binary under a **deliberately distinct** asset name (`reco-cli-cuda-<sha>` vs. the CPU-only scripts' `reco-cli-<sha>`) so a cuda-featured binary can never silently leak into or overwrite the cache the M1/follow-cam/CPU-A-B-test scripts rely on. Re-exports YOLOv8n @ 1920 only (matches Johnson's ask to isolate 1920 specifically). Runs the same `reco stitch` flags as the CPU 1920 test (`--tracking field --panner-preset broadcast --lookahead 1.5 --detection-interval 1 --no-zero-copy`, same `--start-time 7 --end-time 27` window) — the CUDA build is the *only* variable. Does **not** trust wall-clock speed alone as proof: greps `stitch_1920_cuda.log` for the literal `ort_session.rs` log lines (`"ORT: CUDA execution provider enabled"` vs `"ORT: CUDA EP failed (...), falling back to CPU"`) and independently samples `nvidia-smi` GPU utilization every 5s for the duration of the stitch call (`gpu_util.log`), so a silent CPU fallback can't be mistaken for a GPU result. All three signals (EP status, confirmed input size, measured fps) land in one `result_summary.txt`.
- `.github/workflows/oev-gpu-detector-test.yml` — same Vast.ai lifecycle verbatim. No Drive upload (throwaway diagnostic). "Report outcome" step is a warning not a hard failure — even a build failure on `--features cuda` (real risk: this is the first time this repo has ever compiled with that feature) is itself useful information to capture, not a wasted run.

**Verification run `31466295819`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31466295819 — head commit `2ee0981`. Real build risk this time (unlike the last two tickets, which were pure config/workflow changes): `--features cuda` has never been compiled in this repo's Vast.ai pipeline before. Possible failure modes: `ort/cuda`'s prebuilt-binary download not matching the installed CUDA runtime version, or the CUDA EP initializing but silently falling back to CPU at runtime (both are things `result_summary.txt` is specifically designed to catch and report, not paper over).

**Next (fresh chat if needed — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Check `result_summary.txt` from run `31466295819` first, before anything else — confirms whether CUDA EP actually engaged (not just whether the run finished) and the real fps.
2. If CUDA EP engaged and 1920 becomes fast/cheap enough: test 2560 next (recall was still climbing steeply 1280→1920, per Johnson — not yet confident 1920 is the ceiling).
3. If CUDA EP failed to engage or the build itself failed: read `build.log`/`stitch_1920_cuda.log` for the actual reason before trying TensorRT — Johnson's stated order is CUDA first (least change), TensorRT only if CUDA works but isn't fast enough, so a CUDA failure needs its own diagnosis, not an automatic jump to TensorRT.
4. Still no FieldPanner tuning, no player dedup, no new tracker architecture — those remain blocked on the detector question being fully settled (per session 8/9 notes).

## 2026-08-10 session (cont. 11): run 31466295819 diagnosed — CUDA EP never actually registered; cuDNN fix dispatched

**Run `31466295819` result: FAIL, root cause found — not a resolution/viability verdict on 1920.** `"ORT: CUDA execution provider enabled"` was logged, but that line only reflects the builder call succeeding. One line later, `ort`'s own internal warning told the real story: `"No execution providers from session options registered successfully; may fall back to CPU."` Corroborated independently: GPU utilization sat at ~0% for ~98% of the run (peaked briefly at 27% twice — plausibly just CUDA context init, not inference), only 1.7GB GPU memory ever allocated, and throughput was 0.8 fps — *worse* than the CPU baseline's 1.0 fps (EP negotiation overhead added with nothing gained).

**Note for future sessions: the `result_summary.txt` check built for run `31466295819` was insufficient** — it treated the "enabled" log line as proof. This has been corrected (see below), but the lesson generalizes: a single positive-sounding log line from application code is not the same as confirming the underlying library actually did the thing, when the library itself logs a separate, easy-to-miss warning about the same event.

**Root cause: cuDNN was never installed.** `cuda-runtime` (the apt meta-package installed in run `31466295819`) provides `cudart`/`cublas`/`nvrtc`/`opencl` but not cuDNN, and ONNX Runtime's CUDA EP requires it. Confirmed via `ort.pyke.io` docs: `ort` 2.0.0-rc.12 targets cuDNN ≥ 9.19 and CUDA ≥ 12.8 or ≥ 13.2. **CUDA 13.2 (what's already installed) is not the problem** — it meets ort's stated minimum for CUDA 13 support. Also confirmed from the same docs: `ort` ships both CUDA 12 and CUDA 13 prebuilt binaries and auto-detects which to use, but can guess wrong (its own docs say so) — overridable via the `ORT_CUDA_VERSION` env var (`12` or `13`), which must be set before `cargo build` since `ort-sys`'s build script downloads/links the matching prebuilt binary at build time (confirmed: the binary is statically linked, `-l static=onnxruntime`, via `ort`'s `download-binaries` strategy).

**Fix (Johnson's exact spec), pushed straight to `main` as a single-file edit to the existing script (commit `6f069f5`, diff verified: `oev_gpu_detector_test_remote.sh` only, +78/-9, nothing else touched):**
1. Installs `libcudnn9-cuda-13` (Ubuntu 24.04 + CUDA 13.x package name, confirmed via NVIDIA's own cuDNN install docs) after the existing CUDA-runtime install block, with a fallback attempt at the unversioned `cudnn9-cuda-13` meta-package name.
2. Forces `export ORT_CUDA_VERSION=13` before the `cargo build --features cuda` step (and it stays exported through the stitch run too) — no auto-detection.
3. **Binary cache key changed again** (`reco-cli-cuda13-<sha>`, distinct from both the CPU-only scripts' key and the previous cuda attempt's `reco-cli-cuda-<sha>` key) — deliberately forces a fresh build with `ORT_CUDA_VERSION=13` actually set, rather than risking a cache hit silently reusing the ambiguous previous build.
4. **Acceptance check rewritten** to match Johnson's exact bar — PASS requires ALL THREE: (a) zero occurrences of the "No execution providers ... may fall back to CPU" warning, (b) GPU utilization sustained (≥50% of 5s samples above 10% util, not just a peak blip), (c) measured fps > 2.0 (materially above the 1.0 fps CPU baseline). Anything short of all three is reported as FAIL in `result_summary.txt`, explicitly, rather than left for a human to infer from raw numbers.

Per Johnson's explicit constraint: nothing else touched — no TensorRT, no panner, no tracker, no other resolution.

**Verification run `31470468751`: DISPATCHED — UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31470468751 — head commit `6f069f5`. Real risk still open: `libcudnn9-cuda-13` may not exist/resolve cleanly in the apt repo state on a fresh Vast.ai instance (per public reports, NVIDIA's cuDNN-for-new-CUDA package availability lags at times) — the script's fallback attempt and the tightened acceptance check are both designed to surface that clearly rather than silently mask it.

**Next (fresh chat if needed — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Check `result_summary.txt` from run `31470468751` FIRST — look at the explicit PASS/FAIL verdict line, not just whether the job went green (a green CI job only means the script didn't crash, not that CUDA worked — this is the exact mistake made reading run `31466295819`'s first pass).
2. If PASS: 1920 recall (57.2%) at real GPU speed is the headline result — decide whether to test 2560 next (Johnson noted recall was still climbing steeply 1280→1920, not yet confident 1920 is the ceiling) and/or move toward making this the production follow-cam config.
3. If FAIL: read exactly which condition failed (fallback warning present? GPU util still ~0%? fps still low despite clean EP registration?) before deciding whether to keep debugging CUDA or move to TensorRT — per Johnson, a CUDA failure needs its own diagnosis, not an automatic TensorRT fallback.
4. Still no FieldPanner tuning, no player dedup, no new tracker architecture — blocked on the detector question being fully settled (sessions 8/9/10/11).

## 2026-08-10 session (cont. 12): CUDA EP CONFIRMED WORKING — 5.7 fps at 1920, real GPU inference

**Run `31470468751`: PASS, verified manually against all three of Johnson's conditions (the run's own `result_summary.txt` verdict line was WRONG — see bug note below, do not trust that specific artifact's printed verdict, trust these numbers instead):**
- Fallback warning count: **0** (clean CUDA EP registration, confirmed both via the `ort_session.rs` "enabled" log line and the absence of `ort`'s own internal fallback warning).
- GPU utilization: **sustained**, not a blip — 33 of 42 samples (79%) above 10%, mean 14%, peak 23%. (Contrast with the first CUDA attempt, run `31466295819`: ~0% almost the entire run.)
- Throughput: **5.7 fps** (1199 frames in 209.7s) — **5.7× the 1.0 fps CPU baseline**, comfortably past "materially exceeds."

**Fix that got it there (cuDNN + explicit CUDA version, commit `6f069f5`):** installed `libcudnn9-cuda-13` (missing in the first attempt — root cause of that attempt's silent fallback) and forced `export ORT_CUDA_VERSION=13` before the build, per Johnson's exact spec, removing ort's auto-detection ambiguity entirely. Confirmed via `ort.pyke.io` docs during that session that CUDA 13.2 (already installed) was never the problem — it meets ort rc.12's stated ≥13.2 minimum; the only real gap was cuDNN.

**Bug found and fixed in the diagnostic script itself, not the pipeline (commit `f3c4c64`):** the script's own verdict-check printed **FAIL on this genuinely-passing run**. Root cause: `SAMPLES_ABOVE_10=$(... | grep -oE 'samples_above_10pct=[0-9]+' | grep -oE '[0-9]+')` also matched the literal "10" inside the label text "10pct" (not just the intended value "33"), corrupting the downstream arithmetic. Verified the fix against `31470468751`'s actual `gpu_util.log` before pushing (manually reproduced `SUSTAINED=1`, `MATERIALLY_FASTER=1` with the corrected parsing) — did not just push and hope. **A second, unrelated real-world event during this fix:** the GitHub API had a transient outage (repeated 503s across multiple endpoints, ~10 minutes) while pushing this fix; retried with backoff until it cleared rather than assuming the credentials/request were wrong. **Lesson for future sessions, generalized:** having designed the wrong verification check once already this session (mistaking `"CUDA execution provider enabled"` for proof) doesn't mean the second, more careful check is automatically trustworthy either — always sanity-check a new verdict script's output against the raw data by hand before trusting it, which is what caught this bug.

**Did NOT redispatch a fresh Vast.ai run to re-verify the fixed script** — the true result was already established by hand from `31470468751`'s real data with certainty; re-running just to regenerate a correctly-formatted summary line would have been pure wasted compute. A run WAS accidentally dispatched (`31473270594`) and immediately cancelled (`conclusion: cancelled`, ~15s of billed time) once this was noticed.

**Bottom line: 1920 is real and affordable.** 57.2% ball recall (vs 7.2% at 640) at 5.7 fps on GPU. This changes the shape of the whole detector-resolution question — 1920 is no longer a "great recall, terrible cost" tradeoff, it's just better, cheaply.

**Next (fresh chat if needed — mandatory bootstrap: read `CLAUDE.md` + this file first):**
1. Test 2560 next, per Johnson's standing question (recall was still climbing steeply 1280→1920, not yet confident 1920 is the ceiling) — now that the CUDA/cuDNN build recipe is proven, this should reuse the same `oev_gpu_detector_test_remote.sh` pattern (new resolution export + rerun), not redo the CUDA plumbing from scratch.
2. Once resolution is settled (1920 vs 2560), the earlier session-8 FieldPanner finding (`cluster_alpha`/`dead_zone_rad` vs. a noisy/duplicate-heavy raw cluster) becomes relevant again — a much stronger ball signal from higher-resolution GPU detection may substantially change how much panner tuning is even needed.
3. Still no player dedup, no new tracker architecture implemented — flagged, not built.
4. Given the GPU-CUDA-detector question is now fundamentally answered (yes, it works, cheaply), the next follow-cam full-pipeline test (not just a detector diagnostic) should use the `--features cuda` + cuDNN + `ORT_CUDA_VERSION=13` build recipe from this session, not the CPU-only build the original `oev_followcam_test_remote.sh` still uses.

## 2026-08-10 session (cont. 13): 2560 resolution test dispatched

**Johnson's correction to the prior "1920 is just better/cheaply" conclusion:** 5.7fps on a 20s test window is not real-time-viable on its own -- a 60fps match naively processed frame-by-frame at 5.7fps would take ~10x real time (~16hrs for a 90-min match). This is very likely solvable (detection doesn't need to run every frame; `--detection-interval` already exists for this, tracker fills gaps between detections) but is explicitly OUT OF SCOPE for the current ticket -- eyesight (recall) and speed optimization are being kept as separate questions on purpose. Also flagged: 14% mean GPU utilization at 1920 means the GPU is nowhere near saturated -- CPU preprocessing, host<->device copies, sequential L/R inference, or general ORT overhap may still be leaving real performance on the table, independent of TensorRT. Not investigated this session -- noted for later.

**Next controlled point, per Johnson: 2560.** The 1280->1920 jump (17.0% -> 57.2%) was too large to assume 1920 is the ceiling. Decision framing given in advance: recall in the 70-80%+ range at 2560 continues the resolution investigation; recall in the ~60-62% range at roughly half of 1920's throughput means 1920 wins and resolution-chasing stops there.

**New metric to add after this run, per Johnson (not yet computed): longest continuous gap between ball detections, not just overall %.** 57.2% recall at 60fps could mean either "scattered short 1-2 frame misses, a tracker easily bridges these" or "a few multi-second total blackout stretches, which no tracker can bridge." The percentage alone doesn't distinguish these -- gap-length distribution does.

**Script changes (commit `4f6384c`, `oev_gpu_detector_test_remote.sh`, +28/-21) and workflow changes (commit `5d88618`, `oev-gpu-detector-test.yml`, +11/-3), diff verified before push -- exactly these two files, nothing else:** parametrized resolution via `MODEL_RES` env var (default `2560`), replacing every hardcoded `1920` in filenames/log names/export command/stitch args. Reuses the now-proven cuDNN + `ORT_CUDA_VERSION=13` CUDA build recipe from the 1920 fix (sessions 11/12) unchanged -- no other variable touched, exactly per Johnson's "keep every other variable identical" instruction. Workflow gained a `model_res` dispatch input (default `2560`) threaded through to the remote script invocation and the artifact pull-back step's dynamic filenames.

**Verification run `31474170441`: DISPATCHED -- UNVERIFIED.** https://github.com/JhnsonO/ffa-automations/actions/runs/31474170441 -- head commit `5d88618`, `model_res=2560`. Same 7-27s window as every prior detector test.

**Next (fresh chat if needed -- mandatory bootstrap: read `CLAUDE.md` + this file first). Johnson has asked for a handover after this run is checked and reported, so treat that as the natural stopping point for this ticket:**
1. Check `result_summary.txt` from run `31474170441` -- verify the printed verdict against the raw logs by hand before trusting it (per the session-12 lesson: this exact script's verdict math has been wrong once already, catch it again if it recurs).
2. Compute the same recall/confidence/per-camera stats already computed for 640/1280/1920 (frames tested, %frames with >=1 ball, total detections, confidence distribution, Left/Right split) from `events_2560_cuda.jsonl`.
3. Compute the NEW metric Johnson asked for: longest continuous run of consecutive frames with zero ball detections, for 640/1280/1920/2560 all four (not just 2560) -- report this alongside the recall percentages, since it's the more decision-relevant number per Johnson's own framing.
4. Classify against Johnson's stated thresholds (70-80%+ => keep investigating resolution; ~60-62% at roughly half 1920's fps => 1920 wins, stop pushing resolution) and report plainly which regime this lands in.
5. Per Johnson: after this, STOP increasing resolution regardless of outcome, and make the actual engineering trade-off call (recall vs speed) rather than continuing to chase a bigger number. Do not dispatch a 3xxx-resolution test without being asked.
6. Do not fold in detection-interval/frame-skipping optimization or TensorRT/GPU-saturation investigation into this same ticket -- both are explicitly deferred, separate follow-on tracks per Johnson, not part of "the eyesight experiment."
7. This is the natural handover point for the OEV follow-cam/detector-resolution investigation -- summarize the full resolution arc (640 -> 1280 -> 1920 -> 2560, CPU vs GPU, the cuDNN fix, the continuous-gap metric) compactly for whoever picks this up next, rather than assuming they'll re-read every session note in full.

## 2026-08-10 session (cont. 14): 2560 result — 1920 confirmed as final resolution; cache bug diagnosed (not yet fixed)

**Run `31474170441` (2560, GPU): verdict — 1920 wins, resolution-chasing stops here, per Johnson's own pre-stated thresholds.**

| resolution | recall | longest gap | conf median | L/R split |
|---|---|---|---|---|
| 640 | 7.2% | 11.31s | 0.40 | 81/11 |
| 1280 | 17.0% | 4.80s | 0.48 | 189/40 |
| 1920 | 57.2% | 2.34s | 0.36 | 789/91 |
| 2560 | 54.4% | 1.60s | 0.37 | 800/121 |

Recall 57.2% → 54.4% is flat/noise, not a climb — lands in Johnson's pre-stated "~60-62%, 1920 wins" regime, nowhere near the 70-80% bar that would justify continuing. Longest-gap did keep shrinking (2.34s → 1.60s) but on recall alone the resolution question is settled: **1920 is the standing follow-cam detector resolution.**

**Bug found in this run, not a resolution finding: 2560's speed number (0.7 fps, CPU-baseline-like) is contaminated, do not use it.** `build.log` shows a binary-cache HIT (build skipped). The cache script only archives the `reco` binary itself, not the companion `libonnxruntime_providers_shared.so` that ships alongside it. That file was missing on this fresh instance, so the CUDA EP failed to load and silently fell back to CPU. Detection results (recall table above) are unaffected — same model/weights/algorithm regardless of backend — only the fps number is bad. **Fix not yet applied**: widen the cache archive/tar step to include `*.so` alongside the `reco` binary, in whichever script does the caching for the cuda-featured build (`oev_gpu_detector_test_remote.sh` cache key `reco-cli-cuda13-<sha>`).

**Per Johnson's explicit handover instruction, this closes the resolution-investigation ticket.** Next ticket (separate, scoped, one variable at a time — per Johnson's stated ordering):
1. Fix the cache bug first (widen tar/cache step to include companion `.so` files) — otherwise any future cache-hit run on the GPU detector silently regresses to CPU again, undetected.
2. Then run a `--detection-interval` sweep at 1920 (not more resolution testing) — same 20s window, values 1/2/4/6/8 (10 excluded — Johnson: don't assume 60/interval fps naively holds real-time-viable, cameraman lateness matters more than the raw fps target). Metrics: fps, plus ball-freshness (how stale the last real detection is when the tracker/panner needs a position), not just speed.
3. Only after both: revisit session-8 FieldPanner tuning finding (`cluster_alpha`/`dead_zone_rad`) — stronger GPU-detected ball signal at 1920 may change how much tuning is even needed.

Still deferred, unstarted: GPU-saturation investigation (14% mean util at 1920 — CPU preprocessing/copies/sequential L/R inference/ORT overhead unexamined), player dedup across camera overlap, TensorRT, new tracker architecture.

## 2026-08-10 session (cont. 15): cache bug fixed — companion .so files now archived (merge `eac53f0`)

**Changed:** `oev_gpu_detector_test_remote.sh` only, +9/-2, cache-write step in the cuda binary-cache block. On cache write, now archives every `*.so*` found alongside `$RECO_BIN` (e.g. `libonnxruntime_providers_shared.so`) in addition to the `reco` binary itself, instead of just the binary. Extraction side needed no change — it already untars everything present in the archive.

**Verified:** diff reviewed by hand before merge — confirmed exactly this one hunk touched, nothing else in the 440-line script changed. Not dispatched/run-tested (no compute spent) — Johnson scoped this ticket to the fix only, no detection-interval work in this branch.

**Risk, real and unresolved: the existing cached asset for the current `reco-cli` (`JhnsonO/video-stitcher`) HEAD SHA still lacks the `.so` files** — it was uploaded before this fix, under the same `reco-cli-cuda13-<sha>` key. Since the cache key is keyed on the upstream `reco-cli` source SHA (not this script's own SHA), the next run will still get a cache **HIT** on that stale, `.so`-less asset and silently fall back to CPU again, exactly as before — this fix only takes effect once either (a) the upstream `reco-cli` SHA changes (natural cache MISS + rebuild), or (b) the stale `reco-cli-cuda13-<sha>` release asset is manually deleted to force a rebuild. Not deleted this session — needs a decision, not assumed.

**Next:** before the detection-interval sweep (or any other run of this script) is trusted to be GPU-real, either delete the stale cache asset or bump/force a cache-key change so the fixed caching logic actually gets exercised. Then proceed with the `--detection-interval` sweep at 1920 (values 1/2/4/6/8, same 20s window) as its own separate ticket, per Johnson's scoping.

## 2026-08-10 session (cont. 16): stale cuda13 cache asset deleted — next run rebuilds clean

**Changed (infra, not code):** deleted GitHub Release asset `reco-cli-cuda13-53fe10f548d5767ad94ef66aeaedf2d8c7161f27.tar.gz` (id `509856429`) from the `oev-build-cache` release — this was the `.so`-less asset uploaded before the cont.15 fix. Confirmed removed (204, and absent from the release's asset list afterward).

**Effect:** the risk noted in cont.15 is closed. Next dispatch of `oev_gpu_detector_test_remote.sh` at this source SHA will cache-MISS on the binary, rebuild from source with the cont.15 fix in place, and re-upload a cache asset containing both `reco` and its companion `.so` files. No forced cache-key bump was needed — deletion was reliable via the standard release-assets API.

**Next:** detection-interval sweep at 1920 (values 1/2/4/6/8, same 20s window) now starts from a trustworthy GPU baseline — first run will pay the one-time rebuild cost, every run after reuses the corrected cache.

## 2026-08-10 session (cont. 17): PRIORITY CHANGE — product usability gate replaces detector-throughput optimisation; sweep parked, follow-cam upgraded to proven GPU/1920

**Johnson's explicit re-scope: do not run the detection-interval sweep now.** The parametrization from cont.16 stays merged (useful later), but running 1/2/4/6/8 now would be optimisation work with no product in hand yet. New active gate: **"Can we record an FFA session, run the pipeline, and get a watchable follow-cam video back?"** — not "how fast can the detector go." 5.7 fps (proven GPU, session 12) is slow but works; that's good enough for a V1 judged on watchability, not throughput.

**Changed:** `oev_followcam_test_remote.sh` only (merge `2be3e63`, +168/-25). This is the production-facing follow-cam script (separate from the 20s `oev_gpu_detector_test_remote.sh` diagnostic script) — until now it still built CPU-only (no `--features cuda`) and exported YOLO at the ultralytics default (640), i.e. it had never actually picked up either proven result from the detector-resolution investigation.
- Added the full CUDA runtime + cuDNN 9 + `ORT_CUDA_VERSION=13` install block, copied verbatim from `oev_gpu_detector_test_remote.sh` (proven in sessions 11/12).
- Build now uses `cargo build --release -p reco-cli --features cuda`, sharing the same `reco-cli-cuda13-<sha>` cache key as the detector-test script (including the cont.15 companion-`.so` cache fix) — a rebuild triggered by either script now benefits both.
- YOLO export changed to `imgsz=1920` (was unset / default 640) — the proven resolution from the 640/1280/1920/2560 comparison (cont.9/13/14: 57.2% recall vs 7.2% at 640, and 2560 showed no further gain).
- `--detection-interval` left at `1` and `--no-zero-copy` left in place — no frame-skipping, no decode-path change; this ticket is resolution + GPU-inference only, matching Johnson's "detector throughput is not the blocker" framing.
- Added an informational (non-fatal) CUDA-EP status check to the existing GPU/decode summary block, so the run log makes clear whether detection actually ran on GPU or silently fell back to CPU — doesn't change the pass/fail acceptance gate (still just real-detections + real-camera-movement, unchanged from before).
- Workflow file (`oev-followcam-test.yml`) needed **no changes** — resolution/interval/CUDA are internal to the script now, not dispatch inputs.

**Verified:** diff reviewed by hand — CUDA/cuDNN block copied verbatim (byte-identical to the proven block), only the build command, cache key, YOLO export line, and stitch/summary comments changed relative to the prior version. Not run-tested yet (no compute spent this session).

**Not dispatched.** Next action is a real dispatch of `oev-followcam-test.yml` (`left_clip`/`right_clip` inputs, defaults to the same trimmed clip pair used in the M1/follow-cam sessions unless Johnson wants a different/longer session clip) to produce an actual `followcam.mp4`, which Johnson will watch and judge against plain criteria: does it generally show where the ball is, does it keep play in frame, does it avoid random camera movement, would this be postable to YouTube without embarrassment. If yes: **lock this as V1**, and all further work (frame-skipping, TensorRT, batching, GPU saturation, panner tuning, dedup) becomes separate, non-blocking optimisation/fix work scoped one item at a time against whatever the viewing actually shows is wrong. If no: fix only what visibly makes it unusable, re-render, re-judge — do not pre-guess which of panner/ROI/dedup/tracking is the problem before watching the output.

**Known gap, not blocking, noted for whenever the sweep resumes:** the cont.16 events-schema parser's `hit_field` detection doesn't yet match this repo's actual schema — confirmed from `oev_followcam_test_remote.sh`'s own acceptance check that events use `kind` (not `type`/`event`) as the type field (parser already covers `kind`, that part's fine) and that `detections_raw` events carry a `detections` list where a non-empty list (not a boolean field) signals a real hit — the parser currently only checks boolean-style fields (`has_ball`/`ball_found`/`detected`/`hit`) and would fall back to "every detections_raw event is fresh," overcounting. Fix before trusting sweep output, not before this session's product-gate work.

## 2026-08-10 session (cont. 18): OEV ROI stills workflow — decision found + shipped, dispatched (`ceb97b5`, run `31489346775`)

**Decision found (changes the planned ticket):** the "click a polygon, get normalized ROI" tool Johnson asked for already exists upstream in `JhnsonO/video-stitcher` — no new build needed. `reco-core/src/calibration.rs` already has `FieldRoi` on `MatchCalibration`; `scripts/field_roi.py` (cv2 GUI) and `resources/roi_editor.html` (browser/mobile-usable canvas clicker) both already output normalized `field_roi.left`/`.right` in the exact schema; `reco-autocam/src/roi_filter.rs` already drops any detection whose center falls outside the polygon; `reco-cli/src/stitch.rs` (line 108, 352-353) already auto-loads `field_roi` from `match.json` into the autocam config — no CLI flag needed.

**Real gap:** `oev_followcam_test_remote.sh` always runs `reco calibrate` fresh each run and feeds that raw `match.json` straight into `stitch` — nothing today injects a `field_roi` into it. Fix is a small merge step, not a new tool, and needs the actual clicked polygon first.

**Changed:** new file `.github/workflows/oev-roi-stills.yml` only (`ffa-automations`, feature branch `feature/oev-roi-stills`, GitHub-hosted only, no Vast.ai, no GPU). Mirrors `flatcam-stills.yml`'s Drive-download (`YOUTUBE_TOKEN`/`YOUTUBE_CREDENTIALS` oauth-refresh) and ffmpeg-single-frame-extract pattern verbatim; resolves `left_clip`/`right_clip` filenames to Drive file IDs inside the OEV `Trimmed/` folder the same way `oev_reco_stitch_remote.sh` does. Extracts one raw frame each (default clips: `trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`, same pair used throughout M1/follow-cam, `00:01:00` timestamp) and uploads `left.jpg`/`right.jpg` as an artifact. No frozen file touched (`oev_reco_stitch_remote.sh`, `oev_followcam_test_remote.sh`, and all fork frozen files untouched).

**Verified:** self-verified (explicit routing override, Johnson: "Go, do it yourself") — diff is a single new additive file, no frozen paths touched, pattern copied verbatim from `flatcam-stills.yml` (proven) and the Drive-folder-resolution logic copied from the proven `oev_reco_stitch_remote.sh` path.

**Merged to main:** `ceb97b5` (required so `workflow_dispatch` registers the workflow — GitHub only exposes dispatch for workflow files present on the default branch).

**Dispatched:** run `31489346775`, `ref=main`, default inputs. One immediate status check only: `in_progress`, no fast failure. **DISPATCHED — UNVERIFIED** — artifact not yet inspected.

**Next action:** once the run completes, pull the `oev-roi-stills-31489346775` artifact (`left.jpg`/`right.jpg`), Johnson clicks the pitch-boundary polygon on each (via `resources/roi_editor.html` in the fork, or `scripts/field_roi.py` locally) and supplies the two normalized point lists. Ticket 2 (not yet built): check in `oev/venues/st_margarets_field_roi.json` (mirrors `flatcam/venues/st_margarets_msv.json` pattern) with those fixed polygons, and add a small merge step to `oev_followcam_test_remote.sh` that injects them into `match.json` right after `reco calibrate`, before `reco stitch` — then re-dispatch follow-cam and re-judge watchability with the neighbouring pitch masked out.

## 2026-08-10 session (cont. 19): ROI injection shipped + merged; St Margaret's ROI-filtered follow-cam dispatched

**Ticket:** apply the fixed St Margaret's `field_roi` (Johnson's marked polygons from cont.18 stills) to `match.json` before `reco stitch`, re-render the exact same clip pair, no other tuning. Question this run answers: does excluding the neighbouring pitch keep the otherwise-good cameraman on the correct game.

**Changed:** `oev_followcam_test_remote.sh` only (+69/-0). Added a block immediately after `Calibrate OK: match.json written` (before the stitch section) that loads `match.json`, sets `field_roi.left`/`field_roi.right` to the fixed 11-point/13-point St Margaret's polygons (verbatim from Johnson's ticket), rewrites `match.json`, and validates both arrays are present and non-empty before continuing — non-fatal path aborts with exit 2 (same as existing calibrate-stage failures) if injection or validation fails. No new venue-JSON file, no ROI-editor invocation, no CLI flag — matches the existing `reco stitch` auto-load of `field_roi` from `match.json` (`reco-cli/src/stitch.rs`, confirmed in cont.18). `oev_reco_stitch_remote.sh`, all frozen `video-stitcher` fork files, detector resolution (1920), CUDA/cuDNN/ORT setup, `--detection-interval 1`, tracking mode/panner-preset/lookahead/render-size/acceptance logic, and `oev-followcam-test.yml` — all untouched.

**Verified:** diff against `main` reviewed — single file, +69/-0, addition-only, one insertion point (confirmed via `diff` against pre-edit copy). `bash -n` syntax-check passed. Embedded Python merge/validation logic tested standalone against a synthetic `match.json` — confirms `field_roi` is written and read back correctly, matches the `FieldRoi` schema in `reco-core/src/calibration.rs` (`{"left": [[x,y],...], "right": [[x,y],...]}`).

**Merged to main:** `e25875e` (feature branch `feature/oev-st-margarets-roi`, commit `76a6b51`).

**Dispatched:** run `31494793310`, `ref=main`, same clip pair as always (`trimmed_GX010197.MP4` / `trimmed_GX010173.MP4`, workflow defaults). One immediate status check only: `in_progress`, no fast failure. **DISPATCHED — UNVERIFIED** — artifact/logs not yet inspected.

**Next action:** once the run completes, pull `calibrate.log` to confirm the `field_roi` injection/validation lines logged successfully, confirm normal follow-cam acceptance still passes (unchanged criteria), and Johnson visually watches `followcam.mp4` against the single product question above (does ROI filtering keep the cameraman on the correct game). Do not tune/diagnose further until that visual review happens.

## 2026-08-10 session (cont. 20): CUDA cache-poisoning root-caused; verification + random test-segment shipped, dispatched

**Bug found from run `31494793310` artifact:** `stitch.log` showed `libonnxruntime_providers_shared.so` missing at `/tmp/reco-src/target/release/`, forcing silent CPU fallback (~1.5 fps on a 45s clip = ~30.5 min). Root cause: binary-cache HIT restored a `reco-cli-cuda13-<sha>.tar.gz` tarball that never contained the CUDA companion `.so` in the first place — cache poisoning, not a one-off. Secondary bug found in the same investigation: 3 `tee -a build.log` calls in the build block ran *after* `cd /tmp/reco-src`, silently writing to `/tmp/reco-src/build.log` (never collected) instead of `/tmp/oev_run/build.log` — this is why the collected `build.log` showed only the header + "Build OK" with no HIT/MISS line, making the bug invisible from the artifact until `stitch.log` was checked directly.

**Product read (Johnson, visual review of the ROI run):** ROI fix confirmed working — `Autocam: field ROI filtering enabled` logged, camera no longer wanders onto the neighbouring pitch. Follow-cam quality once it sees the ball: good. Initial ball acquisition: still an open question, needs another segment to judge (not yet answered). This run's *speed* (1.5 fps) is invalid/discard — CPU-fallback artifact, not a real GPU result.

**Changed:** `oev_followcam_test_remote.sh` only (+49/-8, one file, merge `08d5992`).
1. Fixed the 3 misdirected `tee -a build.log` → `tee -a /tmp/oev_run/build.log`.
2. Added a hard post-build/post-cache-restore check for `libonnxruntime_providers_shared.so` next to `$RECO_BIN`. Cache-HIT path: if the `.so` is missing after extraction, `bin_cache_hit` is reset to 0 so the script falls through to a real `cargo build` (bad cache is discarded, not trusted). Both paths (cache-restore and fresh build) converge on one final unconditional check — if the `.so` still isn't present, the run now fails loudly (`exit 1`) instead of silently proceeding to a CPU-fallback stitch.
3. Added a random 15–20s test-segment selection (inserted right after system deps/ffmpeg install, before CUDA runtime install): probes the full downloaded clip's duration via `ffprobe`, picks a random `SEG_DURATION` (15–20s) and random `SEG_START` within bounds via bash `$RANDOM`, then `ffmpeg -ss/-t -c copy` trims both `left.mp4`/`right.mp4` in place before build/calibrate/stitch. Same clip-pair filenames/dispatch inputs as always (`trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`) — this only changes which sub-window of that same footage is used, picked at random rather than hand-chosen, per Johnson's explicit direction. Logged to new `segment.log` (start/duration/full-duration), collected in the artifact alongside the existing logs (workflow YAML itself untouched — `segment.log` isn't yet in the `Pull back logs + outputs` scp list, so it will only be visible via a manual pull-back if needed for the next diagnosis, not automatically in the artifact zip).

No frozen file touched. `oev_reco_stitch_remote.sh`, all `video-stitcher` fork files, `oev-followcam-test.yml`, detector resolution (1920), `--detection-interval 1`, tracking mode/panner-preset/lookahead/render-size, and acceptance logic all unchanged.

**Verified:** diff against `main` reviewed — single file, +49/-8, confined to the build-cache block and one new segment-selection block. `bash -n` syntax-check passed. Cache-invalidation branch logic tested standalone (simulated a `.so`-missing cache extraction → confirms it correctly falls through to the rebuild branch, and the final hard check passes once the `.so` is present). Random-segment arithmetic tested standalone under real `bash` (the script's actual shebang) — duration consistently lands in [15,20], start+duration stays within a 45s clip across repeated runs.

**Merged to main:** `08d5992` (feature branch `feature/oev-cuda-cache-fix-random-segment`, commit `bb57a75`).

**Dispatched:** run `31499510994`, `ref=main`, same clip-pair inputs (`trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`) — random segment is chosen inside the script itself, not via workflow inputs. One immediate status check only: `queued`. **DISPATCHED — UNVERIFIED**.

**Known gap, not blocking:** `segment.log` (new) isn't in the workflow's scp pull-back list yet — if the next diagnosis needs to see the exact random start/duration chosen, it'll need a manual `scp` or a follow-up one-line addition to the workflow's "Pull back logs + outputs" step (not done here — out of scope for this ticket, and the values are also visible via `grep-log` on the run if needed).

**Next action (superseded by session below):** ~~once run `31499510994` completes...~~ — this segment-selection approach (random crop of the already-trimmed 45s clips) was itself replaced in the next session because it wasn't a genuine generalisation test.

## 2026-08-11 session: synchronised test segment now drawn from FULL original source clips (not trimmed), CUDA EP fail-fast, stage timing

**Product question:** on a genuinely unseen section of the original match footage, with the validated St Margaret's ROI, does the current 1920+CUDA follow-cam acquire the ball and produce good camerawork? The previous random-segment logic only cropped inside the already-trimmed 45s test clips, so it never actually tested unseen footage — fixed this session.

**Changed** (merge `d3bb7831`, feature branch `feature/oev-followcam-fullsource-segment`, built directly by Claude per explicit routing override — Johnson: "Do it yourself"). Diff verified before merge: exactly 2 files, +156/-42, no frozen files touched (`oev_reco_stitch_remote.sh`, `video-stitcher` fork, ROI implementation, detector resolution, `--detection-interval`, tracking/panner/lookahead/render settings all unchanged).

1. `oev_followcam_test_remote.sh`:
   - Segment selection rewritten: `left.mp4`/`right.mp4` as downloaded are now the FULL original source clips (not the 45s trimmed clips). ffprobes both, takes `min(left_dur, right_dur)` as the valid overlap window, picks one random 15–20s duration and one random start within that bound, applies identically to both cameras via `-ss/-t -c copy`. Logs full-source filenames (via new `LEFT_CLIP`/`RIGHT_CLIP` env vars) + chosen start/duration/overlap to `segment.log`.
   - New CUDA execution-provider fail-fast check (exit code 6) inserted after the build/`.so`-presence check, before `calibrate`/`stitch`: installs `onnxruntime-gpu` in the existing yolo-venv and actually creates a `CUDAExecutionProvider` `InferenceSession` against the exported `yolov8n.onnx`, hard-failing if CUDA isn't available, session creation raises, or the active provider isn't CUDA. Replaces the old post-stitch informational-only log grep (that check is still present afterward, now just confirmatory).
   - New `timing.log`: per-stage wall-clock timing for segment extraction, deps/build/setup, calibrate, stitch/render, and total runtime. Diagnostic only, not used to tune anything this session.
2. `.github/workflows/oev-followcam-test.yml`:
   - `left_clip`/`right_clip` inputs now resolved directly in the OEV Drive root folder (`18Y8hI_S29BMeg5FEoxlaqoGy1DsQ7GKJ`) instead of the `Trimmed/` subfolder. Defaults changed from `trimmed_GX010197.MP4`/`trimmed_GX010173.MP4` to `GX010197.MP4`/`GX010173.MP4` (the full originals — confirmed already present at Drive root from the earlier `oev-drive-upload.yml` runs, same base filenames as the trimmed pair used throughout M1/follow-cam).
   - `LEFT_CLIP`/`RIGHT_CLIP` now passed into the remote script's SSH invocation (previously only `GH_TOKEN` was) so `segment.log` can record the real source filenames.
   - `Pull back logs + outputs` step now also scp's `segment.log` and `timing.log` into the run artifact.

**Verified before merge:** diff scope confirmed (2 files, no frozen-boundary violations); `bash -n` passed; segment start/duration/overlap arithmetic tested standalone under real bash across 20 randomized left/right duration pairs (always lands in [15,20]s, never exceeds either source's overlap); CUDA EP fail-fast decision branches (unavailable / session-raises / silent-fallback / real-success) tested standalone in isolation; workflow YAML parses.

**Dispatched:** run `31508983649`, `ref=main` (head `d3bb7831`), `left_clip=GX010197.MP4`, `right_clip=GX010173.MP4`. https://github.com/JhnsonO/ffa-automations/actions/runs/31508983649 — status `queued` at dispatch. **DISPATCHED — UNVERIFIED.**

**Run `31508983649`: FAILED (infra, not the segment/CUDA-EP logic).** Job log: `left.mp4` (full `GX010197.MP4`) downloaded fully at 22.1GB; `right.mp4` (full `GX010173.MP4`) download hit aria2 `(ERR):error occurred` mid-transfer (disk full, partial `.aria2` file left at 22.1GB). Next step's `scp` of the (tiny) remote script itself then failed: `scp: dest open ".../oev_followcam_test_remote.sh": Failure` — "No space left on device". Root cause: the offer query's `disk_space: {'gte': 40}` was sized for the old ~small trimmed-45s-clip workflow; the FULL original GoPro source clips are ~22GB each (44GB combined), which alone exceeds the 40GB floor before build/CUDA-runtime/venv space is even considered. **Fix (`5da01a7`, single line, workflow file only):** raised `disk_space` threshold to 120GB. Not a bug in the segment-selection or CUDA-EP-fail-fast logic from the `d3bb7831` merge — those never got a chance to run.

**Redispatched:** run `31514234521`, `ref=main` (head `5da01a7`), same inputs. https://github.com/JhnsonO/ffa-automations/actions/runs/31514234521 — status `in_progress` at last check. **DISPATCHED — UNVERIFIED.** This is debug cycle 1 of 3 for this session.

**Run `31514234521`: FAILED at "Launch reliable Vast.ai GPU instance"** (never reached SSH/download/preflight). All 15 tried offers got `HTTP Error 400: Bad Request` immediately from the `PUT /asks/{offer_id}/` launch call. Root cause: the `disk_space` **query filter** was raised to 120 in the previous fix, but the **launch request body** that actually creates the instance still hardcoded `'disk': 40` — decoupled parameter, never updated. **Fix (`f573427`, single line, workflow file only):** launch-time `'disk'` now also 120, matching the query floor. This mismatch would also have reproduced the original disk-full failure on any offer that *did* launch, so the fix was necessary regardless of whether it's the exact 400 cause.

**Redispatched:** run `31514597279`, `ref=main` (head `f573427`), same inputs. https://github.com/JhnsonO/ffa-automations/actions/runs/31514597279 — status `queued` at dispatch. **DISPATCHED — UNVERIFIED.** This is debug cycle 2 of 3 for this session.

**Run `31514597279`: FAILED identically to `31514234521`** — uniform `HTTP Error 400: Bad Request` on all 15 offers tried, across completely different GPU models/machines/prices, immediately at the launch PUT call. Since disk=40/query-filter=40 (the original, unmodified config) never showed this, and it started exactly when the launch `'disk'` param was raised 40→120 (cycle 2's fix), the leading hypothesis is that 120 hits some Vast.ai-side cap/validation on the `disk` field — but `request()` was discarding the actual HTTP error response body, so this can't be confirmed from the logs as they stand.

**Fix (`34441e2`, workflow file only, diagnostic not mechanics):** `request()` now raises with the real HTTP error body (truncated 500 chars) on non-2xx instead of the generic urllib message; added explicit `urllib.error` import. Per-offer try/except/continue retry structure itself untouched — CLAUDE.md's verbatim-lifecycle rule is about retry/cleanup/termination *mechanics*, not error-message content. Did **not** additionally guess-change the `disk` value again this cycle — with only one debug cycle left in this session's budget, priority is getting the real error text into the log rather than layering another unverified guess on top of the previous two failed guesses.

**Dispatched:** run `31514919487`, `ref=main` (head `34441e2`), same inputs. https://github.com/JhnsonO/ffa-automations/actions/runs/31514919487 — status `queued` at dispatch. **DISPATCHED — UNVERIFIED.** This is debug cycle 3 of 3 (final) for this session.

**Run `31514919487`: FAILED — real cause finally visible.** `{"error":"insufficient_credit","msg":"Your account lacks credit; see the billing page."}` on every offer. Nothing to do with the `disk` parameter at all — both cycle-2 and cycle-3's "uniform 400 across all offers" symptom was Vast.ai account balance, not a launch-request bug. The `disk_space`/`disk` fixes (`5da01a7`, `f573427`) may still be correct/necessary for when full-source clips are used (unverified — never got far enough to prove it), but they were not the actual blocker on these two runs.

**Session debug budget (3/3) exhausted.** Per CLAUDE.md, handing off to a fresh chat.

**First action in next chat: fetch and read `CLAUDE.md` and `docs/ai-project-state.md` from the repo before doing anything else.**

**Gate:** OEV follow-cam full-source-segment ticket (2026-08-11) — code changes (`d3bb7831`) verified via diff/syntax/standalone-arithmetic but never actually exercised on a full run; every dispatch since has failed at Vast.ai instance launch.

**Done this session:**
- `d3bb7831` — segment selection from full original source clips (not trimmed), CUDA EP fail-fast (exit 6), stage timing. Verified via diff + `bash -n` + standalone arithmetic/branch tests only — **not yet run to completion.**
- `5da01a7` — offer query `disk_space` 40→120GB (fixes real disk-full failure seen on run `31508983649`).
- `f573427` — launch-time `disk` param 40→120GB (matches the above; not yet proven necessary/sufficient on its own since account credit blocked verification).
- `34441e2` — `request()` now surfaces real HTTP error bodies instead of discarding them. This is what found the actual blocker.

**Runs this session, all UNVERIFIED/FAILED, none reached the segment-selection/CUDA-EP code:**
- `31508983649` — disk full mid-download (before `disk_space`/`disk` fixes).
- `31514234521`, `31514919487` (and `31514597279`, same signature) — `insufficient_credit` at Vast.ai launch, surfaced only on the last one once error-body logging was added.

**Blocker (not code):** Vast.ai account (`console.vast.ai`) is out of credit. Johnson needs to top up before any further dispatch of this or any other Vast.ai-based OEV workflow will get past instance launch.

**Next action:** once credit is topped up, dispatch `oev-followcam-test.yml` (`left_clip=GX010197.MP4`, `right_clip=GX010173.MP4`) on `main` (head `2199075` or later) — this will be the first real test of the `d3bb7831`/`5da01a7`/`f573427` changes. Then follow the standard verification: pull `segment.log` (full-source filenames, matching start/duration, within-overlap-bounds), confirm CUDA EP fail-fast passed, confirm `field_roi` still injected, confirm follow-cam acceptance passed, and Johnson visually watches `followcam.mp4` against the product question: does the follow-cam acquire the ball and produce good camerawork on a genuinely unseen segment. Do not tune panner/tracker/detector/ROI/speed until that visual review happens.

**Credit topped up (Johnson, 2026-08-11) — redispatched.** New session, fresh debug budget. Run `31516693534`, `ref=main` (head `d3c3edf`), `left_clip=GX010197.MP4`, `right_clip=GX010173.MP4`. https://github.com/JhnsonO/ffa-automations/actions/runs/31516693534 — status `queued` at dispatch. **DISPATCHED — UNVERIFIED.**

**Run `31516693534`: real progress — launch/download/segment-selection/build all passed.** `segment.log` confirmed correct: left=`GX010197.MP4` (2953s), right=`GX010173.MP4` (2952s), overlap=2952s, chosen start=1313s duration=19s, both cameras trimmed identically and successfully. `reco-cli` build completed clean ("Build OK", CUDA provider companion libs cached). **Died in the new CUDA EP fail-fast check itself** (exit 6) — but for a false-negative reason, not a real GPU problem: `pip install onnxruntime-gpu` (no version pin) pulled latest (1.28.0), which hard-requires actual CUDA 13 system libs (`libcublasLt.so.13`). This host's driver correctly caps CUDA at 12.6 (`nvidia-smi`: Driver 560.35.03 → CUDA Version 12.6) — the same cap `reco-cli`'s own CUDA runtime install already targets (`HOST_CUDA_MAX` detection, unchanged, pre-existing logic) and has proven to work with on prior successful GPU stitch runs. The fail-fast check was testing a stricter onnxruntime build than what `reco-cli` itself needs.

**Fix (`9286ece`, script file only):** pinned to `onnxruntime-gpu==1.20.1` (CUDA 12.x-targeting) instead of latest.

**Redispatched:** run `31523236281`, `ref=main` (head `9286ece`), same inputs. https://github.com/JhnsonO/ffa-automations/actions/runs/31523236281 — status `queued` at dispatch. **DISPATCHED — UNVERIFIED.**

**Run `31523236281`: same failure point, different real bug — a genuine architectural gap, not a repeat of the same guess.** Landed on a different Vast.ai box (driver `595.71.05`, `nvidia-smi` reports CUDA 13.2 support) — but the CUDA-runtime `.deb` cache asset (`cuda-runtime-debs.tar.gz`) had a fixed name regardless of host CUDA capability, so it silently reused a **12.6-box's cached debs on this 13.2-capable box**, installing CUDA 12.6 (and breaking the `libcudnn9-cuda-13` dependency chain as a side effect). Separately, the `onnxruntime-gpu==1.20.1` pin from the previous fix turned out not to be a real published version (pip listed 1.20.0/1.20.2 etc. as the actual options) — it silently installed nothing, leaving zero GPU execution providers, which is why the failure signature looked similar but the underlying cause was compounding, not a repeat.

**Fix (`73b1d3a`, script file only):**
- CUDA-runtime cache asset now keyed by host CUDA major version: `cuda-runtime-debs-cuda{12,13}.tar.gz`, so different-capability boxes get separate caches instead of cross-contaminating.
- `onnxruntime-gpu` version now chosen dynamically from the same `HOST_CUDA_MAX` detection: latest (needs CUDA 13) on 13.x hosts, pinned `1.20.2` (a real version, CUDA 12.x) on older hosts.

**Redispatched:** run `31524916652`, `ref=main` (head `73b1d3a`), same inputs. https://github.com/JhnsonO/ffa-automations/actions/runs/31524916652 — status `in_progress` at last check. **DISPATCHED — UNVERIFIED.**

**Run `31524916652`: a THIRD distinct real bug, on a THIRD different Vast.ai box.** `Host driver max-supported CUDA version: 12.0` — `Highest cuda-runtime-X-Y <= host max (12.0): none found`. NVIDIA's apt repo for Ubuntu 24.04 doesn't ship any `cuda-runtime-X-Y` package that low (lowest available is 12.4+), so the pre-existing (not touched this session) version-selection awk logic silently found nothing and installed **no CUDA runtime at all** — `WARNING: no libcudart/libcublas found after CUDA runtime install` was already logged but non-fatal, so the run limped forward all the way to the new CUDA EP fail-fast check before dying there instead of failing loud immediately when the CUDA runtime install itself came up empty.

**Not fixed this session — needs a design decision, not a quick patch.** The pattern across this session's last 3 runs (`31516693534` CUDA13-required-pin-on-CUDA12-box, `31523236281` cache poisoned across hosts, `31524916652` no-CUDA-runtime-available-for-this-driver-at-all) is a different real edge case on a different box every time. Reactively patching per-box symptoms is not converging. The better fix is almost certainly upstream: add a minimum CUDA-capability floor to the Vast.ai offer query (alongside the existing `disk_space`/`reliability` filters) so the launch step doesn't land on boxes whose driver is too old for the Ubuntu 24.04 CUDA repo to serve at all — plus making "no compatible cuda-runtime package found" a hard, loud, immediate failure instead of a non-fatal warning that lets the run limp forward for several more minutes before failing somewhere else.

**Session debug budget long exhausted (5 distinct real fixes this session: disk_space query, launch disk param, error-body visibility, CUDA-cache-per-host keying, onnxruntime version pin). Handing off — do not continue patching per-box symptoms in this chat.**

**First action in next chat: fetch and read `CLAUDE.md` and `docs/ai-project-state.md` from the repo before doing anything else.**

**Gate:** OEV follow-cam full-source-segment ticket (2026-08-11) — the segment-selection/CUDA-EP-fail-fast/timing code itself (`d3bb7831`) is verified working (proven in run `31516693534`: correct 19s segment from full originals, build succeeds) but no run has yet gotten past the CUDA EP check to actually exercise calibrate/stitch/acceptance, because 3 different Vast.ai boxes in a row have hit 3 different real CUDA-runtime-availability edge cases.

**Done this session (all merged to main, head `9295bc4`):**
- `d3bb7831` — segment selection from full source clips, CUDA EP fail-fast (exit 6), stage timing. **Proven working** on the segment-selection side; CUDA EP fail-fast has correctly caught every real GPU misconfiguration thrown at it so far (that's it doing its job, not a bug in the check itself).
- `5da01a7`, `f573427` — offer query + launch `disk_space`/`disk` raised 40→120GB (needed for full-size 22GB source clips). Working — 2 different boxes have downloaded successfully since.
- `34441e2` — `request()` surfaces real HTTP error bodies (found the `insufficient_credit` blocker).
- `73b1d3a` — CUDA-runtime cache keyed by host CUDA major version (fixed cross-host cache poisoning); `onnxruntime-gpu` version chosen dynamically by host capability.

**Not yet fixed:** the pre-existing (untouched by this session) CUDA-runtime version-selection logic has no floor — if a box's driver reports a CUDA max below whatever the Ubuntu 24.04 NVIDIA repo's oldest available `cuda-runtime-X-Y` package is, it silently installs nothing rather than failing loud or excluding that box up front.

**Next action:** decide and implement one of — (a) add a CUDA-capability floor to the offer query in `oev-followcam-test.yml` so low-driver boxes are never selected (check what field Vast.ai's API exposes for this, likely under the same bundle search used for `disk_space`/`reliability`), or (b) make the "no compatible cuda-runtime package found" case in `oev_followcam_test_remote.sh` a hard, immediate `exit 1` instead of a non-fatal warning (cheaper fix, but doesn't prevent wasted GPU time on bad boxes — b) alone just fails faster, it doesn't fix the underlying selection problem). Once whichever fix lands, redispatch (`left_clip=GX010197.MP4`, `right_clip=GX010173.MP4`) and continue the standard verification: `segment.log`, CUDA EP pass, `field_roi`, follow-cam acceptance, then Johnson's visual review of `followcam.mp4` against the product question. Do not tune panner/tracker/detector/ROI/speed until that visual review happens.

## 2026-08-11 session (new chat, per handoff): known-good-only Vast host selection + real ORT CUDA preflight — merged, dispatched

**Root causes found in `main` @ `9295bc4` (before this session's changes):**
1. Offer pool was `cheap_known_good + normal_pool + fallback_known_good` — arbitrary hosts were tried *before* the 2750 fallback, despite `KNOWN_GOOD_MACHINES` already existing. This is why the last 3 sessions each landed on a different random box with a different CUDA-runtime edge case.
2. The real ORT CUDA `InferenceSession` fail-fast check (`d3bb7831`, exit 6, in `oev_followcam_test_remote.sh`) already met the rigor bar (real session creation + active-provider check, not just nvidia-smi/Vulkan/provider-list) but ran *after* the 44GB source download + full `reco-cli` build — so 3 sessions in a row burned a full download+build cycle before the bad host was caught.

**Changed** (merge `4661bcd` on `main`, via feature branch `feature/oev-followcam-known-good-only`, built directly by Claude per explicit routing override — Johnson: "You do it"). Diff verified before merge: only the 2 targeted files, no frozen files touched (`oev_followcam_test_remote.sh`, `video-stitcher` fork, ROI, detector resolution, segment-selection arithmetic, CUDA-runtime cache-keying, onnxruntime version-selection logic all unchanged).

1. `.github/workflows/oev-followcam-test.yml`: offer pool is now `cheap_known_good + fallback_known_good` only — `normal_pool` removed entirely. If both are empty: clear stderr message + `sys.exit(1)`, no fallthrough to arbitrary hosts. Preflight summary line and `failed_checks` list extended with the new `ort_cuda` field (see below).
2. `oev_gpu_preflight.sh`: added check 4, `PREFLIGHT_ORT_CUDA` — a genuine ONNX Runtime `InferenceSession` against a trivial self-generated 2-node graph (`Add(x,x)→Relu`, not `yolov8n.onnx` — avoids needing ultralytics at preflight stage), with `providers=['CUDAExecutionProvider']`, active-provider check, and a real `session.run()` call. `onnxruntime-gpu` version chosen the same way as the production script (host `HOST_CUDA_MAX` from `nvidia-smi` decides latest vs pinned `1.20.2`). This now runs **before** the clip download (it's inside the launch-step preflight, same stage as vulkan/cuda_init/nvdec), catching the exact failure class that killed runs `31516693534`/`31523236281`/`31524916652` last session in under a minute instead of after a full download+build.
3. `scripts/gh.sh` (merge `20da2fe`, separate small tooling commit before the above): added `branch` and `merge` subcommands (create ref from a base, POST to `/merges`) — needed since Claude was building directly this session rather than routing through Codex/a PR.

**Verified before merge:** diff scope confirmed (2 target files only); `bash -n` passed on `oev_gpu_preflight.sh`; YAML parses; the embedded launch-step Python (heredoc-extracted) compiles clean; the ORT smoke-test Python (heredoc-extracted) compiles clean and its ONNX graph was built/saved/run standalone on CPU EP in the sandbox (no GPU available there) — output `[[2.0, 2.0, 2.0, 2.0]]`, confirming graph correctness; the import-failure branch was also exercised directly (prints `FATAL: onnxruntime-gpu import failed: ...` and returns cleanly, matching intended non-crashing fail path).

**Dispatched:** run `31531708916`, `ref=main` (head `4661bcd`), `left_clip=GX010197.MP4`, `right_clip=GX010173.MP4`. https://github.com/JhnsonO/ffa-automations/actions/runs/31531708916 — status `in_progress` at last check. **DISPATCHED — UNVERIFIED.**

**Gate:** OEV follow-cam full-source-segment ticket — code changes now include known-good-only host selection + real ORT CUDA preflight-before-download. Segment-selection/CUDA-EP-fail-fast/timing code (`d3bb7831`) still unexercised end-to-end; this run is the first real test of the full stack together.

**Next action:** poll run `31531708916` once. If it fails, capture the real error window (`gh.sh logs`) — do not guess-patch. If it reaches stitch/acceptance, pull `segment.log` (full-source filenames, in-bounds start/duration), confirm `PREFLIGHT_ORT_CUDA=PASS` appears in the launch-step log before the download step ran, confirm the remote script's own CUDA EP check (still present, now confirmatory) also passed with no fallback, confirm `field_roi` injected, confirm follow-cam acceptance passed, then Johnson visually reviews `followcam.mp4` against the product question (does it acquire the ball and produce good camerawork on a genuinely unseen segment). Do not tune panner/tracker/detector/ROI/speed until that visual review happens.

**First action in next chat: fetch and read `CLAUDE.md` and `docs/ai-project-state.md` from the repo before doing anything else.**

**Run `31531708916`: clean fail-fast, exactly as designed — not a code bug.** `ERROR: No rentable offer found on either known-good machine (7213 cheap / 2750 fallback). Not falling through to arbitrary Vast hosts -- failing fast per product decision (2026-08-11).` This is the new guardrail correctly refusing to fall through to an arbitrary host — it means neither 7213 nor 2750 currently has a rentable offer on Vast.ai, not a bug in the known-good-only logic or the ORT preflight (preflight never ran; launch failed before any instance was created). Per the ticket's own instruction ("If neither known-good machine is available, DO NOT start another infrastructure-debug cycle. Report that clearly and stop."), this is not being treated as a debug cycle.

**Blocker (not code, availability):** neither known-good machine (7213 RTX 3090 cheap, 2750 A100 40GB fallback) currently has a rentable offer on console.vast.ai. Johnson needs to either wait for one to become available, or manually validate a new machine and add it to `KNOWN_GOOD_MACHINES` in `oev-followcam-test.yml`.

**Next action:** once 7213 or 2750 has a rentable offer (check console.vast.ai), redispatch `oev-followcam-test.yml` (`left_clip=GX010197.MP4`, `right_clip=GX010173.MP4`) on `main` (head `4661bcd`) — this will be the first real exercise of known-good-only selection + ORT-preflight-before-download together. No code changes needed unless a fresh error surfaces after a successful launch.

## 2026-08-11 session (cont.): ORT preflight pip bug found + fixed; availability remains the blocker

**Run `31531827387`: real bug found — first genuine exercise of the new ORT preflight check.** Offer found on machine 2750 (A100-SXM4-40GB, driver 580.82.09). vulkan/cuda_init/nvdec all `PASS`. New `ort_cuda` check `FAIL`: `FATAL: onnx import failed: No module named 'onnx'`. Root cause: `pip3 install` on Ubuntu 24.04 is blocked by PEP 668 (externally-managed-environment) without `--break-system-packages` — the install silently failed (script has no `set -e`), so the python check ran with nothing installed.

**Fix (`e68a990`, single line, `oev_gpu_preflight.sh` only):** added `--break-system-packages` to the `pip3 install` call. This is debug cycle 1 of 3 for this continuation.

**Redispatched:** run `31532316482`, head `e68a990`. **Result: clean fail-fast** — no rentable offer on either 7213 or 2750 at dispatch time; never reached preflight, so the pip fix itself is still unverified end-to-end. Not counted as a further debug cycle per the ticket's explicit instruction not to treat availability gaps as code problems.

**Gate:** known-good-only selection is proven working (both real runs this session correctly refused to fall through to arbitrary hosts). The `e68a990` ORT-preflight pip fix is applied but not yet proven — needs one more run that actually reaches the preflight stage on 7213 or 2750.

**Next action: SUPERSEDED (2026-08-12 RunPod session, see below).** This Vast.ai redispatch was the active next action at the time, but RunPod Ubuntu 24.04 has since been validated end-to-end and is now the primary path (see the 2026-08-12 RunPod session entry below for the current single active next action). This Vast.ai finding (known-good-only selection working, ORT-preflight pip fix applied but unverified end-to-end) remains true and Vast.ai remains available as a fallback path, but no further arbitrary-host Vast.ai redispatch is planned for the current product milestone -- do not act on this specific next-action line.

**First action in next chat: fetch and read `CLAUDE.md` and `docs/ai-project-state.md` from the repo before doing anything else.**

## 2026-08-12 session: RunPod Ubuntu 24.04 L4 — production Reco stack VALIDATED, RunPod desktop track abandoned

**RunPod is now a VALIDATED execution environment for OEV, alongside Vast.ai.** Confirmed end-to-end on the real `JhnsonO/video-stitcher` fork (`53fe10f548d5767ad94ef66aeaedf2d8c7161f27`), `reco-cli` built with `--features cuda`, against a real trimmed footage fixture (`trimmed_GX010197.MP4`/`trimmed_GX010173.MP4`, 3s window) with the real production 1920px YOLOv8n ONNX model (`nms=True`):

- `Selected GPU: NVIDIA L4 (Vulkan)` — real device, zero `llvmpipe` occurrences in the log
- NVDEC active on both streams (`left/right GPU decoder: NVDEC (CUDA)`)
- `CUDAExecutionProvider` genuinely registered for both detectors (ball + player), not just listed as available
- Zero-copy GPU path exercised (no `--no-zero-copy` crutch) — `Decode: GPU zero-copy (CUDA/Vulkan)`
- `h264_nvenc (hardware)` encode
- GPU utilization 76–79% during the actual inference/render window (idle before/after)
- 650 real pipeline events in `events.jsonl`, genuine per-frame ball/player detections at real confidence scores using the real model's class IDs (ball=32, person=0, auto-resolved from model metadata)
- Valid output: `followcam_smoke.mp4`, 130 frames, correct duration

**Two `next_frame_gpu returned None (non-CUDA?)` errors observed at the tail of the run are benign** — end-of-stream signal from the deliberately tiny 3s fixture running out of frames after sync-offset trimming, not a CUDA fault; 130 frames still encoded successfully afterward with zero further errors.

**Critical distinction — the earlier RunPod failures were a base-image problem, not a GPU-runtime problem.** The first RunPod pod tried this session was `runpod/kasm-desktop:1.0.0`, a Kasm desktop image built on **Ubuntu 20.04 "focal"**, which only ships FFmpeg 4.2.7 in its repos — too old for `reco-io`'s `Pixel::VAAPI` usage (6 compile errors, `crates/reco-io/src/ffmpeg/{encoder,hw_upload}.rs`), on top of missing `pip3`, ancient `vulkan-tools` (no `--summary` support), Python 3.8 capping `onnxruntime-gpu` at a CUDA-11-era build requiring a manually-assembled CUDA-11 pip runtime stack, and a `cuda-runtime-12-8` apt dependency conflict that would have tried to pull a newer NVIDIA driver package than the host's actual 570.195.03. **None of these were GPU/Vulkan/CUDA-driver problems** — Vulkan, the CUDA driver API, NVDEC, and Python ORT CUDA EP all passed cleanly on that same desktop pod's hardware (test 1). Switching to a plain RunPod official template, `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` (Ubuntu 24.04, CUDA 12.8.1, no desktop/X11), resolved every one of the base-image gaps in a single clean pass — FFmpeg 6.1.1 already present, `pkg-config`/`libclang`/ffmpeg-dev all installed cleanly via one `apt-get install`, and the `reco-cli` `cuda`-featured build compiled in under 2 minutes with zero dependency-chasing cycles (versus 8+ on the desktop image).

**Validated environment recipe (RunPod, `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404`, driver 580.159.04 observed this session, CUDA 12.8):**
- Build `reco-cli` with `--features cuda`, `ORT_CUDA_VERSION=12` (matched to the driver's CUDA-12 class — **do not** reuse Vast.ai's `ORT_CUDA_VERSION=13`/`libcudnn9-cuda-13` pairing, that was matched to CUDA-13-class Vast hosts specifically)
- `cuda-cudart-12-8` only (runtime libs) — explicitly **not** the full `cuda-runtime-12-8` meta-package, which depends on `libnvidia-compute-570 >= 570.211.01` and would attempt to pull a newer NVIDIA driver package than the host's actual installed driver; confirmed this fails cleanly as a dependency conflict rather than silently breaking anything
- `libcudnn9-cuda-12`
- Standard build deps not preinstalled on this base: `git curl build-essential pkg-config cmake libavutil-dev libavcodec-dev libavformat-dev libswscale-dev libavdevice-dev libavfilter-dev libswresample-dev libssl-dev libclang-dev clang`, Rust via `rustup`
- EGL ICD override (same trick proven on Vast.ai): write `/tmp/nvidia_egl_icd.json` pointing at `libEGL_nvidia.so.0`, export both `VK_DRIVER_FILES` and `VK_ICD_FILENAMES` to it
- This base is headless (no X11/desktop) — `DISPLAY` unset is defensive/harmless here, not load-bearing the way it was on the Kasm desktop image
- Real production stitch flags confirmed working: `--projection l-shape --tracking field --panner-preset broadcast --lookahead 1.5 --detection-interval 1 --events <path> --width 1920 --height 1080`, **no** `--no-zero-copy`

**Vast.ai status: remains a valid fallback path, not deprecated.** No further arbitrary-host driver/Vulkan/CUDA debugging planned against Vast.ai for the current product milestone — that track is closed per Johnson's standing "production-ready enough for now" direction from the earlier GPU-selection session. RunPod is now the primary path under active development.

**Not yet done, deliberately deferred to a later hardening ticket:** publishing a custom Docker image / RunPod template for this recipe. Current approach is a bootstrap script run against the stock `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` template, chosen specifically to avoid standing up an image-registry/template-publishing pipeline before the real unseen follow-cam validation (next product step) has passed on this environment.

**`runpod_bootstrap.sh` + `runpod_gpu_preflight.sh` added (commit `98dc47a`), then corrected (this entry's session, correction pass) after review found four real issues in the first pass:** the Reco CUDA smoke test wasn't exercising `--model`/the detector path at all (renderer-only, not proof CUDAExecutionProvider or OrtGpuDetector actually worked); the synthetic calibration fixture used two unrelated scenes (testsrc2 + mandelbrot) making calibrate pass/fail probabilistic rather than guaranteed; the cuDNN install step assumed `libcudnn9-cuda-12` was required without evidence (it's known to fail on this exact base while production reco-cli still worked, meaning the base's own bundled CUDA userspace was likely already sufficient); and the preflight's ORT-version-pin logic had a real bug regexing a "CUDA version" out of `$SMI_OUT`, which only ever contains `name,driver_version` -- never a CUDA field -- silently misreading driver numbers as CUDA major versions. All four fixed: smoke test now derives a deterministic overlap-guaranteed fixture from one source (crop-shifted, not two unrelated generators) and requires explicit log evidence for `CUDAExecutionProvider` registration + `OrtGpuDetector ... warmup inference complete` + NVDEC + no-CPU-fallback, not just a zero exit code; cuDNN resolution now inspects the real built `libonnxruntime_providers_cuda.so`'s dependencies via `ldd` and only installs a package if `ldd` proves something is actually missing; the preflight's ORT pin is now a deterministic `onnxruntime-gpu==1.20.2` matching this environment's fixed CUDA-12 contract, with driver-text parsing kept as diagnostic-only logging, never a decision input.

**RunPod reproducibility LIVE VERIFIED (commit `da21975`), on a fresh stock pod, real scripts fetched from that exact commit -- not manual commands.** Live run surfaced and fixed one further real bug beyond the four already caught in review (commit `bdce2c9`):

**Bug found + fixed, commit `da21975`: SIGPIPE/pipefail false-negative in `echo "$VAR" | grep -q PATTERN`.** With `set -o pipefail` active (both scripts), `grep -q` exits the instant it finds a match; on a large captured variable (real `vulkaninfo` output is 75KB+, comfortably past the pipe buffer), the `echo` writer gets SIGPIPE writing the unconsumed remainder into a pipe `grep` already closed -- and `pipefail` reports the whole pipeline as failed even though `grep` genuinely found the match. This caused `PREFLIGHT_VULKAN=FAIL` on the very first live preflight run despite the printed vulkaninfo output correctly showing `deviceType = PHYSICAL_DEVICE_TYPE_DISCRETE_GPU`/`deviceName = NVIDIA L4` -- confirmed root cause directly (exit code 141 = 128+13 = SIGPIPE, reproduced deterministically with/without `pipefail` toggled). Audited both scripts for the same anti-pattern and found 5 more instances (preflight: CUDA driver API check, NVDEC check, ORT smoke check; bootstrap: Vulkan check) -- all fixed the same way, replacing `echo "$VAR" | grep -q ...` with `grep -q ... <<< "$VAR"` (bash here-string, no pipe, no separate writer process, structurally immune rather than worked around).

**Second live run, same pod, fixed scripts: full clean PASS, both scripts, no further issues.**
- `runpod_gpu_preflight.sh`: `PREFLIGHT_VULKAN=PASS`, `PREFLIGHT_CUDA_INIT=PASS`, `PREFLIGHT_NVDEC=PASS`, `PREFLIGHT_ORT_CUDA=PASS`, `PREFLIGHT_RESULT=PASS`. GPU: NVIDIA L4, driver 570.195.03 (this specific pod -- differs from the 580.159.04 seen on the pod used for the original manual validation session, both CUDA-12-class, both work).
- `runpod_bootstrap.sh`: full clean run start to finish, `exit 0`. `reco-cli --features cuda` built in 1m37s from a cold clone (no cache). **cuDNN evidence-based resolution confirmed working exactly as designed**: `ldd` on the real built `libonnxruntime_providers_cuda.so` showed every dependency (`libcudnn.so.9`, `libcublas.so.12`, `libcublasLt.so.12`, `libcurand.so.10`, `libcufft.so.11`, `libcudart.so.12`) already resolved from the base image's own `/lib/x86_64-linux-gnu/` + the installed `/usr/local/cuda-12.8/lib64/` -- script correctly logged "no cuDNN/cublas package install needed" and never attempted the `libcudnn9-cuda-12` apt install that's known to fail on this base. This directly confirms the point 3 correction was right: that package was never load-bearing.
- Deterministic synthetic fixture calibrated cleanly: 1/1 frame pairs matched, 33 real matched points, 66% confidence (lower than real-footage runs, expected -- synthetic crop-pair geometry, not real stereo camera calibration; the fixture's design goal was guaranteed-calibratable, not high-fidelity).
- Real `--model` Reco smoke test: every required log line present -- `Selected GPU: NVIDIA L4 (Vulkan)` (no llvmpipe), `left/right GPU decoder: NVDEC (CUDA)`, `Successfully registered CUDAExecutionProvider` x2, `ORT: CUDA execution provider enabled` x2, `OrtGpuDetector ready` + `OrtGpuDetector: warmup inference complete`, `GpuResident detection: CUDA path (TensorRT/ORT-CUDA)`, `h264_nvenc (hardware)` encode, 20 frames processed, valid `smoke_output.mp4`. Script's own 8-point acceptance check passed all 8.
- The two items flagged as "watch, don't pre-fix" (python3-venv availability, PEP 668 pip block on the preflight's global `pip3 install`) -- **neither triggered.** Base image already provides working `venv` and allows the global pip install. No speculative patch was needed for either, confirming the "test on real evidence, don't import Vast-specific workarounds" instinct was correct.

**Infrastructure recipe is now considered properly proven** -- both scripts run clean from a cold fetch of the merged commit on a fresh stock `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` pod, no manual pre-configuration beyond deploying the pod itself.

## 2026-08-12 session (cont.): Task 4 -- RunPod workflow integration, green-lit, spec confirmed, Codex prompt not yet drafted/pushed

**Green light given for Task 4.** Kept deliberately boring per Johnson's own framing: automate the exact environment that already passed live verification, don't redesign it.

**RunPod REST API mechanics confirmed against RunPod's own current docs (not GraphQL, not guessed):**
- Create: `POST https://rest.runpod.io/v1/pods`, JSON body incl. `imageName: "runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404"`, `gpuTypeIds: ["NVIDIA L4"]` (confirmed exact valid GPU ID string), `ports: ["22/tcp"]`, `containerDiskInGb`, `cloudType`.
- Poll: `GET /v1/pods/{id}` for status + SSH connection info.
- Stop (NOT sufficient for cleanup): `POST /v1/pods/{id}/stop` -- only pauses, does not free resources or guarantee no further billing/cleanup.
- **Terminate (the actual cleanup guarantee): `DELETE /v1/pods/{id}`.** This is the one the workflow's always-cleanup step must call, not stop -- confirmed explicitly, this distinction matters for the "always terminate on success and failure" requirement.

**Two new files planned, not yet drafted/pushed:**
1. `.github/workflows/oev-runpod-followcam.yml` -- launch (RunPod REST API) -> SCP `runpod_gpu_preflight.sh`+`runpod_bootstrap.sh`+new remote script from the pinned commit (same SCP-from-checkout pattern as existing Vast workflows, not a fresh git clone on the pod) -> SSH-run preflight, hard-fail if `PREFLIGHT_RESULT != PASS` -> SSH-run bootstrap, hard-fail if nonzero -> only then SSH-run the follow-cam remote script -> pull back all logs/artifacts unconditionally -> `DELETE /v1/pods/{id}` in a final `if: always()` step.
2. `runpod_followcam_remote.sh` -- **new script, deliberately NOT a reuse of `oev_followcam_test_remote.sh` verbatim**, because that script bundles its own Vast-tuned build/CUDA-13/cuDNN install logic which would redo a build with the wrong CUDA version on top of what `runpod_bootstrap.sh` already produced (exactly the "Vast-style package roulette" Johnson said to avoid). Reuses verbatim: Drive OAuth + file resolution + download logic, the field_roi injection into `match.json` (11-pt left / 13-pt right polygons), the acceptance-check logic (`Autocam: tracking enabled` + real detections + camera movement). Points `reco` calls at the binary `runpod_bootstrap.sh` already built -- no rebuild.

**Resolved ambiguity, explicit decision:** the existing Vast script's stitch invocation includes `--no-zero-copy`, which Johnson confirmed is a **historical Vast diagnostic compromise, not a locked production setting** (it forces CPU decode while still allowing CUDA inference -- doesn't "sidestep CUDA entirely", but does disable the full NVDEC-to-GPU-resident pipeline). **`runpod_followcam_remote.sh` omits `--no-zero-copy` entirely** -- this is the canonical RunPod path, matching exactly what test 3 already proved live in this session (real `GPU zero-copy`, `NVDEC (CUDA)`, `CUDAExecutionProvider`, no CPU fallback). All other follow-cam settings remain locked and unchanged: 1920px YOLOv8n model, `--tracking field`, `--panner-preset broadcast`, `--lookahead 1.5`, `--detection-interval 1`, field ROI, calibration/acceptance logic verbatim from the Vast script.

**Explicit acceptance requirement for the new script:** must log evidence of `GPU zero-copy`, `NVDEC (CUDA)`, `CUDAExecutionProvider` registration, and no CPU fallback -- not just a zero exit code (same lesson as the bootstrap smoke-test correction earlier this session).

**CURRENT SINGLE ACTIVE NEXT ACTION:** draft the Codex prompt for these two files (frozen-file constraints, exact reuse instructions, RunPod API mechanics above as hard constraints), route to Codex, fetch diff, verify against this spec, present to Johnson for review -- **do not push or dispatch until Johnson reviews the draft.** After Task 4 merges and is itself validated (workflow plumbing proven): dispatch one genuinely unseen 15-20s full-source follow-cam segment on RunPod and judge the camerawork -- the actual next product validation step, no more synthetic infrastructure tests after that unless the workflow itself fails.

## 2026-08-12 session (cont.): Task 4 -- RunPod follow-cam workflow LIVE VERIFIED, merged to main

**CURRENT SINGLE ACTIVE NEXT ACTION line above is SUPERSEDED -- Task 4 is done.** Both new files (`.github/workflows/oev-runpod-followcam.yml`, `runpod_followcam_remote.sh`) were written directly (not routed through Codex, per Johnson's explicit instruction that session), pushed to `feat/runpod-followcam-workflow`, and dispatched three times:

1. Run `31556733557` -- FAILED at pod launch, `HTTP 401: Malformed` on `POST /v1/pods`. Root cause: `RUNPOD_API_KEY` repo secret was never actually set. Not a code bug.
2. Run `31557014385` -- FAILED at preflight, `PREFLIGHT_ORT_CUDA=FAIL` / `No module named 'onnx'`. Root cause: pre-existing bug in the frozen `runpod_gpu_preflight.sh` (line 230) -- `pip3 install onnx "$ORT_GPU_PIN"` ran against system Python with no venv and no `--break-system-packages`, silently blocked by PEP 668 on this pod's Python 3.12. Fixed with a one-line change (commit `6c9f21b`), same run also confirmed the earlier "termination didn't happen" report was a false alarm -- the log showed `Pod z2uioikt57xegr termination confirmed (HTTP 204)` the whole time.
3. Run `31557269688` -- **PASSED end-to-end, all 17 steps green.** Real log evidence, not just exit 0: `SmartFileSource: GPU zero-copy (3840x2160, Nv12)`, `Successfully registered CUDAExecutionProvider`, `Autocam: tracking enabled` with real pan_decision yaw movement, and this script's own additional RunPod-specific acceptance gate (zero-copy + NVDEC + CUDAEP evidence, not just tracking) all passed. `followcam.mp4` + `events.jsonl` are in the run's artifact (`oev-runpod-followcam-31557269688`). Pod terminated cleanly (confirmed HTTP 204).

Note: between my push and the first dispatch, Johnson pushed a follow-up commit (`4d3bf272`, "Fix RunPod workflow launch, cleanup, runtime env, disk and Reco SHA gate") directly to the branch, including a new "Verify exact validated Reco revision" gate step not in my original draft. That commit's changes are what actually ran and passed in runs 2 and 3 above (plus my one-line preflight fix on top).

**Merged to main.** Branch `feat/runpod-followcam-workflow` (commits `12a08fb`, `4d3bf272`, `6c9f21b`) merged into `main`.

**Not yet done -- the actual next product step, no more synthetic infrastructure tests needed first:** pull `followcam.mp4` from run `31557269688`'s artifact and visually judge the camerawork on this genuinely unseen 15-20s segment (start=2395s, duration=16s, from the full `GX010197.MP4`/`GX010173.MP4` source pair). A green workflow proves execution only, not product acceptance -- per the standing rule in `ai-project-state.md`, visual evidence outranks aggregate counts for this kind of decision gate.

## 2026-08-12 session (cont. 2): CORRECTION -- zero-copy claim in prior entry was wrong; reco-cli has a real zero-copy bug

**The "LIVE VERIFIED... run 31557269688" entry above is WRONG about product quality.** That run went green on every acceptance signal (tracking, CUDAExecutionProvider, zero-copy log lines) but Johnson's visual review of the resulting `followcam.mp4` showed a corrupted solid green band across the frame. A green CI job proved execution only, exactly the standing caveat -- I wrote the caveat in that same entry and still didn't catch the actual bug until Johnson looked at the video.

**Isolated via A/B, both on RunPod, same segment/geometry/match.json, only the zero-copy flag changed:**
- Run `31557269688` (zero-copy active, `--no-zero-copy` omitted): green CI, corrupted green-band video.
- Run `31558373625` (`--no-zero-copy` added back, diagnostic branch): green CI, clean correct video (Johnson confirmed: "It worked").

**Conclusion:** the corruption is specific to `reco-cli`'s zero-copy NV12 decode/encode path in the `video-stitcher` fork -- an actual bug in that fork, not in RunPod, not in this repo's workflow/script geometry, flags, or match.json. That code path had never been exercised end-to-end before run `31557269688` (Vast has always run `--no-zero-copy`), so this bug would surface on Vast too if zero-copy were ever tried there.

**Fix applied to `main`:** `runpod_followcam_remote.sh` now passes `--no-zero-copy`, matching Vast, as the interim production setting. The zero-copy evidence acceptance-check block is still present but now gated to only run when `--no-zero-copy` is absent from `STITCH_ARGS` -- it's dormant, not deleted, for whenever the underlying bug is fixed and zero-copy is re-enabled. Do not remove `--no-zero-copy` from `runpod_followcam_remote.sh` again until the `reco-cli` NV12 zero-copy bug is actually fixed and re-verified with a visual (not just CI-green) check.

**Open item, not yet scheduled:** diagnose/fix the NV12 chroma-plane bug in `JhnsonO/video-stitcher`'s zero-copy encode path. Likely candidates: chroma plane stride/pitch mismatch, U/V plane pointer not correctly passed through the zero-copy GPU buffer, or a colorspace (BT.601/BT.709) conversion step being skipped only on the zero-copy code path. Not investigated yet -- no reco-cli source was read this session.

**Standing lesson for future acceptance checks in this repo:** a script-level acceptance check that greps logs for "the right things happened" is not sufficient for anything touching pixel/frame correctness -- it can pass while the actual output is broken. Any acceptance gate claiming video-quality validation should either include an automated frame-sanity check (e.g. sample-frame color histogram, not-all-one-color) or explicitly say in its output that visual human review is still required, rather than implying "PASSED acceptance" covers picture quality.

## 2026-08-12 session (cont. 3): Drive-upload gap fixed — RunPod follow-cam output was artifact-only, never reached Drive

**Bug (Johnson caught it):** `oev-runpod-followcam.yml` downloads footage from the OEV Drive folder but never uploaded results back — `followcam.mp4`/`events.jsonl` only ever landed in the GitHub Actions artifact zip, unlike `oev-reco-stitch.yml` (M1), which uploads to a `Stitched/` Drive folder. Confirmed by reading both workflow files directly (no Drive-upload block existed in the followcam workflow).

**Fix @ `6f2bed5` (`.github/workflows/oev-runpod-followcam.yml` only, Claude-authored direct build), pushed to `main`.** Diff verified: single hunk, +65 lines, nothing else touched. New step "Upload follow-cam result to OEV Drive Followcam/ folder", inserted after "Upload run artifacts" / before "Evaluate acceptance", gated on `hashFiles('followcam_artifacts/followcam.mp4') != ''`. Mirrors `oev-reco-stitch.yml`'s `Stitched/` pattern exactly (same OAuth token reuse, same find-or-create-folder logic): creates/reuses a `Followcam/` subfolder of the OEV Drive folder (`18Y8hI_S29BMeg5FEoxlaqoGy1DsQ7GKJ`), uploads `followcam.mp4` as `followcam_<left_clip>`, and `events.jsonl` (if present/non-empty) as `events_<left_clip>.jsonl`. `runpod_followcam_remote.sh` untouched — upload happens workflow-side against files already pulled back by the existing "Pull back logs + outputs" step, no change to remote script output paths.

**UNVERIFIED — not yet dispatched.** Next run of `oev-runpod-followcam.yml` will exercise this for the first time; confirm the Drive-upload step runs (only fires if `followcam.mp4` exists locally, i.e. remote script succeeded) and that `followcam_<left_clip>` + `events_<left_clip>.jsonl` actually appear in the OEV Drive `Followcam/` folder.

## 2026-08-12 session (cont. 4): RunPod GPU fallback pool — implemented, merged, live-dispatched

**Trigger:** `oev-runpod-followcam.yml` failed at pod-launch (`500: no instances currently available`) — single hardcoded `gpuTypeIds: ['NVIDIA L4']`, no fallback. Not a workflow/code bug.

**Fix, built directly (not routed through Codex, per Johnson's explicit instruction this session), single file (`.github/workflows/oev-runpod-followcam.yml`):**
- `create_body` now sends `gpuTypeIds: ['NVIDIA GeForce RTX 4090', 'NVIDIA RTX A5000', 'NVIDIA GeForce RTX 3090', 'NVIDIA L4']` + `gpuTypePriority: 'custom'`. Confirmed against RunPod's live `POST /v1/pods` OpenAPI spec: `gpuTypePriority: "custom"` makes RunPod try the array in order natively — no manual retry loop needed, no separate launch attempts per GPU.
- Added observability (lightweight, no new benchmarking system): on pod-ready, log + emit as step outputs the selected `machine.gpuDisplayName`/`gpuTypeId` and `costPerHr`; on launch, emit an ISO launch timestamp; around the follow-cam remote-script step, capture processing start/end; in the always-run "Pull back logs + outputs" step, compute processing duration and approx GPU cost (`price/hr × duration`) and write all of it into `run_metadata.txt` plus a single summary log line.
- Preflight (`runpod_gpu_preflight.sh`), bootstrap, Reco-SHA gate, cleanup (`DELETE /v1/pods/{id}` in the final `if: always()` step), and Vast.ai workflows are all untouched — same compatibility gate regardless of which of the 4 GPUs is selected, no GPU-specific code paths added.
- No A100/H100/L40S or network volumes added, per Johnson's explicit scope limits.

**Merged to `main`** (feature branch `runpod-gpu-fallback-pool` → merge commit `9a140b9624d4b1e4d07f60bc183b82685990cca1`).

**Live test dispatched: run `31581931231`** (`https://github.com/JhnsonO/ffa-automations/actions/runs/31581931231`), default clip pair (`GX010197.MP4`/`GX010173.MP4`). One immediate status check done (in_progress, no fast failure). **DISPATCHED — UNVERIFIED** — full outcome (which GPU RunPod actually selected, whether preflight passed, GPU/cost log line) not yet inspected.

**CURRENT SINGLE ACTIVE NEXT ACTION:** check run `31581931231` to completion — confirm it selected one of the 4 priority GPUs, preflight PASSED on whichever was selected, and the new `run_metadata.txt`/summary log line (gpu, price/hr, duration, approx cost) appears correctly. If green, this closes the GPU-fallback ticket. The separate Drive-upload fix from the prior session entry (commit `6f2bed5`) is also still unverified by a real run and can be checked from the same run's artifact if it reaches the upload step.

## 2026-08-12 session (cont. 5): RunPod GPU-pool observability still broken after 3 fix attempts — handing off, budget exhausted

**GPU fallback mechanism itself is confirmed working.** 4 consecutive live dispatches (`31581931231`, `31584467337`, `31585222136`, `31585822004`) all launched successfully against the 4-GPU priority pool (`gpuTypeIds: ['NVIDIA GeForce RTX 4090', 'NVIDIA RTX A5000', 'NVIDIA GeForce RTX 3090', 'NVIDIA L4']`, `gpuTypePriority: 'custom'`). All 4 landed on $0.74/hr (not L4's ~$0.45-0.55/hr rate) — good evidence RunPod is honoring the priority order and picking a non-L4 GPU. Cleanup (`DELETE /v1/pods/{id}`) confirmed clean (HTTP 204, attempt 1) on all 4 — no leaked pods, no billing risk.

**Two real problems found, NOT yet solved — both need a fresh session:**

**1. `gpu_type` observability field still logs `unknown` after 2 fix attempts.**
- Attempt 1: read `machine.gpuType.displayName` per RunPod's `POST /pods` OpenAPI schema — still `unknown`.
- Attempt 2 (diagnostic): dumped the raw `machine` field at the poll point that first sees IP+port → came back **`{}` (genuinely empty)**, not a wrong key path.
- Attempt 3: switched to reading the docs' separate top-level `gpu.displayName` object (shown in `GET /pods/{podId}` example response, distinct from `machine`) as primary, `machine.gpuType.displayName` as fallback — **still `unknown`** on run `31585822004`.
- Conclusion: neither `machine` nor top-level `gpu` is populated in the `GET /pods/{id}` response at the exact moment this workflow first sees a public IP + mapped SSH port. `costPerHr` *is* reliably present at that same moment (confirmed all 4 runs, `0.74`), so the poll loop itself is fine — only the GPU-identity fields lag behind IP/port readiness.
- **Not yet tried:** an extra poll (or short delay + second `GET /pods/{id}`) after IP/port resolve, specifically for `gpu`/`machine`, on the theory that RunPod populates GPU metadata only once the pod reaches a fully `RUNNING` desiredStatus, later than SSH becoming reachable. Cost/price already prove the field exists in the payload shape generally — this needs one more live-run confirmation, not further doc-reading.

**2. `PREFLIGHT_NVDEC=FAIL` (`cuvidGetDecoderCaps` → `CUDA_ERROR_NO_DEVICE`) reproduced identically on all 4 dispatches, always at the same $0.74/hr price point**, while `PREFLIGHT_VULKAN`, `PREFLIGHT_CUDA_INIT`, and `PREFLIGHT_ORT_CUDA` all pass every time. This is no longer explainable as one-off host flakiness — 4/4 reproductions at the same price strongly suggests either (a) the specific GPU pool RunPod is allocating first under this account (likely the RTX 4090 tier, matching the $0.74/hr price) has a systemic NVDEC/driver problem, independent of this workflow's code, or (b) something about the `runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` image + that GPU family combination breaks NVDEC specifically. **Preflight is correctly catching this — it is doing its job, not failing itself.** Not investigated further: whether A5000/3090/L4 (lower in the priority list) would pass NVDEC if actually reached — never happens because the 4090-priced pool is always selected first and preflight correctly hard-fails before any lower-priority GPU is tried (there's no retry-next-GPU-on-preflight-fail logic, only RunPod's own launch-time availability fallback — this is expected behavior per the original spec, not a bug).

**Diagnostic idea, not yet run:** temporarily reorder the priority list (e.g. A5000 first) as a one-off manual test to see whether NVDEC passes on a different GPU tier — would help isolate (a) vs (b) above. Do not make this a permanent reorder without confirming it actually fixes NVDEC; revert to the 4090-first spec order afterward regardless of outcome.

**Debug budget for this chat is exhausted (3 diagnose→fix→dispatch cycles on the `gpu_type` sub-problem, plus the original launch-failure fix).** Handing off.

**CURRENT SINGLE ACTIVE NEXT ACTION (supersedes the "cont. 4" entry above):**
1. Fix `gpu_type`/`cost_per_hr` observability: add a short re-poll (e.g. 2-3 extra `GET /pods/{id}` calls, 5-10s apart) after IP/port resolve, specifically waiting for `gpu.displayName` or `machine.gpuType.displayName` to become non-empty, with a bounded timeout and graceful `unknown` fallback if it never populates — don't block pod usability on this, it's observability-only.
2. Separately diagnose the reproducible NVDEC failure: either accept it as a real, currently-uninvestigated RunPod host/image issue (log it, move on, let the existing preflight gate keep protecting against it) or run one manual diagnostic dispatch with A5000 prioritized first to see if NVDEC passes on a different tier.
3. Once preflight actually passes on some run, the rest of the pipeline (bootstrap, Reco SHA gate, follow-cam remote script, Drive upload from the earlier `6f2bed5` fix) is still fully unverified end-to-end on RunPod's fallback pool — none of the 4 dispatches got past preflight.

**First action in next chat: fetch and read `CLAUDE.md` and `docs/ai-project-state.md` from the repo before doing anything else.**

## OEV Test Runtime v1 — Secrets-in-layers check redesign (12 Aug 2026 session)

**Context:** `oev-test-runtime-build.yml`'s original "No-secrets-in-layers check" step (`docker pull` + `docker history --no-trunc`) failed due to runner disk exhaustion from redundant re-pull. Full redesign undertaken to fix the check without duplicating the image pull.

**Correction to a prior state entry:** the assumption that Buildx leaves the image "loaded locally" after `push: true` is wrong for this workflow — the build step uses `load: false` (implicit default), confirmed from run `31619934141` logs. No local Docker daemon image exists after build; verification must always go registry-side.

**Design evolution (each defect found and fixed in sequence):**
1. `docker pull` + `docker history` → disk exhaustion (root cause of original red run). Diagnosed from run `31619934141` log: grep never ran, failure was `no space left on device`.
2. Replaced with `crane export` (merged filesystem) → rejected: a secret added then deleted in an earlier layer disappears from the merged export while still existing in the registry blob. Not a complete check.
3. Redesigned to raw per-layer streaming: `crane manifest` (platform-resolved via `--platform linux/amd64`, since the image is an OCI index/manifest list) → enumerate layer digests + mediaTypes → `crane blob` streams each layer individually (no docker daemon, no full-image reconstruction) → compression-branch (`tar+gzip`/`tar+zstd`/uncompressed `tar`) → `tar -xO` to stdout → `gitleaks stdin`.
4. `gitleaks detect` (deprecated CLI, hidden since v8.19.0) → corrected to `gitleaks stdin`.
5. Gitleaks pinned to `8.30.0` (not `8.30.1`) as a defensive choice — `8.30.1`'s GitHub release was initially reported missing/orphaned-tag (later corrected: the release does exist with assets; kept the 8.30.0 pin anyway as still-defensible, plus added a canary).
6. Added a **gitleaks canary self-test**: pipes a known fake secret through `gitleaks stdin` before trusting the real scan, so a future silently-broken gitleaks build fails loud instead of reporting a false "no leaks found". Canary value went through 2 revisions: `AKIAIOSFODNN7EXAMPLE` doesn't work — it's explicitly allowlisted by name in gitleaks' own default `aws-access-token` rule (confirmed via gitleaks `config/allowlist.go` / issue #1932) because it's the official AWS documentation example key. `AKIAZZZZZZZZZZZZZZZZ` (repeated char) also doesn't work — zero Shannon entropy fails the rule's `Entropy: 3` threshold. Working value, verified locally against real gitleaks 8.30.0 binary before pushing: **`AKIAQWERTYUIOPASDFGH`**.
7. Added `--redact` to all `gitleaks stdin` invocations — without it, a real detected secret would print in plaintext to the public Actions log.
8. All crane/gitleaks binary+checksum downloads hardened with `curl --fail --retry 5 --retry-delay 2` after hitting two distinct transient GitHub release-CDN failures live (a `503` and a `curl: (56) Connection died`) during testing — both cleared on retry, confirmed not code bugs.

**Fast-iteration infrastructure added:** new standalone workflow `.github/workflows/oev-test-runtime-scan-check.yml` (on `main`, `workflow_dispatch` only, `permissions: contents: read, packages: read` — read-only GHCR access) runs just the install+canary+layer-scan steps against an existing `image_ref` input, with no Docker build. Turns iteration from ~35-40min (full rebuild) to ~20-30s. Created because `workflow_dispatch` only recognizes workflow files present on the default branch, even when targeting a different ref — so this file had to be added directly to `main` (confirmed via diff: `+93/-0`, nothing else touched).

**RESOLVED — private-key findings confirmed false positive:** extracted layer `sha256:5a7813e071bfadf18aaa6ca8318be4824a9b6297b3240f2cc84c1db6f4113040` and located the source via `grep -rlP "BEGIN (EC|DSA) PRIVATE KEY"`: all 10 findings trace to a single file, `usr/lib/x86_64-linux-gnu/libgnutls.so.30.37.1`. `dpkg -S` confirms this is the stock Ubuntu package `libgnutls30t64:amd64`, pulled in as an apt dependency in the base image — not repo code. Ran gitleaks v8.30.0 (pinned version) directly against the extracted layer and inspected the 10 raw (unredacted, local-only) findings: they are GnuTLS's own compiled-in known-answer-test (KAT) key material (PRIVATE KEY, EC PRIVATE KEY, DSA PRIVATE KEY, RSA PRIVATE KEY variants) — a standard, documented practice for crypto libraries self-testing RSA/DSA/ECDSA. Confirmed false positive, not a leak.

**Fix implemented — `.gitleaks.toml` allowlist (commit `c5575ca` on `fix/secrets-layer-scan`):** narrow `[[allowlists]]` rule using `regexTarget = "secret"` with 10 literal regex fingerprints (unique 48-char base64 prefix from each of the 10 confirmed findings), since layer scanning pipes concatenated file contents through `gitleaks stdin` and loses per-file path context (so a path-based allowlist won't work here — content-based regex is required). Validated locally before pushing:
- Canary self-test (fake AWS key) still fires with the allowlist active — confirms allowlist isn't accidentally broad.
- The flagged libgnutls layer scans clean (exit 0) with the allowlist active.
- A genuinely different, unrelated real private key (freshly generated via `openssl genrsa`) still triggers a finding — confirms the allowlist is narrowly scoped and doesn't suppress real leaks.

Both `oev-test-runtime-build.yml` (branch) and `oev-test-runtime-scan-check.yml` (branch + main, kept in sync) updated to pass `--config .gitleaks.toml` on all three layer-scan `gitleaks stdin` invocations, plus a checkout step added to `oev-test-runtime-scan-check.yml` (it previously had none, since it's the no-checkout fast-iteration variant) so `.gitleaks.toml` is available at scan time.

**New issue found during verification (separate from the secrets work) — resolved:** dispatching the standalone check twice in a row against the real image reproducibly failed on one specific ~2GB layer (`sha256:cbb9175a9bc5f6553f8c0c5025ea9521898b8a3956ee24798dc35c24c6185053`), both times with `stream error: stream ID 1; PROTOCOL_ERROR; received from peer` from `crane blob`, at 1.49GB and then 1.98GB into the transfer — not a secrets finding (gitleaks reported "no leaks found" on the partial data both times before the pipe died). Root cause: `crane blob` streams directly into `gunzip | tar | gitleaks stdin` with zero retry/resume logic, so any mid-transfer HTTP/2 drop on a multi-GB blob kills the whole pipeline and forces a full restart, which barely fits the workflow's 15-minute timeout even once.

Consulted externally (ChatGPT) on the failure signature and design tradeoffs; confirmed via GitHub's own docs that GHCR's registry API (`https://ghcr.io/v2/{repo}/blobs/{digest}`) returns a `307` redirect to a short-lived signed URL on `pkg-containers.githubusercontent.com` (Azure blob storage backend) — this is the OCI-standard resumable-pull pattern. Also confirmed `crane blob -o` is not a real flag (no output-file option exists on that subcommand); correct usage is `crane blob ... > file`.

**Fix implemented — resumable download-to-file (commit `610ba2d` scan-check.yml branch, `741cd52` build.yml branch, `38296b0` scan-check.yml main):** replaced the direct `crane blob | gunzip | tar | gitleaks stdin` pipe with:
1. `crane auth token -H ghcr.io/{repo}` to get a scoped bearer token (registry prefix is required — omitting it silently defaults to Docker Hub's auth realm and produces a confusing "invalid token" 403, hit and fixed during this session).
2. `curl --http1.1 --fail --retry 8 --retry-delay 3 --retry-all-errors -C -` downloading each layer to a local temp file (`curl -L` needed, since GHCR's `307` hands off to a different host and curl correctly drops the `Authorization` header on that cross-host redirect automatically).
3. SHA-256 digest verification of the downloaded file against the manifest digest before scanning.
4. Scan from the local file (not a live network pipe), then delete the temp file.

Validated locally against the exact previously-failing layer digest: full 2.05GB download succeeded in 53s, digest matched exactly, and the download transparently absorbed 3 transient `503` errors via `--retry` (direct proof the retry logic works, not just theoretical). `timeout-minutes` bumped 15→20 on `oev-test-runtime-scan-check.yml` for headroom (`oev-test-runtime-build.yml` already had 90 min, untouched).

**Files at latest state:**
- `.gitleaks.toml` — new file, `fix/secrets-layer-scan` only (NOT on main), commit `c5575ca`
- `.github/workflows/oev-test-runtime-build.yml` on branch `fix/secrets-layer-scan` (NOT merged to main): `.gitleaks.toml` wiring at `c515a2d`, resumable-download fix at `741cd52`
- `.github/workflows/oev-test-runtime-scan-check.yml` on `main`: checkout step + `.gitleaks.toml` wiring at `7689324`, resumable-download fix at `38296b0`
- `.github/workflows/oev-test-runtime-scan-check.yml` on `fix/secrets-layer-scan`: was stale (predated even main's canary-key hardening) — fully synced at `d69c5e6`, then resumable-download fix at `610ba2d`
- Both workflow files kept in sync manually — confirmed drift happened at least once this session (branch copy of scan-check.yml was stale enough that the first post-allowlist dispatch silently ran old logic); worth a diff check before any future merge, not just a "was it edited" check

**Next action:** a CI dispatch of `oev-test-runtime-scan-check.yml` against `fix/secrets-layer-scan` with the resumable-download fix was in flight when this session's debug budget (3 diagnose→fix→dispatch cycles) was reached — run in progress at dispatch time, not yet confirmed complete. **First action next session: check that run's outcome** (URL will be in chat handoff). If it passes clean, merge `fix/secrets-layer-scan` → `main` (brings in `.gitleaks.toml`, the allowlist wiring, and the resumable-download fix together) and this gate closes. If it still fails, do NOT immediately retry again — the failure mode has already been diagnosed and fixed once; a repeat failure would mean the fix itself has a bug and needs its own diagnosis, not another blind dispatch.

## OEV Test Runtime v1 — Secrets-in-layers check: structural fix + full false-positive triage (13 Aug 2026 session)

**Context:** picked up from the prior session's "run in flight" handoff. That run (`31647881352`) came back **cancelled** — not a code failure, a budget failure: the standalone check's `timeout-minutes: 20` killed the job mid-scan on a second problem layer (`sha256:abf026459f528efac0543168ff07d7037e9415fe3eb956b82f8fd617c7d2db1f`) after the previously-failing layer (`cbb9175a...`) scanned clean in ~11m45s, proving the resumable-download fix held. Bumped `timeout-minutes` 20→90 (commit `cf761e9`) and redispatched (`31658223485`) — completed in 27 min, fully exposing 14 real findings on the `abf026` layer (exit 2): 12 traced cleanly to CUDA/Nsight/kernel-header/perl-header vendor strings via local extraction+`strings`, but 2 `generic-api-key` findings on a literal `"key": "..."` JSON pattern couldn't be reproduced in isolated per-file scans no matter which CollectX binary was tried.

**Structural investigation of the 2 unresolved findings:** built a corpus testing all 9,259 adjacent file-boundary pairs in the layer's tar member order in one batched gitleaks pass (to avoid one gitleaks-subprocess-per-boundary). This surfaced a real cross-file-boundary bleed mechanism (confirmed at the `atmapi.h`→`atmarp.h` boundary, triggered by "api" substring bleeding across the injected test separator) — proving `tar -xO`'s zero-byte-separator concatenation is a structurally unsound design, not just a source of these 2 specific findings. Proposed and initially applied a `tar --to-command='cat; printf "\n"'` newline-separator patch, but this was correctly rejected: a newline separator can still bleed (as literally just demonstrated), so it doesn't eliminate the underlying bug, only shrinks it.

**Real fix — extract-then-`gitleaks dir` (this session's actual merged design):** replaced `gunzip -c | tar -xO | gitleaks stdin` with `gunzip -c | tar -x --no-same-owner --no-same-permissions -C "$layerdir"` followed by `gitleaks dir "$layerdir"`, then `rm -rf "$layerdir"` between layers. Real file boundaries, real filenames — eliminates the concatenation-bleed class entirely (not just these 2 findings) and gives every future finding a real `File:`/`Line:` instead of forcing byte-forensics. Applied identically to both `oev-test-runtime-scan-check.yml` and `oev-test-runtime-build.yml` (previously named differently — build workflow is `oev-test-runtime-build.yml`, standalone is `oev-test-runtime-scan-check.yml`, both under `.github/workflows/`, both were **only ever on `fix/secrets-layer-scan`** despite an earlier session's note that scan-check.yml was pushed to `main` directly — that stale main copy was superseded during this session's merge, see below).

**Full false-positive triage across every layer this fix newly reached (dir-mode gave real paths, making all of this fast and unambiguous):**
- 2 findings on `abf026` layer: RSA **public** keys (not private — confirmed via `openssl rsa -pubin ... -text -noout`, valid 1024-bit SubjectPublicKeyInfo, public exponent 65537) embedded in `qtwebengine_resources.pak` — standard Chromium extension-manifest `"key"` field format.
- 24 findings on a never-before-reached layer (`819e509d...`, first run to survive past `abf026` at all): 3 were **real** — `/etc/ssh/ssh_host_{ecdsa,rsa,ed25519}_key`, actual SSH host private keys baked into the RunPod base image at build time (openssh-server postinst artifact). Allowlisted narrowly by path (inherited-base-layer bytes aren't removable without flattening/rebuilding the RunPod base — not worth it), but this is a **tracked real finding**, not swept under the rug: practical mitigation is regenerating host keys at container startup (`ssh-keygen -A` before sshd starts) so no running pod uses the static inherited identity — **not yet implemented, this session only allowlisted the scanner finding, the startup-regen script is still a TODO**. The other 21 were vendor/stdlib false positives (vim keymap variable names, oauthlib/jwt library test/example code + compiled `.pyc`, a cryptography-library function parameter literally named `key`, CMake's known-public Windows code-signing test cert, rsync-ssl config keyword) — allowlisted by path.
- 3 findings on the next newly-reached layer: `FourKeyMap`, a real CodeMirror/JupyterLab keybinding class name, minifier-mangled into `t.FourKeyMap=` assignment-like syntax across 3 webpack JS chunks — allowlisted by exact path (hash-named chunks, this specific image only).
- 4 findings on the next layer: Rust toolchain bundled docs (`rustup` stable toolchain) — Cargo's own registry-auth documentation showing example `token = "..."` config syntax, and the real AVX/SM4 SIMD intrinsic `_mm256_sm4key4_epi32` appearing in std-doc sidebar JSON for both x86 and x86_64 — allowlisted by path.
- 1 finding on the final layer reached: `mozilla/mp4parse-rust`'s own vendored test file (`.cargo/git/checkouts/...`), a well-known public CENC test key_id/key vector (`7e571d04.../7e574444...`) also present in that project's own `public.rs` on GitHub — allowlisted by path.

**Result — run `31685815975`: fully green.** All 47 layers in the image scanned clean, "No secrets found in any layer." printed, job succeeded. `.gitleaks.toml` now has one content-fingerprint block (10 GnuTLS entries from the prior session) plus 8 new path- and content-fingerprint-scoped blocks from this session (12 CUDA/Nsight/kernel/perl entries, 1 Qt-pubkey entry, 2 path-based blocks for SSH-keys and 21-vendor-hits, 1 JupyterLab block, 1 Rust-toolchain block, 1 mp4parse-rust block) — every allowlist entry is scoped to specific confirmed content or paths, none are blanket rule suppressions, and each was regression-tested locally (canary still fires, an unrelated real secret still fires, the specific target still suppresses) before pushing.

**Merge to `main` — conflict resolved:** `main` had independently diverged (23 commits each way) via unrelated automation (`xbotgo.db` chore commits) plus a **stale copy of `oev-test-runtime-scan-check.yml`** that an earlier session had pushed directly to `main` (pre-dir-mode-fix, `timeout-minutes: 20`, `tar -xO`/`gitleaks stdin` design — see that session's own state-doc entry above documenting why: `workflow_dispatch` requires the workflow file to exist on the default branch to be dispatchable against other refs). Resolved via local clone + manual merge: took `fix/secrets-layer-scan`'s `scan-check.yml` entirely (supersedes the stale main copy), reconciled this file (kept both sessions' narrative, dropped the now-stale "unresolved, do not merge" paragraph that a prior session's `main`-side update had left in place), let `.gitleaks.toml` (branch-only, clean add) and `uploaded.db` (main-only, unrelated) merge automatically with no conflict.

**Files at latest state (post-merge):**
- `.gitleaks.toml` — now on `main`, 9 `[[allowlists]]` blocks total
- `.github/workflows/oev-test-runtime-scan-check.yml` — now on `main`, dir-mode design, `timeout-minutes: 90`, single canonical copy (the stale pre-fix main copy is gone, superseded)
- `.github/workflows/oev-test-runtime-build.yml` — now on `main`, same dir-mode fix mirrored in
- Both workflow files now identical in scan-step design; no more manual-sync-drift risk since only one copy of each exists on `main`

**Next action:** implement the SSH host-key startup-regeneration script (`ssh-keygen -A` or equivalent before sshd starts in whatever entrypoint/startup path this image uses) — the real finding was allowlisted in the scanner but the underlying practice (static inherited host keys) is still live in the image today. Not otherwise urgent; the secrets-in-layers check itself is now fully closed out.

**Post-merge cleanup:** deleted `fix/secrets-layer-scan` (confirmed fully merged into `main`, zero unique commits remaining). No other stray files from this session's local forensic work (layer extraction, boundary-corpus testing, etc.) were ever committed to the repo — all of that happened in a scratch container, not in git.

**Package visibility (decided, not actioned):** considered making the `oev-test-runtime` GHCR package private as defense-in-depth on top of the scan fix. Decision: **skip for now** — no confirmed consumer other than the two GitHub Actions workflows (already authenticate via `docker/login-action` + `GITHUB_TOKEN`, unaffected either way); unclear whether RunPod pulls this image tag directly and anonymously, and there's no API-driven way to change GHCR container visibility (confirmed via GitHub community discussions — visibility toggle is web-UI-only, no REST endpoint exists), so flipping it blind risked breaking an unverified consumer with no easy revert mid-pull. Revisit next time the RunPod pull path is being touched anyway.

## OEV Test Runtime — network-volume migration IN PROGRESS, blocked on RunPod account balance (13 Aug 2026)

**Ticket:** move OEV Test Runtime from a fully-baked ~19.7GB Docker image to a runtime-lite image + a persistent RunPod Network Volume for the heavy build/model assets. Base: `main` @ `ff872ce9`.

**Merged to `main` (`80bb608`, then two follow-up fixes `3371bba`, `d5ddb5b`):**
- `Dockerfile` — stripped to runtime-lite: base image + vulkan-tools/libvulkan1/aria2/curl/ca-certificates + cuda-cudart install block only. Rust toolchain, build-essential/clang/cmake/pkg-config, ffmpeg -dev headers, git-clone+cargo-build, and YOLO export all removed. Adds `/runpod-volume` mount point + PATH/LD_LIBRARY_PATH pointed there. Tag scheme: `v1-lite`.
- `.github/workflows/oev-network-volume-setup.yml` (new) — idempotent find-or-create RunPod network volume via `rest.runpod.io/v1`, live datacenter auto-select via `api.runpod.io/v2/catalog/gpus?include=AVAILABILITY&product=POD` (RunPod API v2 — v1 has no catalog/availability route, confirmed 400 on first dispatch), writes `vars.OEV_NETWORK_VOLUME_ID`.
- `.github/workflows/oev-populate-volume.yml` + `oev_populate_volume_remote.sh` (new) — attaches the volume to a throwaway RunPod pod, builds reco-cli (`--features cuda`) at the pinned SHA + exports YOLO26 s/m/l/x@1920 onto the volume, idempotent via manifest.json SHA/version check. **Not yet exercised by any run** — blocked behind the volume-setup step below.
- `.github/workflows/oev-test-runtime-benchmark.yml` — `create_pod()` gets `networkVolumeId`/`volumeMountPath: /runpod-volume`; acceptance checks read `/runpod-volume/oev-runtime` instead of `/opt/oev-runtime`. gpuTypeIds, MAX_ATTEMPTS, `wait_for_network` timeout (1200s), job timeout (90min), tiered minDownloadMbps ([800,400,None]) all untouched.
- `oev_test_runtime_benchmark_remote.sh` — `RECO_BIN`/`MODEL_PATH` + sanity checks repointed at `/runpod-volume/oev-runtime`.
- `.github/workflows/oev-test-runtime-build.yml` — build steps trimmed to match the lite Dockerfile (dropped `reco_sha` input/build-arg, default tag `v1-lite`, rewrote the post-build smoke-check for the new image contents). Gitleaks canary + secrets-in-layers steps confirmed byte-identical, untouched.

Frozen files confirmed untouched by diff: `oev-runpod-followcam.yml`, `runpod_bootstrap.sh`, `runpod_gpu_preflight.sh`, `runpod_followcam_remote.sh`, `oev-benchmark-pack-prep.yml`.

**`oev-network-volume-setup.yml` dispatch history, this session (3 cycles, code now proven correct up to the point of volume creation):**
1. Run `31700328452` — FAILED. `rest.runpod.io/v1/gputypes` doesn't exist (`400`, "path does not exist in the specification"). Fixed by switching the datacenter-availability query to `api.runpod.io/v2/catalog/gpus?include=AVAILABILITY&product=POD` (confirmed real via that host's live `openapi.json`), commit `3371bba`.
2. Run `31700466893` — FAILED. New endpoint hit a Cloudflare 403 (`error_code=1010`, `browser_signature_banned`) — urllib's default User-Agent looked like a bot. Fixed by setting an explicit `User-Agent` header on all requests in the script, commit `d5ddb5b`.
3. Run `31700543149` — FAILED, but this is a real external blocker, not a code defect: **the catalog query and datacenter auto-select both worked correctly** (returned live availability for 31 datacenters — every RTX 4090 datacenter currently shows `LOW` or `NONE` availability, auto-selected `EU-RO-1` at `LOW` as the best available, matching the intended "no HIGH/MEDIUM → don't fail, just pick the best of what's there" behavior). The `POST /v1/networkvolumes` call itself then failed: `HTTP 500 "create network volume: You must have at least $5 in your account to create a network volume"`.

**Blocker (not code): RunPod account balance is under $5, the minimum required to create a network volume.** Per debug-budget policy, this is not being treated as a further code-fix cycle — there's nothing left in this script to fix; it's an account-funding action for Johnson.

**Not yet done, next chat/session (once RunPod account is topped up above $5):**
1. Re-dispatch `oev-network-volume-setup.yml` on `main` — should now succeed and write `vars.OEV_NETWORK_VOLUME_ID`. Confirm the repo variable was actually set (check Settings → Actions → Variables, or `gh.sh` if extended for it).
2. Dispatch `oev-populate-volume.yml` — this is the one-time paid GPU cost (~15–20 min RTX 4090, roughly $0.20–0.25 at recent rates). Tell Johnson before dispatching (paid compute). Verify it writes a valid `manifest.json` + `reco` binary + 4 YOLO26 models to the volume (pull the run's `build.log`/`timing.log` artifact).
3. Dispatch `oev-test-runtime-build.yml` to publish the new `v1-lite` image to GHCR (confirm the build workflow diff itself hasn't been exercised by any run yet either).
4. Dispatch `oev-test-runtime-benchmark.yml` against the new lite image + volume. Verify pull time no longer hits anywhere near the 1200s `wait_for_network` ceiling, and that calibration/tracking/detection/pan acceptance checks still pass (same `oev_test_runtime_benchmark_remote.sh` logic, just repointed at the volume).
5. Produce the deliverable: image size before (~19.7GB, 47 layers, confirmed via GHCR manifest earlier) vs after (new lite image, size TBD from step 3's run), pull time before vs after (from `timing.json`), and an ADOPT/DO-NOT-ADOPT verdict.



## OEV network-volume-setup — RESOLVED, volume live (13 Aug 2026, follow-up session)

RunPod account topped up above $5. Two more real bugs found and fixed this session (debug cycles 1-2 of 3):

1. Run `31701097517` - FAILED. Datacenter auto-select picked `EU-CZ-1` (has RTX 4090 GPU availability, does NOT support network volumes) - `POST /v1/networkvolumes` returned `HTTP 500` naming the 19 actual volume-capable datacenters. Fixed by intersecting the GPU-availability candidates against that known volume-capable set before ranking. Commit `fc04715`.
2. Run `31701250820` - datacenter selection now correctly picked `EU-RO-1` and the volume **was created** (`id=gdso18q8kw`, `EU-RO-1`, 100GB). The next step - writing `vars.OEV_NETWORK_VOLUME_ID` via the workflow's own `GITHUB_TOKEN` - got `HTTP 403 "Resource not accessible by integration"`. Root cause: repo's Settings -> Actions -> General -> "Workflow permissions" is set to read-only, which caps `GITHUB_TOKEN` regardless of the `permissions: actions: write` block in the yml. Not fixed at the repo-settings level (would need Johnson to flip that setting to "Read and write permissions" for future runs to self-heal this step). Worked around this run only by writing the variable directly via API with a PAT: `OEV_NETWORK_VOLUME_ID=gdso18q8kw` is now set and confirmed (`201` on create).

Current state: `vars.OEV_NETWORK_VOLUME_ID = gdso18q8kw` (EU-RO-1, 100GB) is live and set. `oev-network-volume-setup.yml` itself will still 403 on the variable-write step on any future re-run until the repo Settings permission is changed - harmless since the variable is already correct and the step is idempotent/best-effort, but worth flagging to Johnson.

Not yet done, next chat/session:
1. (Optional, Johnson) Settings -> Actions -> General -> Workflow permissions -> "Read and write permissions", so `oev-network-volume-setup.yml` stops 403ing on re-runs.
2. Dispatch `oev-populate-volume.yml` - one-time paid GPU cost (~15-20 min RTX 4090, roughly $0.20-0.25 at recent rates). Tell Johnson before dispatching (paid compute). Verify it writes a valid `manifest.json` + `reco` binary + 4 YOLO26 models to the volume.
3. Dispatch `oev-test-runtime-build.yml` to publish the new `v1-lite` image to GHCR.
4. Dispatch `oev-test-runtime-benchmark.yml` against the new lite image + volume. Verify pull time no longer hits near the 1200s `wait_for_network` ceiling, and calibration/tracking/detection/pan acceptance checks still pass.
5. Produce the deliverable: image size before (~19.7GB, 47 layers) vs after, pull time before vs after, ADOPT/DO-NOT-ADOPT verdict.

**First action in next chat: fetch and read `CLAUDE.md` and `docs/ai-project-state.md` from the repo before doing anything else.**
## OEV network-volume — populate-volume RESOLVED, volume populated (13 Aug 2026, run `31701565656`)

Dispatched `oev-populate-volume.yml` on `main`. All 11 steps green, verified against logs (not just exit code):
- `cargo build --release -p reco-cli --features cuda` — Finished in 1m34s, no errors.
- All 4 YOLO26 models exported @1920 and written to `/runpod-volume/oev-runtime/models/`: `yolo26s.onnx` (37.8MB), `yolo26m.onnx` (79.4MB), `yolo26l.onnx` (96.3MB), `yolo26x.onnx` (214.2MB).
- `manifest.json` written with sha256 checksums for all 4 models.
- Pod `mp0ah6q6kkf13v` terminated confirmed (HTTP 204, attempt 1) — no leak.

**Current state:** network volume `gdso18q8kw` (EU-RO-1, 100GB) now holds a built `reco-cli` binary + manifest + all 4 YOLO26 models.

Not yet done, next chat/session:
1. Dispatch `oev-test-runtime-build.yml` to publish the new `v1-lite` image to GHCR.
2. Dispatch `oev-test-runtime-benchmark.yml` against the new lite image + volume. Verify pull time no longer hits near the 1200s `wait_for_network` ceiling, and calibration/tracking/detection/pan acceptance checks still pass.
3. Produce the deliverable: image size before (~19.7GB, 47 layers) vs after, pull time before vs after (from `timing.json`), ADOPT/DO-NOT-ADOPT verdict.

**First action in next chat: fetch and read `CLAUDE.md` and `docs/ai-project-state.md` from the repo before doing anything else.**

## OEV Test Runtime v1-lite — image built + published to GHCR (13 Aug 2026, runs `31702638274`→`31707056434`→`31708241913`)

`oev-test-runtime-build.yml` published `ghcr.io/jhnsono/oev-test-runtime:v1-lite` @ `sha256:3434a07b0c389af45c8ad35b7075946fc430ae597692bcb2d03e746c1f2528c4`. Build/push/gitleaks-scan passed on the first two attempts; the smoke-check step needed 2 fixes:

1. `0c90c4a` — `vulkaninfo --summary` fails with `ERROR_INCOMPATIBLE_DRIVER` on the GPU-less GHA runner (correct behavior, no driver present) — tried `vulkaninfo --help` as a driver-independent alternative. Wrong: still failed.
2. `e6236c1` — `vulkaninfo --help` itself exits 1 by design (confirmed against Vulkan-Tools source: `main()` returns 1 after printing help) — not a GPU issue, a tool quirk. Fixed with `which vulkaninfo` (presence-only check, sidesteps invoking the tool at all). **This is the correct final form — do not swap to `command -v` without wrapping in `sh -c`, which would be a structural change; leave as `which`.**

Run `31708241913`: all 11 steps green, including smoke-check (ffmpeg -version, `which vulkaninfo`, aria2c --version, both `/runpod-volume/oev-runtime/{bin,models}` dir checks).

Not yet done, next chat/session:
1. Dispatch `oev-test-runtime-benchmark.yml` against the new `v1-lite` image + populated network volume (`gdso18q8kw`). Verify pull time no longer hits near the 1200s `wait_for_network` ceiling, and calibration/tracking/detection/pan acceptance checks still pass.
2. Produce the deliverable: image size before (~19.7GB, 47 layers) vs after (v1-lite, size TBD — check via GHCR manifest), pull time before vs after (from `timing.json`), ADOPT/DO-NOT-ADOPT verdict.

**First action in next chat: fetch and read `CLAUDE.md` and `docs/ai-project-state.md` from the repo before doing anything else.**

## OEV Test Runtime v1-lite — BENCHMARK COMPLETE, ADOPT (13 Aug 2026, run `31713169685`)

Dispatched `oev-test-runtime-benchmark.yml` against `v1-lite` + populated network volume `gdso18q8kw`. All 14 steps green. Volume acceptance gate confirmed `manifest.json` + `reco --version` readable from the mounted volume before any render started (no rebuild/re-export — `model_export_s=0`, `reco_build_s=0` in `timing.json`, confirmed).

**Image size, confirmed via GHCR manifest (not estimated):**
| | layers | size |
|---|---|---|
| old baked image (`v1-reco-53fe10f5`) | 47 | 19.66GB (confirmed earlier session) |
| new `v1-lite` + volume | 38 | 10.57GB (image only; heavy assets — reco binary + 4 YOLO26 models — live on the 100GB network volume instead) |

**Timing breakdown (`timing.json`, full):**
```
job_start_to_pod_requested_s:        74.6
pod_requested_to_network_ready_s:    13.0
network_ready_to_ssh_preflight_pass_s: 27.0
image_acceptance_check_s:            15.0
benchmark_pack_download_s:           42.5
render_total_wrapper_s:              260.3  (env_sanity 0.3 + calibrate 8.3 + render 249.3)
model_export_s:                      0
reco_build_s:                        0
total_wall_clock_s:                  432.3  (~7.2 min)
```
GPU: RTX 4090 @ $0.74/hr → this run cost ≈ 432s × $0.74/3600 ≈ **$0.09**.

**Compared to the ~40min/44GB old full-download-and-build baseline: ~5.5x faster wall clock, ~1.9x smaller image.** The old path's ~40min included a full `cargo build`+model-export cycle on every run; this run did neither (both `_s` fields are 0) because the volume already carries the built binary/models.

**Acceptance checks (from job log, real evidence not just exit code):**
- `left/right decoder: NVDEC (CUDA) (3840x2160)` — real hardware decode confirmed on both streams.
- `reco_calibrate`: 193 matched points, confidence 1.00.
- Render GPU backend: `llvmpipe` (software Vulkan) — matches the long-standing, already-accepted pattern (NVDEC decode is real GPU; render/calibrate backend software-only is a known, non-blocking characteristic of this pipeline, not a regression from the volume-migration work).
- `Acceptance OK: AI tracking confirmed active; zero rebuild/re-export/re-install evidence confirmed.`
- Pod `ks8i8npxhh9qd7` termination confirmed (HTTP 204, attempt 1).

**Verdict: ADOPT.** `v1-lite` image + network-volume architecture is confirmed working end-to-end, materially faster and smaller than the old fully-baked image, no rebuild tax on any run. This closes the OEV Test Runtime network-volume migration ticket.

**Not yet done, optional follow-up:** (1) decide whether to deprecate/stop publishing the old `v1-reco-53fe10f5` full-baked-image build path now that `v1-lite` is proven; (2) the repo Settings → Actions → Workflow permissions read-only issue (from the network-volume-setup 403, still un-flipped) remains a minor, non-blocking annoyance on any future `oev-network-volume-setup.yml` re-run.

**First action in next chat: fetch and read `CLAUDE.md` and `docs/ai-project-state.md` from the repo before doing anything else.**
