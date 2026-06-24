# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 24 June 2026 — geometry propagation smoke reviewed; multi-cue diagnostic ready for one manual dispatch.

## Start here

1. Read this file and `CLAUDE.md`.
2. Read only the files named by the active gate.
3. Preserve frozen boundaries.
4. A workflow is `DISPATCHED — UNVERIFIED` until its artifact is inspected.

## Product invariant

Offline 360° football post-production. The camera follows only a credible **fused ball path**.

- Ball evidence comes first; temporal evidence can support it but never proves it alone.
- Player/pose activity is a search and recovery prior only; it must never directly set camera yaw or pitch.
- Wide fallback is allowed only after fused ball evidence fails.
- Experiments and diagnostics stay separate from the renderer and production pipeline.

## Frozen / do-not-break

- `ball_tracker/run_tracker.py` v11 remains the honest baseline.
- Stage 1b quarantine, Stage 2 temporal linker, Tier A experimental filter, and renderer are not to be changed by the active task.
- Stage 2 remains separate from the renderer and is not called v12.
- No active score, suppression, threshold, or model change is approved from this state.

## Current data contracts

### Stage 1 candidates

Frame-indexed `stage1_candidates.json` candidates include:

- `yaw`, `pitch`, `raw_conf`, `penalty`, `weighted_conf`
- `source`, `crop_yaw`, `region`
- `detection_geometry`

`detection_geometry` is populated for fresh Stage 1c detections and explicit-null for Stage 0 reuse. Stage 1 uses four yaw-only 110° perspective crops at 1280×720.

### Stage 2 tracklets

Observations contain associated Stage 1 candidates: `frame`, `yaw`, `pitch`, `weighted_conf`, `score`, `alternates`.

For the **Tier A experimental output path only**, `detection_geometry` is now post-link propagated onto each observation without changing the frozen linker.

## Verified evidence

### Candidate-quality diagnosis

The generic detector is the present bottleneck, not projection or JSON mapping. Micro re-detection matched stored/fresh positions within 0.00–0.58° but repeatedly found wrong scene targets: fence, mount, net, turf texture, nearby player/body regions and other static clutter.

### Stage 1b static quarantine — VERIFIED

Run `28035387017`, artifact `7824742847`:

- 6,897 candidates before
- 3,470 quarantined
- 3,427 active candidates remain
- 1,344 frames become genuinely zero-candidate

This is evidence-preserving quarantine only. It did not rerun the detector or tune thresholds.

### Tier A experimental output — REVIEWED, NOT PRODUCTION

Tier A reduced the known static clutter but did not establish trusted ball tracking.

- Original → Tier A: 520 → 182 total tracklets
- Anchors: 41 → 26
- Passing: 153 → 35
- Fragments: 326 → 121

Human anchor triage in `ball_tracker/data/tier_a_anchor_adjudication_filled.csv`:

- likely ball: `T0001`, `T0025`
- likely false positive: 13
- unclear: 11

The only confirmed likely-ball examples are too few to approve a follow-cam bridge.

### Temporal ball-likeness score — FAILED OUT-OF-SAMPLE VALIDATION

The diagnostic score used observation count, spatial spread, velocity consistency and net displacement. It is parked.

Known false positives `T0079`, `T0093`, and `T0080` ranked too highly. Therefore sustained or smooth motion is not ball-specific enough to activate a filter or tune weights.

**Do not tune that score further.**

### Geometry metadata propagation — VERIFIED

Root cause: frozen `stage2_temporal_link.py` did not carry Stage 1c geometry into tracklet observations.

Scoped fix: `ball_tracker/stage2_tier_a_experimental_output.py` adds a post-link `(frame, yaw, pitch)` geometry stitch. The linker remains untouched.

Verified smoke:

- workflow run `28107675223`
- artifact `7853375656`
- 158/158 observations have a `detection_geometry` key
- 145 observations contain populated geometry
- 13 preserve source-null geometry
- usable geometry coverage: 91.77%

Supporting tests: `ball_tracker/tests/test_geometry_propagation.py` (7 fixtures).

## Active gate and next action

# MULTI-CUE BALL CANDIDATE DIAGNOSTIC — READY FOR MANUAL DISPATCH

The aim is one bounded visual experiment: test whether independent context cues demote obvious false detections while preserving the two likely-ball examples.

### Active files

- `ball_tracker/experiments/multi_cue_diagnostic.py`
- `ball_tracker/tests/test_multi_cue_diagnostic.py`
- `.github/workflows/360-multi-cue-diagnostic.yml`

### Fixed sample (10 tracklets / 30 frames maximum)

- likely ball: `T0001`, `T0025`
- known false positives: `T0093`, `T0080`, `T0079`, `T0036`
- unclear: `T0130`, `T0030`, `T0090`, `T0175`

### What the diagnostic does

For early/mid/late evidence on each selected tracklet, it renders:

- all same-crop Stage 1 candidates and the selected tracklet candidate;
- source detector confidence;
- vertical playable-view band status — explicitly not a calibrated pitch polygon;
- YOLO pose/person boxes plus nearest person, lower-body and ankle distances;
- Stage 1c bbox width, height, area and aspect ratio;
- existing temporal context;
- a transparent, diagnostic-only fused score.

Missing pose or geometry is neutral/unknown, not a penalty. The fused score must never auto-accept or auto-reject anything.

### Dispatch and acceptance

The workflow downloads Tier A experimental artifact `7846400233` and the fixed equirect source video, then runs `yolov8n-pose.pt` on the 30 selected perspective crops only.

Expected artifact: `multi-cue-ball-diagnostic-<run_id>` with:

- `multi_cue_diagnostic_pack.pdf`
- `multi_cue_diagnostic.csv`
- `multi_cue_diagnostic_summary.txt`

**Acceptance question:** Do the likely-ball examples retain sensible multi-cue support while the known false positives visibly lack player/lower-body/pitch/geometry support? This is a visual review decision only.

### Explicitly out of scope

- no full-video pose pass;
- no pose model fine-tuning or training;
- no football detector swap or threshold change;
- no filter/suppression activation;
- no Stage 1/1b/2 or renderer modifications;
- no follow-cam integration.

## Compact change log

- **2026-06-24:** Multi-cue diagnostic added as a bounded manual-dispatch experiment. Script, six helper tests and workflow are isolated from frozen pipeline files.
- **2026-06-24:** Geometry metadata propagation smoke verified (run `28107675223`, artifact `7853375656`): 145/158 populated geometry observations; frozen linker untouched.
- **2026-06-24:** Temporal ball-likeness score validation rejected. Known false positives ranked too highly; score parked and not to be tuned.
- **2026-06-24:** Tier A experimental human review completed: only T0001/T0025 remain likely-ball; output not approved for follow-cam use.
- **2026-06-24:** Stage 1b static quarantine and Stage 1c geometry preservation remain verified baseline evidence.
