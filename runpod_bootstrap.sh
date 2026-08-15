#!/usr/bin/env bash
# TEST-ONLY v4 wrapper for experiment/lookahead-ball-containment-01.
#
# Single-variable follow-up to the successful v3 render:
#   - preserve v3 upstream ball trajectory hysteresis unchanged
#   - preserve 1.5s lookahead / containment / global speed+accel limits unchanged
#   - only damp small (<4deg) final-camera corrections adaptively
#   - tiny reversals brake gently before changing direction instead of chattering
#
# Fetches the exact v3 experiment wrapper by commit, patches only its
# camera_axis_step helper, then runs the normal v3 bootstrap path.
set -euo pipefail

V3_FFA_SHA="c91314eaca8e2cbb0b6813f6e8204e4da23f408c"
V3_SCRIPT="/tmp/runpod_bootstrap_v3_exact.sh"

curl -fsSL \
  "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${V3_FFA_SHA}/runpod_bootstrap.sh" \
  -o "$V3_SCRIPT"
test -s "$V3_SCRIPT"

python3 - "$V3_SCRIPT" <<'PY'
from pathlib import Path
import sys

p = Path(sys.argv[1])
s = p.read_text()

start_marker = "fn camera_axis_step(\n"
end_marker = "\n}\n\n/// Apply minimum ball containment, then smooth the final camera *dynamics*."
if s.count(start_marker) != 1 or s.count(end_marker) != 1:
    raise SystemExit(
        f"expected one camera_axis_step block; start={s.count(start_marker)} end={s.count(end_marker)}"
    )
start = s.index(start_marker)
end = s.index(end_marker, start) + 2

new_fn = r'''fn camera_axis_step(
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
            // Continuous blend: no hard behavior cliff at 4 degrees.
            // Near center, only ~20% of a tiny target wobble is chased and
            // acceleration/reversal braking are deliberately gentle. The
            // factors smoothly return to 1.0 at the edge of the micro zone.
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

    // If we're already essentially centered and nearly stopped, hold rather
    // than hunting sub-degree noise. If still moving, the normal braking path
    // below eases us to rest instead of creating an abrupt stop.
    if in_micro_zone
        && error_deg <= MICRO_HOLD_DEG
        && previous_step.abs() <= effective_max_accel
    {
        return 0.0;
    }

    // A small-zone direction flip first bleeds existing velocity gently. This
    // acts as temporal hysteresis: a one/two-frame reversal usually vanishes
    // before the camera has actually committed in the opposite direction.
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
}'''

s = s[:start] + new_fn + s[end:]

replacements = {
    "Experiment v3 targets the remaining bounce / wrong-way excursions seen in":
        "Experiment v4 preserves the v3 trajectory fix and adds micro-movement damping after",
    "post-smoothing containment + acceleration-limited camera dynamics":
        "post-smoothing containment + acceleration-limited camera dynamics + adaptive 4deg micro damping",
    "TEST-ONLY patch active: upstream ball trajectory hysteresis + containment + acceleration-limited camera dynamics;":
        "TEST-ONLY patch active: upstream ball trajectory hysteresis + containment + acceleration-limited camera dynamics + adaptive 4deg micro damping;",
    "TEST_ONLY_RECO_PATCH=lookahead_ball_containment_03 reco_base_sha=${RECO_SHA} innovation_gate_deg=3.0 switch_confirm_frames=18 safe_margin_deg=3.0 max_yaw_step_deg=0.75 yaw_accel_deg_per_frame2=0.08":
        "TEST_ONLY_RECO_PATCH=lookahead_ball_containment_04_micro_damping reco_base_sha=${RECO_SHA} innovation_gate_deg=3.0 switch_confirm_frames=18 safe_margin_deg=3.0 max_yaw_step_deg=0.75 yaw_accel_deg_per_frame2=0.08 micro_zone_deg=4.0 micro_hold_deg=0.35",
}
for old, new in replacements.items():
    if old not in s:
        raise SystemExit(f"v4 marker not found: {old}")
    s = s.replace(old, new)

p.write_text(s)
print(
    "v4 wrapper prepared: exact v3 behavior retained outside 4deg; "
    "adaptive micro target/speed/accel/reversal damping active inside 4deg"
)
PY

chmod +x "$V3_SCRIPT"
echo "TEST_ONLY_V4_DELTA=micro_movement_damping_only base_v3_sha=${V3_FFA_SHA} micro_zone_deg=4.0 micro_hold_deg=0.35"
exec "$V3_SCRIPT"
