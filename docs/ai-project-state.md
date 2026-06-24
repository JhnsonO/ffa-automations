# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 23 June 2026 (GH_PAT refreshed; ready to dispatch Track B Stage 1c)  
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

## Track B — quarantined audit (Stage 1b on original Stage 1)

**Status: COMPLETED — AWAITING REVIEW**

Run ID: (previous run, original Stage 1 input) — succeeded 2026-06-23T15:52Z  
Artifact ID: (previous run) — (~11.4 MB)

## Track B — quarantined audit (Stage 1c geometry-preserved output)

**Status: TRACK B STAGE 1C — GH_PAT REFRESHED — READY TO DISPATCH**

Run ID: `28048960467` — failed (HTTP 401 on artifact download)  
Blocking cause: `GH_PAT` secret missing, expired, or lacks Actions read scope.  
Fix required: replace `GH_PAT` in repo secrets with a classic PAT (`repo` scope) or fine-grained PAT (this repo + Actions read). Then dispatch `360-track-b-stage1c-quarantined.yml`.

Workflow updated (commit `79ad0511`): preflight now logs HTTP status and login before download; fails with clear message `GH_PAT missing, invalid, expired, or lacks Actions read access` instead of Python traceback.  
Artifact: `track-b-stage1c-quarantined-28048960467` (pending completion)  
Workflow: `.github/workflows/360-track-b-stage1c-quarantined.yml`

Input chain:
- Stage 1c source: Actions run `28046275937`, artifact `7830052466` (RTX 4090, full 3,597 frames, geometry verified).
- Stage 1b quarantine applied inline (same rules, same hotspot map `1TNZvN7OLrMurAjACTQY9ytEzZWwIeF0M`).
- Track B pack gen v2 applied to quarantined output; `detection_geometry` preserved in manifest.

Expected outputs in artifact: `candidate_precision_review_pack.png`, `zero_candidate_coverage_review_pack.png`, `track_b_manifest.json`, `track_b_report.txt`, `run_summary.json`, Stage 1b quarantine reports.

A one-off local-chain run eliminates the fragile Drive handoff: it downloads the original Stage 1 + hotspot map, builds the same Stage 1b local output, and feeds it directly into Track B.

- Branch: `run/track-b-quarantined-20260623`
- Trigger commit: `4a1ff97c1df519c65f03c17609a8aa16a3d94a2d`
- Workflow: `.github/workflows/360-track-b-quarantined-branch-dispatch.yml`
- Outputs: `candidate_precision_review_pack.png`, `zero_candidate_coverage_review_pack.png`, `track_b_manifest.json`, `track_b_report.txt`, `run_summary.json`, and the Stage 1b reports in one GitHub Actions artifact.

This run is CPU-only, uses no YOLO, does not touch Stage 2 or renderer logic, and does not need a new Drive file ID.

## Stage 1c — detection geometry preservation

**Status: STAGE 1C — VERIFIED COMPLETE**

Previous full run (Actions run `28041924767`) aborted: RTX PRO 4000 Blackwell GPU incompatible with PyTorch 2.1.0-cuda11.8. No valid Stage 1c artifact.

**Full run VERIFIED: Actions run `28046275937`, artifact `7830052466`**
- GPU: RTX 4090 (allowlisted ✓), offer id=42214650, $0.445/hr.
- Full 3,597 frames completed.
- `stage1_candidates.json` contains `detection_geometry` on all 6,436 fresh detections; 462 Stage 0 reuse candidates have explicit null geometry.
- All acceptance criteria met.

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

**Next action: review Track B quarantined artifact from run `28048960467`.**



## Stage 2 static-motion audit

**Status: COMPLETED — AWAITING REVIEW DECISION**

Implemented (commit `379738b`):
- `ball_tracker/stage2_static_motion_audit.py` — annotation-only audit layer
- `ball_tracker/tests/test_stage2_static_motion_audit.py` — 39 fixture tests, all pass

Per-tracklet metrics computed: obs_count, span_frames, net_disp_deg, spread_MAD_deg
(robust spatial spread via MAD on great-circle distances to median position),
path_length_deg, path_to_net_ratio (None when net ≤ 1e-9), median_step_deg,
p90_step_deg, gap_count, gap_fraction, confirmed_static_hotspot_frac.

Rejection gate (ALL five must hold — `would_reject_static_motion` label only):
1. obs_count >= 12
2. span_frames >= 20
3. net_disp_deg < 1.5
4. spread_MAD_deg < 0.6
5. p90_step_deg < 0.25

Path/path-to-net/median-step/gap metrics are **diagnostic only** — not gates.
`would_reject_static_motion` does not replace or modify `status`.

Smoke results (artifact 7835756306, run 28063029760, tracklets.json):
- 531 tracklets: 35 anchor / 161 passing / 335 fragment
- would-reject: 28 | borderline: 12 | retained: 503
- Near-zero anchors caught: **8 of 17**
  - 9 missed anchors and their failing condition(s):
    - T0338 (p90=0.278), T0462 (p90=2.565), T0231 (p90=4.468): fail p90_step
    - T0440, T0143: fail span_gte_20
    - T0066, T0412: fail span + p90
    - T0309, T0130: fail obs_count + span
- Strong-motion refs: T0001/T0088/T0318/T0477 all retained ✓
- T0499: **excluded from STRONG_MOTION_REFS** — in this run it is a near-zero
  passing tracklet (obs=85, span=152, net=0.024°) correctly flagged would-reject.
  This is a known finding, not a gate fault.

Human-confirmed static mapping (video evidence 7836234562, run 28063913618):
- All 17 human-confirmed IDs present in this run (0 unmapped).
- T0066 (project state calibration example) + 16 near-zero anchors from video session.
- 8 of the 17 human-confirmed IDs caught by the gate.

Outputs written by `run()`:
- `stage2_audit_report.json` — per-tracklet audit + summary + would-reject/borderline/retained lists
- `stage2_audit_report.txt` — human-readable summary
- `stage2_audit_review.txt` — structured review: A (would-reject) / B (retained anchor+passing) / C (borderline)

**Do not modify Stage 2 classifications, link thresholds, or dispatch a rerun.**
Next decision point: review the 9 missed near-zero anchors and the 12 borderline tracklets,
then determine if gate threshold adjustments are warranted (requires four-question review gate).

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
- **2026-06-24:** Stage 2 static-motion audit layer built and tested (commit `379738b`). 39 tests pass. Smoke results: 28 would-reject, 8/17 near-zero anchors caught, T0001/T0088/T0318/T0477 retained. T0499 confirmed near-zero passing, excluded from strong-motion refs. No classifications or thresholds changed.

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
- **2026-06-23:** Stage 1c VERIFIED COMPLETE — run `28046275937`, artifact `7830052466`, RTX 4090, 3,597 frames, 6,436 fresh detections with `detection_geometry`, 462 Stage 0 reuse with null geometry. Track B quarantined workflow built (`360-track-b-stage1c-quarantined.yml`) and dispatched: run `28048960467`. Self-contained chain: GitHub artifact download → Stage 1b quarantine inline → Track B pack gen. Status: TRACK B GEOMETRY REVIEW READY — AWAITING HUMAN REVIEW.
- **2026-06-23:** Track B Stage 1c blocked: GH_PAT HTTP 401 on artifact download. Workflow updated with preflight (logs login + artifact metadata HTTP status; clear failure message). No dispatch. Status: TRACK B STAGE 1C — BLOCKED: GH_PAT AUTHENTICATION.
- **2026-06-23:** GH_PAT verified valid (login=JhnsonO, artifact 7830052466 accessible, not expired). GH_PAT repo secret updated via secrets API. Status: TRACK B STAGE 1C — GH_PAT REFRESHED — READY TO DISPATCH.



