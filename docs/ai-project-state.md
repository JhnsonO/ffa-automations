# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 24 June 2026  
**Authority:** Living source of truth for AI work. Replace obsolete state rather than appending chat transcripts.

## Start here

1. Read this file and `CLAUDE.md`.
2. Read only active-task files using targeted search/line ranges.
3. Preserve frozen boundaries.
4. A successful workflow is not product acceptance. Use `DISPATCHED — UNVERIFIED` until its artifact is reviewed.

## Product invariant

Offline 360° football post-production. The camera follows only a credible fused ball path.

- Ball evidence first; temporal evidence can strengthen it but cannot prove it.
- Player activity is a search/recovery prior only; never set camera yaw or pitch from it.
- Wide fallback is allowed only after fused evidence fails.
- Keep diagnostics, experiments, and renderer changes separate.

## Frozen / do-not-break

- `ball_tracker/run_tracker.py` v11 is the honest baseline. Do not modify it for Stage 1b, Stage 2, Track A, Track B, diagnostics, or smoke tests.
- v11 static suppression remains intact.
- Stage 2 is separate, not wired into the renderer, and must not be called v12.
- Existing v6 safe fallback remains unchanged.

## Current data contracts

### Stage 1

`stage1_candidates.json` is frame-indexed. Candidate fields include:

- `yaw`, `pitch`, `raw_conf`, `penalty`, `weighted_conf`
- `source`, `crop_yaw`, `region`
- `detection_geometry`

`detection_geometry` is present on every candidate:

- fresh YOLO detections: `bbox_xyxy`, width, height, area, aspect ratio, crop width, crop height
- Stage 0 reused detections: same keys with explicit `null` values

Stage 1 uses yaw-only perspective crops at 0°, 90°, 180°, 270°; FoV 110°; 1280×720.

### Stage 2

`tracklets.json` observations are raw associated Stage 1 candidates: `frame`, `yaw`, `pitch`, `weighted_conf`, `score`, `alternates`. Kalman/predicted positions are transient association aids. Gaps are separate in `gaps.json`.

## Established evidence

### Candidate-quality diagnosis

Micro re-detection confirmed stored coordinates and fresh Stage 1 re-detections agree within 0.00–0.58° for probe samples. Projection/serialisation is not the root cause; the detector repeatedly identifies wrong or mislocalised scene targets.

Diagnosis: candidate-quality failure, not geometry mapping or Stage 2 association.

### Stage 1b confirmed-static quarantine

**COMPLETED — VERIFIED**

- `ball_tracker/stage1b_static_quarantine.py`
- `ball_tracker/tests/test_stage1b_quarantine.py`
- `.github/workflows/360-stage1b-quarantine.yml`

Rule: a candidate is quarantined only when its hotspot region has `peak_duty >= hotspot_map.duty_cycle_threshold` and its angular distance is within that region radius.

Verified artifact: run `28035387017`, artifact `7824742847`.

- 6,897 candidates before
- 3,470 quarantined
- 3,427 active candidates remain
- 1,344 frames became genuinely zero-candidate
- confirmed static region: approximately `(-77°, -3°)`

No hard-coded coordinate, detector rerun, or model threshold change.

### Stage 1c detection geometry

**COMPLETED — VERIFIED**

Verified full run: Actions run `28046275937`, artifact `7830052466`.

- GPU: RTX 4090
- 3,597 frames completed
- 6,436 fresh detections with populated geometry
- 462 Stage 0 reuse detections with explicit null geometry

Supporting changes:

- `ball_tracker/stage1_candidate_gen.py`
- `ball_tracker/track_b_pack_gen.py`
- `ball_tracker/tests/test_stage1c_geometry.py`

Stage 1 runtime hardening is active:

- CUDA/GPU preflight and fail-fast CPU rejection
- live unbuffered output
- progress every 100 frames with ETA
- `run_summary.json`
- GPU allowlist: RTX 3090/4090, A40, A100, L40/L40S
- Blackwell rejection
- smoke mode runs 50 frames

### Track B Stage 1c geometry review

**BLOCKED — NEEDS SUCCESSFUL RE-DISPATCH**

The self-contained workflow downloads Stage 1c artifact `7830052466`, runs Stage 1b quarantine inline, then generates Track B review outputs.

- workflow: `.github/workflows/360-track-b-stage1c-quarantined.yml`
- failed run: `28048960467`
- failure point: Stage 1c artifact download authentication
- no Stage 1b or Track B outputs were produced by that run

Expected output after a successful run:

- candidate precision review pack
- zero-candidate coverage pack
- manifest with `detection_geometry`
- report and run summary
- Stage 1b quarantine reports

Do not mark this workstream review-ready until a successful artifact exists.

### Stage 2 static-motion audit

**COMPLETED — ANNOTATION ONLY**

- `ball_tracker/stage2_static_motion_audit.py`
- `ball_tracker/tests/test_stage2_static_motion_audit.py`

The audit adds metrics and `would_reject_static_motion`; it does not change tracklet status, thresholds, linking, renderer, or scores.

Five conditions for the annotation:

1. observations >= 12
2. span >= 20 frames
3. net displacement < 1.5°
4. spread MAD < 0.6°
5. p90 step < 0.25°

Smoke audit: run `28063029760`, artifact `7835756306`.

- 531 tracklets
- 28 would-reject
- 12 borderline
- 8 of 17 human-confirmed near-zero/static anchors caught
- T0499 was correctly reclassified as near-static, not strong motion

No threshold changes are approved.

### Stage 2 repeated-static location audit

**BUILT — AWAITING REAL-DATA RUN AND REVIEW**

- `ball_tracker/stage2_repeated_static_audit.py`
- `ball_tracker/tests/test_stage2_repeated_static_audit.py`
- commit `1086552`

Annotation-only location clustering for repeated near-static tracklets.

- broad eligibility: near-static candidates, excluding existing rejected-static tracklets
- major-motion exclusion protects T0373 and comparable cases
- cluster radius: 4.0°
- repeated-static flag requires at least 3 members, 150-frame temporal span, and 2 distinct temporal windows separated by at least 50 frames

Expected real-data finding to verify: repeated false-positive location around yaw 24.5°, pitch 13.2° across multiple separated tracklets.

No tracklet state, Stage 2 threshold, renderer, or candidate score is modified.

## Active gate and next action

**Primary next action:** run `stage2_repeated_static_audit.py` against the verified Stage 2 smoke `tracklets.json`, then review discovered location clusters.

Required checks:

1. confirm whether the yaw≈24.5°, pitch≈13.2° cluster is discovered;
2. confirm T0373 is excluded by major-motion protection;
3. inspect top discovered location cards before any filtering decision.

**Parallel blocked workstream:** repair and re-dispatch the Stage 1c → Stage 1b → Track B self-contained workflow. Do not treat it as complete until its artifact is inspected.

## Efficient AI work protocol

- Batch independent targeted reads; avoid broad logs and unrelated files.
- Do not narrate routine tool calls.
- Poll once shortly after dispatch for a quick failure, then wait for a supplied result.
- Return only **Changed**, **Verified**, **Dispatched**, and a genuine **Risk**.

## Compact change log

- **2026-06-24:** Reconciled state file and working contract. Fresh chats should bootstrap from `CLAUDE.md` + this file without a large handover.
- **2026-06-24:** Stage 2 repeated-static location audit built and tested; annotation-only, no dispatch yet.
- **2026-06-24:** Stage 2 static-motion audit built and reviewed; annotation-only, no threshold changes.
- **2026-06-23:** Stage 1c geometry preservation verified on full RTX 4090 run `28046275937` / artifact `7830052466`.
- **2026-06-23:** Stage 1b confirmed-static quarantine verified on run `28035387017` / artifact `7824742847`.
- **2026-06-23:** Track B Stage 1c self-contained workflow failed before processing at artifact-download authentication; remains blocked pending a successful re-dispatch.
