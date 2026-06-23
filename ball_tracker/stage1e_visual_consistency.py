#!/usr/bin/env python3
"""
FFA Offline Recovery Pipeline — Stage 1e: Visual Consistency Scorer
====================================================================

Purpose
-------
For each Stage 1d active ``new_detection`` candidate, extract two tight
perspective crops (centred and yaw-shifted) and re-run the existing YOLO ball
detector on each. Annotate each candidate with a ``stage1e`` block containing:

  - per-crop: total boxes returned, selected-box confidence and angular
    reprojection error (box with minimum angular error to original candidate);
  - ``verification_consistency``: score in {0, 0.5, 1} based on the centred
    crop only;
  - ``shifted_consistency``: {0, 1} robustness signal; does NOT affect the
    primary score;
  - all threshold / provenance fields for full auditability.

Score semantics (verbatim, as per specification):
  1   — centred tight-crop re-detection reprojects within tolerance of original
        candidate.
  0.5 — model returned at least one detection, but the closest reprojected
        detection was outside tolerance.
  0   — no centred tight-crop detection.
  0.5 is NOT partial ball confidence.

``stage0_reuse`` candidates receive no ``stage1e`` key.  Downstream code MUST
treat absence of the key as "not scored", not as score 0.

This is an ANNOTATE-ONLY pass.  No candidates are removed.  No quarantine
collections are modified.  No Stage 2 integration is performed.  No threshold
based on the Stage 1e score is applied.

IMPORTANT — same-model limitation
----------------------------------
The verifier uses the same ``football-ball-detection.pt`` model that produced
the original Stage 1 detection.  A false positive that is spatially stable
under re-presentation will score 1.  The score is labelled
``verification_consistency``, not ``ball_confidence``, to make this explicit.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Any

import cv2
import numpy as np

VERSION = "stage1e_v1"

# ── Geometry constants matching Stage 1 ──────────────────────────────────────
STAGE1_CROP_FOV_DEG  = 110.0
STAGE1_CROP_W        = 1280
STAGE1_CROP_H        = 720
STAGE1_CROP_YAWS     = [0, 90, 180, 270]

VERIFY_FOV_DEG       = 25.0
VERIFY_CROP_SIZE     = 640          # square
VERIFY_CONF          = 0.20
SHIFTED_YAW_OFFSET   = 10.0        # degrees

TOLERANCE_SCALE      = 1.5         # bbox_angular_diameter × this = tolerance
TOLERANCE_FLOOR_DEG  = 0.5         # minimum tolerance regardless of bbox size

SEED                 = 42          # for --max-candidates deterministic selection


# ─────────────────────────────────────────────────────────────────────────────
# Geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _world_ray(yaw_deg: float, pitch_deg: float) -> np.ndarray:
    y = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    return np.array([math.sin(y) * math.cos(p),
                     math.sin(p),
                     math.cos(y) * math.cos(p)])


def extract_perspective(eqr: np.ndarray,
                        look_yaw: float, look_pitch: float,
                        fov_deg: float, out_w: int, out_h: int) -> np.ndarray:
    """
    Perspective crop centred on (look_yaw, look_pitch).
    Identical to track_b_pack_gen.py / render_segment.py — world-up look-at.
    Used for the 25° centred and shifted crops.
    """
    h_eq, w_eq = eqr.shape[:2]
    L = _world_ray(look_yaw, look_pitch)
    world_up = np.array([0.0, 1.0, 0.0])

    R = np.cross(world_up, L)
    if np.linalg.norm(R) < 1e-6:
        R = np.array([1.0, 0.0, 0.0])
    else:
        R = R / np.linalg.norm(R)
    U = np.cross(L, R)
    U = U / np.linalg.norm(U)

    f   = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs  = np.linspace(0, out_w - 1, out_w)
    ys  = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)

    cx = (xv - out_w / 2.0) / f
    cy = -(yv - out_h / 2.0) / f

    wx = cx * R[0] + cy * U[0] + L[0]
    wy = cx * R[1] + cy * U[1] + L[1]
    wz = cx * R[2] + cy * U[2] + L[2]
    n  = np.sqrt(wx**2 + wy**2 + wz**2)
    wx, wy, wz = wx / n, wy / n, wz / n

    mx = ((np.arctan2(wx, wz) / (2 * math.pi)) + 0.5) * w_eq
    my = (0.5 - np.arcsin(np.clip(wy, -1, 1)) / math.pi) * h_eq
    return cv2.remap(eqr, mx.astype(np.float32), my.astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def inverse_project_perspective(px: float, py: float,
                                 look_yaw: float, look_pitch: float,
                                 fov_deg: float, out_w: int, out_h: int
                                 ) -> tuple[float, float]:
    """
    Back-project a pixel (px, py) in a perspective crop produced by
    extract_perspective(look_yaw, look_pitch, fov_deg, out_w, out_h) to
    world (yaw_deg, pitch_deg).

    Inverts the world-up look-at camera model exactly:
      1. pixel → normalised camera-space ray via focal length
      2. camera-space ray → world ray via [R | U | L] basis
      3. world ray → (yaw, pitch) via atan2 / asin
    """
    L = _world_ray(look_yaw, look_pitch)
    world_up = np.array([0.0, 1.0, 0.0])

    R_vec = np.cross(world_up, L)
    if np.linalg.norm(R_vec) < 1e-6:
        R_vec = np.array([1.0, 0.0, 0.0])
    else:
        R_vec = R_vec / np.linalg.norm(R_vec)
    U_vec = np.cross(L, R_vec)
    U_vec = U_vec / np.linalg.norm(U_vec)

    f   = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    cx  = (px - out_w / 2.0) / f
    cy  = -(py - out_h / 2.0) / f

    wx = cx * R_vec[0] + cy * U_vec[0] + L[0]
    wy = cx * R_vec[1] + cy * U_vec[1] + L[1]
    wz = cx * R_vec[2] + cy * U_vec[2] + L[2]
    n  = math.sqrt(wx**2 + wy**2 + wz**2)
    wx, wy, wz = wx / n, wy / n, wz / n

    yaw_out   = math.degrees(math.atan2(wx, wz))
    pitch_out = math.degrees(math.asin(max(-1.0, min(1.0, wy))))
    return yaw_out, pitch_out


def extract_stage1_crop(eqr: np.ndarray, crop_yaw_deg: float) -> np.ndarray:
    """
    Stage 1 yaw-only crop (FOV=110°, 1280×720).
    Replicates stage1_candidate_gen.extract_crop_frame exactly.
    Used for the LEFT audit panel.
    """
    h_eq, w_eq = eqr.shape[:2]
    f   = (STAGE1_CROP_W / 2.0) / math.tan(math.radians(STAGE1_CROP_FOV_DEG / 2.0))
    xs  = np.linspace(0, STAGE1_CROP_W - 1, STAGE1_CROP_W)
    ys  = np.linspace(0, STAGE1_CROP_H - 1, STAGE1_CROP_H)
    xv, yv = np.meshgrid(xs, ys)

    rx = (xv - STAGE1_CROP_W / 2.0) / f
    ry = -(yv - STAGE1_CROP_H / 2.0) / f
    rz = np.ones_like(rx)
    nrm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / nrm, ry / nrm, rz / nrm

    cy   = math.radians(crop_yaw_deg)
    wx   =  math.cos(cy) * rx + math.sin(cy) * rz
    wy   = ry
    wz   = -math.sin(cy) * rx + math.cos(cy) * rz

    yaw_map   = np.arctan2(wx, wz)
    pitch_map = np.arcsin(np.clip(wy, -1, 1))
    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    return cv2.remap(eqr, map_x.astype(np.float32), map_y.astype(np.float32),
                     cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def stage1_crop_pixel_to_world(px: float, py: float,
                                crop_yaw_deg: float) -> tuple[float, float]:
    """
    Inverse of extract_stage1_crop: pixel → world (yaw, pitch).
    Replicates stage1_candidate_gen.crop_pixel_to_yaw_pitch.
    Used only to project the candidate position into the left audit panel.
    """
    f   = (STAGE1_CROP_W / 2.0) / math.tan(math.radians(STAGE1_CROP_FOV_DEG / 2.0))
    cx  = (px - STAGE1_CROP_W / 2.0) / f
    cy_ = -(py - STAGE1_CROP_H / 2.0) / f
    ray = np.array([cx, cy_, 1.0])
    ray = ray / np.linalg.norm(ray)

    c = math.radians(crop_yaw_deg)
    Ry = np.array([[ math.cos(c), 0, math.sin(c)],
                   [           0, 1,            0],
                   [-math.sin(c), 0, math.cos(c)]])
    world = Ry @ ray
    yaw_out   = math.degrees(math.atan2(world[0], world[2]))
    pitch_out = math.degrees(math.asin(max(-1.0, min(1.0, world[1]))))
    return yaw_out, pitch_out


def world_to_stage1_crop_pixel(yaw_deg: float, pitch_deg: float,
                                crop_yaw_deg: float) -> tuple[float, float] | None:
    """
    Project world (yaw, pitch) to pixel (px, py) in the Stage 1 yaw-only crop.
    Returns None if the point is behind the camera (z < 0 after rotation).
    """
    ray = _world_ray(yaw_deg, pitch_deg)
    c   = math.radians(crop_yaw_deg)
    # Inverse of Ry: transpose = Ry^-1
    Ry_inv = np.array([[math.cos(c), 0, -math.sin(c)],
                       [          0, 1,             0],
                       [math.sin(c), 0,  math.cos(c)]])
    cam = Ry_inv @ ray
    if cam[2] <= 0:
        return None
    f  = (STAGE1_CROP_W / 2.0) / math.tan(math.radians(STAGE1_CROP_FOV_DEG / 2.0))
    px = cam[0] / cam[2] * f + STAGE1_CROP_W / 2.0
    py = -cam[1] / cam[2] * f + STAGE1_CROP_H / 2.0
    return px, py


def world_to_perspective_pixel(yaw_deg: float, pitch_deg: float,
                                look_yaw: float, look_pitch: float,
                                fov_deg: float, out_w: int, out_h: int
                                ) -> tuple[float, float] | None:
    """
    Project world (yaw, pitch) to pixel in the world-up perspective crop.
    Returns None if behind the camera.
    """
    L = _world_ray(look_yaw, look_pitch)
    world_up = np.array([0.0, 1.0, 0.0])
    R_vec = np.cross(world_up, L)
    if np.linalg.norm(R_vec) < 1e-6:
        R_vec = np.array([1.0, 0.0, 0.0])
    else:
        R_vec = R_vec / np.linalg.norm(R_vec)
    U_vec = np.cross(L, R_vec)
    U_vec = U_vec / np.linalg.norm(U_vec)

    ray = _world_ray(yaw_deg, pitch_deg)
    # Project onto camera basis
    # cam_x = dot(ray, R), cam_y = dot(ray, U), cam_z = dot(ray, L)
    cam_x = float(np.dot(ray, R_vec))
    cam_y = float(np.dot(ray, U_vec))
    cam_z = float(np.dot(ray, L))
    if cam_z <= 0:
        return None
    f  = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    px = cam_x / cam_z * f + out_w / 2.0
    py = -cam_y / cam_z * f + out_h / 2.0
    return px, py


def great_circle_deg(y1: float, p1: float, y2: float, p2: float) -> float:
    """Great-circle angular distance in degrees."""
    dot = (math.sin(math.radians(p1)) * math.sin(math.radians(p2)) +
           math.cos(math.radians(p1)) * math.cos(math.radians(p2)) *
           math.cos(math.radians(y1 - y2)))
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def wrap_yaw(yaw_deg: float) -> float:
    """Wrap yaw into [-180, +180)."""
    return ((yaw_deg + 180.0) % 360.0) - 180.0


def compute_tolerance(bbox_width_px: float) -> float:
    """
    Tolerance in degrees, scaled to original detection size.
    bbox_angular_diameter = bbox_width_px * (STAGE1_FOV / STAGE1_CROP_W)
    tolerance = max(FLOOR, angular_diameter * SCALE)
    """
    angular_diameter = bbox_width_px * (STAGE1_CROP_FOV_DEG / STAGE1_CROP_W)
    return max(TOLERANCE_FLOOR_DEG, angular_diameter * TOLERANCE_SCALE)


# ─────────────────────────────────────────────────────────────────────────────
# Per-crop inference
# ─────────────────────────────────────────────────────────────────────────────

def _run_crop_inference(model, crop_bgr: np.ndarray,
                        look_yaw: float, look_pitch: float,
                        cand_yaw: float, cand_pitch: float,
                        fov_deg: float, size: int) -> dict[str, Any]:
    """
    Run the YOLO model on a single crop.
    For every returned box, back-project its centre to world and compute
    angular error to (cand_yaw, cand_pitch).
    Select the box with minimum angular error.
    """
    results = model.predict(crop_bgr, conf=VERIFY_CONF, imgsz=size, verbose=False)
    boxes = results[0].boxes if results and results[0].boxes is not None else None

    all_boxes: list[dict] = []
    if boxes is not None:
        for box in boxes:
            x1, y1, x2, y2 = [float(v) for v in box.xyxy[0]]
            cx_px = (x1 + x2) / 2.0
            cy_px = (y1 + y2) / 2.0
            det_yaw, det_pitch = inverse_project_perspective(
                cx_px, cy_px, look_yaw, look_pitch, fov_deg, size, size
            )
            err = great_circle_deg(det_yaw, det_pitch, cand_yaw, cand_pitch)
            all_boxes.append({
                "conf": round(float(box.conf[0]), 4),
                "angular_error_deg": round(err, 4),
            })

    total = len(all_boxes)
    if total == 0:
        return {"total_boxes_returned": 0,
                "selected_conf": None,
                "selected_error_deg": None,
                "fired": False}

    best = min(all_boxes, key=lambda b: b["angular_error_deg"])
    return {"total_boxes_returned": total,
            "selected_conf": best["conf"],
            "selected_error_deg": best["angular_error_deg"],
            "fired": True}


# ─────────────────────────────────────────────────────────────────────────────
# Main scoring pass
# ─────────────────────────────────────────────────────────────────────────────

def score_candidates(stage1d: dict[str, Any],
                     equirect_path: str,
                     model,
                     max_candidates: int | None = None,
                     input_file_id: str = "") -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Annotate Stage 1d active new_detection candidates with stage1e blocks.
    Returns (annotated_stage1d, report).
    """
    import random
    frames_raw = stage1d.get("frames", {})

    # Collect all eligible candidates as (frame_key, candidate_index)
    eligible: list[tuple[str, int]] = []
    for fk, cands in frames_raw.items():
        for i, c in enumerate(cands):
            if c.get("source") == "new_detection":
                eligible.append((fk, i))

    total_eligible = len(eligible)

    if max_candidates is not None and max_candidates < total_eligible:
        random.seed(SEED)
        eligible = random.sample(eligible, max_candidates)
        eligible.sort(key=lambda x: (int(x[0]), x[1]))  # restore frame order

    print(f"[stage1e] Eligible new_detection: {total_eligible}  "
          f"scoring: {len(eligible)}  "
          f"{'(SMOKE RUN)' if max_candidates else '(FULL RUN)'}")

    # Build output as deep copy
    output = copy.deepcopy(stage1d)
    output_frames = output["frames"]

    cap = cv2.VideoCapture(equirect_path)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {equirect_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 29.97
    prev_frame_idx = -1
    eqr_bgr = None

    scored = 0
    score_counts = {0: 0, 0.5: 0, 1: 0}
    centred_fired = 0
    shifted_fired = 0

    created_utc = datetime.now(timezone.utc).isoformat()

    for fk, ci in eligible:
        frame_idx = int(fk)
        cand = output_frames[fk][ci]

        # Lazy frame seek
        if frame_idx != prev_frame_idx:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, frame = cap.read()
            if not ok:
                print(f"[stage1e] WARN: could not read frame {frame_idx}", file=sys.stderr)
                continue
            eqr_bgr = frame
            prev_frame_idx = frame_idx

        cand_yaw   = float(cand["yaw"])
        cand_pitch = float(cand["pitch"])
        bbox_w     = float(cand["detection_geometry"]["bbox_width_px"])
        tolerance  = compute_tolerance(bbox_w)

        # ── Centred crop ──────────────────────────────────────────────────
        centred_crop = extract_perspective(
            eqr_bgr, cand_yaw, cand_pitch,
            VERIFY_FOV_DEG, VERIFY_CROP_SIZE, VERIFY_CROP_SIZE
        )
        centred_result = _run_crop_inference(
            model, centred_crop,
            cand_yaw, cand_pitch, cand_yaw, cand_pitch,
            VERIFY_FOV_DEG, VERIFY_CROP_SIZE
        )

        # ── Shifted crop ──────────────────────────────────────────────────
        raw_shifted_yaw = cand_yaw + SHIFTED_YAW_OFFSET
        shifted_yaw     = wrap_yaw(raw_shifted_yaw)
        yaw_wrapped     = (shifted_yaw != raw_shifted_yaw)

        shifted_crop = extract_perspective(
            eqr_bgr, shifted_yaw, cand_pitch,
            VERIFY_FOV_DEG, VERIFY_CROP_SIZE, VERIFY_CROP_SIZE
        )
        shifted_result = _run_crop_inference(
            model, shifted_crop,
            shifted_yaw, cand_pitch, cand_yaw, cand_pitch,
            VERIFY_FOV_DEG, VERIFY_CROP_SIZE
        )

        # ── Scores ────────────────────────────────────────────────────────
        err_c = centred_result["selected_error_deg"]
        if not centred_result["fired"]:
            vc_score = 0
        elif err_c <= tolerance:
            vc_score = 1
        else:
            vc_score = 0.5

        err_s = shifted_result["selected_error_deg"]
        sc_score = 1 if (shifted_result["fired"] and err_s is not None
                         and err_s <= tolerance) else 0

        # ── Annotate ──────────────────────────────────────────────────────
        cand["stage1e"] = {
            "centred": {
                "total_boxes_returned": centred_result["total_boxes_returned"],
                "selected_conf":        centred_result["selected_conf"],
                "selected_error_deg":   centred_result["selected_error_deg"],
                "fired":                centred_result["fired"],
            },
            "shifted": {
                "shifted_yaw_used_deg":  round(shifted_yaw, 4),
                "yaw_wrapped":           yaw_wrapped,
                "total_boxes_returned":  shifted_result["total_boxes_returned"],
                "selected_conf":         shifted_result["selected_conf"],
                "selected_error_deg":    shifted_result["selected_error_deg"],
                "fired":                 shifted_result["fired"],
            },
            "tolerance_deg":             round(tolerance, 4),
            "verification_consistency":  vc_score,
            "shifted_consistency":       sc_score,
            "model":                     "football-ball-detection.pt",
            "crop_fov_deg":              VERIFY_FOV_DEG,
            "crop_size_px":              VERIFY_CROP_SIZE,
            "conf_threshold":            VERIFY_CONF,
            "rule_version":              VERSION,
        }

        score_counts[vc_score] += 1
        if centred_result["fired"]:
            centred_fired += 1
        if shifted_result["fired"]:
            shifted_fired += 1
        scored += 1

        if scored % 100 == 0:
            print(f"[stage1e]   scored {scored}/{len(eligible)} ...")

    cap.release()

    output["stage1e_meta"] = {
        "version":           VERSION,
        "created_utc":       created_utc,
        "input_file_id":     input_file_id,
        "total_eligible":    total_eligible,
        "scored":            scored,
        "smoke_run":         max_candidates is not None,
        "max_candidates":    max_candidates,
        "rules": {
            "verify_fov_deg":      VERIFY_FOV_DEG,
            "verify_crop_size_px": VERIFY_CROP_SIZE,
            "conf_threshold":      VERIFY_CONF,
            "shifted_yaw_offset_deg": SHIFTED_YAW_OFFSET,
            "tolerance_scale":     TOLERANCE_SCALE,
            "tolerance_floor_deg": TOLERANCE_FLOOR_DEG,
            "applies_to":          "new_detection",
            "stage0_reuse_action": "skip_no_annotation",
        },
        "score_semantics": {
            "1":   "centred tight-crop re-detection reprojects within tolerance of original candidate",
            "0.5": "model returned at least one detection but closest reprojection was outside tolerance",
            "0":   "no centred tight-crop detection",
            "note": "0.5 is NOT partial ball confidence. Same model — spatial stability only.",
        },
        "counts": {
            "score_1":        score_counts[1],
            "score_0_5":      score_counts[0.5],
            "score_0":        score_counts[0],
            "centred_fired":  centred_fired,
            "shifted_fired":  shifted_fired,
        },
    }

    total_scored = scored
    report: dict[str, Any] = {
        "version":        VERSION,
        "created_utc":    created_utc,
        "input_file_id":  input_file_id,
        "smoke_run":      max_candidates is not None,
        "total_eligible": total_eligible,
        "scored":         total_scored,
        "rules":          output["stage1e_meta"]["rules"],
        "score_semantics": output["stage1e_meta"]["score_semantics"],
        "score_distribution": {
            "score_1":    score_counts[1],
            "score_0_5":  score_counts[0.5],
            "score_0":    score_counts[0],
            "pct_1":      round(100 * score_counts[1]   / max(scored, 1), 1),
            "pct_0_5":    round(100 * score_counts[0.5] / max(scored, 1), 1),
            "pct_0":      round(100 * score_counts[0]   / max(scored, 1), 1),
        },
        "centred_fired_rate": round(centred_fired / max(scored, 1), 4),
        "shifted_fired_rate": round(shifted_fired / max(scored, 1), 4),
    }

    return output, report


def text_report(report: dict[str, Any]) -> str:
    sd = report["score_distribution"]
    lines = [
        "=" * 70,
        "STAGE 1E — VISUAL CONSISTENCY SCORER — REPORT",
        "=" * 70,
        "",
        "IMPORTANT: verification_consistency is NOT ball confidence.",
        "The same detector model is reused; a spatially stable false positive",
        "will score 1.  Inspect the 90-tile audit pack before drawing conclusions.",
        "",
        f"Smoke run : {'YES — partial set' if report['smoke_run'] else 'NO — full eligible set'}",
        f"Scored    : {report['scored']} / {report['total_eligible']} eligible new_detection candidates",
        f"stage0_reuse candidates: skipped (no annotation added)",
        "",
        "SCORE DISTRIBUTION",
        "-" * 70,
        f"  score=1   (within tolerance) : {sd['score_1']:5d}  ({sd['pct_1']:.1f}%)",
        f"  score=0.5 (fired, outside)   : {sd['score_0_5']:5d}  ({sd['pct_0_5']:.1f}%)",
        f"  score=0   (no detection)     : {sd['score_0']:5d}  ({sd['pct_0']:.1f}%)",
        "",
        f"Centred-crop fired rate : {report['centred_fired_rate']:.3f}",
        f"Shifted-crop fired rate : {report['shifted_fired_rate']:.3f}",
        "",
        "SCORE SEMANTICS",
        "-" * 70,
        "  1   — centred tight-crop re-detection reprojects within tolerance",
        "  0.5 — at least one detection but closest was outside tolerance",
        "  0   — no centred tight-crop detection",
        "  0.5 is NOT partial ball confidence.",
        "=" * 70,
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Audit pack generator
# ─────────────────────────────────────────────────────────────────────────────

AUDIT_TILES      = 90      # 30 per score bucket
AUDIT_PER_BUCKET = 30
AUDIT_PANEL_W    = 426     # per panel; 3 panels → 1278px wide tile
AUDIT_PANEL_H    = 240     # height of each panel image
AUDIT_LBL_H      = 36      # label strip height
AUDIT_TILE_H     = AUDIT_PANEL_H + AUDIT_LBL_H
AUDIT_COLS       = 3       # tiles per row in the sheet
BG               = (20, 20, 20)
WHITE            = (255, 255, 255)
RED              = (50, 50, 255)     # BGR
GREEN            = (50, 200, 50)
YELLOW           = (50, 200, 200)
RETICLE_R        = 10


def _draw_crosshair(img: np.ndarray, x: int, y: int, colour: tuple, r: int = RETICLE_R):
    g = 3
    cv2.line(img, (x - r, y), (x - g, y), colour, 1)
    cv2.line(img, (x + g, y), (x + r, y), colour, 1)
    cv2.line(img, (x, y - r), (x, y - g), colour, 1)
    cv2.line(img, (x, y + g), (x, y + r), colour, 1)


def _draw_box_centre(img: np.ndarray, cx: float, cy: float, colour: tuple):
    x, y = int(round(cx)), int(round(cy))
    cv2.circle(img, (x, y), 4, colour, -1)
    cv2.circle(img, (x, y), 6, colour, 1)


def _resize(img: np.ndarray, w: int, h: int) -> np.ndarray:
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)


def _panel_bgr_to_pil(bgr: np.ndarray):
    from PIL import Image
    return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))


def build_audit_pack(annotated: dict[str, Any],
                     equirect_path: str,
                     output_path: str) -> None:
    from PIL import Image, ImageDraw, ImageFont
    import random

    frames_raw = annotated.get("frames", {})

    # Bucket candidates by score
    buckets: dict[Any, list[tuple[str, int]]] = {1: [], 0.5: [], 0: []}
    for fk, cands in frames_raw.items():
        for ci, c in enumerate(cands):
            s1e = c.get("stage1e")
            if s1e is None:
                continue
            score = s1e["verification_consistency"]
            if score in buckets:
                buckets[score].append((fk, ci))

    random.seed(SEED + 10)
    selected: list[tuple[str, int, Any]] = []
    for score in [1, 0.5, 0]:
        pool = buckets[score]
        n    = min(AUDIT_PER_BUCKET, len(pool))
        samp = random.sample(pool, n)
        samp.sort(key=lambda x: int(x[0]))
        for fk, ci in samp:
            selected.append((fk, ci, score))

    cap = cv2.VideoCapture(equirect_path)
    prev_fi = -1
    eqr_bgr = None

    TILE_W = AUDIT_PANEL_W * 3
    n_cols = AUDIT_COLS
    n_rows = math.ceil(len(selected) / n_cols)
    sheet_w = TILE_W * n_cols
    sheet_h = AUDIT_TILE_H * n_rows

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 10)
    except Exception:
        font = ImageFont.load_default()

    sheet = Image.new("RGB", (sheet_w, sheet_h), (20, 20, 20))
    draw  = ImageDraw.Draw(sheet)

    for tile_i, (fk, ci, score) in enumerate(selected):
        frame_idx = int(fk)
        cand      = frames_raw[fk][ci]
        s1e       = cand["stage1e"]

        if frame_idx != prev_fi:
            cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
            ok, fr = cap.read()
            eqr_bgr = fr if ok else np.zeros((2048, 4096, 3), dtype=np.uint8)
            prev_fi = frame_idx

        cand_yaw   = float(cand["yaw"])
        cand_pitch = float(cand["pitch"])
        crop_yaw   = float(cand["crop_yaw"])
        tol        = s1e["tolerance_deg"]

        # ── Left panel: original Stage 1 110° crop ───────────────────────
        left_bgr = extract_stage1_crop(eqr_bgr, crop_yaw)
        proj = world_to_stage1_crop_pixel(cand_yaw, cand_pitch, crop_yaw)
        if proj is not None:
            _draw_crosshair(left_bgr, int(proj[0]), int(proj[1]), RED)
        left_small = _resize(left_bgr, AUDIT_PANEL_W, AUDIT_PANEL_H)

        # ── Middle panel: centred 25° crop ───────────────────────────────
        mid_bgr = extract_perspective(
            eqr_bgr, cand_yaw, cand_pitch,
            VERIFY_FOV_DEG, VERIFY_CROP_SIZE, VERIFY_CROP_SIZE
        )
        c_info = s1e["centred"]
        if c_info["fired"] and c_info["selected_error_deg"] is not None:
            # Find the best-match box centre in crop pixels by re-running inverse:
            # We stored error; approximate pixel by projecting world back
            # (exact only if selected_error_deg came from the minimum-error box)
            proj_mid = world_to_perspective_pixel(
                cand_yaw, cand_pitch,
                cand_yaw, cand_pitch,
                VERIFY_FOV_DEG, VERIFY_CROP_SIZE, VERIFY_CROP_SIZE
            )
            colour_c = GREEN if s1e["verification_consistency"] == 1 else YELLOW
            if proj_mid:
                _draw_box_centre(mid_bgr, proj_mid[0], proj_mid[1], colour_c)
        mid_small = _resize(mid_bgr, AUDIT_PANEL_W, AUDIT_PANEL_H)

        # ── Right panel: shifted 25° crop ────────────────────────────────
        sh_yaw = s1e["shifted"]["shifted_yaw_used_deg"]
        right_bgr = extract_perspective(
            eqr_bgr, sh_yaw, cand_pitch,
            VERIFY_FOV_DEG, VERIFY_CROP_SIZE, VERIFY_CROP_SIZE
        )
        sh_info = s1e["shifted"]
        if sh_info["fired"] and sh_info["selected_error_deg"] is not None:
            colour_s = GREEN if s1e["shifted_consistency"] == 1 else YELLOW
            proj_sh = world_to_perspective_pixel(
                cand_yaw, cand_pitch,
                sh_yaw, cand_pitch,
                VERIFY_FOV_DEG, VERIFY_CROP_SIZE, VERIFY_CROP_SIZE
            )
            if proj_sh:
                _draw_box_centre(right_bgr, proj_sh[0], proj_sh[1], colour_s)
        right_small = _resize(right_bgr, AUDIT_PANEL_W, AUDIT_PANEL_H)

        # ── Composite tile ───────────────────────────────────────────────
        tile_bgr = np.full((AUDIT_TILE_H, TILE_W, 3), BG, dtype=np.uint8)
        tile_bgr[:AUDIT_PANEL_H, :AUDIT_PANEL_W]                   = left_small
        tile_bgr[:AUDIT_PANEL_H, AUDIT_PANEL_W:2*AUDIT_PANEL_W]    = mid_small
        tile_bgr[:AUDIT_PANEL_H, 2*AUDIT_PANEL_W:]                  = right_small

        tile_pil = _panel_bgr_to_pil(tile_bgr)
        td        = ImageDraw.Draw(tile_pil)

        c_err_str = f"{c_info['selected_error_deg']:.2f}°" if c_info["selected_error_deg"] is not None else "—"
        c_conf_str = f"{c_info['selected_conf']:.2f}" if c_info["selected_conf"] is not None else "—"
        label = (f"f={frame_idx}  vc={score}  c_conf={c_conf_str}  "
                 f"c_err={c_err_str}  tol={tol:.2f}°  sh={s1e['shifted_consistency']}")
        td.text((4, AUDIT_PANEL_H + 4), label, font=font, fill=(220, 220, 220))

        col = tile_i % n_cols
        row = tile_i // n_cols
        x0  = col * TILE_W
        y0  = row * AUDIT_TILE_H
        sheet.paste(tile_pil, (x0, y0))

    cap.release()
    sheet.save(output_path)
    print(f"[stage1e] Audit pack -> {output_path}  ({len(selected)} tiles)")


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Stage 1e visual consistency scorer")
    ap.add_argument("--stage1d-candidates", required=True,
                    help="Path to stage1_candidates_geo_filtered.json (Stage 1d output)")
    ap.add_argument("--equirect", required=True,
                    help="Path to equirect_trim.mp4")
    ap.add_argument("--weights", required=True,
                    help="Path to football-ball-detection.pt")
    ap.add_argument("--output-dir", default="stage1e_output")
    ap.add_argument("--input-file-id", default="")
    ap.add_argument("--max-candidates", type=int, default=None,
                    help="Limit inference to N candidates (deterministic; default: all eligible)")
    ap.add_argument("--audit-only", action="store_true",
                    help="Skip inference; regenerate audit pack from existing annotated JSON")
    ap.add_argument("--annotated-json", default=None,
                    help="Path to already-annotated JSON (for --audit-only)")
    args = ap.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    if args.audit_only:
        if not args.annotated_json:
            ap.error("--audit-only requires --annotated-json")
        with open(args.annotated_json) as f:
            annotated = json.load(f)
        audit_path = os.path.join(args.output_dir, "stage1e_audit_pack.png")
        build_audit_pack(annotated, args.equirect, audit_path)
        return

    with open(args.stage1d_candidates) as f:
        stage1d = json.load(f)

    from ultralytics import YOLO
    print(f"[stage1e] Loading model: {args.weights}")
    model = YOLO(args.weights)
    model.to("cpu")

    annotated, report = score_candidates(
        stage1d, args.equirect, model,
        max_candidates=args.max_candidates,
        input_file_id=args.input_file_id,
    )

    out_json     = os.path.join(args.output_dir, "stage1_candidates_stage1e.json")
    out_rep_json = os.path.join(args.output_dir, "stage1e_report.json")
    out_rep_txt  = os.path.join(args.output_dir, "stage1e_report.txt")
    out_audit    = os.path.join(args.output_dir, "stage1e_audit_pack.png")

    with open(out_json, "w") as f:
        json.dump(annotated, f, indent=2)
    with open(out_rep_json, "w") as f:
        json.dump(report, f, indent=2)
    with open(out_rep_txt, "w") as f:
        f.write(text_report(report) + "\n")

    print(text_report(report))

    build_audit_pack(annotated, args.equirect, out_audit)

    print(f"\n[stage1e] Annotated JSON  -> {out_json}")
    print(f"[stage1e] Report JSON     -> {out_rep_json}")
    print(f"[stage1e] Report text     -> {out_rep_txt}")
    print(f"[stage1e] Audit pack      -> {out_audit}")


if __name__ == "__main__":
    main()
