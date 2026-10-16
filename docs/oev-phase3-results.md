# OEV-003 results: spare-ball static sites on media time (2026-10-16)

Code: `video-stitcher@experiment/static-site-media-time-01` (draft PR #11),
commits `e6fef3e` (tracker) and `37939eb` (tests), on
`experiment/static-site-media-time-base-01` = `fa3c79e` + media clock
`7fee2da` (patch-identical to `628a1fe`, PR #10) + replay tooling
(`f8cc068`, `5ead232`). Ticket: `docs/oev-phase3-ticket.md`.

All numbers are CPU replay, `production-media` clock, tracker stage
(backward bridge and panner not replayed). The decoy figure is a fixed
location proxy. **No human ball-identity annotations exist for these runs**
(only the harness's visual review windows), so nothing here proves the
correct ball was followed.

## What changed (tracker only)

The static-site gate in `BallTracker` moved to `trackers/static_sites.rs`.
Selection, scoring, coast, grace, player anchor, ROI, detector, panner and
stitching are unchanged; the gate still only rejects candidates inside a
dormant site.

| Problem (Phase 1/2) | Old logic | OEV-003 |
|---|---|---|
| Static detection depends on frame rate and on what the tracker accepted | episodes only from accepted detections; per-sample velocity test | static clusters learned from **all** ball candidates; per 1.5 s window, spread (3x MAD, 0.35-1 deg) and net drift, no velocity |
| Dormancy | >= 3 episodes over >= 20 s, one contradicted (player centroid far OR any other candidate) | same counts and span; contradiction = another ball **moving** elsewhere (a lone dead ball is never contradicted) |
| Neighbouring detections escape | site radius only | separate clusters; a static cluster within 2 deg of a dormant site needs one contradicted window |
| False releases (unrelated motion, bystander) | any candidate outside 2r for 750 ms; any player within 11 deg | departure must be a speed-plausible tracklet (178 deg/s) that was at the site at its last sighting and moved outward, or the same player in contact (0.05 rad) carrying it; then the site must stay empty 2 s, cancelled if the spare is seen again |
| Relearning after release | evidence wiped | 10 s probation keeps evidence; re-static -> dormant at once |

The confirmation delay costs nothing for genuine play: the exclusion zone
is at most 1 deg, so a kicked ball is tracked as soon as it leaves the spot.

### Approach iterations (honest record)
1. Candidate-based sites alone: decoy 66.1 -> 17.5 s but 30 releases
   (25 pickup). Cause: tracklet gate allowed 0.35 rad jumps regardless of
   time (real ball hijacked the spare's tracklet) and "player near site"
   used the 11 deg anchor radius.
2. Speed-scaled gate + 0.05 rad contact: 15.0 s, 16 releases. 15 of 16
   were false (site re-observed within ~1 s; spare detection dropouts reach
   2.1 s).
3. Confirmed release (2 s empty): 13.0 s, 5 releases.

## Four September runs (baseline = clock fix only)

Base clock: `production-media`; candidate clock: `phase3`. Tracker stage only.
Decoy metrics are a location proxy, not identity truth.

### Run 34202680125

| Metric | production-media | phase3 |
|---|---:|---:|
| dormant transitions | 0 | 0 |
| distinct dormant sites | 0 | 0 |
| first dormancy (video s) | None | None |
| actual releases | 0 | 0 |
| releases at decoy | 0 | 0 |
| decoy Tracking episodes | 9 | 9 |
| decoy Tracking total (s) | 0.3 | 0.3 |
| decoy longest lock (s) | 0.1 | 0.1 |
| decoy Tracking frames after first decoy dormancy | None | None |
| full losses | 67 | 67 |
| acquisitions | 68 | 68 |
| None frames | 1621 | 1621 |
| coast episodes | 671 | 671 |
| median Lost->reacquire (video s) | 0.25 | 0.25 |
| reacquisitions inside 750 ms grace (tracker clock) | 56 | 56 |
| Tracking frames | 5320 | 5320 |
| available (Tracking+Coasting) % | 84.3 | 84.3 |
| median Tracking segment (video s) | 0.03 | 0.03 |
| adjacent Tracking jumps > 0.1 rad | 49 | 49 |
| decision hash | edfeb09c7581c870 | edfeb09c7581c870 |

### Run 34228909395

| Metric | production-media | phase3 |
|---|---:|---:|
| dormant transitions | 0 | 0 |
| distinct dormant sites | 0 | 0 |
| first dormancy (video s) | None | None |
| actual releases | 0 | 0 |
| releases at decoy | 0 | 0 |
| decoy Tracking episodes | 9 | 9 |
| decoy Tracking total (s) | 0.3 | 0.3 |
| decoy longest lock (s) | 0.1 | 0.1 |
| decoy Tracking frames after first decoy dormancy | None | None |
| full losses | 67 | 67 |
| acquisitions | 68 | 68 |
| None frames | 1621 | 1621 |
| coast episodes | 671 | 671 |
| median Lost->reacquire (video s) | 0.25 | 0.25 |
| reacquisitions inside 750 ms grace (tracker clock) | 56 | 56 |
| Tracking frames | 5320 | 5320 |
| available (Tracking+Coasting) % | 84.3 | 84.3 |
| median Tracking segment (video s) | 0.03 | 0.03 |
| adjacent Tracking jumps > 0.1 rad | 49 | 49 |
| decision hash | edfeb09c7581c870 | edfeb09c7581c870 |

### Run 34236809938

| Metric | production-media | phase3 |
|---|---:|---:|
| dormant transitions | 1 | 8 |
| distinct dormant sites | 1 | 6 |
| first dormancy (video s) | 75.6 | 22.3 |
| actual releases | 1 | 5 |
| releases at decoy | 1 | 3 |
| decoy Tracking episodes | 177 | 81 |
| decoy Tracking total (s) | 66.1 | 13.0 |
| decoy longest lock (s) | 5.96 | 5.96 |
| decoy Tracking frames after first decoy dormancy | 1677 | 375 |
| full losses | 15 | 53 |
| acquisitions | 16 | 54 |
| None frames | 117 | 577 |
| coast episodes | 629 | 765 |
| median Lost->reacquire (video s) | 0.03 | 0.07 |
| reacquisitions inside 750 ms grace (tracker clock) | 15 | 51 |
| Tracking frames | 8614 | 6321 |
| available (Tracking+Coasting) % | 98.8 | 94.2 |
| median Tracking segment (video s) | 0.03 | 0.03 |
| adjacent Tracking jumps > 0.1 rad | 241 | 164 |
| decision hash | ff0bfd260e48d920 | 20a8e728ecb41d85 |

### Run 34278293284

| Metric | production-media | phase3 |
|---|---:|---:|
| dormant transitions | 1 | 8 |
| distinct dormant sites | 1 | 6 |
| first dormancy (video s) | 75.6 | 22.3 |
| actual releases | 1 | 5 |
| releases at decoy | 1 | 3 |
| decoy Tracking episodes | 177 | 81 |
| decoy Tracking total (s) | 66.1 | 13.0 |
| decoy longest lock (s) | 5.96 | 5.96 |
| decoy Tracking frames after first decoy dormancy | 1677 | 375 |
| full losses | 15 | 53 |
| acquisitions | 16 | 54 |
| None frames | 117 | 577 |
| coast episodes | 629 | 765 |
| median Lost->reacquire (video s) | 0.03 | 0.07 |
| reacquisitions inside 750 ms grace (tracker clock) | 15 | 51 |
| Tracking frames | 8614 | 6321 |
| available (Tracking+Coasting) % | 98.8 | 94.2 |
| median Tracking segment (video s) | 0.03 | 0.03 |
| adjacent Tracking jumps > 0.1 rad | 241 | 164 |
| decision hash | ff0bfd260e48d920 | 20a8e728ecb41d85 |


## Where the ROI-run tracking went

Zones: `decoy` within 0.035 rad of the spare; `static` within 1 deg of any
site OEV-003 made dormant ((0.218-0.226, 0.12) - the recurring false target
Sol flagged in September - and (1.085, -0.026)); otherwise `play`.

| base -> candidate | frames |
|---|---:|
| play -> play | 3003 |
| none -> none | 1961 |
| decoy -> none | 1652 |
| decoy -> play | 1463 |
| static -> none | 808 |
| decoy -> decoy | 712 |
| static -> play | 580 |
| none -> play | 198 |
| static -> static | 149 |
| decoy -> static | 139 |
| static -> decoy | 75 |
| play -> none | 33 |
| none -> static | 2 |

play-zone Tracking frames: base 3036, candidate 5244
real-play lost (play -> none): 33 frames

| window | base play/decoy/static/none % | candidate play/decoy/static/none % |
|---|---|---|
| early loss 6.0-15.0 s | 59/13/5/23 | 59/13/5/23 |
| 22s near-loss 20.0-24.0 s | 35/22/21/22 | 35/22/21/22 |
| 36-41s losses 34.0-43.0 s | 13/64/13/10 | 39/1/6/53 |
| 1:26 loss 84.0-90.0 s | 11/0/29/60 | 30/0/0/70 |
| 1:36 brief loss 94.0-99.0 s | 77/0/1/22 | 77/0/0/23 |
| 1:59-2:09 stationary goal ball 117.0-131.0 s | 50/0/34/16 | 72/0/0/28 |
| 2:14-2:19 long-pass recovery 132.0-141.0 s | 4/78/14/4 | 27/0/5/68 |


Reading: of 845 frames where the baseline tracked something and OEV-003
tracks nothing, 808 were the baseline sitting on another static spot; only
**33 frames (0.55 s, longest gap 0.13 s)** were in-play tracking lost.
1,463 frames moved from the spare to an in-play candidate and 198 frames
gained tracking where the baseline had none. The rise in full losses
(15 -> 53) and None frames (117 -> 577) is mostly frames where the only
candidate was a static object; the tracker now reports no ball instead of
the spare. The 134-139 s hard case goes from 78% spare / 4% play to 0% spare
/ 27% play / 68% no ball: the real ball is often undetected there, which the
bridge/panner and detector must handle, not this gate.

Residual decoy-proxy time 13.0 s: 6.8 s before the spare can be learned
(first decoy dormancy 22.5 s video; one 5.96 s lock at 13.8 s - the 20 s
evidence rule cannot exclude earlier) and 6.3 s after, mostly short
episodes around 164-179 s near late releases of the (0.962-0.964,
0.019-0.031) neighbour sites.

## Robustness

Sensitivity sweep (ROI decoy s / releases / real-play lost frames; control
always unchanged, hash edfeb09c):

| Variant | ROI |
|---|---|
| chosen | 13.0 / 5 / 33 |
| release confirm 1 s / 3 s | 13.1 / 5 / 33 ; 13.0 / 3 / 33 |
| contact 0.03 / 0.08 rad | 13.0 / 5 / 33 ; 13.0 / 5 / 33 |
| association 0.5 / 0.9 deg | 13.1 / 5 / 33 ; 13.1 / 5 / 33 |
| neighbour 1 deg / 3 deg | 22.2 / 5 / 29 ; 13.0 / 5 / 33 |
| probation 5 s / 20 s | 13.0 / 5 / 33 ; 13.0 / 5 / 33 |

Only the neighbour radius matters: the two neighbouring stationary clusters
sit 0.92 and 1.43 deg from the spare. Determinism: identical hash x3 and
under 100 us/frame throttling.

## Tests

- reco-autocam lib: 90 pass (static-site unit tests rewritten with the same
  intents + rate-invariant static fit, drift rejection, no release while
  observed).
- Replay parity: 4 pass. Exact agreement with the recorded (pre-OEV-003)
  decisions up to the first dormant transition on the real fixture; on
  full runs the first divergence is at/after the first new dormancy (ROI
  93/108, control 3851/3851): behaviour changes only through the gate.
- Causal fixtures: 17 pass. Flipped: unrelated ball and bystander no longer
  release. New: 30 s dead ball then kick never excluded; neighbour 16 mrad
  excluded, 5 deg away not; probation re-dormant; same dormancy video time
  at 30/60/120 fps.
- fmt clean on touched files; clippy clean except pre-existing reco-core
  `collapsible_if` / `manual_is_multiple_of`.

## Acceptance vs ticket

| Criterion | Result |
|---|---|
| ROI decoy proxy <= ~20 s | 13.0 s (from 66.1) - met |
| ROI longest lock reduced | 5.96 s unchanged - not met (lock occurs before the site is learnable) |
| Control not worse | identical decisions - met |
| Full losses <= +20%, None <= +20%, Tracking >= -5% | not met as written (53 vs 15, 577 vs 117, -27%); the ticket metric counts removing spare/static tracking as loss. Real-play loss: 33 frames. Proposed replacement metric: in-play tracking lost and gained, above |
| Adjacent jumps not worse +10% | 164 vs 241 - met |
| Deterministic | met |

## Unresolved

- No identity labels: label ball identity for the review windows (at least
  134-139 s, 36-41 s, 117-131 s) before any accuracy claim.
- Static object learned in the first ~22 s is unavoidable with the 20 s
  rule; a two-pass/offline occupancy prior (Sol fix #5) would remove it.
- Late releases near the neighbour sites (164-179 s) still leak ~6 s.
- (1.085, -0.026) became dormant: a static object on this clip, identity
  unverified.
- The bridge can still interpolate across a dormant site; panner and bridge
  were not replayed.
- Only one clip contains the spare; the five-sample set has no saved event
  caches here.
