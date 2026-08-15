#!/usr/bin/env bash
# TEST-ONLY bootstrap wrapper for experiment/lookahead-ball-containment-01.
# It fetches the exact baseline bootstrap, pins Reco to the proven main SHA,
# patches ONLY the final buffered render pose with a tracked-ball containment
# guard after centered smoothing, then runs the normal bootstrap/build.
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
/// TEST-ONLY experiment guard injected by ffa-automations.
///
/// The normal panner and centered lookahead smoother still decide composition.
/// This final guard runs after that smoothing and minimally translates the
/// rendered crop only when a freshly Tracking ball would otherwise leave a
/// 3-degree safe margin. Coasting/Lost estimates are deliberately not hard
/// constraints.
///
/// Returns (guarded_pose, absolute_yaw_correction_deg,
/// absolute_pitch_correction_deg).
fn enforce_tracked_ball_containment(
    mut pose: crate::detect::director::ViewportPosition,
    world: &crate::detect::tracker::WorldState,
) -> (crate::detect::director::ViewportPosition, f32, f32) {
    let Some(fov_deg) = pose.fov_degrees else {
        return (pose, 0.0, 0.0);
    };
    let Some(ball) = world.ball.as_ref() else {
        return (pose, 0.0, 0.0);
    };
    if !matches!(
        ball.state,
        crate::detect::tracker::TrackState::Tracking
    ) || !ball.yaw.is_finite()
        || !ball.pitch.is_finite()
        || !pose.yaw.is_finite()
        || !pose.pitch.is_finite()
        || !fov_deg.is_finite()
        || fov_deg <= 0.0
    {
        return (pose, 0.0, 0.0);
    }

    const ASPECT: f32 = 16.0 / 9.0;
    const SAFE_MARGIN_DEG: f32 = 3.0;

    // Reco's fov_degrees is horizontal FOV. Derive vertical FOV for 16:9.
    let half_h_full = (0.5 * fov_deg).to_radians();
    let margin = SAFE_MARGIN_DEG.to_radians();
    let half_h_safe = (half_h_full - margin).max(0.5_f32.to_radians());
    let half_v_full = (half_h_full.tan() / ASPECT).atan();
    let half_v_safe = (half_v_full - margin).max(0.5_f32.to_radians());

    let original_yaw = pose.yaw;
    let original_pitch = pose.pitch;

    // Shortest signed angular difference handles panorama wrap cleanly.
    let raw_yaw_delta = ball.yaw - pose.yaw;
    let yaw_delta = raw_yaw_delta.sin().atan2(raw_yaw_delta.cos());
    if yaw_delta > half_h_safe {
        pose.yaw += yaw_delta - half_h_safe;
    } else if yaw_delta < -half_h_safe {
        pose.yaw += yaw_delta + half_h_safe;
    }

    let pitch_delta = ball.pitch - pose.pitch;
    if pitch_delta > half_v_safe {
        pose.pitch += pitch_delta - half_v_safe;
    } else if pitch_delta < -half_v_safe {
        pose.pitch += pitch_delta + half_v_safe;
    }

    (
        pose,
        (pose.yaw - original_yaw).abs().to_degrees(),
        (pose.pitch - original_pitch).abs().to_degrees(),
    )
}
'''

s = s.replace(impl_marker, "\n" + helper + impl_marker, 1)

render_call = (
    "self.render_buffered_frame(oldest, smoothed_pose, start, &ctx, on_progress)?;"
)
if s.count(render_call) != 2:
    raise SystemExit(
        f"expected exactly two buffered render calls, found {s.count(render_call)}"
    )

guarded_render = r'''let (guarded_pose, guard_yaw_deg, guard_pitch_deg) =
                    enforce_tracked_ball_containment(smoothed_pose, &oldest.world_state);
                if guard_yaw_deg > 0.001 || guard_pitch_deg > 0.001 {
                    log::info!(
                        "BALL_CONTAINMENT_GUARD frame={} yaw_correction_deg={:.3} pitch_correction_deg={:.3}",
                        self.frame_count,
                        guard_yaw_deg,
                        guard_pitch_deg,
                    );
                }
                self.render_buffered_frame(oldest, guarded_pose, start, &ctx, on_progress)?;'''

s = s.replace(render_call, guarded_render)
path.write_text(s)
print(
    "patched run_loop.rs: post-centered-smooth Tracking-ball containment guard "
    "inserted at both steady-state and drain render sites"
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
python3 "{patcher}" "$WORKDIR/crates/reco-core/src/session/run_loop.rs" || fail "test-only ball-containment source patch failed" 3
log "TEST-ONLY patch active: post-smoothing Tracking-ball containment guard; video-stitcher checkout remains {reco_sha} with a dirty working tree"
REPO_SHA=$(git -C "$WORKDIR" rev-parse HEAD)
'''
s = s.replace(needle, replacement, 1)
base.write_text(s)
PY

chmod +x "$BASE_SCRIPT"
echo "TEST_ONLY_RECO_PATCH=lookahead_ball_containment_01 reco_base_sha=${RECO_SHA} safe_margin_deg=3.0 output_aspect=16:9"
exec "$BASE_SCRIPT"
