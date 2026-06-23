# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 23 June 2026  
**Authority:** Compact source of truth for AI work. It supersedes older chat summaries where they conflict. Update this file after every meaningful decision, code/workflow change, completed/failed run, or new artefact.

## Start here

1. Read this file and `CLAUDE.md` first.
2. Read only the files named by the active task; use targeted searches/line ranges.
3. Preserve frozen boundaries.
4. Replace obsolete state rather than accumulating chat history.

## Product invariant

Offline 360° football post-production. The camera follows only a credible fused ball path.

- Ball evidence first; temporal evidence may strengthen it.
- Player activity is a search/recovery prior only; it must never set camera yaw or pitch.
- Wide fallback is allowed only after fused evidence fails.
- Keep diagnostics, experiments, and rendering isolated.

## Frozen / do-not-break

- `ball_tracker/run_tracker.py` v11 is the honest baseline. Do not modify it for Stage 2, diagnostics, Track A, Track B, or smoke tests.
- v11 static suppression remains intact; its reported confirmed coverage was 11.34% and it eliminated the known fence lock honestly.
- Stage 2 remains a separate module, never called v12 and not wired into the renderer.
- Existing v6 safe fallback remains unchanged.
- Never place secrets, tokens, credentials, or private keys in code, docs, commits, artefacts, or responses.

## Key contracts

### Stage 1

`stage1_candidates.json` is frame-indexed. Candidates retain `yaw`, `pitch`, `raw_conf`, `penalty`, `weighted_conf`, `source`, `crop_yaw`, and `region`.

Original YOLO `xyxy` dimensions are not retained. Stage 1 uses yaw-only perspective crops at 0°, 90°, 180°, 270°, FoV 110°, output 1280×720. Any Stage 1 geometry check must reproduce that exact convention.

### Stage 2

`tracklets.json` observations are raw associated Stage 1 candidates only: `frame`, `yaw`, `pitch`, `weighted_conf`, `score`, `alternates`.

Kalman/predicted positions are transient association aids, not persisted observations. Gaps are stored separately in `gaps.json`.

## Accepted evidence

### Stage 1 candidate generation

Accepted for the current ~2-minute clip:

- 3,597 frames processed.
- 10,504 raw candidates (703 Stage 0 reuse, 9,801 newly detected).
- 3,607 pitch-rejected; 6,897 retained.
- 56.5% weighted-confidence reduction.
- Known fence region near `(-77.4°, -3.9°)` remains down-weighted, not hard-deleted.

Therefore temporal continuity alone is not ball proof; static hotspot tracklets require explicit static rejection.

### Stage 2 implementation

Implemented separately:

- `ball_tracker/stage2_temporal_link.py`
- `.github/workflows/360-stage2-link.yml`
- `ball_tracker/tests/test_stage2_fixture.py`

It links candidates into tracklets, preserves alternates, calculates static-region/residual/motion features, and emits `tracklets.json` plus `gaps.json`.

**Automatic `anchor` status is provisional only.** Smooth candidate motion and high detector confidence are not sufficient evidence of a real ball.

## Verified tracklet decisions

- `T0066`: confirmed static false lock. Keep as `untrusted_static` and a labelled calibration example for a future general static classifier; do not hard-code it as a special-case exception.
- `T0001`: verified detector false-positive/mislocalised tracklet. Never use as anchor, smoke input, or Stage 2 tuning evidence.
- `T0017`: rejected by visual pack; no credible candidate-centre ball correspondence. Never use as anchor, smoke input, or Stage 2 tuning evidence.
- `T0088`: verified detector false-positive/mislocalised tracklet. Nearby visible balls do not rescue it because the candidate centre is not on the ball.

No smoke render is permitted from these tracklets.

## Micro re-detection gate

**Status: COMPLETED — REVIEWED**

Implemented:

- `ball_tracker/micro_redetect.py`
- `.github/workflows/360-micro-redetect.yml`

Probes: T0088 frames 952, 977, 990; T0001 frame 14 control.

Result:

- Stored coordinates and fresh Stage 1 re-detection agree within 0.00–0.58° on all four probes.
- Therefore Stage 1 mapping/serialisation/projection is not the root cause.
- In every probe, the visible ball is offset from the stored marker; the fresh detector repeats the same wrong/mislocalised target rather than detecting the ball at its visible location.

**Diagnosis: detector candidate-quality failure, not a Stage 1 geometry bug and not a Stage 2 association bug.**

## Parallel tracks

### Track A — hotspot / fallback

Multi-timepoint hotspot scan and renderer wide-fallback remain independent. Do not bundle renderer edits into Stage 2 or Track B.

### Track B — detector-quality audit

**Status: DISPATCHED — UNVERIFIED**

Script: `ball_tracker/track_b_pack_gen.py` (v1)  
Workflow: `.github/workflows/360-track-b-audit.yml` (updated)  
Commit: `5042d7e5115dbcf66b9f33c8172d0e1f5ebb2ee7`

Inputs required (workflow dispatch):
- `equirect_file_id` — Drive ID of `equirect_trim.mp4`
- `stage1_candidates_file_id` — Drive ID of `stage1_candidates.json`

Expected outputs (Drive folder `1gHW29JbvUWnbvJTCC0J8O7r-O1IPZAd8`):
- `candidate_precision_review_pack-{run_id}.png` — 60 tiles, 10×6 grid
- `zero_candidate_coverage_review_pack-{run_id}.png` — 15 rows × 4 crops
- `track_b_manifest-{run_id}.json` — deterministic manifest (seed=42)
- `track_b_report-{run_id}.txt` — stratum counts only
- `run_summary-{run_id}.json`

No YOLO. No tracking.json. Uses `stage1_candidates.json` penalty field for hotspot strata.

Do not accept results until PNG packs are reviewed. Do not tune Stage 2, smoke render, or modify the renderer before review is complete.

## Next gate

1. Dispatch `360 Track B - Detector Review Packs` workflow with equirect + stage1_candidates Drive IDs.
2. Review `candidate_precision_review_pack.png` (60 tiles) and `zero_candidate_coverage_review_pack.png` (15 rows).
3. Update this file to `COMPLETED — AWAITING REVIEW`, then to `ACCEPTED`/`REJECTED` after labels assigned.
4. Then choose exactly one response: candidate filtering/detector mitigation, targeted recovery strategy, or bounded Stage 1 data-contract improvement.

Do not tune Stage 2, dispatch smoke rendering, or modify the renderer before Track B is reviewed.

## Efficient AI work protocol

- Batch independent targeted reads; avoid broad logs and unrelated files.
- Do not narrate routine tool calls.
- For a task result, report only **Changed**, **Verified**, **Dispatched**, and a genuine **Risk** if one exists.
- Poll once shortly after dispatch for a quick failure; otherwise wait for the supplied result.
- Mark a dispatch `DISPATCHED — UNVERIFIED`; only a reviewed artefact can become accepted/rejected.

## Change log

- **2026-06-23:** Added living project-state file and `CLAUDE.md` operating contract.
- **2026-06-23:** Reviewed micro re-detect panel. Ruled out Stage 1 geometry/serialisation as root cause; confirmed detector-quality failure for T0001/T0088; released manifest-driven Track B audit as the next gate.
- **2026-06-23:** Built Track B review pack generator (`track_b_pack_gen.py`, `360-track-b-audit.yml` updated). No YOLO. 60-tile candidate pack + 15-row zero-coverage pack. Status: DISPATCHED — UNVERIFIED. Commit: `5042d7e`.

