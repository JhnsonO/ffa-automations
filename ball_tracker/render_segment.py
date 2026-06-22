#!/usr/bin/env python3
"""
FFA 360 Render Segment — v2
============================
Pure render step. Reads tracking.json (pre-computed), renders a frame window
as two outputs:
  - render_clean.mp4  : 16:9 follow-cam, no overlays
  - render_debug.mp4  : follow-cam + HUD overlay + equirect inset with ball marker

Adds render-side STATIC FALLBACK:
  When the smoothed camera position hasn't moved more than STATIC_MAX_DISPLACEMENT_DEG
  over STATIC_WINDOW_FRAMES, the renderer enters STATIC_HOLD (freeze last good pose),
  then STATIC_DRIFT (slow pan toward configurable fallback pose), and snaps back on
  genuine motion recovery (STATIC_RECOVER).

No detection, no Kalman — this is a visual validation step only.

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
from collections import deque

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

INSET_W      = 480         # equirect inset width
INSET_H      = 270         # equirect inset height (16:9)
INSET_X      = 20          # inset position (bottom-left)
INSET_Y_OFF  = 20          # offset from bottom

HUD_FONT     = cv2.FONT_HERSHEY_SIMPLEX
HUD_SCALE    = 0.65
HUD_THICK    = 2
HUD_COLOR    = (255, 255, 255)
HUD_SHADOW   = (0, 0, 0)

# ---------------------------------------------------------------------------
# Static Fallback Config
# ---------------------------------------------------------------------------
STATIC_WINDOW_FRAMES        = 90    # frames over which to measure displacement
STATIC_MAX_DISPLACEMENT_DEG = 0.5   # max spherical displacement to trigger suspect
STATIC_HOLD_FRAMES          = 60    # frames to hold last good pose before drifting
STATIC_RECOVERY_FRAMES      = 5     # consecutive moving frames required to exit fallback
FALLBACK_YAW                = 0.0   # configurable pitch-facing fallback yaw (degrees)
FALLBACK_PITCH              = -5.0  # configurable pitch-facing fallback pitch (degrees)
FALLBACK_FOV                = 100.0 # fallback FOV — slightly wider to show more pitch
DRIFT_ALPHA                 = 0.02  # lerp rate toward fallback pose per frame

# ---------------------------------------------------------------------------
# Render states (tracker + static fallback combined)
# ---------------------------------------------------------------------------
RENDER_STATE_NORMAL         = "NORMAL"
RENDER_STATE_STATIC_SUSPECT = "STATIC_SUSPECT"
RENDER_STATE_STATIC_HOLD    = "STATIC_HOLD"
RENDER_STATE_STATIC_DRIFT   = "STATIC_DRIFT"
RENDER_STATE_STATIC_RECOVER = "STATIC_RECOVER"

STATE_COLORS = {
    "TRACKING":         (0, 220, 0),
    "UNCERTAIN":        (0, 180, 220),
    "LOST":             (0, 0, 220),
    "WARMING_UP":       (220, 180, 0),
    "UNINITIALIZED":    (100, 100, 100),
    "tracking":         (0, 220, 0),
    "extrapolating":    (0, 180, 220),
    "holding":          (0, 120, 220),
    "player_drift":     (0, 0, 220),
    "uninitialised":    (100, 100, 100),
    "warming_up":       (220, 180, 0),
    # Static fallback states
    RENDER_STATE_STATIC_SUSPECT: (0, 200, 255),
    RENDER_STATE_STATIC_HOLD:    (0, 100, 255),
    RENDER_STATE_STATIC_DRIFT:   (30, 30, 255),
    RENDER_STATE_STATIC_RECOVER: (0, 255, 180),
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
    # Yaw rotation
    cy = math.radians(yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    # Pitch rotation
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


def spherical_displacement(yaw1, pitch1, yaw2, pitch2):
    """Angular distance between two spherical positions (degrees)."""
    y1, p1 = math.radians(yaw1), math.radians(pitch1)
    y2, p2 = math.radians(yaw2), math.radians(pitch2)
    # Convert to unit vectors
    x1 = math.cos(p1) * math.sin(y1)
    y1v = math.sin(p1)
    z1 = math.cos(p1) * math.cos(y1)
    x2 = math.cos(p2) * math.sin(y2)
    y2v = math.sin(p2)
    z2 = math.cos(p2) * math.cos(y2)
    dot = max(-1.0, min(1.0, x1*x2 + y1v*y2v + z1*z2))
    return math.degrees(math.acos(dot))


def lerp_yaw(current, target, alpha):
    """Lerp yaw taking shortest path across 360° seam."""
    diff = (target - current + 540) % 360 - 180
    return current + alpha * diff


# ---------------------------------------------------------------------------
# HUD helpers
# ---------------------------------------------------------------------------
def draw_text_shadowed(img, text, pos, scale, color, thick, shadow=HUD_SHADOW):
    x, y = pos
    cv2.putText(img, text, (x+1, y+1), HUD_FONT, scale, shadow, thick + 1, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), HUD_FONT, scale, color, thick, cv2.LINE_AA)


def draw_hud(frame, frame_data, frame_idx, fps, cam_yaw, cam_pitch, cam_fov,
             render_state, static_hold_counter, static_recover_counter):
    out = frame.copy()
    h, w = out.shape[:2]

    # Tracker state — use render_state if in static fallback, else tracker state
    tracker_state_str = frame_data.get("tracker_state") or frame_data.get("loss_state", "?")
    if render_state != RENDER_STATE_NORMAL:
        display_state = render_state
    else:
        display_state = tracker_state_str

    state_key = display_state.split(" ")[0]
    bar_color = STATE_COLORS.get(state_key, STATE_COLORS.get(display_state, (128, 128, 128)))
    cv2.rectangle(out, (0, 0), (w, 40), bar_color, -1)
    cv2.rectangle(out, (0, 0), (w, 40), (0, 0, 0), 2)
    draw_text_shadowed(out, f"STATE: {display_state}", (10, 28),
                       HUD_SCALE * 1.1, (0, 0, 0), HUD_THICK)

    # Frame / time
    t_sec = frame_idx / fps if fps else 0
    draw_text_shadowed(out, f"Frame {frame_idx}  t={t_sec:.2f}s",
                       (10, 75), HUD_SCALE, HUD_COLOR, HUD_THICK)

    # Camera pose
    draw_text_shadowed(out,
                       f"Cam  yaw={cam_yaw:.1f}°  pitch={cam_pitch:.1f}°  fov={cam_fov:.0f}°",
                       (10, 105), HUD_SCALE, HUD_COLOR, HUD_THICK)

    # Ball tracker position
    smoothed = frame_data.get("smoothed") or {}
    ball_yaw   = smoothed.get("yaw", "?")
    ball_pitch = smoothed.get("pitch", "?")
    draw_text_shadowed(out,
                       f"Ball yaw={ball_yaw}°  pitch={ball_pitch}°",
                       (10, 133), HUD_SCALE, HUD_COLOR, HUD_THICK)

    # Detection count + best score
    dets = frame_data.get("detections", [])
    best_score = frame_data.get("best_score")
    score_str = f"{best_score:.3f}" if best_score is not None else "—"
    draw_text_shadowed(out,
                       f"Detections: {len(dets)}   Best score: {score_str}",
                       (10, 161), HUD_SCALE, HUD_COLOR, HUD_THICK)

    # Static fallback counters
    if render_state in (RENDER_STATE_STATIC_HOLD, RENDER_STATE_STATIC_DRIFT,
                        RENDER_STATE_STATIC_SUSPECT, RENDER_STATE_STATIC_RECOVER):
        draw_text_shadowed(out,
                           f"hold_frames={static_hold_counter}  recover_streak={static_recover_counter}",
                           (10, 189), HUD_SCALE, (0, 200, 255), HUD_THICK)

    # Ball crosshair
    ball_offset_px = int(LEAD_DEG / OUTPUT_FOV * w)
    cx = w // 2 - ball_offset_px
    cy_mid = h // 2
    crosshair_color = (128, 128, 128) if render_state in (
        RENDER_STATE_STATIC_HOLD, RENDER_STATE_STATIC_DRIFT) else (0, 255, 255)
    cv2.circle(out, (cx, cy_mid), 18, crosshair_color, 2)
    cv2.circle(out, (cx, cy_mid), 4,  crosshair_color, -1)
    cv2.line(out, (cx - 28, cy_mid), (cx + 28, cy_mid), crosshair_color, 1)
    cv2.line(out, (cx, cy_mid - 28), (cx, cy_mid + 28), crosshair_color, 1)

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
        cv2.circle(inset, (px, py), 8,  (0, 255, 255), 2)
        cv2.circle(inset, (px, py), 2,  (0, 255, 255), -1)
    for det in frame_data.get("detections", []):
        dx, dy = yaw_pitch_to_equirect_pixel(det["yaw"], det["pitch"], inset_w, inset_h)
        dx = max(3, min(inset_w - 3, dx))
        dy = max(3, min(inset_h - 3, dy))
        cv2.circle(inset, (dx, dy), 4, (0, 80, 255), -1)
    cv2.rectangle(inset, (0, 0), (inset_w - 1, inset_h - 1), (200, 200, 200), 1)
    return inset


# ---------------------------------------------------------------------------
# Static Fallback State Machine
# ---------------------------------------------------------------------------
class StaticFallbackFSM:
    """
    Monitors EMA camera pose for static lock.
    States: NORMAL → STATIC_SUSPECT → STATIC_HOLD → STATIC_DRIFT → STATIC_RECOVER → NORMAL

    Displacement check uses max spherical distance over a rolling window of recent poses,
    not std dev, so a single-direction drift still triggers if it is very slow.
    """

    def __init__(self):
        self.state = RENDER_STATE_NORMAL
        self.pose_history = deque(maxlen=STATIC_WINDOW_FRAMES)
        self.last_good_yaw = None
        self.last_good_pitch = None
        self.hold_counter = 0
        self.recover_counter = 0
        self.drift_yaw = None
        self.drift_pitch = None
        self.drift_fov = OUTPUT_FOV

    def update(self, ema_yaw, ema_pitch, tracker_state):
        """
        Call each frame with the current EMA pose.
        Returns (cam_yaw, cam_pitch, cam_fov, render_state).
        """
        self.pose_history.append((ema_yaw, ema_pitch))

        # Compute max spherical displacement over history window
        max_disp = 0.0
        if len(self.pose_history) >= STATIC_WINDOW_FRAMES:
            ref_yaw, ref_pitch = self.pose_history[0]
            for py, pp in self.pose_history:
                d = spherical_displacement(ref_yaw, ref_pitch, py, pp)
                if d > max_disp:
                    max_disp = d

        is_static = (len(self.pose_history) >= STATIC_WINDOW_FRAMES
                     and max_disp < STATIC_MAX_DISPLACEMENT_DEG)

        if self.state == RENDER_STATE_NORMAL:
            if is_static:
                # Save last good pose before we enter suspect
                if self.last_good_yaw is None:
                    self.last_good_yaw = ema_yaw
                    self.last_good_pitch = ema_pitch
                self.state = RENDER_STATE_STATIC_SUSPECT
                self.hold_counter = 0
                self.recover_counter = 0
            else:
                self.last_good_yaw = ema_yaw
                self.last_good_pitch = ema_pitch

        elif self.state == RENDER_STATE_STATIC_SUSPECT:
            if not is_static:
                self.state = RENDER_STATE_NORMAL
                self.last_good_yaw = ema_yaw
                self.last_good_pitch = ema_pitch
            else:
                # Immediately transition to HOLD (SUSPECT is a 1-frame gate)
                self.state = RENDER_STATE_STATIC_HOLD
                self.hold_counter = 0
                self.drift_yaw = self.last_good_yaw
                self.drift_pitch = self.last_good_pitch
                self.drift_fov = OUTPUT_FOV

        elif self.state == RENDER_STATE_STATIC_HOLD:
            if not is_static:
                self.recover_counter += 1
                if self.recover_counter >= STATIC_RECOVERY_FRAMES:
                    self.state = RENDER_STATE_STATIC_RECOVER
            else:
                self.recover_counter = 0
                self.hold_counter += 1
                if self.hold_counter >= STATIC_HOLD_FRAMES:
                    self.state = RENDER_STATE_STATIC_DRIFT
                    self.drift_yaw = self.last_good_yaw
                    self.drift_pitch = self.last_good_pitch
                    self.drift_fov = OUTPUT_FOV

        elif self.state == RENDER_STATE_STATIC_DRIFT:
            if not is_static:
                self.recover_counter += 1
                if self.recover_counter >= STATIC_RECOVERY_FRAMES:
                    self.state = RENDER_STATE_STATIC_RECOVER
            else:
                self.recover_counter = 0
                # Drift toward fallback pose
                self.drift_yaw   = lerp_yaw(self.drift_yaw, FALLBACK_YAW, DRIFT_ALPHA)
                self.drift_pitch = self.drift_pitch + DRIFT_ALPHA * (FALLBACK_PITCH - self.drift_pitch)
                self.drift_fov   = self.drift_fov   + DRIFT_ALPHA * (FALLBACK_FOV   - self.drift_fov)

        elif self.state == RENDER_STATE_STATIC_RECOVER:
            if not is_static:
                self.recover_counter += 1
                if self.recover_counter >= STATIC_RECOVERY_FRAMES:
                    # Fully recovered — snap to live EMA
                    self.state = RENDER_STATE_NORMAL
                    self.last_good_yaw = ema_yaw
                    self.last_good_pitch = ema_pitch
                    self.recover_counter = 0
            else:
                # Lost it again — back to hold
                self.recover_counter = 0
                self.state = RENDER_STATE_STATIC_HOLD
                self.hold_counter = 0

        # --- Output camera pose ---
        if self.state == RENDER_STATE_NORMAL:
            return ema_yaw, ema_pitch, OUTPUT_FOV, self.state

        elif self.state == RENDER_STATE_STATIC_SUSPECT:
            # Still using live EMA for one frame
            return ema_yaw, ema_pitch, OUTPUT_FOV, self.state

        elif self.state == RENDER_STATE_STATIC_HOLD:
            return self.last_good_yaw, self.last_good_pitch, OUTPUT_FOV, self.state

        elif self.state == RENDER_STATE_STATIC_DRIFT:
            return self.drift_yaw, self.drift_pitch, self.drift_fov, self.state

        elif self.state == RENDER_STATE_STATIC_RECOVER:
            # Fast lerp toward live EMA
            snap_alpha = 0.25
            recover_yaw   = lerp_yaw(self.drift_yaw if self.drift_yaw is not None else ema_yaw,
                                     ema_yaw, snap_alpha)
            recover_pitch = (self.drift_pitch if self.drift_pitch is not None else ema_pitch)
            recover_pitch = recover_pitch + snap_alpha * (ema_pitch - recover_pitch)
            self.drift_yaw   = recover_yaw
            self.drift_pitch = recover_pitch
            return recover_yaw, recover_pitch, OUTPUT_FOV, self.state

        return ema_yaw, ema_pitch, OUTPUT_FOV, self.state


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------
def render_segment(equirect_path, tracking_path, start_frame, end_frame,
                   output_clean, output_debug):

    print(f"[render] Loading tracking.json...")
    with open(tracking_path) as f:
        tracking = json.load(f)

    fps_raw = tracking.get("fps", 29.97)
    fps = float(fps_raw)
    frames_data = tracking.get("frames", [])
    total_tracked = len(frames_data)
    print(f"[render] Tracking data: {total_tracked} frames @ {fps:.2f} fps")
    print(f"[render] Rendering frames {start_frame}–{end_frame} ({end_frame - start_frame} frames)")
    print(f"[render] Static fallback config: window={STATIC_WINDOW_FRAMES}fr "
          f"threshold={STATIC_MAX_DISPLACEMENT_DEG}° hold={STATIC_HOLD_FRAMES}fr "
          f"recovery={STATIC_RECOVERY_FRAMES}fr fallback=({FALLBACK_YAW},{FALLBACK_PITCH})°")

    if start_frame >= total_tracked or end_frame > total_tracked:
        print(f"[render] WARNING: clamping end_frame from {end_frame} to {total_tracked}")
        end_frame = min(end_frame, total_tracked)

    cap = cv2.VideoCapture(equirect_path)
    total_video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[render] Video: {total_video_frames} frames")
    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)

    def make_ffmpeg_writer(output_path):
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
            output_path
        ], stdin=subprocess.PIPE)

    writer_clean = make_ffmpeg_writer(output_clean)
    writer_debug = make_ffmpeg_writer(output_debug)

    # EMA state
    ema_yaw, ema_pitch = None, None
    ema_yaw_ref = 0.0
    EMA_ALPHA = 0.18

    static_fsm = StaticFallbackFSM()

    rendered = 0
    prev_loss_state = None
    snap_event = False

    for frame_idx in range(start_frame, end_frame):
        ret, equirect = cap.read()
        if not ret:
            print(f"[render] Video ended at frame {frame_idx}")
            break

        frame_data = frames_data[frame_idx] if frame_idx < len(frames_data) else {}
        smoothed   = frame_data.get("smoothed") or {}
        ball_yaw   = smoothed.get("yaw", 0.0)
        ball_pitch = smoothed.get("pitch", 0.0)
        loss_state = frame_data.get("loss_state", "tracking")

        # EMA smoothing
        alpha = EMA_ALPHA if "tracking" in str(loss_state).lower() else 0.08
        tracker_state = frame_data.get("tracker_state", "")
        is_confirmed  = (tracker_state == "TRACKING" and frame_data.get("best_score") is not None)
        was_hold      = prev_loss_state in {"holding", "hold"} or (
                            prev_loss_state and str(prev_loss_state).startswith("holding"))

        if ema_yaw is None:
            ema_yaw, ema_pitch = ball_yaw, ball_pitch
            ema_yaw_ref = ball_yaw
        elif was_hold and is_confirmed:
            ema_yaw, ema_pitch = ball_yaw, ball_pitch
            ema_yaw_ref = ball_yaw
            snap_event = True
        else:
            dyaw = ball_yaw - ema_yaw_ref
            if dyaw > 180:
                ball_yaw -= 360
            elif dyaw < -180:
                ball_yaw += 360
            ema_yaw_ref = ball_yaw
            ema_yaw   = alpha * ball_yaw   + (1 - alpha) * ema_yaw
            ema_pitch = alpha * ball_pitch + (1 - alpha) * ema_pitch

        # Static fallback FSM
        cam_yaw, cam_pitch, cam_fov, render_state = static_fsm.update(
            ema_yaw, ema_pitch, tracker_state)

        # Apply lead only in NORMAL/RECOVER states
        if render_state in (RENDER_STATE_NORMAL, RENDER_STATE_STATIC_RECOVER,
                            RENDER_STATE_STATIC_SUSPECT):
            final_cam_yaw = cam_yaw + LEAD_DEG
        else:
            final_cam_yaw = cam_yaw  # no lead offset during hold/drift

        # --- Clean render ---
        clean_frame = extract_crop_frame(equirect, final_cam_yaw, cam_pitch,
                                         cam_fov, OUTPUT_W, OUTPUT_H)
        writer_clean.stdin.write(clean_frame.tobytes())

        # --- Debug render ---
        debug_frame = draw_hud(clean_frame, frame_data, frame_idx, fps,
                                final_cam_yaw, cam_pitch, cam_fov,
                                render_state,
                                static_fsm.hold_counter,
                                static_fsm.recover_counter)

        if snap_event:
            draw_text_shadowed(debug_frame, "*** REACQUIRE: EMA RESET ***",
                               (OUTPUT_W // 2 - 200, OUTPUT_H // 2),
                               HUD_SCALE * 1.3, (0, 255, 255), HUD_THICK + 1)
            snap_event = False

        # Equirect inset
        inset = draw_equirect_inset(equirect, frame_data, INSET_W, INSET_H)
        iy = OUTPUT_H - INSET_H - INSET_Y_OFF
        debug_frame[iy:iy + INSET_H, INSET_X:INSET_X + INSET_W] = inset

        writer_debug.stdin.write(debug_frame.tobytes())

        prev_loss_state = loss_state
        rendered += 1
        if rendered % 50 == 0:
            print(f"[render] frame {frame_idx}  cam_yaw={final_cam_yaw:.1f}°  "
                  f"ema_yaw={ema_yaw:.1f}°  render_state={render_state}")

    cap.release()
    writer_clean.stdin.close()
    writer_debug.stdin.close()
    writer_clean.wait()
    writer_debug.wait()
    print(f"[render] Done. {rendered} frames rendered.")
    print(f"[render] Clean:  {output_clean}")
    print(f"[render] Debug:  {output_debug}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--input",         default="equirect_trim.mp4")
    parser.add_argument("--tracking",      default="tracking.json")
    parser.add_argument("--start-frame",   type=int, default=800)
    parser.add_argument("--end-frame",     type=int, default=1000)
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
