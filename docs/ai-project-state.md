# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 23 June 2026  
**Authority:** This file is the compact source of truth for AI work on this repository. It supersedes older chat summaries where they conflict. Update it in the same change, or immediately after, whenever a meaningful decision, code change, workflow result, artifact, or acceptance gate changes.

## Start here

1. Read this file first.
2. Read only the files listed under the active task; do not fetch broad logs or unrelated modules.
3. Preserve frozen files and boundaries below.
4. After work, update **Current state**, **Next gate**, and **Change log**.

## Product invariant

This is an **offline** 360° football post-production pipeline. The camera follows only a credible fused ball path. Future frames and targeted re-runs may be used.

- Ball evidence first; temporal evidence can strengthen it.
- Player activity is a **search/recovery prior only**. It must never directly set camera yaw or pitch.
- Wide fallback is allowed only after fused evidence fails, not because a single detector is weak.
- Keep diagnostics, experiments, and production rendering separate.

## Frozen / do-not-break

- `ball_tracker/run_tracker.py` v11 is the honest baseline. Do not modify it for Stage 2, Track A, Track B, diagnostics, or smoke tests.
- v11 bootstrap static suppression is the foundation; reported confirmed coverage was 11.34% and it eliminated the known fence lock honestly.
- Stage 2 is a separate module, not "v12" and not wired into the renderer.
- No activity-driven renderer targets. Existing v6 safe fallback remains unchanged.
- Never put tokens, credentials, secrets, or private keys in code, state docs, commits, or chat output.

## Key data contracts

### Stage 1

`stage1_candidates.json` contains a frame-indexed candidate dictionary. Candidate fields include:

`yaw`, `pitch`, `raw_conf`, `penalty`, `weighted_conf`, `source`, `crop_yaw`, `region`.

Original YOLO `xyxy` box dimensions are **not persisted**. Stage 1 uses four yaw-only perspective crops at 0°, 90°, 180°, and 270°, with 110° FoV and 1280×720 output. Any geometry diagnostic that claims to reproduce Stage 1 must use that exact projection convention.

### Stage 2

`tracklets.json` observations are raw associated Stage 1 candidates only. Each `frames[]` observation holds `frame`, `yaw`, `pitch`, `weighted_conf`, `score`, and `alternates`.

Kalman/predicted positions are transient for association; they are not persisted as observations. Gap records are separate in `gaps.json`.

## Accepted results

### Stage 0 / Stage 1

Stage 1 is accepted for the current ~2 minute clip:

- 3,597 frames processed.
- 10,504 raw candidates: 703 reused from Stage 0 and 9,801 newly detected.
- 3,607 pitch-rejected (34.3%); 6,897 retained.
- Weighted-confidence reduction: 56.5%.
- Known fence region near `(-77.4°, -3.9°)`: 3,478 retained candidates but 90.0% confidence suppression.

The fence is intentionally down-weighted rather than hard-deleted. A temporal linker must therefore explicitly reject static hotspot tracklets; continuity alone cannot be treated as proof of a ball.

## Current implementation status

### Stage 2 temporal linking

Implemented separately:

- `ball_tracker/stage2_temporal_link.py`
- `.github/workflows/360-stage2-link.yml`
- `ball_tracker/tests/test_stage2_fixture.py`

It links candidates into tracklets, carries alternates, computes static-region occupancy / spread / displacement / persistence features, emits `tracklets.json` and `gaps.json`, and has static-hotspot rejection logic.

**Important:** automatic `anchor` status is provisional only. Smooth movement plus detector confidence has already proven insufficient evidence of a real ball.

### Tracklet verification decisions

- `T0066`: confirmed static false lock. Treat as `untrusted_static`; retain as a labelled calibration example for the future general static classifier. Do not hard-code it as an exception.
- `T0001`, `T0017`, `T0088`: not permitted as anchors, smoke-render inputs, or Stage 2 configuration evidence. They require explicit visual evidence.
- No smoke render and no new Stage 2 tuning until the current micro probe is reviewed.

### Verification artifacts

Completed:

- Stage 2 contact sheet.
- Candidate-centred verification pack for T0001, T0017, T0088, with tight and context views plus separate analysis panels.

Review rule: label clean review tiles before viewing alternates or predictions. Valid labels are:

`ball_at_centre` / `ball_nearby_but_offset` / `not_ball` / `occluded_or_unclear`.

A tracklet cannot become verified if it has repeated `not_ball` labels or relies mostly on `ball_nearby_but_offset` / `occluded_or_unclear`.

### Current live gate: micro re-detection

Implemented on `main`:

- `ball_tracker/micro_redetect.py`
- `.github/workflows/360-micro-redetect.yml`

Probe frames:

- T0088: frames 952, 977, 990.
- T0001: frame 14 (control).

The probe re-runs the same model and fixed Stage 1 crop geometry, displays stored-coordinate markers, raw re-detected boxes/centres, and a candidate-centred context crop. A run was reported as dispatched; its result has **not yet been reviewed or recorded here**.

Interpretation:

| Observation | Diagnosis |
|---|---|
| Box covers ball; stored marker elsewhere | Mapping / serialisation / geometry defect |
| Box covers non-ball and stored marker matches it | Detector false positive |
| No box near ball | Detector miss |
| Box and stored marker agree near ball | Stage 2 association is the next suspect |

## Parallel tracks

### Track A — hotspot / fallback

Multi-timepoint hotspot scan and renderer wide-fallback work remain independent from Stage 2. Do not bundle renderer edits into any Stage 2 or diagnostic task.

### Track B — detector-quality audit

The existing `ball_tracker/detector_audit.py` and `.github/workflows/360-detector-audit.yml` are **not valid as a ground-truth gate** because they sample uniformly and cross-reference fence-corrupted v11 `tracking.json`.

Before Track B is run, replace it with a manifest-driven, stratified audit derived from Stage 1 observable features:

- temporal coverage across the clip;
- zero / one / multiple-candidate frames;
- high / medium / low top weighted confidence;
- candidate yaw/pitch coverage;
- hotspot-adjacent versus neutral candidates;
- detector-empty and cluttered frames.

Use `stage1_candidates.json` and the clip; hotspot data may be annotated. Do **not** require v11 `tracking.json` as truth.

## Next gate

1. Retrieve and review `micro_redetect_panel.png`.
2. Record the diagnosis in this file.
3. Only then choose one bounded follow-up:
   - mapping/serialisation investigation;
   - detector false-positive mitigation;
   - detector miss / recovery strategy; or
   - Stage 2 association investigation.

Do not dispatch Track B or change tracking/rendering merely to bypass this gate.

## Efficient AI work protocol

- Prefer targeted function/line reads and one existing workflow pattern over broad file reads.
- Batch independent reads. Do not narrate routine tool calls.
- Do not inspect full logs unless a run fails or an exact decision requires them.
- For a build request, return only: **Changed**, **Verified**, **Dispatched**, and one genuine **Risk** if needed.
- Poll once shortly after a dispatch for an immediate failure; then stop polling until asked or until a result is supplied.
- A workflow dispatch is not a completed result. Mark it `DISPATCHED — UNVERIFIED` until an artifact or run outcome is inspected.

## Change log

- **2026-06-23:** Added this living state file. Recorded Stage 1 acceptance, Stage 2 boundaries, verified-tracklet restrictions, Track A/B requirements, and the pending micro re-detection gate.
