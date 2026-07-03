#!/usr/bin/env python3
"""
playcam/venue_calibration.py

Standalone calibrator: click around the intended pitch boundary on a still
equirectangular frame, confirm with Enter, and save the resulting polygon
into a playcam venue profile's "play_area" field.

playcam-only. Never reads or writes ball_tracker/venue_mask.json.

Usage:
  python3 playcam/venue_calibration.py \
      --input equirect_frame.jpg \
      --profile playcam/venue_profiles/st_margarets.json

Controls (in the OpenCV window):
  Left click   - add a polygon point
  u            - undo last point
  r            - reset all points
  Enter        - confirm polygon and save
  Esc / q      - quit without saving

Requires a display (run locally, not in a headless sandbox/CI runner).
"""

import argparse
import json
import sys
from pathlib import Path

import cv2

WINDOW_NAME = "playcam venue calibration -- click pitch boundary, Enter to confirm, u=undo, r=reset, Esc=quit"
DISPLAY_MAX_W = 1600


def parse_args():
    p = argparse.ArgumentParser(description="Interactive play_area polygon calibrator")
    p.add_argument("--input", required=True, type=Path,
                    help="Still equirectangular frame (jpg/png) to calibrate against")
    p.add_argument("--profile", required=True, type=Path,
                    help="Venue profile JSON to write play_area into (playcam/venue_profiles/*.json)")
    return p.parse_args()


def validate(args):
    errors = []
    if not args.input.exists():
        errors.append(f"--input does not exist: {args.input}")
    if not args.profile.exists():
        errors.append(f"--profile does not exist: {args.profile}. "
                       f"Create it first (see playcam/venue_profiles/) -- this tool only "
                       f"adds play_area to an existing profile, it doesn't create one.")
    if errors:
        for e in errors:
            print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


def main():
    args = parse_args()
    validate(args)

    try:
        profile = json.loads(args.profile.read_text())
    except json.JSONDecodeError as e:
        print(f"ERROR: --profile is not valid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    frame = cv2.imread(str(args.input))
    if frame is None:
        print(f"ERROR: could not read image: {args.input}", file=sys.stderr)
        sys.exit(1)

    h_full, w_full = frame.shape[:2]
    scale = min(1.0, DISPLAY_MAX_W / w_full)
    disp_w, disp_h = int(w_full * scale), int(h_full * scale)
    display_base = cv2.resize(frame, (disp_w, disp_h))

    points_full = []  # polygon in FULL image resolution (frame_width/frame_height)

    def redraw():
        img = display_base.copy()
        pts_disp = [(int(x * scale), int(y * scale)) for x, y in points_full]
        for i, pt in enumerate(pts_disp):
            cv2.circle(img, pt, 5, (0, 255, 0), -1)
            cv2.putText(img, str(i), (pt[0] + 8, pt[1] - 8),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        if len(pts_disp) >= 2:
            import numpy as np
            cv2.polylines(img, [np.array(pts_disp, dtype="int32")],
                          isClosed=len(pts_disp) >= 3, color=(0, 200, 255), thickness=2)
        cv2.putText(img, f"points={len(points_full)}  "
                          f"(click=add  u=undo  r=reset  Enter=save  Esc/q=quit)",
                    (10, disp_h - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        cv2.imshow(WINDOW_NAME, img)

    def on_mouse(event, x, y, flags, userdata):
        if event == cv2.EVENT_LBUTTONDOWN:
            points_full.append((x / scale, y / scale))
            redraw()

    cv2.namedWindow(WINDOW_NAME)
    cv2.setMouseCallback(WINDOW_NAME, on_mouse)
    redraw()

    saved = False
    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in (27, ord('q')):  # Esc / q
            break
        elif key == ord('u'):
            if points_full:
                points_full.pop()
                redraw()
        elif key == ord('r'):
            points_full.clear()
            redraw()
        elif key in (13, 10):  # Enter
            if len(points_full) < 3:
                print("Need at least 3 points before confirming.")
                continue
            polygon = [[round(x, 1), round(y, 1)] for x, y in points_full]
            profile["play_area"] = {
                "polygon": polygon,
                "frame_width": w_full,
                "frame_height": h_full,
            }
            args.profile.write_text(json.dumps(profile, indent=2) + "\n")
            print(f"Saved {len(polygon)}-point play_area to {args.profile} "
                  f"(frame {w_full}x{h_full})")
            saved = True
            break

    cv2.destroyAllWindows()
    if not saved:
        print("Quit without saving.")
        sys.exit(1)


if __name__ == "__main__":
    main()
