# OEV CPU replay (OEV-001)

Replays recorded OEV `events.jsonl` detections through the **real, unmodified**
`BallTracker` on CPU. No video decode, no YOLO, no GPU, no RunPod, no dispatch.
A full 180 s sample replays in ~0.03 s once built.

## What it is

- `video-stitcher@audit/cpu-ball-replay-01`
  - `crates/reco-autocam/examples/cpu_replay.rs`: the replay tool.
  - `crates/reco-autocam/tests/ball_replay_parity.rs`: real-data parity on a
    committed bounded fixture (run 34236809938 frames 0-1299).
  - `crates/reco-autocam/tests/ball_replay_regression.rs`: 16 causal fixtures
    documenting existing tracker behaviour (including known weaknesses).
- `ffa-automations@test/oev-cpu-replay-pack-01`
  - `oev/cpu_replay/provenance.json`: the four September caches (Drive ids,
    bytes, sha256, generating refs, event semantics).
  - `scripts/oev_cpu_replay.sh`: fetch + verify + build + replay all runs on
    both clocks + release ablation + comparisons.
  - `scripts/oev_replay_compare.py`: better/worse/unchanged between reports.

The tool feeds, per recorded frame and in dispatch order: recorded
`world_state.players` -> `observe_world`, then recorded `detections_raw` ->
`update`, into a tracker built exactly as field mode (`class 32`,
`max_jump 0.8`, `coast 20`). It reimplements no tracker rule. Dormant and
release transitions are read from the tracker's own `log` output.

## Running

```bash
# needs: Rust (toolchain pinned by video-stitcher), python3, curl
git clone -b audit/cpu-ball-replay-01 https://github.com/JhnsonO/video-stitcher
bash scripts/oev_cpu_replay.sh --stitcher ./video-stitcher --out ./oev-replay-out
# one run by hand:
cargo run --release -p reco-autocam --no-default-features --example cpu_replay -- \
  --events run34236809938.jsonl --clock historical --out r.json --trace r.csv
python3 scripts/oev_replay_compare.py base.json candidate.json
# tests:
cargo test -p reco-autocam
```

To test a tracker change: build `cpu_replay` on the change's branch, replay,
and compare against the `historical_*` reports. Parity (0 mismatches) is
expected only for unchanged tracker code on the historical clock.

## Input semantics (important)

- `detections_raw` is the **post-wrapper** mapped tracker input (ROI-filtered,
  panorama-projected), not a pre-ROI detector cache.
- `world_state.timestamp_ms` is the exact tracker input time. At stride 1 it is
  **wall-clock processing time** (`run_loop.rs`: `analysis_elapsed =
  wall_elapsed` when `frame_stride == 1`): ~210-224 ms per frame on these runs
  vs 16.68 ms of video. Stride > 1 uses source time.
- `world_state.ball` is post backward-bridge. Bridging only turns tracker
  `None`/`Coasting` into `Bridged`, so parity treats those as consistent.

## Results (2026-10-16, video-stitcher `8144231`, base `fa3c79e`)

Replay environment: 2-vCPU Linux x86_64 cloud sandbox, 7 GB RAM. Load 0.4-0.5 s, peak RSS ~18 MB,
replay 0.02-0.03 s per 10,775-frame run (target was <30 s on 4 cores).

### Input validation
All four caches: byte size matches, sha256 recorded, 10,775 indices (0-10774),
5 event kinds per index, frame order monotonic, tracker timestamps strictly
increasing. Tracker source is byte-identical at `0636bc8`, `c5d4022` and
`fa3c79e`, so one replay binary covers all four runs.

### Historical parity: EXACT on all four runs
0 mismatches / 10,775 frames from frame 0 on every run (state, yaw/pitch
within 1e-6 rad, confidence). Reproduces the September stitch-log figures:

| Run | Mapped ball candidates | Tracker Tracking | Dormant | Releases (decoy) | Decoy proxy, any recorded state |
|---|---:|---:|---:|---:|---|
| 34202680125 control | 8,074 | 5,260 | 4 | 1 (0) | 7 eps / 0.78 s |
| 34228909395 v4 | 8,074 | 5,259 | 5 | 1 (0) | 7 eps / 0.78 s |
| 34236809938 ROI | 23,712 | 8,418 | 34 | 23 (9: 7 outgoing, 2 pickup) | 80 eps / 69.2 s / longest 6.54 s |
| 34278293284 8-a-side | 23,712 | 8,418 | 34 | 23 (9) | identical to ROI |

The 9/Sep figures (23 releases, 9 at the decoy, 7+2 reasons; 80 eps / 69.3 s /
6.5 s) are reproduced. The 8 Sep control figure (15 eps / ~3.7 s) used a
different decoy window definition; with this report's fixed 0.03 rad proxy
the control run is 7 eps / 0.78 s. Not comparable 1:1.

Determinism: identical decision hash and identical reports across 3 runs and
under 200 us/frame throttling (~100x slower execution).

### Clock (historical vs media)
Media clock = `frame_index * 1001/60` ms (test comparison only).

- ROI run: dormant transitions 34 -> 1, releases 23 -> 1, yet decoy proxy
  Tracking 63.4 s -> 66.1 s. The static-site thresholds (1.5 s episode, 20 s
  span, 750 ms gaps) were being satisfied on a clock running ~13x slower than
  video; on video time the filter barely engages on this clip.
- Control vs v4 (and ROI vs 8-a-side) give **identical decision traces** on the
  media clock; their historical differences were wall-clock jitter only.
- Media-clock replay differs from the recorded decisions on 1,283 (ROI) / 72 (control) / 74 (v4) frames.

### Release ablation (`968e156` log-only `release_site`, historical clock, ROI inputs)
- Actual releases 23 -> 0; attempted calls 6,844 (fires repeatedly because the
  early return also skips clearing outgoing/pickup state, by construction).
- Decoy proxy Tracking 63.4 s -> 18.5 s (161 -> 120 eps).
- True decoy site (0.967, 0.006) first dormant at frame 1569 in both. Fresh
  Tracking within 1 deg after that: 3,175 -> 475. The 475 residual accepts are
  same-camera (Left), 6-18 mrad from the site centre, i.e. outside the dormant
  site radius (0.35-1 deg clamp): an adjacent static cluster, not a release.
- Verdict: **supports** the hypothesis that permissive reactivation is the
  major cause of the ROI decoy relock (on the historical clock), but it is
  **not the sole cause** (site-radius geometry). On the media clock the effect
  largely disappears (46.2 s vs 66.1 s) because sites rarely become dormant.
- Diagnostic only. Disabling release breaks legitimate reacquisition
  (full losses 20 -> 49, None frames 160 -> 538).

### Causal fixtures (documents existing behaviour)
Pass: stationary spare goes dormant and is rejected; single long dead-ball
episode not excluded; disappear/return within coast; full loss and far
reacquisition; kicked-away ball releases (outgoing motion); dribbled-away ball
releases (pickup fires first); competing detections (nearest wins); confidence
competition after full Lost (highest confidence wins); stale evidence (>750 ms
gaps) never forms episodes; cross-camera overlap stays continuous; replay-rate
invariance; clock-scale sensitivity.

Known weaknesses captured as passing "documents current behaviour" tests:
- an unrelated ball appearing within 750 ms of the last spare sighting,
  anywhere outside 2 * radius, releases the site (outgoing motion);
- a stationary bystander player near the spare plus the real ball moving near
  other players releases the site (pickup occlusion).

## Limits
- No human identity annotations: decoy metrics are a location proxy. No
  correct-ball accuracy is claimed.
- Media clock is a comparison only; production timing unchanged.
- Bridge parity is checked by category only; the bridge itself is not replayed.
- Horizontal OOF / containment proxy not recomputed here (panner not replayed).
- Audit-report sha256 values were not available to cross-check; sha256 are as
  computed on download, sizes match the OEV-001 table.
