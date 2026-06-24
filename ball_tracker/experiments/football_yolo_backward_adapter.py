#!/usr/bin/env python3
"""Modern football-YOLO candidate adapter + backward-anchor propagation.

EXPERIMENT ONLY.

This adapter is intentionally model-agnostic: any Ultralytics-compatible
football checkpoint can be passed via --model once a verified checkpoint is
selected. It emits detector candidates in the existing spherical yaw/pitch
contract and feeds those candidates into backward_anchor_propagation.py.

It does not modify Stage 1/1b/2, Tier A, the renderer, or production tracking.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Tuple

import cv2
import numpy as np
from ultralytics import YOLO

from ball_tracker.experiments.backward_anchor_propagation import BackwardConfig, propagate_backward

CROP_YAWS = (0, 90, 180, 270)
CROP_FOV_DEG = 110.0
CROP_W, CROP_H = 1280, 720


def crop_to_equirect(video_frame: np.ndarray, crop_yaw_deg: float) -> np.ndarray:
    """Project one perspective crop from equirectangular source footage."""
    h_eq, w_eq = video_frame.shape[:2]
    focal = (CROP_W / 2.0) / math.tan(math.radians(CROP_FOV_DEG / 2.0))
    xs = np.linspace(0, CROP_W - 1, CROP_W)
    ys = np.linspace(0, CROP_H - 1, CROP_H)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - CROP_W / 2.0) / focal
    ry = -(yv - CROP_H / 2.0) / focal
    rz = np.ones_like(rx)
    norm = np.sqrt(rx * rx + ry * ry + rz * rz)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    yaw = math.radians(crop_yaw_deg)
    wx = math.cos(yaw) * rx + math.sin(yaw) * rz
    wy = ry
    wz = -math.sin(yaw) * rx + math.cos(yaw) * rz
    map_x = ((np.arctan2(wx, wz) / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - np.arcsin(np.clip(wy, -1.0, 1.0)) / math.pi) * h_eq
    return cv2.remap(
        video_frame,
        map_x.astype(np.float32),
        map_y.astype(np.float32),
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_WRAP,
    )


def pixel_to_yaw_pitch(x: float, y: float, crop_yaw_deg: float) -> Tuple[float, float]:
    focal = (CROP_W / 2.0) / math.tan(math.radians(CROP_FOV_DEG / 2.0))
    rx = (x - CROP_W / 2.0) / focal
    ry = -(y - CROP_H / 2.0) / focal
    rz = 1.0
    norm = math.sqrt(rx * rx + ry * ry + rz * rz)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    yaw = math.radians(crop_yaw_deg)
    wx = math.cos(yaw) * rx + math.sin(yaw) * rz
    wy = ry
    wz = -math.sin(yaw) * rx + math.cos(yaw) * rz
    out_yaw = math.degrees(math.atan2(wx, wz))
    out_pitch = math.degrees(math.asin(max(-1.0, min(1.0, wy))))
    return out_yaw, out_pitch


def detect_frame(
    model: YOLO,
    frame: np.ndarray,
    ball_class: int,
    confidence: float,
    imgsz: int,
) -> List[Dict[str, Any]]:
    """Run selected football YOLO checkpoint over all four perspective crops."""
    candidates: List[Dict[str, Any]] = []
    for crop_yaw in CROP_YAWS:
        crop = crop_to_equirect(frame, crop_yaw)
        result = model.predict(crop, conf=confidence, imgsz=imgsz, verbose=False)[0]
        if result.boxes is None:
            continue
        boxes = result.boxes.xyxy.cpu().numpy()
        scores = result.boxes.conf.cpu().numpy()
        classes = result.boxes.cls.cpu().numpy().astype(int)
        for box, score, cls in zip(boxes, scores, classes):
            if int(cls) != ball_class:
                continue
            x1, y1, x2, y2 = map(float, box)
            centre_x, centre_y = (x1 + x2) / 2.0, (y1 + y2) / 2.0
            yaw, pitch = pixel_to_yaw_pitch(centre_x, centre_y, crop_yaw)
            candidates.append({
                "yaw": yaw,
                "pitch": pitch,
                "football_conf": float(score),
                "crop_yaw": crop_yaw,
                "detection_geometry": {
                    "bbox_xyxy": [x1, y1, x2, y2],
                    "width": x2 - x1,
                    "height": y2 - y1,
                },
                "source": "modern_football_yolo",
            })
    return candidates


def run(args: argparse.Namespace) -> None:
    model = YOLO(args.model)
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open {args.video}")

    frame_candidates: Dict[int, List[Dict[str, Any]]] = {}
    for frame_index in range(args.stop_frame, args.anchor_frame + 1):
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
        ok, frame = cap.read()
        if not ok:
            raise RuntimeError(f"Cannot read frame {frame_index}")
        frame_candidates[frame_index] = detect_frame(
            model,
            frame,
            args.ball_class,
            args.confidence,
            args.imgsz,
        )
        print(f"[football-yolo] frame={frame_index} candidates={len(frame_candidates[frame_index])}")
    cap.release()

    anchor = {
        "yaw": args.anchor_yaw,
        "pitch": args.anchor_pitch,
        "football_conf": 1.0,
        "source": "manual_or_verified_anchor",
    }
    path = propagate_backward(
        frame_candidates,
        anchor,
        args.anchor_frame,
        args.stop_frame,
        BackwardConfig(max_jump_deg=args.max_jump_deg, max_gap_frames=args.max_gap_frames),
    )
    output = {
        "model": args.model,
        "ball_class": args.ball_class,
        "confidence": args.confidence,
        "imgsz": args.imgsz,
        "anchor": anchor,
        "frames": frame_candidates,
        "backward_path": path,
    }
    Path(args.output).write_text(json.dumps(output, indent=2))
    print(f"BACKWARD_PATH_POINTS={len(path)}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True, help="Ultralytics-compatible football YOLO .pt checkpoint")
    parser.add_argument("--video", required=True)
    parser.add_argument("--anchor-frame", type=int, required=True)
    parser.add_argument("--anchor-yaw", type=float, required=True)
    parser.add_argument("--anchor-pitch", type=float, required=True)
    parser.add_argument("--stop-frame", type=int, required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--ball-class", type=int, default=0)
    parser.add_argument("--confidence", type=float, default=0.08)
    parser.add_argument("--imgsz", type=int, default=1280)
    parser.add_argument("--max-jump-deg", type=float, default=8.0)
    parser.add_argument("--max-gap-frames", type=int, default=3)
    args = parser.parse_args()
    run(args)


if __name__ == "__main__":
    main()
