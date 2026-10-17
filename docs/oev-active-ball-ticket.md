# OEV-004: active-ball selection (stop following knees, boots, hands, spares)

**Status:** proposed, not implemented. CPU only. No GPU, merge or production change.
**Suggested model:** Sonnet to write this; **Opus** to implement steps 1-2 (selection design).

## Why (evidence, all sample_02, 17 Oct 2026)
- Blind image audit (753 stills): the old tracker sat on one left-goal spare for
  62% (W2) / 35% (W3) of stills; Phase 3 removes that (154 -> 0, 139 -> 0) but
  wrong targets remain: fence feature, boots, upper bodies/hands, knee patch
  (93 false-object image-sides).
- Near-side ball dropped by the ball-ROI margin bug is fixed in
  `video-stitcher#12` (`119fc4f`); detections regenerated (run
  `39039272579`). With them, Phase 3 still puts the tracked ball inside a
  player's torso/legs zone for ~15.0 s of 180 s (21 stretches >= 0.3 s; old
  logic ~8.9 s). Longest: 119.2-121.9 s, 86.2-87.6 s. 86.9 s confirmed as a
  knee/shorts feature at ~65% of player height.
- Rule "reject anything inside a player box" alone is not enough: it would
  reject ~4% of audited real-ball frames (6/166) and catch only 3/25 false
  objects (most wrong targets are outside player boxes).

## Base
`video-stitcher`: new branch `experiment/active-ball-selection-01` from
`experiment/static-site-media-time-01` (`37939eb`) + cherry-pick `119fc4f`
(ROI margin fix). Replay on `production-media` clock against the regenerated
events (`oevdata/fixrun/events.jsonl`) and the four September caches.

## Scope (reco-autocam ball selection only)
1. **Body-attached rejection.** A candidate whose offset inside one player's
   box stays ~constant for >= ~0.3 s while that player moves, and which never
   separates from that player, is heavily down-weighted. Covers knee patches,
   hands, boots swinging with gait. A ball that is dribbled/kicked separates
   and leaves this state. Uses existing `world_state.players`.
2. **Size/position prior.** Expected ball size vs pitch position (fixed camera)
   from labelled real-ball frames; implausible size/height down-weighted.
3. **(Later, separate decision) crop verifier.** Small CPU classifier on each
   candidate crop: ball vs boot/hand/clothing/fence. Needs labelled crops
   (audit labels + new ones). Only if 1-2 leave material failures.
All three feed a soft score; no hard reject except near-certain body
attachment. Identity-of-ball labels remain the ground truth.

Out of scope: detector, ROI geometry (beyond #12), bridge, panner,
static-site exclusion logic (Phase 3), production main.

## Tests (must add)
- Knee-patch fixture: candidate rigidly attached to a walking player for 2 s,
  real ball elsewhere moving -> real ball chosen.
- Held ball / chest control: candidate stays in torso zone but real ball
  evidence exists (separation or motion) -> not rejected after separation.
- Dribble: ball near feet, separates, rolls -> never rejected.
- Boot swing: oscillating candidate at player's feet, real ball present ->
  real ball chosen; boot-only (no ball anywhere) -> still allowed to coast, no
  crash.
- Size prior: implausibly large/small candidate loses to a plausible one.
- Keep passing: all Phase 1-3 fixtures (spare dormancy, release, kicked,
  dribbled-away, replay-rate invariance, causal regression).
- Determinism: 3x identical replay hash.

## Acceptance (CPU replay, production-media clock)
- Suspect stretches (ball box inside player torso/legs zone, >= 0.3 s):
  Phase 3 15.0 s -> <= 5 s on the regenerated sample_02 events.
- Real-ball retention: >= 98% of audit `REAL_PLAY_BALL` frames still tracked
  (join on frame/version; old-events caveat noted).
- Tracking frames not more than -5% vs Phase 3 (6,837); full losses not more
  than +20% (48); decoy proxy not worse than 9.1 s.
- Ball-in-view metric (overlay.py projection) >= Phase 3's 91.3%.
- Report states identity is judged by human labels, not proxies.
- Then, and only then, one GPU confirmation run (~$0.30) with Johnson's
  approval; Phase 3 + #10 clock + #12 + this combined.

## Labelling side task (can go to ChatGPT, no tools needed)
Crop sheets of candidates labelled ball / boot / hand / knee-clothing /
fence-feature / spare, for the stretches in `PHASE3_suspects.csv` and the
audit's 93 false-object image-sides. Gives step 3 training data and a
held-out check for 1-2.

## Risks
- One clip, one pitch, same players: overfitting. State the limit; get more
  footage before trusting any learned verifier.
- Player boxes are noisy; a wrong box must not reject a real ball (soft score).
- Candidate identities are not logged (no IDs in events): attachment is
  inferred from positions. Consider logging candidate IDs (separate ticket).
- The clock fix (#10), ROI fix (#12) and Phase 3 (#11) are unmerged; this
  ticket stacks on them and inherits their review state. #12's CLA check is
  waiting on Johnson.
