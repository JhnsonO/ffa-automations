#!/usr/bin/env python3
"""
FFA 360 Render Segment — v3
============================
Pure render step. Reads tracking.json (pre-computed), renders a frame window
as two outputs:
  - render_clean.mp4  : 16:9 follow-cam, no overlays
  - render_debug.mp4  : follow-cam + HUD overlay + equirect inset with ball marker

v3: Replaces displacement-based StaticFallbackFSM with a tracker-state-aware
wide playable-area fallback FSM.

Render modes:
  FOLLOW        — Tracker confirmed ball this frame → EMA follow-cam
  WIDE_FALLBACK — Tracker UNCERTAIN/LOST, or no confirmed ball for
                  HOLD_BEFORE_FALLBACK frames → smooth lerp to configured
                  wide pitch-facing view
  REACQUIRE     — Tracker returns confirmed ball for REACQUIRE_MIN_FRAMES
                  consecutive frames → fast lerp back to follow-cam + EMA reset

Config:
  FALLBACK_YAW, FALLBACK_PITCH, FALLBACK_FOV  — manual wide view (v1)
  HOLD_BEFORE_FALLBACK                         — frames to hold last pose before drifting
  REACQUIRE_MIN_FRAMES                         — confirmed frames needed to exit fallback
  FALLBACK_LERP_ALPHA                          — drift rate into wide view per frame
  REACQUIRE_LERP_ALPHA                         — snap rate back to follow-cam per frame

No detection, no Kalman — render-only step.

Usage:
  python3 render_segment.py \\
    --input equirect_trim.mp4 \\
    --tracking tracking.json \\
    --start-frame 800 \\
    --end-frame 1000 \\
    --output-clean render_clean.mp4 \\
    --output-debug render_debug.mp4
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
# v3: Tracker-state-aware wide fallback config
# ---------------------------------------------------------------------------
FALLBACK_YAW             = 0.0    # wide view centre yaw (degrees) — point at pitch centre
FALLBACK_PITCH           = -5.0   # wide view centre pitch (degrees)
FALLBACK_FOV             = 100.0  # wide view FOV — broader than follow-cam
HOLD_BEFORE_FALLBACK     = 30     # frames to hold last good pose before drifting to wide
REACQUIRE_MIN_FRAMES     = 5      # consecutive confirmed frames before exiting fallback
FALLBACK_LERP_ALPHA      = 0.03   # drift rate toward wide view per frame (slow, smooth)
REACQUIRE_LERP_ALPHA     = 0.20   # snap rate back to follow-cam on reacquisition

EMA_ALPHA_TRACKING       = 0.18
EMA_ALPHA_LOSS           = 0.08

# ---------------------------------------------------------------------------
# Render states
# ---------------------------------------------------------------------------
RENDER_FOLLOW        = "FOLLOW"
RENDER_WIDE_FALLBACK = "WIDE_FALLBACK"
RENDER_REACQUIRE     = "REACQUIRE"

STATE_COLORS = {
    # Tracker states → bar colour
    "TRACKING":         (0, 180, 0),
    "UNCERTAIN":        (0, 160, 200),
    "LOST":             (0, 0, 200),
    "WARMING_UP":       (200, 160, 0),
    "UNINITIALIZED":    (80, 80, 80),
    # Render mode → bar colour
    RENDER_FOLLOW:        (0, 180, 0),
    RENDER_WIDE_FALLBACK: (30, 30, 220),
    RENDER_REACQUIRE:     (0, 210, 140),
}


# ---------------------------------------------------------------------------
# Geometry
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


def yaw_pitch_to_equirect_pixel(yaw_deg, pitch_deg, w, h):
    x = int(((yaw_deg / 360.0) + 0.5) % 1.0 * w)
    y = int((0.5 - pitch_deg / 180.0) * h)
    return x, y


def lerp_yaw(current, target, alpha):
    """Lerp yaw taking shortest path across 360° seam."""
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
             render_mode, hold_counter, reacquire_streak):
    out = frame.copy()
    h, w = out.shape[:2]

    # State bar — show render mode prominently
    tracker_state = frame_data.get("tracker_state", "?")
    loss_state    = frame_data.get("loss_state", "")
    bar_color = STATE_COLORS.get(render_mode, (80, 80, 80))
    cv2.rectangle(out, (0, 0), (w, 44), bar_color, -1)
    cv2.rectangle(out, (0, 0), (w, 44), (0, 0, 0), 2)
    label = f"RENDER: {render_mode}   |   TRACKER: {tracker_state}   {loss_state}"
    draw_text_shadowed(out, label, (10, 30), HUD_SCALE * 1.05, (0, 0, 0), HUD_THICK)

    # Frame / time
    t_sec = frame_idx / fps if fps else 0
    draw_text_shadowed(out, f"Frame {frame_idx}  t={t_sec:.2f}s",
                       (10, 78), HUD_SCALE, HUD_COLOR, HUD_THICK)

    # Camera pose
    draw_text_shadowed(out,
                       f"Cam  yaw={cam_yaw:.1f}  pitch={cam_pitch:.1f}  fov={cam_fov:.0f}",
                       (10, 106), HUD_SCALE, HUD_COLOR, HUD_THICK)

    # Ball tracker position
    smoothed   = frame_data.get("smoothed") or {}
    ball_yaw   = smoothed.get("yaw", "?")
    ball_pitch = smoothed.get("pitch", "?")
    best_score = frame_data.get("best_score")
    score_str  = f"{best_score:.3f}" if best_score is not None else "—"
    draw_text_shadowed(out,
                       f"Ball yaw={ball_yaw}  pitch={ball_pitch}  score={score_str}",
                       (10, 134), HUD_SCALE, HUD_COLOR, HUD_THICK)

    # Fallback counters
    if render_mode == RENDER_WIDE_FALLBACK:
        draw_text_shadowed(out,
                           f"hold_counter={hold_counter}  (fallback after {HOLD_BEFORE_FALLBACK}fr)",
                           (10, 162), HUD_SCALE, (80, 160, 255), HUD_THICK)
    elif render_mode == RENDER_REACQUIRE:
        draw_text_shadowed(out,
                           f"reacquire_streak={reacquire_streak}/{REACQUIRE_MIN_FRAMES}",
                           (10, 162), HUD_SCALE, (0, 220, 140), HUD_THICK)

    # Detections
    dets = frame_data.get("detections", [])
    draw_text_shadowed(out,
                       f"Detections: {len(dets)}",
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
# v3 Tracker-State-Aware Fallback FSM
# ---------------------------------------------------------------------------
class WideAreaFallbackFSM:
    """
    Driven by tracker state, not by measuring EMA displacement.

    FOLLOW        : tracker confirmed ball → EMA follow-cam
    WIDE_FALLBACK : tracker not confirmed for HOLD_BEFORE_FALLBACK frames
                    → slow lerp to (FALLBACK_YAW, FALLBACK_PITCH, FALLBACK_FOV)
    REACQUIRE     : tracker confirms ball for REACQUIRE_MIN_FRAMES consecutive frames
                    → fast lerp back to follow-cam, then snap EMA
    """

    def __init__(self):
        self.mode              = RENDER_FOLLOW
        self.hold_counter      = 0
        self.reacquire_streak  = 0
        # Pose held during fallback drift
        self.fb_yaw            = FALLBACK_YAW
        self.fb_pitch          = FALLBACK_PITCH
        self.fb_fov            = OUTPUT_FOV
        # Last confirmed EMA before entering fallback
        self.last_follow_yaw   = None
        self.last_follow_pitch = None

    def update(self, ema_yaw, ema_pitch, tracker_state, best_score):
        """
        ema_yaw/pitch: current EMA camera position from tracker smoothed output
        tracker_state: string from tracking.json
        best_score:    float or None (None = no confirmed detection this frame)

        Returns (cam_yaw, cam_pitch, cam_fov, render_mode, hold_counter, reacquire_streak)
        """
        confirmed = (best_score is not None)

        if self.mode == RENDER_FOLLOW:
            self.last_follow_yaw   = ema_yaw
            self.last_follow_pitch = ema_pitch
            if confirmed:
                self.hold_counter     = 0
                self.reacquire_streak = 0
                # Normal follow
                return ema_yaw, ema_pitch, OUTPUT_FOV, RENDER_FOLLOW, 0, 0
            else:
                self.hold_counter += 1
                if self.hold_counter >= HOLD_BEFORE_FALLBACK:
                    # Enter fallback — initialise drift pose from current EMA
                    self.mode    = RENDER_WIDE_FALLBACK
                    self.fb_yaw  = ema_yaw
                    self.fb_pitch = ema_pitch
                    self.fb_fov  = OUTPUT_FOV
                    self.reacquire_streak = 0
                # Still holding at last good pose
                return ema_yaw, ema_pitch, OUTPUT_FOV, RENDER_FOLLOW, self.hold_counter, 0

        elif self.mode == RENDER_WIDE_FALLBACK:
            if confirmed:
                self.reacquire_streak += 1
                if self.reacquire_streak >= REACQUIRE_MIN_FRAMES:
                    # Enough consecutive confirmations — start reacquire
                    self.mode         = RENDER_REACQUIRE
                    self.hold_counter = 0
            else:
                self.reacquire_streak = 0
                # Drift toward wide fallback view
                self.fb_yaw   = lerp_yaw(self.fb_yaw,   FALLBACK_YAW,   FALLBACK_LERP_ALPHA)
                self.fb_pitch = lerp_pitch(self.fb_pitch, FALLBACK_PITCH, FALLBACK_LERP_ALPHA)
                self.fb_fov   = lerp_pitch(self.fb_fov,   FALLBACK_FOV,   FALLBACK_LERP_ALPHA)

            return (self.fb_yaw, self.fb_pitch, self.fb_fov,
                    RENDER_WIDE_FALLBACK, self.hold_counter, self.reacquire_streak)

        elif self.mode == RENDER_REACQUIRE:
            if confirmed:
                self.reacquire_streak += 1
                # Fast lerp current fb pose toward live EMA
                self.fb_yaw   = lerp_yaw(self.fb_yaw,    ema_yaw,   REACQUIRE_LERP_ALPHA)
                self.fb_pitch = lerp_pitch(self.fb_pitch, ema_pitch, REACQUIRE_LERP_ALPHA)
                self.fb_fov   = lerp_pitch(self.fb_fov,   OUTPUT_FOV, REACQUIRE_LERP_ALPHA)

                # Once close enough, snap to FOLLOW and reset EMA
                dist_yaw   = abs(((self.fb_yaw - ema_yaw + 540) % 360) - 180)
                dist_pitch = abs(self.fb_pitch - ema_pitch)
                if dist_yaw < 3.0 and dist_pitch < 3.0:
                    self.mode             = RENDER_FOLLOW
                    self.hold_counter     = 0
                    self.reacquire_streak = 0
                    return ema_yaw, ema_pitch, OUTPUT_FOV, RENDER_FOLLOW, 0, 0

                return (self.fb_yaw, self.fb_pitch, self.fb_fov,
                        RENDER_REACQUIRE, self.hold_counter, self.reacquire_streak)
            else:
                # Lost ball again during reacquire — back to fallback
                self.mode             = RENDER_WIDE_FALLBACK
                self.reacquire_streak = 0
                return (self.fb_yaw, self.fb_pitch, self.fb_fov,
                        RENDER_WIDE_FALLBACK, self.hold_counter, 0)

        # Fallthrough (should not reach)
        return ema_yaw, ema_pitch, OUTPUT_FOV, RENDER_FOLLOW, 0, 0


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------
def render_segment(equirect_path, tracking_path, start_frame, end_frame,
                   output_clean, output_debug):

    print("[render v3] Loading tracking.json...")
    with open(tracking_path) as f:
        tracking = json.load(f)

    fps         = float(tracking.get("fps", 29.97))
    frames_data = tracking.get("frames", [])
    total_tracked = len(frames_data)
    print(f"[render v3] {total_tracked} frames @ {fps:.2f} fps")
    print(f"[render v3] Rendering frames {start_frame}–{end_frame}")
    print(f"[render v3] Fallback config: hold={HOLD_BEFORE_FALLBACK}fr "
          f"reacquire={REACQUIRE_MIN_FRAMES}fr "
          f"fallback=({FALLBACK_YAW},{FALLBACK_PITCH},{FALLBACK_FOV}°)")

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

    # EMA state
    ema_yaw, ema_pitch = None, None
    ema_yaw_ref = 0.0
    prev_best_score = None

    fsm = WideAreaFallbackFSM()
    rendered = 0

    for frame_idx in range(start_frame, end_frame):
        ret, equirect = cap.read()
        if not ret:
            print(f"[render v3] Video ended at frame {frame_idx}")
            break

        frame_data   = frames_data[frame_idx] if frame_idx < len(frames_data) else {}
        smoothed     = frame_data.get("smoothed") or {}
        ball_yaw     = smoothed.get("yaw", 0.0)
        ball_pitch   = smoothed.get("pitch", 0.0)
        tracker_state = frame_data.get("tracker_state", "")
        best_score    = frame_data.get("best_score")

        # EMA
        confirmed = best_score is not None
        alpha = EMA_ALPHA_TRACKING if confirmed else EMA_ALPHA_LOSS

        if ema_yaw is None:
            ema_yaw, ema_pitch = ball_yaw, ball_pitch
            ema_yaw_ref = ball_yaw
        else:
            # Reacquisition snap: prev was not confirmed, now is
            was_confirmed = prev_best_score is not None
            if confirmed and not was_confirmed:
                # Snap EMA to confirmed position, skip lerp
                ema_yaw   = ball_yaw
                ema_pitch = ball_pitch
                ema_yaw_ref = ball_yaw
            else:
                dyaw = ball_yaw - ema_yaw_ref
                if dyaw > 180:  ball_yaw -= 360
                elif dyaw < -180: ball_yaw += 360
                ema_yaw_ref = ball_yaw
                ema_yaw   = alpha * ball_yaw   + (1 - alpha) * ema_yaw
                ema_pitch = alpha * ball_pitch + (1 - alpha) * ema_pitch

        prev_best_score = best_score

        # FSM
        cam_yaw, cam_pitch, cam_fov, render_mode, hold_ctr, reacq_streak = fsm.update(
            ema_yaw, ema_pitch, tracker_state, best_score
        )

        # Lead offset only in FOLLOW/REACQUIRE
        final_cam_yaw = cam_yaw + LEAD_DEG if render_mode != RENDER_WIDE_FALLBACK else cam_yaw

        # Clean render
        clean = extract_crop_frame(equirect, final_cam_yaw, cam_pitch, cam_fov, OUTPUT_W, OUTPUT_H)
        writer_clean.stdin.write(clean.tobytes())

        # Debug render
        debug = draw_hud(clean, frame_data, frame_idx, fps,
                         final_cam_yaw, cam_pitch, cam_fov,
                         render_mode, hold_ctr, reacq_streak)

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
            print(f"[render v3] frame {frame_idx}  mode={render_mode}  "
                  f"cam=({final_cam_yaw:.1f},{cam_pitch:.1f})  fov={cam_fov:.0f}")

    cap.release()
    writer_clean.stdin.close()
    writer_debug.stdin.close()
    writer_clean.wait()
    writer_debug.wait()
    print(f"[render v3] Done. {rendered} frames rendered.")
    print(f"[render v3] Clean : {output_clean}")
    print(f"[render v3] Debug : {output_debug}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",         default="equirect_trim.mp4")
    parser.add_argument("--tracking",      default="tracking.json")
    parser.add_argument("--start-frame",   type=int, default=700)
    parser.add_argument("--end-frame",     type=int, default=1300)
    parser.add_argument("--output-clean",  default="render_clean.mp4")
    parser.add_argument("--output-debug",  default="render_debug.mp4")
    args = parser.parse_args()

    render_segment(
        equirect_path=args.input,
        tracking_path=args.tracking,
        start_frame=args.start_frame,
        end_frame=args.end_frame,
        output_clean=args.output_clean,
        output_debug=args.output_debug,
    )
