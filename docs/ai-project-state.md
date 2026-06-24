# FFA 360 Ball Tracker — AI Project State

**Last reconciled:** 25 June 2026 — Phase A code pushed and verified; loss-window detector fixed for real Stage 1 dict-keyed input shape; two runs dispatched, unverified.

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

### PHASE A — STATUS: AWAITING ARTIFACT REVIEW

**What has been pushed (verified):**
- `ball_tracker/render_segment.py` — local wide fallback FSM patch (5 hunks, commit `053db06e`)
  - `FALLBACK_FOV` raised to 130°
  - `last_trusted_yaw/pitch` recorded on every FOLLOW confirm
  - Zoom-out anchors to last trusted pose, not stale EMA
  - HUD updated to show `wide_target_*` coordinates
- `ball_tracker/loss_window_detector.py` — supports real Stage 1 dict-keyed frames shape + list-form unit tests (commit `8c3bd41f`)
- `ball_tracker/tests/test_loss_window_detector.py` — 6/6 pass locally (commit `a0ba207e`)
- `.github/workflows/360-loss-window-detector.yml` — dispatches detector on existing Stage 1 candidates (commit `2b0bd882`)

**Stage 1 real input shape confirmed:**
`frames` is a string-keyed dict `{"0": [...], "1": [...]}`, not a list. Detector normalises both shapes.

**Active dispatch:**
- Loss window detector: run `28136111246` — DISPATCHED — UNVERIFIED
- Render segment debug clip: run `28135812487` — DISPATCHED — UNVERIFIED (frames 800–1000, defaults)

**Acceptance criteria remaining:**
1. `loss_windows.json` artifact produced with plausible window counts
2. Debug clip shows camera holding last trusted yaw/pitch and widening locally on ball loss (not snapping to pitch-centre)
3. Visual approval from Johnson → Phase B unlocked

### PHASE B — BIDIRECTIONAL RESOLVER + VLM INTERFACE (after Phase A accepted)

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

- **2026-06-25 (session 2):** Phase A code pushed: `render_segment.py` local wide fallback (5-hunk patch), `loss_window_detector.py` with dict-keyed Stage 1 shape support (6/6 tests), `360-loss-window-detector.yml` workflow. Two runs dispatched unverified: loss-window `28136111246`, render-segment `28135812487`.
- **2026-06-25:** Architecture redesigned; pragmatic hybrid model adopted; Phase A+B scope locked; VLM-as-targeted-detector with backtracking cost model; CLAUDE.md updated.
- **2026-06-24:** FootAndBall benchmark rejected; backward-anchor propagation and football-YOLO adapter built (experiments only).
- **2026-06-24:** Candidate-fusion, pose selection, temporal ball-likeness score all rejected.
- **2026-06-24:** Geometry propagation verified (run `28107675223`).
