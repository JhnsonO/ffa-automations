#!/usr/bin/env python3
"""
Multi-cue ball candidate diagnostic (EXPERIMENT ONLY).

Creates a small, visual evidence pack for a fixed Tier A experimental sample.
It combines independent diagnostic cues without changing the tracker,
thresholds, filters, or renderer:

- existing ball-detector confidence;
- a vertical playable-view band (not a learned pitch mask);
- person / lower-body proximity from a pose model;
- existing Stage 1c bounding-box metadata;
- existing temporal tracklet metadata.

The fused number is explicitly a review aid only. It never accepts or rejects
candidates and is not written back into any pipeline output.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

CROP_YAWS = (0, 90, 180, 270)
CROP_FOV_DEG = 110.0
CROP_W, CROP_H = 1280, 720

# Deliberately a soft vertical view-band, not a claimed full pitch polygon.
# A true playable-area mask needs its own clip/camera calibration task.
VIEW_BAND_MIN_DEG = -30.0
VIEW_BAND_MAX_DEG = 18.0

SAMPLE_IDS = (
    "T0001", "T0025",                 # likely ball
    "T0093", "T0080", "T0079", "T0036",  # known false positives
    "T0130", "T0030", "T0090", "T0175",  # unclear
)

# Fixed review-only weights. Never used as a production decision.
FUSED_WEIGHTS = {
    "detector": 0.35,
    "view_band": 0.10,
    "pose": 0.20,
    "geometry": 0.10,
    "temporal": 0.25,
}

COL = {
    "bg": (14, 14, 22),
    "panel": (22, 22, 34),
    "white": (232, 232, 232),
    "dim": (145, 145, 160),
    "target": (222, 80, 240),
    "candidate": (70, 190, 240),
    "person": (60, 225, 100),
    "lower": (255, 205, 55),
    "good": (60, 220, 100),
    "warn": (230, 190, 45),
    "bad": (225, 75, 75),
    "header": (37, 40, 68),
    "inset": (8, 8, 12),
}


def clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, float(value)))


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    paths = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSansMono{'Bold' if bold else ''}.ttf",
        f"/usr/share/fonts/truetype/liberation/LiberationMono-{'Bold' if bold else 'Regular'}.ttf",
    ]
    for path in paths:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    return ImageFont.load_default()


# ── Sphere / projection helpers ─────────────────────────────────────────────

def angular_distance(yaw_a: float, pitch_a: float, yaw_b: float, pitch_b: float) -> float:
    dy = math.radians(yaw_a - yaw_b)
    a = (
        math.sin(math.radians(pitch_a)) * math.sin(math.radians(pitch_b))
        + math.cos(math.radians(pitch_a)) * math.cos(math.radians(pitch_b)) * math.cos(dy)
    )
    return math.degrees(math.acos(clamp(a, -1.0, 1.0)))


def extract_crop(equirect_bgr: np.ndarray, crop_yaw_deg: float,
                 fov_deg: float = CROP_FOV_DEG, out_w: int = CROP_W,
                 out_h: int = CROP_H) -> np.ndarray:
    """Projection copied from Stage 1 / anchor-review geometry."""
    h_eq, w_eq = equirect_bgr.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_w / 2.0) / f
    ry = -(yv - out_h / 2.0) / f
    rz = np.ones_like(rx)
    norm = np.sqrt(rx ** 2 + ry ** 2 + rz ** 2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    cy = math.radians(crop_yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    map_x = ((np.arctan2(wx, wz) / (2.0 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - np.arcsin(np.clip(wy, -1, 1)) / math.pi) * h_eq
    return cv2.remap(
        equirect_bgr,
        map_x.astype(np.float32),
        map_y.astype(np.float32),
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_WRAP,
    )


def yaw_pitch_to_pixel(yaw_deg: float, pitch_deg: float, crop_yaw_deg: float,
                        fov_deg: float = CROP_FOV_DEG, width: int = CROP_W,
                        height: int = CROP_H) -> Optional[Tuple[float, float]]:
    f = (width / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    ya, pa = math.radians(yaw_deg), math.radians(pitch_deg)
    wx = math.sin(ya) * math.cos(pa)
    wy = math.sin(pa)
    wz = math.cos(ya) * math.cos(pa)
    cy = math.radians(crop_yaw_deg)
    rx = math.cos(-cy) * wx + math.sin(-cy) * wz
    rz = -math.sin(-cy) * wx + math.cos(-cy) * wz
    if rz <= 0:
        return None
    px = (rx / rz) * f + width / 2.0
    py = -(wy / rz) * f + height / 2.0
    if 0 <= px < width and 0 <= py < height:
        return px, py
    return None


def nearest_crop_yaw(yaw_deg: float) -> int:
    def diff(a: float, b: float) -> float:
        d = abs(a - b) % 360.0
        return min(d, 360.0 - d)
    return min(CROP_YAWS, key=lambda value: diff(yaw_deg, value))


# ── Data helpers ─────────────────────────────────────────────────────────────

def load_json(path: str | Path) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def selected_observations(tracklet: Dict[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    frames = tracklet.get("frames", [])
    if not frames:
        return []
    if len(frames) == 1:
        return [("EARLY", frames[0]), ("MID", frames[0]), ("LATE", frames[0])]
    if len(frames) == 2:
        return [("EARLY", frames[0]), ("MID", frames[0]), ("LATE", frames[-1])]
    return [("EARLY", frames[0]), ("MID", frames[len(frames) // 2]), ("LATE", frames[-1])]


def make_frame_index(candidates_data: Dict[str, Any]) -> Dict[int, List[Dict[str, Any]]]:
    output: Dict[int, List[Dict[str, Any]]] = {}
    for raw_frame, candidates in candidates_data.get("frames", {}).items():
        try:
            output[int(raw_frame)] = candidates if isinstance(candidates, list) else []
        except (TypeError, ValueError):
            continue
    return output


def match_source_candidate(candidates: Sequence[Dict[str, Any]], obs: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the corresponding Stage 1 candidate for an associated Stage 2 observation."""
    yaw, pitch = obs.get("yaw"), obs.get("pitch")
    if yaw is None or pitch is None:
        return None
    best: Optional[Tuple[float, Dict[str, Any]]] = None
    for candidate in candidates:
        if candidate.get("yaw") is None or candidate.get("pitch") is None:
            continue
        distance = angular_distance(float(yaw), float(pitch), float(candidate["yaw"]), float(candidate["pitch"]))
        if best is None or distance < best[0]:
            best = (distance, candidate)
    # Tracklet observations normally match exactly. Do not claim an unrelated
    # same-frame candidate if coordinates have drifted.
    return best[1] if best and best[0] <= 0.03 else None


def bbox_from_geometry(geometry: Any) -> Optional[Tuple[float, float, float, float]]:
    if not isinstance(geometry, dict):
        return None
    values = geometry.get("bbox_xyxy")
    if not isinstance(values, (list, tuple)) or len(values) != 4:
        return None
    try:
        x1, y1, x2, y2 = [float(value) for value in values]
    except (TypeError, ValueError):
        return None
    if x2 <= x1 or y2 <= y1:
        return None
    return x1, y1, x2, y2


def candidate_pixel(candidate: Optional[Dict[str, Any]], obs: Dict[str, Any], crop_yaw: int) -> Optional[Tuple[float, float]]:
    geometry = candidate.get("detection_geometry") if candidate else obs.get("detection_geometry")
    bbox = bbox_from_geometry(geometry)
    if bbox:
        x1, y1, x2, y2 = bbox
        return (x1 + x2) / 2.0, (y1 + y2) / 2.0
    if obs.get("yaw") is None or obs.get("pitch") is None:
        return None
    return yaw_pitch_to_pixel(float(obs["yaw"]), float(obs["pitch"]), crop_yaw)


def geometry_values(candidate: Optional[Dict[str, Any]], obs: Dict[str, Any]) -> Dict[str, Optional[float]]:
    geo = candidate.get("detection_geometry") if candidate else obs.get("detection_geometry")
    if not isinstance(geo, dict):
        return {"width": None, "height": None, "area": None, "aspect": None}

    def get_num(*keys: str) -> Optional[float]:
        for key in keys:
            value = geo.get(key)
            if value is not None:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    pass
        return None

    width = get_num("bbox_width_px", "width_px", "width")
    height = get_num("bbox_height_px", "height_px", "height")
    area = get_num("bbox_area_px", "area_px", "area")
    aspect = get_num("bbox_aspect_ratio", "aspect_ratio")
    bbox = bbox_from_geometry(geo)
    if bbox:
        x1, y1, x2, y2 = bbox
        width = width if width is not None else x2 - x1
        height = height if height is not None else y2 - y1
        area = area if area is not None else (x2 - x1) * (y2 - y1)
        aspect = aspect if aspect is not None and aspect > 0 else (x2 - x1) / max(1e-6, y2 - y1)
    return {"width": width, "height": height, "area": area, "aspect": aspect}


def point_to_rect_distance(point: Tuple[float, float], rect: Tuple[float, float, float, float]) -> float:
    px, py = point
    x1, y1, x2, y2 = rect
    dx = max(x1 - px, 0.0, px - x2)
    dy = max(y1 - py, 0.0, py - y2)
    return math.hypot(dx, dy)


# ── Pose ─────────────────────────────────────────────────────────────────────

def load_pose_model(weights: str):
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise RuntimeError("ultralytics is required: pip install ultralytics") from exc
    return YOLO(weights)


def preflight_pose(weights: str, device: str) -> None:
    model = load_pose_model(weights)
    blank = np.zeros((64, 64, 3), dtype=np.uint8)
    model.predict(blank, imgsz=64, conf=0.25, device=device, verbose=False)
    print(f"POSE_PREFLIGHT_OK model={weights} device={device}")


def infer_people(model: Any, crop_bgr: np.ndarray, conf: float, imgsz: int, device: str) -> List[Dict[str, Any]]:
    results = model.predict(crop_bgr, conf=conf, imgsz=imgsz, device=device, verbose=False)
    if not results:
        return []
    result = results[0]
    if result.boxes is None or len(result.boxes) == 0:
        return []
    boxes = result.boxes.xyxy.cpu().numpy()
    confidences = result.boxes.conf.cpu().numpy()
    try:
        keypoint_data = result.keypoints.data.cpu().numpy() if result.keypoints is not None else None
    except Exception:
        keypoint_data = None

    people: List[Dict[str, Any]] = []
    for idx, box in enumerate(boxes):
        x1, y1, x2, y2 = [float(value) for value in box]
        lower: List[Tuple[float, float]] = []
        ankles: List[Tuple[float, float]] = []
        if keypoint_data is not None and idx < len(keypoint_data):
            keypoints = keypoint_data[idx]
            # COCO pose layout: knees 13/14; ankles 15/16.
            for kp_index in (13, 14, 15, 16):
                if kp_index >= len(keypoints):
                    continue
                x, y, score = [float(value) for value in keypoints[kp_index]]
                if score >= 0.20:
                    lower.append((x, y))
                    if kp_index in (15, 16):
                        ankles.append((x, y))
        people.append({
            "bbox": (x1, y1, x2, y2),
            "conf": float(confidences[idx]),
            "lower": lower,
            "ankles": ankles,
        })
    return people


def pose_metrics(target_px: Optional[Tuple[float, float]], people: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if target_px is None or not people:
        return {"person_distance_px": None, "lower_body_distance_px": None, "ankle_distance_px": None, "pose_score": None}
    person_distance = min(point_to_rect_distance(target_px, person["bbox"]) for person in people)
    lower_points = [point for person in people for point in person.get("lower", [])]
    ankle_points = [point for person in people for point in person.get("ankles", [])]
    lower_distance = min((math.dist(target_px, point) for point in lower_points), default=None)
    ankle_distance = min((math.dist(target_px, point) for point in ankle_points), default=None)

    # Interaction support only. Missing/occluded ankles are unknown rather than a penalty.
    if ankle_distance is not None:
        score = clamp(1.0 - ankle_distance / 260.0)
    elif lower_distance is not None:
        score = clamp(1.0 - lower_distance / 320.0)
    else:
        score = None
    return {
        "person_distance_px": person_distance,
        "lower_body_distance_px": lower_distance,
        "ankle_distance_px": ankle_distance,
        "pose_score": score,
    }


# ── Diagnostic cues ──────────────────────────────────────────────────────────

def view_band_cue(pitch: Optional[float]) -> Tuple[str, Optional[float]]:
    if pitch is None:
        return "unknown", None
    if VIEW_BAND_MIN_DEG <= float(pitch) <= VIEW_BAND_MAX_DEG:
        return "valid", 0.75
    return "invalid", 0.10


def geometry_cue(values: Dict[str, Optional[float]]) -> Optional[float]:
    aspect = values.get("aspect")
    if aspect is None or aspect <= 0:
        return None
    # Square-like compactness only; deliberately no hard ball-size prior.
    return clamp(1.0 - abs(math.log(aspect)) / math.log(4.0))


def temporal_cue(tracklet: Dict[str, Any]) -> float:
    obs_count = float(tracklet.get("observation_count") or len(tracklet.get("frames", [])) or 0)
    spread = float(tracklet.get("spatial_spread_deg") or 0.0)
    net_disp = float(tracklet.get("net_displacement_deg") or 0.0)
    velocity = tracklet.get("velocity_consistency")
    velocity_score = float(velocity) if velocity is not None else 0.5
    # Fixed broad scales: review aids, never fitted to the two positive labels.
    return (
        0.30 * clamp(math.log1p(obs_count) / math.log1p(120.0))
        + 0.25 * clamp(spread / 10.0)
        + 0.20 * clamp(net_disp / 20.0)
        + 0.25 * clamp(velocity_score)
    )


def fused_score(cues: Dict[str, Optional[float]]) -> Optional[float]:
    active = [(key, value) for key, value in cues.items() if value is not None and key in FUSED_WEIGHTS]
    if not active:
        return None
    total_weight = sum(FUSED_WEIGHTS[key] for key, _ in active)
    return sum(FUSED_WEIGHTS[key] * float(value) for key, value in active) / total_weight


def cue_bundle(tracklet: Dict[str, Any], obs: Dict[str, Any], candidate: Optional[Dict[str, Any]], pose: Dict[str, Any]) -> Dict[str, Any]:
    raw_conf = candidate.get("raw_conf", candidate.get("weighted_conf")) if candidate else obs.get("weighted_conf")
    try:
        detector_score = clamp(float(raw_conf)) if raw_conf is not None else None
    except (TypeError, ValueError):
        detector_score = None
    status, view_score = view_band_cue(obs.get("pitch"))
    geo = geometry_values(candidate, obs)
    cues = {
        "detector": detector_score,
        "view_band": view_score,
        "pose": pose.get("pose_score"),
        "geometry": geometry_cue(geo),
        "temporal": temporal_cue(tracklet),
    }
    return {
        "detector_raw_conf": raw_conf,
        "view_band_status": status,
        "geometry": geo,
        "temporal_score": cues["temporal"],
        "cue_scores": cues,
        "fused_score": fused_score(cues),
    }


# ── Drawing ──────────────────────────────────────────────────────────────────

def draw_crosshair(image: np.ndarray, point: Tuple[float, float], colour: Tuple[int, int, int], label: Optional[str] = None) -> None:
    x, y = int(round(point[0])), int(round(point[1]))
    cv2.circle(image, (x, y), 17, colour, 2, cv2.LINE_AA)
    cv2.line(image, (x - 22, y), (x + 22, y), colour, 1, cv2.LINE_AA)
    cv2.line(image, (x, y - 22), (x, y + 22), colour, 1, cv2.LINE_AA)
    cv2.circle(image, (x, y), 3, colour, -1, cv2.LINE_AA)
    if label:
        cv2.putText(image, label, (x + 20, max(18, y - 18)), cv2.FONT_HERSHEY_SIMPLEX, 0.48, colour, 1, cv2.LINE_AA)


def draw_candidate_overlay(crop: np.ndarray, source_candidates: Sequence[Dict[str, Any]],
                           target: Optional[Dict[str, Any]], obs: Dict[str, Any], crop_yaw: int) -> Optional[Tuple[float, float]]:
    for candidate in source_candidates:
        source_yaw = candidate.get("crop_yaw")
        if source_yaw is not None and int(round(float(source_yaw))) != int(crop_yaw):
            continue
        bbox = bbox_from_geometry(candidate.get("detection_geometry"))
        if bbox:
            x1, y1, x2, y2 = [int(round(value)) for value in bbox]
            cv2.rectangle(crop, (x1, y1), (x2, y2), COL["candidate"], 1, cv2.LINE_AA)
        else:
            point = yaw_pitch_to_pixel(float(candidate.get("yaw", 0.0)), float(candidate.get("pitch", 0.0)), crop_yaw)
            if point:
                cv2.circle(crop, (int(point[0]), int(point[1])), 6, COL["candidate"], 1, cv2.LINE_AA)

    point = candidate_pixel(target, obs, crop_yaw)
    bbox = bbox_from_geometry(target.get("detection_geometry")) if target else None
    if bbox:
        x1, y1, x2, y2 = [int(round(value)) for value in bbox]
        cv2.rectangle(crop, (x1, y1), (x2, y2), COL["target"], 3, cv2.LINE_AA)
    if point:
        draw_crosshair(crop, point, COL["target"], "TRACKLET CANDIDATE")
    return point


def draw_people(crop: np.ndarray, people: Sequence[Dict[str, Any]]) -> None:
    for person in people:
        x1, y1, x2, y2 = [int(round(value)) for value in person["bbox"]]
        cv2.rectangle(crop, (x1, y1), (x2, y2), COL["person"], 2, cv2.LINE_AA)
        cv2.putText(crop, f"person {person['conf']:.2f}", (x1, max(18, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, COL["person"], 1, cv2.LINE_AA)
        for point in person.get("lower", []):
            cv2.circle(crop, (int(round(point[0])), int(round(point[1]))), 4, COL["lower"], -1, cv2.LINE_AA)


def make_zoom(crop: np.ndarray, target: Optional[Tuple[float, float]], size: int = 178, radius: int = 130) -> np.ndarray:
    if target is None:
        return np.full((size, size, 3), 18, dtype=np.uint8)
    x, y = int(round(target[0])), int(round(target[1]))
    x1, x2 = x - radius, x + radius
    y1, y2 = y - radius, y + radius
    pad_l, pad_t = max(0, -x1), max(0, -y1)
    pad_r, pad_b = max(0, x2 - crop.shape[1]), max(0, y2 - crop.shape[0])
    padded = cv2.copyMakeBorder(crop, pad_t, pad_b, pad_l, pad_r, cv2.BORDER_CONSTANT, value=COL["inset"])
    x1 += pad_l; x2 += pad_l; y1 += pad_t; y2 += pad_t
    return cv2.resize(padded[y1:y2, x1:x2], (size, size), interpolation=cv2.INTER_NEAREST)


def bgr_to_pil(image: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))


def cue_colour(value: Optional[float]) -> Tuple[int, int, int]:
    if value is None:
        return COL["dim"]
    if value >= 0.67:
        return COL["good"]
    if value >= 0.34:
        return COL["warn"]
    return COL["bad"]


def _fmt(value: Optional[float]) -> str:
    if value is None:
        return "unknown"
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "unknown"


def render_tracklet_page(tracklet: Dict[str, Any], records: Sequence[Dict[str, Any]]) -> Image.Image:
    page_w, header_h, panel_h, info_h, footer_h = 1920, 70, 360, 172, 34
    page_h = header_h + panel_h + info_h + footer_h
    page = Image.new("RGB", (page_w, page_h), COL["bg"])
    draw = ImageDraw.Draw(page)
    fn = _font(18)
    fn_small = _font(14)
    fn_bold = _font(21, True)

    draw.rectangle([0, 0, page_w - 1, header_h - 1], fill=COL["header"])
    draw.text((14, 9), f"MULTI-CUE DIAGNOSTIC  {tracklet['id']}  [{tracklet.get('status', 'unknown')}]", fill=COL["white"], font=fn_bold)
    draw.text((14, 39), "EXPERIMENT ONLY — cues are review aids; fused score is not a verdict or tracker input.", fill=COL["dim"], font=fn_small)

    for index, record in enumerate(records):
        x0 = index * 640
        crop = record["overlay"]
        page.paste(bgr_to_pil(cv2.resize(crop, (640, 360), interpolation=cv2.INTER_AREA)), (x0, header_h))
        zoom = make_zoom(crop, record["target_point"])
        page.paste(bgr_to_pil(zoom), (x0 + 452, header_h + 170))
        draw.rectangle([x0 + 449, header_h + 167, x0 + 633, header_h + 353], outline=COL["white"], width=1)

        y = header_h + panel_h + 8
        draw.rectangle([x0 + 4, y - 4, x0 + 636, header_h + panel_h + info_h - 7], fill=COL["panel"])
        label, obs, cue, pose = record["label"], record["obs"], record["cue"], record["pose"]
        geo = cue["geometry"]
        draw.text((x0 + 12, y + 2), f"{label}  fr {obs['frame']}  yaw {obs['yaw']:+.2f}°  pitch {obs['pitch']:+.2f}°", fill=COL["white"], font=fn)
        raw = cue["detector_raw_conf"]
        score = cue["fused_score"]
        draw.text((x0 + 12, y + 27), f"detector raw={_fmt(raw)} | view-band={cue['view_band_status']} | fused diagnostic={_fmt(score)}", fill=cue_colour(score), font=fn_small)
        fmt_px = lambda value: "unknown" if value is None else f"{value:.0f}px"
        draw.text((x0 + 12, y + 50), f"nearest person-box={fmt_px(pose['person_distance_px'])} | lower-body={fmt_px(pose['lower_body_distance_px'])} | ankle={fmt_px(pose['ankle_distance_px'])}", fill=COL["white"], font=fn_small)
        draw.text((x0 + 12, y + 72), f"bbox w={_fmt(geo['width'])} h={_fmt(geo['height'])} area={_fmt(geo['area'])} aspect={_fmt(geo['aspect'])}", fill=COL["white"], font=fn_small)
        cue_y = y + 98
        for offset, key in enumerate(("detector", "view_band", "pose", "geometry", "temporal")):
            value = cue["cue_scores"].get(key)
            draw.text((x0 + 12 + (offset % 3) * 202, cue_y + (offset // 3) * 24), f"{key}={_fmt(value)}", fill=cue_colour(value), font=fn_small)
        draw.text((x0 + 12, y + 148), "blue=all same-crop Stage 1 candidates | magenta=tracklet candidate | green=person | yellow=lower body", fill=COL["dim"], font=_font(11))

    draw.rectangle([0, page_h - footer_h, page_w - 1, page_h - 1], fill=(10, 10, 16))
    draw.text((12, page_h - footer_h + 8), "Fixed cue weights: detector .35 | vertical view-band .10 | pose .20 | bbox compactness .10 | temporal .25. Missing cue is omitted and weights renormalise.", fill=COL["dim"], font=_font(12))
    return page


# ── Core execution ───────────────────────────────────────────────────────────

def read_requested_frame(cap: cv2.VideoCapture, frame_index: int) -> Optional[np.ndarray]:
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(frame_index))
    ok, frame = cap.read()
    return frame if ok else None


def run_diagnostic(args: argparse.Namespace) -> None:
    candidates_data = load_json(args.stage1_candidates)
    tracklets_data = load_json(args.tracklets)
    frame_index = make_frame_index(candidates_data)
    by_id = {tracklet.get("id"): tracklet for tracklet in tracklets_data.get("tracklets", [])}
    wanted_ids = [item.strip() for item in args.tracklet_ids.split(",") if item.strip()]
    missing = [tracklet_id for tracklet_id in wanted_ids if tracklet_id not in by_id]
    if missing:
        raise RuntimeError(f"Requested tracklets absent: {', '.join(missing)}")

    pose_model = load_pose_model(args.pose_model)
    print(f"[multi-cue] pose model={args.pose_model} device={args.device} imgsz={args.pose_imgsz}")
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {args.video}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pages: List[Image.Image] = []
    csv_rows: List[Dict[str, Any]] = []

    for tracklet_id in wanted_ids:
        tracklet = by_id[tracklet_id]
        records: List[Dict[str, Any]] = []
        print(f"[multi-cue] {tracklet_id}")
        for label, obs in selected_observations(tracklet):
            frame_number = int(obs["frame"])
            source_candidates = frame_index.get(frame_number, [])
            source_candidate = match_source_candidate(source_candidates, obs)
            crop_yaw_raw = source_candidate.get("crop_yaw") if source_candidate else None
            crop_yaw = int(round(float(crop_yaw_raw))) if crop_yaw_raw is not None else nearest_crop_yaw(float(obs["yaw"]))
            equirect = read_requested_frame(cap, frame_number)
            if equirect is None:
                raise RuntimeError(f"Could not read frame {frame_number}")
            crop = extract_crop(equirect, crop_yaw)
            # Infer on unannotated pixels so visual markers cannot contaminate pose input.
            people = infer_people(pose_model, crop.copy(), args.pose_conf, args.pose_imgsz, args.device)
            target_point = draw_candidate_overlay(crop, source_candidates, source_candidate, obs, crop_yaw)
            draw_people(crop, people)
            pose = pose_metrics(target_point, people)
            cue = cue_bundle(tracklet, obs, source_candidate, pose)
            records.append({"label": label, "obs": obs, "overlay": crop, "target_point": target_point, "pose": pose, "cue": cue})
            geo = cue["geometry"]
            csv_rows.append({
                "tracklet_id": tracklet_id,
                "tracklet_status": tracklet.get("status"),
                "phase": label.lower(),
                "frame": frame_number,
                "yaw": obs.get("yaw"),
                "pitch": obs.get("pitch"),
                "detector_raw_conf": cue["detector_raw_conf"],
                "view_band_status": cue["view_band_status"],
                "nearest_person_bbox_px": pose["person_distance_px"],
                "nearest_lower_body_px": pose["lower_body_distance_px"],
                "nearest_ankle_px": pose["ankle_distance_px"],
                "bbox_width_px": geo["width"],
                "bbox_height_px": geo["height"],
                "bbox_area_px": geo["area"],
                "bbox_aspect_ratio": geo["aspect"],
                "temporal_score": cue["temporal_score"],
                "detector_score": cue["cue_scores"]["detector"],
                "view_band_score": cue["cue_scores"]["view_band"],
                "pose_score": cue["cue_scores"]["pose"],
                "geometry_score": cue["cue_scores"]["geometry"],
                "fused_diagnostic_score": cue["fused_score"],
            })
        pages.append(render_tracklet_page(tracklet, records))

    cap.release()
    if not pages:
        raise RuntimeError("No pages rendered")
    pdf_path = out_dir / "multi_cue_diagnostic_pack.pdf"
    pages[0].save(pdf_path, save_all=True, append_images=pages[1:], resolution=150.0)
    pages[0].save(out_dir / "multi_cue_diagnostic_page_1.png")
    csv_path = out_dir / "multi_cue_diagnostic.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(csv_rows[0].keys()))
        writer.writeheader()
        writer.writerows(csv_rows)

    summary = [
        "=== Multi-cue ball candidate diagnostic ===",
        "EXPERIMENT ONLY — no automatic acceptance/rejection and no tracker changes.",
        f"Tracklets: {', '.join(wanted_ids)}",
        f"Observations reviewed: {len(csv_rows)}",
        f"Pose model: {args.pose_model} (device={args.device})",
        f"Vertical playable-view band: {VIEW_BAND_MIN_DEG:.1f}° to {VIEW_BAND_MAX_DEG:.1f}°",
        "This is a diagnostic vertical band, not a full calibrated pitch polygon.",
        "Fused diagnostic score (available cues only; missing cues renormalise):",
        "  detector=0.35 | view_band=0.10 | pose=0.20 | geometry=0.10 | temporal=0.25",
        "Pose cue = interaction support only; absent pose is unknown/omitted, not a penalty.",
        "Geometry cue = square-like compactness only; no hard football-size prior.",
        "Temporal cue = existing tracklet evidence, explicitly not proof of a ball.",
        "",
        "Outputs:",
        f"  {pdf_path.name}",
        f"  {csv_path.name}",
    ]
    (out_dir / "multi_cue_diagnostic_summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print(f"[multi-cue] wrote {pdf_path}")
    print(f"[multi-cue] wrote {csv_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Diagnostic-only multi-cue ball evidence review pack")
    parser.add_argument("--stage1-candidates", help="Tier A experimental Stage 1 candidate JSON")
    parser.add_argument("--tracklets", help="Tier A experimental tracklets JSON")
    parser.add_argument("--video", help="Source equirectangular MP4")
    parser.add_argument("--output-dir", default="multi_cue_output")
    parser.add_argument("--tracklet-ids", default=",".join(SAMPLE_IDS))
    parser.add_argument("--pose-model", default="yolov8n-pose.pt")
    parser.add_argument("--pose-conf", type=float, default=0.25)
    parser.add_argument("--pose-imgsz", type=int, default=960)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--preflight", action="store_true", help="Load pose model and run one tiny inference, then exit")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        if args.preflight:
            preflight_pose(args.pose_model, args.device)
            return 0
        required = ("stage1_candidates", "tracklets", "video")
        absent = [name for name in required if not getattr(args, name)]
        if absent:
            parser.error("Missing required arguments: " + ", ".join("--" + item.replace("_", "-") for item in absent))
        run_diagnostic(args)
        return 0
    except Exception as exc:
        print(f"FATAL multi-cue diagnostic: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
