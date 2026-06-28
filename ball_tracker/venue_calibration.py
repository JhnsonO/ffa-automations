#!/usr/bin/env python3
"""Interactive pitch-boundary calibration for equirectangular footage."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import cv2
import numpy as np

DEFAULT_MASK_PATH = Path(__file__).with_name("venue_mask.json")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def read_frame(path):
    """Load an image or extract a representative middle frame from a video."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        frame = cv2.imread(str(path))
        if frame is None:
            raise ValueError(f"Could not read image: {path}")
        return frame

    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise ValueError(f"Could not open video: {path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    if total > 1:
        cap.set(cv2.CAP_PROP_POS_FRAMES, total // 2)
    ok, frame = cap.read()
    cap.release()
    if not ok or frame is None:
        raise ValueError(f"Could not extract frame from video: {path}")
    return frame


def as_polygon(points):
    return np.asarray(points, dtype=np.int32).reshape((-1, 1, 2))


def draw(frame, points, closed=False, preview=False):
    view = frame.copy()
    if len(points) >= 3 and preview:
        overlay = view.copy()
        cv2.fillPoly(overlay, [as_polygon(points)], (0, 180, 0))
        view = cv2.addWeighted(overlay, 0.25, view, 0.75, 0)
    if len(points) >= 2:
        cv2.polylines(view, [as_polygon(points)], closed, (0, 255, 0), 2)
    for point in points:
        cv2.circle(view, point, 5, (0, 0, 255), -1)
    return view


def load_venue_mask(mask_path, frame_width, frame_height):
    """Load a saved mask and reject it if its source dimensions differ."""
    data = json.loads(Path(mask_path).read_text(encoding="utf-8"))
    if data.get("frame_width") != frame_width or data.get("frame_height") != frame_height:
        raise ValueError(
            "Venue mask dimension mismatch: "
            f"mask={data.get('frame_width')}x{data.get('frame_height')}, "
            f"frame={frame_width}x{frame_height}"
        )
    polygon = data.get("polygon")
    if not isinstance(polygon, list) or len(polygon) < 4:
        raise ValueError("Venue mask polygon must contain at least 4 points")
    return data


def save_mask(points, frame, mask_path):
    height, width = frame.shape[:2]
    data = {
        "venue": "default",
        "polygon": [[int(x), int(y)] for x, y in points],
        "frame_width": width,
        "frame_height": height,
        "created": datetime.now(timezone.utc).isoformat(),
    }
    Path(mask_path).write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def calibrate(path, mask_path):
    frame = read_frame(path)
    points = []
    window = "Venue calibration"

    def on_click(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN:
            points.append((x, y))

    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.setMouseCallback(window, on_click)
    print("Click grass boundary points. Enter saves; Escape restarts; q quits.")
    while True:
        view = draw(frame, points, closed=len(points) >= 3, preview=len(points) >= 3)
        cv2.putText(view, "Click boundary | Enter save | Esc restart | q quit", (20, 35),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.imshow(window, view)
        key = cv2.waitKey(20) & 0xFF
        if key in (10, 13):
            if len(points) < 4:
                print("Need at least 4 points.")
            else:
                save_mask(points, frame, mask_path)
                print(f"Saved venue mask to {mask_path}")
                break
        elif key == 27:
            points.clear()
            print("Selection restarted.")
        elif key == ord("q"):
            print("Quit without saving.")
            break
    cv2.destroyAllWindows()


def preview(path, mask_path):
    frame = read_frame(path)
    height, width = frame.shape[:2]
    data = load_venue_mask(mask_path, width, height)
    points = [tuple(point) for point in data["polygon"]]
    window = "Venue mask preview"
    cv2.namedWindow(window, cv2.WINDOW_NORMAL)
    cv2.imshow(window, draw(frame, points, closed=True, preview=True))
    print("Preview loaded. Press any key to close.")
    cv2.waitKey(0)
    cv2.destroyAllWindows()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", help="Equirectangular video or image path")
    parser.add_argument("--preview", action="store_true", help="Overlay existing mask")
    parser.add_argument("--mask", default=str(DEFAULT_MASK_PATH), help="Mask JSON path")
    args = parser.parse_args()
    preview(args.path, args.mask) if args.preview else calibrate(args.path, args.mask)


if __name__ == "__main__":
    main()
