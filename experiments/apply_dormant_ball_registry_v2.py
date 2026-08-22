#!/usr/bin/env python3
"""Apply test-only Dynamic Dormant-Ball Registry v2 to Reco.

This experiment keeps containment v2 untouched and hardens only the ball-state
identity/recovery path:
- all buffered tracker time is media time (source_frame_index / fps), never
  processing wall time;
- dormant stationary objects are learned only while a distinct credible active
  ball is continuously observed;
- once dormant, detector confidence cannot reactivate an object; actual movement
  (or a long absence safety expiry) is required;
- weak far reacquisitions after a full loss need spatially-consistent
  confirmation before they can become Tracking/bridge anchors;
- weak large jumps remain confidence-gated.

There is deliberately no challenger, forced_best, forced switch, teleport, or
fixed-duration pitch-coordinate blacklist. Production video-stitcher/main is
never modified; replacements fail closed against the validated c8b0d74 source.
"""
from pathlib import Path

BALL_PATH = Path("/tmp/video-stitcher/crates/reco-autocam/src/trackers/ball.rs")
RUN_LOOP_PATH = Path("/tmp/video-stitcher/crates/reco-core/src/session/run_loop.rs")

ball = BALL_PATH.read_text()
run_loop = RUN_LOOP_PATH.read_text()


def replace_once(text: str, label: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# 1) Correct the stride-1 timebase before it enters tracker state.
# ---------------------------------------------------------------------------
run_loop = replace_once(
    run_loop,
    "media-time",
    """            let decode_time = frame_t0.elapsed();
            let wall_elapsed = start.elapsed();
            let source_index = *produce_count;
            let frame_stride = session.frame_stride.max(1);
            let analysis_frame = source_index.is_multiple_of(frame_stride);
            let analysis_elapsed = if frame_stride > 1 {
                std::time::Duration::from_secs_f64(source_index as f64 / source.info().fps.max(1.0))
            } else {
                wall_elapsed
            };
""",
    """            let decode_time = frame_t0.elapsed();
            let _wall_elapsed = start.elapsed();
            let source_index = *produce_count;
            let frame_stride = session.frame_stride.max(1);
            let analysis_frame = source_index.is_multiple_of(frame_stride);
            // Tracker/recovery state must advance in media time, not machine
            // processing time. This is intentionally identical at stride 1
            // and sparse-analysis strides.
            let analysis_elapsed = std::time::Duration::from_secs_f64(
                source_index as f64 / source.info().fps.max(1.0),
            );
""",
)


# ---------------------------------------------------------------------------
# 2) BallTracker identity/recovery state.
# ---------------------------------------------------------------------------
ball = replace_once(
    ball,
    "constants",
    """pub const DEFAULT_PLAYER_ANCHOR_RAD: f32 = 0.20;

/// Singleton ball tracker emitting at most one
""",
    """pub const DEFAULT_PLAYER_ANCHOR_RAD: f32 = 0.20;

// Dynamic dormant-ball v2. Thresholds are deliberately conservative in media
// time: the goal/spare-ball failures persist for seconds, whereas normal ball
// pauses should not be classified dormant unless a distinct active ball is
// simultaneously credible elsewhere.
const LARGE_JUMP_RAD: f32 = 0.20;
const VERY_LARGE_JUMP_RAD: f32 = 0.30;
const LARGE_JUMP_MIN_CONFIDENCE: f32 = 0.18;
const VERY_LARGE_JUMP_MIN_CONFIDENCE: f32 = 0.30;

const RECENT_LOSS_MEMORY_MS: f64 = 3_000.0;
const FAR_REACQUIRE_RAD: f32 = 0.15;
const FAR_REACQUIRE_STRONG_CONFIDENCE: f32 = 0.35;
const FAR_REACQUIRE_CONFIRM_OBSERVATIONS: u32 = 3;
const REACQUIRE_MATCH_RAD: f32 = 0.10;

const ACTIVE_BALL_CONFIDENCE: f32 = 0.30;
const ACTIVE_BALL_MATCH_RAD: f32 = 0.18;
const DORMANT_STATIONARY_RADIUS_RAD: f32 = 0.040;
const DORMANT_MATCH_RAD: f32 = 0.10;
const DORMANT_FILTER_RADIUS_RAD: f32 = 0.055;
const DORMANT_MIN_ACTIVE_SEPARATION_RAD: f32 = 0.18;
// sample_02's real goal/spare detections remain almost fixed for multi-second
// windows. 1.5s media-time + 8 qualifying observations catches those before
// the known ~1:06/~1:59 failures without the old wall-clock over-learning.
const DORMANT_DWELL_MS: f64 = 1_500.0;
const DORMANT_MIN_QUALIFYING_OBSERVATIONS: u32 = 8;
// Unconfirmed candidate tracklets are disposable. Confirmed dormant objects
// persist long enough to survive camera absence between the ~1:06 and ~1:59
// goal-ball incidents; movement remains the normal release path.
const DORMANT_PENDING_ABSENCE_RELEASE_MS: f64 = 5_000.0;
const DORMANT_CONFIRMED_ABSENCE_RELEASE_MS: f64 = 90_000.0;

/// Singleton ball tracker emitting at most one
""",
)

ball = replace_once(
    ball,
    "tracker-fields",
    """    coaster: Coaster,
    last: Option<LastKnown>,
    max_jump_rad: f32,
""",
    """    coaster: Coaster,
    last: Option<LastKnown>,
    /// Last genuine accepted location retained briefly after full loss so a
    /// weak far candidate cannot instantly become a trusted bridge anchor.
    recent_lost: Option<LostMemory>,
    pending_reacquire: Option<PendingCandidate>,
    /// Passive stationary-object memory. It only removes known dormant
    /// candidates; it can never select or force another ball.
    dormant_candidates: Vec<DormantCandidate>,
    max_jump_rad: f32,
""",
)

ball = replace_once(
    ball,
    "state-structs",
    """struct LastKnown {
    yaw: f32,
    pitch: f32,
    origin: CameraId,
}

impl BallTracker {
""",
    """struct LastKnown {
    yaw: f32,
    pitch: f32,
    origin: CameraId,
}

#[derive(Debug, Clone, Copy)]
struct LostMemory {
    yaw: f32,
    pitch: f32,
    timestamp_ms: f64,
}

#[derive(Debug, Clone, Copy)]
struct PendingCandidate {
    yaw: f32,
    pitch: f32,
    observations: u32,
}

#[derive(Debug, Clone, Copy)]
struct DormantCandidate {
    anchor_yaw: f32,
    anchor_pitch: f32,
    first_seen_ms: f64,
    last_seen_ms: f64,
    qualifying_since_ms: Option<f64>,
    qualifying_observations: u32,
    dormant: bool,
}

impl BallTracker {
""",
)

ball = replace_once(
    ball,
    "init-fields",
    """            coaster: Coaster::new(DEFAULT_COAST_FRAMES),
            last: None,
            max_jump_rad: DEFAULT_MAX_JUMP_RAD,
""",
    """            coaster: Coaster::new(DEFAULT_COAST_FRAMES),
            last: None,
            recent_lost: None,
            pending_reacquire: None,
            dormant_candidates: Vec::new(),
            max_jump_rad: DEFAULT_MAX_JUMP_RAD,
""",
)

ball = replace_once(
    ball,
    "confidence-aware-jump-gate",
    """                if dist > self.max_jump_rad {
                    None
                } else {
                    // Balance proximity and confidence; the 0.1-rad
                    // weight on confidence picks the sharper detection
                    // when two candidates are within a pixel or two.
                    Some(dist - 0.1 * det.confidence)
                }
""",
    """                if dist > self.max_jump_rad
                    || (dist > LARGE_JUMP_RAD && det.confidence < LARGE_JUMP_MIN_CONFIDENCE)
                    || (dist > VERY_LARGE_JUMP_RAD
                        && det.confidence < VERY_LARGE_JUMP_MIN_CONFIDENCE)
                {
                    log::trace!(
                        "BallTracker: drop implausible jump — dist={:.3}rad conf={:.2}",
                        dist,
                        det.confidence
                    );
                    None
                } else {
                    // Balance proximity and confidence; the 0.1-rad
                    // weight on confidence picks the sharper detection
                    // when two candidates are within a pixel or two.
                    Some(dist - 0.1 * det.confidence)
                }
""",
)

ball = replace_once(
    ball,
    "helpers",
    """    /// Decide whether this detection survives the player-anchor gate.
    fn passes_player_anchor(&self, pos_yaw: f32, pos_pitch: f32) -> bool {
        if self.current_players.is_empty() {
            return true;
        }
        self.current_players.iter().any(|(py, pp)| {
            let dy = pos_yaw - *py;
            let dp = pos_pitch - *pp;
            (dy * dy + dp * dp).sqrt() <= self.player_anchor_max_rad
        })
    }
}
""",
    """    /// Decide whether this detection survives the player-anchor gate.
    fn passes_player_anchor(&self, pos_yaw: f32, pos_pitch: f32) -> bool {
        if self.current_players.is_empty() {
            return true;
        }
        self.current_players.iter().any(|(py, pp)| {
            let dy = pos_yaw - *py;
            let dp = pos_pitch - *pp;
            (dy * dy + dp * dp).sqrt() <= self.player_anchor_max_rad
        })
    }

    fn active_confident_observation(
        &self,
        detections: &[MappedDetection],
    ) -> Option<(f32, f32)> {
        let last = self.last?;
        detections
            .iter()
            .filter(|d| d.class_id == self.class_id && d.confidence >= ACTIVE_BALL_CONFIDENCE)
            .filter_map(|d| {
                let pos = d.position?;
                if !pos.yaw.is_finite()
                    || !pos.pitch.is_finite()
                    || !self.passes_player_anchor(pos.yaw, pos.pitch)
                    || self.is_dormant_candidate(pos.yaw, pos.pitch)
                {
                    return None;
                }
                let dy = pos.yaw - last.yaw;
                let dp = pos.pitch - last.pitch;
                let dist = (dy * dy + dp * dp).sqrt();
                (dist <= ACTIVE_BALL_MATCH_RAD)
                    .then_some((d.confidence, pos.yaw, pos.pitch))
            })
            .max_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal))
            .map(|(_, yaw, pitch)| (yaw, pitch))
    }

    fn update_dormant_registry(
        &mut self,
        detections: &[MappedDetection],
        timestamp_ms: f64,
        active_ball: Option<(f32, f32)>,
    ) {
        let observations: Vec<(f32, f32)> = detections
            .iter()
            .filter(|d| d.class_id == self.class_id)
            .filter_map(|d| {
                let pos = d.position?;
                if !pos.yaw.is_finite()
                    || !pos.pitch.is_finite()
                    || !self.passes_player_anchor(pos.yaw, pos.pitch)
                {
                    return None;
                }
                Some((pos.yaw, pos.pitch))
            })
            .collect();

        for (yaw, pitch) in observations {
            let matched = self
                .dormant_candidates
                .iter()
                .enumerate()
                .filter_map(|(i, entry)| {
                    let dy = yaw - entry.anchor_yaw;
                    let dp = pitch - entry.anchor_pitch;
                    let dist = (dy * dy + dp * dp).sqrt();
                    (dist <= DORMANT_MATCH_RAD).then_some((i, dist))
                })
                .min_by(|a, b| a.1.partial_cmp(&b.1).unwrap_or(std::cmp::Ordering::Equal))
                .map(|(i, _)| i);

            if let Some(i) = matched {
                let entry = &mut self.dormant_candidates[i];
                let dy = yaw - entry.anchor_yaw;
                let dp = pitch - entry.anchor_pitch;
                let drift = (dy * dy + dp * dp).sqrt();

                if drift > DORMANT_STATIONARY_RADIUS_RAD {
                    if entry.dormant {
                        log::info!(
                            "BallTracker: dormant object released by movement — old=({:.3},{:.3}) new=({:.3},{:.3}) drift={:.3}rad",
                            entry.anchor_yaw,
                            entry.anchor_pitch,
                            yaw,
                            pitch,
                            drift,
                        );
                    }
                    *entry = DormantCandidate {
                        anchor_yaw: yaw,
                        anchor_pitch: pitch,
                        first_seen_ms: timestamp_ms,
                        last_seen_ms: timestamp_ms,
                        qualifying_since_ms: None,
                        qualifying_observations: 0,
                        dormant: false,
                    };
                    continue;
                }

                entry.last_seen_ms = timestamp_ms;

                // A confirmed dormant object remains dormant regardless of
                // detector confidence. Only movement/long absence releases it.
                if entry.dormant {
                    continue;
                }

                let qualifies = active_ball.is_some_and(|(active_yaw, active_pitch)| {
                    let ady = entry.anchor_yaw - active_yaw;
                    let adp = entry.anchor_pitch - active_pitch;
                    (ady * ady + adp * adp).sqrt() >= DORMANT_MIN_ACTIVE_SEPARATION_RAD
                });

                if qualifies {
                    if entry.qualifying_since_ms.is_none() {
                        entry.qualifying_since_ms = Some(timestamp_ms);
                        entry.qualifying_observations = 1;
                    } else {
                        entry.qualifying_observations =
                            entry.qualifying_observations.saturating_add(1);
                    }

                    let dwell_ms =
                        timestamp_ms - entry.qualifying_since_ms.unwrap_or(timestamp_ms);
                    if dwell_ms >= DORMANT_DWELL_MS
                        && entry.qualifying_observations >= DORMANT_MIN_QUALIFYING_OBSERVATIONS
                    {
                        entry.dormant = true;
                        log::info!(
                            "BallTracker: dormant object learned — yaw={:.3} pitch={:.3} qualified_dwell_ms={:.0} observations={}",
                            entry.anchor_yaw,
                            entry.anchor_pitch,
                            dwell_ms,
                            entry.qualifying_observations,
                        );
                    }
                } else {
                    // Dormancy must be earned while another distinct trusted
                    // ball is continuously active. Pauses/kickoffs therefore
                    // cannot teach the match ball itself as dormant.
                    entry.qualifying_since_ms = None;
                    entry.qualifying_observations = 0;
                }
            } else {
                self.dormant_candidates.push(DormantCandidate {
                    anchor_yaw: yaw,
                    anchor_pitch: pitch,
                    first_seen_ms: timestamp_ms,
                    last_seen_ms: timestamp_ms,
                    qualifying_since_ms: None,
                    qualifying_observations: 0,
                    dormant: false,
                });
            }
        }

        self.dormant_candidates.retain(|entry| {
            let age = timestamp_ms - entry.last_seen_ms;
            if entry.dormant {
                age <= DORMANT_CONFIRMED_ABSENCE_RELEASE_MS
            } else {
                age <= DORMANT_PENDING_ABSENCE_RELEASE_MS
            }
        });
    }

    fn is_dormant_candidate(&self, yaw: f32, pitch: f32) -> bool {
        self.dormant_candidates.iter().any(|entry| {
            if !entry.dormant {
                return false;
            }
            let dy = yaw - entry.anchor_yaw;
            let dp = pitch - entry.anchor_pitch;
            (dy * dy + dp * dp).sqrt() <= DORMANT_FILTER_RADIUS_RAD
        })
    }
}
""",
)

ball = replace_once(
    ball,
    "update-prefix",
    """    fn update(&mut self, detections: &[MappedDetection], timestamp_ms: f64) -> Vec<TrackedEntity> {
        // Step 1-4: filter candidates down to survivors.
""",
    """    fn update(&mut self, detections: &[MappedDetection], timestamp_ms: f64) -> Vec<TrackedEntity> {
        let active_confident = self.active_confident_observation(detections);
        self.update_dormant_registry(detections, timestamp_ms, active_confident);

        // Step 1-4: filter candidates down to survivors.
""",
)

ball = replace_once(
    ball,
    "candidate-filter",
    """            if !self.passes_player_anchor(pos.yaw, pos.pitch) {
                log::trace!(
                    "BallTracker: drop off-player — yaw={:.3} pitch={:.3} nearest player > {:.3}rad",
                    pos.yaw,
                    pos.pitch,
                    self.player_anchor_max_rad
                );
                continue;
            }
            survivors.push(det);
""",
    """            if !self.passes_player_anchor(pos.yaw, pos.pitch) {
                log::trace!(
                    "BallTracker: drop off-player — yaw={:.3} pitch={:.3} nearest player > {:.3}rad",
                    pos.yaw,
                    pos.pitch,
                    self.player_anchor_max_rad
                );
                continue;
            }
            if self.is_dormant_candidate(pos.yaw, pos.pitch) {
                log::debug!(
                    "BallTracker: ignore dormant candidate — yaw={:.3} pitch={:.3} conf={:.2}",
                    pos.yaw,
                    pos.pitch,
                    det.confidence,
                );
                continue;
            }
            survivors.push(det);
""",
)

ball = replace_once(
    ball,
    "selection-and-reacquire",
    """        // Step 5: nearest-to-last selection.
        let best: Option<&MappedDetection> = survivors
            .iter()
            .filter_map(|d| self.score(d).map(|s| (s, *d)))
            .min_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal))
            .map(|(_, d)| d);

        // Step 6: lifecycle.
""",
    """        // Step 5: ordinary nearest-to-last selection. Dormant memory never
        // supplies or forces a candidate; it only removed known stationary
        // rubbish above.
        let mut best: Option<&MappedDetection> = survivors
            .iter()
            .filter_map(|d| self.score(d).map(|s| (s, *d)))
            .min_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal))
            .map(|(_, d)| d);

        // After a full loss, a weak candidate far from the last genuine
        // position must be spatially consistent across three observations
        // before it can become Tracking. Until then it remains untrusted and
        // therefore cannot become a backward-bridge anchor.
        if self.last.is_none()
            && let Some(det) = best
            && let Some(lost) = self.recent_lost
        {
            let pos = det.position.expect("score() guarantees Some");
            let dy = pos.yaw - lost.yaw;
            let dp = pos.pitch - lost.pitch;
            let dist = (dy * dy + dp * dp).sqrt();
            let recent = timestamp_ms - lost.timestamp_ms <= RECENT_LOSS_MEMORY_MS;
            let weak_far = recent
                && dist > FAR_REACQUIRE_RAD
                && det.confidence < FAR_REACQUIRE_STRONG_CONFIDENCE;

            if weak_far {
                let observations = self.pending_reacquire.map_or(1, |previous| {
                    let pdy = pos.yaw - previous.yaw;
                    let pdp = pos.pitch - previous.pitch;
                    if (pdy * pdy + pdp * pdp).sqrt() <= REACQUIRE_MATCH_RAD {
                        previous.observations.saturating_add(1)
                    } else {
                        1
                    }
                });
                self.pending_reacquire = Some(PendingCandidate {
                    yaw: pos.yaw,
                    pitch: pos.pitch,
                    observations,
                });

                if observations < FAR_REACQUIRE_CONFIRM_OBSERVATIONS {
                    log::debug!(
                        "BallTracker: hold weak far reacquire — dist={:.3}rad conf={:.2} confirmation={}/{}",
                        dist,
                        det.confidence,
                        observations,
                        FAR_REACQUIRE_CONFIRM_OBSERVATIONS,
                    );
                    best = None;
                } else {
                    log::info!(
                        "BallTracker: confirmed weak far reacquire — yaw={:.3} pitch={:.3} dist={:.3}rad conf={:.2} observations={}",
                        pos.yaw,
                        pos.pitch,
                        dist,
                        det.confidence,
                        observations,
                    );
                    self.pending_reacquire = None;
                }
            } else {
                // Strong or nearby plausible evidence is trusted immediately.
                self.pending_reacquire = None;
            }
        } else if best.is_none() {
            // Do not carry confirmation across an observation gap.
            self.pending_reacquire = None;
        }

        // Step 6: lifecycle.
""",
)

ball = replace_once(
    ball,
    "accepted-state",
    """            self.coaster.accept_fresh();
            self.last = Some(LastKnown {
                yaw: pos.yaw,
                pitch: pos.pitch,
                origin: det.camera,
            });
            let _ = timestamp_ms;
            self.age_frames = self.age_frames.saturating_add(1);
""",
    """            self.coaster.accept_fresh();
            self.last = Some(LastKnown {
                yaw: pos.yaw,
                pitch: pos.pitch,
                origin: det.camera,
            });
            self.recent_lost = None;
            self.pending_reacquire = None;
            self.age_frames = self.age_frames.saturating_add(1);
""",
)

ball = replace_once(
    ball,
    "loss-memory",
    """            CoastStatus::Lost => {
                if let Some(last) = self.last.take() {
                    log::info!(
                        "BallTracker: track lost after {} coast frames (last yaw={:.3} pitch={:.3})",
                        self.coaster.frames_coasting(),
                        last.yaw,
                        last.pitch
                    );
                    // Age resets on full loss so the next acquisition
""",
    """            CoastStatus::Lost => {
                if let Some(last) = self.last.take() {
                    self.recent_lost = Some(LostMemory {
                        yaw: last.yaw,
                        pitch: last.pitch,
                        timestamp_ms,
                    });
                    self.pending_reacquire = None;
                    log::info!(
                        "BallTracker: track lost after {} coast frames (last yaw={:.3} pitch={:.3})",
                        self.coaster.frames_coasting(),
                        last.yaw,
                        last.pitch
                    );
                    // Age resets on full loss so the next acquisition
""",
)

# ---------------------------------------------------------------------------
# 3) Focused regression tests.
# ---------------------------------------------------------------------------
ball = replace_once(
    ball,
    "tests",
    """    #[test]
    fn class_id_accessor() {
""",
    """    #[test]
    fn confidence_aware_gate_rejects_weak_large_jump() {
        let mut t = BallTracker::new(0).with_max_jump_rad(0.35);
        t.update(&[det(CameraId::Left, 0.0, 0.0, 0.8, 0.5, 0.5)], 0.0);
        let out = t.update(
            &[det(CameraId::Left, 0.25, 0.0, 0.10, 0.5, 0.5)],
            100.0,
        );
        assert_eq!(out[0].state, TrackState::Coasting);
        assert_eq!(out[0].yaw, 0.0);
    }

    fn teach_dormant_spare(t: &mut BallTracker, spare_yaw: f32) {
        for i in 0..=8 {
            let ts = i as f64 * 250.0;
            let out = t.update(
                &[
                    det(CameraId::Left, 0.0, 0.0, 0.90, 0.5, 0.5),
                    det(CameraId::Right, spare_yaw, 0.0, 0.70, 0.5, 0.5),
                ],
                ts,
            );
            assert!((out[0].yaw - 0.0).abs() < 1e-6);
        }
        assert!(
            t.dormant_candidates
                .iter()
                .any(|d| d.dormant && (d.anchor_yaw - spare_yaw).abs() < 0.02)
        );
    }

    #[test]
    fn dormant_object_stays_ignored_even_at_high_confidence() {
        let mut t = BallTracker::new(0).with_max_jump_rad(0.35);
        teach_dormant_spare(&mut t, 0.29);

        let out = t.update(
            &[det(CameraId::Right, 0.29, 0.0, 0.99, 0.5, 0.5)],
            2_250.0,
        );
        assert_eq!(out[0].state, TrackState::Coasting);
        assert!((out[0].yaw - 0.0).abs() < 1e-6);
    }

    #[test]
    fn dormant_object_becomes_eligible_after_real_movement() {
        let mut t = BallTracker::new(0).with_max_jump_rad(0.35);
        teach_dormant_spare(&mut t, 0.29);

        let out = t.update(
            &[det(CameraId::Right, 0.34, 0.0, 0.90, 0.5, 0.5)],
            2_250.0,
        );
        assert_eq!(out[0].state, TrackState::Tracking);
        assert!((out[0].yaw - 0.34).abs() < 1e-6);
    }

    #[test]
    fn stationary_match_ball_is_not_dormant_without_other_active_ball() {
        let mut t = BallTracker::new(0);
        for i in 0..20 {
            let ts = i as f64 * 250.0;
            let out = t.update(
                &[det(CameraId::Left, 0.10, 0.0, 0.90, 0.5, 0.5)],
                ts,
            );
            assert_eq!(out[0].state, TrackState::Tracking);
        }
        assert!(!t.dormant_candidates.iter().any(|d| d.dormant));
    }

    #[test]
    fn weak_far_reacquire_requires_three_consistent_observations() {
        let mut t = BallTracker::new(0).with_max_coast_frames(1);
        t.update(&[det(CameraId::Left, 0.0, 0.0, 0.8, 0.5, 0.5)], 0.0);
        t.update(&[], 100.0); // coast
        let lost = t.update(&[], 200.0);
        assert_eq!(lost[0].state, TrackState::Lost);

        let weak_far = det(CameraId::Right, 0.60, 0.0, 0.20, 0.5, 0.5);
        assert!(t.update(&[weak_far], 300.0).is_empty());
        assert!(t.update(&[weak_far], 400.0).is_empty());
        let out = t.update(&[weak_far], 500.0);
        assert_eq!(out[0].state, TrackState::Tracking);
        assert!((out[0].yaw - 0.60).abs() < 1e-6);
    }

    #[test]
    fn strong_far_reacquire_is_immediate_when_plausible() {
        let mut t = BallTracker::new(0).with_max_coast_frames(1);
        t.update(&[det(CameraId::Left, 0.0, 0.0, 0.8, 0.5, 0.5)], 0.0);
        t.update(&[], 100.0);
        t.update(&[], 200.0);

        let out = t.update(
            &[det(CameraId::Right, 0.60, 0.0, 0.80, 0.5, 0.5)],
            300.0,
        );
        assert_eq!(out[0].state, TrackState::Tracking);
        assert!((out[0].yaw - 0.60).abs() < 1e-6);
    }

    #[test]
    fn stationary_incumbent_never_forces_far_challenger_switch() {
        let mut t = BallTracker::new(0).with_max_jump_rad(1.0);
        for i in 0..20 {
            let ts = i as f64 * 250.0;
            let out = t.update(
                &[
                    det(CameraId::Left, 0.0, 0.0, 0.20, 0.5, 0.5),
                    det(CameraId::Right, 0.50, 0.0, 0.95, 0.5, 0.5),
                ],
                ts,
            );
            assert!(
                out[0].yaw < 0.10,
                "passive registry must never force a challenger switch"
            );
        }
    }

    #[test]
    fn class_id_accessor() {
""",
)

BALL_PATH.write_text(ball)
RUN_LOOP_PATH.write_text(run_loop)

print("DORMANT_BALL_REGISTRY_V2_PATCH_APPLIED")
print("  media_time=source_frame_index_over_fps_all_strides")
print("  forced_switches=none")
print("  dormant_confidence_bypass=none")
print("  dormant_release=movement_or_90s_absence")
print("  dormant_learning=continuous_distinct_active_evidence_1500ms_8obs")
print("  weak_far_reacquire=3_consistent_observations")
print("  confidence_jump_guard=enabled")
