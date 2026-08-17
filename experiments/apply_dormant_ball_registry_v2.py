#!/usr/bin/env python3
"""Apply test-only dynamic dormant-ball memory to Reco BallTracker.

Passive only: learn stationary ball-like candidates while another ball is
confidently active; ignore learned dormant candidates only during uncertainty;
never force a switch. A moved object is immediately eligible once it leaves its
small dormant region. Keeps only the previously-promising confidence-aware jump
gate from the rejected active-ball v1 experiment.
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


replace_once("constants", """pub const DEFAULT_PLAYER_ANCHOR_RAD: f32 = 0.20;

/// Singleton ball tracker emitting at most one
""", """pub const DEFAULT_PLAYER_ANCHOR_RAD: f32 = 0.20;

const LARGE_JUMP_RAD: f32 = 0.20;
const VERY_LARGE_JUMP_RAD: f32 = 0.30;
const LARGE_JUMP_MIN_CONFIDENCE: f32 = 0.18;
const VERY_LARGE_JUMP_MIN_CONFIDENCE: f32 = 0.30;

const ACTIVE_BALL_CONFIDENCE: f32 = 0.30;
const ACTIVE_BALL_MATCH_RAD: f32 = 0.18;
const DORMANT_STATIONARY_RADIUS_RAD: f32 = 0.040;
const DORMANT_MATCH_RAD: f32 = 0.070;
const DORMANT_FILTER_RADIUS_RAD: f32 = 0.050;
const DORMANT_MIN_ACTIVE_SEPARATION_RAD: f32 = 0.18;
const DORMANT_DWELL_MS: f64 = 2_000.0;
const DORMANT_MIN_OBSERVATIONS: u32 = 8;
const DORMANT_ABSENCE_RELEASE_MS: f64 = 4_000.0;

/// Singleton ball tracker emitting at most one
""")

replace_once("tracker-fields", """    coaster: Coaster,
    last: Option<LastKnown>,
    max_jump_rad: f32,
""", """    coaster: Coaster,
    last: Option<LastKnown>,
    /// Passive stationary-object memory. It can suppress a candidate while
    /// uncertain, but can never select/force a replacement ball.
    dormant_candidates: Vec<DormantCandidate>,
    max_jump_rad: f32,
""")

replace_once("state-struct", """struct LastKnown {
    yaw: f32,
    pitch: f32,
    origin: CameraId,
}

impl BallTracker {
""", """struct LastKnown {
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
""")

replace_once("init-field", """            coaster: Coaster::new(DEFAULT_COAST_FRAMES),
            last: None,
            max_jump_rad: DEFAULT_MAX_JUMP_RAD,
""", """            coaster: Coaster::new(DEFAULT_COAST_FRAMES),
            last: None,
            dormant_candidates: Vec::new(),
            max_jump_rad: DEFAULT_MAX_JUMP_RAD,
""")

replace_once("jump-gate", """                if dist > self.max_jump_rad {
                    None
                } else {
                    // Balance proximity and confidence; the 0.1-rad
                    // weight on confidence picks the sharper detection
                    // when two candidates are within a pixel or two.
                    Some(dist - 0.1 * det.confidence)
                }
""", """                if dist > self.max_jump_rad
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
                    Some(dist - 0.1 * det.confidence)
                }
""")

replace_once("helpers", """    /// Decide whether this detection survives the player-anchor gate.
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
""", """    /// Decide whether this detection survives the player-anchor gate.
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
                {
                    return None;
                }
                let dy = pos.yaw - last.yaw;
                let dp = pos.pitch - last.pitch;
                let dist = (dy * dy + dp * dp).sqrt();
                (dist <= ACTIVE_BALL_MATCH_RAD).then_some((d.confidence, pos.yaw, pos.pitch))
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
                if drift <= DORMANT_STATIONARY_RADIUS_RAD {
                    entry.last_seen_ms = timestamp_ms;
                    entry.observations = entry.observations.saturating_add(1);
                } else {
                    if entry.dormant {
                        log::debug!(
                            "BallTracker: dormant object moved — old=({:.3},{:.3}) new=({:.3},{:.3}); eligible",
                            entry.anchor_yaw, entry.anchor_pitch, yaw, pitch,
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
                        "BallTracker: dormant object learned — yaw={:.3} pitch={:.3} dwell_ms={:.0} observations={} separation={:.3}rad",
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
""")

replace_once("update-prefix", """    fn update(&mut self, detections: &[MappedDetection], timestamp_ms: f64) -> Vec<TrackedEntity> {
        // Step 1-4: filter candidates down to survivors.
""", """    fn update(&mut self, detections: &[MappedDetection], timestamp_ms: f64) -> Vec<TrackedEntity> {
        let active_confident = self.active_confident_observation(detections);
        self.update_dormant_registry(detections, timestamp_ms, active_confident);
        let bypass_dormant_filter = active_confident.is_some();

        // Step 1-4: filter candidates down to survivors.
""")

replace_once("candidate-filter", """            if !self.passes_player_anchor(pos.yaw, pos.pitch) {
                log::trace!(
                    "BallTracker: drop off-player — yaw={:.3} pitch={:.3} nearest player > {:.3}rad",
                    pos.yaw,
                    pos.pitch,
                    self.player_anchor_max_rad
                );
                continue;
            }
            survivors.push(det);
""", """            if !self.passes_player_anchor(pos.yaw, pos.pitch) {
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
                    pos.yaw, pos.pitch, det.confidence,
                );
                continue;
            }
            survivors.push(det);
""")

replace_once("tests", """    #[test]
    fn class_id_accessor() {
""", """    #[test]
    fn confidence_aware_gate_rejects_weak_large_jump() {
        let mut t = BallTracker::new(0).with_max_jump_rad(0.35);
        t.update(&[det(CameraId::Left, 0.0, 0.0, 0.8, 0.5, 0.5)], 0.0);
        let out = t.update(&[det(CameraId::Left, 0.25, 0.0, 0.10, 0.5, 0.5)], 100.0);
        assert_eq!(out[0].state, TrackState::Coasting);
        assert_eq!(out[0].yaw, 0.0);
    }

    fn teach_dormant_spare(t: &mut BallTracker, spare_yaw: f32) {
        for i in 0..=9 {
            let ts = i as f64 * 250.0;
            let out = t.update(
                &[
                    det(CameraId::Left, 0.0, 0.0, 0.90, 0.5, 0.5),
                    det(CameraId::Right, spare_yaw, 0.0, 0.45, 0.5, 0.5),
                ],
                ts,
            );
            assert!((out[0].yaw - 0.0).abs() < 1e-6);
        }
        assert!(t.dormant_candidates.iter().any(|d| d.dormant));
    }

    #[test]
    fn dormant_candidate_is_filtered_only_when_active_ball_uncertain() {
        let mut t = BallTracker::new(0).with_max_jump_rad(0.35);
        teach_dormant_spare(&mut t, 0.30);
        let out = t.update(
            &[det(CameraId::Right, 0.30, 0.0, 0.80, 0.5, 0.5)],
            2600.0,
        );
        assert_eq!(out[0].state, TrackState::Coasting);
        assert!((out[0].yaw - 0.0).abs() < 1e-6);
    }

    #[test]
    fn moved_dormant_object_is_immediately_eligible_outside_small_region() {
        let mut t = BallTracker::new(0).with_max_jump_rad(0.35);
        teach_dormant_spare(&mut t, 0.27);
        let out = t.update(
            &[det(CameraId::Right, 0.34, 0.0, 0.80, 0.5, 0.5)],
            2600.0,
        );
        assert_eq!(out[0].state, TrackState::Tracking);
        assert!((out[0].yaw - 0.34).abs() < 1e-6);
    }

    #[test]
    fn class_id_accessor() {
""")

PATH.write_text(s)
print("DORMANT_BALL_REGISTRY_PATCH_APPLIED")
print("  forced_switches=none")
print("  weak_reacquire_confirmation=none")
print("  confidence_jump_guard=enabled")
print("  dormant_learning=stationary_plus_other_confident_active_ball")
print("  dormant_filter=uncertainty_only")
print("  confident_active_ball=bypass")
print("  motion_outside_region=eligible")
