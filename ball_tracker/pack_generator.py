#!/usr/bin/env python3
"""Create targeted visual packs for corridor-gated VLM review frames."""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PATCH_FOV_DEG = 60.0  # ±30 degrees around the selected candidate/corridor centre
PATCH_WIDTH = 640
PATCH_HEIGHT = 360
PITCH_LENGTH_M = 105.0
PITCH_WIDTH_M = 68.0


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _queue_items(payload: Any) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        queue = payload.get("queue", payload.get("windows", []))
    else:
        queue = payload
    if not isinstance(queue, list):
        raise ValueError("ai_review_queue must contain a list under 'queue'.")
    return [item for item in queue if isinstance(item, Mapping)]


def _review_frames(item: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    frames = item.get("frames", [])
    if not isinstance(frames, list):
        return []
    return [frame for frame in frames if isinstance(frame, Mapping)]


def _candidate_location(review_frame: Mapping[str, Any]) -> tuple[float, float] | None:
    candidate = review_frame.get("candidate")
    if isinstance(candidate, Mapping):
        yaw = _finite_float(candidate.get("yaw"))
        pitch = _finite_float(candidate.get("pitch"))
        if yaw is not None and pitch is not None:
            return yaw, pitch

    corridor = review_frame.get("corridor")
    if isinstance(corridor, Mapping):
        centre = corridor.get("centre")
        if isinstance(centre, Mapping):
            yaw = _finite_float(centre.get("yaw"))
            pitch = _finite_float(centre.get("pitch"))
            if yaw is not None and pitch is not None:
                return yaw, pitch
    return None


def extract_perspective_patch(
    equirect_frame: Any,
    yaw_deg: float,
    pitch_deg: float,
    *,
    fov_deg: float = PATCH_FOV_DEG,
    out_width: int = PATCH_WIDTH,
    out_height: int = PATCH_HEIGHT,
) -> Any:
    """Extract an equirectangular perspective crop centred on yaw/pitch."""
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("pack_generator requires opencv-python and numpy.") from exc

    height, width = equirect_frame.shape[:2]
    focal = (out_width / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(0, out_width - 1, out_width)
    ys = np.linspace(0, out_height - 1, out_height)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_width / 2.0) / focal
    ry = -(yv - out_height / 2.0) / focal
    rz = np.ones_like(rx)
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm

    yaw = math.radians(yaw_deg)
    wx = math.cos(yaw) * rx + math.sin(yaw) * rz
    wy = ry
    wz = -math.sin(yaw) * rx + math.cos(yaw) * rz

    pitch = math.radians(pitch_deg)
    wx2 = wx
    wy2 = math.cos(pitch) * wy - math.sin(pitch) * wz
    wz2 = math.sin(pitch) * wy + math.cos(pitch) * wz
    yaw_map = np.arctan2(wx2, wz2)
    pitch_map = np.arcsin(np.clip(wy2, -1.0, 1.0))
    map_x = ((yaw_map / (2.0 * math.pi)) + 0.5) * width
    map_y = (0.5 - pitch_map / math.pi) * height

    return cv2.remap(
        equirect_frame,
        map_x.astype(np.float32),
        map_y.astype(np.float32),
        interpolation=cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_WRAP,
    )


def _write_minimap(destination: Path, yaw: float, pitch: float, frame: int) -> None:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.patches import Rectangle
    except ImportError as exc:
        raise RuntimeError("pack_generator requires matplotlib to render minimaps.") from exc

    # This is an orientation minimap, not a calibrated pitch-coordinate estimate.
    x = ((yaw + 180.0) % 360.0) / 360.0 * PITCH_LENGTH_M
    y = (90.0 - max(-90.0, min(90.0, pitch))) / 180.0 * PITCH_WIDTH_M
    figure, axis = plt.subplots(figsize=(6, 4), dpi=130)
    axis.add_patch(Rectangle((0, 0), PITCH_LENGTH_M, PITCH_WIDTH_M, fill=False, linewidth=1.5))
    axis.plot([PITCH_LENGTH_M / 2.0, PITCH_LENGTH_M / 2.0], [0, PITCH_WIDTH_M], linewidth=1.0)
    axis.add_patch(plt.Circle((PITCH_LENGTH_M / 2.0, PITCH_WIDTH_M / 2.0), 9.15, fill=False, linewidth=1.0))

    for x0 in (0.0, PITCH_LENGTH_M - 16.5):
        axis.add_patch(Rectangle((x0, (PITCH_WIDTH_M - 40.32) / 2.0), 16.5, 40.32, fill=False, linewidth=1.0))
        axis.add_patch(Rectangle((x0, (PITCH_WIDTH_M - 18.32) / 2.0), 5.5, 18.32, fill=False, linewidth=1.0))

    axis.scatter([x], [y], s=45, zorder=3)
    axis.set_title(f"Review orientation — frame {frame}")
    axis.set_xlim(-2, PITCH_LENGTH_M + 2)
    axis.set_ylim(PITCH_WIDTH_M + 2, -2)
    axis.set_aspect("equal")
    axis.axis("off")
    figure.tight_layout(pad=0.25)
    figure.savefig(destination, bbox_inches="tight")
    plt.close(figure)


@dataclass
class EquirectFrameReader:
    input_path: Path
    _capture: Any = None
    _image: Any = None

    def __enter__(self) -> "EquirectFrameReader":
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("pack_generator requires opencv-python.") from exc

        if self.input_path.is_dir():
            return self
        if self.input_path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            self._image = cv2.imread(str(self.input_path))
            if self._image is None:
                raise FileNotFoundError(f"Could not read equirectangular image: {self.input_path}")
            return self

        self._capture = cv2.VideoCapture(str(self.input_path))
        if not self._capture.isOpened():
            raise FileNotFoundError(f"Could not open equirectangular video: {self.input_path}")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._capture is not None:
            self._capture.release()

    def read(self, frame: int) -> Any:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("pack_generator requires opencv-python.") from exc

        if self.input_path.is_dir():
            names = (
                f"frame_{frame}.jpg",
                f"frame_{frame:06d}.jpg",
                f"frame_{frame}.png",
                f"frame_{frame:06d}.png",
            )
            for name in names:
                image_path = self.input_path / name
                if image_path.is_file():
                    image = cv2.imread(str(image_path))
                    if image is not None:
                        return image
            raise FileNotFoundError(f"No frame image found for frame {frame} in {self.input_path}")

        if self._image is not None:
            return self._image.copy()
        assert self._capture is not None
        self._capture.set(cv2.CAP_PROP_POS_FRAMES, frame)
        ok, image = self._capture.read()
        if not ok or image is None:
            raise RuntimeError(f"Could not decode frame {frame} from {self.input_path}")
        return image


def generate_packs(
    queue_payload: Any,
    equirect_input: str | Path,
    output_dir: str | Path = "ai_review_packs",
) -> dict[str, int]:
    try:
        import cv2
    except ImportError as exc:
        raise RuntimeError("pack_generator requires opencv-python.") from exc

    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    generated = 0
    skipped = 0
    seen: set[tuple[str, int]] = set()

    with EquirectFrameReader(Path(equirect_input)) as reader:
        for queue_item in _queue_items(queue_payload):
            window_id = str(queue_item.get("window_id", ""))
            if not window_id:
                continue
            window_dir = output_root / window_id

            for review_frame in _review_frames(queue_item):
                if not review_frame.get("eligible_for_vlm", False):
                    skipped += 1
                    continue
                try:
                    frame = int(review_frame["frame"])
                except (KeyError, TypeError, ValueError):
                    skipped += 1
                    continue
                if (window_id, frame) in seen:
                    continue
                seen.add((window_id, frame))

                location = _candidate_location(review_frame)
                if location is None:
                    skipped += 1
                    continue
                yaw, pitch = location
                patch = extract_perspective_patch(reader.read(frame), yaw, pitch)
                window_dir.mkdir(parents=True, exist_ok=True)
                patch_path = window_dir / f"frame_{frame}.jpg"
                minimap_path = window_dir / f"minimap_{frame}.png"
                if not cv2.imwrite(str(patch_path), patch):
                    raise RuntimeError(f"Failed to write patch image: {patch_path}")
                _write_minimap(minimap_path, yaw, pitch, frame)
                generated += 1

    return {"generated": generated, "skipped": skipped}


def write_packs(
    queue_path: str | Path,
    equirect_input: str | Path,
    output_dir: str | Path = "ai_review_packs",
) -> dict[str, int]:
    with Path(queue_path).open("r", encoding="utf-8") as handle:
        queue_payload = json.load(handle)
    return generate_packs(queue_payload, equirect_input, output_dir)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate targeted VLM review visual packs.")
    parser.add_argument("--queue", required=True)
    parser.add_argument("--input", required=True, help="Equirectangular video, image, or frame-image directory")
    parser.add_argument("--output-dir", default="ai_review_packs")
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    result = write_packs(args.queue, args.input, args.output_dir)
    print(f"[pack-generator] generated={result['generated']} skipped={result['skipped']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
