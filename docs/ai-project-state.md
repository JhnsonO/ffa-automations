# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 24 June 2026 — FootAndBall benchmark completed; detector-agnostic backward-anchor path and modern-YOLO adapter added.

## Start here

1. Read this file and `CLAUDE.md`.
2. Read only the files named by the active gate.
3. Preserve frozen boundaries.
4. A workflow is `DISPATCHED — UNVERIFIED` until its artifact is inspected.

## Product invariant

Offline 360° football post-production. The camera follows only a credible **fused ball path**.

- Ball evidence comes first; temporal evidence can support it but never prove it alone.
- Player/pose activity is a search and recovery prior only; it must never directly set camera yaw or pitch.
- Wide fallback is allowed only after fused ball evidence fails.
- Experiments stay separate from the renderer and production pipeline.

## Frozen / do-not-break

- `ball_tracker/run_tracker.py` v11 remains the honest baseline.
- Stage 1b quarantine, Stage 2 temporal linker, Tier A filter, and renderer are frozen for the active task.
- Stage 2 remains separate from the renderer and is not called v12.
- No threshold, suppression, score, model, or follow-cam activation is approved.

## Current data contracts

### Stage 1 candidates

Frame-indexed candidates include `yaw`, `pitch`, `raw_conf`, `penalty`, `weighted_conf`, `source`, `crop_yaw`, `region`, and `detection_geometry`.

Fresh Stage 1c detections carry bbox geometry; Stage 0 reuse carries explicit nulls. Stage 1 uses four yaw-only 110° crops at 1280×720.

### Experimental modern-football detector candidates

Any future Ultralytics-compatible football checkpoint must emit detector candidates with:
- `yaw`, `pitch`, `football_conf`, `crop_yaw`, `detection_geometry`, and `source`.
- These are experiment-only and are not consumed by Stage 1/1b/2 or the renderer.

## Verified evidence

### Candidate-quality diagnosis

Generic detector candidate quality is the main limit, not projection or serialisation. Stored/fresh re-detections agree within 0.00–0.58°, but generic detections repeatedly attach to fence, mount, net, turf texture and player/body clutter.

### Stage 1b static quarantine — VERIFIED

Run `28035387017`, artifact `7824742847`: 6,897 candidates before; 3,470 quarantined; 3,427 remain; 1,344 genuinely zero-candidate frames. No detector rerun or threshold tuning.

### Tier A experimental output — REVIEWED, NOT PRODUCTION

Tier A reduces known static clutter but does not establish trusted ball tracking: 520 → 182 total tracklets; anchors 41 → 26; passing 153 → 35; fragments 326 → 121.

Human anchor triage:
- likely ball: `T0001`, `T0025`
- likely false positive: 13
- unclear: 11

This is insufficient to approve a follow-cam bridge.

### Temporal ball-likeness score — FAILED OUT-OF-SAMPLE VALIDATION

Known false positives `T0079`, `T0093`, and `T0080` ranked too highly. Do not tune or activate that score further.

### Geometry propagation — VERIFIED

Run `28107675223`, artifact `7853375656`:
- 158/158 observations carry a `detection_geometry` key
- 145 populated geometry observations
- 13 source-null geometry observations preserved
- geometry coverage 91.77%

Frozen linker remains untouched.

### Multi-cue pose / candidate-fusion diagnostics — COMPLETED, NOT PRODUCTION

- Pose provides useful context but cannot independently identify the ball.
- Candidate fusion is not a valid production direction because it partly rewards agreement with the old tracklet, making it circular.
- Do not create more score packs or tune weights around legacy Stage 2 observations.

### FootAndBall benchmark — COMPLETED, REJECTED FOR THIS FOOTAGE

Run `28114044649`, artifact `7856116823`.

Finding: FootAndBall detected players but did not provide reliable ball detections on the fixed 30 cropped samples from the current 360 footage. Do not swap to FootAndBall.

## Active gate and next action

# MODERN FOOTBALL YOLO + BACKWARD ANCHOR PROPAGATION — CHECKPOINT SELECTION / BENCHMARK

### Active files

- `ball_tracker/experiments/backward_anchor_propagation.py`
- `ball_tracker/tests/test_backward_anchor_propagation.py`
- `ball_tracker/experiments/football_yolo_backward_adapter.py`
- `.github/workflows/360-footandball-benchmark.yml` (completed benchmark; keep as evidence, do not extend)

### What is already built

1. **Backward-anchor propagation core**
   - Starts from an independently credible later anchor.
   - Walks backwards using only future chosen path motion + current detector confidence.
   - Does not use old Stage 2 tracklets as evidence.
   - Stops after a configured short gap rather than inventing a path.

2. **Modern football-YOLO adapter**
   - Accepts a future Ultralytics-compatible football `.pt` checkpoint.
   - Runs four 110° perspective crops over equirectangular footage.
   - Converts boxes to spherical yaw/pitch candidates.
   - Feeds candidates into backward-anchor propagation.
   - Experiment-only: no production integration.

### Required next implementation

Select one verified modern football-specific Ultralytics checkpoint and build a one-off benchmark using the same fixed sample and source-video download pattern already proven by the FootAndBall workflow.

Acceptance criteria:
- checkpoint loads successfully in Actions;
- output includes raw candidate JSON and a visual overlay / contact sheet;
- compare only against current detector evidence on the fixed sample;
- decide keep/reject once; no threshold-tuning loop;
- if it wins visually, run backward-anchor propagation from a manually verified later anchor and inspect the reconstructed path.

### Explicitly out of scope

- production detector swap;
- altering Stage 1/1b/2, Tier A, or renderer;
- automatic follow-cam activation;
- tuning global scores or thresholds;
- using legacy Stage 2 path agreement as proof;
- full model training before the MAX2 benchmark footage arrives.

## Immediate plan for Johnson

1. Buy/receive the GoPro MAX2 and record a controlled daytime benchmark clip at the usual setup.
2. Include easy passes, aerials, shots, ball near fence, ball behind players, and low-contrast moments.
3. Preserve the raw 8K source and one short trimmed equirectangular test file.
4. Use that as the permanent detector benchmark before any fine-tuning.

## Compact change log

- **2026-06-24:** Added detector-agnostic backward-anchor propagation core and unit tests (`1a65c6f`, `8e52546`).
- **2026-06-24:** Added modern football-YOLO to spherical-candidate/backward-path adapter (`99b533f`); no checkpoint selected or production wiring added.
- **2026-06-24:** FootAndBall benchmark completed (run `28114044649`, artifact `7856116823`) and rejected for current 360 footage.
- **2026-06-24:** Candidate-fusion diagnostic rejected as partly circular; stop legacy-tracklet score experiments.
- **2026-06-24:** Pose candidate-selection diagnostic completed; pose is context only, not a ball decision engine.
- **2026-06-24:** Multi-cue visual review completed (run `28109857417`, artifact `7854364494`); pose useful, vertical band/bbox shape not discriminative enough alone.
- **2026-06-24:** Geometry propagation smoke verified (run `28107675223`, artifact `7853375656`).
- **2026-06-24:** Temporal ball-likeness score rejected; do not tune further.
