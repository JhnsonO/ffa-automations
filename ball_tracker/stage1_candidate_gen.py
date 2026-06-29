#!/usr/bin/env python3
"""
FFA Offline Recovery Pipeline — Stage 1: Candidate Generation
=============================================================
Per docs/offline-recovery-pipeline.md §4.

Purpose
-------
Produce a weighted candidate list for every frame in the clip, ready for
Stage 2 temporal linking.  This stage is cheap and conservative:

  1. Reuse Stage 0 sampled detections from stage0_detections.json — no
     re-running the detector on already-sampled frames.
  2. For frames NOT covered by Stage 0 (between sampled timestamps), run the
     same cheap YOLO detector at the same conf floor.
  3. Apply the hotspot penalty map (region-aware) to every candidate
     immediately on ingestion.
  4. Apply pitch bounds as a hard pre-filter — anything outside
     [PITCH_MIN_DEG, PITCH_MAX_DEG] is dropped before any further processing.
  5. Emit a per-frame candidate list and a reduction report.

No rendering changes.  No temporal tracking.  Output only.

Pitch bounds (configurable, not derived from Stage 0 data)
----------------------------------------------------------
  PITCH_MIN_DEG = -30   (conservative lower bound)
  PITCH_MAX_DEG = +18   (conservative upper bound)
These are workflow/config inputs and must not be globally relaxed just because
high-pitch detections exist in Stage 0.  Stage 3 gap recovery may search wider
locally when forward/backward anchors justify it.

Inputs
------
  --hotspot-map      : stage0_output/hotspot_map.json
  --stage0-detections: stage0_output/stage0_detections.json
  --input            : equirect MP4 (same clip used for Stage 0)
  --output-dir       : output directory

Outputs
-------
  stage1_candidates.json  — per-frame candidate list with penalties applied
  stage1_report.txt       — reduction stats and fence region check
"""

import argparse
import json
import math
import os
import sys
import time

import cv2
import numpy as np

# ── Optional torch — imported lazily so dry-run doesn't require GPU env ──────
def _torch_env_banner():
    """Print Python/PyTorch/CUDA environment and return (device_str, gpu_name)."""
    import platform
    print(f"[stage1] Python  : {platform.python_version()}")
    try:
        import torch
        print(f"[stage1] PyTorch : {torch.__version__}")
        cuda_ver = torch.version.cuda if torch.version.cuda else "n/a"
        print(f"[stage1] CUDA rt : {cuda_ver}")
        avail = torch.cuda.is_available()
        print(f"[stage1] CUDA ok : {avail}")
        if avail:
            dev = torch.device("cuda")
            gpu = torch.cuda.get_device_name(0)
        else:
            dev = torch.device("cpu")
            gpu = None
        print(f"[stage1] Device  : {dev}  gpu={gpu or 'none'}")
        return str(dev), gpu, torch.__version__, cuda_ver
    except ImportError:
        print("[stage1] PyTorch NOT importable")
        return "cpu", None, "n/a", "n/a"


def _preflight_check(model, device_str):
    """
    Run one tiny detector inference and confirm it executes on the expected device.
    Exits with code 2 if CUDA was expected but inference ran on CPU or timed out.
    """
    import time as _time
    print("[stage1] Preflight: running single-inference check ...")
    dummy = np.zeros((64, 64, 3), dtype=np.uint8)
    t_pre = _time.time()
    try:
        results = model.predict(dummy, conf=0.5, imgsz=64, verbose=False)
        elapsed = _time.time() - t_pre
        # Verify the model's parameters are on the expected device
        try:
            import torch
            actual_device = next(model.model.parameters()).device
            print(f"[stage1] Preflight OK: {elapsed*1000:.0f} ms  model_device={actual_device}")
            if device_str == "cuda" and "cuda" not in str(actual_device):
                print(f"[stage1] PREFLIGHT FAIL: expected CUDA but model is on {actual_device}", file=sys.stderr)
                sys.exit(2)
        except Exception as e:
            print(f"[stage1] Preflight device check skipped ({e}); elapsed={elapsed*1000:.0f} ms")
    except Exception as exc:
        print(f"[stage1] PREFLIGHT FAIL: inference raised {exc}", file=sys.stderr)
        sys.exit(2)

# ── Detector config — identical to Stage 0 / run_tracker.py cheap path ────────
CROP_YAWS_DEG    = [0, 90, 180, 270]
CROP_FOV_DEG     = 110
CROP_W           = 1280
CROP_H           = 720
DEDUP_THRESH_DEG = 15
YOLO_CONF        = 0.12
YOLO_IMGSZ       = 1280
BALL_CLASS_ID    = 0

# ── Pitch bounds — configurable, conservative defaults ────────────────────────
DEF_PITCH_MIN_DEG = -30.0
DEF_PITCH_MAX_DEG =  18.0

# ── Known venue reference (for report) ───────────────────────────────────────
KNOWN_FENCE_YAW   = -77.4
KNOWN_FENCE_PITCH = -3.9
DEFAULT_VENUE_MASK = os.path.join(os.path.dirname(__file__), "venue_mask.json")


def _load_venue_mask(mask_path, frame_width, frame_height):
    """Load an optional calibration polygon; missing file means full-frame mode."""
    if not mask_path or not os.path.isfile(mask_path):
        return None
    with open(mask_path) as f:
        data = json.load(f)
    if data.get("frame_width") != frame_width or data.get("frame_height") != frame_height:
        raise ValueError(
            "Venue mask dimension mismatch: "
            f"mask={data.get('frame_width')}x{data.get('frame_height')}, "
            f"frame={frame_width}x{frame_height}"
        )
    polygon = data.get("polygon")
    if not isinstance(polygon, list) or len(polygon) < 4:
        raise ValueError("Venue mask polygon must contain at least 4 points")
    return np.asarray(polygon, dtype=np.int32).reshape((-1, 1, 2))


def _venue_contains(yaw, pitch, polygon, frame_width, frame_height):
    """Test spherical candidate coordinates against the equirectangular mask."""
    if polygon is None:
        return True
    x = ((yaw / 360.0) + 0.5) * frame_width
    y = (0.5 - pitch / 180.0) * frame_height
    return cv2.pointPolygonTest(polygon, (float(x), float(y)), False) >= 0


# ─────────────────────────────────────────────────────────────────────────────
# Geometry — verbatim from run_tracker.py / stage0
# ─────────────────────────────────────────────────────────────────────────────
def extract_crop_frame(equirect_frame, crop_yaw_deg, fov_deg, out_w, out_h):
    h_eq, w_eq = equirect_frame.shape[:2]
    f = (out_w / 2.0) / math.tan(math.radians(fov_deg / 2.0))
    xs = np.linspace(0, out_w - 1, out_w)
    ys = np.linspace(0, out_h - 1, out_h)
    xv, yv = np.meshgrid(xs, ys)
    rx = (xv - out_w / 2.0) / f
    ry = -(yv - out_h / 2.0) / f
    rz = np.ones_like(rx)
    norm = np.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    cy = math.radians(crop_yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    yaw_map   = np.arctan2(wx, wz)
    pitch_map = np.arcsin(np.clip(wy, -1, 1))
    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    return cv2.remap(equirect_frame,
                     map_x.astype(np.float32), map_y.astype(np.float32),
                     interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def crop_pixel_to_yaw_pitch(px, py, crop_yaw_deg, fov_deg, w, h):
    nx = (px - w / 2.0) / (w / 2.0)
    ny = (py - h / 2.0) / (h / 2.0)
    f  = 1.0 / math.tan(math.radians(fov_deg / 2.0))
    ray = np.array([nx / f, -ny / f * (w / h), 1.0])
    ray = ray / np.linalg.norm(ray)
    cy = math.radians(crop_yaw_deg)
    Ry = np.array([[ math.cos(cy), 0, math.sin(cy)],
                   [            0, 1,            0],
                   [-math.sin(cy), 0, math.cos(cy)]])
    world = Ry @ ray
    yaw   = math.degrees(math.atan2(world[0], world[2]))
    pitch = math.degrees(math.asin(max(-1.0, min(1.0, world[1]))))
    return yaw, pitch


def angular_distance(y1, p1, y2, p2):
    dy = math.radians(y1 - y2)
    return math.degrees(math.acos(max(-1.0, min(1.0,
        math.sin(math.radians(p1)) * math.sin(math.radians(p2)) +
        math.cos(math.radians(p1)) * math.cos(math.radians(p2)) * math.cos(dy)
    ))))


def dedupe_detections(detections, thresh_deg=DEDUP_THRESH_DEG):
    kept = []
    for det in sorted(detections, key=lambda d: -d[2]):
        yaw, pitch, conf = det[:3]
        if all(angular_distance(yaw, pitch, k[0], k[1]) > thresh_deg for k in kept):
            kept.append(det)
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Penalty — loaded from hotspot_map.json, applied per-candidate
# ─────────────────────────────────────────────────────────────────────────────
def load_hotspot_map(path):
    with open(path) as f:
        hm = json.load(f)
    # Reconstruct penalty lookup from the bin list
    bin_lookup = {}
    for b in hm.get("bins", []):
        key = (b["yaw_bin"], b["pitch_bin"])
        bin_lookup[key] = b["penalty_weight"]
    return hm, bin_lookup


def bin_id_for(yaw, pitch, bin_deg):
    yaw_w = ((yaw + 180.0) % 360.0) - 180.0
    yb = int(math.floor((yaw_w + 180.0) / bin_deg))
    pb = int(math.floor((pitch + 90.0) / bin_deg))
    return (yb, pb)


def penalty_weight_from_map(yaw, pitch, hm, bin_lookup):
    """
    Region-aware penalty at (yaw, pitch).

    Two sources, take the stronger (lower weight):
      1. Per-bin lookup — exact bin match from Stage 0 histogram.
      2. Region-distance penalty — smooth falloff from hotspot region centres.

    Returns weight in [penalty_min, 1.0].  1.0 = neutral.
    """
    bin_deg   = hm["sphere_bin_deg"]
    low_floor = hm["low_duty_floor"]
    threshold = hm["duty_cycle_threshold"]
    pmin      = hm["penalty_min"]

    # 1. Per-bin
    b = bin_id_for(yaw, pitch, bin_deg)
    bin_w = bin_lookup.get(b, 1.0)

    # 2. Region-distance  (re-implements region_penalty_for_point from Stage 0)
    region_w = 1.0
    for r in hm.get("hotspot_regions", []):
        d = angular_distance(yaw, pitch, r["centre_yaw"], r["centre_pitch"])
        core = r["radius_deg"]
        band = core
        if d <= core:
            eff_duty = r["peak_duty"]
        elif d <= core + band:
            frac     = 1.0 - (d - core) / band
            eff_duty = r["peak_duty"] * frac
        else:
            continue
        # Penalty curve
        if eff_duty < low_floor:
            w = 1.0
        elif eff_duty >= threshold:
            w = pmin
        else:
            frac2 = (eff_duty - low_floor) / (threshold - low_floor)
            w = pmin + (1.0 - pmin) * 0.5 * (1.0 + math.cos(math.pi * frac2))
        if w < region_w:
            region_w = w

    return max(pmin, min(bin_w, region_w))


# ─────────────────────────────────────────────────────────────────────────────
# Detector (same cheap path as Stage 0)
# ─────────────────────────────────────────────────────────────────────────────
def load_detector(weights_path):
    from ultralytics import YOLO
    return YOLO(weights_path)


def detect_ball_candidates(model, equirect_frame):
    """
    Run YOLO on each perspective crop and return raw detections.

    Each entry in the returned list is a tuple:
        (yaw, pitch, conf, crop_yaw, bbox_xyxy, crop_w, crop_h)
    where bbox_xyxy = [x1, y1, x2, y2] in crop-pixel coordinates.
    """
    raw = []
    for crop_yaw in CROP_YAWS_DEG:
        crop = extract_crop_frame(equirect_frame, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
        results = model.predict(crop, conf=YOLO_CONF, imgsz=YOLO_IMGSZ,
                                classes=[BALL_CLASS_ID], verbose=False)
        for r in results:
            if r.boxes is None:
                continue
            for box in r.boxes:
                conf = float(box.conf[0])
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                cx, cy_px = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                yaw, pitch = crop_pixel_to_yaw_pitch(cx, cy_px, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                raw.append((yaw, pitch, conf, crop_yaw, [x1, y1, x2, y2], CROP_W, CROP_H))
    # Deduplicate using (yaw, pitch, conf) only — geometry kept on first surviving hit
    return _dedupe_with_geometry(raw)


def _dedupe_with_geometry(raw, thresh_deg=DEDUP_THRESH_DEG):
    """
    Deduplication that preserves geometry alongside each surviving detection.
    Mirrors dedupe_detections() logic exactly; highest-conf wins per cluster.
    """
    kept = []  # each entry: (yaw, pitch, conf, crop_yaw, bbox_xyxy, crop_w, crop_h)
    for det in sorted(raw, key=lambda d: -d[2]):
        yaw, pitch = det[0], det[1]
        if all(angular_distance(yaw, pitch, k[0], k[1]) > thresh_deg for k in kept):
            kept.append(det)
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Detection geometry helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_detection_geometry(x1, y1, x2, y2, crop_w, crop_h):
    """
    Build the detection_geometry sub-object for a new YOLO detection.

    All values are in crop-pixel coordinates.  No rounding on bbox_xyxy to
    preserve raw detector output; derived scalars are rounded to 2 dp.
    """
    w = x2 - x1
    h = y2 - y1
    area = w * h
    aspect = round(w / h, 4) if h > 0 else None
    return {
        "bbox_xyxy":       [round(x1, 2), round(y1, 2), round(x2, 2), round(y2, 2)],
        "bbox_width_px":   round(w, 2),
        "bbox_height_px":  round(h, 2),
        "bbox_area_px":    round(area, 2),
        "bbox_aspect_ratio": aspect,
        "crop_width_px":   crop_w,
        "crop_height_px":  crop_h,
    }


def _null_detection_geometry():
    """
    Explicit null geometry for Stage 0 reused detections where raw box
    coordinates are unavailable.  All fields present, all values null.
    """
    return {
        "bbox_xyxy":         None,
        "bbox_width_px":     None,
        "bbox_height_px":    None,
        "bbox_area_px":      None,
        "bbox_aspect_ratio": None,
        "crop_width_px":     None,
        "crop_height_px":    None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Candidate processing
# ─────────────────────────────────────────────────────────────────────────────
def process_candidate(yaw, pitch, raw_conf, source, crop_yaw,
                      hm, bin_lookup, pitch_min, pitch_max,
                      detection_geometry=None):
    """
    Apply pitch hard-filter then hotspot penalty.

    Returns a dict ready for stage1_candidates.json, or None if pitch-rejected.

    detection_geometry must be the output of _make_detection_geometry() for
    new detections, or _null_detection_geometry() for Stage 0 reused
    detections.  If omitted (legacy callers / dry-run), null geometry is used.
    """
    # Hard pitch filter — drop before any further processing
    if pitch < pitch_min or pitch > pitch_max:
        return None

    penalty = penalty_weight_from_map(yaw, pitch, hm, bin_lookup)
    weighted_conf = raw_conf * penalty

    # Region label for report (nearest region within 2× its core radius)
    region_label = None
    for r in hm.get("hotspot_regions", []):
        d = angular_distance(yaw, pitch, r["centre_yaw"], r["centre_pitch"])
        if d <= r["radius_deg"] * 2:
            region_label = f"({r['centre_yaw']:.1f},{r['centre_pitch']:.1f})"
            break

    if detection_geometry is None:
        detection_geometry = _null_detection_geometry()

    return {
        "yaw":           round(yaw, 3),
        "pitch":         round(pitch, 3),
        "raw_conf":      round(raw_conf, 4),
        "penalty":       round(penalty, 4),
        "weighted_conf": round(weighted_conf, 4),
        "source":        source,        # "stage0_reuse" | "new_detection"
        "crop_yaw":      crop_yaw,
        "region":        region_label,  # None if not near any hotspot region
        # ── Stage 1c: detector box geometry ─────────────────────────────────
        # Present for new_detection; all fields null for stage0_reuse.
        # Do not filter or re-weight on these fields here; evidence only.
        "detection_geometry": detection_geometry,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
# MOG2 equirect blob → yaw/pitch conversion
# ─────────────────────────────────────────────────────────────────────────────
def _equirect_pixel_to_yaw_pitch(px, py, frame_width, frame_height):
    """Convert equirectangular pixel (cx, cy) to (yaw_deg, pitch_deg)."""
    yaw   = (px / frame_width  - 0.5) * 360.0
    pitch = (0.5 - py / frame_height) * 180.0
    return yaw, pitch


def _mog2_candidate_geometry(x, y, w, h, frame_width, frame_height):
    """Build detection_geometry for a MOG2 blob in equirect pixel coords."""
    area   = w * h
    aspect = round(w / h, 4) if h > 0 else None
    return {
        "bbox_xyxy":         [round(float(x), 2), round(float(y), 2),
                               round(float(x + w), 2), round(float(y + h), 2)],
        "bbox_width_px":     round(float(w), 2),
        "bbox_height_px":    round(float(h), 2),
        "bbox_area_px":      round(float(area), 2),
        "bbox_aspect_ratio": aspect,
        "crop_width_px":     frame_width,
        "crop_height_px":    frame_height,
    }


def run_stage1(args):
    t0 = time.time()

    # ── Environment banner (req 1) ────────────────────────────────────────────
    _device_str, _gpu_name, _torch_ver, _cuda_ver = _torch_env_banner()

    # ── Load Stage 0 outputs ──────────────────────────────────────────────────
    print(f"[stage1] Loading hotspot map: {args.hotspot_map}")
    hm, bin_lookup = load_hotspot_map(args.hotspot_map)

    print(f"[stage1] Loading Stage 0 detections: {args.stage0_detections}")
    with open(args.stage0_detections) as f:
        s0 = json.load(f)
    s0_fps    = s0["fps"]
    s0_stride = s0["sample_stride"]
    # Keys are stored as strings in JSON; normalise to int
    s0_frames = {int(k): v for k, v in s0["frames"].items()}
    s0_frame_set = set(s0_frames.keys())
    print(f"[stage1] Stage 0: {len(s0_frame_set)} sampled frames  "
          f"fps={s0_fps:.2f}  stride={s0_stride}")
    print(f"[stage1] Pitch bounds: [{args.pitch_min_deg}°, {args.pitch_max_deg}°]  (hard filter)")
    print(f"[stage1] Hotspot regions loaded: {len(hm.get('hotspot_regions', []))}")

    # ── Open video ────────────────────────────────────────────────────────────
    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open {args.input}")
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps          = cap.get(cv2.CAP_PROP_FPS) or s0_fps
    frame_width  = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    venue_polygon = _load_venue_mask(args.venue_mask, frame_width, frame_height)
    venue_mask_enabled = venue_polygon is not None
    if venue_mask_enabled:
        print(f"[stage1] Venue mask enabled: {args.venue_mask}")
    else:
        print("[stage1] Venue mask not found; using full frame")
    print(f"[stage1] Clip: {total_frames} frames @ {fps:.2f} fps")

    # ── Initialise MOG2 ──────────────────────────────────────────────────────
    mog2 = None
    if not getattr(args, "no_mog2", False) and not args.dry_run:
        mog2 = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=16, detectShadows=False
        )
        print("[stage1] MOG2 primary detector enabled "
              "(varThreshold=16, history=500, min-circ=0.50)")

    model = None
    if not args.dry_run:
        if not args.weights or not os.path.isfile(args.weights):
            raise RuntimeError(f"Detector weights not found: {args.weights}")
        print(f"[stage1] Loading detector: {args.weights}")
        model = load_detector(args.weights)
        # ── Preflight check (req 2) ───────────────────────────────────────────
        if _device_str != "cuda":
            print("[stage1] PREFLIGHT FAIL: CUDA is not available — refusing to run on CPU.", file=sys.stderr)
            sys.exit(2)
        _preflight_check(model, _device_str)

    # ── Per-frame processing ──────────────────────────────────────────────────
    all_candidates = {}        # frame_idx -> [candidate_dict, ...]

    # Counters
    n_frames_processed   = 0
    n_s0_reuse           = 0   # detections taken from stage0_detections.json
    n_new_detected       = 0   # detections from re-running the detector
    n_pitch_rejected     = 0   # dropped by hard pitch filter
    n_venue_rejected     = 0   # dropped outside optional venue polygon
    n_kept               = 0   # after venue/pitch filters, penalty applied
    n_nms_warnings       = 0   # NMS log lines captured from Ultralytics logger
    n_mog2_primary       = 0   # frames where MOG2 sole blob used as candidate
    n_mog2_fallthrough   = 0   # frames where MOG2 result caused YOLO fallthrough

    # Intercept Ultralytics logger for NMS warning lines
    import logging as _logging
    class _NMSCounter(_logging.Handler):
        def __init__(self): super().__init__(); self.count = 0
        def emit(self, record):
            msg = record.getMessage().lower()
            if "nms" in msg and ("time" in msg or "warn" in msg or "limit" in msg):
                self.count += 1
    _nms_handler = _NMSCounter()
    try:
        _ul_logger = _logging.getLogger("ultralytics")
        _ul_logger.addHandler(_nms_handler)
    except Exception:
        _nms_handler = None

    fence_weighted_total = 0.0  # weighted_conf sum for candidates near fence
    fence_raw_total      = 0.0  # raw_conf sum for candidates near fence
    fence_count          = 0

    for frame_idx in range(total_frames):
        if args.max_frames and frame_idx >= args.max_frames:
            break

        # Each entry: (yaw, pitch, conf, crop_yaw, source, geometry_or_none)
        candidates_raw = []

        if frame_idx in s0_frame_set:
            # ── Reuse Stage 0 detections ──────────────────────────────────────
            for det in s0_frames[frame_idx]:
                # det: [yaw, pitch, conf, crop_yaw, [yb, pb]]
                # Stage 0 did not record bbox; geometry is explicitly null.
                candidates_raw.append((
                    det[0], det[1], det[2], det[3],
                    "stage0_reuse",
                    _null_detection_geometry(),
                ))
            n_s0_reuse += len(s0_frames[frame_idx])
        else:
            # ── Detect on this frame ───────────────────────────────────────────
            if args.dry_run:
                dets = []
            else:
                cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
                ret, frame = cap.read()
                if not ret:
                    all_candidates[frame_idx] = []
                    continue

                # ── MOG2 primary ──────────────────────────────────────────────
                mog2_used = False
                if mog2 is not None:
                    fg_mask = mog2.apply(frame)
                    # Morphological open to remove noise
                    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
                    fg_mask = cv2.morphologyEx(fg_mask, cv2.MORPH_OPEN, kernel)
                    contours, _ = cv2.findContours(
                        fg_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
                    )
                    blobs = []
                    for cnt in contours:
                        area = cv2.contourArea(cnt)
                        if area < 100 or area > 800:
                            continue
                        x, y, w, h = cv2.boundingRect(cnt)
                        aspect = w / h if h > 0 else 999
                        if aspect > 2.5:
                            continue
                        perim = cv2.arcLength(cnt, True)
                        circ  = (4 * math.pi * area / (perim * perim)) if perim > 0 else 0
                        if circ < 0.50:
                            continue
                        blobs.append((x, y, w, h, circ))

                    if len(blobs) == 1:
                        # Single confident blob → use as candidate, skip YOLO
                        x, y, w, h, circ = blobs[0]
                        cx = x + w / 2.0
                        cy_px = y + h / 2.0
                        yaw, pitch = _equirect_pixel_to_yaw_pitch(
                            cx, cy_px, frame_width, frame_height
                        )
                        geom = _mog2_candidate_geometry(x, y, w, h, frame_width, frame_height)
                        candidates_raw.append((yaw, pitch, round(circ, 4), None, "mog2", geom))
                        n_mog2_primary += 1
                        mog2_used = True
                    else:
                        # 0 or >1 blobs → fall through to YOLO
                        n_mog2_fallthrough += 1
                else:
                    # warm MOG2 background model even when not used as primary
                    pass

                if not mog2_used:
                    dets = detect_ball_candidates(model, frame)
                    for det in dets:
                        yaw, pitch, conf, crop_yaw, bbox_xyxy, crop_w, crop_h = det
                        geom = _make_detection_geometry(
                            bbox_xyxy[0], bbox_xyxy[1], bbox_xyxy[2], bbox_xyxy[3],
                            crop_w, crop_h,
                        )
                        candidates_raw.append((yaw, pitch, conf, crop_yaw, "new_detection", geom))
                    n_new_detected += len(dets)

        # ── Apply venue mask, pitch filter + penalty ──────────────────────────
        frame_cands = []
        for (yaw, pitch, raw_conf, crop_yaw, source, geom) in candidates_raw:
            if not _venue_contains(yaw, pitch, venue_polygon, frame_width, frame_height):
                n_venue_rejected += 1
                continue
            cand = process_candidate(
                yaw, pitch, raw_conf, source, crop_yaw,
                hm, bin_lookup,
                args.pitch_min_deg, args.pitch_max_deg,
                detection_geometry=geom,
            )
            if cand is None:
                n_pitch_rejected += 1
                continue
            frame_cands.append(cand)
            n_kept += 1

            # Fence proximity tracking (for report)
            d = angular_distance(yaw, pitch, KNOWN_FENCE_YAW, KNOWN_FENCE_PITCH)
            if d <= 5.0:
                fence_count          += 1
                fence_raw_total      += raw_conf
                fence_weighted_total += cand["weighted_conf"]

        all_candidates[frame_idx] = frame_cands
        n_frames_processed += 1

        if (n_frames_processed % 100) == 0:
            el = time.time() - t0
            pct = 100.0 * n_frames_processed / total_frames if total_frames else 0.0
            spf = el / n_frames_processed if n_frames_processed else 0.0
            remaining = (total_frames - n_frames_processed) * spf
            # n_raw_so_far = detections before pitch filter (counters updated per-frame)
            n_raw_so_far = n_s0_reuse + n_new_detected
            print(
                f"[stage1] {n_frames_processed}/{total_frames} ({pct:.1f}%)  "
                f"elapsed={el:.1f}s  spf={spf:.3f}s  ETA={remaining:.0f}s  "
                f"raw={n_raw_so_far}  kept={n_kept}  venue_rej={n_venue_rejected}  "
                f"pitch_rej={n_pitch_rejected}  nms_warn={n_nms_warnings}",
                flush=True,
            )

    cap.release()

    # Sync NMS warning count from Ultralytics logger handler
    if _nms_handler is not None:
        n_nms_warnings = _nms_handler.count
        try:
            _ul_logger.removeHandler(_nms_handler)
        except Exception:
            pass

    # ── Compute stats ─────────────────────────────────────────────────────────
    n_raw_total  = n_s0_reuse + n_new_detected  # before pitch filter
    # Reduction vs raw (pre-filter) candidates
    pitch_rej_pct    = (n_pitch_rejected / n_raw_total * 100) if n_raw_total else 0.0
    candidates_after = n_kept
    # Weighted confidence reduction: 1 - sum(weighted_conf) / sum(raw_conf)
    sum_raw_kept      = sum(c["raw_conf"]      for cs in all_candidates.values() for c in cs)
    sum_weighted_kept = sum(c["weighted_conf"] for cs in all_candidates.values() for c in cs)
    weight_reduction  = (1.0 - sum_weighted_kept / sum_raw_kept) if sum_raw_kept else 0.0

    fence_suppression = (1.0 - fence_weighted_total / fence_raw_total) if fence_raw_total else None

    # Region breakdown: how many candidates landed in each region
    region_counts = {}
    for cs in all_candidates.values():
        for c in cs:
            label = c["region"] or "_none_"
            region_counts[label] = region_counts.get(label, 0) + 1

    # ── Write outputs ─────────────────────────────────────────────────────────
    os.makedirs(args.output_dir, exist_ok=True)

    out_cands = os.path.join(args.output_dir, "stage1_candidates.json")
    with open(out_cands, "w") as f:
        json.dump({
            "fps":              fps,
            "total_frames":     total_frames,
            "pitch_min_deg":    args.pitch_min_deg,
            "pitch_max_deg":    args.pitch_max_deg,
            "venue_mask_enabled": venue_mask_enabled,
            "venue_mask":       args.venue_mask if venue_mask_enabled else None,
            "hotspot_map":      os.path.basename(args.hotspot_map),
            "stage0_detections": os.path.basename(args.stage0_detections),
            "frames":           all_candidates,
        }, f)
    print(f"[stage1] Candidates written -> {out_cands}")

    report = build_report(
        n_frames_processed, n_raw_total, n_s0_reuse, n_new_detected,
        n_pitch_rejected, pitch_rej_pct, n_venue_rejected, venue_mask_enabled,
        candidates_after, weight_reduction,
        fence_count, fence_suppression,
        region_counts, args,
    )
    out_rep = os.path.join(args.output_dir, "stage1_report.txt")
    with open(out_rep, "w") as f:
        f.write(report)
    print("\n" + report)
    print(f"\n[stage1] Done in {time.time()-t0:.1f}s. Outputs in {args.output_dir}/")

    # ── run_summary.json (req 5) ──────────────────────────────────────────────
    duration = time.time() - t0
    summary = {
        "device":          _device_str,
        "gpu_name":        _gpu_name,
        "torch_version":   _torch_ver,
        "cuda_version":    _cuda_ver,
        "total_frames":    total_frames,
        "frames_processed": n_frames_processed,
        "duration_s":      round(duration, 2),
        "avg_spf":         round(duration / n_frames_processed, 4) if n_frames_processed else None,
        "n_s0_reuse":      n_s0_reuse,
        "n_new_detected":  n_new_detected,
        "n_pitch_rejected": n_pitch_rejected,
        "n_venue_rejected": n_venue_rejected,
        "venue_mask_enabled": venue_mask_enabled,
        "n_kept":              n_kept,
        "n_nms_warnings":      n_nms_warnings,
        "mog2_primary_count":  n_mog2_primary,
        "mog2_fallthrough_count": n_mog2_fallthrough,
    }
    out_summary = os.path.join(args.output_dir, "run_summary.json")
    with open(out_summary, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"[stage1] run_summary.json -> {out_summary}")


def build_report(n_frames, n_raw, n_s0_reuse, n_new, n_pitch_rej, pitch_rej_pct,
                 n_venue_rej, venue_mask_enabled, n_kept, weight_reduction,
                 fence_count, fence_suppression,
                 region_counts, args):
    lines = []
    lines.append("=" * 70)
    lines.append("STAGE 1 — CANDIDATE GENERATION — REPORT")
    lines.append("=" * 70)
    lines.append(f"Input clip            : {os.path.basename(args.input)}")
    lines.append(f"Pitch bounds          : [{args.pitch_min_deg}°, {args.pitch_max_deg}°]  (hard filter)")
    lines.append(f"Hotspot map           : {os.path.basename(args.hotspot_map)}")
    lines.append(f"Venue mask            : {args.venue_mask if venue_mask_enabled else 'disabled (full frame)'}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("CANDIDATE COUNTS")
    lines.append("-" * 70)
    lines.append(f"Frames processed      : {n_frames}")
    lines.append(f"Raw candidates        : {n_raw}")
    lines.append(f"  from Stage 0 reuse  : {n_s0_reuse}")
    lines.append(f"  from new detection  : {n_new}")
    lines.append(f"Venue-rejected        : {n_venue_rej}")
    lines.append(f"Pitch-rejected        : {n_pitch_rej}  ({pitch_rej_pct:.1f}% of raw)")
    lines.append(f"After venue/pitch     : {n_kept}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("PENALTY EFFECT (weighted confidence reduction)")
    lines.append("-" * 70)
    lines.append(f"Weighted conf reduction vs raw : {weight_reduction*100:.1f}%")
    lines.append(f"  (1 - sum(weighted_conf) / sum(raw_conf) across kept candidates)")
    lines.append("")
    lines.append("-" * 70)
    lines.append("REGION BREAKDOWN (candidates touching each hotspot region)")
    lines.append("-" * 70)
    for label, count in sorted(region_counts.items(), key=lambda x: -x[1]):
        lines.append(f"  {label:<30} : {count}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("FENCE REGION CHECK")
    lines.append("-" * 70)
    lines.append(f"Known fence reference : ({KNOWN_FENCE_YAW}, {KNOWN_FENCE_PITCH})")
    lines.append(f"Candidates within 5°  : {fence_count}")
    if fence_suppression is not None:
        lines.append(f"Weighted conf reduction in fence zone : {fence_suppression*100:.1f}%")
        flag = "YES" if fence_suppression >= 0.50 else "NO"
        lines.append(f"-> FENCE EFFECTIVELY DOWN-WEIGHTED: {flag}  (want >=50% suppression)")
    else:
        lines.append("-> No candidates near fence — nothing to check")
    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Stage 1 candidate generation")
    ap.add_argument("--input",              required=True,
                    help="equirect MP4 (same clip as Stage 0)")
    ap.add_argument("--hotspot-map",        required=True,
                    help="stage0_output/hotspot_map.json")
    ap.add_argument("--stage0-detections",  required=True,
                    help="stage0_output/stage0_detections.json")
    ap.add_argument("--output-dir",         default="stage1_output")
    ap.add_argument("--weights",            default=os.environ.get("BALL_WEIGHTS", ""),
                    help="YOLO ball detector weights (.pt)")
    ap.add_argument("--venue-mask",         default=DEFAULT_VENUE_MASK,
                    help="Optional venue_mask.json; missing file uses full frame")
    ap.add_argument("--pitch-min-deg",      type=float, default=DEF_PITCH_MIN_DEG,
                    help="Hard pitch lower bound (default: -30)")
    ap.add_argument("--pitch-max-deg",      type=float, default=DEF_PITCH_MAX_DEG,
                    help="Hard pitch upper bound (default: +18)")
    ap.add_argument("--max-frames",         type=int, default=None,
                    help="Cap frames processed (quick test)")
    ap.add_argument("--dry-run",            action="store_true",
                    help="Skip detection; reuse Stage 0 data only (pipeline/IO test)")
    ap.add_argument("--no-mog2",            action="store_true",
                    help="Disable MOG2 primary detector; use YOLO only (regression test)")
    args = ap.parse_args()
    run_stage1(args)


if __name__ == "__main__":
    main()
