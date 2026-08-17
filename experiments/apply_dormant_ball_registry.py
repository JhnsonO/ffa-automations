#!/usr/bin/env python3
"""Apply test-only dynamic dormant-ball candidate memory to Reco BallTracker.

This replaces the rejected forced stationary-ball challenger experiment with a
passive candidate filter:
- stationary ball-like objects are learned while another ball is confidently
  tracked elsewhere;
- learned dormant objects are ignored only when the active ball is uncertain;
- a confidently observed active ball bypasses the dormant filter completely;
- a dormant object becomes eligible again as soon as it moves outside its
  learned small stationary region (and its registry entry is reset when motion
  is observed).

The patch also keeps the promising confidence-aware large-jump guard from the
previous A/B. It does NOT force a tracker switch, does NOT blacklist a whole
pitch area permanently, and does NOT add weak-reacquisition confirmation.
Production video-stitcher/main is untouched; exact replacements fail closed if
the validated c8b0d74 source moves.
"""
from pathlib import Path

PATH = Path("/tmp/video-stitcher/crates/reco-autocam/src/trackers/ball.rs")
s = PATH.read_text()


def replace_once(label: str, old: str, new: str) -> None:
    global s
    count = s.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    s = s.replace(old, new, 1)


replace_once(
    "constants",
    """pub const DEFAULT_PLAYER_ANCHOR_RAD: f32 = 0.20;

/// Singleton ball tracker emitting at most one
""",
    """pub const DEFAULT_PLAYER_ANCHOR_RAD: f32 = 0.20;

// Test-derived guard retained from active-ball validation v1: a large jump
// must carry proportionally stronger detector evidence before it is allowed to
// become a genuine Tracking/bridge anchor.
const LARGE_JUMP_RAD: f32 = 0.20;
const VERY_LARGE_JUMP_RAD: f32 = 0.30;
const LARGE_JUMP_MIN_CONFIDENCE: f32 = 0.18;
const VERY_LARGE_JUMP_MIN_CONFIDENCE: f32 = 0.30;

// Dynamic dormant-ball registry. These are candidate-memory thresholds, not
// camera/panner behaviour. A dormant entry is a small object region, not a
// permanent pitch-location ban: motion outside the filter radius is eligible.
const ACTIVE_BALL_CONFIDENCE: f32 = 0.30;
const ACTIVE_BALL_MATCH_RAD: f32 = 0.18;
const DORMANT_STATIONARY_RADIUS_RAD: f32 = 0.025;
const DORMANT_MATCH_RAD: f32 = 0.050;
const DORMANT_FILTER_RADIUS_RAD: f32 = 0.040;
const DORMANT_MIN_ACTIVE_SEPARATION_RAD: f32 = 0.18;
const DORMANT_DWELL_MS: f64 = 2_000.0;
const DORMANT_MIN_OBSERVATIONS: u32 = 12;
const DORMANT_ABSENCE_RELEASE_MS: f64 = 4_000.0;

/// Singleton ball tracker emitting at most one
""",
)

replace_once(
    "tracker-fields",
    """    coaster: Coaster,
    last: Option<LastKnown>,
    max_jump_rad: f32,
""",
    """    coaster: Coaster,
    last: Option<LastKnown>,
    /// Passive memory of repeatedly stationary ball-like candidates. Entries
    /// never force a switch; they only suppress known dormant candidates while
    /// the match ball is uncertain.
    dormant_candidates: Vec<DormantCandidate>,
    max_jump_rad: f32,
""",
)

replace_once(
    "state-struct",
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
struct DormantCandidate {
    anchor_yaw: f32,
    anchor_pitch: f32,
    first_seen_ms: f64,
    last_seen_ms: f64,
    observations: u32,
    dormant: bool,
}

impl BallTracker {
""",
)

replace_once(
    "init-field",
    """            coaster: Coaster::new(DEFAULT_COAST_FRAMES),
            last: None,
            max_jump_rad: DEFAULT_MAX_JUMP_RAD,
""",
    """            coaster: Coaster::new(DEFAULT_COAST_FRAMES),
            last: None,
            dormant_candidates: Vec::new(),
            max_jump_rad: DEFAULT_MAX_JUMP_RAD,
""",
)

replace_once(
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

replace_once(
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

    /// A high-confidence observation close to the current trusted ball means
    /// we already know which ball is active. In that case dormant memory must
    /// not second-guess the tracker at all.
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
                {
                    return None;
                }
                let dy = pos.yaw - last.yaw;
                let dp = pos.pitch - last.pitch;
                let dist = (dy * dy + dp * dp).sqrt();
                (dist <= ACTIVE_BALL_MATCH_RAD).then_some((d.confidence, pos.yaw, pos.pitch))
            })
            .max_by(|a, b| {
                a.0.partial_cmp(&b.0)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
            .map(|(_, yaw, pitch)| (yaw, pitch))
    }

    /// Update passive stationary-object memory from detector candidates. A
    /// candidate only becomes dormant after it has stayed in a tiny region for
    /// long enough AND another confidently tracked ball is elsewhere. That
    /// prevents a stationary match ball at kickoff/free-kick from blacklisting
    /// itself.
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
                .min_by(|a, b| {
                    a.1.partial_cmp(&b.1)
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
                .map(|(i, _)| i);

            if let Some(i) = matched {
                let entry = &mut self.dormant_candidates[i];
                let dy = yaw - entry.anchor_yaw;
                let dp = pitch - entry.anchor_pitch;
                let drift = (dy * dy + dp * dp).sqrt();
                if drift <= DORMANT_STATIONARY_RADIUS_RAD {
                    entry.last_seen_ms = timestamp_ms;
                    entry.observations = entry.observations.saturating_add(1);
                } else {
                    if entry.dormant {
                        log::debug!(
                            "BallTracker: dormant candidate MOVED — old=({:.3},{:.3}) new=({:.3},{:.3}); eligible again",
                            entry.anchor_yaw,
                            entry.anchor_pitch,
                            yaw,
                            pitch,
                        );
                    }
                    *entry = DormantCandidate {
                        anchor_yaw: yaw,
                        anchor_pitch: pitch,
                        first_seen_ms: timestamp_ms,
                        last_seen_ms: timestamp_ms,
                        observations: 1,
                        dormant: false,
                    };
                }
            } else {
                self.dormant_candidates.push(DormantCandidate {
                    anchor_yaw: yaw,
                    anchor_pitch: pitch,
                    first_seen_ms: timestamp_ms,
                    last_seen_ms: timestamp_ms,
                    observations: 1,
                    dormant: false,
                });
            }
        }

        // A stale region should not become a permanent pitch-coordinate ban if
        // the object disappeared/moved while YOLO briefly missed it.
        self.dormant_candidates.retain(|entry| {
            timestamp_ms - entry.last_seen_ms <= DORMANT_ABSENCE_RELEASE_MS
        });

        if let Some((active_yaw, active_pitch)) = active_ball {
            for entry in &mut self.dormant_candidates {
                if entry.dormant
                    || entry.observations < DORMANT_MIN_OBSERVATIONS
                    || entry.last_seen_ms - entry.first_seen_ms < DORMANT_DWELL_MS
                {
                    continue;
                }
                let dy = entry.anchor_yaw - active_yaw;
                let dp = entry.anchor_pitch - active_pitch;
                let separation = (dy * dy + dp * dp).sqrt();
                if separation >= DORMANT_MIN_ACTIVE_SEPARATION_RAD {
                    entry.dormant = true;
                    log::info!(
                        "BallTracker: dormant candidate LEARNED — yaw={:.3} pitch={:.3} dwell_ms={:.0} observations={} active_separation={:.3}rad",
                        entry.anchor_yaw,
                        entry.anchor_pitch,
                        entry.last_seen_ms - entry.first_seen_ms,
                        entry.observations,
                        separation,
                    );
                }
            }
        }
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

replace_once(
    "update-prefix",
    """    fn update(&mut self, detections: &[MappedDetection], timestamp_ms: f64) -> Vec<TrackedEntity> {
        // Step 1-4: filter candidates down to survivors.
""",
    """    fn update(&mut self, detections: &[MappedDetection], timestamp_ms: f64) -> Vec<TrackedEntity> {
        let active_confident = self.active_confident_observation(detections);
        self.update_dormant_registry(detections, timestamp_ms, active_confident);
        let bypass_dormant_filter = active_confident.is_some();

        // Step 1-4: filter candidates down to survivors.
""",
)

replace_once(
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
            if !bypass_dormant_filter && self.is_dormant_candidate(pos.yaw, pos.pitch) {
                log::debug!(
                    "BallTracker: ignore dormant candidate while active ball uncertain — yaw={:.3} pitch={:.3} conf={:.2}",
                    pos.yaw,
                    pos.pitch,
                    det.confidence,
                );
                continue;
            }
            survivors.push(det);
""",
)

replace_once(
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

    #[test]
    fn dormant_candidate_is_learned_but_does_not_interfere_with_confident_active_ball() {
        let mut t = BallTracker::new(0).with_max_jump_rad(0.35);
        // Active match ball at 0.0; spare/dead ball at 0.30. The active ball
        // remains the nearest strong observation while the spare accumulates
        // enough dwell to become dormant.
        for i in 0..=5 {
            let ts = i as f64 * 500.0;
            let out = t.update(
                &[
                    det(CameraId::Left, 0.0, 0.0, 0.90, 0.5, 0.5),
                    det(CameraId::Right, 0.30, 0.0, 0.45, 0.5, 0.5),
                ],
                ts,
            );
            assert!((out[0].yaw - 0.0).abs() < 1e-6);
        }
        assert!(t.dormant_candidates.iter().any(|d| d.dormant));

        // Once the active ball is uncertain/absent, the learned stationary
        // candidate must NOT steal the trusted track even though it is within
        // the ordinary 0.35rad max-jump gate.
        let out = t.update(
            &[det(CameraId::Right, 0.30, 0.0, 0.80, 0.5, 0.5)],
            3100.0,
        );
        assert_eq!(out[0].state, TrackState::Coasting);
        assert!((out[0].yaw - 0.0).abs() < 1e-6);
    }

    #[test]
    fn dormant_object_becomes_eligible_again_after_moving_outside_region() {
        let mut t = BallTracker::new(0).with_max_jump_rad(0.35);
        for i in 0..=5 {
            let ts = i as f64 * 500.0;
            t.update(
                &[
                    det(CameraId::Left, 0.0, 0.0, 0.90, 0.5, 0.5),
                    det(CameraId::Right, 0.27, 0.0, 0.45, 0.5, 0.5),
                ],
                ts,
            );
        }
        assert!(t.dormant_candidates.iter().any(|d| d.dormant));

        // Moving > filter/stationary radius means the object is no longer at
        // its dormant location. A strong moved observation is therefore a
        // normal candidate again rather than being permanently blacklisted.
        let out = t.update(
            &[det(CameraId::Right, 0.34, 0.0, 0.80, 0.5, 0.5)],
            3100.0,
        );
        assert_eq!(out[0].state, TrackState::Tracking);
        assert!((out[0].yaw - 0.34).abs() < 1e-6);
    }

    #[test]
    fn class_id_accessor() {
""",
)

PATH.write_text(s)
print("DORMANT_BALL_REGISTRY_PATCH_APPLIED")
print("  forced_switches=none")
print("  weak_reacquire_confirmation=none")
print("  confidence_jump_guard=enabled")
print("  dormant_learning=stationary+other_confident_active_ball")
print("  dormant_filter=uncertainty_only")
print("  confident_active_ball=bypasses_registry")
print("  dormant_object_movement=eligible_outside_small_region")
