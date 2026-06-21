#!/usr/bin/env python3
"""
FFA 360 Render Segment — v1
============================
Pure render step. Reads tracking.json (pre-computed), renders a frame window
as two outputs:
  - render_clean.mp4  : 16:9 follow-cam, no overlays
  - render_debug.mp4  : follow-cam + HUD overlay + equirect inset with ball marker

No detection, no Kalman — this is a visual validation step only.

Usage:
  python3 render_segment.py \
    --input equirect_trim.mp4 \
    --tracking tracking.json \
    --start-frame 800 \
    --end-frame 1000 \
    --output-clean render_clean.mp4 \
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

INSET_W      = 480         # equirect inset width
INSET_H      = 270         # equirect inset height (16:9)
INSET_X      = 20          # inset position (bottom-left)
INSET_Y_OFF  = 20          # offset from bottom

HUD_FONT     = cv2.FONT_HERSHEY_SIMPLEX
HUD_SCALE    = 0.65
HUD_THICK    = 2
HUD_COLOR    = (255, 255, 255)
HUD_SHADOW   = (0, 0, 0)

STATE_COLORS = {
    "TRACKING":      (0, 220, 0),
    "UNCERTAIN":     (0, 180, 220),
    "LOST":          (0, 0, 220),
    "WARMING_UP":    (220, 180, 0),
    "UNINITIALIZED": (100, 100, 100),
    "tracking":      (0, 220, 0),
    "extrapolating": (0, 180, 220),
    "holding":       (0, 120, 220),
    "player_drift":  (0, 0, 220),
    "uninitialised": (100, 100, 100),
    "warming_up":    (220, 180, 0),
}


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
def extract_crop_frame(equirect_frame, yaw_deg, fov_deg, out_w, out_h):
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
    yaw_map   = np.arctan2(wx, wz)
    pitch_map = np.arcsin(np.clip(wy, -1, 1))
    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    return cv2.remap(equirect_frame,
                     map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def yaw_pitch_to_equirect_pixel(yaw_deg, pitch_deg, w, h):
    """Convert yaw/pitch (degrees) to pixel coords in equirectangular frame."""
    x = int(((yaw_deg / 360.0) + 0.5) % 1.0 * w)
    y = int((0.5 - pitch_deg / 180.0) * h)
    return x, y


# ---------------------------------------------------------------------------
# HUD helpers
# ---------------------------------------------------------------------------
def draw_text_shadowed(img, text, pos, scale, color, thick, shadow=HUD_SHADOW):
    x, y = pos
    cv2.putText(img, text, (x+1, y+1), HUD_FONT, scale, shadow, thick + 1, cv2.LINE_AA)
    cv2.putText(img, text, (x, y), HUD_FONT, scale, color, thick, cv2.LINE_AA)


def draw_hud(frame, frame_data, frame_idx, fps, cam_yaw, cam_pitch):
    """Draw debug overlay on a copy of frame."""
    out = frame.copy()
    h, w = out.shape[:2]

    # Tracker state colour bar at top
    state_str = frame_data.get("tracker_state") or frame_data.get("loss_state", "?")
    state_key = state_str.split(" ")[0]  # strip "(N)" suffix
    bar_color = STATE_COLORS.get(state_key, (128, 128, 128))
    cv2.rectangle(out, (0, 0), (w, 40), bar_color, -1)
    cv2.rectangle(out, (0, 0), (w, 40), (0, 0, 0), 2)

    # State label
    draw_text_shadowed(out, f"STATE: {state_str}", (10, 28),
                       HUD_SCALE * 1.1, (0, 0, 0), HUD_THICK)

    # Frame / time
    t_sec = frame_idx / fps if fps else 0
    draw_text_shadowed(out, f"Frame {frame_idx}  t={t_sec:.2f}s",
                       (10, 75), HUD_SCALE, HUD_COLOR, HUD_THICK)

    # Camera yaw/pitch
    smoothed = frame_data.get("smoothed") or {}
    ball_yaw   = smoothed.get("yaw", "?")
    ball_pitch = smoothed.get("pitch", "?")
    draw_text_shadowed(out,
                       f"Cam  yaw={cam_yaw:.1f}°  pitch={cam_pitch:.1f}°",
                       (10, 105), HUD_SCALE, HUD_COLOR, HUD_THICK)
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

    # Ball dot on follow-cam (centre of frame = ball position)
    # The camera leads by LEAD_DEG so ball should be slightly left of centre
    ball_offset_px = int(LEAD_DEG / OUTPUT_FOV * w)
    cx = w // 2 - ball_offset_px
    cy_mid = h // 2
    cv2.circle(out, (cx, cy_mid), 18, (0, 255, 255), 2)
    cv2.circle(out, (cx, cy_mid), 4,  (0, 255, 255), -1)
    cv2.line(out, (cx - 28, cy_mid), (cx + 28, cy_mid), (0, 255, 255), 1)
    cv2.line(out, (cx, cy_mid - 28), (cx, cy_mid + 28), (0, 255, 255), 1)

    return out


def draw_equirect_inset(equirect_frame, frame_data, inset_w, inset_h):
    """Return a small equirect thumbnail with ball position marked."""
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
    # Draw all raw detections in red
    for det in frame_data.get("detections", []):
        dx, dy = yaw_pitch_to_equirect_pixel(det["yaw"], det["pitch"], inset_w, inset_h)
        dx = max(3, min(inset_w - 3, dx))
        dy = max(3, min(inset_h - 3, dy))
        cv2.circle(inset, (dx, dy), 4, (0, 80, 255), -1)
    # Border
    cv2.rectangle(inset, (0, 0), (inset_w - 1, inset_h - 1), (200, 200, 200), 1)
    return inset


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

    # Validate range
    if start_frame >= total_tracked or end_frame > total_tracked:
        print(f"[render] WARNING: clamping end_frame from {end_frame} to {total_tracked}")
        end_frame = min(end_frame, total_tracked)

    cap = cv2.VideoCapture(equirect_path)
    total_video_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"[render] Video: {total_video_frames} frames")

    # Seek to start_frame
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

    # EMA state — initialise from frame at start_frame
    ema_yaw, ema_pitch = None, None
    EMA_ALPHA = 0.18

    rendered = 0
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

        # EMA smoothing (matches tracker behaviour)
        alpha = EMA_ALPHA if "tracking" in str(loss_state).lower() else 0.08
        if ema_yaw is None:
            ema_yaw, ema_pitch = ball_yaw, ball_pitch
        else:
            ema_yaw   = alpha * ball_yaw   + (1 - alpha) * ema_yaw
            ema_pitch = alpha * ball_pitch + (1 - alpha) * ema_pitch

        cam_yaw   = ema_yaw   + LEAD_DEG
        cam_pitch = ema_pitch

        # --- Clean render ---
        clean_frame = extract_crop_frame(equirect, cam_yaw, OUTPUT_FOV, OUTPUT_W, OUTPUT_H)
        writer_clean.stdin.write(clean_frame.tobytes())

        # --- Debug render ---
        debug_frame = draw_hud(clean_frame, frame_data, frame_idx, fps, cam_yaw, cam_pitch)

        # Equirect inset (bottom-left)
        inset = draw_equirect_inset(equirect, frame_data, INSET_W, INSET_H)
        iy = OUTPUT_H - INSET_H - INSET_Y_OFF
        debug_frame[iy:iy + INSET_H, INSET_X:INSET_X + INSET_W] = inset

        writer_debug.stdin.write(debug_frame.tobytes())

        rendered += 1
        if rendered % 50 == 0:
            print(f"[render] frame {frame_idx}  cam_yaw={cam_yaw:.1f}°  state={loss_state}")

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
