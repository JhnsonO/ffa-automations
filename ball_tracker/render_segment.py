#!/usr/bin/env python3
"""
FFA 360 Render Segment — v4
============================
Pure render step. Reads tracking.json (pre-computed), renders a frame window
as two outputs:
  - render_clean.mp4  : follow-cam / equirect-band panoramic fallback, no overlays
  - render_debug.mp4  : same + HUD overlay + equirect inset with ball marker

v4: WIDE_FALLBACK mode uses a fixed equirectangular band crop (numpy remap,
no ffmpeg v360) instead of a perspective wide-FOV crop.
    - Configurable per venue: fallback yaw, pitch, horizontal span, vertical span
    - Default profile: yaw=0°, pitch=0°, h_span=160°, v_span=45°
    - Output is letterboxed to OUTPUT_W × OUTPUT_H with black bars top/bottom

Render modes:
  FOLLOW        — Tracker confirmed ball this frame → EMA follow-cam (perspective)
  WIDE_FALLBACK — Tracker UNCERTAIN/LOST → fixed equirect panoramic band
  REACQUIRE     — Tracker returns confirmed ball for REACQUIRE_MIN_FRAMES
                  consecutive frames → fast lerp back to follow-cam + EMA reset

Config (overridable via CLI):
  FALLBACK_YAW     — centre yaw of panoramic band (degrees)
  FALLBACK_PITCH   — centre pitch of panoramic band (degrees)
  FALLBACK_H_SPAN  — horizontal span of panoramic band (degrees)
  FALLBACK_V_SPAN  — vertical span of panoramic band (degrees)
  HOLD_BEFORE_FALLBACK   — frames to hold last pose before switching to band
  REACQUIRE_MIN_FRAMES   — consecutive confirmed frames before exiting fallback
  FALLBACK_LERP_ALPHA    — unused in v4 (band is fixed, no drift needed)
  REACQUIRE_LERP_ALPHA   — snap rate back to follow-cam on reacquisition

No detection, no Kalman — render-only step.

Usage:
  python3 render_segment.py \\
    --input equirect_trim.mp4 \\
    --tracking tracking.json \\
    --start-frame 700 \\
    --end-frame 1300 \\
    --output-clean render_clean.mp4 \\
    --output-debug render_debug.mp4 \\
    --fallback-yaw 0 \\
    --fallback-pitch 0 \\
    --fallback-h-span 160 \\
    --fallback-v-span 45
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
OUTPUT_FOV   = 90          # follow-cam FOV (degrees)
LEAD_DEG     = 3.0         # camera leads ball by this much in yaw

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
# v4: Wide fallback config — equirect band panoramic
# Overridden by CLI args; these are the defaults for this camera profile.
# ---------------------------------------------------------------------------
FALLBACK_YAW             = 0.0     # band centre yaw (degrees)
FALLBACK_PITCH           = 0.0     # band centre pitch (degrees)
FALLBACK_H_SPAN          = 160.0   # horizontal angular span (degrees)
FALLBACK_V_SPAN          = 45.0    # vertical angular span (degrees)

HOLD_BEFORE_FALLBACK     = 30      # frames to hold last good pose before entering band
REACQUIRE_MIN_FRAMES     = 5       # consecutive confirmed frames before exiting fallback
REACQUIRE_LERP_ALPHA     = 0.20    # snap rate back to follow-cam on reacquisition

EMA_ALPHA_TRACKING       = 0.18
EMA_ALPHA_LOSS           = 0.08

# ---------------------------------------------------------------------------
# Render states
# ---------------------------------------------------------------------------
RENDER_FOLLOW        = "FOLLOW"
RENDER_WIDE_FALLBACK = "WIDE_FALLBACK"
RENDER_REACQUIRE     = "REACQUIRE"

STATE_COLORS = {
    "TRACKING":           (0, 180, 0),
    "UNCERTAIN":          (0, 160, 200),
    "LOST":               (0, 0, 200),
    "WARMING_UP":         (200, 160, 0),
    "UNINITIALIZED":      (80, 80, 80),
    RENDER_FOLLOW:        (0, 180, 0),
    RENDER_WIDE_FALLBACK: (30, 30, 220),
    RENDER_REACQUIRE:     (0, 210, 140),
}


# ---------------------------------------------------------------------------
# Geometry — perspective crop (FOLLOW / REACQUIRE)
# ---------------------------------------------------------------------------
def extract_crop_frame(equirect_frame, yaw_deg, pitch_deg, fov_deg, out_w, out_h):
    h_eq, w_eq = equirect_frame.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_w / 2.0) / f
    ry = -(yv - out_h / 2.0) / f
    rz = np.ones_like(rx)
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


# ---------------------------------------------------------------------------
# Geometry — equirect band crop (WIDE_FALLBACK)
# Slices a yaw×pitch window straight from the equirectangular image.
# No perspective distortion. Output letterboxed to out_w × out_h.
# ---------------------------------------------------------------------------
def extract_equirect_band(equirect_frame, centre_yaw_deg, centre_pitch_deg,
                           h_span_deg, v_span_deg, out_w, out_h):
    h_eq, w_eq = equirect_frame.shape[:2]

    # Pixel extents of the band in the equirect
    band_w = int(round(w_eq * h_span_deg / 360.0))
    band_h = int(round(h_eq * v_span_deg / 180.0))
    band_w = max(1, min(band_w, w_eq))
    band_h = max(1, min(band_h, h_eq))

    # Centre pixel in equirect
    cx = int(((centre_yaw_deg / 360.0) + 0.5) % 1.0 * w_eq)
    cy = int((0.5 - centre_pitch_deg / 180.0) * h_eq)
    cy = max(band_h // 2, min(h_eq - band_h // 2, cy))

    x0 = cx - band_w // 2
    x1 = x0 + band_w
    y0 = cy - band_h // 2
    y1 = y0 + band_h

    # Horizontal wrap
    if x0 >= 0 and x1 <= w_eq:
        band = equirect_frame[y0:y1, x0:x1]
    else:
        # Wrap around seam
        x0_w = x0 % w_eq
        cols_left  = w_eq - x0_w
        cols_right = band_w - cols_left
        left_strip  = equirect_frame[y0:y1, x0_w:]
        right_strip = equirect_frame[y0:y1, :cols_right]
        band = np.concatenate([left_strip, right_strip], axis=1)

    # Resize band to fill output width; letterbox height
    scale = out_w / band_w
    render_h = int(round(band_h * scale))
    render_h = min(render_h, out_h)

    resized = cv2.resize(band, (out_w, render_h), interpolation=cv2.INTER_LINEAR)

    # Letterbox onto black canvas
    canvas = np.zeros((out_h, out_w, 3), dtype=np.uint8)
    y_off  = (out_h - render_h) // 2
    canvas[y_off:y_off + render_h, :] = resized
    return canvas


def yaw_pitch_to_equirect_pixel(yaw_deg, pitch_deg, w, h):
    x = int(((yaw_deg / 360.0) + 0.5) % 1.0 * w)
    y = int((0.5 - pitch_deg / 180.0) * h)
    return x, y


def lerp_yaw(current, target, alpha):
    diff = (target - current + 540) % 360 - 180
    return current + alpha * diff


def lerp_pitch(current, target, alpha):
    return current + alpha * (target - current)


# ---------------------------------------------------------------------------
# HUD helpers
# ---------------------------------------------------------------------------
def draw_text_shadowed(img, text, pos, scale, color, thick, shadow=HUD_SHADOW):
    x, y = pos
    cv2.putText(img, text, (x+1, y+1), HUD_FONT, scale, shadow, thick + 1, cv2.LINE_AA)
    cv2.putText(img, text, (x, y),     HUD_FONT, scale, color,  thick,     cv2.LINE_AA)


def draw_hud(frame, frame_data, frame_idx, fps, cam_yaw, cam_pitch, cam_fov,
             render_mode, hold_counter, reacquire_streak,
             fallback_yaw, fallback_pitch, fallback_h_span, fallback_v_span):
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

    if render_mode == RENDER_WIDE_FALLBACK:
        draw_text_shadowed(out,
                           f"BAND  yaw={fallback_yaw:.0f}°  pitch={fallback_pitch:.0f}°  "
                           f"h={fallback_h_span:.0f}°  v={fallback_v_span:.0f}°",
                           (10, 106), HUD_SCALE, (80, 160, 255), HUD_THICK)
    else:
        draw_text_shadowed(out,
                           f"Cam  yaw={cam_yaw:.1f}  pitch={cam_pitch:.1f}  fov={cam_fov:.0f}",
                           (10, 106), HUD_SCALE, HUD_COLOR, HUD_THICK)

    smoothed   = frame_data.get("smoothed") or {}
    ball_yaw   = smoothed.get("yaw", "?")
    ball_pitch = smoothed.get("pitch", "?")
    best_score = frame_data.get("best_score")
    score_str  = f"{best_score:.3f}" if best_score is not None else "—"
    draw_text_shadowed(out,
                       f"Ball yaw={ball_yaw}  pitch={ball_pitch}  score={score_str}",
                       (10, 134), HUD_SCALE, HUD_COLOR, HUD_THICK)

    if render_mode == RENDER_WIDE_FALLBACK:
        draw_text_shadowed(out,
                           f"hold_counter={hold_counter}  (fallback after {HOLD_BEFORE_FALLBACK}fr)",
                           (10, 162), HUD_SCALE, (80, 160, 255), HUD_THICK)
    elif render_mode == RENDER_REACQUIRE:
        draw_text_shadowed(out,
                           f"reacquire_streak={reacquire_streak}/{REACQUIRE_MIN_FRAMES}",
                           (10, 162), HUD_SCALE, (0, 220, 140), HUD_THICK)

    dets = frame_data.get("detections", [])
    draw_text_shadowed(out, f"Detections: {len(dets)}",
                       (10, 190), HUD_SCALE, HUD_COLOR, HUD_THICK)

    # Crosshair — grey in fallback, cyan in follow/reacquire
    ball_offset_px = int(LEAD_DEG / OUTPUT_FOV * w)
    cx = w // 2 - ball_offset_px
    cy_mid = h // 2
    ch_color = (128, 128, 128) if render_mode == RENDER_WIDE_FALLBACK else (0, 255, 255)
    cv2.circle(out, (cx, cy_mid), 18, ch_color, 2)
    cv2.circle(out, (cx, cy_mid), 4,  ch_color, -1)
    cv2.line(out, (cx - 28, cy_mid), (cx + 28, cy_mid), ch_color, 1)
    cv2.line(out, (cx, cy_mid - 28), (cx, cy_mid + 28), ch_color, 1)

    return out


def draw_equirect_inset(equirect_frame, frame_data, inset_w, inset_h):
    inset = cv2.resize(equirect_frame, (inset_w, inset_h))
    smoothed = frame_data.get("smoothed") or {}
    yaw   = smoothed.get("yaw")
    pitch = smoothed.get("pitch")
    if yaw is not None and pitch is not None:
        px, py = yaw_pitch_to_equirect_pixel(yaw, pitch, inset_w, inset_h)
        px = max(5, min(inset_w - 5, px))
        py = max(5, min(inset_h - 5, py))
        cv2.circle(inset, (px, py), 8, (0, 255, 255), 2)
        cv2.circle(inset, (px, py), 2, (0, 255, 255), -1)
    for det in frame_data.get("detections", []):
        dx, dy = yaw_pitch_to_equirect_pixel(det["yaw"], det["pitch"], inset_w, inset_h)
        dx = max(3, min(inset_w - 3, dx))
        dy = max(3, min(inset_h - 3, dy))
        cv2.circle(inset, (dx, dy), 4, (0, 80, 255), -1)
    cv2.rectangle(inset, (0, 0), (inset_w - 1, inset_h - 1), (200, 200, 200), 1)
    return inset


# ---------------------------------------------------------------------------
# v4 Tracker-State-Aware Fallback FSM
# ---------------------------------------------------------------------------
class WideAreaFallbackFSM:
    """
    FOLLOW        : tracker confirmed ball → EMA follow-cam (perspective)
    WIDE_FALLBACK : tracker not confirmed for HOLD_BEFORE_FALLBACK frames
                    → fixed equirect band panoramic (no lerp needed — it's static)
    REACQUIRE     : tracker confirms ball for REACQUIRE_MIN_FRAMES consecutive frames
                    → fast lerp back to follow-cam perspective, then snap EMA
    """

    def __init__(self, fallback_yaw, fallback_pitch, fallback_h_span, fallback_v_span):
        self.mode              = RENDER_FOLLOW
        self.hold_counter      = 0
        self.reacquire_streak  = 0
        # Reacquire lerp pose (perspective, interpolates from band back to follow)
        self.reacq_yaw         = fallback_yaw
        self.reacq_pitch       = fallback_pitch
        self.reacq_fov         = OUTPUT_FOV
        # Fallback config (fixed per venue)
        self.fallback_yaw      = fallback_yaw
        self.fallback_pitch    = fallback_pitch
        self.fallback_h_span   = fallback_h_span
        self.fallback_v_span   = fallback_v_span

    def update(self, ema_yaw, ema_pitch, tracker_state, best_score):
        """
        Returns (cam_yaw, cam_pitch, cam_fov, render_mode, hold_counter, reacquire_streak)
        cam_yaw/pitch/fov are only used in FOLLOW and REACQUIRE (perspective).
        In WIDE_FALLBACK the caller uses self.fallback_* directly.
        """
        confirmed = (best_score is not None)

        if self.mode == RENDER_FOLLOW:
            if confirmed:
                self.hold_counter     = 0
                self.reacquire_streak = 0
                return ema_yaw, ema_pitch, OUTPUT_FOV, RENDER_FOLLOW, 0, 0
            else:
                self.hold_counter += 1
                if self.hold_counter >= HOLD_BEFORE_FALLBACK:
                    self.mode             = RENDER_WIDE_FALLBACK
                    self.reacquire_streak = 0
                    # Prime reacquire lerp pose at fallback centre so transition
                    # starts from the right place when ball returns
                    self.reacq_yaw   = self.fallback_yaw
                    self.reacq_pitch = self.fallback_pitch
                    self.reacq_fov   = OUTPUT_FOV
                return ema_yaw, ema_pitch, OUTPUT_FOV, RENDER_FOLLOW, self.hold_counter, 0

        elif self.mode == RENDER_WIDE_FALLBACK:
            if confirmed:
                self.reacquire_streak += 1
                if self.reacquire_streak >= REACQUIRE_MIN_FRAMES:
                    self.mode         = RENDER_REACQUIRE
                    self.hold_counter = 0
            else:
                self.reacquire_streak = 0
            # Band is fixed — cam_yaw/pitch/fov returned here are unused by caller in this mode
            return (self.fallback_yaw, self.fallback_pitch, OUTPUT_FOV,
                    RENDER_WIDE_FALLBACK, self.hold_counter, self.reacquire_streak)

        elif self.mode == RENDER_REACQUIRE:
            if confirmed:
                self.reacquire_streak += 1
                # Fast lerp reacq pose toward live EMA
                self.reacq_yaw   = lerp_yaw(self.reacq_yaw,   ema_yaw,    REACQUIRE_LERP_ALPHA)
                self.reacq_pitch = lerp_pitch(self.reacq_pitch, ema_pitch, REACQUIRE_LERP_ALPHA)
                self.reacq_fov   = lerp_pitch(self.reacq_fov,  OUTPUT_FOV, REACQUIRE_LERP_ALPHA)

                dist_yaw   = abs(((self.reacq_yaw - ema_yaw + 540) % 360) - 180)
                dist_pitch = abs(self.reacq_pitch - ema_pitch)
                if dist_yaw < 3.0 and dist_pitch < 3.0:
                    self.mode             = RENDER_FOLLOW
                    self.hold_counter     = 0
                    self.reacquire_streak = 0
                    return ema_yaw, ema_pitch, OUTPUT_FOV, RENDER_FOLLOW, 0, 0

                return (self.reacq_yaw, self.reacq_pitch, self.reacq_fov,
                        RENDER_REACQUIRE, self.hold_counter, self.reacquire_streak)
            else:
                # Lost again during reacquire — back to band
                self.mode             = RENDER_WIDE_FALLBACK
                self.reacquire_streak = 0
                return (self.fallback_yaw, self.fallback_pitch, OUTPUT_FOV,
                        RENDER_WIDE_FALLBACK, self.hold_counter, 0)

        return ema_yaw, ema_pitch, OUTPUT_FOV, RENDER_FOLLOW, 0, 0


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------
def render_segment(equirect_path, tracking_path, start_frame, end_frame,
                   output_clean, output_debug,
                   fallback_yaw, fallback_pitch, fallback_h_span, fallback_v_span):

    print("[render v4] Loading tracking.json...")
    with open(tracking_path) as f:
        tracking = json.load(f)

    fps         = float(tracking.get("fps", 29.97))
    frames_data = tracking.get("frames", [])
    total_tracked = len(frames_data)
    print(f"[render v4] {total_tracked} frames @ {fps:.2f} fps")
    print(f"[render v4] Rendering frames {start_frame}–{end_frame}")
    print(f"[render v4] Fallback band: yaw={fallback_yaw}° pitch={fallback_pitch}° "
          f"h={fallback_h_span}° v={fallback_v_span}°")

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

    fsm = WideAreaFallbackFSM(fallback_yaw, fallback_pitch, fallback_h_span, fallback_v_span)
    rendered = 0

    for frame_idx in range(start_frame, end_frame):
        ret, equirect = cap.read()
        if not ret:
            print(f"[render v4] Video ended at frame {frame_idx}")
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
                ema_yaw   = ball_yaw
                ema_pitch = ball_pitch
                ema_yaw_ref = ball_yaw
            else:
                dyaw = ball_yaw - ema_yaw_ref
                if dyaw > 180:   ball_yaw -= 360
                elif dyaw < -180: ball_yaw += 360
                ema_yaw_ref = ball_yaw
                ema_yaw   = alpha * ball_yaw   + (1 - alpha) * ema_yaw
                ema_pitch = alpha * ball_pitch + (1 - alpha) * ema_pitch

        prev_best_score = best_score

        cam_yaw, cam_pitch, cam_fov, render_mode, hold_ctr, reacq_streak = fsm.update(
            ema_yaw, ema_pitch, tracker_state, best_score
        )

        # Render frame
        if render_mode == RENDER_WIDE_FALLBACK:
            clean = extract_equirect_band(equirect,
                                          fallback_yaw, fallback_pitch,
                                          fallback_h_span, fallback_v_span,
                                          OUTPUT_W, OUTPUT_H)
        else:
            final_cam_yaw = cam_yaw + LEAD_DEG
            clean = extract_crop_frame(equirect, final_cam_yaw, cam_pitch, cam_fov,
                                       OUTPUT_W, OUTPUT_H)

        writer_clean.stdin.write(clean.tobytes())

        # Debug render
        if render_mode == RENDER_WIDE_FALLBACK:
            display_yaw, display_pitch, display_fov = fallback_yaw, fallback_pitch, OUTPUT_FOV
        else:
            display_yaw, display_pitch, display_fov = cam_yaw + LEAD_DEG, cam_pitch, cam_fov

        debug = draw_hud(clean, frame_data, frame_idx, fps,
                         display_yaw, display_pitch, display_fov,
                         render_mode, hold_ctr, reacq_streak,
                         fallback_yaw, fallback_pitch, fallback_h_span, fallback_v_span)

        if render_mode == RENDER_REACQUIRE and confirmed and (reacq_streak == REACQUIRE_MIN_FRAMES):
            draw_text_shadowed(debug, "*** REACQUIRE ***",
                               (OUTPUT_W // 2 - 160, OUTPUT_H // 2),
                               HUD_SCALE * 1.4, (0, 220, 140), HUD_THICK + 1)

        inset = draw_equirect_inset(equirect, frame_data, INSET_W, INSET_H)
        iy = OUTPUT_H - INSET_H - INSET_Y_OFF
        debug[iy:iy + INSET_H, INSET_X:INSET_X + INSET_W] = inset

        writer_debug.stdin.write(debug.tobytes())

        rendered += 1
        if rendered % 100 == 0:
            print(f"[render v4] frame {frame_idx}  mode={render_mode}  "
                  f"cam=({display_yaw:.1f},{display_pitch:.1f})")

    cap.release()
    writer_clean.stdin.close()
    writer_debug.stdin.close()
    writer_clean.wait()
    writer_debug.wait()
    print(f"[render v4] Done. {rendered} frames rendered.")
    print(f"[render v4] Clean : {output_clean}")
    print(f"[render v4] Debug : {output_debug}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",            default="equirect_trim.mp4")
    parser.add_argument("--tracking",         default="tracking.json")
    parser.add_argument("--start-frame",      type=int,   default=700)
    parser.add_argument("--end-frame",        type=int,   default=1300)
    parser.add_argument("--output-clean",     default="render_clean.mp4")
    parser.add_argument("--output-debug",     default="render_debug.mp4")
    parser.add_argument("--fallback-yaw",     type=float, default=FALLBACK_YAW)
    parser.add_argument("--fallback-pitch",   type=float, default=FALLBACK_PITCH)
    parser.add_argument("--fallback-h-span",  type=float, default=FALLBACK_H_SPAN)
    parser.add_argument("--fallback-v-span",  type=float, default=FALLBACK_V_SPAN)
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
        fallback_h_span=args.fallback_h_span,
        fallback_v_span=args.fallback_v_span,
    )
