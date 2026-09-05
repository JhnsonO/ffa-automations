#!/usr/bin/env bash
set -euo pipefail

BASE_SHA="c8b0d74b537d192c7de8d2856de64620a82830cf"
BASE_AUTOMATIONS_SHA="c06aea887c3f0824c75b487b54c511cb7eb8f50b"
WORKDIR="/tmp/video-stitcher"
BASE_BOOTSTRAP="/tmp/runpod_bootstrap_production.sh"

curl -fsSL "https://raw.githubusercontent.com/JhnsonO/ffa-automations/${BASE_AUTOMATIONS_SHA}/runpod_bootstrap.sh" -o "$BASE_BOOTSTRAP"
chmod +x "$BASE_BOOTSTRAP"
"$BASE_BOOTSTRAP"

# Production bootstrap installs Rust in a child shell; restore Cargo in this wrapper.
if [ -f "$HOME/.cargo/env" ]; then
  . "$HOME/.cargo/env"
else
  export PATH="$HOME/.cargo/bin:$PATH"
fi
command -v cargo >/dev/null || {
  echo "cargo missing after production bootstrap" >&2
  exit 127
}

git -C "$WORKDIR" fetch origin "$BASE_SHA"
git -C "$WORKDIR" reset --hard "$BASE_SHA"

python3 - <<'PY'
from pathlib import Path

path = Path('/tmp/video-stitcher/crates/reco-autocam/src/panners/field.rs')
s = path.read_text()

def repl(old, new, label):
    global s
    if old not in s:
        raise SystemExit(f'B2b patch anchor missing: {label}')
    s = s.replace(old, new, 1)

repl(
    'const LOG_INTERVAL: u64 = 30;\n',
    '''const LOG_INTERVAL: u64 = 30;\n\n// Frozen ActionState B1/B2b experiment constants. 0.30 rad deliberately mirrors\n// the production cluster_bandwidth_rad default without changing that config.\nconst B1_LOCAL_RADIUS_RAD: f32 = 0.30;\nconst B1_BALL_WEIGHT: f32 = 0.50;\nconst B1_LOCAL_WEIGHT: f32 = 0.30;\nconst B1_FUTURE_WEIGHT: f32 = 0.20;\nconst B1_FUTURE_HORIZON_S: f32 = 0.60;\n''',
    'constants',
)

repl(
    'pub struct FieldPanner {\n    config: FieldPannerConfig,\n',
    'pub struct FieldPanner {\n    config: FieldPannerConfig,\n    fps: f32,\n',
    'fps field',
)

repl(
    '        Self {\n            config,\n            yaw: 0.0,\n',
    '        Self {\n            config,\n            fps,\n            yaw: 0.0,\n',
    'fps init',
)

anchor = '''    /// Instantaneous look-at target from a world state, with no\n    /// smoothing or internal state, else the live ball, else None. Used\n'''
helper = '''    /// Unweighted geometric centroid of non-Lost players within the frozen\n    /// B1 radius of a Tracking or retrospectively validated Bridged ball.\n    /// Coasting remains excluded because it is blind live prediction; Lost\n    /// remains excluded. No local players still forces exact production aim.\n    fn b1_local_centroid(&self, world: &WorldState) -> Option<(f32, f32)> {\n        let ball = world\n            .ball\n            .as_ref()\n            .filter(|b| !matches!(b.state, TrackState::Lost | TrackState::Coasting))?;\n        if !ball.yaw.is_finite() || !ball.pitch.is_finite() {\n            return None;\n        }\n\n        let radius_sq = B1_LOCAL_RADIUS_RAD * B1_LOCAL_RADIUS_RAD;\n        let mut sum_yaw = 0.0_f32;\n        let mut sum_pitch = 0.0_f32;\n        let mut n = 0u32;\n        for p in &world.players {\n            if matches!(p.state, TrackState::Lost) || !p.yaw.is_finite() || !p.pitch.is_finite() {\n                continue;\n            }\n            let dy = p.yaw - ball.yaw;\n            let dp = p.pitch - ball.pitch;\n            if dy * dy + dp * dp <= radius_sq {\n                sum_yaw += p.yaw;\n                sum_pitch += p.pitch;\n                n += 1;\n            }\n        }\n        (n > 0).then_some((sum_yaw / n as f32, sum_pitch / n as f32))\n    }\n\n    /// Frozen B2b aim policy: identical to B1's 50% ball + 30% local centroid\n    /// + 20% local centroid at t+0.6s, except validated Bridged balls retain\n    /// B1 authority. Coasting and Lost still fall back to production. If the\n    /// future local group is unavailable, drop future and renormalise 50/30.\n    fn b1_aim_target(&self, world: &WorldState, future: &[WorldState]) -> Option<(f32, f32)> {\n        let ball = world\n            .ball\n            .as_ref()\n            .filter(|b| !matches!(b.state, TrackState::Lost | TrackState::Coasting))?;\n        let (local_yaw, local_pitch) = self.b1_local_centroid(world)?;\n\n        let future_frames = (B1_FUTURE_HORIZON_S * self.fps).round() as usize;\n        let future_index = future_frames.saturating_sub(1);\n        let future_local = future\n            .get(future_index)\n            .and_then(|w| self.b1_local_centroid(w));\n\n        if let Some((future_yaw, future_pitch)) = future_local {\n            // Express the future signal explicitly as current local centroid\n            // plus its 0.6s displacement; algebraically this is future_local.\n            let displacement_yaw = future_yaw - local_yaw;\n            let displacement_pitch = future_pitch - local_pitch;\n            let future_signal_yaw = local_yaw + displacement_yaw;\n            let future_signal_pitch = local_pitch + displacement_pitch;\n            Some((\n                B1_BALL_WEIGHT * ball.yaw\n                    + B1_LOCAL_WEIGHT * local_yaw\n                    + B1_FUTURE_WEIGHT * future_signal_yaw,\n                B1_BALL_WEIGHT * ball.pitch\n                    + B1_LOCAL_WEIGHT * local_pitch\n                    + B1_FUTURE_WEIGHT * future_signal_pitch,\n            ))\n        } else {\n            let current_total = B1_BALL_WEIGHT + B1_LOCAL_WEIGHT;\n            Some((\n                (B1_BALL_WEIGHT * ball.yaw + B1_LOCAL_WEIGHT * local_yaw) / current_total,\n                (B1_BALL_WEIGHT * ball.pitch + B1_LOCAL_WEIGHT * local_pitch) / current_total,\n            ))\n        }\n    }\n\n'''
repl(anchor, helper + anchor, 'B2b helpers')

old_target_tail = '''        } else {\n            None\n        };\n\n        // Lookahead lead: aim toward where the action is heading. The\n'''
new_target_tail = '''        } else {\n            None\n        };\n\n        // B2b changes only B1's accepted current ball states: Tracking +\n        // Bridged have local-action authority; Coasting/Lost fall back.\n        // Production cluster/FOV, ball-presence state and downstream motion\n        // pipeline remain intact.\n        let b1_target = if self.config.framing == FramingMode::Action {\n            self.b1_aim_target(world, future)\n        } else {\n            None\n        };\n        let b1_active = b1_target.is_some();\n\n        // Additive diagnostics only. Consecutive rows reconstruct\n        // Tracking -> Bridged -> Tracking transitions and verify that the local\n        // neighbourhood remains available through the hard case.\n        if (133000.0..=140000.0).contains(&_ctx.timestamp_ms) {\n            let ball_state = world.ball.as_ref().map(|b| b.state);\n            let local_ok = self.b1_local_centroid(world).is_some();\n            eprintln!(\n                "OEV_B2B frame={} t_ms={:.3} ball_state={:?} local_centroid={} b1_active={}",\n                _ctx.frame_index, _ctx.timestamp_ms, ball_state, local_ok, b1_active\n            );\n        }\n\n        if let Some((b1_yaw, b1_pitch)) = b1_target {\n            target = Some((b1_yaw, b1_pitch));\n            // Keep PannerDebug target coordinates aligned with the applied aim.\n            // effective_ball_weight=0.50 records the fixed B1 ball contribution.\n            if cluster.is_some() {\n                cluster_target = Some((b1_yaw, b1_pitch, B1_BALL_WEIGHT));\n            }\n        }\n\n        // Lookahead lead: aim toward where the action is heading. The\n'''
repl(old_target_tail, new_target_tail, 'B2b target override')

repl(
    '''                target = Some((ty + self.lead_yaw, tp + self.lead_pitch));\n''',
    '''                // Keep production lead state warm so fallback frames retain\n                // production continuity, but do not apply the 1.5s mean lead while\n                // B1/B2b is active; the experiment applies only its frozen 0.6s future term.\n                if !b1_active {\n                    target = Some((ty + self.lead_yaw, tp + self.lead_pitch));\n                }\n''',
    'lookahead application gate',
)

path.write_text(s)
PY

cd "$WORKDIR"
cargo fmt --all
cargo test -p reco-autocam --lib panners::field::tests -- --nocapture
time cargo build --release -p reco-cli --features cuda

echo "ffa_bootstrap_sha=$BASE_AUTOMATIONS_SHA" >> /tmp/runpod_bootstrap_versions.log
echo "video-stitcher_sha=$BASE_SHA" >> /tmp/runpod_bootstrap_versions.log
echo "video-stitcher_overlay=actionstate-b2b-bridged-authority" >> /tmp/runpod_bootstrap_versions.log
echo "video-stitcher_field_sha256=$(sha256sum crates/reco-autocam/src/panners/field.rs | awk '{print $1}')" >> /tmp/runpod_bootstrap_versions.log
