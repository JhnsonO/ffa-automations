#!/usr/bin/env python3
"""Apply test-only active-ball validation to Reco's BallTracker.

Keeps detector candidate generation untouched. The patch hardens the trusted
singleton ball track against two failure modes observed on sample_02:
- weak/far one-frame reacquisitions becoming trusted bridge/panner anchors;
- a long-lived stationary goal/spare ball winning nearest-to-last while a
  stronger competing match-ball candidate is visible elsewhere.

Production video-stitcher/main is not modified; every replacement fails closed
if the validated c8b0d74 source changes.
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

// Test-derived active-ball validation defaults. These deliberately prefer an
// explicit uncertain/coasting state over allowing a weak one-frame detection
// to become a trusted camera/bridge anchor.
const LARGE_JUMP_RAD: f32 = 0.20;
const VERY_LARGE_JUMP_RAD: f32 = 0.30;
const LARGE_JUMP_MIN_CONFIDENCE: f32 = 0.18;
const VERY_LARGE_JUMP_MIN_CONFIDENCE: f32 = 0.30;
const RECENT_LOSS_MEMORY_MS: f64 = 3_000.0;
const FAR_REACQUIRE_RAD: f32 = 0.15;
const FAR_REACQUIRE_STRONG_CONFIDENCE: f32 = 0.35;
const FAR_REACQUIRE_CONFIRM_FRAMES: u32 = 2;
const REACQUIRE_MATCH_RAD: f32 = 0.08;
const STATIONARY_RADIUS_RAD: f32 = 0.06;
const STATIONARY_DWELL_MS: f64 = 1_500.0;
const CHALLENGER_MIN_SEPARATION_RAD: f32 = 0.25;
const CHALLENGER_MIN_CONFIDENCE: f32 = 0.20;
const CHALLENGER_CONFIDENCE_MARGIN: f32 = 0.05;
const CHALLENGER_MATCH_RAD: f32 = 0.08;
const CHALLENGER_CONFIRM_FRAMES: u32 = 3;
const STATIONARY_SUSPECT_RADIUS_RAD: f32 = 0.08;
const STATIONARY_SUSPECT_HOLD_MS: f64 = 8_000.0;

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
    /// Remember the last fully-lost location briefly so a weak detection on
    /// the opposite side cannot become trusted from a single frame.
    recent_lost: Option<LostMemory>,
    pending_reacquire: Option<PendingCandidate>,
    /// Detect when the currently accepted ball has stayed inside a small
    /// angular region long enough to plausibly be a dead/spare goal ball.
    stationary_watch: Option<StationaryWatch>,
    /// A far competing candidate must persist for several frames before it
    /// can evict a stationary incumbent.
    challenger: Option<PendingCandidate>,
    /// Once a stationary incumbent is evicted, suppress its small region for
    /// a short period so nearest-to-last cannot immediately snap back.
    stationary_suspect: Option<SuspectRegion>,
    max_jump_rad: f32,
""",
)

replace_once(
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
    confidence: f32,
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
    count: u32,
}

#[derive(Debug, Clone, Copy)]
struct StationaryWatch {
    anchor_yaw: f32,
    anchor_pitch: f32,
    since_ms: f64,
}

#[derive(Debug, Clone, Copy)]
struct SuspectRegion {
    yaw: f32,
    pitch: f32,
    until_ms: f64,
}

impl BallTracker {
""",
)

replace_once(
    "init-fields",
    """            coaster: Coaster::new(DEFAULT_COAST_FRAMES),
            last: None,
            max_jump_rad: DEFAULT_MAX_JUMP_RAD,
""",
    """            coaster: Coaster::new(DEFAULT_COAST_FRAMES),
            last: None,
            recent_lost: None,
            pending_reacquire: None,
            stationary_watch: None,
            challenger: None,
            stationary_suspect: None,
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
    "expire-suspect",
    """    fn update(&mut self, detections: &[MappedDetection], timestamp_ms: f64) -> Vec<TrackedEntity> {
        // Step 1-4: filter candidates down to survivors.
""",
    """    fn update(&mut self, detections: &[MappedDetection], timestamp_ms: f64) -> Vec<TrackedEntity> {
        if self
            .stationary_suspect
            .is_some_and(|s| timestamp_ms >= s.until_ms)
        {
            self.stationary_suspect = None;
        }

        // Step 1-4: filter candidates down to survivors.
""",
)

replace_once(
    "suspect-filter",
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
            if let Some(suspect) = self.stationary_suspect {
                let dy = pos.yaw - suspect.yaw;
                let dp = pos.pitch - suspect.pitch;
                if (dy * dy + dp * dp).sqrt() <= STATIONARY_SUSPECT_RADIUS_RAD {
                    log::trace!(
                        "BallTracker: drop stationary-suspect region — yaw={:.3} pitch={:.3}",
                        pos.yaw,
                        pos.pitch
                    );
                    continue;
                }
            }
            survivors.push(det);
""",
)

replace_once(
    "selection-and-validation",
    """        // Step 5: nearest-to-last selection.
        let best: Option<&MappedDetection> = survivors
            .iter()
            .filter_map(|d| self.score(d).map(|s| (s, *d)))
            .min_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal))
            .map(|(_, d)| d);

        // Step 6: lifecycle.
""",
    """        // Step 5a: if the incumbent has been effectively stationary for
        // long enough, allow a clearly stronger far candidate to challenge it.
        // Confirmation across three frames prevents a single YOLO false positive
        // from teleporting the trusted ball track.
        let mut forced_best: Option<&MappedDetection> = None;
        if let (Some(last), Some(watch)) = (self.last, self.stationary_watch)
            && timestamp_ms - watch.since_ms >= STATIONARY_DWELL_MS
        {
            let challenger_candidate = survivors
                .iter()
                .copied()
                .filter(|d| {
                    let pos = d.position.expect("survivors require position");
                    let dy = pos.yaw - last.yaw;
                    let dp = pos.pitch - last.pitch;
                    let separation = (dy * dy + dp * dp).sqrt();
                    separation >= CHALLENGER_MIN_SEPARATION_RAD
                        && d.confidence >= CHALLENGER_MIN_CONFIDENCE
                        && d.confidence >= last.confidence + CHALLENGER_CONFIDENCE_MARGIN
                })
                .max_by(|a, b| {
                    a.confidence
                        .partial_cmp(&b.confidence)
                        .unwrap_or(std::cmp::Ordering::Equal)
                });

            if let Some(candidate) = challenger_candidate {
                let pos = candidate.position.expect("survivors require position");
                let count = self.challenger.map_or(1, |previous| {
                    let dy = pos.yaw - previous.yaw;
                    let dp = pos.pitch - previous.pitch;
                    if (dy * dy + dp * dp).sqrt() <= CHALLENGER_MATCH_RAD {
                        previous.count.saturating_add(1)
                    } else {
                        1
                    }
                });
                self.challenger = Some(PendingCandidate {
                    yaw: pos.yaw,
                    pitch: pos.pitch,
                    count,
                });

                if count >= CHALLENGER_CONFIRM_FRAMES {
                    self.stationary_suspect = Some(SuspectRegion {
                        yaw: last.yaw,
                        pitch: last.pitch,
                        until_ms: timestamp_ms + STATIONARY_SUSPECT_HOLD_MS,
                    });
                    self.challenger = None;
                    forced_best = Some(candidate);
                    log::info!(
                        "BallTracker: active-ball switch — stationary incumbent ({:.3},{:.3}) conf={:.2} -> challenger ({:.3},{:.3}) conf={:.2}",
                        last.yaw,
                        last.pitch,
                        last.confidence,
                        pos.yaw,
                        pos.pitch,
                        candidate.confidence
                    );
                }
            } else {
                self.challenger = None;
            }
        } else {
            self.challenger = None;
        }

        // Step 5b: ordinary nearest-to-last selection.
        let mut best: Option<&MappedDetection> = forced_best.or_else(|| {
            survivors
                .iter()
                .filter_map(|d| self.score(d).map(|s| (s, *d)))
                .min_by(|a, b| a.0.partial_cmp(&b.0).unwrap_or(std::cmp::Ordering::Equal))
                .map(|(_, d)| d)
        });

        // After a full loss, a weak detection far from the last trusted
        // location must appear consistently for two frames before it can become
        // Tracking. A strong detection or a nearby reacquisition is immediate.
        if self.last.is_none()
            && let Some(det) = best
            && let Some(lost) = self.recent_lost
        {
            let pos = det.position.expect("score() guarantees Some");
            let dy = pos.yaw - lost.yaw;
            let dp = pos.pitch - lost.pitch;
            let dist = (dy * dy + dp * dp).sqrt();
            let recent = timestamp_ms - lost.timestamp_ms <= RECENT_LOSS_MEMORY_MS;
            if recent && dist > FAR_REACQUIRE_RAD && det.confidence < FAR_REACQUIRE_STRONG_CONFIDENCE {
                let count = self.pending_reacquire.map_or(1, |previous| {
                    let pdy = pos.yaw - previous.yaw;
                    let pdp = pos.pitch - previous.pitch;
                    if (pdy * pdy + pdp * pdp).sqrt() <= REACQUIRE_MATCH_RAD {
                        previous.count.saturating_add(1)
                    } else {
                        1
                    }
                });
                self.pending_reacquire = Some(PendingCandidate {
                    yaw: pos.yaw,
                    pitch: pos.pitch,
                    count,
                });
                if count < FAR_REACQUIRE_CONFIRM_FRAMES {
                    log::debug!(
                        "BallTracker: hold weak far reacquire — dist={:.3}rad conf={:.2} confirmation={}/{}",
                        dist,
                        det.confidence,
                        count,
                        FAR_REACQUIRE_CONFIRM_FRAMES
                    );
                    best = None;
                } else {
                    log::debug!(
                        "BallTracker: confirmed weak far reacquire — dist={:.3}rad conf={:.2}",
                        dist,
                        det.confidence
                    );
                    self.pending_reacquire = None;
                }
            } else {
                self.pending_reacquire = None;
            }
        }

        // Step 6: lifecycle.
""",
)

replace_once(
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

            match self.stationary_watch {
                Some(watch) => {
                    let dy = pos.yaw - watch.anchor_yaw;
                    let dp = pos.pitch - watch.anchor_pitch;
                    if (dy * dy + dp * dp).sqrt() > STATIONARY_RADIUS_RAD {
                        self.stationary_watch = Some(StationaryWatch {
                            anchor_yaw: pos.yaw,
                            anchor_pitch: pos.pitch,
                            since_ms: timestamp_ms,
                        });
                    }
                }
                None => {
                    self.stationary_watch = Some(StationaryWatch {
                        anchor_yaw: pos.yaw,
                        anchor_pitch: pos.pitch,
                        since_ms: timestamp_ms,
                    });
                }
            }

            self.last = Some(LastKnown {
                yaw: pos.yaw,
                pitch: pos.pitch,
                origin: det.camera,
                confidence: det.confidence,
            });
            self.recent_lost = None;
            self.pending_reacquire = None;
            self.age_frames = self.age_frames.saturating_add(1);
""",
)

replace_once(
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
                    self.stationary_watch = None;
                    self.challenger = None;
                    log::info!(
                        "BallTracker: track lost after {} coast frames (last yaw={:.3} pitch={:.3})",
                        self.coaster.frames_coasting(),
                        last.yaw,
                        last.pitch
                    );
                    // Age resets on full loss so the next acquisition
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
    fn weak_far_reacquire_requires_confirmation() {
        let mut t = BallTracker::new(0).with_max_coast_frames(1);
        t.update(&[det(CameraId::Left, 0.0, 0.0, 0.8, 0.5, 0.5)], 0.0);
        t.update(&[], 100.0); // coast
        let lost = t.update(&[], 200.0);
        assert_eq!(lost[0].state, TrackState::Lost);

        let weak_far = det(CameraId::Right, 0.6, 0.0, 0.20, 0.5, 0.5);
        assert!(t.update(&[weak_far], 300.0).is_empty());
        let out = t.update(&[weak_far], 400.0);
        assert_eq!(out[0].state, TrackState::Tracking);
        assert!((out[0].yaw - 0.6).abs() < 1e-6);
    }

    #[test]
    fn stationary_incumbent_switches_only_after_confirmed_stronger_challenger() {
        let mut t = BallTracker::new(0).with_max_jump_rad(0.35);
        for (i, ts) in [0.0, 500.0, 1000.0, 1600.0].into_iter().enumerate() {
            let yaw = 0.005 * i as f32;
            t.update(&[det(CameraId::Left, yaw, 0.0, 0.20, 0.5, 0.5)], ts);
        }

        for ts in [1700.0, 1800.0] {
            let out = t.update(
                &[
                    det(CameraId::Left, 0.015, 0.0, 0.15, 0.5, 0.5),
                    det(CameraId::Right, 0.50, 0.0, 0.55, 0.5, 0.5),
                ],
                ts,
            );
            assert!(out[0].yaw < 0.1, "challenger must not win before confirmation");
        }

        let out = t.update(
            &[
                det(CameraId::Left, 0.015, 0.0, 0.15, 0.5, 0.5),
                det(CameraId::Right, 0.51, 0.0, 0.55, 0.5, 0.5),
            ],
            1900.0,
        );
        assert_eq!(out[0].state, TrackState::Tracking);
        assert!(out[0].yaw > 0.4, "confirmed challenger should become active ball");

        // The old stationary region is temporarily suppressed, so an even
        // higher-confidence detection there cannot immediately steal the track back.
        let out = t.update(
            &[
                det(CameraId::Left, 0.015, 0.0, 0.95, 0.5, 0.5),
                det(CameraId::Right, 0.52, 0.0, 0.45, 0.5, 0.5),
            ],
            2000.0,
        );
        assert!(out[0].yaw > 0.4);
    }

    #[test]
    fn class_id_accessor() {
""",
)

PATH.write_text(s)
print("ACTIVE_BALL_FILTER_PATCH_APPLIED")
print("  weak_large_jump=confidence_gated")
print("  far_reacquire=2_frame_confirmation_when_weak")
print("  stationary_dwell_ms=1500")
print("  stationary_challenger=3_frame_stronger_candidate")
print("  evicted_stationary_region_hold_ms=8000")
