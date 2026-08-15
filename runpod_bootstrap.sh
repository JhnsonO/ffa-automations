#!/usr/bin/env bash
# TEST-ONLY bootstrap wrapper for experiment/lookahead-ball-containment-01.
# It fetches the exact baseline bootstrap, pins Reco to the proven main SHA,
# and patches ONLY the final buffered render pose after centered smoothing.
#
# Experiment v2 specifically targets the snapping seen in run 31906046913:
#   - bridge short Tracking -> Coasting gaps instead of dropping containment
#   - require 3 consistent frames before trusting a large ball-position jump
#   - cap final yaw slew at 0.75 deg/frame (~45 deg/s at 60fps), safely above
#     the intended small-pitch panner speed but far below the observed snaps
#   - cap final pitch slew at 0.50 deg/frame
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
/// TEST-ONLY experiment state injected by ffa-automations.
/// Keeps the final containment guard continuous across tracker flicker without
/// changing the production tracker or panner implementations.
#[derive(Default)]
struct BallContainmentGuardState {
    last_trusted_ball: Option<(f32, f32)>,
    pending_jump: Option<(f32, f32, u8)>,
    coast_frames: u32,
    missing_frames: u32,
    last_output_pose: Option<crate::detect::director::ViewportPosition>,
}

fn guard_angle_delta(target: f32, current: f32) -> f32 {
    let raw = target - current;
    raw.sin().atan2(raw.cos())
}

fn guard_ball_distance_deg(a: (f32, f32), b: (f32, f32)) -> f32 {
    let yaw = guard_angle_delta(a.0, b.0).to_degrees();
    let pitch = (a.1 - b.1).to_degrees();
    yaw.hypot(pitch)
}

/// Choose a stable ball target for the hard framing guard.
///
/// - Fresh Tracking measurements that move plausibly are accepted immediately.
/// - A >12 degree one-frame jump is treated as a possible false reacquisition
///   and must be seen in roughly the same place for 3 consecutive frames.
/// - Coasting keeps the last trusted target for up to 18 frames (~0.3s @ 60fps),
///   preventing the Tracking/Coasting on/off guard flicker seen in the v1 trace.
/// - Lost/None gets a shorter 8-frame grace period; after that containment
///   releases, but the final slew limiter still prevents a snap back.
fn stable_guard_ball_target(
    world: &crate::detect::tracker::WorldState,
    state: &mut BallContainmentGuardState,
) -> Option<(f32, f32)> {
    const LARGE_JUMP_DEG: f32 = 12.0;
    const PENDING_MATCH_DEG: f32 = 6.0;
    const PENDING_CONFIRM_FRAMES: u8 = 3;
    const MAX_COAST_FRAMES: u32 = 18;
    const MAX_MISSING_HOLD_FRAMES: u32 = 8;

    match world.ball.as_ref() {
        Some(ball)
            if matches!(ball.state, crate::detect::tracker::TrackState::Tracking)
                && ball.yaw.is_finite()
                && ball.pitch.is_finite() =>
        {
            state.coast_frames = 0;
            state.missing_frames = 0;
            let candidate = (ball.yaw, ball.pitch);

            match state.last_trusted_ball {
                None => {
                    state.last_trusted_ball = Some(candidate);
                    state.pending_jump = None;
                    Some(candidate)
                }
                Some(last) => {
                    let jump_deg = guard_ball_distance_deg(candidate, last);
                    if jump_deg <= LARGE_JUMP_DEG {
                        state.last_trusted_ball = Some(candidate);
                        state.pending_jump = None;
                        Some(candidate)
                    } else {
                        let next_count = match state.pending_jump {
                            Some((py, pp, count))
                                if guard_ball_distance_deg(candidate, (py, pp))
                                    <= PENDING_MATCH_DEG =>
                            {
                                count.saturating_add(1)
                            }
                            _ => 1,
                        };
                        state.pending_jump = Some((candidate.0, candidate.1, next_count));
                        if next_count >= PENDING_CONFIRM_FRAMES {
                            log::info!(
                                "BALL_GUARD_REACQUIRE_ACCEPT jump_deg={:.3} confirmations={}",
                                jump_deg,
                                next_count,
                            );
                            state.last_trusted_ball = Some(candidate);
                            state.pending_jump = None;
                            Some(candidate)
                        } else {
                            log::info!(
                                "BALL_GUARD_REACQUIRE_HOLD jump_deg={:.3} confirmation={}/{} confidence={:.3}",
                                jump_deg,
                                next_count,
                                PENDING_CONFIRM_FRAMES,
                                ball.confidence,
                            );
                            Some(last)
                        }
                    }
                }
            }
        }
        Some(ball) if matches!(ball.state, crate::detect::tracker::TrackState::Coasting) => {
            state.coast_frames = state.coast_frames.saturating_add(1);
            state.missing_frames = 0;
            state.pending_jump = None;
            if state.coast_frames <= MAX_COAST_FRAMES {
                state.last_trusted_ball.or_else(|| {
                    if ball.yaw.is_finite() && ball.pitch.is_finite() {
                        Some((ball.yaw, ball.pitch))
                    } else {
                        None
                    }
                })
            } else {
                None
            }
        }
        _ => {
            state.coast_frames = 0;
            state.missing_frames = state.missing_frames.saturating_add(1);
            state.pending_jump = None;
            if state.missing_frames <= MAX_MISSING_HOLD_FRAMES {
                state.last_trusted_ball
            } else {
                None
            }
        }
    }
}

/// Final test-only camera guard.
///
/// Normal panner + centered lookahead smoothing still choose composition. This
/// applies only the minimum translation required to keep the stable ball target
/// inside a 3-degree safe margin, then slew-limits the FINAL requested pose so
/// tracker state changes or false reacquisitions cannot create one-frame snaps.
///
/// Returns (final_pose, containment_yaw_correction_deg,
/// containment_pitch_correction_deg, anti_snap_yaw_reduction_deg,
/// anti_snap_pitch_reduction_deg).
fn enforce_stable_ball_containment(
    mut pose: crate::detect::director::ViewportPosition,
    world: &crate::detect::tracker::WorldState,
    state: &mut BallContainmentGuardState,
) -> (crate::detect::director::ViewportPosition, f32, f32, f32, f32) {
    const ASPECT: f32 = 16.0 / 9.0;
    const SAFE_MARGIN_DEG: f32 = 3.0;
    const MAX_YAW_STEP_DEG: f32 = 0.75;
    const MAX_PITCH_STEP_DEG: f32 = 0.50;

    let original_yaw = pose.yaw;
    let original_pitch = pose.pitch;

    if let (Some(fov_deg), Some((ball_yaw, ball_pitch))) =
        (pose.fov_degrees, stable_guard_ball_target(world, state))
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

            let yaw_delta = guard_angle_delta(ball_yaw, pose.yaw);
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

    let containment_yaw_deg = guard_angle_delta(pose.yaw, original_yaw).abs().to_degrees();
    let containment_pitch_deg = (pose.pitch - original_pitch).abs().to_degrees();

    let desired_yaw = pose.yaw;
    let desired_pitch = pose.pitch;
    let mut anti_snap_yaw_reduction_deg = 0.0;
    let mut anti_snap_pitch_reduction_deg = 0.0;

    if let Some(prev) = state.last_output_pose {
        let yaw_delta = guard_angle_delta(desired_yaw, prev.yaw);
        let max_yaw_step = MAX_YAW_STEP_DEG.to_radians();
        if yaw_delta.abs() > max_yaw_step {
            pose.yaw = prev.yaw + yaw_delta.clamp(-max_yaw_step, max_yaw_step);
            anti_snap_yaw_reduction_deg =
                (yaw_delta.abs() - max_yaw_step).max(0.0).to_degrees();
        }

        let pitch_delta = desired_pitch - prev.pitch;
        let max_pitch_step = MAX_PITCH_STEP_DEG.to_radians();
        if pitch_delta.abs() > max_pitch_step {
            pose.pitch = prev.pitch + pitch_delta.clamp(-max_pitch_step, max_pitch_step);
            anti_snap_pitch_reduction_deg =
                (pitch_delta.abs() - max_pitch_step).max(0.0).to_degrees();
        }
    }

    state.last_output_pose = Some(pose);
    (
        pose,
        containment_yaw_deg,
        containment_pitch_deg,
        anti_snap_yaw_reduction_deg,
        anti_snap_pitch_reduction_deg,
    )
}
'''

s = s.replace(impl_marker, "\n" + helper + impl_marker, 1)

state_marker = "        let mut panner_frame_idx: u64 = 0;\n"
if s.count(state_marker) != 1:
    raise SystemExit(
        f"expected exactly one panner_frame_idx marker, found {s.count(state_marker)}"
    )
s = s.replace(
    state_marker,
    state_marker + "        let mut ball_containment_guard_state = BallContainmentGuardState::default();\n",
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
                    anti_snap_yaw_deg,
                    anti_snap_pitch_deg,
                ) = enforce_stable_ball_containment(
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
                if anti_snap_yaw_deg > 0.001 || anti_snap_pitch_deg > 0.001 {
                    log::info!(
                        "BALL_ANTI_SNAP frame={} yaw_reduction_deg={:.3} pitch_reduction_deg={:.3}",
                        self.frame_count,
                        anti_snap_yaw_deg,
                        anti_snap_pitch_deg,
                    );
                }
                self.render_buffered_frame(oldest, guarded_pose, start, &ctx, on_progress)?;'''

s = s.replace(render_call, guarded_render)
path.write_text(s)
print(
    "patched run_loop.rs: stable coasting bridge + 3-frame jump confirmation + "
    "post-smoothing containment + final anti-snap slew limiter inserted at both render sites"
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
python3 "{patcher}" "$WORKDIR/crates/reco-core/src/session/run_loop.rs" || fail "test-only stable ball-containment source patch failed" 3
log "TEST-ONLY patch active: coasting bridge + jump confirmation + post-smoothing containment + anti-snap slew limiter; video-stitcher checkout remains {reco_sha} with a dirty working tree"
REPO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)
'''
s = s.replace(needle, replacement, 1)
base.write_text(s)
PY

chmod +x "$BASE_SCRIPT"
echo "TEST_ONLY_RECO_PATCH=lookahead_ball_containment_02 reco_base_sha=${RECO_SHA} safe_margin_deg=3.0 coast_hold_frames=18 reacquire_confirm_frames=3 max_yaw_step_deg=0.75 max_pitch_step_deg=0.50"
exec "$BASE_SCRIPT"
