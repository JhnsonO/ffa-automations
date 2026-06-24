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

**COMPLETED — AWAITING REVIEW**

- `ball_tracker/stage2_repeated_static_audit.py`
- `ball_tracker/tests/test_stage2_repeated_static_audit.py`
- commit `1086552`

Real-data run against smoke artifact `7835756306` (531 tracklets). Static-motion audit run inline.

Summary: eligible=152/531 | clusters=18 | repeated-static=9
T0373 confirmed excluded (net_disp=42.64° ≥ 42.0° major-motion exclusion). ✓

Repeated-static clusters discovered:

  C001  yaw=24.49°, pitch=13.19°  members=57  windows=33  frames 277–3596  obs=780
        INSIDE Stage 0 hotspot (25°,13°) duty=0.250
        Contains all 7 handover borderlines: T0338, T0462, T0400, T0103, T0235, T0525, T0395
        Dominant false-positive; persists virtually entire clip.

  C002  yaw=-22.72°, pitch=-18.75°  members=47  windows=27  frames 4–3579  obs=364
        INSIDE Stage 0 hotspot (-23°,-19°) duty=0.158. Contains T0472.

  C003  yaw=133.54°, pitch=-18.47°  members=8  windows=8  frames 2249–3231  obs=146
        No hotspot overlap. Contains T0440.

  C004  yaw=-137.35°, pitch=-17.32°  members=6  windows=6  frames 351–2775  obs=46
        No hotspot overlap. Contains T0206.

  C005  yaw=-133.64°, pitch=-23.00°  members=6  windows=4  frames 213–2441  obs=34
        No hotspot overlap. radius=2.37°.

  C006  yaw=-55.54°, pitch=15.81°  members=6  windows=5  frames 1559–3450  obs=31
        No hotspot overlap.

  C007  yaw=136.16°, pitch=-12.91°  members=3  windows=3  frames 75–3028  obs=16
        No hotspot overlap. radius=2.48°.

  C008  yaw=-139.18°, pitch=-21.67°  members=3  windows=3  frames 1839–3547  obs=41
        No hotspot overlap. Contains T0231.

  C009  yaw=-173.78°, pitch=-21.55°  members=3  windows=3  frames 1251–2534  obs=21
        No hotspot overlap. Contains T0143.

C001 and C002 are inside known Stage 0 hotspot regions. C003–C009 are newly identified
false-positive locations outside the hotspot map — require visual verification.

No thresholds, tracklet statuses, or frozen files changed.

### Stage 2 discovered-static location match annotation

**COMPLETED — VERIFIED**

- script: `ball_tracker/stage2_discovered_static_match.py`
- tests: `ball_tracker/tests/test_stage2_discovered_static_match.py` (13 fixtures)
- workflow: `.github/workflows/360-stage2-discovered-static-match.yml`
- verified artifact: `7841215970`, run `28078249103`

Output: `tracklets_repeated_static_audit.json` (immutable derived copy; original `tracklets.json` confirmed untouched).

Verified counts: total=531 | eligible=152 | matched (would_suppress)=139 | unmatched=13.

Match radius derivation: p95 member dist + 0.5° guard, capped 6.0°. Discovery radius 4.0° never used as action radius. T0373 confirmed unmatched (major-motion exclusion). ✓

Per-cluster match radii and decision tier:

| Cluster | Radius | Matched | Tier |
|---------|--------|---------|------|
| C001 | 0.602° | 57 | tight — future suppression candidate |
| C002 | 0.596° | 47 | tight — future suppression candidate |
| C003 | 0.667° | 8  | tight — future suppression candidate |
| C004 | 0.733° | 6  | tight — future suppression candidate |
| C005 | 2.566° | 6  | wide — diagnosis pending |
| C006 | 1.109° | 6  | mid-range — diagnosis pending |
| C007 | 2.925° | 3  | wide — diagnosis pending |
| C008 | 0.595° | 3  | tight — future suppression candidate |
| C009 | 3.025° | 3  | wide — diagnosis pending |

No active suppression. Tight clusters (C001–C004, C008) are potential future candidates only.

## Active gate and next action

**STAGE 2 WIDE DISCOVERED-STATIC CLUSTER DIAGNOSIS — AWAITING REVIEW**

Wide clusters C005, C006, C007, C009 require subcluster analysis before any future action decision.

- script: `ball_tracker/stage2_wide_cluster_diagnosis.py`
- workflow: `.github/workflows/360-stage2-wide-cluster-diagnosis.yml`
- status: DISPATCHED — UNVERIFIED

Outputs: `wide_cluster_diagnosis.json`, `wide_cluster_diagnosis.txt`, `wide_cluster_subpack.png`.

Each cluster diagnosed with:
- member-level distance distribution from cluster centre
- natural subcluster detection at split_radius=1.2°
- pairwise separation matrix
- gap analysis in sorted distance sequence
- recommendation: KEEP AS ONE | SPLIT | ANNOTATION-ONLY

Decision after review: confirmed tight subclusters may become future suppression candidates; chained/spread clusters remain annotation-only.

**Parallel blocked workstream:** repair and re-dispatch the Stage 1c → Stage 1b → Track B self-contained workflow. Do not treat it as complete until its artifact is inspected.

No changes to: filtering, thresholds, tracklet status, Stage 1, Stage 1b, Stage 2 linking, renderer, or hotspot-map behaviour.

## Efficient AI work protocol

- Batch independent targeted reads; avoid broad logs and unrelated files.
- Do not narrate routine tool calls.
- Poll once shortly after dispatch for a quick failure, then wait for a supplied result.
- Return only **Changed**, **Verified**, **Dispatched**, and a genuine **Risk**.

## Compact change log

- **2026-06-24:** Wide cluster diagnosis dispatched for C005, C006, C007, C009 (`stage2_wide_cluster_diagnosis.py` + `360-stage2-wide-cluster-diagnosis.yml`). Gate: WIDE CLUSTER DIAGNOSIS AWAITING REVIEW.
- **2026-06-24:** Annotation layer verified (run `28078249103`, artifact `7841215970`). eligible=152, matched=139, T0373 unmatched. Tight clusters C001–C004, C008 flagged as future suppression candidates. C005/C007/C009 wide; C006 mid-range; all require diagnosis.
- **2026-06-24:** C003–C009 visually confirmed fixed-scene/camera-mount. Discovered-static match annotation layer built and dispatched.
- **2026-06-24:** Visual verification pack dispatched for C003–C009. Artifact `7841063584`, run `28077774459`. All clusters confirmed fixed scene.
- **2026-06-24:** Reconciled state file and working contract.
- **2026-06-24:** Stage 2 repeated-static location audit run against smoke data (artifact 7835756306). 9 repeated-static clusters confirmed.
- **2026-06-24:** Stage 2 repeated-static location audit built and tested; annotation-only.
- **2026-06-24:** Stage 2 static-motion audit built and reviewed; annotation-only.
- **2026-06-23:** Stage 1c geometry preservation verified on full RTX 4090 run `28046275937` / artifact `7830052466`.
- **2026-06-23:** Stage 1b confirmed-static quarantine verified on run `28035387017` / artifact `7824742847`.
- **2026-06-23:** Track B Stage 1c self-contained workflow failed before processing at artifact-download authentication; remains blocked pending a successful re-dispatch.
