# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 23 June 2026  
**Authority:** Compact source of truth for AI work. Update after every meaningful decision, code/workflow change, completed/failed run, or new artefact. Replace obsolete state; do not add chat transcripts.

## Start here

1. Read this file and `CLAUDE.md`.
2. Read only active-task files using targeted searches/line ranges.
3. Preserve frozen boundaries.
4. A dispatch is not a result. Record `DISPATCHED — UNVERIFIED` only after a real run starts.

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

Original YOLO `xyxy` dimensions are not retained. Stage 1 uses yaw-only perspective crops at 0°, 90°, 180°, 270°, FoV 110°, output 1280×720. Any Stage 1 geometry check must reproduce that convention.

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

**Status: COMPLETED — REVIEWED FOR PRECISION; RECALL INCONCLUSIVE**

Canonical files:

- `ball_tracker/track_b_pack_gen.py` (v2)
- `.github/workflows/360-track-b-audit.yml` (v2)

Completed artifact:

- GitHub Actions run `28034071184`, artifact `7824231279`.
- Generated 60 candidate-centred samples (50 top-ranked + 10 second-ranked) and 15 zero-candidate rows.

Manual review finding:

- The candidate pool is heavily polluted by the known static/background problem. **35 of 60** reviewed candidates sit at the same fence location near `(-77.4°, -3.9°)` and are visibly not the ball.
- **44 of 60** samples are hotspot-adjacent, while only 5 are hotspot-neutral; the current candidate pack is therefore dominated by known static/background false evidence rather than a balanced residual-detector assessment.
- No reviewed tile provides credible `ball_at_centre` evidence. Several tiles show a real ball elsewhere in the crop, confirming mislocalised/false candidate centres.

What Track B proves:

- Current candidate precision is unacceptable before static/background contamination is removed.
- Stage 1 geometry is not the issue; the detector/candidate field is the issue.

What Track B does **not** yet prove:

- Recall / missed-ball rate. The zero-candidate pack yielded only 2 early, 2 mid, and 11 late rows because false static candidates prevent many earlier/mid frames from qualifying as zero-candidate. At the current 4× wide 110° crop format, it cannot support a reliable missed-ball count.

## Next gate — Stage 1b confirmed-static quarantine

Build a **separate, reversible Stage 1b adapter**. Do not modify Stage 1 generation, Stage 2, or renderer.

Purpose: remove only candidates inside **generic confirmed-static regions** from the active candidate pool so later diagnostics/tracking are not flooded by proven fixed background detections.

Requirements:

1. Input: `stage1_candidates.json` and Stage 0 `hotspot_map.json` using the existing confirmed-static rule (`peak_duty` against the map threshold), not hard-coded coordinates.
2. Output: `stage1_candidates_quarantined.json` plus `stage1b_quarantine_report.txt/json`.
3. Preserve all original candidates in a `quarantined_candidates`/audit field with reason and region; do not silently delete evidence.
4. Active `frames` must contain only candidates eligible for temporal linking or residual precision audit.
5. Report counts by region, frame, raw/weighted confidence, and number of frames newly becoming zero-candidate.
6. No detector rerun and no model/threshold change.

Then rerun Track B against the quarantined output:

- Candidate pack must measure remaining non-static candidate precision.
- Zero-candidate pack must include frames that became empty after quarantine, giving a meaningful missed-ball audit.
- Keep the clean manual-label UI and manifest provenance.

Only after that review may we choose between detector mitigation, targeted recovery, a Stage 1 data-contract improvement, or Stage 2 work.

## Efficient AI work protocol

- Batch independent targeted reads; avoid broad logs and unrelated files.
- Do not narrate routine tool calls.
- For a task result, report only **Changed**, **Verified**, **Dispatched**, and a genuine **Risk** if needed.
- Poll once shortly after dispatch for a quick failure; otherwise wait for supplied result.

## Change log

- **2026-06-23:** Added living project-state file and `CLAUDE.md` operating contract.
- **2026-06-23:** Reviewed micro re-detect panel. Ruled out Stage 1 geometry/serialisation; confirmed detector-quality failure for T0001/T0088.
- **2026-06-23:** Track B generator/workflow added, then corrected: ubuntu-latest runner, non-top quota, reticle, clean headers, and manifest provenance.
- **2026-06-23:** Reviewed Track B artifact. Confirmed heavy static/background candidate contamination and no credible centred-ball samples; recall remains inconclusive. Released reversible Stage 1b confirmed-static quarantine as the next bounded gate.
