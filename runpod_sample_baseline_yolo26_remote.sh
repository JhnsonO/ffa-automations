#!/usr/bin/env bash
# Runs ON a RunPod pod (uploaded + executed via SSH by
# oev-runpod-followcam.yml), AFTER runpod_bootstrap.sh has already
# produced a working --features cuda reco-cli build at
# /tmp/video-stitcher/target/release/reco and runpod_gpu_preflight.sh has
# already confirmed PREFLIGHT_RESULT=PASS on this pod.
#
# This script does NOT install any CUDA runtime, cuDNN, Rust toolchain,
# or rebuild reco-cli -- all of that is runpod_bootstrap.sh's job and is
# already done by the time this script runs. Redoing it here would mean
# building against the Vast script's CUDA-13-tuned install logic on top
# of this environment's already-proven CUDA 12.8 contract -- exactly the
# "wrong CUDA version on top of bootstrap's output" mistake this ticket
# exists to avoid (docs/ai-project-state.md, Task 4 spec).
#
# This is the OEV baseline sample-pack variant of runpod_followcam_remote.sh.
# Only the segment-selection block differs (no internal re-trimming --
# see below); calibrate, field_roi injection, the stitch invocation, and
# the tracking-acceptance check are reused verbatim from
# runpod_followcam_remote.sh (production follow-cam script), which in
# turn reused them verbatim from oev_followcam_test_remote.sh (the
# Vast.ai equivalent). No tracking/panner/threshold/render changes here
# -- this ticket is baseline measurement only.
#
# Linux shared-buffer GPU path is production-enabled as of 2026-08-15.
# Reco main f27cbb6d replaces the broken direct shared-VkImage path with
# CUDA-VMM shared VkBuffers -> Vulkan GPU buffer-to-texture copies.
# Verified on real GoPro NV12 and real P010/10-bit fixtures; CUDA detection
# remains GPU-resident. Do not re-add --no-zero-copy except as an explicit
# fallback/diagnostic comparison.
#
# Deliberately does NOT pass --allow-no-tracking: if Reco can't
# initialize tracking, the run must fail loudly, not silently produce a
# plain static stitch that looks like a follow-cam but isn't one.
#
# Expects, in /tmp/oev_run/:
#   left.mp4, right.mp4   (the exact pre-cut sample_NN_{left,right}_{30,60,180}s.mp4
#                          clip for the requested sample/duration, copied
#                          from the RunPod network volume by the workflow
#                          -- NOT full source clips, no further trimming
#                          is applied here)
#
# Produces, in /tmp/oev_run/:
#   segment.log     - sample_id/duration + measured left/right clip durations
#   calibrate.log   - reco calibrate output + field_roi injection
#   stitch.log       - reco stitch output
#   acceptance.log   - tracking + zero-copy acceptance check output
#   match.json        - calibration result (present even if stitch fails)
#   events.jsonl      - pipeline event trace (present if stitch ran)
#   followcam.mp4     - follow-cam output (only if stitch succeeds)
#
# Exit codes: 1=segment-selection failure, 2=calibrate/field_roi failure,
# 3=stitch command failure, 4=stitch reported success but output missing,
# 5=acceptance failure (tracking not confirmed active, OR -- RunPod-
# specific -- zero-copy/NVDEC/CUDAExecutionProvider evidence missing from
# stitch.log).

set -uo pipefail
cd /tmp/oev_run

RECO_BIN="/tmp/video-stitcher/target/release/reco"
if [ ! -x "$RECO_BIN" ]; then
  echo "FATAL: $RECO_BIN not found or not executable -- runpod_bootstrap.sh must run and succeed before this script" | tee -a segment.log
  exit 1
fi

# --- EXPERIMENTAL TEST BINARY (one-off: v4 hysteresis/micro-damping + relaxed
# ball-ROI reconstruction, at Johnson's request) ---
# Reconstructs the pre-regression "v4" camera behavior (the accepted quality
# reference, run 31913398625, which Johnson was happy with) on Reco
# f27cbb6d -- this PREDATES frame-stride testing, B2b bridged-authority,
# decaying-reacquire-grace, and the dead_zone/velocity-clamp retune
# (bb1d38a964). None of that later work is included here, by design: a lot
# of it existed to work around problems the fixed ROI polygon was actually
# causing, which was only diagnosed on 6 Sep. The v3+v4 ball-trajectory-
# hysteresis + containment + acceleration-limited-camera-dynamics +
# micro-damping patch to run_loop.rs is reconstructed verbatim from
# ffa-automations commits c91314ea (V3 patcher) + 8febf7a9 (V4 micro-damping
# delta) -- both since removed from main, reconstructed and re-verified
# byte-for-byte against those commits before use here. The ONE addition on
# top of the original v4 behavior: the ball-class ROI vertical-margin
# relaxation (+0.40) from commit 916529a61f, since we now know the fixed
# ROI polygon was rejecting genuine lofted-ball detections. Remove this
# block once the experiment is resolved (merge-or-discard).
EXPERIMENT_RECO_SHA="f27cbb6d0d65fcf9a11fb4d82d119ae214695318"
EXPERIMENT_DIR="/tmp/video-stitcher-v4-roi-experiment"
echo "Building v4-hysteresis + ROI-relaxed reco-cli at pinned SHA $EXPERIMENT_RECO_SHA..." | tee -a segment.log

if [ -d "$EXPERIMENT_DIR/.git" ]; then
  git -C "$EXPERIMENT_DIR" fetch origin || { echo "FATAL: git fetch failed for experimental checkout" | tee -a segment.log; exit 1; }
else
  git clone https://github.com/JhnsonO/video-stitcher.git "$EXPERIMENT_DIR" || { echo "FATAL: git clone failed for experimental checkout" | tee -a segment.log; exit 1; }
fi

git -C "$EXPERIMENT_DIR" checkout --detach "$EXPERIMENT_RECO_SHA" || { echo "FATAL: checkout of pinned experimental SHA failed" | tee -a segment.log; exit 1; }
ACTUAL_EXPERIMENT_SHA=$(git -C "$EXPERIMENT_DIR" rev-parse HEAD)
if [ "$ACTUAL_EXPERIMENT_SHA" != "$EXPERIMENT_RECO_SHA" ]; then
  echo "FATAL: experimental checkout resolved to $ACTUAL_EXPERIMENT_SHA, expected $EXPERIMENT_RECO_SHA" | tee -a segment.log
  exit 1
fi
echo "experiment_video-stitcher_source_sha=$ACTUAL_EXPERIMENT_SHA (v4-hysteresis+roi-relax base)" | tee -a segment.log

echo "Applying reconstructed v3+v4 ball-hysteresis/containment/camera-dynamics patch to run_loop.rs" | tee -a segment.log
python3 - "$EXPERIMENT_DIR/crates/reco-core/src/session/run_loop.rs" <<'RUNLOOP_PATCH'
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

    // v4 single-variable polish: adaptive damping only when the desired
    // correction is already small. Large/medium pans retain the exact v3
    // dynamics, so counters and clearances do not become sluggish.
    const MICRO_ZONE_DEG: f32 = 4.0;
    const MICRO_HOLD_DEG: f32 = 0.35;
    let error_deg = desired_delta.abs().to_degrees();
    let in_micro_zone = error_deg <= MICRO_ZONE_DEG;

    let (effective_delta, effective_max_step, effective_max_accel, effective_reversal_brake) =
        if in_micro_zone {
            let t = (error_deg / MICRO_ZONE_DEG).clamp(0.0, 1.0);
            let target_gain = 0.20 + 0.80 * t * t;
            let speed_gain = 0.32 + 0.68 * t;
            let accel_gain = 0.30 + 0.70 * t;
            let reversal_gain = 0.14 + 0.86 * t;
            (
                desired_delta * target_gain,
                max_step * speed_gain,
                max_accel * accel_gain,
                reversal_brake * reversal_gain,
            )
        } else {
            (desired_delta, max_step, max_accel, reversal_brake)
        };

    if in_micro_zone
        && error_deg <= MICRO_HOLD_DEG
        && previous_step.abs() <= effective_max_accel
    {
        return 0.0;
    }

    if previous_step * effective_delta < 0.0 && previous_step.abs() > 1.0e-6 {
        let brake = effective_reversal_brake.min(previous_step.abs());
        return previous_step - previous_step.signum() * brake;
    }

    let stopping_limited =
        (2.0 * effective_max_accel * effective_delta.abs())
            .sqrt()
            .min(effective_max_step);
    let target_step = effective_delta.signum()
        * stopping_limited.min(effective_delta.abs());

    let change = (target_step - previous_step)
        .clamp(-effective_max_accel, effective_max_accel);
    let mut step = previous_step + change;

    if step.signum() == effective_delta.signum()
        && step.abs() > effective_delta.abs()
    {
        step = effective_delta;
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
    "post-smoothing containment + acceleration-limited camera dynamics + adaptive 4deg micro damping"
)
RUNLOOP_PATCH
if [ $? -ne 0 ]; then
  echo "FATAL: v3/v4 run_loop.rs patch failed" | tee -a segment.log
  exit 1
fi

echo "Applying ball-ROI vertical-margin relaxation (+0.40) to roi_filter.rs" | tee -a segment.log
python3 - "$EXPERIMENT_DIR/crates/reco-autocam/src/roi_filter.rs" <<'ROI_FILTER_PATCH'
import sys
from pathlib import Path

path = Path(sys.argv[1])
s = path.read_text()

def repl(old, new, label):
    global s
    if old not in s:
        raise SystemExit(f'roi diag patch anchor missing: {label}')
    if s.count(old) != 1:
        raise SystemExit(f'roi diag patch anchor not unique: {label}')
    s = s.replace(old, new, 1)

repl(
    "pub struct RoiFilteredDetector {\n    inner: Box<dyn UnifiedDetector>,\n    roi: FieldRoi,\n    class_anchors: HashMap<u16, RoiAnchor>,\n    default_anchor: RoiAnchor,\n}",
    "pub struct RoiFilteredDetector {\n    inner: Box<dyn UnifiedDetector>,\n    roi: FieldRoi,\n    class_anchors: HashMap<u16, RoiAnchor>,\n    default_anchor: RoiAnchor,\n    // Diagnostic-only (OEV ball-ROI hypothesis test). Every other class's\n    // filtering path above is untouched; ball_class_id stays None unless\n    // with_ball_roi_diagnostics is called, so default behaviour is identical\n    // to production.\n    ball_class_id: Option<u16>,\n    ball_vertical_margin: f64,\n    roi_diag_frame_left: u64,\n    roi_diag_frame_right: u64,\n    roi_diag_fps: f32,\n    roi_diag_frame_stride: u32,\n}",
    "struct fields",
)

repl(
    "    pub fn new(inner: Box<dyn UnifiedDetector>, roi: FieldRoi) -> Self {\n        Self {\n            inner,\n            roi,\n            class_anchors: HashMap::new(),\n            default_anchor: RoiAnchor::Center,\n        }\n    }",
    "    pub fn new(inner: Box<dyn UnifiedDetector>, roi: FieldRoi) -> Self {\n        Self {\n            inner,\n            roi,\n            class_anchors: HashMap::new(),\n            default_anchor: RoiAnchor::Center,\n            ball_class_id: None,\n            ball_vertical_margin: 0.0,\n            roi_diag_frame_left: 0,\n            roi_diag_frame_right: 0,\n            roi_diag_fps: 60.0,\n            roi_diag_frame_stride: 1,\n        }\n    }",
    "constructor",
)

repl(
    "    /// Override the default anchor for classes without an explicit\n    /// [`with_class_anchor`](Self::with_class_anchor) entry. Chainable.\n    pub fn with_default_anchor(mut self, anchor: RoiAnchor) -> Self {\n        self.default_anchor = anchor;\n        self\n    }\n}",
    "    /// Override the default anchor for classes without an explicit\n    /// [`with_class_anchor`](Self::with_class_anchor) entry. Chainable.\n    pub fn with_default_anchor(mut self, anchor: RoiAnchor) -> Self {\n        self.default_anchor = anchor;\n        self\n    }\n\n    /// Diagnostic-only (OEV ball-ROI hypothesis test, hard case 134-139s).\n    /// Gives `ball_class_id` a generous vertical allowance above the field\n    /// polygon's top edge before the ROI test, so a lofted ball whose\n    /// center is genuinely above the pitch line is not rejected purely for\n    /// being airborne. No other class is affected: `filter_by_roi` below\n    /// still runs unmodified for every class except this one. `fps` /\n    /// `frame_stride` are used only to derive an approximate timestamp for\n    /// window-scoped `eprintln!` diagnostics, matching the same cadence\n    /// formula used elsewhere in the autocam pipeline. Chainable.\n    pub fn with_ball_roi_diagnostics(\n        mut self,\n        ball_class_id: u16,\n        vertical_margin: f64,\n        fps: f32,\n        frame_stride: u32,\n    ) -> Self {\n        self.ball_class_id = Some(ball_class_id);\n        self.ball_vertical_margin = vertical_margin;\n        self.roi_diag_fps = fps;\n        self.roi_diag_frame_stride = frame_stride;\n        self\n    }\n}",
    "builder method",
)

repl(
    "    fn detect(\n        &mut self,\n        camera: CameraId,\n        frame: &DetectorFrame<'_>,\n    ) -> Result<Vec<Detection>, DetectorError> {\n        let detections = self.inner.detect(camera, frame)?;\n        Ok(filter_by_roi(\n            detections,\n            &self.roi,\n            &self.class_anchors,\n            self.default_anchor,\n        ))\n    }",
    "    fn detect(\n        &mut self,\n        camera: CameraId,\n        frame: &DetectorFrame<'_>,\n    ) -> Result<Vec<Detection>, DetectorError> {\n        let detections = self.inner.detect(camera, frame)?;\n\n        // Diagnostic-only: everything below is a no-op (ball_dets always\n        // empty, kept == filter_by_roi(detections, ...)) unless\n        // with_ball_roi_diagnostics was called.\n        let frame_index = match camera {\n            CameraId::Left => {\n                let i = self.roi_diag_frame_left;\n                self.roi_diag_frame_left += 1;\n                i\n            }\n            CameraId::Right => {\n                let i = self.roi_diag_frame_right;\n                self.roi_diag_frame_right += 1;\n                i\n            }\n        };\n        let timestamp_ms = frame_index as f64 * 1000.0 * self.roi_diag_frame_stride as f64\n            / self.roi_diag_fps as f64;\n        let diag_window = (133000.0..=140000.0).contains(&timestamp_ms);\n\n        let (ball_dets, other_dets): (Vec<Detection>, Vec<Detection>) = detections\n            .into_iter()\n            .partition(|d| self.ball_class_id == Some(d.class_id));\n\n        let mut kept = filter_by_roi(other_dets, &self.roi, &self.class_anchors, self.default_anchor);\n\n        let polygon: &[[f64; 2]] = match camera {\n            CameraId::Left => &self.roi.left,\n            CameraId::Right => &self.roi.right,\n        };\n\n        if polygon.len() < 3 {\n            kept.extend(ball_dets);\n        } else {\n            for d in ball_dets {\n                let cx = d.center_x as f64;\n                let cy = d.center_y as f64;\n                let adj_cy = cy + self.ball_vertical_margin;\n                let roi_pass = point_in_polygon([cx, adj_cy], polygon);\n                if diag_window {\n                    eprintln!(\n                        \"OEV_ROI_DIAG cam={:?} t_ms={:.3} cx={:.4} cy={:.4} adj_cy={:.4} roi_pass={}\",\n                        camera, timestamp_ms, cx, cy, adj_cy, roi_pass\n                    );\n                }\n                if roi_pass {\n                    kept.push(d);\n                }\n            }\n        }\n\n        Ok(kept)\n    }",
    "detect() ball diagnostics",
)

path.write_text(s)
ROI_FILTER_PATCH
if [ $? -ne 0 ]; then
  echo "FATAL: roi_filter.rs relaxation patch failed" | tee -a segment.log
  exit 1
fi

echo "Applying ball-ROI vertical-margin relaxation (+0.40) to lib.rs" | tee -a segment.log
python3 - "$EXPERIMENT_DIR/crates/reco-autocam/src/lib.rs" <<'ROI_LIB_PATCH'
import sys
from pathlib import Path

path = Path(sys.argv[1])
s = path.read_text()

def repl(old, new, label):
    global s
    if old not in s:
        raise SystemExit(f'lib.rs diag patch anchor missing: {label}')
    if s.count(old) != 1:
        raise SystemExit(f'lib.rs diag patch anchor not unique: {label}')
    s = s.replace(old, new, 1)

repl(
    'let person_id_for_roi = resolve_or(&class_names, &["person"], 0);',
    'let person_id_for_roi = resolve_or(&class_names, &["person"], 0);\n    // Diagnostic-only (OEV ball-ROI hypothesis test). Resolved the same way\n    // as person_id_for_roi above; falls back to COCO\'s "sports ball" id (32)\n    // if the model\'s label list doesn\'t have an exact match.\n    let ball_id_for_roi = resolve_or(&class_names, &["ball", "sports ball", "football"], 32);',
    "ball id resolution",
)

repl(
    "    let wrap_with_roi = |inner: Box<dyn reco_core::detect::detector::UnifiedDetector>,\n                         roi: reco_core::calibration::FieldRoi|\n     -> Box<dyn reco_core::detect::detector::UnifiedDetector> {\n        Box::new(\n            RoiFilteredDetector::new(inner, roi)\n                .with_class_anchor(person_id_for_roi, RoiAnchor::Bottom),\n        )\n    };",
    "    let wrap_with_roi = |inner: Box<dyn reco_core::detect::detector::UnifiedDetector>,\n                         roi: reco_core::calibration::FieldRoi|\n     -> Box<dyn reco_core::detect::detector::UnifiedDetector> {\n        Box::new(\n            RoiFilteredDetector::new(inner, roi)\n                .with_class_anchor(person_id_for_roi, RoiAnchor::Bottom)\n                // Diagnostic-only: deliberately generous vertical margin\n                // (0.40, normalized) to test the \"lofted ball rejected by\n                // ROI\" hypothesis. Not a tuned production value.\n                .with_ball_roi_diagnostics(ball_id_for_roi, 0.40, fps, 1),\n        )\n    };",
    "wrap_with_roi wiring",
)

path.write_text(s)
ROI_LIB_PATCH
if [ $? -ne 0 ]; then
  echo "FATAL: lib.rs relaxation patch failed" | tee -a segment.log
  exit 1
fi

( cd "$EXPERIMENT_DIR" && { source "$HOME/.cargo/env" 2>/dev/null || true; } && CARGO_TARGET_DIR="$EXPERIMENT_DIR/target" cargo build --release -p reco-cli --features cuda 2>&1 | tee -a segment.log )
EXPERIMENT_BUILD_EXIT=${PIPESTATUS[0]}
if [ "$EXPERIMENT_BUILD_EXIT" -ne 0 ]; then
  echo "FATAL: experimental cargo build failed (exit $EXPERIMENT_BUILD_EXIT)" | tee -a segment.log
  exit 1
fi

EXPERIMENT_BIN="$EXPERIMENT_DIR/target/release/reco"
if [ ! -x "$EXPERIMENT_BIN" ]; then
  echo "FATAL: $EXPERIMENT_BIN not found or not executable after experimental build" | tee -a segment.log
  exit 1
fi

# Log the actual BINARY's hash, not just the source commit it was built
# from -- proves what ran, independent of source-checkout correctness.
EXPERIMENT_BIN_SHA256=$(sha256sum "$EXPERIMENT_BIN" | awk '{print $1}')
echo "experiment_reco_binary_sha256=$EXPERIMENT_BIN_SHA256" | tee -a segment.log
echo "Using EXPERIMENTAL binary (source $ACTUAL_EXPERIMENT_SHA, binary sha256 $EXPERIMENT_BIN_SHA256) for this test run -- NOT the gated production binary." | tee -a segment.log

RECO_BIN="$EXPERIMENT_BIN"

# --- Fixed-duration sample clip, NO internal re-trimming. ---
# This is the baseline sample-pack test script: unlike
# runpod_followcam_remote.sh (which expects the FULL original source
# clips and picks its own random 15-20s sub-window), this script expects
# left.mp4/right.mp4 to ALREADY be the exact pre-cut sample clip for the
# requested sample_id/duration (staged on the RunPod network volume by
# oev_populate_volume_samples_remote.sh, copied into place by the
# workflow before this script runs). The full clip IS the test window --
# no further trimming, no random start selection.
echo "=== Baseline sample clip (no re-trimming) ===" | tee segment.log
SAMPLE_ID_LOG="${SAMPLE_ID:-unknown}"
DURATION_S_LOG="${DURATION_S:-unknown}"
LEFT_SOURCE_NAME="${LEFT_CLIP:-left.mp4}"
RIGHT_SOURCE_NAME="${RIGHT_CLIP:-right.mp4}"
if [ ! -s left.mp4 ] || [ ! -s right.mp4 ]; then
  echo "FATAL: left.mp4/right.mp4 missing/empty -- expected pre-staged sample clips, not full sources" | tee -a segment.log
  exit 1
fi
LEFT_CLIP_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 left.mp4)
RIGHT_CLIP_DURATION=$(ffprobe -v error -show_entries format=duration -of csv=p=0 right.mp4)
LEFT_CLIP_DURATION_INT=$(printf '%.0f' "${LEFT_CLIP_DURATION:-0}")
RIGHT_CLIP_DURATION_INT=$(printf '%.0f' "${RIGHT_CLIP_DURATION:-0}")
if [ "$LEFT_CLIP_DURATION_INT" -le 0 ] || [ "$RIGHT_CLIP_DURATION_INT" -le 0 ]; then
  echo "FATAL: could not determine duration of left.mp4/right.mp4 (left=${LEFT_CLIP_DURATION_INT}s right=${RIGHT_CLIP_DURATION_INT}s)" | tee -a segment.log
  exit 1
fi
{
  echo "Sample: ${SAMPLE_ID_LOG} (requested duration ${DURATION_S_LOG}s)"
  echo "Left clip:  ${LEFT_SOURCE_NAME} (measured duration ${LEFT_CLIP_DURATION_INT}s)"
  echo "Right clip: ${RIGHT_SOURCE_NAME} (measured duration ${RIGHT_CLIP_DURATION_INT}s)"
  echo "No re-trimming applied -- full pre-staged sample clip is the test segment."
} | tee -a segment.log

# --- YOLO model: YOLO26 A/B variant. Unlike the YOLOv8n baseline script,
# this NEVER exports fresh -- it requires the model already staged on the
# attached network volume (built by oev-populate-volume.yml, confirmed
# present on EU-RO-1 volume 0hta9vhuue at reco_sha=53fe10f548d5767ad94ef
# 66aeaedf2d8c7161f27, run 31747259271). Hard-fails if missing rather than
# silently falling back to YOLOv8n, so a missing-model run can never be
# mistaken for a real YOLO26 result. ---
: "${YOLO26_VARIANT:=yolo26m}"
YOLO_MODEL="/runpod-volume/oev-runtime/models/${YOLO26_VARIANT}.onnx"
if [ ! -s "$YOLO_MODEL" ]; then
  echo "FATAL: ${YOLO_MODEL} not found on attached volume -- run oev-populate-volume.yml for this datacenter first, no fresh-export fallback for YOLO26" | tee -a segment.log
  exit 1
fi
echo "Using YOLO model: $YOLO_MODEL (YOLO26 A/B variant, no re-export)" | tee -a segment.log
cp "$YOLO_MODEL" "/tmp/oev_run/${YOLO26_VARIANT}.onnx"

echo "=== calibrate.log: reco calibrate ===" | tee calibrate.log
# Same pinned Hero10 Wide profile as the Vast follow-cam script (both
# cameras are GoPro Hero 10, Wide mode; auto-detect is known to fail on
# this footage's telemetry -- see docs/ai-project-state.md).
LENS_PROFILE_URL="https://raw.githubusercontent.com/gyroflow/lens_profiles/main/GoPro/GoPro_HERO10%20Black_Wide_16by9.json"
echo "Downloading lens profile: $LENS_PROFILE_URL" | tee -a calibrate.log
curl -fsSL "$LENS_PROFILE_URL" -o hero10_wide_16by9.json
if [ ! -s hero10_wide_16by9.json ]; then
  echo "FATAL: failed to download lens profile from $LENS_PROFILE_URL" | tee -a calibrate.log
  exit 2
fi
stdbuf -oL -eL "$RECO_BIN" calibrate left.mp4 right.mp4 \
  --left-profile hero10_wide_16by9.json \
  --right-profile hero10_wide_16by9.json \
  -o match.json 2>&1 | tee -a calibrate.log
calibrate_rc=${PIPESTATUS[0]}
if [ "$calibrate_rc" -ne 0 ]; then
  echo "FATAL: reco calibrate failed (exit $calibrate_rc), see calibrate.log" | tee -a calibrate.log
  exit 2
fi
if [ ! -f match.json ]; then
  echo "FATAL: calibrate reported success but match.json missing" | tee -a calibrate.log
  exit 2
fi
echo "Calibrate OK: match.json written" | tee -a calibrate.log

# Fixed St Margaret's field ROI (verbatim from oev_followcam_test_remote.sh
# -- same prototype polygon Johnson marked on the calibrate-stills
# screenshots for this exact clip pair/camera setup). Injected into
# match.json after calibrate, before stitch, so reco stitch's already-
# existing field_roi auto-load filters out detections from the
# neighbouring pitch.
echo "Injecting St Margaret's field_roi into match.json" | tee -a calibrate.log
python3 - <<'PYROI'
import json

with open("match.json") as f:
    match = json.load(f)

match["field_roi"] = {
    "left": [
        [0.1227, 0.9611],
        [0.0573, 0.6846],
        [0.1802, 0.6285],
        [0.2645, 0.5769],
        [0.4382, 0.4864],
        [0.4988, 0.4658],
        [0.5942, 0.4474],
        [0.7835, 0.4175],
        [0.9285, 0.3785],
        [1.0000, 1.0000],
        [0.1227, 1.0000],
    ],
    "right": [
        [0.0391, 0.4206],
        [0.0818, 0.4101],
        [0.1839, 0.4070],
        [0.2783, 0.4070],
        [0.3448, 0.4083],
        [0.4100, 0.4161],
        [0.4684, 0.4319],
        [0.6239, 0.4801],
        [0.7368, 0.5200],
        [0.7980, 0.5465],
        [0.7454, 0.9011],
        [0.7454, 1.0000],
        [0.0000, 1.0000],
    ],
}

with open("match.json", "w") as f:
    json.dump(match, f, indent=2)

assert len(match["field_roi"]["left"]) == 11
assert len(match["field_roi"]["right"]) == 13
print("field_roi injected: left=%d pts, right=%d pts" % (
    len(match["field_roi"]["left"]), len(match["field_roi"]["right"])))
PYROI
if [ $? -ne 0 ]; then
  echo "FATAL: field_roi injection into match.json failed" | tee -a calibrate.log
  exit 2
fi
if ! python3 -c "
import json, sys
m = json.load(open('match.json'))
roi = m.get('field_roi')
assert roi and isinstance(roi.get('left'), list) and len(roi['left']) > 0, 'field_roi.left missing/empty'
assert isinstance(roi.get('right'), list) and len(roi['right']) > 0, 'field_roi.right missing/empty'
" 2>>calibrate.log; then
  echo "FATAL: match.json field_roi validation failed after injection" | tee -a calibrate.log
  exit 2
fi
echo "field_roi validated in match.json (left/right polygons present)" | tee -a calibrate.log

echo "=== stitch.log: reco stitch (field follow-cam, l-shape, shared-buffer GPU path) ===" | tee stitch.log
# Same flag set agreed with Johnson as the Vast script: normal
# perspective (l-shape, default) projection, NOT cylindrical.
# --detection-interval 1 (no frame-skipping, out of scope for this
# ticket). Deliberately NO --allow-no-tracking: a tracking-init failure
# must fail this run loudly, not silently degrade to a static stitch.
# Shared-buffer zero-copy is intentionally enabled here (no --no-zero-copy).
# The merged path was hardware-verified on RTX 4090 and L4 without the old
# green/corrupt direct-shared-VkImage failure.
: "${LOOKAHEAD:=1.5}"
STITCH_ARGS=(stitch left.mp4 right.mp4 -c match.json -o followcam.mp4
  --model "${YOLO26_VARIANT}.onnx"
  --tracking field
  --panner-preset broadcast
  --lookahead "${LOOKAHEAD}"
  --detection-interval 1
  --events events.jsonl
  --width 1920 --height 1080)

# --- Measurement-only overlay: optional cluster_alpha override on top of
# the broadcast preset. Blank/unset CLUSTER_ALPHA_OVERRIDE (the default)
# leaves this block entirely inert -- STITCH_ARGS is unchanged and
# behavior is byte-identical to the pre-existing baseline. Only alpha is
# overridden; every other broadcast parameter (dead_zone_rad, lead_gain,
# lead_alpha, lookahead_reactivity, ball_weight) stays at preset default.
if [ -n "${CLUSTER_ALPHA_OVERRIDE:-}" ]; then
  echo "{\"cluster_alpha\": ${CLUSTER_ALPHA_OVERRIDE}}" > panner_overlay.json
  STITCH_ARGS+=(--panner-config panner_overlay.json)
  echo "Panner overlay active: cluster_alpha=${CLUSTER_ALPHA_OVERRIDE} (panner_overlay.json)" | tee -a stitch.log
fi

echo "reco stitch args: ${STITCH_ARGS[*]}" | tee -a stitch.log
stdbuf -oL -eL "$RECO_BIN" "${STITCH_ARGS[@]}" 2>&1 | tee -a stitch.log
stitch_rc=${PIPESTATUS[0]}
if [ "$stitch_rc" -ne 0 ]; then
  echo "FATAL: reco stitch failed (exit $stitch_rc), see stitch.log (match.json/${YOLO26_VARIANT}.onnx are still valid)" | tee -a stitch.log
  exit 3
fi
if [ ! -f followcam.mp4 ]; then
  echo "FATAL: stitch reported success but followcam.mp4 missing" | tee -a stitch.log
  exit 4
fi
echo "Stitch OK: followcam.mp4 written" | tee -a stitch.log

echo "=== acceptance.log: verifying AI-driven follow-cam (not a static stitch) ===" | tee acceptance.log
python3 - <<'PY' 2>&1 | tee -a acceptance.log
import json, sys

accept_fail = False

try:
    stitch_log = open('stitch.log').read()
except FileNotFoundError:
    print("FAIL: stitch.log missing")
    sys.exit(1)

if "Autocam: tracking enabled" not in stitch_log:
    print("FAIL: 'Autocam: tracking enabled' not found in stitch.log -- tracking did not initialize")
    accept_fail = True
else:
    print("OK: 'Autocam: tracking enabled' found in stitch.log")

try:
    lines = open('events.jsonl').read().splitlines()
except FileNotFoundError:
    print("FAIL: events.jsonl missing")
    accept_fail = True
    lines = []

detections_with_hits = 0
pan_yaws = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        ev = json.loads(line)
    except json.JSONDecodeError:
        continue
    if ev.get('kind') == 'detections_raw' and ev.get('detections'):
        detections_with_hits += 1
    if ev.get('kind') == 'pan_decision':
        pose = ev.get('pose') or {}
        yaw = pose.get('yaw')
        if yaw is not None:
            pan_yaws.append(yaw)

print(f"Total event lines: {len(lines)}")
print(f"detections_raw events with >=1 detection: {detections_with_hits}")
print(f"pan_decision events with a yaw value: {len(pan_yaws)}")

if detections_with_hits == 0:
    print("FAIL: no detections_raw event contained any detection -- detector produced nothing")
    accept_fail = True

if len(pan_yaws) < 2:
    print("FAIL: fewer than 2 pan_decision events with a pose -- can't judge camera movement")
    accept_fail = True
else:
    yaw_spread = max(pan_yaws) - min(pan_yaws)
    print(f"pan_decision yaw spread (radians): {yaw_spread}")
    if yaw_spread < 1e-4:
        print("FAIL: pan_decision yaw never changes -- camera is static, not AI-driven")
        accept_fail = True
    else:
        print("OK: pan_decision yaw shows real movement")

if accept_fail:
    sys.exit(1)
print("ACCEPTANCE (tracking): PASS")
PY
tracking_accept_rc=${PIPESTATUS[0]}
if [ "$tracking_accept_rc" -ne 0 ]; then
  echo "FATAL: follow-cam tracking acceptance check FAILED -- see acceptance.log. followcam.mp4 exists but is NOT confirmed AI-driven." | tee -a acceptance.log
  exit 5
fi

# --- RunPod-specific: zero-copy evidence check. Only runs when
# --no-zero-copy is NOT in STITCH_ARGS -- i.e. only when zero-copy is
# actually expected to be active. As of 2026-08-12, --no-zero-copy IS in
# STITCH_ARGS (interim production setting, see file header), so this
# whole block is skipped on this path; it's left in place, unmodified,
# for whenever the reco-cli zero-copy bug is fixed and --no-zero-copy is
# removed again -- do not delete this block to "clean up" while
# --no-zero-copy is the active setting. ---
if printf '%s\n' "${STITCH_ARGS[@]}" | grep -qx -- '--no-zero-copy'; then
  echo "=== --no-zero-copy is active this run -- skipping zero-copy evidence check (expected, not applicable) ===" | tee -a acceptance.log
else
  echo "=== Verifying full zero-copy path actually engaged (RunPod-specific, no Vast equivalent) ===" | tee -a acceptance.log
  zero_copy_fail=0
  if grep -qiE 'zero-copy|zero copy' stitch.log; then
    echo "OK: zero-copy log line found" | tee -a acceptance.log
  else
    echo "FAIL: no zero-copy log line found in stitch.log" | tee -a acceptance.log
    zero_copy_fail=1
  fi
  if grep -qiE 'NVDEC.*CUDA|NVDEC \(CUDA\)|cuvid' stitch.log; then
    echo "OK: GPU decode (NVDEC/CUDA) log line found" | tee -a acceptance.log
  else
    echo "FAIL: no NVDEC/CUDA decode log line found in stitch.log" | tee -a acceptance.log
    zero_copy_fail=1
  fi
  if grep -qE "No execution providers from session options registered successfully" stitch.log; then
    echo "FAIL: CUDA EP fallback warning present in stitch.log -- detection likely ran on CPU" | tee -a acceptance.log
    zero_copy_fail=1
  elif grep -qiE 'CUDAExecutionProvider' stitch.log; then
    echo "OK: CUDAExecutionProvider log line found, no fallback warning" | tee -a acceptance.log
  else
    echo "FAIL: no CUDAExecutionProvider log line found in stitch.log" | tee -a acceptance.log
    zero_copy_fail=1
  fi
  if [ "$zero_copy_fail" -ne 0 ]; then
    echo "FATAL: zero-copy acceptance check FAILED -- see acceptance.log. Tracking passed but full zero-copy is NOT confirmed active on this run." | tee -a acceptance.log
    exit 5
  fi
  echo "Acceptance OK: zero-copy (GPU decode + CUDA inference) confirmed engaged" | tee -a acceptance.log
fi
echo "Acceptance OK: AI tracking confirmed active with real detections + camera movement" | tee -a acceptance.log

echo "=== All stages completed ==="
