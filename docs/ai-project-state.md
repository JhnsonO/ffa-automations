# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 23 June 2026  
**Authority:** Compact source of truth for AI work. Update after every meaningful decision, code/workflow change, completed/failed run, or new artefact. Replace obsolete state; do not add chat transcripts.

## Start here

1. Read this file and `CLAUDE.md`.
2. Read only active-task files using targeted searches/line ranges.
3. Preserve the frozen boundaries.
4. A dispatch is not a result: record `DISPATCHED — UNVERIFIED` only after an actual workflow run starts.

## Product invariant

Offline 360° football post-production. The camera follows only a credible fused ball path.

- Ball evidence first; temporal evidence may strengthen it.
- Player activity is a search/recovery prior only; it must never set camera yaw or pitch.
- Wide fallback is allowed only after fused evidence fails.
- Keep diagnostics, experiments, and renderer changes separate.

## Frozen / do-not-break

- `ball_tracker/run_tracker.py` v11 is the honest baseline. Do not modify it for Stage 2, diagnostics, Track A, Track B, or smoke tests.
- v11 static suppression stays intact; its reported confirmed coverage was 11.34% and it eliminated the known fence lock honestly.
- Stage 2 is separate, never called v12, and is not wired into the renderer.
- Existing v6 safe fallback remains unchanged.
- Never expose or move secrets, tokens, credentials, or private keys into code, artefacts, logs, or third-party compute unless strictly necessary and explicitly approved.

## Key contracts

### Stage 1

`stage1_candidates.json` is frame-indexed. Candidates retain `yaw`, `pitch`, `raw_conf`, `penalty`, `weighted_conf`, `source`, `crop_yaw`, and `region`.

Original YOLO `xyxy` dimensions are not retained. Stage 1 uses yaw-only perspective crops at 0°, 90°, 180°, 270°, FoV 110°, output 1280×720. Any Stage 1 geometry check must reproduce that exact convention.

### Stage 2

`tracklets.json` observations are raw associated Stage 1 candidates only: `frame`, `yaw`, `pitch`, `weighted_conf`, `score`, `alternates`.

Kalman/predicted positions are transient association aids, not persisted observations. Gaps are separate in `gaps.json`.

## Accepted evidence

### Stage 1 candidate generation

Accepted for the current ~2-minute clip:

- 3,597 frames; 10,504 raw candidates (703 Stage 0 reuse, 9,801 new).
- 3,607 pitch-rejected; 6,897 retained.
- 56.5% weighted-confidence reduction.
- Fence region near `(-77.4°, -3.9°)` remains down-weighted, not hard-deleted.

Temporal continuity alone is not ball proof; static hotspot tracklets require explicit static rejection.

### Stage 2 temporal linking

Implemented separately:

- `ball_tracker/stage2_temporal_link.py`
- `.github/workflows/360-stage2-link.yml`
- `ball_tracker/tests/test_stage2_fixture.py`

It links candidates into tracklets, preserves alternates, calculates static/motion/residual features, and emits `tracklets.json` plus `gaps.json`.

**Automatic `anchor` status is provisional only.** Smooth candidate motion and high detector confidence are not proof of a real ball.

## Verified tracklet decisions

- `T0066`: confirmed static false lock. Keep as `untrusted_static` and calibration for a future general static classifier; do not hard-code it as a special exception.
- `T0001`: verified detector false-positive/mislocalised tracklet. Never use as anchor, smoke input, or Stage 2 tuning evidence.
- `T0017`: rejected by visual pack; no credible candidate-centre ball correspondence. Never use as anchor, smoke input, or Stage 2 tuning evidence.
- `T0088`: verified detector false-positive/mislocalised tracklet. A nearby visible ball does not rescue it because its candidate centre is not on the ball.

No smoke render is permitted from these tracklets.

## Micro re-detection gate

**Status: COMPLETED — REVIEWED**

- Stored coordinates and fresh Stage 1 re-detection agree within 0.00–0.58° for T0088 frames 952/977/990 and T0001 frame 14.
- Stage 1 mapping, serialisation, and projection are not the root cause.
- The fresh detector repeats wrong/mislocalised targets while visible balls remain offset.

**Diagnosis:** detector candidate-quality failure, not a Stage 1 geometry defect and not a Stage 2 association defect.

## Parallel tracks

### Track A — hotspot / fallback

Independent. Do not bundle renderer edits into Stage 2 or Track B.

### Track B — detector-quality audit

**Status: IMPLEMENTED — REWORK REQUIRED — NOT DISPATCHED**

Current files:

- `ball_tracker/track_b_pack_gen.py`
- `.github/workflows/360-track-b-audit.yml`

The current implementation must be corrected before dispatch:

1. Use a standard GitHub Actions runner. This no-YOLO diagnostic is modest CPU/image work; do not provision Vast.ai or relay Drive access through external compute.
2. Candidate review is currently top-candidate-per-frame only. The manifest must identify the reviewed candidate and its rank; reserve a bounded quota for non-top candidates in multi-candidate frames so Stage 2-relevant false candidates are audited.
3. Manual-review tiles need a clear centre reticle and visible label slots for:
   `ball_at_centre` / `ball_nearby_but_offset` / `not_ball` / `occluded_or_unclear`.
4. Clean review tiles must show only frame ID and centre reticle. Do not show confidence-derived strata such as `high_conf` / `low_conf`, hotspot category, scores, alternates, predictions, or anchor status; retain those only in the manifest/report.
5. Zero-candidate pack: temporal stratification is valid. Each row’s four Stage 1 fixed yaw crops provides horizontal spatial coverage. Do not claim unknown ball pitch-zone stratification without ground truth.
6. `track_b_manifest.json` must record deterministic seed, sample type, frame, reviewed candidate index/rank, yaw/pitch, source/crop_yaw, candidate count, and hidden strata. It must retain enough provenance to recreate every tile.

Expected outputs after corrected dispatch:

- `candidate_precision_review_pack.png` — 60 individual candidate samples.
- `zero_candidate_coverage_review_pack.png` — 15 zero-candidate rows with four Stage 1 crops each.
- `track_b_manifest.json`, `track_b_report.txt`, `run_summary.json`.

No YOLO and no `tracking.json` in Track B. No automatic quality conclusion before human labels are reviewed.

## Next gate

1. Correct the Track B generator/workflow only to satisfy the six pre-dispatch requirements above.
2. Commit the corrections and update this file to `IMPLEMENTED — READY TO DISPATCH`.
3. Dispatch with the two Drive IDs. Then record `DISPATCHED — UNVERIFIED`.
4. Review both packs and label the results.
5. Only then choose one response: candidate filtering/detector mitigation, targeted recovery strategy, or a bounded Stage 1 data-contract improvement.

Do not tune Stage 2, run smoke rendering, or modify the renderer before Track B review.

## Efficient AI work protocol

- Batch independent targeted reads; avoid broad logs and unrelated files.
- Do not narrate routine tool calls.
- For a task result, report only **Changed**, **Verified**, **Dispatched**, and a genuine **Risk** if needed.
- Poll once shortly after dispatch for a quick failure; otherwise wait for supplied result.

## Change log

- **2026-06-23:** Added living project-state file and `CLAUDE.md` operating contract.
- **2026-06-23:** Reviewed micro re-detect panel. Ruled out Stage 1 geometry/serialisation; confirmed detector-quality failure for T0001/T0088.
- **2026-06-23:** Track B generator/workflow added, but pre-dispatch review found an unnecessary Vast.ai workflow, review-bias leakage, no explicit centre/label UI, and top-only candidate sampling. Status corrected from `DISPATCHED — UNVERIFIED` to `IMPLEMENTED — REWORK REQUIRED — NOT DISPATCHED`.
