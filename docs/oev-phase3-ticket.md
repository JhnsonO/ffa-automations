# OEV-003: static-site exclusion and reactivation on media time

**Status:** proposed, not implemented. CPU only. No GPU, merge or production change.

## Why
With the tracker on real video time (`fix/oev-media-clock-01`), the
September static-site design barely engages (ROI sample_02: 34 -> 1 dormant
transitions, decoy proxy 66 s). Its thresholds only worked because the clock
ran ~13x slow. Phase 1 also showed releases are permissive (unrelated ball
within 750 ms of last spare sighting; bystander pickup) and that a neighbouring
static cluster ~16 mrad from the site sits outside the 0.35-1 deg site radius.

## Base
`video-stitcher`: new branch `experiment/static-site-media-time-01` from
`experiment/ball-roi-relax-01` (`fa3c79e`) + cherry-pick of
`fix/oev-media-clock-01` (`628a1fe`, applies cleanly). Evaluate only on the
`production-media` replay clock.

## Scope (ball.rs static-site + reactivation only)
1. **Rate-invariant static episodes.** Qualification must not depend on sample
   rate: static test on displacement over a fixed time window (or resampled
   >= ~200 ms), not per-sample velocity; episode continuity tolerant of short
   coasts/gaps measured in video time; thresholds expressed in video seconds
   and chosen from replay evidence, documented with the rejection-reason tally
   (`short` / `spread` / `velocity` / `few`) before vs after.
2. **Neighbouring clusters.** Merge or extend a dormant site to cover adjacent
   same-camera static clusters (e.g. the 16 mrad neighbour) using measured
   detection jitter, without growing sites over open pitch.
3. **Associated release.** Outgoing motion must start inside/at the site
   boundary from a candidate associated to the site's last sighting (same
   camera, continuity from the site, progressively increasing distance), not
   any candidate outside 2 x radius. Pickup must involve the player that was
   at the site (same id) moving with the ball. Release enters a probation
   state that keeps episode evidence; a site that goes static again inside
   probation re-dormants immediately instead of relearning 3 episodes.
4. Instrumentation: keep the DIAG-style episode outcome and release reason
   logs (stable site id, frame, coordinates) at info/debug.

Out of scope: detector, ROI geometry, bridge, panner, player-anchor and
selection scoring, production main.

## Tests (must add)
- Flip the two Phase 1 "documents current behaviour" weakness fixtures to the
  new expected behaviour: unrelated ball after spare sighting -> no release;
  bystander + distant ball -> no release.
- Keep passing: spare goes dormant and is rejected; single dead ball not
  excluded; disappear/return within coast; full loss + far reacquisition;
  kicked-away ball releases; dribbled-away ball releases; nearest-wins;
  stale-evidence; cross-camera overlap; replay-rate invariance.
- New: adjacent static cluster within N mrad is covered; the same scenario
  sampled at 30 / 60 / 120 fps reaches the same dormancy decision; release
  probation re-dormants a re-static site; a genuine ball resting at the site
  briefly (< probation) then kicked is still released.
- All on media-time timestamps.

## Acceptance
- `cargo test -p reco-autocam` all pass (87 lib + replay tests + new
  fixtures); fmt clean on touched files; clippy clean apart from known
  pre-existing reco-core lints.
- CPU replay, `production-media` clock, same definitions as
  `scripts/oev_clock_compare.py`, all four Sep runs:
  - ROI lineage (34236809938, 34278293284): decoy proxy Tracking total
    reduced materially from 66.1 s (target <= 20 s; report actual), longest
    lock reduced from 5.96 s;
  - control lineage (34202680125, 34228909395): decoy proxy not worse than
    0.3 s total;
  - all runs: full losses not more than +20% vs media baseline (15 / 67),
    None frames not more than +20%, Tracking frames not more than -5%,
    adjacent jumps > 0.1 rad not worse than baseline +10%.
- Deterministic replay (3x identical hashes).
- Report states clearly that the decoy figure is a location proxy; any
  identity claim needs human labels for the 134-139 s window (recommended
  side task: label ball identity for that window and two other windows).
- Then, and only then, one GPU confirmation run would be requested from
  Johnson (not part of this ticket).

## Risks
- Single clip with a known decoy -> overfitting. Mitigate with the five
  sample set if caches exist; otherwise state the limit.
- Tighter release can block legitimate reintroduction (Phase 1 ablation:
  disabling release doubled full losses). Probation design must keep the
  kicked/dribbled fixtures passing.
