# OEV Phase 2: tracking clock (2026-10-16)

## What was wrong

`video-stitcher` `crates/reco-core/src/session/run_loop.rs`:

- Buffered lookahead loop (`run_buffered` / `produce_one`):
  `analysis_elapsed = if frame_stride > 1 { source_index / fps } else { wall_elapsed }`.
  At stride 1 (the OEV quality path) trackers and panner got **wall-clock
  processing time since session start**.
- Immediate loop (`run_immediate`): `process_frame_any(&frame, start.elapsed(), ..)`
  gave the trackers wall time at **any** stride.
- That value reaches `BallTracker::update(.., timestamp_ms)` and is also the
  recorded `world_state.timestamp_ms`. On the September GPU runs it advanced
  ~210-224 ms per frame against 16.68 ms of 59.94 fps video (~13x slow), so
  every millisecond threshold in `ball.rs` meant ~13x less video time than
  written, and varied with GPU speed.
- `process_frame_any`'s `elapsed` is used only for the detector/tracker/panner
  chain; in lookahead mode the render call has `skip_detection = true`, so the
  wall time passed at render never reaches the tracker. These were the only
  two tracker clock sites.

## Fix (`video-stitcher@fix/oev-media-clock-01`, draft PR #10, base `main`)

- `media_clock_rate(SourceInfo)`: offline sources report an exact
  `fps_rational`; live cameras (v4l2, libcamera, GStreamer camera) report
  `None`. Used as the offline/live switch.
- `media_elapsed(index, (num, den))`: `index * den / num` s in integer ns.
- Buffered and immediate loops use media time when the rate is known; live
  keeps wall time (and the existing stride>1 `index/fps` fallback).
- `FrameStart.timestamp_ms` stays wall time (profiling). `world_state`
  timestamps become media time.
- Not changed: stitching, detectors, ball selection, ROI, static-site logic,
  panner tuning. The panner also receives the corrected `timestamp_ms` via
  `PanContext`; its tuning was not touched.

PTS: `StereoFrame` carries no per-frame PTS and file sources decode
sequentially after a fixed start-up camera sync offset, so frame index x
rational fps is the recording timeline for constant-frame-rate files.
Variable-frame-rate input or mid-stream decoder drops are **not** handled and
would need PTS plumbed through every `FrameSource`; not observed on OEV
GoPro inputs, not verified either.

## Verification

Production code path (real `StitchSession::run`, CPU via Mesa lavapipe):

| Test | Before fix (main) | After |
|---|---|---|
| stride 1 lookahead -> tracker + world_state = media time | FAIL | pass |
| stride 3 lookahead -> analysis frames 0,3,6,9,12 at media time | pass | pass |
| immediate loop -> media time | FAIL | pass |
| tracker time identical at 0 ms vs 30 ms/detect processing | FAIL | pass |
| strictly monotonic, strides 1/2/4, both loops | pass | pass |
| frame_start stays wall clock | FAIL (media assert) | pass |
| live source (no fps_rational) keeps wall clock | pass | pass |

Plus 4 pure unit tests (exact at 60000/1001, no drift over 10 h, monotonic,
<1 us from the old stride>1 formula). `cargo test -p reco-core --lib
-- --include-ignored`: 177 pass; reco-autocam lib 69 pass; fmt clean; clippy
clean except main's pre-existing `collapsible_if` / `manual_is_multiple_of`.
The fix cherry-picks cleanly onto the experiment lineage (`fa3c79e`) and the
42 session tests pass there.

Replay (simulation, not the production loop):

- Historical replay unchanged: 0 mismatches / 10,775 frames on all four runs,
  same decision hashes as Phase 1.
- `cpu_replay --clock production-media` mirrors `media_elapsed` bit-for-bit;
  its decision traces equal the Phase 1 float `media` mode on all four runs.

**Gap:** the saved events cannot be pushed through the real run loop
end-to-end: they hold mapped panorama positions, and re-mapping needs the
run's calibration (`match.json`), which is not in the caches. The claim
"the fixed pipeline makes the replay's production-media decisions" rests on
(a) the run-loop tests proving the tracker receives `media_elapsed(index)`
and (b) the replay feeding the identical value to the identical tracker.
The backward bridge and panner are not replayed.

## Consequences (tracker stage, same definitions both clocks)

`scripts/oev_clock_compare.py`; durations in video seconds (frame / 59.94).

| Metric | Control 34202680125 hist -> media | v4 34228909395 | ROI 34236809938 | 8-a-side 34278293284 |
|---|---|---|---|---|
| dormant transitions | 4 -> 0 | 5 -> 0 | 34 -> 1 | 34 -> 1 |
| distinct dormant sites | 4 -> 0 | 5 -> 0 | 5 -> 1 | 5 -> 1 |
| first dormancy (video s) | 91.5 -> none | 91.5 -> none | 11.1 -> 75.6 | same as ROI |
| actual releases (at decoy) | 1 (0) -> 0 | 1 (0) -> 0 | 23 (9) -> 1 (1) | same |
| decoy proxy Tracking eps / total s / longest s | 9/0.3/0.1 -> same | same | 161/63.4/6.31 -> 177/66.1/5.96 | same |
| full losses / acquisitions | 68/69 -> 67/68 | 68/69 -> 67/68 | 20/21 -> 15/16 | same |
| None frames | 1639 -> 1621 | 1639 -> 1621 | 160 -> 117 | same |
| median Lost->reacquire (video s) | 0.26 -> 0.25 | same | 0.05 -> 0.03 | same |
| reacquisitions inside 750 ms grace (tracker clock) | 11 -> 56 | 11 -> 56 | 10 -> 15 | same |
| Tracking frames | 5260 -> 5320 | 5259 -> 5320 | 8418 -> 8614 | same |
| available % | 84.2 -> 84.3 | 84.2 -> 84.3 | 98.3 -> 98.8 | same |
| adjacent Tracking jumps > 0.1 rad | 50 -> 49 | 50 -> 49 | 215 -> 241 | same |

Reading:
- **Stationary detection/dormancy** almost stops. Qualified static episodes
  on ROI fall 155 -> 5 (diagnostic log, not committed). Main rejection on
  media time is `short` (283): accept runs at the decoy never span 1.5 s of
  video (median 9 points, max 80 = 1.33 s), because the run breaks on gaps
  or nearby accepts. On the historical clock 1.5 s was ~7 frames.
- **Spare-ball proxy** does not improve on the ROI lineage (66.1 s vs 63.4 s);
  unchanged on the control lineage (which rarely sees the decoy).
- **Loss/reacquisition**: slightly fewer full losses; post-loss grace now
  really spans 750 ms of video, so more reacquisitions fall inside it
  (11 -> 56 on control). Coasting/Lost are frame-counted and unaffected.
- **Consistency**: availability +0.1-0.5 pt; ROI large adjacent jumps
  215 -> 241. Control and v4 (and ROI and 8-a-side) give identical media-clock
  decisions; their historical differences were clock jitter.

Effective September thresholds in video time (x ~1/13.3): static episode 1.5 s
-> ~0.11 s; episode gap 750 ms -> ~56 ms; episode start separation 5 s ->
~0.38 s; dormancy span 20 s -> ~1.5 s; reacquire grace 750 ms -> ~56 ms;
pickup window 2 s -> ~150 ms; player-far 1 s -> ~75 ms.

Static-velocity check: decoy frame-to-frame jitter is 0.13 mrad (median) on
both clocks. Per-ms it is 6.5e-7 rad/ms historically vs 7.9e-6 rad/ms on media
time, against a limit of radius/1500 ms = 4.1e-6 to 1.2e-5 rad/ms. The check
is sample-rate dependent. A diagnostic that resampled it to >= 200 ms raised
ROI dormancy only 1 -> 3 and the decoy proxy got worse (78.1 s) through
releases. Velocity is a secondary gate, not the fix.

No ball-identity claim: the decoy figure is a location proxy.
