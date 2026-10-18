# Aylestone held-out first pass (OEV-006), recorded 18 Oct 2026 before any tuning

- Detections: runs 39127297456 (s01, 600 s), 39127303237 (s02, 1320 s), 39127309211 (s03, 2040 s); events.jsonl from run artifacts.
- Engine: video-stitcher@experiment/oev-006-director-01 b4d07c8, `director_replay`, ball filter `none` (frozen first-pass params, code defaults per arms.json `frozen_params_first_pass`).
- Arms: A0 (reference) and C, recipes exactly as arms.json `replay_cmd`, venue ROI `venues/aylestone_roi.json` (approved).
- Annotation: NONE exists for Aylestone. Scored with an empty annotation, so every football metric and both hard gates are NOT MEASURABLE. Only comfort gates (vs A0) are scored.

| Window | C pan spikes/min (A0) | C zoom reversals/min (A0) | Comfort gates | C time in ball_tracked / ball_missing |
|---|---|---|---|---|
| s01 | 0.4 (0.4) | 15.6 (17.8) | pass | 95% / 5% |
| s02 | 0.4 (0.0) | 18.2 (16.0) | FAIL both | 95% / 5% |
| s03 | 0.4 (0.4) | 14.0 (15.6) | pass | 95% / 5% |

C never entered `uncertain` or `stoppage_restart` on any held-out window (same gap as dev s01).
After this record, Aylestone counts as development data.
