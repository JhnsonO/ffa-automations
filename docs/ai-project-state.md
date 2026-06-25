# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 25 June 2026 (session 10) — Phase 2 corrected render dispatched (run 28201650371, f837–927). Awaiting ChatGPT visual review of ZOOMING_IN blend at f867→f887.

## Start here

1. Read this file and `CLAUDE.md`.
2. Read only the files named by the active gate.
3. Preserve frozen boundaries.
4. A workflow is `DISPATCHED — UNVERIFIED` until its artifact is inspected.

## Product invariant

Offline 360° football post-production. The camera follows only a credible **fused ball path**.

- Ball evidence comes first; temporal evidence can support it but never prove it alone.
- Player/pose activity is a search and recovery prior only; it must never directly set camera yaw or pitch.
- Wide fallback is allowed only after fused ball evidence fails.
- Experiments stay separate from the renderer and production pipeline.

## AI model — pragmatic hybrid

- **ChatGPT** generates code files, schemas, architecture documents. No usage limits.
- **Claude** verifies against live repo, runs tests, pushes commits, dispatches workflows. Cannot be replaced for anything requiring repo access or execution.
- ChatGPT output is verified by Claude before any commit. Claude is the skeptic, not a relay.

## Architecture — locked 25 June 2026

### Target pipeline

```
equirectangular video
→ detector interface (yolo_backend | vlm_backend | yolo_finetuned_backend)
→ loss_windows.json (gap labeller, read-only)
→ bidirectional resolver (forward from last anchor + backward from re-acquisition anchor)
→ ai_review_queue.json (only unresolved / disagreeing windows)
→ vlm_reviewer (corridor-gated, targeted calls only)
→ ai_decisions.json
→ tracking_final.json
→ renderer (local wide fallback, not generic pitch-centre)
→ render_clean.mp4 + render_debug.mp4
```

### Detector interface — all backends emit this shared schema

```json
{
  "frame": int,
  "yaw": float,
  "pitch": float,
  "conf": float,
  "source": "yolo|vlm|yolo_finetuned",
  "crop_yaw": float,
  "detection_geometry": {
    "bbox_xyxy": [x1,y1,x2,y2],
    "bbox_area_px": float,
    "bbox_aspect_ratio": float
  }
}
```

### Backtracking cost model

VLM calls are corridor-gated by physics, not blanket per-frame:

1. Re-acquisition anchor must clear quality gate (conf, geometry, not in fence quarantine, stable 2–3 frames).
2. Backward trace walks from anchor; each step checks only 1–2 crops along the physically plausible displacement corridor.
3. Forward trace walks from last trusted anchor toward the gap.
4. Where forward + backward agree → resolve without VLM.
5. Where they disagree or a borderline YOLO candidate sits in both corridors → VLM adjudicates that frame only.
6. Estimated: 60–120 VLM calls per 30-min session ≈ £0.10–0.30.

### Anchor quality gate (required before backtracking)

- YOLO conf above borderline threshold
- bbox_area_px and bbox_aspect_ratio within ball-plausible range
- Not in Stage 1b fence quarantine zone
- Detection stable across 2–3 consecutive frames

### Renderer — local wide fallback (replaces generic pitch-centre)

```
credible follow (normal FOV ~90°)
→ hold last trusted yaw/pitch + widen FOV locally
→ keep searching / wait for re-acquisition
→ only fall to generic wide if no trusted position exists at all
```

### Merge gate — camera path NOT wired to resolver/VLM decisions yet

Resolver and VLM outputs write JSON only. Camera path reads `tracking_final.json` only after Johnson visually approves one full debug session.

## Frozen / do-not-break

- `ball_tracker/run_tracker.py` v11 — honest baseline, do not modify.
- Stage 1b quarantine, Stage 2 temporal linker, Tier A filter — frozen.
- Stage 2 remains separate from renderer.
- No threshold, suppression, or follow-cam activation without explicit approval.
- Detector interface backends are additive; do not alter existing Stage 1 candidate schema.

## Current data contracts

### Stage 1 candidates (existing, frozen)

Frame-indexed candidates include `yaw`, `pitch`, `raw_conf`, `penalty`, `weighted_conf`, `source`, `crop_yaw`, `region`, and `detection_geometry`.

Fresh Stage 1c detections carry bbox geometry; Stage 0 reuse carries explicit nulls. Stage 1 uses four yaw-only 110° crops at 1280×720.

### New artifacts (Phase A+B)

- `loss_windows.json` — labelled gap windows, read-only, no camera effect
- `bidirectional_repairs.json` — resolved gaps with forward/backward evidence
- `ai_review_queue.json` — unresolved windows for VLM
- `ai_review_packs/` — visual crops + minimap per window
- `ai_decisions.json` — VLM responses, confidence, reasoning
- `tracking_final.json` — merged final path (camera-safe after visual approval)
- `render_debug.mp4` — decision overlay showing why each frame was chosen

## Verified evidence

### Candidate-quality diagnosis

Generic detector candidate quality is the main limit. Generic detections attach to fence, mount, net, turf texture, and player/body clutter.

### Stage 1b static quarantine — VERIFIED

Run `28035387017`, artifact `7824742847`: 6,897 → 3,427 after quarantine; 1,344 zero-candidate frames.

### Tier A experimental output — REVIEWED, NOT PRODUCTION

520 → 182 tracklets; only 2 of 41 anchors human-verified as likely ball. Insufficient to approve follow-cam.

### Temporal ball-likeness score — FAILED

Known false positives ranked too highly. Do not tune or activate further.

### Geometry propagation — VERIFIED

Run `28107675223`, artifact `7853375656`: 158/158 observations carry detection_geometry; 91.77% populated.

### FootAndBall benchmark — REJECTED

Run `28114044649`, artifact `7856116823`. Detected players, not ball reliably. Do not use.

### Backward-anchor propagation — BUILT, NOT BENCHMARKED

`ball_tracker/experiments/backward_anchor_propagation.py` + unit tests. Detector-agnostic. Not wired to production.

### Modern football-YOLO adapter — BUILT, NO CHECKPOINT SELECTED

`ball_tracker/experiments/football_yolo_backward_adapter.py`. Experiment only.

## Active gate and next action

### PHASE A — STATUS: COMPLETE ✓

**What has been pushed (verified):**
- `ball_tracker/render_segment.py` — local wide fallback FSM patch (5 hunks, commit `053db06e`)
- `ball_tracker/loss_window_detector.py` — dict-keyed Stage 1 shape support, 6/6 tests (commit `8c3bd41f`)
- `ball_tracker/tests/test_loss_window_detector.py` — 6/6 pass (commit `a0ba207e`)
- `.github/workflows/360-loss-window-detector.yml` — loss window workflow (commit `2b0bd882`)
- `.github/workflows/360-render-segment.yml` — fallback_fov default raised 120→130 (commit `51d2741`)

**Verified runs:**
- Loss window detector: run `28136111246` — VERIFIED. 434 windows (430 short, 4 long ≥30f), all `bridgeable`.
- Render debug clip: run `28135812487`, artifact `7864826387` — REVIEWED (ChatGPT, 200 frames).
  - Local hold: PASS. Zoom smooth: PASS. No pitch-centre snap: PASS.
  - Defect: HUD FOV=120.0 (workflow default `'120'` overrode code constant). Fixed commit `51d2741`.

**Renderer — VISUALLY ACCEPTED (25 June 2026):**
- Render FOV verification: run `28152280742`, artifact `7870796514` — REVIEWED (ChatGPT, 200 frames).
  - FOV reaches 130.0°: PASS
  - Local hold on loss: PASS
  - No pitch-centre snap: PASS
  - Zoom-out, wide hold, reacquisition, FOLLOW handoff: PASS
  - Do not modify `render_segment.py` unless a regression is found.

**Loss window detector — VERIFIED locally (25 June 2026):**
- 6/6 unit tests pass (all shapes: bare list, list-under-frames, dict-under-frames).
- Detector run on Stage 1b quarantined candidates: 434 windows across 3,597 frames, all `bridgeable` (430 short, 4 long ≥30f: W0019 56f, W0023 32f, W0055 74f, W0057 32f).

**Phase A is COMPLETE. Phase B is unlocked.**

### PHASE B — STATUS: COMPLETE — VISUAL GATE IN PROGRESS

**All Phase B code verified and merged to main.**

**Drive file IDs (active):**
- Equirect video: `1z2p2FgLsjgvIIBw0HZXWEenckMLWpVNX`
- Stage 1b candidates: `19feQa2zx3YcqU4LIP6MNOG_q8vyi8TmJ`
- loss_windows.json: `1cWfx2lQx8GtvCobrlW-4X0bPXHUadvJ4`
- tracking.json (original): `1hpb0rUVnjebNwgJGACcHypqXtTGv5PUF`
- bidirectional_repairs.json: `1IEAR0GZ4d619BsANyoTmjcjeEhN8F0FK`
- tracking_repaired.json: `1xp5MSahcMyq7e-pGBxNqgYYhZ-8jWQaG`

**Resolver run 28167012616 — VERIFIED:** 572 repair_frames, 193 windows (anchor_interpolation). 237 no_corridor, 4 long_window.

**Replay run 28170181136 — VERIFIED:** 567 overrides applied, 5 skipped, 0 not found. `tracking_repaired.json` artifact `7878088056`.

**A/B render — Clip 1 (f100–250) — VISUALLY APPROVED (25 June 2026):**
- Phase B repairs at W0001 (f125–126) and f146–153: smooth follow, no jump, continuous reacquisition.
- Render B visually better than Render A. ✓

**A/B render — Clip 2 (f220–380) — PENDING JOHNSON REVIEW:**
- Covers W0015 (24f unresolved), W0016 (4f), W0019 (56f long fallback).
- Render A artifact: `7878810404` (run `28171828476`)
- Render B artifact: `7878830384` (run `28171849205`)

**Unresolved windows map:**
- W0002: f128–132 (5f) — no_corridor
- W0007: f162–175 (14f) — no_corridor
- W0015: f246–269 (24f) — no_corridor
- W0019: f288–343 (56f) — long_window, primary wide fallback test
- W0023: f359–390 (32f) — long_window
- W0055: f635–708 (74f) — long_window
- W0057: f717–748 (32f) — long_window

**Phase B — STATUS: PAUSED**
ChatGPT review found Phase B replay wrote repair frames as accepted detections/confirmations, causing renderer oscillation. Phase B must not modify best_score, tracker confirmation semantics, or FSM state through replay. Revisit later as non-confirming camera-target overlay only.

**Phase 2 — STATUS: DISPATCHED — UNVERIFIED (visual)**
- `--reacquire-blend-frames 20` added to `render_segment.py` (commit `c1599f58`) and workflow (commit `f5546057`).
- ~~Run `28187153168`, artifact `7885371513`, frames 600–750~~ — INVALID RANGE. Loss hadn't started at f750; reacquisition is at f856–887.
- **Tracking.json investigation (session 10):**
  - Loss window: f760–f855 (96 frames, `player_drift`)
  - First non-None best_score: f856 (bs=0.587)
  - 5 consecutive confirmed frames satisfied: f867 → ZOOMING_IN begins
  - FOLLOW resumes: ~f887 (f867 + 20 blend frames)
- **Corrected validation render: run `28201650371` — f837–927, original tracking.json, --reacquire-blend-frames 20 — DISPATCHED — UNVERIFIED**
- **Next: ChatGPT visual review of run `28201650371` artifact.**
  - Accept: ZOOMING_OUT/WIDE_HOLD visible before f867
  - Accept: ZOOMING_IN begins at f867, blends smoothly over ~20 frames
  - Accept: FOLLOW resumes ~f887 with no snap
  - Accept: FOLLOW stable through f927
- If approved → Phase 2 COMPLETE. Decide next: tune blend frames (30–40?), or open Phase 4 (pitch polygon, now prioritised over Phase 3).

**Next actions (in order):**
1. Johnson downloads artifact `7885371513` and passes to ChatGPT for visual review. — wide fallback on W0019, repair→unresolved boundaries.
2. If approved: Phase B visual gate COMPLETE.
3. Decide: wire `tracking_repaired.json` into full session render, OR proceed to VLM pack generation for unresolved windows.
4. Camera path wiring only after visual approval of full session render.

**Do not touch:** `run_tracker.py`, `render_segment.py`, Stage 1/1b, `bidirectional_resolver.py`, `replay_tracking_final.py`


### PHASE B — BIDIRECTIONAL RESOLVER + VLM INTERFACE (original scope)

**ChatGPT produces:**
1. `ball_tracker/bidirectional_resolver.py` — forward + backward corridor traces, writes `bidirectional_repairs.json`
2. `ball_tracker/detector_interface.py` — shared schema, `yolo_backend`, `vlm_backend` stub
3. `ball_tracker/vlm_reviewer.py` — reads queue, calls API (key from env), writes `ai_decisions.json`
4. `ball_tracker/pack_generator.py` — visual packs for unresolved windows
5. Tests for resolver and detector interface
6. Updated `tracking_final.json` merge logic (writes JSON only, no camera wiring)

**Claude verifies and pushes:** schemas consistent, frozen boundaries intact, VLM stub runnable without API key, unit tests pass.

**Acceptance:** bidirectional resolver closes short gaps on existing candidates without VLM; VLM queue contains only residual unresolved windows; camera path unchanged until visual approval.

## Immediate plan for Johnson

1. Receive Phase A ChatGPT output → paste files to Claude for verification and push.
2. Eyeball Phase A debug clip → approve local wide fallback.
3. Receive Phase B ChatGPT output → paste to Claude for verification and push.
4. Run VLM reviewer on one short test window (pennies) → inspect `ai_decisions.json`.
5. When satisfied → approve merge gate → wire `tracking_final.json` to renderer.
6. GoPro MAX2 when committed → record benchmark clip → fine-tune YOLO → add `yolo_finetuned_backend`.

## Compact change log

- **2026-06-25 (session 10):** Phase 2 validation render range corrected. Original render f600–750 missed reacquisition entirely (loss starts f760). Tracking.json inspected — loss f760–855 (player_drift), first bs f856, ZOOMING_IN at f867, FOLLOW ~f887. Corrected render dispatched: run 28201650371, f837–927, --reacquire-blend-frames 20 — DISPATCHED — UNVERIFIED. Phase 4 (pitch polygon) agreed as next sprint after Phase 2 approval, ahead of Phase 3.
- **2026-06-25 (session 9):** ChatGPT reviewed Render A f200–700 — Phase B rejected (replay wrote confirming detections, caused oscillation). Phase B paused. Phase 2 opened: --reacquire-blend-frames 20 added to render_segment.py (commit c1599f58) + workflow (commit f5546057). Validation render f600–750 dispatched — run 28187153168, artifact 7885371513 — superseded (wrong range).
- **2026-06-25 (session 8):** Replay run 28170181136 VERIFIED (567 overrides). A/B renders: Clip 1 f100–250 APPROVED (W0001 repairs smooth). Clip 2 f220–380 dispatched — superseded by Phase B rejection.
- **2026-06-25 (session 7):** replay_tracking_final.py pushed by ChatGPT (commit ced93d1), verified by Claude — syntax OK, tracker_state never mutated, best_score only overridden when null, detection appended not replaced, provenance preserved. No workflow yet. Next: build 360-replay-tracking-final.yml + A/B render (original vs repaired) for visual approval of repaired windows, unresolved windows, and transition boundaries.
- **2026-06-25 (session 6):** anchor-to-anchor linear interpolation added to resolve_window (commit a4046c1); resolver run 28167012616 VERIFIED: 572 repair_frames across 193 windows (source=anchor_interpolation). 237 windows still no_corridor (anchors disagree >1.25°), 4 long_window.
- **2026-06-25 (session 5):** stable_frames patched 2→1 in ResolverConfig (commit 7db131e); run 28162176817: 0 repairs — root cause was no gap-frame candidates (all 434 = no_corridor_supported_candidates).
- **2026-06-25 (session 4):** Phase A COMPLETE — renderer FOV=130 accepted, loss-window detector 6/6 tests + Stage 1b run verified locally. Phase B unlocked.
- **2026-06-25 (session 3):** Phase A visual review complete; fallback_fov workflow default fixed 120→130 (commit `51d2741`); FOV verification run `28152280742` dispatched.
- **2026-06-25 (session 2):** Phase A code pushed: `render_segment.py` local wide fallback (5-hunk patch), `loss_window_detector.py` with dict-keyed Stage 1 shape support (6/6 tests), `360-loss-window-detector.yml` workflow. Two runs dispatched unverified: loss-window `28136111246`, render-segment `28135812487`.
- **2026-06-25:** Architecture redesigned; pragmatic hybrid model adopted; Phase A+B scope locked; VLM-as-targeted-detector with backtracking cost model; CLAUDE.md updated.
- **2026-06-24:** FootAndBall benchmark rejected; backward-anchor propagation and football-YOLO adapter built (experiments only).
- **2026-06-24:** Candidate-fusion, pose selection, temporal ball-likeness score all rejected.
- **2026-06-24:** Geometry propagation verified (run `28107675223`).


