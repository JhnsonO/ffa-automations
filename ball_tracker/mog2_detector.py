#!/usr/bin/env python3
"""Standalone MOG2 blob detector prototype for equirectangular football footage."""
import argparse
import json
import math
from pathlib import Path

import cv2
import numpy as np


def load_venue_mask(mask_path, frame_shape):
    if not mask_path:
        return None
    path = Path(mask_path)
    if not path.exists():
        raise FileNotFoundError(f"Venue mask not found: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))
    polygon = data.get("polygon")
    if not isinstance(polygon, list) or len(polygon) < 4:
        raise ValueError("Venue mask polygon must contain at least 4 points")

    height, width = frame_shape[:2]
    mask_width = data.get("frame_width")
    mask_height = data.get("frame_height")
    if mask_width is not None and mask_height is not None:
        if int(mask_width) != width or int(mask_height) != height:
            raise ValueError(
                f"Venue mask dimension mismatch: mask={mask_width}x{mask_height}, "
                f"frame={width}x{height}"
            )

    binary = np.zeros((height, width), dtype=np.uint8)
    points = np.asarray(polygon, dtype=np.int32).reshape((-1, 1, 2))
    cv2.fillPoly(binary, [points], 255)
    return binary


def circularity_score(area, perimeter):
    if perimeter <= 0:
        return 0.0
    score = 4.0 * math.pi * area / (perimeter * perimeter)
    return float(max(0.0, min(1.0, score)))


def contour_candidate(contour, min_area, max_area, min_circularity, max_aspect_ratio):
    area = cv2.contourArea(contour)
    if area < min_area or area > max_area:
        return None

    perimeter = cv2.arcLength(contour, True)
    conf = circularity_score(area, perimeter)
    if conf < min_circularity:
        return None

    x, y, w, h = cv2.boundingRect(contour)
    if w > 0 and h / w > max_aspect_ratio:
        return None
    return {
        "x": int(x),
        "y": int(y),
        "w": int(w),
        "h": int(h),
        "conf": round(conf, 4),
        "source": "mog2",
    }


def clean_foreground_mask(fgmask):
    _, fgmask = cv2.threshold(fgmask, 127, 255, cv2.THRESH_BINARY)
    kernel = np.ones((3, 3), dtype=np.uint8)
    fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_OPEN, kernel, iterations=1)
    fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_CLOSE, kernel, iterations=1)
    return fgmask


def draw_preview(frame, candidates, frame_idx):
    view = frame.copy()
    for cand in candidates:
        x, y, w, h = cand["x"], cand["y"], cand["w"], cand["h"]
        cv2.rectangle(view, (x, y), (x + w, y + h), (0, 255, 0), 2)
        cv2.putText(
            view,
            f"{cand['conf']:.2f}",
            (x, max(15, y - 5)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            (0, 255, 0),
            1,
            cv2.LINE_AA,
        )
    cv2.putText(
        view,
        f"frame {frame_idx}",
        (20, 35),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (255, 255, 255),
        2,
        cv2.LINE_AA,
    )
    return view


def detect_video(args):
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {args.video}")

    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    start_frame = max(0, int(args.start_frame or 0))
    end_frame = int(args.end_frame) if args.end_frame is not None else total_frames
    end_frame = min(end_frame, total_frames) if total_frames > 0 else end_frame
    if end_frame <= start_frame:
        raise ValueError("end-frame must be greater than start-frame")

    cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)
    ok, first_frame = cap.read()
    if not ok or first_frame is None:
        raise RuntimeError(f"Could not read frame {start_frame}")

    venue_mask = load_venue_mask(args.venue_mask, first_frame.shape)
    mog2 = cv2.createBackgroundSubtractorMOG2(
        history=args.mog2_history,
        varThreshold=args.mog2_var_threshold,
        detectShadows=args.detect_shadows,
    )

    frame_candidates = {}
    frame_idx = start_frame
    frame = first_frame

    while frame_idx < end_frame:
        fgmask = mog2.apply(frame)
        fgmask = clean_foreground_mask(fgmask)
        if venue_mask is not None:
            fgmask = cv2.bitwise_and(fgmask, fgmask, mask=venue_mask)

        contours, _ = cv2.findContours(fgmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            cand = contour_candidate(
                contour,
                args.min_blob_area,
                args.max_blob_area,
                args.min_circularity,
                args.max_aspect_ratio,
            )
            if cand is not None:
                candidates.append(cand)
        candidates.sort(key=lambda c: c["conf"], reverse=True)
        candidates = candidates[:5]
        frame_candidates[str(frame_idx)] = candidates

        if args.display:
            cv2.imshow("MOG2 detector", draw_preview(frame, candidates, frame_idx))
            key = cv2.waitKey(1) & 0xFF
            if key in (27, ord("q")):
                break

        frame_idx += 1
        if frame_idx >= end_frame:
            break
        ok, frame = cap.read()
        if not ok or frame is None:
            break

    cap.release()
    if args.display:
        cv2.destroyAllWindows()

    output = {"frame_candidates": frame_candidates}
    Path(args.output).write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {args.output}")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("video", help="Path to equirectangular video file")
    parser.add_argument("--venue-mask", default=None, help="Optional venue_mask.json path")
    parser.add_argument("--output", default="mog2_candidates.json", help="Output JSON path")
    parser.add_argument("--display", action="store_true", help="Show live blob preview")
    parser.add_argument("--start-frame", type=int, default=0, help="First frame to process")
    parser.add_argument("--end-frame", type=int, default=None, help="Stop before this frame")
    parser.add_argument("--mog2-var-threshold", type=int, default=16)
    parser.add_argument("--mog2-history", type=int, default=500)
    parser.add_argument("--detect-shadows", action="store_true", default=False)
    parser.add_argument("--min-blob-area", type=float, default=100)
    parser.add_argument("--max-blob-area", type=float, default=800)
    parser.add_argument("--min-circularity", type=float, default=0.50)
    parser.add_argument("--max-aspect-ratio", type=float, default=2.5)
    return parser.parse_args()


def main():
    detect_video(parse_args())


if __name__ == "__main__":
    main()
