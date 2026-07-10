"""Generate lens-distortion comparison stills at multiple correction strengths.

Loads one frame, runs it through undistort_frame() at a fixed list of
distortion_correction_strength overrides (profile JSON on disk is never
written), then center-crops each result to match the production crop
(EDGE_MARGIN 0.80 from follow_camera_flat.py) so the stills reflect what
viewers actually see. This is a still-frame comparison tool only -- no
tracking, no FSM, no video render.
"""
from __future__ import annotations

import argparse
import copy
from pathlib import Path

import cv2

from undistort import load_profile, undistort_frame

# Matches follow_camera_flat.py's EDGE_MARGIN -- kept as a literal here
# rather than imported, since this script has no FSM/tracking dependency
# on that module and importing it would pull in unrelated machinery.
EDGE_MARGIN = 0.80

STRENGTHS = [0.0, 0.25, 0.4, 0.55, 0.7, 1.0]


def center_crop(frame, margin: float):
    h, w = frame.shape[:2]
    crop_w, crop_h = int(round(w * margin)), int(round(h * margin))
    x0 = (w - crop_w) // 2
    y0 = (h - crop_h) // 2
    return frame[y0:y0 + crop_h, x0:x0 + crop_w]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--input", required=True, help="single frame image (jpg/png)")
    ap.add_argument("--output-dir", default="flatcam_artifacts")
    args = ap.parse_args()

    base_profile = load_profile(args.profile)
    frame = cv2.imread(args.input)
    if frame is None:
        raise RuntimeError(f"could not read frame image: {args.input}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for strength in STRENGTHS:
        profile = copy.deepcopy(base_profile)
        profile["distortion_correction_strength"] = strength
        undistorted = undistort_frame(frame, profile)
        cropped = center_crop(undistorted, EDGE_MARGIN)
        out_path = out_dir / f"strength_{strength:.2f}.jpg"
        cv2.imwrite(str(out_path), cropped, [cv2.IMWRITE_JPEG_QUALITY, 92])
        print(out_path)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
