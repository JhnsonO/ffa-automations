from pathlib import Path
p=Path('docs/ai-project-state.md')
t=p.read_text()
heading='## OEV production full-rate frame stride — VERIFIED 2026-08-15'
if heading in t:
    print('state section already present')
    raise SystemExit(0)
section=r'''

## OEV production full-rate frame stride — VERIFIED 2026-08-15

Status: production candidate implemented and hardware-executed on feature branches; **not merged to main**.

### Decision
- Adopt **frame stride 3** as the OEV production AI-analysis cadence candidate.
- At ~59.94fps source this is ~19.98 AI/tracker/panner decisions per second.
- Unlike the earlier testing-only sparse-output experiment, production stride 3 **renders every source frame at the normal output FPS**. Camera poses between AI decision frames are interpolated/smoothed inside Reco; no post-hoc video retiming is part of the production path.
- `--frame-stride` defaults to 1, so upstream/default Reco behaviour remains unchanged unless explicitly enabled.
- Normal OEV RunPod follow-cam on `feature/frame-stride-testing` is wired to `--frame-stride 3`; main remains unchanged pending merge approval/final merged Reco SHA pin.

### Reco production candidate
- Repo/branch: `JhnsonO/video-stitcher` / `feature/frame-stride-testing`
- Candidate SHA: `b2fc622f4b07dfa0c43e3ad9a96ac85b4f450085`
- PR: `JhnsonO/video-stitcher#5`
- Main baseline before this work: `f27cbb6d0d65fcf9a11fb4d82d119ae214695318`
- No wgpu changes.

Implementation:
- CLI: `--frame-stride N`, default 1, currently validated/clamped to 1..=4; stride >1 requires AI tracking and lookahead.
- Decode/copy/render still run for every source frame.
- Detector/tracker/panner state advances only on every Nth source frame.
- Sparse AI decisions use source/media-time cadence; panner FPS and EMA time constants plus ball coast budget are rebased for the stride.
- Lookahead remains source-time correct: 1.5s of ~59.94fps footage still buffers ~90 source frames, while stride 3 presents ~30 future AI states to the panner.
- Full-rate camera poses are interpolated between sparse panner anchors (shortest-path yaw, linear pitch/FOV) and then use the existing centered smoothing path.
- Stereo/shared-buffer CUDA/Vulkan zero-copy architecture is unchanged.

### Verification
Normal Reco CI/targeted checks on the candidate passed the stride-relevant paths including formatter, default/no-default-feature compile, stride unit tests, core interpolation tests, sparse-lookahead tests, GPU backend compile, Linux ARM64, Android, macOS, MSRV, docs, benchmark compile, main tests and secret scan. Remaining repository reds are pre-existing/unrelated: the wgpu Windows `windows`-crate version conflict, cargo-audit/cargo-deny toolchain/policy drift, and an existing `vram_pool.rs` Clippy warning.

Hardware run: FFA Actions `31911506257`, healthy RTX 4090 in EU-RO-1 at `$0.74/hr`, same 30s `GX010197-seed1384188843/sample_01`, YOLO26m, Vulkan/CUDA/NVDEC/ORT-CUDA/shared-buffer zero-copy all active, exact Reco candidate `b2fc622...` rebuilt/tested on the pod.

Observed full-rate production runs:
- stride 1: 1,784 output frames, ~183.535s wall, ~`$0.03773` stitch compute.
- stride 3: 1,784 output frames, normal 60000/1001 output cadence, ~67.064s wall, ~`$0.01379` stitch compute.
- speedup: ~**2.737x**; wall reduction: ~**63.46%**.
- Reco logged `Frame stride: analyze 1/3, render every source frame`, `render 59.94 fps, analyze every 3 frames (19.98 decisions/s)`, and sparse autocam `analysis_fps=19.980`, coast budget 7.
- Both renders completed successfully; stride 3 did not crash, fall back to CPU, or produce sparse 20fps output.

Important evidence caveat: the first production acceptance harness had a post-render measurement bug because JSONL `WorldState`/`PanDecision` events are emitted on the full render cadence in the production buffered path. The harness therefore failed **after both successful renders** and did not preserve the final video/event artifact. The harness was corrected to gate sparse cadence from exact runtime logs and compare the full rendered camera path frame-for-frame. Subsequent retry `31912273486` (EU-RO-1) and one-off `31912645858` (EUR-IS-1) never created a usable pod because RunPod returned no available instances; cleanup passed. No further paid retries were justified that night.

Quality basis remains the earlier same-sample stride matrix (`31904505904`): stride 3 was materially closer to stride 1 than stride 4 in the quality tails (ball p95 ~0.055rad vs ~0.316rad for stride 4; max pan-yaw delta ~2.40° vs ~3.45°), with visual spot checks showing stride 1/2/3 closely aligned. The production path adds full-rate interpolation/smoothing rather than dropping those render frames.

### Recommendation / remaining gate
- **Production candidate: stride 3.** This is worth shipping as a major compute/cost reduction.
- It is **not yet real-time**: ~30s of footage still takes ~67s on this test, so the near-live product goal needs roughly another ~2.2x throughput improvement elsewhere (detector/pipeline overlap/other bottlenecks), rather than automatically moving to stride 4 and accepting weaker tracking tails.
- Reco code is suitable to merge with default stride 1 once owner approval/CLA is handled; OEV production enablement should use stride 3.
- Before treating the feature as final visual sign-off, retain one full-rate stride-3 artifact on the next healthy RunPod allocation and inspect the interpolated output. This is an evidence-quality follow-up, not a known Reco crash/correctness failure.
- No production/main merge performed in this ticket without explicit owner approval.
'''
p.write_text(t.rstrip()+section+'\n')
print('appended production stride state section')
