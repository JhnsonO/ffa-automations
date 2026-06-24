# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 24 June 2026 — multi-cue pose review completed; pose-guided candidate-selection diagnostic ready for manual dispatch.

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

### Stage 2 tracklets

Observations contain raw associated candidates. For Tier A experimental output only, `detection_geometry` is post-link propagated without changing the frozen linker.

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

### Multi-cue visual review — COMPLETED

Run `28109857417`, artifact `7854364494` completed after PNG-only packaging.

Finding: pose/person detection and lower-body keypoints work on the selected crop samples. They are useful as **frame-level context**. The existing vertical playable-view band is not useful as a discriminator because Stage 1 already applies equivalent pitch bounds. Bbox shape alone is also insufficient.

Decision: do not add another global tracklet score or another model. Test pose at **candidate-selection level** inside a frame, with a raw-confidence fallback for aerial/occluded play.

## Active gate and next action

# POSE-GUIDED CANDIDATE SELECTION DIAGNOSTIC — READY FOR MANUAL DISPATCH

### Active files

- `ball_tracker/experiments/pose_guided_candidate_selection.py`
- `.github/workflows/360-pose-candidate-selection-diagnostic.yml`

### Fixed sample

10 tracklets / early-mid-late observations only:
- likely ball: `T0001`, `T0025`
- known false positives: `T0093`, `T0080`, `T0079`, `T0036`
- unclear: `T0130`, `T0030`, `T0090`, `T0175`

### What it tests

For every selected observation, compare:

- **blue:** highest raw detector-confidence candidate in the same Stage 1 crop;
- **green:** pose-guided choice when a candidate has nearby detected lower-body keypoints;
- **magenta:** existing Stage 2-associated candidate.

Pose is never a hard rejection. When no candidate has usable lower-body support, green deliberately equals blue (`RAW_FALLBACK_NO_LOWER_BODY_SUPPORT`) so aerial/occluded play is not penalised.

Outputs:
- one PNG review page per tracklet;
- CSV showing raw vs pose-guided choice, whether either matches the existing associated candidate, lower-body distance and selection mode;
- readme explaining that the result is diagnostic only.

### Acceptance question

Across the known examples, does green visibly demote static/fence/tree-style candidates **without** repeatedly overriding plausible likely-ball selections? Review visually; no automatic verdict or pipeline modification follows from this run.

### Explicitly out of scope

- full-video pose inference;
- model training or fine-tuning;
- detector swap/threshold change;
- production candidate filtering or suppression;
- Stage 1/1b/2 or renderer changes;
- follow-cam integration.

## Compact change log

- **2026-06-24:** Added pose-guided candidate-selection diagnostic and manual workflow. It compares raw vs pose-supported candidate selection with an explicit raw fallback.
- **2026-06-24:** Multi-cue visual review completed (run `28109857417`, artifact `7854364494`); pose useful, vertical band/bbox shape not discriminative enough alone.
- **2026-06-24:** Geometry propagation smoke verified (run `28107675223`, artifact `7853375656`).
- **2026-06-24:** Temporal ball-likeness score rejected; do not tune further.
- **2026-06-24:** Tier A human review: only T0001/T0025 remain likely-ball; no follow-cam approval.
