# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 23 June 2026  
**Authority:** Compact source of truth for AI work. Replace obsolete state rather than adding chat transcripts. Update after any decision, code/workflow change, completed/failed run, or new artifact.

## Start here

1. Read this file and `CLAUDE.md`.
2. Read only active-task files using targeted search/line ranges.
3. Preserve frozen boundaries.
4. A workflow dispatch is not a result: use `DISPATCHED — UNVERIFIED` only after a real run begins, and `COMPLETED — AWAITING REVIEW` only after the artifact exists.

## Product invariant

Offline 360° football post-production. The camera follows only a credible fused ball path.

- Ball evidence first; temporal evidence can strengthen it.
- Player activity is a search/recovery prior only. It must never set camera yaw or pitch.
- Wide fallback is permitted only after fused evidence fails.
- Keep diagnostics, experiments, and renderer changes separate.

## Frozen / do-not-break

- `ball_tracker/run_tracker.py` v11 is the honest baseline. Do not modify it for Stage 1b, Stage 2, Track A, Track B, diagnostics, or smoke tests.
- v11 static suppression remains intact. Reported confirmed coverage was 11.34%; it eliminated the known fence lock honestly.
- Stage 2 is a separate module, never called v12, and is not wired into the renderer.
- Existing v6 safe fallback remains unchanged.
- Never expose or move secrets, tokens, credentials, or private keys into code, artefacts, logs, or third-party compute unless strictly necessary and explicitly approved.

## Key data contracts

### Stage 1

`stage1_candidates.json` is frame-indexed. Candidate fields include `yaw`, `pitch`, `raw_conf`, `penalty`, `weighted_conf`, `source`, `crop_yaw`, and `region`.

Original YOLO `xyxy` dimensions are not retained. Stage 1 uses yaw-only perspective crops at 0°, 90°, 180°, and 270°, FoV 110°, output 1280×720.

### Stage 2

`tracklets.json` observations are raw associated Stage 1 candidates only: `frame`, `yaw`, `pitch`, `weighted_conf`, `score`, `alternates`. Kalman/predicted positions are transient association aids and are not persisted observations. Gaps are stored separately in `gaps.json`.

## Accepted evidence

### Stage 1 candidate generation

Current test clip:

- 3,597 frames; 10,504 raw candidates (703 Stage 0 reuse, 9,801 new).
- 3,607 pitch-rejected; 6,897 retained.
- 56.5% weighted-confidence reduction.
- Fence region near `(-77.4°, -3.9°)` is down-weighted but not hard-deleted.

Temporal continuity alone is therefore not proof of a ball.

### Stage 2 temporal linking

Implemented separately:

- `ball_tracker/stage2_temporal_link.py`
- `.github/workflows/360-stage2-link.yml`
- `ball_tracker/tests/test_stage2_fixture.py`

Automatic `anchor` status is provisional only. Smooth candidate motion and high confidence are insufficient evidence of a real ball.

### Verified tracklet decisions

- `T0066`: confirmed static false lock. Keep as `untrusted_static` and as calibration evidence for a future general static classifier. Do not hard-code it as a special exception.
- `T0001`, `T0017`, `T0088`: rejected as anchors and smoke-render inputs. T0001/T0088 are confirmed detector false/mislocalised tracklets; T0017 has no credible candidate-centre ball correspondence.
- No smoke render is permitted from any of these tracklets.

### Micro re-detection

**Status: COMPLETED — REVIEWED**

- Stored coordinates and fresh Stage 1 re-detection agree within 0.00–0.58° for the probe samples.
- Stage 1 projection/mapping/serialisation is not the root cause.
- The detector repeats wrong/mislocalised targets while visible balls remain offset.

**Diagnosis:** detector candidate-quality failure, not a Stage 1 geometry defect and not a Stage 2 association defect.

## Track A — hotspot / fallback

Independent. Do not bundle renderer edits into Stage 1b, Stage 2, or Track B.

## Track B — detector-quality audit

**Status: COMPLETED — REVIEWED FOR PRECISION; RECALL INCONCLUSIVE**

Canonical files:

- `ball_tracker/track_b_pack_gen.py` (v2)
- `.github/workflows/360-track-b-audit.yml` (v2)

Completed artifact: Actions run `28034071184`, artifact `7824231279`.

Findings:

- 35 of 60 candidate samples were visibly the same known static/background fence location near `(-77.4°, -3.9°)`.
- 44 of 60 candidates were hotspot-adjacent; only 5 were hotspot-neutral.
- No reviewed tile provides credible `ball_at_centre` evidence; several show a real ball elsewhere in the crop.
- Current candidate precision is unacceptable before static/background contamination is removed.
- Recall is inconclusive: false static candidates prevent many earlier/mid frames from qualifying as zero-candidate; the 15-row zero-candidate audit was not a valid missed-ball measure.

## Stage 1b — confirmed-static quarantine

**Status: BRANCH TRIGGER COMMITTED — AWAITING ACTIONS ACKNOWLEDGEMENT**

Purpose: create a reversible, Stage-1-compatible active candidate view that removes only candidates inside generic **confirmed-static** Stage 0 regions. This prevents known fixed background evidence from flooding later tracking/audits.

Implemented:

- `ball_tracker/stage1b_static_quarantine.py`
- `ball_tracker/tests/test_stage1b_quarantine.py`
- `.github/workflows/360-stage1b-quarantine.yml`

Contract:

1. Uses only Stage 1 candidates plus Stage 0 `hotspot_map.json`.
2. Quarantine condition is generic: `region.peak_duty >= hotspot_map.duty_cycle_threshold` and angular candidate distance `<= region.radius_deg`.
3. No hard-coded coordinates, no detector rerun, and no model/threshold change.
4. Active candidate file remains Stage-1-compatible at `frames`.
5. Excluded candidates remain at top-level `quarantined_candidates`, preserving originals plus reason, region, threshold, radius, and angular distance.
6. Emits `stage1_candidates_quarantined.json`, `stage1b_quarantine_report.json`, and `stage1b_quarantine_report.txt` with counts by static region, frame coverage, raw/weighted confidence sums, and newly zero-candidate frames.
7. Fixture tests include confirmed vs nonconfirmed regions and ±180° seam geometry.

For the current map, the map threshold is 0.6. Only the region centred around `(-77°, -3°)` meets it; the other hotspot regions remain active.

Dispatch route:

- Branch: `run/stage1b-quarantine-20260623`
- Trigger commit: `db38555e41957ccacea90da14cc8fad73af5ec39`
- The workflow runs fixture tests first, downloads current Stage 1 / hotspot-map inputs, produces outputs, uploads to Drive, and publishes a GitHub Actions artifact.
- One immediate status check returned no check record. Do not retry in a loop or create another trigger branch.

## Next gate

1. Obtain Stage 1b action artifact and review the report:
   - quarantine count;
   - candidates remaining;
   - frames newly zero-candidate;
   - only confirmed-static regions were affected.
2. Find the Drive ID for `stage1_candidates_quarantined-<run_id>.json`.
3. Rerun Track B using this quarantined file:
   - candidate precision pack measures residual, non-static false candidates;
   - zero-candidate pack now includes frames emptied by static quarantine, making missed-ball coverage meaningful.
4. Review those two packs before considering detector mitigation, recovery strategy, Stage 1 data-contract improvement, or Stage 2 work.

Do not tune Stage 2, smoke render, or modify the renderer before the quarantined Track B review.

## Efficient AI work protocol

- Batch independent targeted reads. Avoid broad logs and unrelated files.
- Do not narrate routine tool calls.
- For a task result, report only **Changed**, **Verified**, **Dispatched**, and a genuine **Risk** if needed.
- Poll once shortly after dispatch for a quick failure, then wait for the supplied result.

## Change log

- **2026-06-23:** Added living project-state file and `CLAUDE.md` operating contract.
- **2026-06-23:** Reviewed micro re-detect panel; ruled out Stage 1 geometry/serialisation and confirmed detector-quality failure.
- **2026-06-23:** Track B generator/workflow added, then corrected: standard runner, non-top quota, clean review UI, and manifest provenance.
- **2026-06-23:** Reviewed Track B artifact. Confirmed heavy static/background candidate contamination; recall remains inconclusive.
- **2026-06-23:** Implemented and branch-dispatched reversible Stage 1b confirmed-static quarantine with fixture tests and Drive/artifact outputs.
