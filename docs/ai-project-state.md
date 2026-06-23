# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 23 June 2026 (full Stage 1c run dispatched)  
**Authority:** Compact source of truth for AI work. Replace obsolete state rather than adding chat transcripts. Update after every decision, code/workflow change, completed/failed run, or artifact.

## Start here

1. Read this file and `CLAUDE.md`.
2. Read only active-task files using targeted search/line ranges.
3. Preserve frozen boundaries.
4. A successful workflow is not product acceptance. Use `DISPATCHED — UNVERIFIED`, then review its artifact before changing any gate.

## Product invariant

Offline 360° football post-production. The camera follows only a credible fused ball path.

- Ball evidence first; temporal evidence can strengthen it.
- Player activity is a search/recovery prior only; it must never set camera yaw or pitch.
- Wide fallback is allowed only after fused evidence fails.
- Keep diagnostics, experiments, and renderer changes separate.

## Frozen / do-not-break

- `ball_tracker/run_tracker.py` v11 is the honest baseline. Do not modify it for Stage 1b, Stage 2, Track A, Track B, diagnostics, or smoke tests.
- v11 static suppression stays intact; reported confirmed coverage was 11.34% and it eliminated the known fence lock honestly.
- Stage 2 is separate, never called v12, and is not wired into the renderer.
- Existing v6 safe fallback remains unchanged.
- Never expose or move secrets, tokens, credentials, or private keys into code, artifacts, logs, or third-party compute unless strictly necessary and explicitly approved.

## Key data contracts

### Stage 1

`stage1_candidates.json` is frame-indexed. Candidate fields include `yaw`, `pitch`, `raw_conf`, `penalty`, `weighted_conf`, `source`, `crop_yaw`, and `region`.

Original YOLO `xyxy` dimensions are not retained. Stage 1 uses yaw-only perspective crops at 0°, 90°, 180°, 270°, FoV 110°, 1280×720.

### Stage 2

`tracklets.json` observations are raw associated Stage 1 candidates only: `frame`, `yaw`, `pitch`, `weighted_conf`, `score`, `alternates`. Kalman/predicted positions are transient association aids. Gaps are separate in `gaps.json`.

## Established evidence

### Stage 1 candidate generation

Current test clip:

- 3,597 frames; 10,504 raw candidates (703 Stage 0 reuse, 9,801 new).
- 3,607 pitch-rejected; 6,897 retained.
- 56.5% weighted-confidence reduction.
- Fence region near `(-77.4°, -3.9°)` was down-weighted but not hard-deleted.

Temporal continuity alone is not ball proof.

### Stage 2 temporal linking

Implemented separately:

- `ball_tracker/stage2_temporal_link.py`
- `.github/workflows/360-stage2-link.yml`
- `ball_tracker/tests/test_stage2_fixture.py`

Automatic anchor status is provisional only. Smooth candidate motion and high confidence are not proof of a real ball.

### Rejected tracklets

- `T0066`: confirmed static false lock; retain as a calibration example for the future general static classifier.
- `T0001`, `T0017`, `T0088`: rejected as anchors/smoke inputs. T0001/T0088 are detector false or mislocalised paths; T0017 has no credible candidate-centre ball correspondence.

No smoke render is permitted from them.

### Micro re-detection

**COMPLETED — REVIEWED**

Stored coordinates and fresh Stage 1 re-detection agree within 0.00–0.58° for the probe samples. Stage 1 mapping/projection/serialisation is not the root cause; the detector repeats wrong or mislocalised targets while visible balls remain offset.

**Diagnosis:** candidate-quality failure, not geometry or Stage 2 association.

## Track A — hotspot / fallback

Independent. Do not bundle renderer edits into Stage 1b, Stage 2, or Track B.

## Track B — original audit

**COMPLETED — REVIEWED FOR PRECISION; RECALL INCONCLUSIVE**

Canonical files:

- `ball_tracker/track_b_pack_gen.py` (v2)
- `.github/workflows/360-track-b-audit.yml` (v2)

Original artifact: Actions run `28034071184`, artifact `7824231279`.

Findings:

- 35 of 60 reviewed candidates were visibly the same static/background fence location near `(-77.4°, -3.9°)`.
- 44 of 60 candidates were hotspot-adjacent; only 5 were hotspot-neutral.
- No reviewed tile provided credible `ball_at_centre` evidence.
- Recall was inconclusive because static candidates prevented many early/mid frames from being zero-candidate frames.

## Stage 1b — confirmed-static quarantine

**COMPLETED — VERIFIED**

Implemented:

- `ball_tracker/stage1b_static_quarantine.py`
- `ball_tracker/tests/test_stage1b_quarantine.py`
- `.github/workflows/360-stage1b-quarantine.yml`

Generic rule:

`region.peak_duty >= hotspot_map.duty_cycle_threshold` **and** candidate angular distance `<= region.radius_deg`.

No hard-coded coordinates, detector rerun, or model/threshold change. Active candidates remain at top-level `frames`; excluded evidence is preserved at `quarantined_candidates` with reason, region, threshold, radius, and angular distance.

Verified Stage 1b artifact: Actions run `28035387017`, artifact `7824742847`.

- 6,897 candidates before.
- 3,470 quarantined.
- 3,427 active candidates remain.
- 1,344 frames became genuinely zero-candidate.
- Only one map-confirmed static region qualified: `(-77.0°, -3.0°)`, radius 1.414°, peak duty 0.8875, against map threshold 0.6.
- Validation: no remaining active candidate lies inside that confirmed-static region.

The intended Stage 1b Drive upload did not surface through the connected Drive search, so downstream work must not depend on a Drive ID for the quarantined output.

## Track B — quarantined audit

**Status: COMPLETED — AWAITING REVIEW**

Run ID:  — succeeded 2026-06-23T15:52Z  
Artifact ID:  —  (~11.4 MB)

A one-off local-chain run eliminates the fragile Drive handoff: it downloads the original Stage 1 + hotspot map, builds the same Stage 1b local output, and feeds it directly into Track B.

- Branch: `run/track-b-quarantined-20260623`
- Trigger commit: `4a1ff97c1df519c65f03c17609a8aa16a3d94a2d`
- Workflow: `.github/workflows/360-track-b-quarantined-branch-dispatch.yml`
- Outputs: `candidate_precision_review_pack.png`, `zero_candidate_coverage_review_pack.png`, `track_b_manifest.json`, `track_b_report.txt`, `run_summary.json`, and the Stage 1b reports in one GitHub Actions artifact.

This run is CPU-only, uses no YOLO, does not touch Stage 2 or renderer logic, and does not need a new Drive file ID.

## Stage 1c — detection geometry preservation

**Status: STAGE 1C — FULL RUN DISPATCHED — UNVERIFIED**

Previous full run (Actions run `28041924767`) aborted: RTX PRO 4000 Blackwell GPU incompatible with PyTorch 2.1.0-cuda11.8. No valid Stage 1c artifact.

**Full run dispatched: Actions run `28046275937` — DISPATCHED — UNVERIFIED**
- Branch: `main` @ HEAD
- Inputs: `smoke_test=false`, `max_frames` unset (full clip), standard Drive IDs.
- GPU selected: RTX 4090 (allowlisted ✓), offer id=42214650, $0.445/hr.
- Artifact name: `stage1-candidates-28046275937` (expected).
- Startup check: GPU allowlisted confirmed; CUDA/preflight/first-progress pending (run still provisioning at check time).
- Acceptance pending: CUDA true; model on CUDA; preflight passes; full 3,597-frame run; `stage1_candidates.json` with `detection_geometry`; `stage1_report.txt`; `run_summary.json`.

Implemented (unchanged):
- `ball_tracker/stage1_candidate_gen.py` — `detection_geometry` sub-object on every candidate; null for Stage 0 reuse; schema backward-compatible.
- `ball_tracker/track_b_pack_gen.py` — geometry passed through to Track B manifest.
- `ball_tracker/tests/test_stage1c_geometry.py` — 4 fixture tests pass.

Observability hardening (active):
- Startup env banner: Python, PyTorch, CUDA runtime, `cuda.is_available()`, device, GPU name, model placement.
- Preflight: single dummy inference; confirms model on CUDA; logs elapsed ms; exits code 2 on CPU fallback.
- Progress every 100 frames: processed/total, %, elapsed, spf, ETA, raw, kept, pitch_rej, nms_warn.
- `PYTHONUNBUFFERED=1 python3 -u` in workflow.
- `run_summary.json` at end of every run.
- NMS warning count captured via Ultralytics logger handler (not Python `warnings` module).

GPU selection hardening (active, `360-stage1-candidates.yml`):
- Allowlist: RTX 3090, RTX 4090, A40, A100, L40/L40S.
- Blackwell rejection: strings `blackwell`, `b100`, `b200`, `gb200`, `rtx pro 4000 b`.
- Scoring preference order: A100/A40 > 4090/L40 > 3090.
- If no allowlisted offer found, workflow exits with error listing available GPU names.

Smoke test path (active):
- `smoke_test: true` input in `360-stage1-candidates.yml`.
- Overrides max_frames to 10; uses same real clip, hotspot map, stage0 detections, model.
- Uploads `stage1_output/` (inc. `run_summary.json`) as artifact `stage1-smoke-<run_id>`.
- Artifact will show GPU name, PyTorch + CUDA versions, CUDA available, model device, preflight elapsed, and at least one 100-frame progress line (or end-of-run summary for short runs).

**Next action: paste run `28046275937` artifact for review. Update state with GPU, PyTorch/CUDA versions, duration, spf, detection count, artifact ID.**


## Next gate

1. Obtain the quarantined Track B artifact.
2. Review candidate pack: this is now the residual non-static precision measurement.
3. Review zero-candidate pack: this now includes the 1,344 frames emptied by quarantine, so it is the first meaningful missed-ball/recall check.
4. Only then choose exactly one path: detector false-positive mitigation, targeted recovery for misses, bounded Stage 1 data-contract improvement (using geometry evidence), or Stage 2 work.

Do not tune Stage 2, smoke render, or modify the renderer before this review.

## Efficient AI work protocol

- Batch independent targeted reads. Avoid broad logs and unrelated files.
- Do not narrate routine tool calls.
- For a task result, report only **Changed**, **Verified**, **Dispatched**, and a genuine **Risk** if needed.
- Poll once shortly after dispatch for a quick failure, then wait for supplied result.

## Change log

- **2026-06-23:** Added living project state and `CLAUDE.md` operating contract.
- **2026-06-23:** Ruled out Stage 1 geometry/serialisation with micro re-detect; confirmed detector candidate-quality failure.
- **2026-06-23:** Built and reviewed original Track B; confirmed static-background contamination.
- **2026-06-23:** Built and verified reversible Stage 1b confirmed-static quarantine; quarantined Track B branch run dispatched.
- **2026-06-23:** Stage 1c — detection geometry preservation implemented and tested. `detection_geometry` sub-object in every Stage 1 candidate; null for Stage 0 reuse; carried through Track B manifest.
- **2026-06-23:** Stage 1c full run dispatched on `main` @ `fc988f05` (Actions run `28041924767`). STAGE 1C OUTPUT READY — AWAITING QUARANTINE + TRACK B once artifact verified.
- **2026-06-23:** Stage 1c run aborted — RTX PRO 4000 Blackwell GPU incompatible with PyTorch 2.1.0-cuda11.8. Stage 1c paused. GPU preflight + observability hardening applied to `stage1_candidate_gen.py` and `360-stage1-candidates.yml`.
- **2026-06-23:** GPU smoke-test path added. GPU allowlist (3090/4090/A40/A100/L40) + Blackwell rejection in offer selection. `smoke_test: true` input runs preflight + 10 frames on real data. NMS counter switched to Ultralytics logger handler. State: AWAITING COMPATIBLE GPU SMOKE TEST.
- **2026-06-23:** Smoke frame cap raised to 50 (guarantees fresh YOLO inference beyond Stage 0 reuse). Smoke test dispatched: run `28045327124` on `main` @ `b21675b`. DISPATCHED — UNVERIFIED.
- **2026-06-23:** Smoke test PASSED (RTX 4090, CUDA ok, fresh detections, geometry output verified). Full Stage 1c run dispatched: Actions run `28046275937` on `main`, `smoke_test=false`, full clip, standard Drive IDs. GPU: RTX 4090 allowlisted. DISPATCHED — UNVERIFIED. Pending: paste artifact → update GPU/PyTorch/CUDA/duration/spf/count → set STAGE 1C OUTPUT READY — AWAITING QUARANTINE + TRACK B.



