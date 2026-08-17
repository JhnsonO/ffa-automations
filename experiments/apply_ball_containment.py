#!/usr/bin/env python3
"""Apply the OEV ball-containment experiment to Reco's FieldPanner.

TEST ONLY. This intentionally patches the checked-out validated Reco SHA on the
RunPod worker so production video-stitcher/main is not changed before the live
A/B is reviewed. Every replacement is exact and fails closed if upstream moves.
"""
from pathlib import Path

PATH = Path("/tmp/video-stitcher/crates/reco-autocam/src/panners/field.rs")
s = PATH.read_text()


def replace_once(label: str, old: str, new: str) -> None:
    global s
    count = s.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    s = s.replace(old, new, 1)


replace_once(
    "config-fields",
    """    pub ball_weight: f32,
    /// When lookahead is active, the base chase runs this many times
""",
    """    pub ball_weight: f32,
    /// Opt-in safety override for ball-first framing. When enabled, a trusted
    /// Tracking/Bridged ball that reaches the configured safe-edge threshold
    /// temporarily overrides player-cluster composition until it is safely
    /// back inside the shot. Disabled by default so production behavior is
    /// unchanged unless explicitly requested by a panner config.
    pub ball_containment_enabled: bool,
    /// Enter containment when the trusted ball's angular offset reaches this
    /// fraction of the current half-FOV. 0.80 means intervene before the ball
    /// actually reaches the visible edge.
    pub ball_containment_enter_fraction: f32,
    /// Leave containment only after the trusted ball returns inside this
    /// fraction of half-FOV. Must be <= the enter fraction; the gap provides
    /// hysteresis so the camera cannot chatter between broadcast/containment.
    pub ball_containment_exit_fraction: f32,
    /// When lookahead is active, the base chase runs this many times
""",
)

replace_once(
    "config-defaults",
    """            ball_weight: 0.5,
            lookahead_reactivity: 2.5,
""",
    """            ball_weight: 0.5,
            ball_containment_enabled: false,
            ball_containment_enter_fraction: 0.80,
            ball_containment_exit_fraction: 0.45,
            lookahead_reactivity: 2.5,
""",
)

replace_once(
    "config-sanitize",
    """        self.ball_presence_decay = self.ball_presence_decay.clamp(0.0, 1.0);
        self.ball_presence_attack = self.ball_presence_attack.clamp(0.0, 1.0);
        if self != before {
""",
    """        self.ball_presence_decay = self.ball_presence_decay.clamp(0.0, 1.0);
        self.ball_presence_attack = self.ball_presence_attack.clamp(0.0, 1.0);
        self.ball_containment_enter_fraction =
            self.ball_containment_enter_fraction.clamp(0.05, 1.0);
        self.ball_containment_exit_fraction = self
            .ball_containment_exit_fraction
            .clamp(0.0, self.ball_containment_enter_fraction);
        if self != before {
""",
)

replace_once(
    "panner-state",
    """    ball_presence: f32,
    last_ball_yaw: f32,
""",
    """    ball_presence: f32,
    /// True while the ball-first containment safety override owns the target.
    ball_containment_active: bool,
    last_ball_yaw: f32,
""",
)

replace_once(
    "panner-state-init",
    """            ball_presence: 0.0,
            last_ball_yaw: 0.0,
""",
    """            ball_presence: 0.0,
            ball_containment_active: false,
            last_ball_yaw: 0.0,
""",
)

replace_once(
    "containment-state-machine",
    """        let ball_detected = self.config.ball_weight > 0.0
            && world
                .ball
                .as_ref()
                .is_some_and(|b| !matches!(b.state, TrackState::Lost));

        let ball_near_cluster = ball_detected
""",
    """        let ball_detected = self.config.ball_weight > 0.0
            && world
                .ball
                .as_ref()
                .is_some_and(|b| !matches!(b.state, TrackState::Lost));

        // Ball containment is deliberately stricter than the ordinary ball
        // blend. A frozen Coasting position is not trustworthy enough to seize
        // the camera; genuine Tracking and lookahead-backed Bridged states are.
        let containment_ball = if self.config.ball_containment_enabled {
            world.ball.as_ref().filter(|b| {
                matches!(b.state, TrackState::Tracking | TrackState::Bridged)
                    && b.yaw.is_finite()
                    && b.pitch.is_finite()
            })
        } else {
            None
        };

        if let Some(b) = containment_ball {
            let offset = ((b.yaw - self.yaw).powi(2) + (b.pitch - self.pitch).powi(2)).sqrt();
            let half_fov = 0.5 * self.current_fov.to_radians();
            if self.ball_containment_active {
                let exit_threshold =
                    half_fov * self.config.ball_containment_exit_fraction;
                if offset <= exit_threshold {
                    self.ball_containment_active = false;
                    log::debug!(
                        "FieldPanner: ball containment EXIT offset={:.2}deg threshold={:.2}deg",
                        offset.to_degrees(),
                        exit_threshold.to_degrees(),
                    );
                }
            } else {
                let enter_threshold =
                    half_fov * self.config.ball_containment_enter_fraction;
                if offset >= enter_threshold {
                    self.ball_containment_active = true;
                    log::debug!(
                        "FieldPanner: ball containment ENTER state={:?} offset={:.2}deg threshold={:.2}deg",
                        b.state,
                        offset.to_degrees(),
                        enter_threshold.to_degrees(),
                    );
                }
            }
        } else if self.ball_containment_active {
            self.ball_containment_active = false;
            log::debug!("FieldPanner: ball containment EXIT (no trusted Tracking/Bridged ball)");
        }

        let ball_near_cluster = ball_detected
""",
)

replace_once(
    "direct-ball-target",
    """        } else {
            None
        };

        // Lookahead lead: aim toward where the action is heading. The
""",
    """        } else {
            None
        };

        // Safety has priority over composition: once containment owns the
        // shot, point directly at the trusted ball. This is intentionally a
        // 100% ball target, not another weighted blend with the player cluster.
        if self.ball_containment_active
            && let Some(b) = containment_ball
        {
            target = Some((b.yaw, b.pitch));
        }

        // Lookahead lead: aim toward where the action is heading. The
""",
)

replace_once(
    "containment-bypasses-cluster-lookahead",
    """        if !future.is_empty()
            && let Some((ty, tp)) = target
""",
    """        if !self.ball_containment_active
            && !future.is_empty()
            && let Some((ty, tp)) = target
""",
)

replace_once(
    "containment-bypasses-dead-zone",
    """            let dz = self.config.dead_zone_rad;
""",
    """            let dz = if self.ball_containment_active {
                0.0
            } else {
                self.config.dead_zone_rad
            };
""",
)

replace_once(
    "containment-widens-fov-with-cluster",
    """            let target_fov = self.target_fov(c.spread, c.pitch, vel_mag);
""",
    """            let target_fov = if self.ball_containment_active {
                self.config.fov_wide
            } else {
                self.target_fov(c.spread, c.pitch, vel_mag)
            };
""",
)

replace_once(
    "containment-widens-fov-without-cluster",
    """        } else {
            self.last_debug = None;
            if target.is_none() {
""",
    """        } else {
            self.last_debug = None;
            if self.ball_containment_active {
                let target_fov = self.config.fov_wide;
                self.current_fov += self.config.fov_alpha * (target_fov - self.current_fov);
            }
            if target.is_none() {
""",
)

replace_once(
    "containment-debug-mode",
    """            let mode = if cluster.is_some() {
                "cluster"
            } else if ball_detected {
""",
    """            let mode = if self.ball_containment_active {
                "ball-containment"
            } else if cluster.is_some() {
                "cluster"
            } else if ball_detected {
""",
)

PATH.write_text(s)
print("BALL_CONTAINMENT_PATCH_APPLIED")
print("  default_enabled=false")
print("  trusted_states=Tracking|Bridged")
print("  containment_target=100% ball")
print("  containment_lookahead=disabled while active")
print("  containment_dead_zone=disabled while active")
print("  containment_fov=fov_wide while active")
