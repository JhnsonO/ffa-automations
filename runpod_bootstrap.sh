#!/usr/bin/env bash
# TEST-ONLY bootstrap wrapper for experiment/lookahead-ball-containment-01.
#
# Experiment v3 targets the remaining bounce / wrong-way excursions seen in
# run 31908687094 without changing production Reco:
#   - stabilize the ball WorldState BEFORE it enters panner/lookahead
#   - trajectory-prediction innovation gate (3 deg/frame)
#   - suspicious competing trajectory needs 18 continuous frames (~0.3s @60fps)
#   - bridge short Tracking/Coasting/Lost gaps with the last trusted ball
#   - keep the post-smoothing containment guard
#   - replace the simple slew clip with acceleration-limited camera dynamics
#
# Nothing is pushed to video-stitcher and production/main behavior is unchanged.
set -euo pipefail

BASE_FFA_SHA="944e4632f8ef79dd3a7ef9e48c3feb7b4f14b426"
RECO_SHA="f27cbb6d0d65fcf9a11fb4d82d119ae214695318"
BASE_SCRIPT="/tmp/runpod_bootstrap_ball_containment_base.sh"
PATCHER="/tmp/oev_patch_ball_containment.py"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${BASE_FFA_SHA}/runpod_bootstrap.sh" \
  -o "$BASE_SCRIPT"
test -s "$BASE_SCRIPT"

cat > "$PATCHER" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
s = path.read_text()

impl_marker = "\nimpl StitchSession {\n"
if s.count(impl_marker) != 1:
    raise SystemExit(
        f"expected exactly one StitchSession impl marker, found {s.count(impl_marker)}"
    )

helper = r'''
/// TEST-ONLY upstream ball-signal filter.
///
/// The production tracker is left untouched. This filter sits between tracking
/// and the buffered panner/lookahead, so both the normal panner and the final
/// containment guard see the same stabilized ball trajectory.
#[derive(Default)]
struct BallSignalFilterState {
    last_trusted: Option<crate::detect::tracker::TrackedEntity>,
    velocity_yaw: f32,
    velocity_pitch: f32,
    pending: Option<(crate::detect::tracker::TrackedEntity, u8)>,
    missing_frames: u32,
}

fn experiment_angle_delta(target: f32, current: f32) -> f32 {
    let raw = target - current;
    raw.sin().atan2(raw.cos())
}

fn experiment_ball_distance_deg(a: (f32, f32), b: (f32, f32)) -> f32 {
    let yaw = experiment_angle_delta(a.0, b.0).to_degrees();
    let pitch = (a.1 - b.1).to_degrees();
    yaw.hypot(pitch)
}

fn held_ball(
    mut ball: crate::detect::tracker::TrackedEntity,
    age_frames: u64,
) -> crate::detect::tracker::TrackedEntity {
    ball.state = crate::detect::tracker::TrackState::Coasting;
    ball.confidence = 0.0;
    ball.age_frames = ball.age_frames.max(age_frames);
    ball
}

fn accept_filtered_ball(
    candidate: crate::detect::tracker::TrackedEntity,
    state: &mut BallSignalFilterState,
    reset_velocity: bool,
) {
    if reset_velocity {
        state.velocity_yaw = 0.0;
        state.velocity_pitch = 0.0;
    } else if let Some(last) = state.last_trusted {
        const VELOCITY_ALPHA: f32 = 0.25;
        let dy = experiment_angle_delta(candidate.yaw, last.yaw);
        let dp = candidate.pitch - last.pitch;
        state.velocity_yaw =
            (1.0 - VELOCITY_ALPHA) * state.velocity_yaw + VELOCITY_ALPHA * dy;
        state.velocity_pitch =
            (1.0 - VELOCITY_ALPHA) * state.velocity_pitch + VELOCITY_ALPHA * dp;
    }
    state.last_trusted = Some(candidate);
    state.pending = None;
    state.missing_frames = 0;
}

/// Stabilize the current WorldState ball in-place.
///
/// At 60 fps, a 3-degree prediction error in one frame already corresponds to
/// ~180 deg/s of unexpected angular motion, so it is deliberately generous.
/// Anything beyond that is treated as a competing hypothesis. Because this is
/// offline processing with 1.5 s lookahead, requiring 18 continuous frames of
/// evidence still leaves roughly 1.2 s for the panner to anticipate a genuine
/// switch before it is rendered.
fn stabilize_world_ball(
    world: &mut crate::detect::tracker::WorldState,
    state: &mut BallSignalFilterState,
    frame_index: u64,
) {
    const INNOVATION_GATE_DEG: f32 = 3.0;
    const PENDING_MATCH_DEG: f32 = 4.0;
    const PENDING_CONFIRM_FRAMES: u8 = 18;
    const MAX_MISSING_HOLD_FRAMES: u32 = 24;

    let raw_ball = world.ball;

    match raw_ball {
        Some(candidate)
            if matches!(
                candidate.state,
                crate::detect::tracker::TrackState::Tracking
            ) && candidate.yaw.is_finite()
                && candidate.pitch.is_finite() =>
        {
            state.missing_frames = 0;

            let Some(last) = state.last_trusted else {
                accept_filtered_ball(candidate, state, true);
                return;
            };

            let predicted = (
                last.yaw + state.velocity_yaw,
                last.pitch + state.velocity_pitch,
            );
            let innovation_deg = experiment_ball_distance_deg(
                (candidate.yaw, candidate.pitch),
                predicted,
            );

            if innovation_deg <= INNOVATION_GATE_DEG {
                accept_filtered_ball(candidate, state, false);
                return;
            }

            let next_count = match state.pending {
                Some((pending, count))
                    if experiment_ball_distance_deg(
                        (candidate.yaw, candidate.pitch),
                        (pending.yaw, pending.pitch),
                    ) <= PENDING_MATCH_DEG =>
                {
                    count.saturating_add(1)
                }
                _ => 1,
            };
            state.pending = Some((candidate, next_count));

            if next_count >= PENDING_CONFIRM_FRAMES {
                log::info!(
                    "BALL_SIGNAL_SWITCH_ACCEPT frame={} innovation_deg={:.3} confirmations={} confidence={:.3}",
                    frame_index,
                    innovation_deg,
                    next_count,
                    candidate.confidence,
                );
                accept_filtered_ball(candidate, state, true);
                return;
            }

            log::info!(
                "BALL_SIGNAL_HOLD frame={} innovation_deg={:.3} confirmation={}/{} raw_confidence={:.3}",
                frame_index,
                innovation_deg,
                next_count,
                PENDING_CONFIRM_FRAMES,
                candidate.confidence,
            );
            world.ball = Some(held_ball(last, candidate.age_frames));
        }
        Some(candidate)
            if matches!(
                candidate.state,
                crate::detect::tracker::TrackState::Coasting
                    | crate::detect::tracker::TrackState::Lost
            ) =>
        {
            state.pending = None;
            state.missing_frames = state.missing_frames.saturating_add(1);
            if state.missing_frames <= MAX_MISSING_HOLD_FRAMES {
                if let Some(last) = state.last_trusted {
                    world.ball = Some(held_ball(last, candidate.age_frames));
                }
            }
        }
        _ => {
            state.pending = None;
            state.missing_frames = state.missing_frames.saturating_add(1);
            if state.missing_frames <= MAX_MISSING_HOLD_FRAMES {
                if let Some(last) = state.last_trusted {
                    world.ball = Some(held_ball(last, last.age_frames.saturating_add(1)));
                }
            }
        }
    }
}

/// TEST-ONLY final crop guard + camera dynamics.
#[derive(Default)]
struct BallContainmentGuardState {
    last_guard_ball: Option<(f32, f32)>,
    missing_frames: u32,
    last_output_pose: Option<crate::detect::director::ViewportPosition>,
    last_yaw_step: f32,
    last_pitch_step: f32,
}

fn guard_ball_target(
    world: &crate::detect::tracker::WorldState,
    state: &mut BallContainmentGuardState,
) -> Option<(f32, f32)> {
    const MAX_GUARD_HOLD_FRAMES: u32 = 24;

    match world.ball.as_ref() {
        Some(ball)
            if !matches!(ball.state, crate::detect::tracker::TrackState::Lost)
                && ball.yaw.is_finite()
                && ball.pitch.is_finite() =>
        {
            state.missing_frames = 0;
            let target = (ball.yaw, ball.pitch);
            state.last_guard_ball = Some(target);
            Some(target)
        }
        _ => {
            state.missing_frames = state.missing_frames.saturating_add(1);
            if state.missing_frames <= MAX_GUARD_HOLD_FRAMES {
                state.last_guard_ball
            } else {
                None
            }
        }
    }
}

fn camera_axis_step(
    desired_delta: f32,
    previous_step: f32,
    max_step: f32,
    max_accel: f32,
    reversal_brake: f32,
) -> f32 {
    if !desired_delta.is_finite() || desired_delta.abs() < 1.0e-6 {
        return 0.0;
    }

    if previous_step * desired_delta < 0.0 && previous_step.abs() > 1.0e-6 {
        let brake = reversal_brake.min(previous_step.abs());
        return previous_step - previous_step.signum() * brake;
    }

    let stopping_limited =
        (2.0 * max_accel * desired_delta.abs()).sqrt().min(max_step);
    let target_step =
        desired_delta.signum() * stopping_limited.min(desired_delta.abs());

    let change = (target_step - previous_step).clamp(-max_accel, max_accel);
    let mut step = previous_step + change;

    if step.signum() == desired_delta.signum() && step.abs() > desired_delta.abs() {
        step = desired_delta;
    }
    step
}

/// Apply minimum ball containment, then smooth the final camera *dynamics*.
///
/// This is intentionally not another EMA. The panner/lookahead still selects
/// the shot. We only bound speed and acceleration of the final requested crop.
fn enforce_containment_and_dynamics(
    mut pose: crate::detect::director::ViewportPosition,
    world: &crate::detect::tracker::WorldState,
    state: &mut BallContainmentGuardState,
) -> (
    crate::detect::director::ViewportPosition,
    f32,
    f32,
    f32,
    f32,
) {
    const ASPECT: f32 = 16.0 / 9.0;
    const SAFE_MARGIN_DEG: f32 = 3.0;

    const MAX_YAW_STEP_DEG: f32 = 0.75;
    const MAX_PITCH_STEP_DEG: f32 = 0.50;
    const MAX_YAW_ACCEL_DEG: f32 = 0.08;
    const MAX_PITCH_ACCEL_DEG: f32 = 0.06;
    const YAW_REVERSAL_BRAKE_DEG: f32 = 0.25;
    const PITCH_REVERSAL_BRAKE_DEG: f32 = 0.15;

    let original_yaw = pose.yaw;
    let original_pitch = pose.pitch;

    if let (Some(fov_deg), Some((ball_yaw, ball_pitch))) =
        (pose.fov_degrees, guard_ball_target(world, state))
    {
        if fov_deg.is_finite()
            && fov_deg > 0.0
            && pose.yaw.is_finite()
            && pose.pitch.is_finite()
        {
            let half_h_full = (0.5 * fov_deg).to_radians();
            let margin = SAFE_MARGIN_DEG.to_radians();
            let half_h_safe = (half_h_full - margin).max(0.5_f32.to_radians());
            let half_v_full = (half_h_full.tan() / ASPECT).atan();
            let half_v_safe = (half_v_full - margin).max(0.5_f32.to_radians());

            let yaw_delta = experiment_angle_delta(ball_yaw, pose.yaw);
            if yaw_delta > half_h_safe {
                pose.yaw += yaw_delta - half_h_safe;
            } else if yaw_delta < -half_h_safe {
                pose.yaw += yaw_delta + half_h_safe;
            }

            let pitch_delta = ball_pitch - pose.pitch;
            if pitch_delta > half_v_safe {
                pose.pitch += pitch_delta - half_v_safe;
            } else if pitch_delta < -half_v_safe {
                pose.pitch += pitch_delta + half_v_safe;
            }
        }
    }

    let containment_yaw_deg =
        experiment_angle_delta(pose.yaw, original_yaw).abs().to_degrees();
    let containment_pitch_deg = (pose.pitch - original_pitch).abs().to_degrees();

    let desired_yaw = pose.yaw;
    let desired_pitch = pose.pitch;
    let mut dynamics_yaw_reduction_deg = 0.0;
    let mut dynamics_pitch_reduction_deg = 0.0;

    if let Some(prev) = state.last_output_pose {
        let desired_yaw_delta = experiment_angle_delta(desired_yaw, prev.yaw);
        let desired_pitch_delta = desired_pitch - prev.pitch;

        let yaw_step = camera_axis_step(
            desired_yaw_delta,
            state.last_yaw_step,
            MAX_YAW_STEP_DEG.to_radians(),
            MAX_YAW_ACCEL_DEG.to_radians(),
            YAW_REVERSAL_BRAKE_DEG.to_radians(),
        );
        let pitch_step = camera_axis_step(
            desired_pitch_delta,
            state.last_pitch_step,
            MAX_PITCH_STEP_DEG.to_radians(),
            MAX_PITCH_ACCEL_DEG.to_radians(),
            PITCH_REVERSAL_BRAKE_DEG.to_radians(),
        );

        pose.yaw = prev.yaw + yaw_step;
        pose.pitch = prev.pitch + pitch_step;

        dynamics_yaw_reduction_deg =
            (desired_yaw_delta.abs() - yaw_step.abs()).max(0.0).to_degrees();
        dynamics_pitch_reduction_deg =
            (desired_pitch_delta.abs() - pitch_step.abs()).max(0.0).to_degrees();

        state.last_yaw_step = yaw_step;
        state.last_pitch_step = pitch_step;
    } else {
        state.last_yaw_step = 0.0;
        state.last_pitch_step = 0.0;
    }

    state.last_output_pose = Some(pose);
    (
        pose,
        containment_yaw_deg,
        containment_pitch_deg,
        dynamics_yaw_reduction_deg,
        dynamics_pitch_reduction_deg,
    )
}
'''

s = s.replace(impl_marker, "\n" + helper + impl_marker, 1)

produce_count_marker = "        let mut produce_count: u64 = 0;\n"
if s.count(produce_count_marker) != 1:
    raise SystemExit(
        f"expected exactly one produce_count marker, found {s.count(produce_count_marker)}"
    )
s = s.replace(
    produce_count_marker,
    produce_count_marker
    + "        let mut ball_signal_filter_state = BallSignalFilterState::default();\n",
    1,
)

produce_closure = "        let produce_one = |session: &mut StitchSession,\n"
if s.count(produce_closure) != 1:
    raise SystemExit(
        f"expected exactly one produce_one closure marker, found {s.count(produce_closure)}"
    )
s = s.replace(
    produce_closure,
    "        let mut produce_one = |session: &mut StitchSession,\n",
    1,
)

world_match = "            let world_state = match detection_result {\n"
if s.count(world_match) != 1:
    raise SystemExit(
        f"expected exactly one world_state match marker, found {s.count(world_match)}"
    )
s = s.replace(
    world_match,
    "            let mut world_state = match detection_result {\n",
    1,
)

world_done = "            };\n            let detections = session.detection.last_detections.clone();\n"
if s.count(world_done) != 1:
    raise SystemExit(
        f"expected exactly one world_state completion marker, found {s.count(world_done)}"
    )
s = s.replace(
    world_done,
    "            };\n"
    "            stabilize_world_ball(&mut world_state, &mut ball_signal_filter_state, *produce_count);\n"
    "            let detections = session.detection.last_detections.clone();\n",
    1,
)

state_marker = "        let mut panner_frame_idx: u64 = 0;\n"
if s.count(state_marker) != 1:
    raise SystemExit(
        f"expected exactly one panner_frame_idx marker, found {s.count(state_marker)}"
    )
s = s.replace(
    state_marker,
    state_marker
    + "        let mut ball_containment_guard_state = BallContainmentGuardState::default();\n",
    1,
)

render_call = (
    "self.render_buffered_frame(oldest, smoothed_pose, start, &ctx, on_progress)?;"
)
if s.count(render_call) != 2:
    raise SystemExit(
        f"expected exactly two buffered render calls, found {s.count(render_call)}"
    )

guarded_render = r'''let (
                    guarded_pose,
                    guard_yaw_deg,
                    guard_pitch_deg,
                    dynamics_yaw_deg,
                    dynamics_pitch_deg,
                ) = enforce_containment_and_dynamics(
                    smoothed_pose,
                    &oldest.world_state,
                    &mut ball_containment_guard_state,
                );
                if guard_yaw_deg > 0.001 || guard_pitch_deg > 0.001 {
                    log::info!(
                        "BALL_CONTAINMENT_GUARD frame={} yaw_correction_deg={:.3} pitch_correction_deg={:.3}",
                        self.frame_count,
                        guard_yaw_deg,
                        guard_pitch_deg,
                    );
                }
                if dynamics_yaw_deg > 0.001 || dynamics_pitch_deg > 0.001 {
                    log::info!(
                        "BALL_CAMERA_DYNAMICS frame={} yaw_reduction_deg={:.3} pitch_reduction_deg={:.3}",
                        self.frame_count,
                        dynamics_yaw_deg,
                        dynamics_pitch_deg,
                    );
                }
                self.render_buffered_frame(oldest, guarded_pose, start, &ctx, on_progress)?;'''

s = s.replace(render_call, guarded_render)
path.write_text(s)
print(
    "patched run_loop.rs: upstream trajectory-hysteresis ball filter + "
    "post-smoothing containment + acceleration-limited camera dynamics"
)
PY

python3 - "$BASE_SCRIPT" "$RECO_SHA" "$PATCHER" <<'PY'
from pathlib import Path
import sys

base = Path(sys.argv[1])
reco_sha = sys.argv[2]
patcher = sys.argv[3]
s = base.read_text()

needle = 'REPO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)\n'
if s.count(needle) != 1:
    raise SystemExit(f"expected exactly one REPO_SHA assignment, found {s.count(needle)}")

replacement = f'''git -C "$WORKDIR" checkout --detach "{reco_sha}" || fail "could not pin video-stitcher to experiment base SHA {reco_sha}" 3
python3 "{patcher}" "$WORKDIR/crates/reco-core/src/session/run_loop.rs" || fail "test-only ball stability source patch failed" 3
log "TEST-ONLY patch active: upstream ball trajectory hysteresis + containment + acceleration-limited camera dynamics; video-stitcher checkout remains {reco_sha} with a dirty working tree"
REPO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)
'''
s = s.replace(needle, replacement, 1)
base.write_text(s)
PY

chmod +x "$BASE_SCRIPT"
echo "TEST_ONLY_RECO_PATCH=lookahead_ball_containment_03 reco_base_sha=${RECO_SHA} innovation_gate_deg=3.0 switch_confirm_frames=18 safe_margin_deg=3.0 max_yaw_step_deg=0.75 yaw_accel_deg_per_frame2=0.08"
exec "$BASE_SCRIPT"
