#!/usr/bin/env python3
"""
FFA 360 Render Segment — v7
============================
Pure render step. Reads tracking.json (pre-computed) and optional activity.json,
renders a frame window as two outputs:
  - render_clean.mp4  : follow-cam output, no overlays
  - render_debug.mp4  : same + HUD overlay + equirect inset with ball marker

v5: Smooth zoom-out fallback — no hard cut.
v6: EMA snap on reacquisition.
v7: Activity-biased wide fallback.
    When tracker is UNCERTAIN/LOST and camera is in wide mode, activity.json
    cluster positions smoothly bias the fallback yaw/pitch toward active play.
    - Activity target is EMA-smoothed (slow, α=ACTIVITY_EMA_ALPHA) — no snapping.
    - Only applied when cluster confidence >= ACTIVITY_CONF_THRESHOLD.
    - Below threshold: fixed fallback pose unchanged.
    - FOLLOW behaviour is identical to v6 — no changes when ball is confirmed.

Render modes (HUD labels):
  FOLLOW        — confirmed ball, EMA follow-cam, FOV=OUTPUT_FOV
  WIDE_ACTIVITY — wide hold, activity cluster biasing yaw/pitch target
  WIDE_FIXED    — wide hold, no confident activity cluster, fixed fallback pose
  REACQUIRE     — ball reacquired, zooming back to follow
  (internal: ZOOMING_OUT / ZOOMING_IN still used for animation logic)

Config (overridable via CLI):
  FALLBACK_YAW          — wide view target yaw (used when no activity)
  FALLBACK_PITCH        — wide view target pitch
  FALLBACK_FOV          — wide view target FOV (degrees) — 120° default
  HOLD_BEFORE_ZOOM      — frames of hold before zoom begins
  FALLBACK_ZOOM_FRAMES  — frames over which zoom-out animation completes (~1–1.5s)
  REACQUIRE_MIN_FRAMES  — consecutive confirmed frames before zoom-in begins
  ACTIVITY_CONF_THRESHOLD — min cluster confidence to apply activity bias (0–1)
  ACTIVITY_EMA_ALPHA    — smoothing rate for activity target (lower = slower)
"""

import argparse
import json
import math
import os
import subprocess

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
FFMPEG       = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")

OUTPUT_W     = 1920
OUTPUT_H     = 1080
OUTPUT_FOV   = 90.0        # follow-cam FOV (degrees)
LEAD_DEG     = 3.0

INSET_W      = 480
INSET_H      = 270
INSET_X      = 20
INSET_Y_OFF  = 20

HUD_FONT     = cv2.FONT_HERSHEY_SIMPLEX
HUD_SCALE    = 0.65
HUD_THICK    = 2
HUD_COLOR    = (255, 255, 255)
HUD_SHADOW   = (0, 0, 0)

# ---------------------------------------------------------------------------
# v5 Fallback config
# ---------------------------------------------------------------------------
FALLBACK_YAW          = 0.0    # pitch-centre yaw
FALLBACK_PITCH        = 5.0    # lift target — test +5 and +8
FALLBACK_FOV          = 120.0  # wide view FOV (120° is acceptable from calibration)
FALLBACK_ROLL         = 4.0    # horizon-levelling roll offset for this venue (degrees)
HOLD_BEFORE_ZOOM      = 15     # frames hold at last follow pose before zoom begins
FALLBACK_ZOOM_FRAMES  = 45     # frames for zoom-out animation (~1.5s @ 30fps)
REACQUIRE_MIN_FRAMES  = 5      # consecutive confirmed frames before zoom-in begins

EMA_ALPHA_TRACKING    = 0.18
EMA_ALPHA_LOSS        = 0.08

# ---------------------------------------------------------------------------
# v7 Activity bias config
# ---------------------------------------------------------------------------
ACTIVITY_CONF_THRESHOLD = 0.5   # min cluster confidence to apply activity bias
ACTIVITY_EMA_ALPHA      = 0.04  # slow smoothing — ~25 frames to move meaningfully

# ---------------------------------------------------------------------------
# Render states
# ---------------------------------------------------------------------------
RENDER_FOLLOW        = "FOLLOW"
RENDER_ZOOMING_OUT   = "ZOOMING_OUT"
RENDER_WIDE_HOLD     = "WIDE_HOLD"
RENDER_ZOOMING_IN    = "ZOOMING_IN"
# HUD display labels (mapped from internal states)
LABEL_FOLLOW         = "FOLLOW"
LABEL_WIDE_ACTIVITY  = "WIDE_ACTIVITY"
LABEL_WIDE_FIXED     = "WIDE_FIXED"
LABEL_REACQUIRE      = "REACQUIRE"

STATE_COLORS = {
    "TRACKING":          (0, 180, 0),
    "UNCERTAIN":         (0, 160, 200),
    "LOST":              (0, 0, 200),
    "WARMING_UP":        (200, 160, 0),
    "UNINITIALIZED":     (80, 80, 80),
    LABEL_FOLLOW:        (0, 180, 0),
    LABEL_WIDE_ACTIVITY: (200, 120, 0),
    LABEL_WIDE_FIXED:    (30, 30, 180),
    LABEL_REACQUIRE:     (0, 210, 140),
    RENDER_ZOOMING_OUT:  (30, 120, 220),
    RENDER_ZOOMING_IN:   (0, 210, 140),
}


# ---------------------------------------------------------------------------
# Easing
# ---------------------------------------------------------------------------
def ease_inout(t):
    """Smooth cubic ease-in-out so zoom feels organic, not mechanical."""
    return t * t * (3.0 - 2.0 * t)


# ---------------------------------------------------------------------------
# Activity bias helper
# ---------------------------------------------------------------------------
class ActivityBias:
    """
    Loads activity.json and provides a smoothed yaw/pitch target for wide fallback.

    - Builds a frame-sorted list of (frame, yaw, pitch, confidence) from
      the per-frame cluster rows.
    - On each update(frame_idx), finds the nearest sampled frame and reads
      its cluster centre + confidence.
    - If confidence >= threshold, EMA-smooths the target yaw/pitch.
    - Returns (target_yaw, target_pitch, active:bool).
    """

    def __init__(self, activity_path, conf_threshold=ACTIVITY_CONF_THRESHOLD,
                 ema_alpha=ACTIVITY_EMA_ALPHA):
        self.conf_threshold = conf_threshold
        self.ema_alpha      = ema_alpha
        self._samples       = []   # list of (frame, yaw, pitch, conf)
        self._ema_yaw       = None
        self._ema_pitch     = None

        if activity_path and os.path.isfile(activity_path):
            with open(activity_path) as f:
                data = json.load(f)
            for row in data.get("frames", []):
                frame  = row.get("frame")
                centre = row.get("cluster_centre")
                conf   = row.get("confidence", 0.0)
                if frame is not None and centre and conf is not None:
                    self._samples.append((frame, float(centre["yaw"]), float(centre["pitch"]), float(conf)))
            self._samples.sort(key=lambda x: x[0])
            print(f"[activity_bias] Loaded {len(self._samples)} samples from {activity_path}")
        else:
            print(f"[activity_bias] No activity.json — wide fallback will use fixed pose")

    def _nearest(self, frame_idx):
        """Return (yaw, pitch, conf) of the nearest sampled frame."""
        if not self._samples:
            return None, None, 0.0
        best = min(self._samples, key=lambda x: abs(x[0] - frame_idx))
        return best[1], best[2], best[3]

    def update(self, frame_idx, fixed_yaw, fixed_pitch):
        """
        Returns (target_yaw, target_pitch, is_active).
        is_active=True means a confident cluster is driving the bias.
        Activity drives YAW only — pitch stays at fixed_pitch (avoids camera
        pointing skyward at player foot-proxy projections which are +30–55°).
        """
        yaw, pitch, conf = self._nearest(frame_idx)
        if yaw is None or conf < self.conf_threshold:
            return fixed_yaw, fixed_pitch, False

        # Initialise EMA on first confident sample
        if self._ema_yaw is None:
            self._ema_yaw   = yaw
            self._ema_pitch = fixed_pitch
        else:
            diff = (yaw - self._ema_yaw + 540) % 360 - 180
            self._ema_yaw = self._ema_yaw + self.ema_alpha * diff
            # Pitch held at fixed_pitch — activity pitch values are foot-proxy
            # equirect projections, not usable as camera pitch targets
            self._ema_pitch = fixed_pitch

        return self._ema_yaw, self._ema_pitch, True


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def extract_crop_frame(equirect_frame, yaw_deg, pitch_deg, fov_deg, out_w, out_h,
                        roll_deg=0.0):
    """Perspective crop with optional roll (horizon levelling). roll_deg=0 = no rotation."""
    h_eq, w_eq = equirect_frame.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_w / 2.0) / f
    ry = -(yv - out_h / 2.0) / f
    rz = np.ones_like(rx)
    # Apply roll around optical axis
    cr = math.cos(math.radians(roll_deg))
    sr = math.sin(math.radians(roll_deg))
    rx, ry = cr * rx - sr * ry, sr * rx + cr * ry
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    cy = math.radians(yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    cp = math.radians(pitch_deg)
    wx2 = wx
    wy2 = math.cos(cp) * wy - math.sin(cp) * wz
    wz2 = math.sin(cp) * wy + math.cos(cp) * wz
    yaw_map   = np.arctan2(wx2, wz2)
    pitch_map = np.arcsin(np.clip(wy2, -1, 1))
    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    return cv2.remap(equirect_frame,
                     map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def yaw_pitch_to_equirect_pixel(yaw_deg, pitch_deg, w, h):
    x = int(((yaw_deg / 360.0) + 0.5) % 1.0 * w)
    y = int((0.5 - pitch_deg / 180.0) * h)
    return x, y


def lerp_yaw(current, target, t):
    diff = (target - current + 540) % 360 - 180
    return current + t * diff


def lerp_val(a, b, t):
    return a + t * (b - a)


# ---------------------------------------------------------------------------
# HUD
# ---------------------------------------------------------------------------
def draw_text_shadowed(img, text, pos, scale, color, thick, shadow=HUD_SHADOW):
    x, y = pos
    cv2.putText(img, text, (x+1, y+1), HUD_FONT, scale, shadow, thick+1, cv2.LINE_AA)
    cv2.putText(img, text, (x, y),     HUD_FONT, scale, color,  thick,   cv2.LINE_AA)


def draw_hud(frame, frame_data, frame_idx, fps, cam_yaw, cam_pitch, cam_fov,
             render_mode, zoom_t, hold_counter, reacquire_streak,
             fallback_yaw, fallback_pitch, fallback_fov):
    out = frame.copy()
    h, w = out.shape[:2]

    tracker_state = frame_data.get("tracker_state", "?")
    loss_state    = frame_data.get("loss_state", "")
    bar_color = STATE_COLORS.get(render_mode, (80, 80, 80))
    cv2.rectangle(out, (0, 0), (w, 44), bar_color, -1)
    cv2.rectangle(out, (0, 0), (w, 44), (0, 0, 0), 2)
    label = f"RENDER: {render_mode}   |   TRACKER: {tracker_state}   {loss_state}"
    draw_text_shadowed(out, label, (10, 30), HUD_SCALE * 1.05, (0, 0, 0), HUD_THICK)

    t_sec = frame_idx / fps if fps else 0
    draw_text_shadowed(out, f"Frame {frame_idx}  t={t_sec:.2f}s",
                       (10, 78), HUD_SCALE, HUD_COLOR, HUD_THICK)

    draw_text_shadowed(out,
                       f"Cam  yaw={cam_yaw:.1f}  pitch={cam_pitch:.1f}  fov={cam_fov:.1f}",
                       (10, 106), HUD_SCALE, HUD_COLOR, HUD_THICK)

    smoothed   = frame_data.get("smoothed") or {}
    ball_yaw   = smoothed.get("yaw", "?")
    ball_pitch = smoothed.get("pitch", "?")
    best_score = frame_data.get("best_score")
    score_str  = f"{best_score:.3f}" if best_score is not None else "—"
    draw_text_shadowed(out,
                       f"Ball yaw={ball_yaw}  pitch={ball_pitch}  score={score_str}",
                       (10, 134), HUD_SCALE, HUD_COLOR, HUD_THICK)

    if render_mode in (RENDER_ZOOMING_OUT, RENDER_ZOOMING_IN, RENDER_WIDE_HOLD):
        zoom_pct = int(zoom_t * 100)
        draw_text_shadowed(out,
                           f"zoom_t={zoom_t:.2f} ({zoom_pct}%)  target=({fallback_yaw:.0f}°,{fallback_pitch:.0f}°,{fallback_fov:.0f}°fov)  hold={hold_counter}",
                           (10, 162), HUD_SCALE, (80, 160, 255), HUD_THICK)
    elif render_mode == RENDER_ZOOMING_IN:
        draw_text_shadowed(out,
                           f"reacquire_streak={reacquire_streak}/{REACQUIRE_MIN_FRAMES}  zoom_t={zoom_t:.2f}",
                           (10, 162), HUD_SCALE, (0, 220, 140), HUD_THICK)

    dets = frame_data.get("detections", [])
    draw_text_shadowed(out, f"Detections: {len(dets)}",
                       (10, 190), HUD_SCALE, HUD_COLOR, HUD_THICK)

    # Crosshair — dimmer when zoomed out
    alpha_ch = 1.0 - zoom_t * 0.6
    ch_val   = int(255 * alpha_ch)
    ch_color = (0, ch_val, ch_val) if render_mode == RENDER_FOLLOW else (80, 80, 80)
    cx = w // 2 - int(LEAD_DEG / OUTPUT_FOV * w * (1.0 - zoom_t))
    cy_mid = h // 2
    cv2.circle(out, (cx, cy_mid), 18, ch_color, 2)
    cv2.circle(out, (cx, cy_mid), 4,  ch_color, -1)
    cv2.line(out, (cx-28, cy_mid), (cx+28, cy_mid), ch_color, 1)
    cv2.line(out, (cx, cy_mid-28), (cx, cy_mid+28), ch_color, 1)

    return out


def draw_equirect_inset(equirect_frame, frame_data, inset_w, inset_h):
    inset = cv2.resize(equirect_frame, (inset_w, inset_h))
    smoothed = frame_data.get("smoothed") or {}
    yaw   = smoothed.get("yaw")
    pitch = smoothed.get("pitch")
    if yaw is not None and pitch is not None:
        px, py = yaw_pitch_to_equirect_pixel(yaw, pitch, inset_w, inset_h)
        px = max(5, min(inset_w-5, px))
        py = max(5, min(inset_h-5, py))
        cv2.circle(inset, (px, py), 8, (0, 255, 255), 2)
        cv2.circle(inset, (px, py), 2, (0, 255, 255), -1)
    for det in frame_data.get("detections", []):
        dx, dy = yaw_pitch_to_equirect_pixel(det["yaw"], det["pitch"], inset_w, inset_h)
        dx = max(3, min(inset_w-3, dx))
        dy = max(3, min(inset_h-3, dy))
        cv2.circle(inset, (dx, dy), 4, (0, 80, 255), -1)
    cv2.rectangle(inset, (0, 0), (inset_w-1, inset_h-1), (200, 200, 200), 1)
    return inset


# ---------------------------------------------------------------------------
# v5 Smooth Zoom Fallback FSM
# ---------------------------------------------------------------------------
class SmoothZoomFallbackFSM:
    """
    FOLLOW      : ball confirmed → EMA follow-cam, FOV=OUTPUT_FOV
    ZOOMING_OUT : ball lost (after hold) → t advances 0→1, camera animates to wide pose
    WIDE_HOLD   : t=1, camera at wide pose, waiting for ball
    ZOOMING_IN  : ball reacquired → t reverses 1→0, camera returns to follow pose

    v7: wide pose target is replaced by activity bias when confidence is high enough.
    """

    def __init__(self, fallback_yaw, fallback_pitch, fallback_fov, fallback_roll,
                 activity_bias=None):
        self.mode              = RENDER_FOLLOW
        self.hold_counter      = 0
        self.reacquire_streak  = 0
        self.zoom_t            = 0.0
        self.zoom_start_yaw    = 0.0
        self.zoom_start_pitch  = 0.0
        self.zoom_start_fov    = OUTPUT_FOV
        self.zoom_start_roll   = 0.0
        # Fixed fallback targets (never mutated)
        self.fallback_yaw      = fallback_yaw
        self.fallback_pitch    = fallback_pitch
        self.fallback_fov      = fallback_fov
        self.fallback_roll     = fallback_roll
        # Live wide target (updated each frame via activity bias)
        self._wide_yaw         = fallback_yaw
        self._wide_pitch       = fallback_pitch
        self._activity_bias    = activity_bias
        self._activity_active  = False   # for HUD label

    def _resolve_wide_target(self, frame_idx):
        """Update live wide target from activity bias if available."""
        if self._activity_bias is not None:
            yaw, pitch, active = self._activity_bias.update(
                frame_idx, self.fallback_yaw, self.fallback_pitch)
            self._wide_yaw        = yaw
            self._wide_pitch      = pitch
            self._activity_active = active
        else:
            self._wide_yaw        = self.fallback_yaw
            self._wide_pitch      = self.fallback_pitch
            self._activity_active = False

    def _interp_pose(self, t):
        """Interpolate from zoom-start pose to current wide target using eased t."""
        et = ease_inout(t)
        yaw   = lerp_yaw(self.zoom_start_yaw,   self._wide_yaw,    et)
        pitch = lerp_val(self.zoom_start_pitch,  self._wide_pitch,  et)
        fov   = lerp_val(self.zoom_start_fov,    self.fallback_fov, et)
        roll  = lerp_val(self.zoom_start_roll,   self.fallback_roll,et)
        return yaw, pitch, fov, roll

    def _hud_label(self):
        if self.mode == RENDER_FOLLOW:
            return LABEL_FOLLOW
        if self.mode == RENDER_ZOOMING_IN:
            return LABEL_REACQUIRE
        # ZOOMING_OUT or WIDE_HOLD
        return LABEL_WIDE_ACTIVITY if self._activity_active else LABEL_WIDE_FIXED

    def update(self, ema_yaw, ema_pitch, tracker_state, best_score, frame_idx=0):
        confirmed = (best_score is not None)
        dt_out = 1.0 / FALLBACK_ZOOM_FRAMES
        dt_in  = 1.0 / FALLBACK_ZOOM_FRAMES

        # Resolve wide target every frame (activity EMA ticks continuously)
        self._resolve_wide_target(frame_idx)
        hud_label = self._hud_label()

        if self.mode == RENDER_FOLLOW:
            if confirmed:
                self.hold_counter     = 0
                self.reacquire_streak = 0
                self.zoom_t           = 0.0
                return ema_yaw, ema_pitch, OUTPUT_FOV, 0.0, hud_label, 0.0, 0, 0
            else:
                self.hold_counter += 1
                if self.hold_counter >= HOLD_BEFORE_ZOOM:
                    self.mode             = RENDER_ZOOMING_OUT
                    self.zoom_t           = 0.0
                    self.zoom_start_yaw   = ema_yaw
                    self.zoom_start_pitch = ema_pitch
                    self.zoom_start_fov   = OUTPUT_FOV
                    self.zoom_start_roll  = 0.0
                    self.reacquire_streak = 0
                return ema_yaw, ema_pitch, OUTPUT_FOV, 0.0, hud_label, 0.0, self.hold_counter, 0

        elif self.mode == RENDER_ZOOMING_OUT:
            self.zoom_t = min(1.0, self.zoom_t + dt_out)
            yaw, pitch, fov, roll = self._interp_pose(self.zoom_t)
            if confirmed:
                self.reacquire_streak += 1
            else:
                self.reacquire_streak = 0
            if self.zoom_t >= 1.0:
                self.mode = RENDER_WIDE_HOLD
            return yaw, pitch, fov, roll, hud_label, self.zoom_t, self.hold_counter, self.reacquire_streak

        elif self.mode == RENDER_WIDE_HOLD:
            yaw, pitch, fov, roll = self._interp_pose(1.0)
            if confirmed:
                self.reacquire_streak += 1
                if self.reacquire_streak >= REACQUIRE_MIN_FRAMES:
                    self.mode             = RENDER_ZOOMING_IN
                    self.zoom_start_yaw   = yaw
                    self.zoom_start_pitch = pitch
                    self.zoom_start_fov   = fov
                    self.zoom_start_roll  = roll
            else:
                self.reacquire_streak = 0
            return yaw, pitch, fov, roll, hud_label, 1.0, self.hold_counter, self.reacquire_streak

        elif self.mode == RENDER_ZOOMING_IN:
            if confirmed:
                self.reacquire_streak += 1
                self.zoom_t = max(0.0, self.zoom_t - dt_in)
                et = ease_inout(self.zoom_t)
                yaw   = lerp_yaw(ema_yaw,   self.zoom_start_yaw,   et)
                pitch = lerp_val(ema_pitch,  self.zoom_start_pitch, et)
                fov   = lerp_val(OUTPUT_FOV, self.zoom_start_fov,   et)
                roll  = lerp_val(0.0,        self.zoom_start_roll,  et)
                if self.zoom_t <= 0.0:
                    self.mode             = RENDER_FOLLOW
                    self.hold_counter     = 0
                    self.reacquire_streak = 0
                    self.zoom_t           = 0.0
                    return ema_yaw, ema_pitch, OUTPUT_FOV, 0.0, LABEL_FOLLOW, 0.0, 0, 0
                return yaw, pitch, fov, roll, hud_label, self.zoom_t, self.hold_counter, self.reacquire_streak
            else:
                cur_yaw, cur_pitch, cur_fov, roll = self._interp_pose(self.zoom_t)
                self.mode             = RENDER_ZOOMING_OUT
                self.zoom_start_yaw   = cur_yaw
                self.zoom_start_pitch = cur_pitch
                self.zoom_start_fov   = cur_fov
                self.zoom_start_roll  = roll
                self.reacquire_streak = 0
                return cur_yaw, cur_pitch, cur_fov, roll, hud_label, self.zoom_t, self.hold_counter, 0

        return ema_yaw, ema_pitch, OUTPUT_FOV, 0.0, LABEL_FOLLOW, 0.0, 0, 0


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------
def render_segment(equirect_path, tracking_path, start_frame, end_frame,
                   output_clean, output_debug,
                   fallback_yaw, fallback_pitch, fallback_fov, fallback_roll,
                   activity_path=None):

    print("[render v7] Loading tracking.json...")
    with open(tracking_path) as f:
        tracking = json.load(f)

    fps           = float(tracking.get("fps", 29.97))
    frames_data   = tracking.get("frames", [])
    total_tracked = len(frames_data)
    zoom_secs     = FALLBACK_ZOOM_FRAMES / fps
    print(f"[render v7] {total_tracked} frames @ {fps:.2f} fps")
    print(f"[render v7] Rendering frames {start_frame}–{end_frame}")
    print(f"[render v7] Zoom: {FALLBACK_ZOOM_FRAMES} frames ({zoom_secs:.2f}s)  "
          f"hold={HOLD_BEFORE_ZOOM}fr")
    print(f"[render v7] Wide target (fixed): yaw={fallback_yaw}° pitch={fallback_pitch}° "
          f"fov={fallback_fov}° roll={fallback_roll}°")

    activity_bias = ActivityBias(activity_path) if activity_path else ActivityBias(None)

    end_frame = min(end_frame, total_tracked)

    cap = cv2.VideoCapture(equirect_path)
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    def make_writer(path):
        return subprocess.Popen([
            FFMPEG, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-s", f"{OUTPUT_W}x{OUTPUT_H}",
            "-pix_fmt", "bgr24",
            "-r", str(fps),
            "-i", "pipe:0",
            "-vcodec", "libx264",
            "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            path
        ], stdin=subprocess.PIPE)

    writer_clean = make_writer(output_clean)
    writer_debug = make_writer(output_debug)

    ema_yaw, ema_pitch = None, None
    ema_yaw_ref = 0.0
    prev_best_score = None

    fsm = SmoothZoomFallbackFSM(fallback_yaw, fallback_pitch, fallback_fov, fallback_roll,
                                 activity_bias=activity_bias)
    rendered = 0

    for frame_idx in range(start_frame, end_frame):
        ret, equirect = cap.read()
        if not ret:
            print(f"[render v7] Video ended at frame {frame_idx}")
            break

        frame_data    = frames_data[frame_idx] if frame_idx < len(frames_data) else {}
        smoothed      = frame_data.get("smoothed") or {}
        ball_yaw      = smoothed.get("yaw", 0.0)
        ball_pitch    = smoothed.get("pitch", 0.0)
        tracker_state = frame_data.get("tracker_state", "")
        best_score    = frame_data.get("best_score")

        confirmed = best_score is not None
        alpha = EMA_ALPHA_TRACKING if confirmed else EMA_ALPHA_LOSS

        if ema_yaw is None:
            ema_yaw, ema_pitch = ball_yaw, ball_pitch
            ema_yaw_ref = ball_yaw
        else:
            was_confirmed = prev_best_score is not None
            if confirmed and not was_confirmed:
                ema_yaw     = ball_yaw
                ema_pitch   = ball_pitch
                ema_yaw_ref = ball_yaw
            else:
                dyaw = ball_yaw - ema_yaw_ref
                if dyaw > 180:    ball_yaw -= 360
                elif dyaw < -180: ball_yaw += 360
                ema_yaw_ref = ball_yaw
                ema_yaw   = alpha * ball_yaw   + (1 - alpha) * ema_yaw
                ema_pitch = alpha * ball_pitch + (1 - alpha) * ema_pitch

        prev_best_score = best_score

        cam_yaw, cam_pitch, cam_fov, cam_roll, render_mode, zoom_t, hold_ctr, reacq_streak = fsm.update(
            ema_yaw, ema_pitch, tracker_state, best_score, frame_idx=frame_idx
        )

        # Lead offset only when fully in follow (fades out during zoom)
        lead = LEAD_DEG * (1.0 - zoom_t)
        final_cam_yaw = cam_yaw + lead

        # Wide target for HUD display (activity bias resolved value)
        disp_wide_yaw   = fsm._wide_yaw
        disp_wide_pitch = fsm._wide_pitch

        clean = extract_crop_frame(equirect, final_cam_yaw, cam_pitch, cam_fov,
                                   OUTPUT_W, OUTPUT_H, roll_deg=cam_roll)
        writer_clean.stdin.write(clean.tobytes())

        debug = draw_hud(clean, frame_data, frame_idx, fps,
                         final_cam_yaw, cam_pitch, cam_fov,
                         render_mode, zoom_t, hold_ctr, reacq_streak,
                         disp_wide_yaw, disp_wide_pitch, fallback_fov)

        inset = draw_equirect_inset(equirect, frame_data, INSET_W, INSET_H)
        iy = OUTPUT_H - INSET_H - INSET_Y_OFF
        debug[iy:iy+INSET_H, INSET_X:INSET_X+INSET_W] = inset

        writer_debug.stdin.write(debug.tobytes())

        rendered += 1
        if rendered % 100 == 0:
            print(f"[render v7] frame {frame_idx}  mode={render_mode}  "
                  f"cam=({final_cam_yaw:.1f},{cam_pitch:.1f})  fov={cam_fov:.1f}  t={zoom_t:.2f}")

    cap.release()
    writer_clean.stdin.close()
    writer_debug.stdin.close()
    writer_clean.wait()
    writer_debug.wait()
    print(f"[render v7] Done. {rendered} frames rendered.")
    print(f"[render v7] Clean : {output_clean}")
    print(f"[render v7] Debug : {output_debug}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",           default="equirect_trim.mp4")
    parser.add_argument("--tracking",        default="tracking.json")
    parser.add_argument("--activity",        default=None,
                        help="Path to activity.json for wide fallback bias (optional)")
    parser.add_argument("--start-frame",     type=int,   default=700)
    parser.add_argument("--end-frame",       type=int,   default=1300)
    parser.add_argument("--output-clean",    default="render_clean.mp4")
    parser.add_argument("--output-debug",    default="render_debug.mp4")
    parser.add_argument("--fallback-yaw",    type=float, default=FALLBACK_YAW)
    parser.add_argument("--fallback-pitch",  type=float, default=FALLBACK_PITCH)
    parser.add_argument("--fallback-fov",    type=float, default=FALLBACK_FOV)
    parser.add_argument("--fallback-roll",   type=float, default=FALLBACK_ROLL)
    args = parser.parse_args()

    render_segment(
        equirect_path=args.input,
        tracking_path=args.tracking,
        start_frame=args.start_frame,
        end_frame=args.end_frame,
        output_clean=args.output_clean,
        output_debug=args.output_debug,
        fallback_yaw=args.fallback_yaw,
        fallback_pitch=args.fallback_pitch,
        fallback_fov=args.fallback_fov,
        fallback_roll=args.fallback_roll,
        activity_path=args.activity,
    )


