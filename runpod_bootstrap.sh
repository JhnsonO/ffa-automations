#!/usr/bin/env bash
set -euo pipefail

BASE_SHA="d525ed206740336973d1f46fcb4dbb2d1bc76857"
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

# Combined A/B: layer camera-response overrides on top of B2b's target
# override. These three constants live in FieldPannerConfig::default(),
# which broadcast() (the preset used in production/harness runs) inherits
# unchanged -- action() explicitly overrides dead_zone_rad separately and
# is untouched here. cluster_alpha and centered_smooth() are deliberately
# left alone per this round's isolate-one-variable-at-a-time scope.
repl(
    'max_velocity_rad_per_sec: 0.18,\n',
    'max_velocity_rad_per_sec: 0.31,\n',
    'camera-response max_velocity_rad_per_sec',
)
repl(
    'velocity_alpha: 0.06,\n',
    'velocity_alpha: 0.08,\n',
    'camera-response velocity_alpha',
)
repl(
    'dead_zone_rad: 0.20,\n',
    'dead_zone_rad: 0.06,\n',
    'camera-response dead_zone_rad',
)

repl(
    'assert!(a.dead_zone_rad < b.dead_zone_rad);\n',
    '',
    'camera-response test invariant (broadcast now has a smaller dead_zone than action for this experiment)',
)

path.write_text(s)
PY

python3 - <<'PY2'
from pathlib import Path

path = Path('/tmp/video-stitcher/crates/reco-autocam/src/roi_filter.rs')
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
    "pub struct RoiFilteredDetector {\n    inner: Box<dyn UnifiedDetector>,\n    roi: FieldRoi,\n    class_anchors: HashMap<u16, RoiAnchor>,\n    default_anchor: RoiAnchor,\n    // Diagnostic-only (OEV ball-ROI hypothesis test). Every other class's\n    // filtering path above is untouched; ball_class_id stays None unless\n    // with_ball_roi_diagnostics is called, so default behaviour is identical\n    // to production.\n    ball_class_id: Option<u16>,\n    ball_vertical_margin: f64,\n    roi_diag_frame_left: u64,\n    roi_diag_frame_right: u64,\n    roi_diag_fps: f32,\n    roi_diag_frame_stride: u64,\n}",
    "struct fields",
)

repl(
    "    pub fn new(inner: Box<dyn UnifiedDetector>, roi: FieldRoi) -> Self {\n        Self {\n            inner,\n            roi,\n            class_anchors: HashMap::new(),\n            default_anchor: RoiAnchor::Center,\n        }\n    }",
    "    pub fn new(inner: Box<dyn UnifiedDetector>, roi: FieldRoi) -> Self {\n        Self {\n            inner,\n            roi,\n            class_anchors: HashMap::new(),\n            default_anchor: RoiAnchor::Center,\n            ball_class_id: None,\n            ball_vertical_margin: 0.0,\n            roi_diag_frame_left: 0,\n            roi_diag_frame_right: 0,\n            roi_diag_fps: 60.0,\n            roi_diag_frame_stride: 1,\n        }\n    }",
    "constructor",
)

repl(
    "    /// Override the default anchor for classes without an explicit\n    /// [`with_class_anchor`](Self::with_class_anchor) entry. Chainable.\n    pub fn with_default_anchor(mut self, anchor: RoiAnchor) -> Self {\n        self.default_anchor = anchor;\n        self\n    }\n}",
    "    /// Override the default anchor for classes without an explicit\n    /// [`with_class_anchor`](Self::with_class_anchor) entry. Chainable.\n    pub fn with_default_anchor(mut self, anchor: RoiAnchor) -> Self {\n        self.default_anchor = anchor;\n        self\n    }\n\n    /// Diagnostic-only (OEV ball-ROI hypothesis test, hard case 134-139s).\n    /// Gives `ball_class_id` a generous vertical allowance above the field\n    /// polygon's top edge before the ROI test, so a lofted ball whose\n    /// center is genuinely above the pitch line is not rejected purely for\n    /// being airborne. No other class is affected: `filter_by_roi` below\n    /// still runs unmodified for every class except this one. `fps` /\n    /// `frame_stride` are used only to derive an approximate timestamp for\n    /// window-scoped `eprintln!` diagnostics, matching the same cadence\n    /// formula used elsewhere in the autocam pipeline. Chainable.\n    pub fn with_ball_roi_diagnostics(\n        mut self,\n        ball_class_id: u16,\n        vertical_margin: f64,\n        fps: f32,\n        frame_stride: u64,\n    ) -> Self {\n        self.ball_class_id = Some(ball_class_id);\n        self.ball_vertical_margin = vertical_margin;\n        self.roi_diag_fps = fps;\n        self.roi_diag_frame_stride = frame_stride;\n        self\n    }\n}",
    "builder method",
)

repl(
    "    fn detect(\n        &mut self,\n        camera: CameraId,\n        frame: &DetectorFrame<'_>,\n    ) -> Result<Vec<Detection>, DetectorError> {\n        let detections = self.inner.detect(camera, frame)?;\n        Ok(filter_by_roi(\n            detections,\n            &self.roi,\n            &self.class_anchors,\n            self.default_anchor,\n        ))\n    }",
    "    fn detect(\n        &mut self,\n        camera: CameraId,\n        frame: &DetectorFrame<'_>,\n    ) -> Result<Vec<Detection>, DetectorError> {\n        let detections = self.inner.detect(camera, frame)?;\n\n        // Diagnostic-only: everything below is a no-op (ball_dets always\n        // empty, kept == filter_by_roi(detections, ...)) unless\n        // with_ball_roi_diagnostics was called.\n        let frame_index = match camera {\n            CameraId::Left => {\n                let i = self.roi_diag_frame_left;\n                self.roi_diag_frame_left += 1;\n                i\n            }\n            CameraId::Right => {\n                let i = self.roi_diag_frame_right;\n                self.roi_diag_frame_right += 1;\n                i\n            }\n        };\n        let timestamp_ms = frame_index as f64 * 1000.0 * self.roi_diag_frame_stride as f64\n            / self.roi_diag_fps as f64;\n        let diag_window = (133000.0..=140000.0).contains(&timestamp_ms);\n\n        let (ball_dets, other_dets): (Vec<Detection>, Vec<Detection>) = detections\n            .into_iter()\n            .partition(|d| self.ball_class_id == Some(d.class_id));\n\n        let mut kept = filter_by_roi(other_dets, &self.roi, &self.class_anchors, self.default_anchor);\n\n        let polygon: &[[f64; 2]] = match camera {\n            CameraId::Left => &self.roi.left,\n            CameraId::Right => &self.roi.right,\n        };\n\n        if polygon.len() < 3 {\n            kept.extend(ball_dets);\n        } else {\n            for d in ball_dets {\n                let cx = d.center_x as f64;\n                let cy = d.center_y as f64;\n                let adj_cy = cy + self.ball_vertical_margin;\n                let roi_pass = point_in_polygon([cx, adj_cy], polygon);\n                if diag_window {\n                    eprintln!(\n                        \"OEV_ROI_DIAG cam={:?} t_ms={:.3} cx={:.4} cy={:.4} adj_cy={:.4} roi_pass={}\",\n                        camera, timestamp_ms, cx, cy, adj_cy, roi_pass\n                    );\n                }\n                if roi_pass {\n                    kept.push(d);\n                }\n            }\n        }\n\n        Ok(kept)\n    }",
    "detect() ball diagnostics",
)

path.write_text(s)
PY2

python3 - <<'PY3'
from pathlib import Path

path = Path('/tmp/video-stitcher/crates/reco-autocam/src/lib.rs')
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
    "    let wrap_with_roi = |inner: Box<dyn reco_core::detect::detector::UnifiedDetector>,\n                         roi: reco_core::calibration::FieldRoi|\n     -> Box<dyn reco_core::detect::detector::UnifiedDetector> {\n        Box::new(\n            RoiFilteredDetector::new(inner, roi)\n                .with_class_anchor(person_id_for_roi, RoiAnchor::Bottom)\n                // Diagnostic-only: deliberately generous vertical margin\n                // (0.40, normalized) to test the \"lofted ball rejected by\n                // ROI\" hypothesis. Not a tuned production value.\n                .with_ball_roi_diagnostics(ball_id_for_roi, 0.40, fps, frame_stride),\n        )\n    };",
    "wrap_with_roi wiring",
)

path.write_text(s)
PY3


cd "$WORKDIR"
cargo fmt --all
cargo test -p reco-autocam --lib panners::field::tests -- --nocapture
time cargo build --release -p reco-cli --features cuda

echo "ffa_bootstrap_sha=$BASE_AUTOMATIONS_SHA" >> /tmp/runpod_bootstrap_versions.log
echo "video-stitcher_sha=$BASE_SHA" >> /tmp/runpod_bootstrap_versions.log
echo "video-stitcher_overlay=actionstate-b2b-bridged-authority" >> /tmp/runpod_bootstrap_versions.log
echo "video-stitcher_field_sha256=$(sha256sum crates/reco-autocam/src/panners/field.rs | awk '{print $1}')" >> /tmp/runpod_bootstrap_versions.log
