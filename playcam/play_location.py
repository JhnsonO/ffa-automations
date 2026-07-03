#!/usr/bin/env python3
"""
playcam/play_location.py

Phase 1 — play-location measurement layer.

Samples an equirectangular 360 clip at a low frame rate, detects people
across four fixed-yaw rectilinear crops (not on the raw distorted 360
frame), back-projects detections to spherical (yaw, pitch) coordinates,
clusters them, and writes a timeline of the dominant active-play area.

This is a MEASUREMENT layer only:
  - no Kalman smoothing
  - no camera rendering
  - no homography
  - no ball detection or ball_tracker/ imports
  - no coupling to ball_tracker/ — geometry helpers below are a standalone
    reimplementation of the same proven math used elsewhere in this repo
    (crop/back-projection identical in form to ball_tracker/player_activity.py),
    kept separate so playcam/ has zero import dependency on ball_tracker/.

Output: playcam/output/play_location.jsonl (one JSON object per sampled frame)
Debug:  playcam/output/debug_frames/frame_<idx>.png (equirect-space plot of
        detections, chosen cluster, and target yaw/pitch)

Usage:
  python3 play_location.py --input clip.mp4
  python3 play_location.py --input clip.mp4 --fps 3 --start 30 --duration 20

Env:
  YOLO_PERSON_WEIGHTS  — person detector weights (default: yolov8n.pt)
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

YOLO_PERSON_WEIGHTS = os.environ.get("YOLO_PERSON_WEIGHTS", "yolov8n.pt")

CROP_YAWS_DEG = [0, 90, 180, 270]
CROP_FOV_DEG = 110
CROP_W = 1280
CROP_H = 720

PERSON_CLASS_ID = 0  # COCO class 0
YOLO_CONF = 0.25
YOLO_IMGSZ = 1280

# Exclude crowd in stands / sky / ground clutter outside plausible play height
PITCH_MIN_DEG = -25.0
PITCH_MAX_DEG = 55.0

# Merge duplicate detections of the same person across overlapping crop seams
DEDUP_RADIUS_DEG = 8.0

# Clustering — dominant area of play
CLUSTER_RADIUS_DEG = 25.0
CLUSTER_MIN_PLAYERS = 2

DEFAULT_SAMPLE_FPS = 3.0
DEFAULT_MAX_FRAMES = 200  # safety cap so a bad --duration doesn't run forever
DEFAULT_FIXED_PITCH = 4.0  # fallback only -- prefer --venue-profile per venue/mount


# ---------------------------------------------------------------------------
# Geometry — equirect crop extraction and spherical back-projection
# (standalone reimplementation; same math as elsewhere in this repo)
# ---------------------------------------------------------------------------

def extract_crop_frame(equirect_frame, yaw_deg, fov_deg=CROP_FOV_DEG,
                        out_w=CROP_W, out_h=CROP_H):
    """Pure yaw-only perspective crop from an equirectangular frame."""
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
    cy = math.radians(yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    yaw_map = np.arctan2(wx, wz)
    pitch_map = np.arcsin(np.clip(wy, -1, 1))
    map_x = ((yaw_map / (2 * math.pi)) + 0.5) * w_eq
    map_y = (0.5 - pitch_map / math.pi) * h_eq
    return cv2.remap(equirect_frame,
                      map_x.astype(np.float32), map_y.astype(np.float32),
                      interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_WRAP)


def crop_pixel_to_yaw_pitch(px, py, crop_yaw_deg,
                             fov_deg=CROP_FOV_DEG, w=CROP_W, h=CROP_H):
    """Back-project a crop pixel to global spherical (yaw deg, pitch deg).

    Must be the exact algebraic inverse of extract_crop_frame's ray math.
    BUG FIXED 2026-07-02: this previously used (w/h) instead of (h/w) for the
    vertical ray component, inverting the aspect-ratio correction and
    amplifying pitch by (w/h)^2 (~3.16x at 1280x720). That pushed detected
    player positions far above their true position (into the sky). Verified
    against extract_crop_frame via run_self_test() below — do not change
    this factor without re-running --self-test.
    """
    nx = (px - w / 2.0) / (w / 2.0)
    ny = (py - h / 2.0) / (h / 2.0)
    f = 1.0 / math.tan(math.radians(fov_deg / 2.0))
    rx = nx / f
    ry = -ny / f * (h / w)
    rz = 1.0
    norm = math.sqrt(rx**2 + ry**2 + rz**2)
    rx, ry, rz = rx / norm, ry / norm, rz / norm
    cy = math.radians(crop_yaw_deg)
    wx = math.cos(cy) * rx + math.sin(cy) * rz
    wy = ry
    wz = -math.sin(cy) * rx + math.cos(cy) * rz
    return (math.degrees(math.atan2(wx, wz)),
            math.degrees(math.asin(max(-1.0, min(1.0, wy)))))


def angular_distance(y1, p1, y2, p2):
    """Great-circle distance in degrees between two (yaw, pitch) points."""
    dy = math.radians(y1 - y2)
    dp = math.radians(p1 - p2)
    a = (math.sin(dp / 2) ** 2
         + math.cos(math.radians(p1)) * math.cos(math.radians(p2))
         * math.sin(dy / 2) ** 2)
    return math.degrees(2 * math.asin(math.sqrt(min(1.0, a))))


def yaw_mean(yaws):
    """Circular mean of yaw angles (handles the -180/180 wraparound)."""
    sx = sum(math.cos(math.radians(y)) for y in yaws)
    sy = sum(math.sin(math.radians(y)) for y in yaws)
    return math.degrees(math.atan2(sy, sx))


def spherical_centroid(points):
    return yaw_mean([p[0] for p in points]), sum(p[1] for p in points) / len(points)


def point_in_polygon(x, y, polygon):
    """Standard ray-casting point-in-polygon test. polygon: list of [x, y]."""
    n = len(polygon)
    inside = False
    x1, y1 = polygon[0]
    for i in range(1, n + 1):
        x2, y2 = polygon[i % n]
        if y > min(y1, y2):
            if y <= max(y1, y2):
                if x <= max(x1, x2):
                    if y1 != y2:
                        x_intersect = (y - y1) * (x2 - x1) / (y2 - y1) + x1
                    if x1 == x2 or x <= x_intersect:
                        inside = not inside
        x1, y1 = x2, y2
    return inside


def foot_point_in_play_area(yaw_deg, pitch_deg, play_area):
    """
    Project a spherical (yaw, pitch) foot-point to the equirect frame the
    play_area polygon was calibrated against (via frame_width/frame_height,
    resolution-independent -- uses fractional position, not actual pixels
    of the current video), and test containment.
    """
    fx, fy = _yaw_pitch_to_equirect_xy(yaw_deg, pitch_deg, 1.0, 1.0)
    px = fx * play_area["frame_width"]
    py = fy * play_area["frame_height"]
    return point_in_polygon(px, py, play_area["polygon"])


# ---------------------------------------------------------------------------
# Deterministic round-trip self-test
# ---------------------------------------------------------------------------

def _yaw_pitch_to_equirect_xy(yaw_deg, pitch_deg, w_eq, h_eq):
    """Same formula extract_crop_frame uses to sample the source equirect.

    BUG FIXED 2026-07-02: previously clamped y to [0, h_eq-1], which assumed
    h_eq was always a pixel count. When called with h_eq=1.0 (fractional,
    resolution-independent use -- see foot_point_in_play_area) this clipped
    every y value to 0.0 regardless of pitch, silently breaking play_area
    masking for every point. Clamp is now [0, h_eq] and pixel callers that
    need a valid array index (e.g. run_self_test) round/clamp separately.
    """
    x = ((yaw_deg / 360.0) + 0.5) * w_eq
    y = (0.5 - pitch_deg / 180.0) * h_eq
    return x % w_eq, max(0.0, min(h_eq, y))


def run_self_test(tolerance_deg=1.5):
    """
    For each of the 4 crop yaws, inject a marker at 5 known crop-local
    positions (center/left/right/top/bottom), draw it on a synthetic
    equirect canvas, run it through the ACTUAL extract_crop_frame (forward,
    used at detection time) to get the crop image, locate the marker in
    that crop, then run it through crop_pixel_to_yaw_pitch (backward, used
    to place detections) and check we recover the yaw/pitch we started
    from within `tolerance_deg`.

    This exercises the exact two functions used in production together,
    so it catches sign, origin, and scale mismatches between them -- not
    just internal self-consistency of one function.
    """
    w_eq, h_eq = 2048, 1024
    points_norm = {
        "center": (0.0, 0.0),
        "left":   (-0.7, 0.0),
        "right":  (0.7, 0.0),
        "top":    (0.0, -0.7),
        "bottom": (0.0, 0.7),
    }

    all_pass = True
    for crop_yaw in CROP_YAWS_DEG:
        for name, (nx, ny) in points_norm.items():
            px = (nx + 1.0) * CROP_W / 2.0
            py = (ny + 1.0) * CROP_H / 2.0

            # Ground truth: what yaw/pitch is this crop pixel supposed to be?
            # Derived independently via crop_pixel_to_yaw_pitch itself would be
            # circular, so instead: inject a marker at a *guessed* target
            # yaw/pitch, forward-project with extract_crop_frame, confirm the
            # marker lands near (px, py) in the crop -- iterating isn't
            # needed since we control the injection point directly:
            # place the marker using crop_pixel_to_yaw_pitch's own output as
            # the injection target, then check extract_crop_frame's crop
            # shows it at (px, py), i.e. round-trip in the OTHER direction.
            target_yaw, target_pitch = crop_pixel_to_yaw_pitch(px, py, crop_yaw)

            canvas = np.zeros((h_eq, w_eq, 3), dtype=np.uint8)
            mx, my = _yaw_pitch_to_equirect_xy(target_yaw, target_pitch, w_eq, h_eq)
            mx_i, my_i = int(round(mx)), int(round(my))
            cv2.circle(canvas, (mx_i, my_i), 6, (255, 255, 255), -1)

            crop = extract_crop_frame(canvas, crop_yaw)
            gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
            _, max_val, _, max_loc = cv2.minMaxLoc(gray)

            if max_val < 100:
                print(f"FAIL crop_yaw={crop_yaw:>3} {name:>6}: marker not found in crop "
                      f"(max_val={max_val})")
                all_pass = False
                continue

            found_px, found_py = max_loc
            err_px = math.hypot(found_px - px, found_py - py)

            # Also verify the back-projection recovers target_yaw/pitch from
            # the pixel where the marker actually landed (not just the
            # pixel we asked for) -- this is the real end-to-end check.
            recovered_yaw, recovered_pitch = crop_pixel_to_yaw_pitch(
                found_px, found_py, crop_yaw)
            err_yaw = abs(((recovered_yaw - target_yaw + 180) % 360) - 180)
            err_pitch = abs(recovered_pitch - target_pitch)

            status = "PASS" if (err_yaw <= tolerance_deg and err_pitch <= tolerance_deg
                                 and err_px <= 15.0) else "FAIL"
            if status == "FAIL":
                all_pass = False
            print(f"{status} crop_yaw={crop_yaw:>3} {name:>6}: "
                  f"target=({target_yaw:6.2f},{target_pitch:6.2f}) "
                  f"pixel_err={err_px:.1f}px  "
                  f"recovered=({recovered_yaw:6.2f},{recovered_pitch:6.2f}) "
                  f"err=({err_yaw:.2f},{err_pitch:.2f})")

    print(f"\n[self-test] {'ALL PASS' if all_pass else 'FAILURES FOUND'}")
    return all_pass


# ---------------------------------------------------------------------------
# Dedup + clustering
# ---------------------------------------------------------------------------

def dedup_players(players, radius_deg=DEDUP_RADIUS_DEG):
    """Merge detections of the same physical person seen in overlapping crops."""
    if not players:
        return []
    order = sorted(range(len(players)), key=lambda i: -players[i]["conf"])
    used = [False] * len(players)
    kept = []
    for i in order:
        if used[i]:
            continue
        kept.append(players[i])
        used[i] = True
        for j in range(len(players)):
            if not used[j]:
                if angular_distance(players[i]["yaw"], players[i]["pitch"],
                                     players[j]["yaw"], players[j]["pitch"]) < radius_deg:
                    used[j] = True
    return kept


def cluster_players(players, radius_deg=CLUSTER_RADIUS_DEG, min_pts=CLUSTER_MIN_PLAYERS):
    """DBSCAN-lite clustering in spherical space."""
    if not players:
        return []
    n = len(players)
    cluster = [-1] * n

    def dist(i, j):
        return angular_distance(players[i]["yaw"], players[i]["pitch"],
                                 players[j]["yaw"], players[j]["pitch"])

    cid = 0
    for i in range(n):
        if cluster[i] != -1:
            continue
        nbrs = [j for j in range(n) if i != j and dist(i, j) <= radius_deg]
        if len(nbrs) + 1 < min_pts:
            continue
        cluster[i] = cid
        stack = list(nbrs)
        while stack:
            k = stack.pop()
            if cluster[k] == -1:
                cluster[k] = cid
                nk = [j for j in range(n) if j != k and dist(k, j) <= radius_deg]
                if len(nk) + 1 >= min_pts:
                    stack.extend(j for j in nk if cluster[j] == -1)
        cid += 1

    groups = defaultdict(list)
    for i, c in enumerate(cluster):
        if c != -1:
            groups[c].append(players[i])
    return list(groups.values())


def best_cluster(clusters):
    """Dominant area of play = largest cluster, tie-broken by mean confidence."""
    if not clusters:
        return None
    return max(clusters, key=lambda c: (len(c), sum(p["conf"] for p in c) / len(c)))


# ---------------------------------------------------------------------------
# Debug preview
# ---------------------------------------------------------------------------

def render_debug_frame(equirect_frame, players, cluster, target, out_path,
                        thumb_w=960, excluded=None):
    """
    Draw a downscaled equirect thumbnail with:
      - a dot per KEPT detected player (yaw/pitch -> equirect x/y)
      - cluster members highlighted
      - excluded (outside play_area) detections in a muted grey
      - a crosshair at the chosen target yaw/pitch
    """
    h_eq, w_eq = equirect_frame.shape[:2]
    thumb_h = int(thumb_w * h_eq / w_eq)
    thumb = cv2.resize(equirect_frame, (thumb_w, thumb_h))

    def yaw_pitch_to_xy(yaw, pitch):
        x = int(((yaw / 360.0) + 0.5) * thumb_w) % thumb_w
        y = int((0.5 - pitch / 180.0) * thumb_h)
        y = max(0, min(thumb_h - 1, y))
        return x, y

    for p in (excluded or []):
        x, y = yaw_pitch_to_xy(p.get("foot_yaw", p["yaw"]), p.get("foot_pitch", p["pitch"]))
        cv2.circle(thumb, (x, y), 4, (110, 110, 110), -1)  # muted grey

    cluster_set = {id(p) for p in cluster} if cluster else set()
    for p in players:
        x, y = yaw_pitch_to_xy(p["yaw"], p["pitch"])
        color = (0, 255, 0) if id(p) in cluster_set else (0, 165, 255)
        cv2.circle(thumb, (x, y), 5, color, -1)

    if target is not None:
        tx, ty = yaw_pitch_to_xy(target["yaw"], target["pitch"])
        cv2.drawMarker(thumb, (tx, ty), (0, 0, 255),
                        markerType=cv2.MARKER_CROSS, markerSize=24, thickness=2)
        cv2.putText(thumb, f"target yaw={target['yaw']:.1f} pitch={target['pitch']:.1f}",
                    (10, thumb_h - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)

    cv2.putText(thumb, f"players={len(players)} cluster={len(cluster) if cluster else 0} "
                        f"excluded={len(excluded or [])}",
                (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)

    cv2.imwrite(str(out_path), thumb)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def render_rectilinear_preview(equirect_frame, camera_yaw, camera_pitch, camera_fov,
                                players, cluster, out_path, out_w=1920, out_h=1080):
    """
    Render an actual rectilinear camera preview at (camera_yaw, camera_pitch,
    camera_fov) -- this is what the shot would actually look like, not an
    equirect-space plot. Overlays detections/cluster projected into this
    camera's own pixel space so it's visible whether the target framing is
    watchable, not just numerically plausible.

    Camera pitch is expected to be a FIXED venue value (see --venue-profile /
    --fixed-pitch), not derived from player detections -- player vertical
    position in frame varies mainly with distance from camera, so driving
    pitch from it tilts toward empty sky or turf as players move.
    """
    crop = extract_crop_frame(equirect_frame, camera_yaw, fov_deg=camera_fov,
                               out_w=out_w, out_h=out_h)

    f = (out_w / 2.0) / math.tan(math.radians(camera_fov / 2.0))
    cy_rad = math.radians(camera_yaw)
    cp_rad = math.radians(camera_pitch)

    def yaw_pitch_to_camera_px(yaw_deg, pitch_deg):
        """Project a global (yaw, pitch) into this camera's pixel space, or
        None if behind the camera / outside the frame."""
        y = math.radians(yaw_deg)
        p = math.radians(pitch_deg)
        wx, wy, wz = math.cos(p) * math.sin(y), math.sin(p), math.cos(p) * math.cos(y)
        # Rotate into camera space (undo yaw, then undo pitch)
        rx = math.cos(cy_rad) * wx - math.sin(cy_rad) * wz
        rz = math.sin(cy_rad) * wx + math.cos(cy_rad) * wz
        ry = wy
        ry2 = math.cos(cp_rad) * ry - math.sin(cp_rad) * rz
        rz2 = math.sin(cp_rad) * ry + math.cos(cp_rad) * rz
        if rz2 <= 0.05:
            return None
        px = out_w / 2.0 + rx * f / rz2
        py = out_h / 2.0 - ry2 * f / rz2
        if 0 <= px < out_w and 0 <= py < out_h:
            return int(px), int(py)
        return None

    cluster_set = {id(p) for p in cluster} if cluster else set()
    for p in players:
        pt = yaw_pitch_to_camera_px(p["yaw"], p["pitch"])
        if pt is not None:
            color = (0, 255, 0) if id(p) in cluster_set else (0, 165, 255)
            cv2.circle(crop, pt, 8, color, -1)

    cv2.putText(crop, f"camera yaw={camera_yaw:.1f} pitch={camera_pitch:.1f} fov={camera_fov:.0f}",
                (15, 35), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    cv2.putText(crop, f"players={len(players)} cluster={len(cluster) if cluster else 0}",
                (15, 65), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)

    cv2.imwrite(str(out_path), crop)



def parse_args():
    p = argparse.ArgumentParser(description="Phase 1 — play-location measurement layer")
    p.add_argument("--input", type=Path, help="Path to equirectangular input video")
    p.add_argument("--self-test", action="store_true",
                    help="Run deterministic round-trip projection test and exit "
                         "(no --input needed)")
    p.add_argument("--output", type=Path, default=Path("playcam/output/play_location.jsonl"))
    p.add_argument("--debug-dir", type=Path, default=Path("playcam/output/debug_frames"),
                    help="Equirect-space detection plots (debug output 1)")
    p.add_argument("--preview-dir", type=Path, default=Path("playcam/output/preview_frames"),
                    help="Rectilinear camera-shot previews (debug output 2)")
    p.add_argument("--venue-profile", type=Path, default=None,
                    help="JSON with {\"pitch\": ...} -- fixed camera pitch for preview "
                         "rendering. Player detections drive YAW only, never pitch. "
                         "See playcam/venue_profiles/. --fixed-pitch overrides this.")
    p.add_argument("--fixed-pitch", type=float, default=None,
                    help=f"Fixed camera pitch for preview rendering (default "
                         f"{DEFAULT_FIXED_PITCH} if no --venue-profile given)")
    p.add_argument("--preview-fov", type=float, default=85.0,
                    help="Diagonal FOV for the rectilinear preview (default 85)")
    p.add_argument("--fps", type=float, default=DEFAULT_SAMPLE_FPS,
                    help=f"Sample rate in frames/sec (default {DEFAULT_SAMPLE_FPS})")
    p.add_argument("--start", type=float, default=0.0, help="Start time in seconds")
    p.add_argument("--duration", type=float, default=None, help="Duration in seconds (default: full clip)")
    p.add_argument("--max-frames", type=int, default=DEFAULT_MAX_FRAMES,
                    help=f"Safety cap on sampled frames (default {DEFAULT_MAX_FRAMES})")
    p.add_argument("--model", default=YOLO_PERSON_WEIGHTS)
    p.add_argument("--no-debug", action="store_true", help="Skip both debug outputs")
    return p.parse_args()


def main():
    args = parse_args()

    if args.self_test:
        ok = run_self_test()
        sys.exit(0 if ok else 1)

    if args.input is None:
        print("ERROR: --input is required (unless using --self-test)", file=sys.stderr)
        sys.exit(1)
    if not args.input.exists():
        print(f"ERROR: input file does not exist: {args.input}", file=sys.stderr)
        sys.exit(1)
    if args.fps <= 0:
        print(f"ERROR: --fps must be > 0, got {args.fps}", file=sys.stderr)
        sys.exit(1)

    from ultralytics import YOLO
    model = YOLO(args.model)

    # Fixed camera pitch for preview rendering -- NEVER derived from player
    # detections. Priority: --fixed-pitch > --venue-profile > module default.
    # Also loads play_area (playcam-only; never touches ball_tracker/venue_mask.json).
    fixed_pitch = args.fixed_pitch
    play_area = None
    if args.venue_profile is not None:
        if not args.venue_profile.exists():
            print(f"ERROR: --venue-profile does not exist: {args.venue_profile}", file=sys.stderr)
            sys.exit(1)
        try:
            profile = json.loads(args.venue_profile.read_text())
        except json.JSONDecodeError as e:
            print(f"ERROR: --venue-profile is not valid JSON: {e}", file=sys.stderr)
            sys.exit(1)
        if fixed_pitch is None:
            if "pitch" not in profile:
                print(f"ERROR: --venue-profile has no 'pitch' key: {args.venue_profile}", file=sys.stderr)
                sys.exit(1)
            fixed_pitch = profile["pitch"]
        if "play_area" in profile:
            pa = profile["play_area"]
            if not all(k in pa for k in ("polygon", "frame_width", "frame_height")):
                print(f"ERROR: play_area missing polygon/frame_width/frame_height: "
                      f"{args.venue_profile}", file=sys.stderr)
                sys.exit(1)
            if len(pa["polygon"]) < 3:
                print(f"ERROR: play_area.polygon needs >= 3 points, got "
                      f"{len(pa['polygon'])}", file=sys.stderr)
                sys.exit(1)
            play_area = pa
    if fixed_pitch is None:
        fixed_pitch = DEFAULT_FIXED_PITCH

    if play_area is None:
        print("[play_location] WARNING: no play_area in venue profile -- "
              "detections are NOT masked to the pitch (will include neighbouring "
              "pitches, spectators, everyone visible). Run venue_calibration.py "
              "to add one.")

    cap = cv2.VideoCapture(str(args.input))
    if not cap.isOpened():
        print(f"ERROR: cannot open input: {args.input}", file=sys.stderr)
        sys.exit(1)

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    start_frame = int(args.start * src_fps)
    if args.duration is not None:
        end_frame = min(total_frames, start_frame + int(args.duration * src_fps))
    else:
        end_frame = total_frames

    step = max(1, round(src_fps / args.fps))
    sample_frames = list(range(start_frame, end_frame, step))[:args.max_frames]

    print(f"[play_location] input={args.input} src_fps={src_fps:.2f} "
          f"total_frames={total_frames}")
    print(f"[play_location] sampling {len(sample_frames)} frames "
          f"(every {step} frames, ~{args.fps} fps effective)")
    print(f"[play_location] fixed preview pitch={fixed_pitch} "
          f"(NOT derived from player detections) fov={args.preview_fov}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    if not args.no_debug:
        args.debug_dir.mkdir(parents=True, exist_ok=True)
        args.preview_dir.mkdir(parents=True, exist_ok=True)

    records = []
    if start_frame > 0:
        cap.set(cv2.CAP_PROP_POS_FRAMES, start_frame)  # one seek, not per-sample
    with open(args.output, "w") as out_f:
        sample_set = set(sample_frames)
        i = 0
        for fidx in range(start_frame, end_frame):
            ret, frame = cap.read()
            if not ret:
                print(f"  [warn] could not read frame {fidx}, stopping early")
                break
            if fidx not in sample_set:
                continue
            i += 1

            all_players = []
            for crop_yaw in CROP_YAWS_DEG:
                crop = extract_crop_frame(frame, crop_yaw)
                results = model(crop, conf=YOLO_CONF, imgsz=YOLO_IMGSZ,
                                 classes=[PERSON_CLASS_ID], verbose=False)
                for r in results:
                    if r.boxes is None:
                        continue
                    for box in r.boxes:
                        x1, y1, x2, y2 = box.xyxy[0].tolist()
                        conf_val = float(box.conf[0])
                        # Center point drives yaw/pitch used for clustering/camera.
                        yaw, pitch = crop_pixel_to_yaw_pitch((x1 + x2) / 2, (y1 + y2) / 2, crop_yaw)
                        if not (PITCH_MIN_DEG <= pitch <= PITCH_MAX_DEG):
                            continue
                        # Foot point (bbox bottom-center) drives play_area masking --
                        # more accurate ground position than the bbox center.
                        foot_yaw, foot_pitch = crop_pixel_to_yaw_pitch((x1 + x2) / 2, y2, crop_yaw)
                        all_players.append({
                            "yaw": round(yaw, 2),
                            "pitch": round(pitch, 2),
                            "conf": round(conf_val, 3),
                            "foot_yaw": round(foot_yaw, 2),
                            "foot_pitch": round(foot_pitch, 2),
                        })

            # Apply play_area mask (foot-point containment) BEFORE dedup,
            # clustering, motion scoring, or debug counts -- per spec.
            if play_area is not None:
                in_area, excluded = [], []
                for p in all_players:
                    if foot_point_in_play_area(p["foot_yaw"], p["foot_pitch"], play_area):
                        in_area.append(p)
                    else:
                        excluded.append(p)
                all_players = in_area
            else:
                excluded = []

            players = dedup_players(all_players)
            clusters = cluster_players(players)
            top_cluster = best_cluster(clusters)

            # NOTE (2026-07-02): this is currently the centroid of ALL visible
            # players filtered only by the min-cluster-size spatial radius --
            # with most/all players in frame in one loose group, it does not
            # yet reliably distinguish an ACTIVE cluster from a crowd shot.
            # Named person_centroid rather than "play_cluster" until density/
            # motion weighting (next step) makes that distinction real.
            person_centroid = None
            if top_cluster:
                cy_m, cp_m = spherical_centroid([(p["yaw"], p["pitch"]) for p in top_cluster])
                person_centroid = {"yaw": round(cy_m, 2), "pitch": round(cp_m, 2)}

            record = {
                "timestamp": round(fidx / src_fps, 3),
                "frame": fidx,
                "players": players,
                "excluded_count": len(excluded),
                "person_centroid_size": len(top_cluster) if top_cluster else 0,
                "person_centroid_yaw": person_centroid["yaw"] if person_centroid else None,
                "person_centroid_pitch": person_centroid["pitch"] if person_centroid else None,
            }
            records.append(record)
            out_f.write(json.dumps(record) + "\n")

            if not args.no_debug:
                debug_path = args.debug_dir / f"frame_{fidx:06d}.png"
                render_debug_frame(frame, players, top_cluster, person_centroid, debug_path,
                                    excluded=excluded)

                # Rectilinear preview: yaw from detections, pitch FIXED (never
                # from detections) -- this is the actual watchable-shot check.
                preview_yaw = person_centroid["yaw"] if person_centroid else 0.0
                preview_path = args.preview_dir / f"frame_{fidx:06d}.png"
                render_rectilinear_preview(frame, preview_yaw, fixed_pitch, args.preview_fov,
                                            players, top_cluster, preview_path)

            if i % 10 == 0 or i == len(sample_frames):
                t_str = (f"centroid yaw={person_centroid['yaw']:.1f} pitch={person_centroid['pitch']:.1f}"
                          if person_centroid else "no cluster")
                print(f"  [{i:3d}/{len(sample_frames)}] frame {fidx:5d} "
                      f"players={len(players):2d} excluded={len(excluded):2d} "
                      f"cluster={record['person_centroid_size']:2d} | {t_str}")

    cap.release()

    with_cluster = [r for r in records if r["person_centroid_yaw"] is not None]
    print(f"\n[play_location] Done. {len(records)} samples -> {args.output}")
    print(f"[play_location] Frames with a person_centroid: "
          f"{len(with_cluster)} / {len(records)}")
    if with_cluster:
        mean_yaw = yaw_mean([r["person_centroid_yaw"] for r in with_cluster])
        mean_pitch = sum(r["person_centroid_pitch"] for r in with_cluster) / len(with_cluster)
        print(f"[play_location] Mean person_centroid: "
              f"yaw={mean_yaw:.1f} pitch={mean_pitch:.1f}")
    if not args.no_debug:
        print(f"[play_location] Equirect debug frames written to: {args.debug_dir}")
        print(f"[play_location] Rectilinear preview frames written to: {args.preview_dir}")


if __name__ == "__main__":
    main()
