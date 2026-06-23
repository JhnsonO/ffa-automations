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

**Status: BRANCH TRIGGER COMMITTED — AWAITING ACTIONS ACKNOWLEDGEMENT**

Canonical implementation:

- `ball_tracker/track_b_pack_gen.py` (v2)
- `.github/workflows/360-track-b-audit.yml` (v2)

Audit design:

1. Runs on `ubuntu-latest`; no Vast.ai.
2. Manifest records reviewed rank, yaw/pitch, source/crop yaw, sample type, candidate count, and hidden strata.
3. Candidate review pack is 60 samples: 50 top-ranked plus 10 second-ranked candidates from multi-candidate frames, temporally spread.
4. Candidate tiles show frame ID, a centre reticle, and label slots only: `ball_at_centre / ball_nearby_but_offset / not_ball / occluded_or_unclear`.
5. Zero-candidate pack is 15 temporally stratified rows, each showing fixed Stage 1 yaw crops at 0/90/180/270° with zero-candidate label slots.
6. No YOLO, no `tracking.json`, and no automatic quality conclusion before review.

One-off dispatch route committed because the available GitHub connector cannot call the manual workflow-dispatch endpoint:

- Branch: `run/track-b-audit-20260623`
- Commit: `80b03336ada21d3e15897f2d160cad130ae409cd`
- Workflow: `.github/workflows/360-track-b-branch-dispatch.yml`
- Inputs fixed to the current clip and `stage1_candidates.json` Drive IDs.
- Output: GitHub Actions artifact `track-b-packs-{run_id}` containing `candidate_precision_review_pack.png`, `zero_candidate_coverage_review_pack.png`, `track_b_manifest.json`, `track_b_report.txt`, and `run_summary.json`.

The branch-trigger commit has been made. A single immediate status check returned no check record yet, so do not claim completion or retry dispatch in a loop. Do not create another trigger branch.

## Next gate

1. Obtain the branch-triggered Track B Actions result/artifact.
2. Set status to `COMPLETED — AWAITING REVIEW` only after the job finishes and the artifact exists.
3. Review and label both packs.
4. Then choose exactly one response: candidate filtering/detector mitigation, targeted recovery strategy, or a bounded Stage 1 data-contract improvement.

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
- **2026-06-23:** Track B v2 — all six pre-dispatch corrections applied. ubuntu-latest runner, non-top quota (10 tiles), reticle, clean tile headers, manifest rank/source/strata fields.
- **2026-06-23:** Branch-trigger dispatch committed for Track B using the current Drive clip and Stage 1 candidate file; awaiting Actions acknowledgement/artifact.
