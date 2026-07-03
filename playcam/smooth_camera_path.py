#!/usr/bin/env python3
"""
playcam/smooth_camera_path.py

Phase 2 — turns the sparse Phase 1 measurement timeline (play_location.jsonl,
~2fps person_centroid_yaw) into a dense, smooth camera path suitable for
actual rendering.

Semantic target is FROZEN per 2026-07-02 review: person_centroid_yaw (masked,
in-pitch players) is the only input used here. activity_centroid_yaw,
stable_group_yaw, and proposed_yaw remain logged diagnostics in Phase 1's
output and are NOT read by this script.

Pitch and FOV are fixed constants here too (+4, 85) -- never derived from
any centroid. No ball_tracker/ imports or edits.

Pipeline:
  1. Read (timestamp, person_centroid_yaw) pairs from play_location.jsonl.
  2. Hold last stable yaw through missing/null samples (gaps in the source).
  3. Deadband: ignore target changes smaller than --deadband-deg (treat as
     noise, don't feed them into the smoother).
  4. Unwrap yaw to a continuous angle (circular-safe, no wraparound jumps),
     linearly interpolate the sparse deadbanded target up to --render-fps.
  5. Ease the dense target through a kinematic limiter: acceleration-limited,
     velocity-capped -- can't snap, can't exceed max pan speed/accel.
  6. Output timestamp / raw_yaw / smoothed_yaw / pitch / fov per render frame
     to a jsonl camera timeline.

Usage:
  python3 playcam/smooth_camera_path.py \
      --input playcam/output/play_location.jsonl \
      --output playcam/output/camera_timeline.jsonl

  # Then render a short side-by-side comparison (fixed camera vs smoothed):
  python3 playcam/smooth_camera_path.py \
      --input playcam/output/play_location.jsonl \
      --output playcam/output/camera_timeline.jsonl \
      --render-comparison --source-video clip.mp4 \
      --comparison-duration 25 \
      --comparison-output playcam/output/comparison.mp4
"""

import argparse
import json
import math
import sys
from pathlib import Path

FIXED_PITCH = 4.0
FIXED_FOV = 85.0

DEFAULT_RENDER_FPS = 30.0
DEFAULT_DEADBAND_DEG = 1.5
DEFAULT_MAX_PAN_SPEED_DEG_S = 25.0    # max yaw angular velocity
DEFAULT_MAX_PAN_ACCEL_DEG_S2 = 60.0   # max yaw angular acceleration
DEFAULT_SPRING_STIFFNESS = 8.0        # how hard the filter pulls toward target


def parse_args():
    p = argparse.ArgumentParser(description="Phase 2 -- smooth camera path from play_location.jsonl")
    p.add_argument("--input", required=True, type=Path,
                    help="play_location.jsonl from Phase 1 (play_location.py)")
    p.add_argument("--output", type=Path, default=Path("playcam/output/camera_timeline.jsonl"))
    p.add_argument("--render-fps", type=float, default=DEFAULT_RENDER_FPS,
                    help=f"Dense output frame rate (default {DEFAULT_RENDER_FPS})")
    p.add_argument("--deadband-deg", type=float, default=DEFAULT_DEADBAND_DEG,
                    help=f"Ignore target changes smaller than this (default {DEFAULT_DEADBAND_DEG})")
    p.add_argument("--max-pan-speed", type=float, default=DEFAULT_MAX_PAN_SPEED_DEG_S,
                    help=f"Max yaw angular velocity, deg/s (default {DEFAULT_MAX_PAN_SPEED_DEG_S})")
    p.add_argument("--max-pan-accel", type=float, default=DEFAULT_MAX_PAN_ACCEL_DEG_S2,
                    help=f"Max yaw angular acceleration, deg/s^2 (default {DEFAULT_MAX_PAN_ACCEL_DEG_S2})")
    p.add_argument("--spring-stiffness", type=float, default=DEFAULT_SPRING_STIFFNESS,
                    help=f"Ease response strength -- higher = snappier within the "
                         f"speed/accel caps (default {DEFAULT_SPRING_STIFFNESS})")

    p.add_argument("--render-comparison", action="store_true",
                    help="Also render a fixed-camera-vs-smoothed side-by-side mp4")
    p.add_argument("--source-video", type=Path, default=None,
                    help="Required with --render-comparison: the equirect source clip")
    p.add_argument("--comparison-duration", type=float, default=25.0,
                    help="Cap comparison render to this many seconds (default 25, max 30)")
    p.add_argument("--comparison-output", type=Path,
                    default=Path("playcam/output/comparison.mp4"))
    return p.parse_args()


# ---------------------------------------------------------------------------
# Step 1-3: load, hold-through-gaps, deadband
# ---------------------------------------------------------------------------

def load_sparse_targets(input_path):
    """Read (timestamp, yaw_or_None) pairs from play_location.jsonl."""
    pairs = []
    with open(input_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            pairs.append((rec["timestamp"], rec.get("person_centroid_yaw")))
    return pairs


def hold_and_deadband(pairs, deadband_deg):
    """
    Hold last stable yaw through None samples. Ignore changes smaller than
    deadband_deg (treat as noise -- don't let the smoother chase jitter).
    Returns list of (timestamp, held_yaw) with no None values, starting
    from the first sample that has a real value.
    """
    out = []
    held = None
    for t, y in pairs:
        if y is None:
            if held is not None:
                out.append((t, held))
            continue
        if held is None:
            held = y
        else:
            delta = ((y - held + 180) % 360) - 180
            if abs(delta) >= deadband_deg:
                held = held + delta  # move held target, still yaw-continuous
                held = ((held + 180) % 360) - 180
            # else: change too small, keep held unchanged (noise rejection)
        out.append((t, held))
    return out


# ---------------------------------------------------------------------------
# Step 4: circular-safe unwrap + interpolation to render-frame rate
# ---------------------------------------------------------------------------

def unwrap_degrees(values):
    """Standard angle unwrap: remove artificial +/-360 jumps so linear
    interpolation across samples doesn't take the long way around."""
    if not values:
        return []
    out = [values[0]]
    for v in values[1:]:
        prev = out[-1]
        delta = ((v - prev + 180) % 360) - 180
        out.append(prev + delta)
    return out


def interpolate_dense(held_pairs, render_fps, total_duration):
    """
    Linearly interpolate the sparse (deadbanded, gap-filled) target onto a
    dense render-frame-rate timeline. Circular-safe via unwrap before
    interpolation, wrapped back to [-180, 180] after.
    Before the first sample: hold first value. After the last: hold last value.
    """
    if not held_pairs:
        return []

    ts = [p[0] for p in held_pairs]
    unwrapped = unwrap_degrees([p[1] for p in held_pairs])

    dt = 1.0 / render_fps
    n_frames = int(math.ceil(total_duration / dt)) + 1
    dense = []
    j = 0
    for k in range(n_frames):
        t = k * dt
        if t <= ts[0]:
            val = unwrapped[0]
        elif t >= ts[-1]:
            val = unwrapped[-1]
        else:
            while j + 1 < len(ts) and ts[j + 1] < t:
                j += 1
            t0, t1 = ts[j], ts[j + 1]
            v0, v1 = unwrapped[j], unwrapped[j + 1]
            frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
            val = v0 + frac * (v1 - v0)
        dense.append((t, ((val + 180) % 360) - 180))
    return dense


# ---------------------------------------------------------------------------
# Step 5: kinematic ease -- velocity-capped, acceleration-limited
# ---------------------------------------------------------------------------

def ease_kinematic(dense_targets, render_fps, max_speed, max_accel, stiffness):
    """
    Critically-damped-ish spring toward the (already deadbanded/interpolated)
    target, with explicit hard caps on angular velocity and acceleration --
    the caps are the real guarantee against snapping, the spring just shapes
    how it approaches within those caps.
    """
    if not dense_targets:
        return []
    dt = 1.0 / render_fps
    damping = 2.0 * math.sqrt(stiffness)  # critical damping for this stiffness

    yaw = dense_targets[0][1]
    vel = 0.0
    out = []
    for t, target in dense_targets:
        delta = ((target - yaw + 180) % 360) - 180
        accel = stiffness * delta - damping * vel
        accel = max(-max_accel, min(max_accel, accel))
        vel += accel * dt
        vel = max(-max_speed, min(max_speed, vel))
        yaw += vel * dt
        yaw = ((yaw + 180) % 360) - 180
        out.append((t, yaw, vel, accel))
    return out


def main():
    args = parse_args()

    if not args.input.exists():
        print(f"ERROR: --input does not exist: {args.input}", file=sys.stderr)
        sys.exit(1)
    if args.render_comparison and args.source_video is None:
        print("ERROR: --render-comparison requires --source-video", file=sys.stderr)
        sys.exit(1)
    if args.render_comparison and args.comparison_duration > 30:
        print("ERROR: --comparison-duration capped at 30s for this validation step "
              "(full-match render is a later step)", file=sys.stderr)
        sys.exit(1)

    pairs = load_sparse_targets(args.input)
    if not pairs:
        print("ERROR: no records in input", file=sys.stderr)
        sys.exit(1)

    held = hold_and_deadband(pairs, args.deadband_deg)
    if not held:
        print("ERROR: no valid person_centroid_yaw values found in input "
              "(all null?)", file=sys.stderr)
        sys.exit(1)

    total_duration = pairs[-1][0]
    dense_targets = interpolate_dense(held, args.render_fps, total_duration)
    eased = ease_kinematic(dense_targets, args.render_fps,
                            args.max_pan_speed, args.max_pan_accel, args.spring_stiffness)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w") as f:
        for (t, raw_yaw), (_, smoothed_yaw, vel, accel) in zip(dense_targets, eased):
            rec = {
                "timestamp": round(t, 4),
                "raw_yaw": round(raw_yaw, 2),
                "smoothed_yaw": round(smoothed_yaw, 2),
                "pitch": FIXED_PITCH,
                "fov": FIXED_FOV,
                "yaw_velocity_deg_s": round(vel, 2),
            }
            f.write(json.dumps(rec) + "\n")

    print(f"[smooth_camera_path] {len(eased)} dense frames -> {args.output} "
          f"({args.render_fps} fps, {total_duration:.1f}s source)")
    max_vel_seen = max(abs(v) for _, _, v, _ in eased)
    print(f"[smooth_camera_path] Peak yaw velocity: {max_vel_seen:.1f} deg/s "
          f"(cap: {args.max_pan_speed})")

    if args.render_comparison:
        render_comparison(eased, args.source_video, args.comparison_duration,
                           args.comparison_output, args.render_fps)


# ---------------------------------------------------------------------------
# Comparison render: fixed camera vs smoothed playcam, side by side
# ---------------------------------------------------------------------------

def render_comparison(eased, source_video, duration, out_path, render_fps):
    import cv2
    sys.path.insert(0, str(Path(__file__).parent))
    from play_location import extract_crop_frame

    cap = cv2.VideoCapture(str(source_video))
    if not cap.isOpened():
        print(f"ERROR: cannot open --source-video: {source_video}", file=sys.stderr)
        sys.exit(1)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0

    frames = [e for e in eased if e[0] <= duration]
    if not frames:
        print("ERROR: no eased frames within --comparison-duration", file=sys.stderr)
        sys.exit(1)

    # Fixed camera = literal static shot: first smoothed yaw held for the
    # whole comparison, same pitch/fov -- represents "no tracking at all".
    fixed_yaw = frames[0][1]

    out_w, out_h = 960, 540  # half-res each side, 1920x540 combined
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    tmp_path = out_path.with_suffix(".tmp.mp4")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(tmp_path), fourcc, render_fps, (out_w * 2, out_h))

    print(f"[comparison] Rendering {len(frames)} frames "
          f"({duration:.1f}s) -- fixed yaw={fixed_yaw:.1f} vs smoothed path")

    src_frame = None
    src_idx = -1
    for k, (t, smoothed_yaw, vel, accel) in enumerate(frames):
        target_src_idx = int(round(t * src_fps))
        while src_idx < target_src_idx:
            ret, f = cap.read()
            if not ret:
                break
            src_idx += 1
            src_frame = f
        if src_frame is None:
            continue

        fixed_crop = extract_crop_frame(src_frame, fixed_yaw, pitch_deg=FIXED_PITCH,
                                         fov_deg=FIXED_FOV, out_w=out_w, out_h=out_h)
        smooth_crop = extract_crop_frame(src_frame, smoothed_yaw, pitch_deg=FIXED_PITCH,
                                          fov_deg=FIXED_FOV, out_w=out_w, out_h=out_h)

        cv2.putText(fixed_crop, "FIXED (no tracking)", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
        cv2.putText(smooth_crop, f"SMOOTHED yaw={smoothed_yaw:.1f}", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

        combined = cv2.hconcat([fixed_crop, smooth_crop])
        writer.write(combined)

        if k % 30 == 0:
            print(f"  [{k}/{len(frames)}] t={t:.1f}s smoothed_yaw={smoothed_yaw:.1f}")

    writer.release()
    cap.release()

    import subprocess
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", str(tmp_path), "-c:v", "libx264", "-preset", "fast",
         "-crf", "20", "-pix_fmt", "yuv420p", str(out_path)],
        capture_output=True, text=True)
    tmp_path.unlink(missing_ok=True)
    if result.returncode != 0:
        print("ERROR: ffmpeg re-encode failed:", result.stderr[-2000:], file=sys.stderr)
        sys.exit(1)

    print(f"[comparison] Done -> {out_path}")


if __name__ == "__main__":
    main()
