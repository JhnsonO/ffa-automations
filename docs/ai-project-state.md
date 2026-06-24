# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 24 June 2026 (label analysis tool added; awaiting adjudication CSV)  
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

### Stage 2 wide-cluster diagnosis — REVIEWED

**COMPLETED — REVIEWED**

Wide clusters C005, C006, C007, C009 have been diagnosed via subcluster analysis.

Terminology note: "Future suppression candidate" means reviewed Tier A evidence only. No runtime suppression is approved. Any eventual action layer requires a separate approval and must use conservative action radii, never discovery radius.

#### C005 — yaw=-133.64°, pitch=-23.00°

Sub1 — T0348, T0343, T0307: **REVIEWED FUTURE SUPPRESSION CANDIDATE**
Tight 3-member location; max spread 0.16°.

Sub2 — T0147, T0354: **ANNOTATION-ONLY**
Tight but only 2 members; insufficient recurrence evidence.

Sub3 — T0028: **REMOVED from reviewed discovered-location list.**
Retain raw audit evidence only. Not a suppression candidate.

#### C006 — yaw=-55.54°, pitch=15.81°

**REVIEWED FUTURE SUPPRESSION CANDIDATE**
One coherent 6-member location; max pairwise 1.007°.

#### C007 — yaw=136.16°, pitch=-12.91°

**REMOVED from reviewed discovered-location list.**
Single-linkage chain of unrelated detections. Not a suppression candidate. Retain raw audit evidence only.

#### C009 — yaw=-173.78°, pitch=-21.55°

T0143 (standalone): **STANDALONE STATIC CANDIDATE — ANNOTATION-ONLY**
Do not treat as a repeated-static location or suppression candidate.

T0379, T0279: **REMOVED from reviewed discovered-location list.**
Retain raw audit evidence only.

## Active gate and next action

**STAGE 2 TIER A EXPERIMENTAL ANCHOR HUMAN ADJUDICATION — AWAITING LABELS**

### Run 28087760893 — decision-gate outcome

Safety check applied (corrected comparator: `is_continuous = spatial OR linked`). 43 credible-motion windows checked.

**Reclassification applied (decision gate 24 June 2026):**

10 frame-only windows split into two categories:

**Tier-A-origin (expected collateral — NOT genuine-motion safety failures):**
These original tracklets are themselves inside a reviewed Tier A action radius. Their candidates were correctly removed. They are excluded from the genuine-motion safety denominator.
- T0236, T0292, T0306, T0316, T0335, T0338, T0409 (7 windows, `expected_removed_tier_a_origin_track`)

**Outside-Tier-A (genuine safety review required):**
These are outside all Tier A suppression radii. Frame continuity present but spatial/linked continuity absent. Evidence collection required before any verdict.
- T0275 passing net_disp=17.145° frames=2175-2190 nearest_frame_dist=2.064°
- T0334 passing net_disp=42.102° frames=2369-2378 nearest_frame_dist=21.311°
- T0394 passing net_disp=5.902°  frames=2650-2669 nearest_frame_dist=5.182°

**Genuine-motion denominator result:** 36 windows (43 − 7 Tier-A-origin). 33 spatial_or_linked_continuous. 3 outside-Tier-A unresolved.

### Comparator update (commit `2de4097`)

`ball_tracker/stage2_tier_a_dry_run_compare.py`:
- `TIER_A_ORIGIN_IDS` frozenset: T0236/T0292/T0306/T0316/T0335/T0338/T0409
- 4th outcome: `expected_removed_tier_a_origin_track`
- Genuine-motion denominator excludes Tier-A-origin windows
- Acceptance FAIL gated on genuine (non-Tier-A-origin) unsafe windows only

### T0275, T0334, T0394 safety review — CLEARED

Visual review completed. T0275, T0334, T0394 are false associations, not credible ball tracks.
Tier A dry-run safety gate cleared for this smoke clip.

### Stage 2 Tier A experimental output path — DISPATCHED — UNVERIFIED

- `ball_tracker/stage2_tier_a_experimental_output.py` (commit pending dispatch)
- `.github/workflows/360-stage2-tier-a-experimental.yml`
- Workflow: `360-stage2-tier-a-experimental` — DISPATCHED — UNVERIFIED (run TBD)

Inputs: Stage 1b-quarantined candidates (artifact 7841528502), hotspot_map.json, frozen Tier A manifest.
Outputs (all labelled experimental):
- `stage1_candidates_tier_a_experimental.json`
- `tracklets_tier_a_experimental.json`
- `gaps_tier_a_experimental.json`
- `tier_a_experimental_review_pack.png` (all anchors; top passing; representative fragments)
- `tier_a_experimental_summary.txt` (side-by-side original vs Tier A counts)
- `tier_a_experimental_counts.json`

Stage 2 linker (`stage2_temporal_link.py`) is called unchanged. No modifications to run_tracker.py,
renderer, YOLO thresholds, Stage 1b, or live production behaviour.

**Previous action completed:** `360-stage2-tier-a-experimental` dispatched and reviewed.
Tier A counts: 520 → 182 total, 41 → 26 anchors, 153 → 35 passing, 326 → 121 fragments.
Metric/table review insufficient for track-quality decision.

**Anchor visual evidence pack — DISPATCHED — UNVERIFIED**

- `ball_tracker/stage2_tier_a_anchor_review.py`
- `.github/workflows/360-stage2-tier-a-anchor-review.yml`
- Workflow: `360-stage2-tier-a-anchor-review` — DISPATCHED — UNVERIFIED

Inputs: `tracklets_tier_a_experimental.json` (latest tier-a-experimental-* artifact), source equirectangular video (Drive `1z2p2FgLsjgvIIBw0HZXWEenckMLWpVNX`).
Outputs:
- `tier_a_anchor_review.png` — one page per anchor (early/mid/late frames, overlays, verdict field)
- `tier_a_anchor_review_summary.txt`

Per-anchor page contains: 3 perspective crops (early/mid/late), candidate marker + crosshair,
overlays (tracklet ID, frame, yaw/pitch, conf, anchor_strength), metrics strip, human verdict field
(likely ball / likely false positive / unclear). Passing section is compact table only.

No changes to: filtering, radii, linking, thresholds, renderer, or runtime behaviour.

**High-resolution adjudication pack — DISPATCHED — UNVERIFIED**

- `ball_tracker/stage2_tier_a_adjudication_pack.py` (commit 0717e58)
- `.github/workflows/360-stage2-tier-a-adjudication.yml` (commit 30107a3)
- Workflow: `360-stage2-tier-a-adjudication` — DISPATCHED — UNVERIFIED

Inputs: `tracklets_tier_a_experimental.json` (latest tier-a-experimental-* artifact), source equirect video.
Outputs:
- `tier_a_anchor_adjudication.pdf` — paginated review pack, one page per anchor
- `tier_a_anchor_adjudication.png` — PNG contact sheet
- `tier_a_anchor_adjudication.csv` — one row per anchor; verdict column blank
- `tier_a_anchor_adjudication_manifest.json` — anchor to source frames mapping

Per-anchor page (3 rows: EARLY / MID / LATE):
  Context panel: full 110 FoV perspective crop at 960x540 (native 1280x720 downscaled).
  Zoom panel: 300px-radius window centred on candidate pixel, upscaled 2x to 600x600 (nearest-neighbour).
  Crosshair at candidate; orange bbox where detection_geometry.bbox_xyxy present.
  Overlaid: tracklet ID, frame, yaw/pitch, conf, anchor strength, bbox dimensions.
  Verdict field: [ ] likely ball  [ ] likely false positive  [ ] unclear
  Metrics: frame span, first/last frame, anchor_strength, sh, net_disp, mean_conf.

No automatic verdicts. No changes to filtering, radii, thresholds, linking, renderer, Stage 1, 1b, or 2.

**Next action:** Complete the verdict column (`likely ball` / `likely false positive` / `unclear`) in `tier_a_anchor_adjudication.csv`. Paste the filled CSV to trigger `stage2_label_analysis.py` label analysis.

Reviewed suppression candidates (Tier A evidence, no runtime suppression approved):
- C001, C002, C003, C004, C008 — tight, previously identified
- C005 Sub1 (T0348, T0343, T0307) — tight 3-member, max spread 0.16°
- C006 — coherent 6-member, max pairwise 1.007°

Annotation-only (not suppression candidates):
- C005 Sub2 (T0147, T0354) — insufficient recurrence evidence
- C009 / T0143 — standalone static, annotation-only

Removed from reviewed discovered-location list (raw audit evidence retained):
- C005 Sub3 (T0028)
- C007 — single-linkage chain
- C009 / T0379, T0279

Any action layer requires a separate approval. Action radii must be conservative and must never use discovery radius.

### Track B Stage 1c Quarantined Audit

**COMPLETED — REVIEWED**

- Run: `28079006609`, artifact: `7841528502`
- Stage 1b counts: 6,898 before; 3,428 active; 3,470 quarantined; 1,344 newly zero-candidate frames
- Track B output: 60 candidate tiles (50 top, 10 non-top); 15 zero-candidate rows
- Residual candidate precision is dominated by scene false positives: fence, mount, net, and pitch-side targets
- Zero-candidate pack is a meaningful recall sample but requires targeted per-frame labelling before drawing a missed-ball conclusion
- No suppression, no YOLO threshold tuning, no Stage 2 threshold tuning, no renderer change approved

No changes to: filtering, thresholds, tracklet status, Stage 1, Stage 1b, Stage 2 linking, renderer, or hotspot-map behaviour.

## Efficient AI work protocol

- Batch independent targeted reads; avoid broad logs and unrelated files.
- Do not narrate routine tool calls.
- Poll once shortly after dispatch for a quick failure, then wait for a supplied result.
- Return only **Changed**, **Verified**, **Dispatched**, and a genuine **Risk**.

## Compact change log

- **2026-06-24:** High-res Tier A adjudication pack built and dispatched: stage2_tier_a_adjudication_pack.py + 360-stage2-tier-a-adjudication.yml. Context 960x540 + zoom 600x600 centred on candidate, bbox overlay, csv + manifest. Gate: AWAITING LABELS.
- **2026-06-24:** Tier A anchor visual evidence pack built and dispatched: `stage2_tier_a_anchor_review.py` + `360-stage2-tier-a-anchor-review.yml`. One page per anchor (early/mid/late frames, overlays, verdict field). Gate: STAGE 2 TIER A EXPERIMENTAL ANCHOR VISUAL REVIEW — AWAITING TRACK-QUALITY DECISION.
- **2026-06-24:** T0275/T0334/T0394 reviewed as false associations (not credible ball tracks). Tier A safety review cleared. Stage 2 Tier A experimental output path built and dispatched: `stage2_tier_a_experimental_output.py` + `360-stage2-tier-a-experimental.yml`. Outputs: stage1_candidates_tier_a_experimental.json, tracklets_tier_a_experimental.json, gaps_tier_a_experimental.json, review pack + counts. Gate: STAGE 2 TIER A EXPERIMENTAL OUTPUT REVIEW — AWAITING TRACK-QUALITY DECISION.
- **2026-06-24:** Outside-Tier-A motion review dispatched for T0275/T0334/T0394. Comparator updated with TIER_A_ORIGIN_IDS reclassification and genuine-motion denominator (commit `2de4097`). Review script + workflow added (commits `260fd54`, `fb14bf8`). Safety review cleared (see above).
- **2026-06-24:** Comparator safety fix: `is_continuous` corrected to `spatial OR linked` only; frame-only is diagnostic; outcome categories added; FAIL verdict on unsafe windows. 5 fixture tests added and PASS. Commits `bfb0d07` (comparator) + `2b80e6c` (tests). Run 28087760893: 43 windows checked, 33 continuous, 7 Tier-A-origin expected, 3 outside-Tier-A unresolved.
- **2026-06-24:** Wide-cluster diagnosis reviewed. C005 split into Sub1 (suppression candidate), Sub2 (annotation-only), Sub3 (removed). C006 confirmed suppression candidate. C007 removed. C009/T0143 annotation-only standalone; T0379/T0279 removed. Gate: AWAITING ACTION-LAYER DESIGN DECISION.
- **2026-06-24:** Wide cluster diagnosis dispatched for C005, C006, C007, C009 (`stage2_wide_cluster_diagnosis.py` + `360-stage2-wide-cluster-diagnosis.yml`).
- **2026-06-24:** Annotation layer verified (run `28078249103`, artifact `7841215970`). eligible=152, matched=139, T0373 unmatched. Tight clusters C001–C004, C008 flagged as future suppression candidates. C005/C007/C009 wide; C006 mid-range; all require diagnosis.
- **2026-06-24:** C003–C009 visually confirmed fixed-scene/camera-mount. Discovered-static match annotation layer built and dispatched.
- **2026-06-24:** Visual verification pack dispatched for C003–C009. Artifact `7841063584`, run `28077774459`. All clusters confirmed fixed scene.
- **2026-06-24:** Reconciled state file and working contract.
- **2026-06-24:** Stage 2 repeated-static location audit run against smoke data (artifact 7835756306). 9 repeated-static clusters confirmed.
- **2026-06-24:** Stage 2 repeated-static location audit built and tested; annotation-only.
- **2026-06-24:** Stage 2 static-motion audit built and reviewed; annotation-only.
- **2026-06-23:** Stage 1c geometry preservation verified on full RTX 4090 run `28046275937` / artifact `7830052466`.
- **2026-06-23:** Stage 1b confirmed-static quarantine verified on run `28035387017` / artifact `7824742847`.
- **2026-06-24:** Stage 1 Tier A static-location dry-run INVALIDATED (run 28084047416). Defects: cluster-ID instability + tracklet-ID motion comparison. Fixed: frozen location manifest (LOC_001…LOC_C005_SUB1), frame/spatial continuity check. Workflow updated; repeated-static audit step removed. AWAITING RE-DISPATCH.
- **2026-06-24:** Track B Stage 1c quarantined audit COMPLETED — REVIEWED. Run 28079006609, artifact 7841528502. Residual precision dominated by scene false positives. Zero-candidate pack requires per-frame labelling. Gate: STAGE 1 RESIDUAL FALSE-POSITIVE MITIGATION — AWAITING TIER A DRY-RUN DECISION.
- **2026-06-23:** Track B Stage 1c self-contained workflow failed before processing at artifact-download authentication; blocked.
- **2026-06-24:** Active gate updated to STAGE 2 TIER A EXPERIMENTAL ANCHOR HUMAN ADJUDICATION — AWAITING LABELS. `stage2_label_analysis.py` standing by as next tool after CSV labels entered.
- **2026-06-24:** `stage2_label_analysis.py` added. Reads filled adjudication CSV + tracklets JSON; reports label summary, feature comparison table, ranked discriminating features, unclear anchor priority list. No filtering, thresholds, or frozen files changed. Gate: BALL-LIKENESS LABEL ANALYSIS — AWAITING FEATURE-DESIGN DECISION.
