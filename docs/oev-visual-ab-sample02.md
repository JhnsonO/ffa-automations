# OEV visual A/B: sample_02 BEFORE vs AFTER (2026-10-17)

## What was rendered
Real OEV broadcast output (two-camera stitch, trackers, field panner,
1.5 s lookahead smoothing, H.264 1920x1080), rendered on CPU in the Claude
sandbox with Mesa lavapipe (software Vulkan). Cost: GBP 0, no GPU, no
RunPod. ~7 fps, ~25 min per arm.

| | BEFORE | AFTER |
|---|---|---|
| video-stitcher ref | `experiment/static-site-media-time-base-01` @ `5ead232` (fa3c79e + media clock + replay tools) | `experiment/static-site-media-time-01` @ `37939eb` (+ OEV-003) |
| Harness | `experiment/visual-ab-render-01` @ `e1ef880` (`reco-cli/examples/replay_render.rs`), identical file in both builds | same |

Identical for both arms: sample_02 left/right 180 s sources (Drive, 3840x2160
HEVC 59.94 fps), calibration `match.json` (sha256 `dc6e1caa...`, CPU
`reco calibrate` + the St Margaret's field_roi from the September launcher),
detections (replayed per frame/camera from run 34236809938's recorded
post-ROI `detections_raw`, YOLO26m on GPU), panner (`v4_micro_damping` +
overlay = exactly the `v4_micro_damping_8aside_test` values), lookahead
1.5 s, stride 1, 1920x1080, H.264 balanced, blend 0.15. Tracker processes
from frame 0 (no mid-clip start).

## Fairness and verification
- Recorded frame index == decoded frame index: verified by cropping the
  source at a 0.91-confidence recorded ball (frame 3039): ball centred; 48
  frames later absent. The 48 extra source frames are at the end.
- Tracker inputs (detections + players + clock) are byte-identical in both
  render event logs (hash `4d5e617cbb18cf19`). The tracker is the only
  difference.
- Production render path == CPU replay: replaying each render's own inputs
  through cpu_replay (production-media) reproduces every render decision
  (0 mismatches / 10,775 frames, both arms), and the render's tracker clock
  equals the corrected media clock exactly (max diff 0.0 ms). This verifies
  the Phase 2 clock fix inside the real render loop.
- Limitation: the September pod's `match.json` no longer exists; local
  calibration maps detections up to 0.8 deg (median far less) from the
  recorded positions, so the renders' tracker decisions differ slightly
  from the Phase 3 replay numbers (same inputs for both arms, so the A/B is
  fair). The spare ball sits at (0.973, 0.007) in this calibration.

## Broadcast metrics (same definitions, both arms)
Location proxies only; no ball-identity labels exist.

| Window | Arm | ball on excluded/static spot % | no ball % | players in view % | camera speed p95 (deg/s) | yaw reversals |
|---|---|---:|---:|---:|---:|---:|
| full 0-179.7 s | BEFORE | 59.6 | 0.1 | 67.1 | 24.5 | 99 |
| full 0-179.7 s | AFTER | 13.4 | 0.7 | 67.4 | 25.5 | 99 |
| 13-21 s early spare | BEFORE | 81.4 | 0.0 | 56.7 | 17.3 | 1 |
| 13-21 s early spare | AFTER | 81.4 | 0.0 | 56.7 | 17.3 | 1 |
| 34-43 s wrong ball | BEFORE | 84.1 | 0.0 | 59.4 | 19.4 | 7 |
| 34-43 s wrong ball | AFTER | 13.1 | 1.3 | 68.6 | 36.2 | 5 |
| 117-131 s stationary ball | BEFORE | 43.1 | 0.0 | 78.6 | 21.4 | 15 |
| 117-131 s stationary ball | AFTER | 0.0 | 0.2 | 67.2 | 11.6 | 7 |
| 132-141 s long pass | BEFORE | 94.4 | 0.0 | 63.3 | 31.4 | 4 |
| 132-141 s long pass | AFTER | 11.1 | 1.5 | 64.9 | 32.6 | 3 |

static spots (union, local calibration): [(0.219, 0.116), (0.227, 0.115), (0.967, 0.033), (0.969, 0.025), (0.97, 0.021), (0.973, 0.007), (0.973, 0.009), (1.091, -0.022), (1.092, -0.022)]


## What the video shows
- 13-21 s: identical. Both follow the spare by the left goal/bench while
  play is at the centre circle; AFTER cannot know the spot yet (first
  exclusion needs 20 s of evidence).
- 34-43 s: BEFORE stays on the spare beside the left goal (camera pulled
  left). AFTER follows a ball in play at 35 s, but at 36-37 s swings right
  to a ball held near the far goal (very likely not the match ball;
  faster pan) and at 42 s is on a ball at the left goal mouth beside a
  player (both arms; may be genuine play).
- 117-131 s: BEFORE tracks static objects at the far fence/left goal for
  43% of the window; AFTER excludes them (0%) and follows a ball among
  the players (118 s) or bridges/coasts on the players (121, 125 s). AFTER
  frames fewer players at ~121 s (players-in-view 78.6% -> 67.2%).
- 132-141 s: BEFORE is on the left-goal spare 94% of the window and the
  camera hangs left; AFTER follows the ball in the air at 133 s, then
  coasts with the camera on the group (11% static).
- Whole clip: framing of the players and camera smoothness are almost
  unchanged (players in view 67.1% vs 67.4%, reversals 99 vs 99), because
  the field panner blends the player cluster (ball_weight 0.55).

## Deliverables (sha256)
```
dc6e1caa83e40d3fce420c2cd18c688c2b1f8ce58f08ffd96fed9ec52729b14d  match.json
ef9df15c66179ebb7f112b89c599167950687cf2b5bff40409b220d43a38327d  OEV_DIAGNOSTIC_W1_10-24s.mp4
b4c9fcb67bca9613e8822b8373c26f326be2031ba108fb52a7842505f0ce4071  OEV_DIAGNOSTIC_W2_31-46s.mp4
7a317ba0b8033d2021aaf92b12774fff70aa0d372b2be8818fd1415e17714bbd  OEV_DIAGNOSTIC_W3_114-144s.mp4
4e21c0a972345a81324300cdabd7c3dd488486cbcd0ec408dca5fb59b99cd1d1  OEV_W1_10-24s_early_spare_fixation.mp4
cb435ed68f43db5ca552a633660d1ef9ce29ec753190a23feaa0c8595c5c027e  OEV_W2_31-46s_incorrect_ball.mp4
5da7005eb193c8125e36e1f8ccf82156b6dcc8d93aefc3b5e69402ca019a307c  OEV_W3_114-144s_stationary_ball_and_long_pass.mp4
f9bef8083762fbb29ae6262006d7a370ec44fc01e704a900ee83597b6214b5ba  OEV_sample02_AFTER_full_1080p.mp4
14a44481855f49999fd4a8a12c26197b8b747cd2d2fbc92d109c858f092cdb08  OEV_sample02_BEFORE_full_1080p.mp4
dc8fb1858278069565bb90383ec88c4bb7e2ad7d61ddb58cd0a216344e795231  OEV_sample02_BEFORE_vs_AFTER_full.mp4
318a7f61700f42258653c16ca569382e61f1018d1aa890f00180607ffe42b42e  OEV_sample02_DIAGNOSTIC_BEFORE_vs_AFTER_full.mp4
```

## Reproduce
`scripts/oev_visual_ab/`: `run_arm.sh` (render one arm), `overlay.py`
(diagnostic overlay using reco's exact view/projection maths),
`compose.sh` (side-by-side + windows), `broadcast_metrics.py`.
