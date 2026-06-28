#!/usr/bin/env python3
"""Headless venue calibration helper for equirectangular footage."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np


DEFAULT_VENUE = "st_margarets"


def extract_frame(video_path, frame_idx, output_frame):
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Could not open video: {video_path}")

    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_idx))
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise RuntimeError(f"Could not read frame {frame_idx} from {video_path}")

    output_frame = Path(output_frame)
    output_frame.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(output_frame), frame):
        raise RuntimeError(f"Could not write frame to {output_frame}")
    print(f"Wrote frame {frame_idx} to {output_frame}")


def parse_points(points_text):
    points = []
    for token in points_text.split():
        parts = token.split(",")
        if len(parts) != 2:
            raise ValueError(f"Invalid point '{token}', expected x,y")
        try:
            x, y = int(parts[0]), int(parts[1])
        except ValueError as exc:
            raise ValueError(f"Invalid integer point '{token}', expected x,y") from exc
        points.append([x, y])

    if len(points) < 3:
        raise ValueError("At least 3 points are required to write a polygon")
    return points


def write_mask(points_text, output_path, venue=DEFAULT_VENUE):
    points = parse_points(points_text)
    data = {
        "venue": venue,
        "polygons": [
            {"points": points}
        ],
    }

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(points)}-point polygon to {output_path}")


def validate_args(args, parser):
    extract_mode = args.video is not None or args.extract_frame is not None or args.output_frame is not None
    write_mode = args.points is not None or args.output is not None

    if extract_mode and write_mode:
        parser.error("Use either extract-frame mode or write-mask mode, not both")

    if extract_mode:
        missing = []
        if args.video is None:
            missing.append("--video")
        if args.extract_frame is None:
            missing.append("--extract-frame")
        if args.output_frame is None:
            missing.append("--output-frame")
        if missing:
            parser.error(f"Extract-frame mode missing: {', '.join(missing)}")
        return "extract"

    if write_mode:
        missing = []
        if args.points is None:
            missing.append("--points")
        if args.output is None:
            missing.append("--output")
        if missing:
            parser.error(f"Write-mask mode missing: {', '.join(missing)}")
        return "write"

    parser.error("Choose extract-frame mode or write-mask mode")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--video", help="Path to equirectangular video")
    parser.add_argument("--extract-frame", type=int, help="Frame index to save as JPEG")
    parser.add_argument("--output-frame", help="Output image path")
    parser.add_argument("--points", help='Space-separated polygon points, e.g. "10,20 30,40 50,60"')
    parser.add_argument("--output", help="Output venue_mask.json path")
    args = parser.parse_args()
    return args, validate_args(args, parser)


def main():
    args, mode = parse_args()
    if mode == "extract":
        extract_frame(args.video, args.extract_frame, args.output_frame)
    else:
        write_mask(args.points, args.output)


if __name__ == "__main__":
    main()
