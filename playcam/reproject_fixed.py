#!/usr/bin/env python3
"""
playcam/reproject_fixed.py

Phase 0 — fixed-yaw/pitch/FOV reprojection from equirectangular 360 video
to a rectilinear 1920x1080 crop, via FFmpeg's v360 filter.

Independent of ball_tracker/. No tracking, no detection, no homography,
no Kalman smoothing. Purpose: produce one static-viewport render so
framing/image quality can be judged before any play-location logic is built.

Usage:
    python3 reproject_fixed.py --input clip.mp4 --output out.mp4
    python3 reproject_fixed.py --input clip.mp4 --output out.mp4 \
        --yaw -15 --pitch -8 --fov 90

Requires: ffmpeg on PATH with the v360 filter (standard in ffmpeg >= 4.3).
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_YAW = 0.0      # degrees, 0 = straight ahead on the camera's forward reference
DEFAULT_PITCH = 4.0    # degrees, positive tilt — venue/mount-tested (St Margarets, 2026-07-02):
                        # -20 pointed at empty turf, -8 clipped players at top, +4 centred play.
                        # Sign convention is mount-dependent; re-test per venue.
DEFAULT_FOV = 85.0     # degrees, diagonal FOV — venue-tested, felt tighter/more camera-like than 90
MAX_FOV = 95.0
MIN_FOV = 20.0
OUT_W = 1920
OUT_H = 1080


def parse_args():
    p = argparse.ArgumentParser(
        description="Fixed-viewport equirectangular -> rectilinear reprojection (Phase 0)."
    )
    p.add_argument("--input", required=True, type=Path, help="Path to equirectangular input video")
    p.add_argument("--output", required=True, type=Path, help="Path to write the rectilinear MP4")
    p.add_argument("--venue-profile", type=Path, default=None,
                    help="Optional JSON file with {\"yaw\":, \"pitch\":, \"fov\":} defaults for this "
                         "venue/mount. Explicit --yaw/--pitch/--fov flags override profile values.")
    p.add_argument("--yaw", type=float, default=None,
                    help=f"Yaw in degrees, -180..180 (default {DEFAULT_YAW} if no profile given)")
    p.add_argument("--pitch", type=float, default=None,
                    help=f"Pitch in degrees, -90..90 (default {DEFAULT_PITCH} if no profile given)")
    p.add_argument("--fov", type=float, default=None,
                    help=f"Diagonal FOV in degrees, {MIN_FOV}..{MAX_FOV} (default {DEFAULT_FOV} if no profile given)")
    p.add_argument("--crf", type=int, default=18, help="x264 CRF, lower = higher quality (default 18)")
    p.add_argument("--preset", default="medium", help="x264 preset (default medium)")
    p.add_argument("--start", type=float, default=None, help="Optional start time in seconds (-ss)")
    p.add_argument("--duration", type=float, default=None, help="Optional duration in seconds (-t)")
    p.add_argument("--dry-run", action="store_true", help="Print the ffmpeg command without running it")
    return p.parse_args()


def resolve_camera_params(args):
    """Fill unset --yaw/--pitch/--fov from --venue-profile JSON, then from module defaults.
    Explicit CLI flags always take priority over the profile file."""
    profile = {}
    if args.venue_profile is not None:
        if not args.venue_profile.exists():
            print(f"ERROR: --venue-profile file does not exist: {args.venue_profile}", file=sys.stderr)
            sys.exit(1)
        try:
            profile = json.loads(args.venue_profile.read_text())
        except json.JSONDecodeError as e:
            print(f"ERROR: --venue-profile is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        unknown = {k for k in profile if not k.startswith("_")} - {"yaw", "pitch", "fov"}
        if unknown:
            print(f"ERROR: --venue-profile has unknown keys: {sorted(unknown)}", file=sys.stderr)
            sys.exit(1)

    if args.yaw is None:
        args.yaw = profile.get("yaw", DEFAULT_YAW)
    if args.pitch is None:
        args.pitch = profile.get("pitch", DEFAULT_PITCH)
    if args.fov is None:
        args.fov = profile.get("fov", DEFAULT_FOV)


def validate(args):
    errors = []

    if shutil.which("ffmpeg") is None:
        errors.append("ffmpeg not found on PATH.")

    if not args.input.exists():
        errors.append(f"Input file does not exist: {args.input}")
    elif args.input.stat().st_size == 0:
        errors.append(f"Input file is empty: {args.input}")

    if not (-180.0 <= args.yaw <= 180.0):
        errors.append(f"--yaw must be in -180..180, got {args.yaw}")

    if not (-90.0 <= args.pitch <= 90.0):
        errors.append(f"--pitch must be in -90..90, got {args.pitch}")

    if not (MIN_FOV <= args.fov <= MAX_FOV):
        errors.append(f"--fov must be in {MIN_FOV}..{MAX_FOV}, got {args.fov} (hard cap {MAX_FOV})")

    if args.crf < 0 or args.crf > 51:
        errors.append(f"--crf must be 0..51, got {args.crf}")

    if args.start is not None and args.start < 0:
        errors.append(f"--start must be >= 0, got {args.start}")

    if args.duration is not None and args.duration <= 0:
        errors.append(f"--duration must be > 0, got {args.duration}")

    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def build_command(args):
    vf = (
        f"v360=input=e:output=flat:"
        f"yaw={args.yaw}:pitch={args.pitch}:roll=0:"
        f"d_fov={args.fov}:w={OUT_W}:h={OUT_H}:interp=cubic"
    )

    cmd = ["ffmpeg", "-y"]

    if args.start is not None:
        cmd += ["-ss", str(args.start)]

    cmd += ["-i", str(args.input)]

    if args.duration is not None:
        cmd += ["-t", str(args.duration)]

    cmd += [
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", args.preset,
        "-crf", str(args.crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        str(args.output),
    ]
    return cmd


def main():
    args = parse_args()
    resolve_camera_params(args)
    validate(args)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    cmd = build_command(args)
    print("Command:", " ".join(cmd))

    if args.dry_run:
        return

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        print("FFmpeg failed:", file=sys.stderr)
        print(result.stderr[-3000:], file=sys.stderr)
        sys.exit(result.returncode)

    if not args.output.exists() or args.output.stat().st_size == 0:
        print(f"ERROR: ffmpeg reported success but output is missing/empty: {args.output}", file=sys.stderr)
        sys.exit(1)

    print(f"OK: wrote {args.output} ({args.output.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
