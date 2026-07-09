"""Approximate equidistant-fisheye undistortion for flatcam.

The map is derived from the lens profile's fov_deg and resolution, then blended
back toward identity by distortion_correction_strength. This is an eyeball-tuned
model for straight fence lines, not calibrated camera intrinsics.

Full-frame remap is a v1 simplicity choice; crop-only remap is the known
optimisation if CPU time matters later.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np

PROFILE_PATH = Path(__file__).with_name("lens_profiles.json")
_MAP_CACHE: dict[tuple[str, int, int, float, float], tuple[np.ndarray, np.ndarray]] = {}


def load_profiles(path: str | Path = PROFILE_PATH) -> dict[str, dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        profiles = json.load(fh)
    return {p["profile_name"]: p for p in profiles}


def load_profile(name: str, path: str | Path = PROFILE_PATH) -> dict[str, Any]:
    profiles = load_profiles(path)
    if name not in profiles:
        raise KeyError(f"unknown lens profile {name!r}; available: {', '.join(sorted(profiles))}")
    return profiles[name]


def _build_map(width: int, height: int, fov_deg: float, strength: float) -> tuple[np.ndarray, np.ndarray]:
    key = ("equidistant", width, height, float(fov_deg), float(strength))
    if key in _MAP_CACHE:
        return _MAP_CACHE[key]

    cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
    xs, ys = np.meshgrid(np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32))
    x = (xs - cx) / cx
    y = (ys - cy) / cy
    ru = np.sqrt(x * x + y * y)
    theta_max = np.deg2rad(fov_deg) / 2.0
    # Pinhole-like undistorted target radius mapped to equidistant source radius.
    theta = np.arctan(np.tan(theta_max) * ru)
    rd = np.divide(theta, theta_max, out=np.zeros_like(theta), where=theta_max != 0)
    scale = np.divide(rd, ru, out=np.ones_like(rd), where=ru > 1e-6)
    fisheye_x = cx + (xs - cx) * scale
    fisheye_y = cy + (ys - cy) * scale
    s = float(np.clip(strength, 0.0, 1.0))
    map_x = (xs * (1.0 - s) + fisheye_x * s).astype(np.float32)
    map_y = (ys * (1.0 - s) + fisheye_y * s).astype(np.float32)
    _MAP_CACHE[key] = (map_x, map_y)
    return map_x, map_y


def undistort_frame(frame: np.ndarray, profile: dict[str, Any]) -> np.ndarray:
    height, width = frame.shape[:2]
    map_x, map_y = _build_map(width, height, float(profile["fov_deg"]), float(profile["distortion_correction_strength"]))
    return cv2.remap(frame, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT)


def draw_grid(frame: np.ndarray, spacing: int = 120) -> np.ndarray:
    out = frame.copy()
    h, w = out.shape[:2]
    for x in range(0, w, spacing):
        cv2.line(out, (x, 0), (x, h - 1), (255, 255, 255), 1, cv2.LINE_AA)
    for y in range(0, h, spacing):
        cv2.line(out, (0, y), (w - 1, y), (255, 255, 255), 1, cv2.LINE_AA)
    return out


def _read_first_frame(path: str) -> np.ndarray:
    img = cv2.imread(path)
    if img is not None:
        return img
    cap = cv2.VideoCapture(path)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        raise RuntimeError(f"could not read image/video: {path}")
    return frame


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", required=True)
    ap.add_argument("--input", required=True)
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--output", default="flatcam/undistort_preview.jpg")
    args = ap.parse_args()
    profile = load_profile(args.profile)
    out = undistort_frame(_read_first_frame(args.input), profile)
    if args.preview:
        out = draw_grid(out)
    cv2.imwrite(args.output, out)
    print(args.output)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
