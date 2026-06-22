#!/usr/bin/env python3
"""
FFA 360 Render Segment — v6
============================
Pure render step. Reads tracking.json (pre-computed), renders a frame window
as two outputs:
  - render_clean.mp4  : follow-cam output, no overlays
  - render_debug.mp4  : same + HUD overlay + equirect inset with ball marker

v5: Smooth zoom-out fallback — no hard cut.
    When ball confidence falls, the camera behaves like a human operator:
      - holds the last follow direction briefly
      - continuously widens FOV from follow FOV (90°) toward FALLBACK_FOV
      - gradually lifts pitch toward FALLBACK_PITCH
      - gently steers yaw toward FALLBACK_YAW (pitch centre)
    All driven by a single t (0→1) over FALLBACK_ZOOM_FRAMES frames.
    On reacquisition: t reverses back to 0 (zoom-in), EMA snaps, FOLLOW resumes.
    One consistent perspective projection throughout — no projection swap.

Render modes:
  FOLLOW        — confirmed ball, EMA follow-cam, FOV=OUTPUT_FOV
  ZOOMING_OUT   — ball lost, t advancing 0→1, FOV/pitch/yaw animating smoothly
  WIDE_HOLD     — t=1, camera settled at wide overview, no ball
  ZOOMING_IN    — ball reacquired, t reversing 1→0, camera returning to follow
  (FOLLOW is re-entered when t reaches 0 and EMA is snapped)

Config (overridable via CLI):
  FALLBACK_YAW          — wide view target yaw
  FALLBACK_PITCH        — wide view target pitch (lift upward, try +5 / +8)
  FALLBACK_FOV          — wide view target FOV (degrees) — 120° default
  HOLD_BEFORE_ZOOM      — frames of hold before zoom begins
  FALLBACK_ZOOM_FRAMES  — frames over which zoom-out animation completes (~1–1.5s)
  REACQUIRE_MIN_FRAMES  — consecutive confirmed frames before zoom-in begins
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
# Render states
# ---------------------------------------------------------------------------
RENDER_FOLLOW      = "FOLLOW"
RENDER_ZOOMING_OUT = "ZOOMING_OUT"
RENDER_WIDE_HOLD   = "WIDE_HOLD"
RENDER_ZOOMING_IN  = "ZOOMING_IN"

STATE_COLORS = {
    "TRACKING":        (0, 180, 0),
    "UNCERTAIN":       (0, 160, 200),
    "LOST":            (0, 0, 200),
    "WARMING_UP":      (200, 160, 0),
    "UNINITIALIZED":   (80, 80, 80),
    RENDER_FOLLOW:     (0, 180, 0),
    RENDER_ZOOMING_OUT:(30, 120, 220),
    RENDER_WIDE_HOLD:  (30, 30, 180),
    RENDER_ZOOMING_IN: (0, 210, 140),
}


# ---------------------------------------------------------------------------
# Easing
# ---------------------------------------------------------------------------
def ease_inout(t):
    """Smooth cubic ease-in-out so zoom feels organic, not mechanical."""
    return t * t * (3.0 - 2.0 * t)


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
    """

    def __init__(self, fallback_yaw, fallback_pitch, fallback_fov, fallback_roll):
        self.mode              = RENDER_FOLLOW
        self.hold_counter      = 0
        self.reacquire_streak  = 0
        self.zoom_t            = 0.0   # 0 = full follow, 1 = full wide
        # Anchor poses for animation
        self.zoom_start_yaw    = 0.0   # yaw/pitch/fov/roll at moment zoom-out began
        self.zoom_start_pitch  = 0.0
        self.zoom_start_fov    = OUTPUT_FOV
        self.zoom_start_roll   = 0.0   # always 0 when leaving FOLLOW
        # Fallback targets
        self.fallback_yaw      = fallback_yaw
        self.fallback_pitch    = fallback_pitch
        self.fallback_fov      = fallback_fov
        self.fallback_roll     = fallback_roll

    def _interp_pose(self, t):
        """Interpolate from zoom-start pose to wide pose using eased t."""
        et = ease_inout(t)
        yaw   = lerp_yaw(self.zoom_start_yaw,   self.fallback_yaw,   et)
        pitch = lerp_val(self.zoom_start_pitch,  self.fallback_pitch, et)
        fov   = lerp_val(self.zoom_start_fov,    self.fallback_fov,   et)
        roll  = lerp_val(self.zoom_start_roll,   self.fallback_roll,  et)
        return yaw, pitch, fov, roll

    def update(self, ema_yaw, ema_pitch, tracker_state, best_score):
        confirmed = (best_score is not None)
        dt_out = 1.0 / FALLBACK_ZOOM_FRAMES
        dt_in  = 1.0 / FALLBACK_ZOOM_FRAMES  # same speed back

        if self.mode == RENDER_FOLLOW:
            if confirmed:
                self.hold_counter     = 0
                self.reacquire_streak = 0
                self.zoom_t           = 0.0
                return ema_yaw, ema_pitch, OUTPUT_FOV, 0.0, RENDER_FOLLOW, 0.0, 0, 0
            else:
                self.hold_counter += 1
                if self.hold_counter >= HOLD_BEFORE_ZOOM:
                    # Begin zoom-out from current EMA position
                    self.mode             = RENDER_ZOOMING_OUT
                    self.zoom_t           = 0.0
                    self.zoom_start_yaw   = ema_yaw
                    self.zoom_start_pitch = ema_pitch
                    self.zoom_start_fov   = OUTPUT_FOV
                    self.zoom_start_roll  = 0.0
                    self.reacquire_streak = 0
                return ema_yaw, ema_pitch, OUTPUT_FOV, 0.0, RENDER_FOLLOW, 0.0, self.hold_counter, 0

        elif self.mode == RENDER_ZOOMING_OUT:
            self.zoom_t = min(1.0, self.zoom_t + dt_out)
            yaw, pitch, fov, roll = self._interp_pose(self.zoom_t)
            if confirmed:
                self.reacquire_streak += 1
            else:
                self.reacquire_streak = 0
            if self.zoom_t >= 1.0:
                self.mode = RENDER_WIDE_HOLD
            return yaw, pitch, fov, roll, RENDER_ZOOMING_OUT, self.zoom_t, self.hold_counter, self.reacquire_streak

        elif self.mode == RENDER_WIDE_HOLD:
            yaw, pitch, fov, roll = self._interp_pose(1.0)
            if confirmed:
                self.reacquire_streak += 1
                if self.reacquire_streak >= REACQUIRE_MIN_FRAMES:
                    self.mode             = RENDER_ZOOMING_IN
                    # Anchor zoom-in FROM current wide pose
                    self.zoom_start_yaw   = yaw
                    self.zoom_start_pitch = pitch
                    self.zoom_start_fov   = fov
                    self.zoom_start_roll  = roll
            else:
                self.reacquire_streak = 0
            return yaw, pitch, fov, roll, RENDER_WIDE_HOLD, 1.0, self.hold_counter, self.reacquire_streak

        elif self.mode == RENDER_ZOOMING_IN:
            if confirmed:
                self.reacquire_streak += 1
                self.zoom_t = max(0.0, self.zoom_t - dt_in)
                # Interpolate from wide back toward live EMA (update target each frame)
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
                    return ema_yaw, ema_pitch, OUTPUT_FOV, 0.0, RENDER_FOLLOW, 0.0, 0, 0
                return yaw, pitch, fov, roll, RENDER_ZOOMING_IN, self.zoom_t, self.hold_counter, self.reacquire_streak
            else:
                # Lost again — zoom back out from current position
                cur_yaw, cur_pitch, cur_fov, roll = self._interp_pose(self.zoom_t)
                self.mode             = RENDER_ZOOMING_OUT
                self.zoom_start_yaw   = cur_yaw
                self.zoom_start_pitch = cur_pitch
                self.zoom_start_fov   = cur_fov
                self.zoom_start_roll  = roll
                self.reacquire_streak = 0
                return cur_yaw, cur_pitch, cur_fov, roll, RENDER_ZOOMING_OUT, self.zoom_t, self.hold_counter, 0

        return ema_yaw, ema_pitch, OUTPUT_FOV, 0.0, RENDER_FOLLOW, 0.0, 0, 0


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------
def render_segment(equirect_path, tracking_path, start_frame, end_frame,
                   output_clean, output_debug,
                   fallback_yaw, fallback_pitch, fallback_fov, fallback_roll):

    print("[render v6] Loading tracking.json...")
    with open(tracking_path) as f:
        tracking = json.load(f)

    fps           = float(tracking.get("fps", 29.97))
    frames_data   = tracking.get("frames", [])
    total_tracked = len(frames_data)
    zoom_secs     = FALLBACK_ZOOM_FRAMES / fps
    print(f"[render v6] {total_tracked} frames @ {fps:.2f} fps")
    print(f"[render v6] Rendering frames {start_frame}–{end_frame}")
    print(f"[render v6] Zoom: {FALLBACK_ZOOM_FRAMES} frames ({zoom_secs:.2f}s)  "
          f"hold={HOLD_BEFORE_ZOOM}fr")
    print(f"[render v6] Wide target: yaw={fallback_yaw}° pitch={fallback_pitch}° "
          f"fov={fallback_fov}° roll={fallback_roll}°")

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

    fsm = SmoothZoomFallbackFSM(fallback_yaw, fallback_pitch, fallback_fov, fallback_roll)
    rendered = 0

    for frame_idx in range(start_frame, end_frame):
        ret, equirect = cap.read()
        if not ret:
            print(f"[render v6] Video ended at frame {frame_idx}")
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
            ema_yaw, ema_pitch, tracker_state, best_score
        )

        # Lead offset only when fully in follow (fades out during zoom)
        lead = LEAD_DEG * (1.0 - zoom_t)
        final_cam_yaw = cam_yaw + lead

        clean = extract_crop_frame(equirect, final_cam_yaw, cam_pitch, cam_fov,
                                   OUTPUT_W, OUTPUT_H, roll_deg=cam_roll)
        writer_clean.stdin.write(clean.tobytes())

        debug = draw_hud(clean, frame_data, frame_idx, fps,
                         final_cam_yaw, cam_pitch, cam_fov,
                         render_mode, zoom_t, hold_ctr, reacq_streak,
                         fallback_yaw, fallback_pitch, fallback_fov)

        inset = draw_equirect_inset(equirect, frame_data, INSET_W, INSET_H)
        iy = OUTPUT_H - INSET_H - INSET_Y_OFF
        debug[iy:iy+INSET_H, INSET_X:INSET_X+INSET_W] = inset

        writer_debug.stdin.write(debug.tobytes())

        rendered += 1
        if rendered % 100 == 0:
            print(f"[render v6] frame {frame_idx}  mode={render_mode}  "
                  f"cam=({final_cam_yaw:.1f},{cam_pitch:.1f})  fov={cam_fov:.1f}  t={zoom_t:.2f}")

    cap.release()
    writer_clean.stdin.close()
    writer_debug.stdin.close()
    writer_clean.wait()
    writer_debug.wait()
    print(f"[render v6] Done. {rendered} frames rendered.")
    print(f"[render v6] Clean : {output_clean}")
    print(f"[render v6] Debug : {output_debug}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",           default="equirect_trim.mp4")
    parser.add_argument("--tracking",        default="tracking.json")
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
    )

