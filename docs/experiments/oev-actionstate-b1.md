# OEV ActionState B1 — ball-centred local action aim

Status: frozen for A/B run; no tuning permitted before verdict.

## Baseline

- Production Reco base: `JhnsonO/video-stitcher@c8b0d74b537d192c7de8d2856de64620a82830cf`.
- Detector/tracker/perception, ROI, model, resolution, stride, temporal recovery and FOV logic remain production-identical.
- A = production `FieldPanner`.
- B1 changes only the aim target while the current ball state is `TrackState::Tracking`.

## B1 hypothesis

During `TrackState::Tracking` only, replacing the production `FieldPanner` aim target with a fixed fusion of 50% tracked ball + 30% ball-centred local-player centroid + 20% 0.6 s future local-action signal will improve action framing in fast 8v8 transitions.

## Frozen mechanics

- Trusted ball means **only** `TrackState::Tracking`.
- `Bridged`, `Coasting`, `Lost`, or no ball: exact production aim behaviour.
- Local radius: **0.30 rad**, deliberately inherited from production `cluster_bandwidth_rad`; the production parameter itself is not modified.
- Local players: all current non-Lost player detections whose Euclidean panorama yaw/pitch distance from the Tracking ball is `<= 0.30 rad`.
- 0 local players: exact production aim behaviour.
- 1+ local players: ordinary unweighted geometric centroid; no confidence weighting.
- Future horizon: exactly **0.6 s**, sampled at the nearest available future world state.
- Future local group: apply the same Tracking-ball + 0.30 rad rule at `t + 0.6 s`.
- Future signal: future local centroid, expressed as current local centroid plus its 0.6 s displacement.
- If the required future state cannot produce a clean local group, future contribution is zero and current ball/local weights are renormalised from 50/30 to 62.5/37.5.
- Fixed full fusion when future is valid: `0.50 * ball + 0.30 * current_local + 0.20 * future_local`.
- Existing production FOV calculation is untouched and continues to use the production cluster/spread path.
- Existing motion pipeline, dead-zone, chase velocity and smoothing remain untouched.
- Production long-window lookahead state continues to update internally, but its aim offset is not applied on B1-active frames; B1's only applied future aim term is the fixed 0.6 s local-action signal.
- No detector, tracker, ReID, SoccerNet, optical-flow, VLM, resolution or model changes.
- No dynamic weights and no parameter tuning after viewing output.

## Evaluation

Primary comparison window: `129–149 s` on `sample_02`.
Known hard case: `134–139 s`.

Review:
- trusted-ball containment;
- maximum sustained trusted-ball out-of-frame duration;
- pan responsiveness / lag;
- unnecessary reversals;
- oscillation;
- wrong-phase or wrong/stationary-ball framing;
- FOV behaviour (must remain production-derived);
- final rendered visual quality.

## Kill criterion

Reject B1 if it does not materially improve 129–149 s / 134–139 s trusted-ball containment and pan responsiveness versus production, or if it introduces new reversals, oscillation, or wrong-phase framing.

If it clearly passes, it earns a broader test. If it fails, kill the concept before adding dynamic weights or more intelligence.
