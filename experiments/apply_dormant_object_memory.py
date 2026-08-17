#!/usr/bin/env python3
"""Apply test-only persistent dormant-ball object memory to Reco.

Starts from the exact good containment-v2 Reco tree. This patch does NOT alter
BallTracker scoring, max-jump behaviour, coast/reacquisition rules, or panner
logic. It inserts a separate raw-candidate memory layer before the existing
singleton ball selection:

- raw ball candidates are associated into lightweight persistent object tracks;
- an object can become DORMANT only after remaining spatially stationary for a
  sustained window while a different, non-dormant ball is confidently active;
- dormant objects survive detector misses indefinitely;
- dormant objects are filtered only while the active match ball is uncertain;
- a known dormant object is not allowed to masquerade as the "confident active"
  bypass merely because YOLO reports high confidence at its old location;
- a dormant object is released only after observed motion: two consistent
  observations outside its stationary anchor radius;
- no dormant object ever forces a switch to another candidate.

Production video-stitcher/main is untouched. Exact replacements fail closed if
validated Reco c8b0d74 moves.
"""
from pathlib import Path

ROOT = Path("/tmp/video-stitcher/crates/reco-autocam/src/trackers")
BALL = ROOT / "ball.rs"
MOD = ROOT / "mod.rs"
DORMANT = ROOT / "dormant_ball.rs"

ball = BALL.read_text()
mods = MOD.read_text()


def replace_once(text: str, label: str, old: str, new: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


DORMANT.write_text(r'''//! Passive persistent memory for stationary ball-like detector candidates.
//!
//! This module deliberately does not choose the active match ball. It only
//! remembers raw candidate identities well enough to say "this object has sat
//! here while another ball was confidently active" and suppress that known
//! dormant object during later uncertainty.

#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct CandidateObservation {
    pub yaw: f32,
    pub pitch: f32,
    pub confidence: f32,
}

#[derive(Debug, Clone, Copy)]
struct PendingMotion {
    yaw: f32,
    pitch: f32,
    count: u32,
}

#[derive(Debug, Clone)]
struct ObjectTrack {
    id: u64,
    last_yaw: f32,
    last_pitch: f32,
    stationary_yaw: f32,
    stationary_pitch: f32,
    stationary_since_ms: f64,
    last_seen_ms: f64,
    observations: u32,
    dormant: bool,
    pending_motion: Option<PendingMotion>,
}

const NORMAL_ASSOCIATION_RAD: f32 = 0.09;
const DORMANT_ASSOCIATION_RAD: f32 = 0.14;
const STATIONARY_RADIUS_RAD: f32 = 0.045;
const DORMANT_FILTER_RADIUS_RAD: f32 = 0.055;
const DORMANT_RELEASE_RADIUS_RAD: f32 = 0.075;
const MOVE_CONFIRM_MATCH_RAD: f32 = 0.08;
const MOVE_CONFIRM_OBSERVATIONS: u32 = 2;
const STATIONARY_DWELL_MS: f64 = 2_500.0;
const STATIONARY_MIN_OBSERVATIONS: u32 = 10;
const MIN_ACTIVE_SEPARATION_RAD: f32 = 0.25;
const ACTIVE_CONFIDENCE: f32 = 0.30;
const ACTIVE_MATCH_RAD: f32 = 0.20;
const NON_DORMANT_STALE_MS: f64 = 3_000.0;

#[derive(Debug, Default)]
pub(crate) struct DormantBallRegistry {
    tracks: Vec<ObjectTrack>,
    next_id: u64,
}

impl DormantBallRegistry {
    pub fn new() -> Self {
        Self::default()
    }

    fn dist(a_y: f32, a_p: f32, b_y: f32, b_p: f32) -> f32 {
        let dy = a_y - b_y;
        let dp = a_p - b_p;
        (dy * dy + dp * dp).sqrt()
    }

    /// Associate this frame's raw candidates into persistent object tracks.
    /// Dormant tracks never expire merely because YOLO stops seeing them.
    pub fn observe(&mut self, observations: &[CandidateObservation], timestamp_ms: f64) {
        let mut used_tracks = vec![false; self.tracks.len()];

        for obs in observations {
            let best = self
                .tracks
                .iter()
                .enumerate()
                .filter(|(i, _)| !used_tracks[*i])
                .filter_map(|(i, track)| {
                    let gate = if track.dormant {
                        DORMANT_ASSOCIATION_RAD
                    } else {
                        NORMAL_ASSOCIATION_RAD
                    };
                    let d = Self::dist(obs.yaw, obs.pitch, track.last_yaw, track.last_pitch);
                    (d <= gate).then_some((i, d))
                })
                .min_by(|a, b| {
                    a.1.partial_cmp(&b.1)
                        .unwrap_or(std::cmp::Ordering::Equal)
                })
                .map(|(i, _)| i);

            if let Some(i) = best {
                used_tracks[i] = true;
                let track = &mut self.tracks[i];

                if track.dormant {
                    let from_anchor = Self::dist(
                        obs.yaw,
                        obs.pitch,
                        track.stationary_yaw,
                        track.stationary_pitch,
                    );
                    if from_anchor <= DORMANT_RELEASE_RADIUS_RAD {
                        track.pending_motion = None;
                    } else {
                        let count = track.pending_motion.map_or(1, |pending| {
                            if Self::dist(obs.yaw, obs.pitch, pending.yaw, pending.pitch)
                                <= MOVE_CONFIRM_MATCH_RAD
                            {
                                pending.count.saturating_add(1)
                            } else {
                                1
                            }
                        });
                        track.pending_motion = Some(PendingMotion {
                            yaw: obs.yaw,
                            pitch: obs.pitch,
                            count,
                        });
                        if count >= MOVE_CONFIRM_OBSERVATIONS {
                            log::info!(
                                "DormantBallRegistry: RELEASED id={} anchor=({:.3},{:.3}) moved_to=({:.3},{:.3})",
                                track.id,
                                track.stationary_yaw,
                                track.stationary_pitch,
                                obs.yaw,
                                obs.pitch,
                            );
                            track.dormant = false;
                            track.stationary_yaw = obs.yaw;
                            track.stationary_pitch = obs.pitch;
                            track.stationary_since_ms = timestamp_ms;
                            track.observations = 1;
                            track.pending_motion = None;
                        }
                    }
                } else {
                    let from_stationary_anchor = Self::dist(
                        obs.yaw,
                        obs.pitch,
                        track.stationary_yaw,
                        track.stationary_pitch,
                    );
                    if from_stationary_anchor <= STATIONARY_RADIUS_RAD {
                        track.observations = track.observations.saturating_add(1);
                    } else {
                        // Object is moving: restart the stationarity clock at
                        // its new location rather than creating a chain of fake
                        // "stationary objects" along the trajectory.
                        track.stationary_yaw = obs.yaw;
                        track.stationary_pitch = obs.pitch;
                        track.stationary_since_ms = timestamp_ms;
                        track.observations = 1;
                    }
                }

                track.last_yaw = obs.yaw;
                track.last_pitch = obs.pitch;
                track.last_seen_ms = timestamp_ms;
            } else {
                let id = self.next_id;
                self.next_id = self.next_id.saturating_add(1);
                self.tracks.push(ObjectTrack {
                    id,
                    last_yaw: obs.yaw,
                    last_pitch: obs.pitch,
                    stationary_yaw: obs.yaw,
                    stationary_pitch: obs.pitch,
                    stationary_since_ms: timestamp_ms,
                    last_seen_ms: timestamp_ms,
                    observations: 1,
                    dormant: false,
                    pending_motion: None,
                });
            }
        }

        // Short-lived moving/noisy candidate tracks may expire to bound memory;
        // once an identity is proven dormant it persists through arbitrary
        // detector misses until actual observed movement releases it.
        self.tracks.retain(|track| {
            track.dormant || timestamp_ms - track.last_seen_ms <= NON_DORMANT_STALE_MS
        });
    }

    pub fn is_dormant(&self, yaw: f32, pitch: f32) -> bool {
        self.tracks.iter().any(|track| {
            track.dormant
                && Self::dist(yaw, pitch, track.stationary_yaw, track.stationary_pitch)
                    <= DORMANT_FILTER_RADIUS_RAD
        })
    }

    /// Find a high-confidence observation consistent with the existing active
    /// track. A known dormant identity is explicitly ineligible for this bypass.
    pub fn confident_active(
        &self,
        last: Option<(f32, f32)>,
        observations: &[CandidateObservation],
    ) -> Option<CandidateObservation> {
        let (last_yaw, last_pitch) = last?;
        observations
            .iter()
            .copied()
            .filter(|obs| obs.confidence >= ACTIVE_CONFIDENCE)
            .filter(|obs| !self.is_dormant(obs.yaw, obs.pitch))
            .filter(|obs| {
                Self::dist(obs.yaw, obs.pitch, last_yaw, last_pitch) <= ACTIVE_MATCH_RAD
            })
            .max_by(|a, b| {
                a.confidence
                    .partial_cmp(&b.confidence)
                    .unwrap_or(std::cmp::Ordering::Equal)
            })
    }

    /// Mark other persistent stationary identities dormant while a different
    /// ball is confidently active. This only changes eligibility; it never
    /// selects or forces a replacement ball.
    pub fn learn_while_active(&mut self, active: CandidateObservation, timestamp_ms: f64) {
        for track in &mut self.tracks {
            if track.dormant
                || track.observations < STATIONARY_MIN_OBSERVATIONS
                || timestamp_ms - track.stationary_since_ms < STATIONARY_DWELL_MS
            {
                continue;
            }
            let separation = Self::dist(
                track.stationary_yaw,
                track.stationary_pitch,
                active.yaw,
                active.pitch,
            );
            if separation >= MIN_ACTIVE_SEPARATION_RAD {
                track.dormant = true;
                track.pending_motion = None;
                log::info!(
                    "DormantBallRegistry: LEARNED id={} anchor=({:.3},{:.3}) dwell_ms={:.0} observations={} active=({:.3},{:.3}) separation={:.3}rad",
                    track.id,
                    track.stationary_yaw,
                    track.stationary_pitch,
                    timestamp_ms - track.stationary_since_ms,
                    track.observations,
                    active.yaw,
                    active.pitch,
                    separation,
                );
            }
        }
    }

    #[cfg(test)]
    fn dormant_count(&self) -> usize {
        self.tracks.iter().filter(|t| t.dormant).count()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn obs(yaw: f32, confidence: f32) -> CandidateObservation {
        CandidateObservation {
            yaw,
            pitch: 0.0,
            confidence,
        }
    }

    #[test]
    fn moving_object_does_not_turn_into_many_dormant_locations() {
        let mut r = DormantBallRegistry::new();
        for i in 0..40 {
            let ts = i as f64 * 100.0;
            let moving = obs(-0.8 + i as f32 * 0.03, 0.55);
            let active = obs(0.8, 0.9);
            r.observe(&[moving, active], ts);
            if let Some(a) = r.confident_active(Some((0.8, 0.0)), &[moving, active]) {
                r.learn_while_active(a, ts);
            }
        }
        assert_eq!(r.dormant_count(), 0);
    }

    #[test]
    fn stationary_object_becomes_dormant_only_with_other_active_ball() {
        let mut r = DormantBallRegistry::new();
        for i in 0..30 {
            let ts = i as f64 * 100.0;
            let spare = obs(0.7, 0.45);
            let active = obs(-0.4, 0.9);
            r.observe(&[spare, active], ts);
            let a = r
                .confident_active(Some((-0.4, 0.0)), &[spare, active])
                .unwrap();
            r.learn_while_active(a, ts);
        }
        assert!(r.is_dormant(0.7, 0.0));
        assert!(!r.is_dormant(-0.4, 0.0));
    }

    #[test]
    fn dormant_identity_survives_long_detector_absence() {
        let mut r = DormantBallRegistry::new();
        for i in 0..30 {
            let ts = i as f64 * 100.0;
            let spare = obs(0.7, 0.45);
            let active = obs(-0.4, 0.9);
            r.observe(&[spare, active], ts);
            let a = r
                .confident_active(Some((-0.4, 0.0)), &[spare, active])
                .unwrap();
            r.learn_while_active(a, ts);
        }
        r.observe(&[], 60_000.0);
        assert!(r.is_dormant(0.7, 0.0));
    }

    #[test]
    fn dormant_identity_releases_only_after_confirmed_observed_motion() {
        let mut r = DormantBallRegistry::new();
        for i in 0..30 {
            let ts = i as f64 * 100.0;
            let spare = obs(0.7, 0.45);
            let active = obs(-0.4, 0.9);
            r.observe(&[spare, active], ts);
            let a = r
                .confident_active(Some((-0.4, 0.0)), &[spare, active])
                .unwrap();
            r.learn_while_active(a, ts);
        }
        assert!(r.is_dormant(0.7, 0.0));

        // One displaced sighting is not enough to erase persistent memory.
        r.observe(&[obs(0.80, 0.5)], 3_100.0);
        assert!(r.is_dormant(0.7, 0.0));
        // A second consistent displaced sighting proves that object moved.
        r.observe(&[obs(0.81, 0.5)], 3_200.0);
        assert!(!r.is_dormant(0.7, 0.0));
    }

    #[test]
    fn known_dormant_object_cannot_be_the_confident_active_bypass() {
        let mut r = DormantBallRegistry::new();
        for i in 0..30 {
            let ts = i as f64 * 100.0;
            let spare = obs(0.7, 0.8);
            let active = obs(-0.4, 0.9);
            r.observe(&[spare, active], ts);
            let a = r
                .confident_active(Some((-0.4, 0.0)), &[spare, active])
                .unwrap();
            r.learn_while_active(a, ts);
        }
        let only_dormant = [obs(0.7, 0.95)];
        assert!(r.confident_active(Some((0.7, 0.0)), &only_dormant).is_none());
    }
}
''')

mods = replace_once(
    mods,
    "module-declaration",
    "pub mod ball;\npub mod class_provider;\n",
    "pub mod ball;\nmod dormant_ball;\npub mod class_provider;\n",
)

ball = replace_once(
    ball,
    "import-registry",
    "use crate::trackers::filters::{CoastStatus, Coaster};\n",
    "use crate::trackers::dormant_ball::{CandidateObservation, DormantBallRegistry};\nuse crate::trackers::filters::{CoastStatus, Coaster};\n",
)

ball = replace_once(
    ball,
    "tracker-field",
    "    coaster: Coaster,\n    last: Option<LastKnown>,\n    max_jump_rad: f32,\n",
    "    coaster: Coaster,\n    last: Option<LastKnown>,\n    /// Passive raw-candidate object memory. It never chooses a replacement\n    /// ball; it only suppresses identities proven dormant while the active\n    /// ball is otherwise uncertain.\n    dormant_registry: DormantBallRegistry,\n    max_jump_rad: f32,\n",
)

ball = replace_once(
    ball,
    "tracker-init",
    "            coaster: Coaster::new(DEFAULT_COAST_FRAMES),\n            last: None,\n            max_jump_rad: DEFAULT_MAX_JUMP_RAD,\n",
    "            coaster: Coaster::new(DEFAULT_COAST_FRAMES),\n            last: None,\n            dormant_registry: DormantBallRegistry::new(),\n            max_jump_rad: DEFAULT_MAX_JUMP_RAD,\n",
)

ball = replace_once(
    ball,
    "candidate-selection",
    '''        // Step 1-4: filter candidates down to survivors.\n        let mut survivors: Vec<&MappedDetection> = Vec::with_capacity(detections.len());\n        for det in detections {\n            if det.class_id != self.class_id {\n                continue;\n            }\n            let Some(pos) = det.position else {\n                log::trace!(\n                    "BallTracker: drop — projection failed (class={} conf={:.2})",\n                    det.class_id,\n                    det.confidence\n                );\n                continue;\n            };\n            if !self.passes_player_anchor(pos.yaw, pos.pitch) {\n                log::trace!(\n                    "BallTracker: drop off-player — yaw={:.3} pitch={:.3} nearest player > {:.3}rad",\n                    pos.yaw,\n                    pos.pitch,\n                    self.player_anchor_max_rad\n                );\n                continue;\n            }\n            survivors.push(det);\n        }\n\n        // Step 5: nearest-to-last selection.\n''',
    '''        // Step 1-3 remain the validated good-v2 path: class, projection,\n        // and player-anchor filtering. Keep those raw eligible candidates\n        // intact so the dormant registry can track object identity separately\n        // from the active singleton selection.\n        let mut eligible: Vec<&MappedDetection> = Vec::with_capacity(detections.len());\n        for det in detections {\n            if det.class_id != self.class_id {\n                continue;\n            }\n            let Some(pos) = det.position else {\n                log::trace!(\n                    "BallTracker: drop — projection failed (class={} conf={:.2})",\n                    det.class_id,\n                    det.confidence\n                );\n                continue;\n            };\n            if !self.passes_player_anchor(pos.yaw, pos.pitch) {\n                log::trace!(\n                    "BallTracker: drop off-player — yaw={:.3} pitch={:.3} nearest player > {:.3}rad",\n                    pos.yaw,\n                    pos.pitch,\n                    self.player_anchor_max_rad\n                );\n                continue;\n            }\n            eligible.push(det);\n        }\n\n        let observations: Vec<CandidateObservation> = eligible\n            .iter()\n            .map(|det| {\n                let pos = det.position.expect("eligible candidates require position");\n                CandidateObservation {\n                    yaw: pos.yaw,\n                    pitch: pos.pitch,\n                    confidence: det.confidence,\n                }\n            })\n            .collect();\n\n        // Identity memory observes raw candidates before selection. It never\n        // mutates `last` or picks an alternative match-ball candidate.\n        self.dormant_registry.observe(&observations, timestamp_ms);\n        let last_position = self.last.map(|last| (last.yaw, last.pitch));\n        let active_confident = self\n            .dormant_registry\n            .confident_active(last_position, &observations);\n        if let Some(active) = active_confident {\n            self.dormant_registry\n                .learn_while_active(active, timestamp_ms);\n        }\n\n        // If a non-dormant ball is confidently consistent with the existing\n        // track, preserve the exact validated good-v2 candidate set. Dormant\n        // memory only participates when that confidence is absent.\n        let bypass_dormant_filter = active_confident.is_some();\n        let mut survivors: Vec<&MappedDetection> = Vec::with_capacity(eligible.len());\n        for (det, obs) in eligible.into_iter().zip(observations.iter()) {\n            if !bypass_dormant_filter && self.dormant_registry.is_dormant(obs.yaw, obs.pitch) {\n                log::debug!(\n                    "BallTracker: suppress known dormant object while active ball uncertain — yaw={:.3} pitch={:.3} conf={:.2}",\n                    obs.yaw,\n                    obs.pitch,\n                    obs.confidence,\n                );\n                continue;\n            }\n            survivors.push(det);\n        }\n\n        // Step 5: the actual active-ball selector is unchanged from good v2.\n''',
)

MOD.write_text(mods)
BALL.write_text(ball)
print("DORMANT_OBJECT_MEMORY_PATCH_APPLIED")
print("  base_active_tracker=exact validated c8b0d74 behaviour")
print("  forced_switches=false")
print("  jump_guard=false")
print("  weak_reacquire_delay=false")
print("  dormant_memory=persistent object tracks")
print("  dormant_absence_release=false")
print("  dormant_release=confirmed observed motion only")
print("  filter_scope=uncertainty only")
