# Offline Ball-Recovery & Fusion Pipeline — Phase 3 Design Spec

**Status:** Approved in principle. Stage 0 to be implemented first.
**Scope:** Offline post-production ball-path recovery for 360° equirectangular grassroots football footage.
**Supersedes:** the abandoned "activity-biased live fallback" approach (v7), reverted to render `v6`.

---

## 1. Architecture principle

This is **offline post-production, not a live camera system.** We have the entire clip available, so we can use future frames, run multiple passes, and spend expensive compute *selectively*. The product goal is to **recover the most likely ball path using fused evidence across multiple passes before ever giving up** and falling back to a wide shot.

The pipeline is a **cost cascade**: cheap models see breadth, the expensive model runs only inside bounded uncertainty regions. A whole-clip static-hotspot map (Stage 0) deletes known-dead compute *before* any tracking effort is spent — it is both an accuracy feature and a compute pre-filter.

**Player activity is strictly a recovery/search prior.** It narrows search regions, models likely occlusion, and biases where we look harder for the ball. It **never directly sets camera yaw or pitch.** Rendering consumes only the fused ball path.

---

## 2. Pipeline overview

| Stage | Name | Frames touched | Model | Cost |
|---|---|---|---|---|
| 0 | Static false-positive sweep | sampled (~3–7k) | cheap, low-res | low |
| 1 | Cheap candidate generation | configurable breadth | cheap, low-res | moderate |
| 2 | Forward temporal tracking | all (CPU) | none | negligible |
| 3 | Bidirectional gap recovery | gap frames × bounded windows | **existing strong detector** | bounded |
| 4 | Fusion → global path | all (CPU) | none | negligible |
| 5 | Render decision | follow / fallback | existing v6 | existing |

The expensive model's workload scales with **how much the ball is actually lost**, not with clip length.

---

## 3. Stage 0 — Static false-positive sweep

**Purpose:** identify objects that recur at fixed spherical locations across widely separated moments (fence, net, trees, lights, line markings) and build a **penalty map** applied to every later stage. Runs first, before any expensive work.

### Inputs
- `equirect_clip` — the stitched equirectangular MP4 (full clip).
- `sweep_config`:
  - `sample_interval_s` — wall-clock spacing between sampled frames (default 0.5–1.0 s).
  - `detector_conf_floor` — deliberately **low** threshold so weak/recurring false positives are caught.
  - `sphere_bin_deg` — spherical histogram bin size in degrees (yaw × pitch), e.g. 2°.
  - `duty_cycle_threshold` — fraction of unique sampled timestamps above which a bin is treated as static (see §3.3).
  - `penalty_curve` params (see §3.4).
  - `pitch_bounds` — playable-area pitch min/max for geometric pre-rejection.

### Outputs
- `hotspot_map.json`:
  - `sphere_bin_deg`, grid dimensions, clip/venue identifiers.
  - Per-bin record: `(yaw_bin, pitch_bin, duty_cycle, penalty_weight)` where `penalty_weight ∈ [0,1]`, `1 = neutral`, `0 = effectively excluded`.
  - **Penalty map first, not blanket hard exclusion** (see §3.5). Hard exclusion only for bins that are both extreme-duty-cycle *and* outside any plausible playable area.
- `stage0_detections.json` — every candidate found during sampling, keyed by frame, **retained for reuse by Stage 1** so the same sampled frames are never detected twice (`(frame, yaw, pitch, score, bin_id)`).
- `sweep_manifest.json` — list of sampled frame indices + config used, so downstream stages know which frames already have detections.

### 3.3 Hotspot duty-cycle rule
- Duty cycle is computed over **unique sampled timestamps**, never raw detection count. We are identifying objects present across many *separate moments*, not objects detected many times in one burst.
- For each spherical bin: `duty_cycle = (number of unique sampled timestamps in which a candidate falls in this bin) / (total number of unique sampled timestamps)`.
- A real ball passes through a location transiently; a fence post is present in nearly every sample. High duty cycle ⇒ static object ⇒ penalised.
- Multiple detections in the same bin at the *same* timestamp count once.

### 3.4 Penalty curve
- `penalty_weight = f(duty_cycle)`, monotonically decreasing.
- Below a `low_duty_floor` (e.g. 0.10): weight = 1.0 (neutral — transient traffic, almost certainly real play).
- Between floor and `duty_cycle_threshold`: smooth roll-off (not a step), e.g. a soft sigmoid/cosine taper, so a moderately busy area is gently down-weighted, never zeroed.
- Above `duty_cycle_threshold`: strong penalty approaching but **not necessarily reaching** 0, unless §3.5 hard-exclusion conditions also hold.
- Exact curve parameters delivered in the Stage 0 module review (next step), tuned on this venue's sweep.

### 3.5 Avoiding suppression of genuine ball traffic around goals
- Goalmouths are legitimately busy: the ball genuinely recurs there. Three protections:
  1. **Penalty, not exclusion, by default** — a high-duty goal bin is down-weighted, never removed, so a real ball there can still win in fusion if detector + temporal + pitch evidence agree.
  2. **Duty cycle on unique timestamps** discriminates a static post (present nearly every sample) from ball traffic (present in a meaningful but far smaller fraction of separated moments). A fence is ~persistent; goal traffic is intermittent.
  3. **Playable-area awareness** — bins inside the known playable/goal region get a capped maximum penalty (a floor on `penalty_weight`) so play zones can never be fully suppressed. Hard exclusion is reserved for high-duty bins **outside** any plausible playable area (fence line above the crowd, lights, trees beyond the pitch).

---

## 4. Stage 1 — Cheap candidate generation (configurable breadth)

**Purpose:** generate ball candidates across the clip cheaply.

### Inputs
- `equirect_clip`, `hotspot_map.json`, `stage0_detections.json`, `sweep_manifest.json`.
- `coverage_config`:
  - `sampling_strategy` — **configurable**: `uniform` (fixed rate) now; `adaptive` later (denser around uncertain / high-motion periods, sparser in stable tracked spans). The interface accepts a per-frame rate function so we are not locked into paying maximum cost everywhere.
  - `base_rate` — default frames-per-second of detection for uniform mode.

### Processing
- For frames already covered by Stage 0, **reuse `stage0_detections`** — do not re-detect.
- For remaining frames per the sampling strategy, run the cheap low-res detector.
- Apply, nearly for free, before any refinement:
  - **Stage 0 penalty** — skip/down-weight candidates in penalised bins.
  - **Pitch/playable-area bounds** — reject geometrically impossible candidates.

### Outputs
- `candidates.json` — per-frame candidate lists: `(frame, yaw, pitch, raw_score, penalty_weight, in_bounds)`.

---

## 5. Stage 2 — Forward temporal tracking → anchors and gaps

**Purpose:** link candidates into tracklets; identify confident anchors and the loss gaps between them. Pure CPU.

### Inputs
- `candidates.json`, motion-model config (Kalman / constant-velocity), gating thresholds.

### Processing
- Associate temporally consistent candidates into tracklets using a motion model and gating.
- Score tracklets; high-confidence tracklets become **anchors**.
- Spans between anchors = **gaps** (loss regions to spend expensive budget on).

### Outputs
- `tracklets.json` — anchor tracklets with per-frame `(frame, yaw, pitch, temporal_score)`.
- `gaps.json` — list of `(gap_start_frame, gap_end_frame, pre_anchor, post_anchor)`.

### Confidence definitions (carried forward)
- `detector_score` — raw cheap-detector confidence.
- `temporal_score` — agreement with the motion model (residual to predicted position, velocity continuity).

---

## 6. Stage 3 — Bidirectional gap recovery

**Purpose:** recover the ball inside each gap using the **existing stronger football detector / higher-resolution crop** approach **only within bounded gap windows**. No new detector model is introduced in this phase.

### Inputs
- `equirect_clip`, `gaps.json`, `tracklets.json`, `hotspot_map.json`, `pitch_bounds`.
- `player_tracks` (activity) — **search prior only.**

### Processing — per gap only
- **Forward** predict from `pre_anchor`; **backward** predict from `post_anchor`. The two endpoints bound the interpolated path and shrink the per-frame spatial search window.
- Run the **existing strong detector on a higher-resolution crop**, restricted to the predicted window — lower threshold / extra scales permitted *because* the region is constrained.
- **Player activity enters here, strictly as a search prior:**
  - **occlusion modelling** — ball entering a player cluster then vanishing ⇒ search around/behind that cluster;
  - **closest cluster / active contest area** — convergence and motion of player tracks bias the search window;
  - permits more aggressive search *inside* the region only.
  - Activity never emits a yaw/pitch the camera follows; it only reshapes the search window.
- Apply Stage 0 penalty and pitch bounds to recovered candidates too.

### Outputs
- `recovered_candidates.json` — per-gap-frame `(frame, yaw, pitch, strong_score, search_region, activity_prior_applied)`.

---

## 7. Stage 4 — Fusion → global most-likely path

**Purpose:** combine all evidence into one trajectory via global optimisation. Pure CPU.

### Inputs
- `candidates.json`, `tracklets.json`, `recovered_candidates.json`, `hotspot_map.json`, `pitch_bounds`, `player_tracks`.

### Processing
- Build a per-frame candidate graph; solve a global shortest-path / Viterbi (or min-cost flow) for the maximum-likelihood trajectory.
- Per-frame node cost = negative-log combination of:
  - raw `detector_score` (cheap and/or strong),
  - **forward and backward** `temporal_score`,
  - pitch/playable-area prior,
  - player-proximity prior,
  - Stage 0 `penalty_weight`.
- Edge cost = motion-model transition plausibility.

### Outputs
- `fused_path.json` — per-frame `(frame, yaw, pitch, fused_confidence, evidence_breakdown)`.
- Per-frame `no_credible_ball` flag (see §8).

### `fused_confidence`
A normalised combination of the contributing scores at the chosen node, plus path-consistency. Single-model low confidence does **not** by itself produce low fused confidence if other evidence supports the candidate.

---

## 8. "No credible ball" definition

A frame (or span) is flagged `no_credible_ball` **only when the fused evidence fails across all of**:
- detector (cheap **and** strong-in-gap),
- temporal path (forward **and** backward),
- pitch/playable-area bounds,
- hotspot map (the only surviving candidate sits in a penalised static bin),
- player-assisted recovery search.

It must **not** mean merely one model having low confidence. Wide fallback is entered only on this fused failure.

---

## 9. Stage 5 — Render decision

- `fused_confidence ≥ follow_threshold` ⇒ FOLLOW the fused path.
- `no_credible_ball` span ⇒ wide fallback = **existing v6 safe behaviour, unchanged** (smooth zoom-out, fixed wide pose, no activity-driven movement).
- Player activity has already done its job upstream; it touches nothing in rendering.

---

## 10. Compute assumptions

- Clip: 60 min @ ~30 fps ≈ 108,000 frames.
- Stage 0: ~3,600–7,200 sampled frames, cheap low-res. Detections reused downstream.
- Stage 1: cheap detector at configurable rate; hotspot + pitch pre-filter cut candidate volume before refinement.
- Stages 2 & 4: CPU graph/maths, negligible model cost.
- Stage 3: the **only** expensive model usage, bounded to gap frames × small windows. Cost scales with ball-loss duration, not clip length.
- The hotspot map shaves cost across every stage by suppressing repeated effort on known false positives.

---

## 11. Data-flow summary

```
equirect_clip
  └─► Stage 0  ─► hotspot_map.json, stage0_detections.json, sweep_manifest.json
        └─► Stage 1 (reuses stage0_detections) ─► candidates.json
              └─► Stage 2 ─► tracklets.json, gaps.json
                    └─► Stage 3 (gaps only, +player_tracks prior) ─► recovered_candidates.json
                          └─► Stage 4 (fuse all) ─► fused_path.json (+ no_credible_ball flags)
                                └─► Stage 5 (existing v6 render) ─► follow | wide fallback
```

---

## 12. Phase 3 implementation order

1. **Stage 0** — static false-positive sweep. Standalone, independently testable, delivers compute saving immediately. **Next module.**
2. Stage 1 cheap candidates (reusing Stage 0 detections).
3. Stage 2 forward tracking + gap detection.
4. Stage 3 bounded bidirectional recovery with activity search prior.
5. Stage 4 fusion.
6. Stage 5 wiring to existing v6 renderer.
