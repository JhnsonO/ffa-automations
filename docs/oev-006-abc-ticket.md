# OEV-006: A/B/C director comparison on unseen footage

**Status:** P0 done (17 Oct 2026). P1 (GPU detection dumps) needs Johnson's approval. Nothing merged, no GPU used.
**Decision doc:** "OEV Build-vs-Rebuild Decision", sections 1 and 11 (Claude artifact).
**Supersedes:** further single-clip tuning on `sample_02`. OEV-005 detector fine-tune is preserved, not cancelled.

## Question

Is the current engine plus a better camera brain the fastest route to a reliable small-sided MVP? Which brain:

- **A:** ball-first (current best).
- **B:** player-first.
- **C:** hybrid four-state director.

All three are judged on footage they were not tuned on.

The test must not be designed to vindicate the existing work. If every arm fails, the failure breakdown says what to rebuild:

- detection
- player identity
- pitch ownership
- camera direction

## Arms and ablations (frozen in `oev/abc/arms.json`)

| Step | What | Measures | Status |
|---|---|---|---|
| A0 | Current best ball-first: clock fix + static sites + ROI fix + OEV-004 active ball, preset `v4_micro_damping` | baseline | runnable now |
| A1 | A0 + ownership-gated ball acceptance | pitch ownership | P2 |
| B0 | Same, `ball_weight = 0` (player-only framing, stateless players) | camerawork alone | runnable now |
| B1 | B0 + persistent player IDs (ByteTrack / OC-SORT port) | player IDs | P2 |
| B2 | B1 + sticky target-pitch ownership | pitch ownership | P2 |
| C | Hybrid director with four states (below) | state machine | P2 |

**C's states**, each with hysteresis and a minimum dwell time:

1. **ball_tracked.** Follow the ball and nearby owned players, with lead room.
2. **ball_missing** (lost for less than about 2 s). Extrapolate from the last trajectory, blended toward owned players moving the same way. This covers long passes.
3. **uncertain.** Widen to the owned-player hull and cap pan/zoom speed.
4. **stoppage_restart.** Hold a stable, slightly wide shot on the restart area.

Player-first is an experiment, not the expected answer. The likely winner is ball-first when detection is reliable, falling back to players otherwise.

## What P0 delivered

### `video-stitcher@experiment/oev-006-director-01` (`481ee83`, base `311c702`)

- **`reco_core::session::replay_dense_lookahead`.** CPU mirror of the stride-1 lookahead loop:
  - buffer schedule;
  - production backward bridge (`bridge_ball` split into `bridge_ball_world`, behaviour unchanged);
  - lookahead window;
  - centered smoothing;
  - `previous_position` interleaving.
- **Parity tests** `offline_director_matches_live_{6,90,1_and_short_clip}`: bit-exact against `StitchSession::run` on lavapipe (CPU Vulkan). A deliberately broken lookahead window fails the test.
  - reco-core lib: 180/180 including GPU tests.
  - No new clippy warnings (5 before, 5 after, all pre-existing).
- **`reco-autocam` example `director_replay`.** Runs a FieldPanner arm over recorded `events.jsonl` and writes a per-frame camera trace.
  - Inputs: preset + overlay merged as `reco stitch` does, plus `--ball-weight`, `--active-ball` and `--worlds recorded|tracker`.
  - **Verified on run 34236809938** (sample_02, 10,775 frames, `v4_micro_damping`, 1.5 s lookahead): replayed poses equal the recorded `pan_decision` on every frame for yaw, pitch and fov (pose hash `e46afd981ecd8989`). Camera arms can therefore be scored on CPU at no cost; GPU is needed only for detection dumps and the final blind-review renders.

### `ffa-automations@test/oev-006-abc-01`

- `oev/abc/arms.json`: frozen arms, thresholds, comfort parameters, footage roles and window rule.
- `oev/abc/annotation.schema.json`:
  - target and neighbour pitch polygons;
  - action points (1 Hz, 5 Hz in event windows);
  - events: long pass, counterattack, shot, goal, near-side ball, restart, neighbour ball present.
  - All coordinates are panorama yaw/pitch in radians; UNSURE is allowed everywhere.
- `scripts/oev_abc_score.py` and `tests/test_oev_abc_score.py` (5 pass). The scorer takes a trace plus an annotation and outputs metrics plus gate results.
  - Any metric without annotated cases is reported as **not measurable**, never as a pass.
  - Any failed hard gate fails the arm.

## Footage (verified 17 Oct via Drive metadata + streamed stills/audio; no full download)

| Role | Session | Files | Evidence |
|---|---|---|---|
| Development + regression | St Margaret's, smaller pitch, 9v9 | GX010197 + GX010173 | Durations 2952.95 / 2951.95 s, created 5 Aug, about 45 s apart. `sample_02` is from this pair. |
| **Held-out test** | Aylestone Rec, bigger pitch | GX010198 + GX010175 | See below. |

**Aylestone evidence:**
- **Pairing confirmed.** Durations are 2611.61 / 2612.61 s, created 6 Aug about 2 min apart. Each camera covers one half of the same pitch, and GX010198 shows its partner GoPro mounted on the same fence.
- **Sync offset: about 117 ms (about 7 frames).** The same sound appears roughly 0.117 s later in GX010198's file timeline than in GX010175's, at minute 10.
  - Basis: audio-envelope cross-correlation over 8.8 s, normalised peak 0.81.
  - Preliminary; `reco calibrate`'s own sync is authoritative.
- **Adjacent match present.** A separate game with pink/green bibs and its own goals is visible beyond our pitch in both cameras, so the neighbour-pitch hazard **is testable** in this session.
- **Not yet verified:**
  - exact player count: Johnson says about 10v10; stills at minute 10 are consistent with about 9–11 per side, but overlap and occlusion prevent an exact count;
  - stitch alignment quality: this needs `reco calibrate`, which runs in the P1 detection job.

**Held-out rules:**
- Windows are fixed by rule before any run.
- First-pass results are published before any tuning; after that Aylestone counts as development data.
- Aylestone is excluded from OEV-005 detector training.

**Limitations:** 2 sessions and 2 venues. This can rank the arms and reveal transfer failure, but it cannot accept the MVP. MVP acceptance needs 4–5 sessions, including:
- a second date at the same venue;
- a different lighting condition.

## Next phases (each needs approval where marked)

**P1 (GPU, approval needed).** For each of 6 windows (3 × 5 min per session at minutes 10/25/40), run one stitch with `--events` on engine `481ee83`.
- Settings: `--tracking field --panner-preset v4_micro_damping --lookahead 1.5 --frame-stride 1`.
- These are the frozen detections for every arm. Rendered output is kept as the A0 reference video.
- Estimate: about £2–£6 in total at the about £0.30 per 180 s sample-baseline rate (RunPod rates not re-verified).

**P2 (CPU, branch-only).** Build B1, B2, A1 and C as panners/trackers selectable in `director_replay`:
1. Validate the player-tracker port against Roboflow `trackers` (Python, offline) on the same detections.
2. Gate each with replay parity/determinism and unit tests.

**P3 (Johnson).** Annotate 6 windows: about 4–6 h with Opus pre-labels. This needs a yaw/pitch annotation helper (a fixed wide render with a known pixel→angle map), to be built at the start of P3.

**P4 (CPU, then GPU approval).**
1. Score every step on CPU.
2. Render A0 plus the best B and C for the 6 windows (about £5–£20).
3. Run a blind shuffled review.

**P5.** Decision gates as in the decision doc, section 11.

## Do not

- Merge to `main`.
- Dispatch GPU work without a stated cost and Johnson's "go".
- Tune on Aylestone before the first-pass record.
- Tune on `sample_02`.
- Stop or modify off-repo YOLO training (status unverified).
