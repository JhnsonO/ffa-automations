#!/usr/bin/env python3
"""
FFA Offline Recovery Pipeline — Stage 0: Static False-Positive Sweep
=====================================================================
Standalone, first-cut implementation per docs/offline-recovery-pipeline.md §3.

Purpose
-------
Sample widely across a 60-minute clip with the *cheap* ball detector at a low
confidence floor, accumulate every candidate into a spherical (yaw, pitch)
histogram, and identify objects that recur at fixed spherical locations across
widely separated moments (fence, net, trees, lights, line markings).

Output is a PENALTY MAP — not hard exclusions. A fixed location may still
legitimately contain the ball occasionally (especially around goals), so this
first cut:
  * applies penalties only;
  * makes NO hard exclusions, even outside the presumed playable area;
  * requires NO playable-area polygons or goal-region setup;
  * preserves ALL detections so later fusion evidence can override the penalty.

Duty cycle is computed over UNIQUE SAMPLED TIMESTAMPS, never raw detection
count — we are identifying objects present across many separate moments.

Detector reuse
--------------
Uses the SAME cheap detector path as run_tracker.py (YOLO, 4 crop yaws,
CROP_FOV_DEG, low YOLO_CONF). Detections are written out per sampled frame so
Stage 1 can reuse them and never detect the same frames twice.

Defaults (signed off)
---------------------
  low_duty_floor       = 0.10
  duty_cycle_threshold = 0.60
  penalty_min          = 0.10

Penalty curve (monotonic, three regions):
  d < low_duty_floor                       -> weight = 1.0     (neutral)
  low_duty_floor <= d < duty_cycle_thresh  -> cosine taper 1.0 -> penalty_min
  d >= duty_cycle_threshold                -> weight = penalty_min

Usage
-----
  python3 ball_tracker/stage0_hotspot_sweep.py \\
      --input         render_work/equirect_trim.mp4 \\
      --output-dir    stage0_output \\
      --sample-interval-s 0.5 \\
      [--max-frames N]   # optional cap for quick tests
"""

import argparse
import json
import math
import os
import sys
import time

import cv2
import numpy as np

# ── Detector config — mirror run_tracker.py cheap path ────────────────────────
CROP_YAWS_DEG    = [0, 90, 180, 270]
CROP_FOV_DEG     = 110
CROP_W           = 1280
CROP_H           = 720
DEDUP_THRESH_DEG = 15
YOLO_CONF        = 0.12     # deliberately low floor — catch weak recurring FPs
YOLO_IMGSZ       = 1280
BALL_CLASS_ID    = 0

# ── Broad pitch bounds (from run_tracker.py v9 plausibility) ─────────────────
# Used ONLY for an informational in-bounds flag on each bin. NOT a hard filter
# in this first cut — all detections are preserved.
PITCH_SOFT_MIN = -30.0
PITCH_SOFT_MAX =  10.0
PITCH_HARD_MAX =  18.0

# ── Known venue reference hotspots (for test reporting) ──────────────────────
KNOWN_FENCE_YAW   = -77.4
KNOWN_FENCE_PITCH = -3.9
KNOWN_INTERMITTENT_YAW   = -37.0   # prior intermittent region (should be light/none)
KNOWN_INTERMITTENT_PITCH =  23.0

# ── Stage 0 signed-off defaults ──────────────────────────────────────────────
DEF_LOW_DUTY_FLOOR       = 0.10
DEF_DUTY_CYCLE_THRESHOLD = 0.60
DEF_PENALTY_MIN          = 0.10
DEF_SPHERE_BIN_DEG       = 2.0
DEF_SAMPLE_INTERVAL_S    = 0.5
DEF_MERGE_RADIUS_DEG     = 4.0   # cluster seed bins within this angular distance


# ─────────────────────────────────────────────────────────────────────────────
# Geometry — copied verbatim from run_tracker.py to guarantee identical math
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
    """Keep highest-conf detection within thresh_deg of each other. detections: (yaw,pitch,conf,crop_yaw)."""
    kept = []
    for det in sorted(detections, key=lambda d: -d[2]):
        yaw, pitch, conf = det[:3]
        if all(angular_distance(yaw, pitch, k[0], k[1]) > thresh_deg for k in kept):
            kept.append(det)
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Spherical binning
# ─────────────────────────────────────────────────────────────────────────────
def bin_id(yaw, pitch, bin_deg):
    """Map (yaw,pitch) to integer bin indices. Yaw wraps at ±180."""
    yaw_w = ((yaw + 180.0) % 360.0) - 180.0
    yb = int(math.floor((yaw_w + 180.0) / bin_deg))
    pb = int(math.floor((pitch + 90.0) / bin_deg))
    return (yb, pb)


def bin_centre(yb, pb, bin_deg):
    yaw = yb * bin_deg - 180.0 + bin_deg / 2.0
    pitch = pb * bin_deg - 90.0 + bin_deg / 2.0
    return yaw, pitch


# ─────────────────────────────────────────────────────────────────────────────
# Penalty curve
# ─────────────────────────────────────────────────────────────────────────────
def penalty_weight(duty_cycle, low_floor, threshold, penalty_min):
    """
    Monotonic decreasing weight in [penalty_min, 1.0].
    1.0 = neutral, penalty_min = strongest penalty (still NOT zero — penalty
    map only, no hard exclusion).
    """
    d = duty_cycle
    if d < low_floor:
        return 1.0
    if d >= threshold:
        return penalty_min
    # Cosine taper across [low_floor, threshold]
    frac = (d - low_floor) / (threshold - low_floor)
    return penalty_min + (1.0 - penalty_min) * 0.5 * (1.0 + math.cos(math.pi * frac))


# ─────────────────────────────────────────────────────────────────────────────
# Hotspot region clustering
#
# Per-bin duty cycle is computed first (unchanged). We then merge adjacent
# high-duty bins into spatial hotspot REGIONS so that the penalty applies by
# angular distance from a hotspot centre/radius rather than exact 2° bin
# membership. This catches detector jitter that spreads a single physical
# hotspot (e.g. the fence) across neighbouring bins like (-77,-3) and (-77,-5).
#
# Soft-penalty logic and the no-hard-exclusion rule are preserved: a bin's
# final penalty is the STRONGER (lower weight) of its own per-bin penalty and
# the region-distance penalty, and is always floored at penalty_min.
# ─────────────────────────────────────────────────────────────────────────────
def cluster_hotspot_regions(bins, low_floor, bin_deg, merge_radius_deg):
    """
    Merge adjacent/high-duty bins into hotspot regions.

    A bin is a "seed" if its own duty cycle is >= low_floor (i.e. it would
    receive any penalty at all). Seeds within merge_radius_deg of each other
    (spherical angular distance) are grouped by union-find into one region.

    Returns list of region dicts:
      { centre_yaw, centre_pitch, radius_deg, peak_duty,
        member_bins:[(yaw,pitch,duty),...], n_members }
    Centre = duty-weighted spherical mean of member bin centres.
    Radius = max angular distance from centre to any member bin centre,
             plus half a bin diagonal so the region covers the bin extent.
    Peak duty = max member duty.
    """
    seeds = [b for b in bins if b["duty_cycle"] >= low_floor]
    if not seeds:
        return []

    n = len(seeds)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    # Union seeds within merge_radius_deg
    for i in range(n):
        for j in range(i + 1, n):
            d = angular_distance(seeds[i]["yaw_centre"], seeds[i]["pitch_centre"],
                                 seeds[j]["yaw_centre"], seeds[j]["pitch_centre"])
            if d <= merge_radius_deg:
                union(i, j)

    # Gather groups
    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(seeds[i])

    bin_diag_half = (bin_deg * math.sqrt(2.0)) / 2.0

    regions = []
    for members in groups.values():
        # Duty-weighted spherical mean of centres (unit-vector mean to handle wrap)
        wsum = sum(m["duty_cycle"] for m in members) or 1.0
        vx = vy = vz = 0.0
        for m in members:
            w = m["duty_cycle"]
            la = math.radians(m["pitch_centre"])
            lo = math.radians(m["yaw_centre"])
            vx += w * math.cos(la) * math.cos(lo)
            vy += w * math.cos(la) * math.sin(lo)
            vz += w * math.sin(la)
        vx /= wsum; vy /= wsum; vz /= wsum
        norm = math.sqrt(vx*vx + vy*vy + vz*vz) or 1.0
        vx, vy, vz = vx/norm, vy/norm, vz/norm
        centre_pitch = math.degrees(math.asin(max(-1.0, min(1.0, vz))))
        centre_yaw   = math.degrees(math.atan2(vy, vx))

        radius = max(
            angular_distance(centre_yaw, centre_pitch, m["yaw_centre"], m["pitch_centre"])
            for m in members
        ) + bin_diag_half
        peak_duty = max(m["duty_cycle"] for m in members)

        regions.append({
            "centre_yaw":   round(centre_yaw, 3),
            "centre_pitch": round(centre_pitch, 3),
            "radius_deg":   round(radius, 3),
            "peak_duty":    round(peak_duty, 4),
            "n_members":    len(members),
            "member_bins":  [(m["yaw_centre"], m["pitch_centre"], m["duty_cycle"]) for m in members],
        })

    regions.sort(key=lambda r: -r["peak_duty"])
    return regions


def region_penalty_for_point(yaw, pitch, regions, low_floor, threshold, penalty_min):
    """
    Penalty contributed by hotspot regions at a spherical point, applied SMOOTHLY
    by angular distance from the nearest region centre.

    Inside a region's core radius: full penalty based on the region's peak duty.
    Beyond the core, the *effective duty* falls off linearly to zero across one
    extra merge band (radius -> radius + falloff_band), so nearby jitter bins
    just outside the core still receive a (decreasing) penalty.

    Returns a weight in [penalty_min, 1.0]; 1.0 = no region influence here.
    """
    best_w = 1.0
    for r in regions:
        d = angular_distance(yaw, pitch, r["centre_yaw"], r["centre_pitch"])
        core = r["radius_deg"]
        band = core  # falloff band width = one more core radius
        if d <= core:
            eff_duty = r["peak_duty"]
        elif d <= core + band:
            # Linear falloff of effective duty from peak -> 0 across the band
            frac = 1.0 - (d - core) / band
            eff_duty = r["peak_duty"] * frac
        else:
            continue
        w = penalty_weight(eff_duty, low_floor, threshold, penalty_min)
        if w < best_w:
            best_w = w
    return best_w


# ─────────────────────────────────────────────────────────────────────────────
# Detector
# ─────────────────────────────────────────────────────────────────────────────
def load_detector(weights_path):
    from ultralytics import YOLO
    model = YOLO(weights_path)
    return model


def detect_ball_candidates(model, equirect_frame):
    """Run cheap detector across 4 crop yaws, return deduped spherical candidates."""
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
                cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
                yaw, pitch = crop_pixel_to_yaw_pitch(cx, cy, crop_yaw, CROP_FOV_DEG, CROP_W, CROP_H)
                raw.append((yaw, pitch, conf, crop_yaw))
    return dedupe_detections(raw)


# ─────────────────────────────────────────────────────────────────────────────
# Main sweep
# ─────────────────────────────────────────────────────────────────────────────
def run_sweep(args):
    t0 = time.time()
    cap = cv2.VideoCapture(args.input)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open {args.input}")
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 29.97
    duration_s = total_frames / fps
    sample_stride = max(1, int(round(args.sample_interval_s * fps)))

    sampled_frames = list(range(0, total_frames, sample_stride))
    if args.max_frames:
        sampled_frames = sampled_frames[:args.max_frames]

    print(f"[stage0] Clip: {total_frames} frames @ {fps:.2f} fps  ({duration_s:.1f}s)")
    print(f"[stage0] Sample interval: {args.sample_interval_s}s  -> stride {sample_stride} frames")
    print(f"[stage0] Sampling {len(sampled_frames)} frames")
    print(f"[stage0] Sphere bin: {args.sphere_bin_deg}°  conf floor: {YOLO_CONF}")

    model = None
    if not args.dry_run:
        if not args.weights or not os.path.isfile(args.weights):
            raise RuntimeError(f"Detector weights not found: {args.weights}")
        print(f"[stage0] Loading detector: {args.weights}")
        model = load_detector(args.weights)

    # bin -> set of unique sampled timestamps (frame indices) that had a candidate here
    bin_timestamps = {}      # (yb,pb) -> set(frame_idx)
    bin_confs      = {}      # (yb,pb) -> list of confidences (for reporting)
    stage0_detections = {}   # frame_idx -> list of (yaw,pitch,conf,crop_yaw,bin_id)
    total_candidates = 0

    for n, frame_idx in enumerate(sampled_frames):
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            print(f"[stage0] WARN could not read frame {frame_idx}, skipping")
            continue

        if args.dry_run:
            cands = []   # no detection in dry-run
        else:
            cands = detect_ball_candidates(model, frame)

        per_frame = []
        seen_bins_this_ts = set()
        for (yaw, pitch, conf, crop_yaw) in cands:
            b = bin_id(yaw, pitch, args.sphere_bin_deg)
            per_frame.append((round(yaw, 3), round(pitch, 3), round(conf, 4), crop_yaw, list(b)))
            # Unique-timestamp duty cycle: count this bin once per timestamp
            if b not in seen_bins_this_ts:
                bin_timestamps.setdefault(b, set()).add(frame_idx)
                seen_bins_this_ts.add(b)
            bin_confs.setdefault(b, []).append(conf)
            total_candidates += 1

        stage0_detections[frame_idx] = per_frame

        if (n + 1) % 100 == 0:
            el = time.time() - t0
            print(f"[stage0] {n+1}/{len(sampled_frames)} frames  "
                  f"{total_candidates} candidates  {el:.1f}s elapsed")

    cap.release()

    # ── Build hotspot map (per-bin duty cycle — unchanged) ────────────────────
    n_unique_ts = len(sampled_frames)
    hotspot_bins = []
    for b, ts_set in bin_timestamps.items():
        duty = len(ts_set) / n_unique_ts if n_unique_ts else 0.0
        bin_w = penalty_weight(duty, args.low_duty_floor, args.duty_cycle_threshold, args.penalty_min)
        yaw_c, pitch_c = bin_centre(b[0], b[1], args.sphere_bin_deg)
        confs = bin_confs.get(b, [])
        in_bounds = (PITCH_SOFT_MIN <= pitch_c <= PITCH_HARD_MAX)
        hotspot_bins.append({
            "yaw_bin": b[0], "pitch_bin": b[1],
            "yaw_centre": round(yaw_c, 2), "pitch_centre": round(pitch_c, 2),
            "duty_cycle": round(duty, 4),
            "bin_penalty_weight": round(bin_w, 4),   # own-bin penalty (pre-clustering)
            "n_timestamps": len(ts_set),
            "n_detections": len(confs),
            "mean_conf": round(sum(confs) / len(confs), 4) if confs else 0.0,
            "in_pitch_bounds": in_bounds,
        })

    hotspot_bins.sort(key=lambda x: -x["duty_cycle"])

    # ── Cluster adjacent high-duty bins into hotspot regions ──────────────────
    merge_radius = args.merge_radius_deg
    regions = cluster_hotspot_regions(hotspot_bins, args.low_duty_floor,
                                      args.sphere_bin_deg, merge_radius)
    print(f"[stage0] Clustered {len(regions)} hotspot region(s) "
          f"(merge radius {merge_radius}°)")

    # ── Final per-bin penalty = stronger of own-bin and region-distance ───────
    # Region penalty applies SMOOTHLY by angular distance from region centres,
    # so jitter bins just outside a region core still receive a (lighter) penalty.
    # No hard exclusions; everything floored at penalty_min.
    for hb in hotspot_bins:
        rw = region_penalty_for_point(hb["yaw_centre"], hb["pitch_centre"], regions,
                                      args.low_duty_floor, args.duty_cycle_threshold,
                                      args.penalty_min)
        final_w = min(hb["bin_penalty_weight"], rw)
        hb["region_penalty_weight"] = round(rw, 4)
        hb["penalty_weight"] = round(max(args.penalty_min, final_w), 4)

    # ── Estimate later-stage candidate reduction (uses FINAL penalties) ───────
    # Reduction estimate = 1 - (sum of final penalty_weight over detections / total).
    bin_lookup = {(hb["yaw_bin"], hb["pitch_bin"]): hb for hb in hotspot_bins}
    weighted_kept = 0.0
    for b, confs in bin_confs.items():
        hb = bin_lookup.get(b)
        w = hb["penalty_weight"] if hb else 1.0
        weighted_kept += w * len(confs)
    est_reduction = (1.0 - weighted_kept / total_candidates) if total_candidates else 0.0

    # ── Write outputs ─────────────────────────────────────────────────────────
    os.makedirs(args.output_dir, exist_ok=True)

    hotspot_map = {
        "sphere_bin_deg": args.sphere_bin_deg,
        "low_duty_floor": args.low_duty_floor,
        "duty_cycle_threshold": args.duty_cycle_threshold,
        "penalty_min": args.penalty_min,
        "merge_radius_deg": merge_radius,
        "n_sampled_timestamps": n_unique_ts,
        "total_candidates": total_candidates,
        "hard_exclusions": False,
        "hotspot_regions": regions,
        "bins": hotspot_bins,
    }
    with open(os.path.join(args.output_dir, "hotspot_map.json"), "w") as f:
        json.dump(hotspot_map, f, indent=2)

    with open(os.path.join(args.output_dir, "stage0_detections.json"), "w") as f:
        json.dump({
            "fps": fps,
            "sample_stride": sample_stride,
            "frames": stage0_detections,
        }, f)

    with open(os.path.join(args.output_dir, "sweep_manifest.json"), "w") as f:
        json.dump({
            "input": os.path.basename(args.input),
            "total_frames": total_frames,
            "fps": fps,
            "sample_interval_s": args.sample_interval_s,
            "sample_stride": sample_stride,
            "sampled_frame_indices": sampled_frames,
            "config": {
                "sphere_bin_deg": args.sphere_bin_deg,
                "low_duty_floor": args.low_duty_floor,
                "duty_cycle_threshold": args.duty_cycle_threshold,
                "penalty_min": args.penalty_min,
                "yolo_conf": YOLO_CONF,
            },
        }, f, indent=2)

    # ── Render duty-cycle histogram image ─────────────────────────────────────
    render_histogram(hotspot_bins, regions, args)

    # ── Test report ───────────────────────────────────────────────────────────
    report = build_report(hotspot_bins, regions, n_unique_ts, total_candidates,
                           est_reduction, args)
    with open(os.path.join(args.output_dir, "stage0_report.txt"), "w") as f:
        f.write(report)
    print("\n" + report)

    print(f"\n[stage0] Done in {time.time()-t0:.1f}s. Outputs in {args.output_dir}/")


def render_histogram(hotspot_bins, regions, args):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Circle

    if not hotspot_bins:
        print("[stage0] No bins to plot")
        return

    yaws   = [b["yaw_centre"]   for b in hotspot_bins]
    pitches = [b["pitch_centre"] for b in hotspot_bins]
    duties = [b["duty_cycle"]   for b in hotspot_bins]

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(16, 12))
    fig.suptitle(f"Stage 0 — Static Hotspot Duty-Cycle Sweep\n"
                 f"{args.n_label}  |  bin={args.sphere_bin_deg}°  "
                 f"floor={args.low_duty_floor} thresh={args.duty_cycle_threshold} pmin={args.penalty_min}",
                 fontsize=13, fontweight="bold")

    # Panel 1: spherical scatter coloured by duty cycle
    sc = ax1.scatter(yaws, pitches, c=duties, cmap="hot_r", s=60,
                     vmin=0, vmax=1, edgecolors="grey", linewidths=0.3)
    ax1.axhline(0, color="black", lw=0.5)
    ax1.axhline(PITCH_SOFT_MIN, color="blue", ls=":", lw=0.8, alpha=0.5, label=f"pitch soft min {PITCH_SOFT_MIN}")
    ax1.axhline(PITCH_HARD_MAX, color="blue", ls=":", lw=0.8, alpha=0.5, label=f"pitch hard max {PITCH_HARD_MAX}")
    # Mark known references
    ax1.scatter([KNOWN_FENCE_YAW], [KNOWN_FENCE_PITCH], marker="X", s=200,
                edgecolors="lime", facecolors="none", linewidths=2.5,
                label=f"known fence ({KNOWN_FENCE_YAW},{KNOWN_FENCE_PITCH})")
    ax1.scatter([KNOWN_INTERMITTENT_YAW], [KNOWN_INTERMITTENT_PITCH], marker="o", s=200,
                edgecolors="cyan", facecolors="none", linewidths=2.5,
                label=f"prior intermittent ({KNOWN_INTERMITTENT_YAW},{KNOWN_INTERMITTENT_PITCH})")
    # Draw merged hotspot region circles (core radius + falloff band)
    for i, r in enumerate(regions):
        core = Circle((r["centre_yaw"], r["centre_pitch"]), r["radius_deg"],
                      fill=False, edgecolor="purple", lw=1.5, ls="-",
                      label="hotspot region (core)" if i == 0 else None)
        band = Circle((r["centre_yaw"], r["centre_pitch"]), r["radius_deg"] * 2,
                      fill=False, edgecolor="purple", lw=0.8, ls=":", alpha=0.6,
                      label="region falloff band" if i == 0 else None)
        ax1.add_patch(core)
        ax1.add_patch(band)
    ax1.set_xlabel("Yaw (°)"); ax1.set_ylabel("Pitch (°)")
    ax1.set_xlim(-180, 180); ax1.set_ylim(-90, 90)
    ax1.set_title("Spherical duty-cycle map (redder = more static)")
    ax1.legend(loc="upper right", fontsize=8)
    ax1.grid(True, alpha=0.3)
    plt.colorbar(sc, ax=ax1, label="duty cycle")

    # Panel 2: penalty curve + bin duty histogram
    ds = np.linspace(0, 1, 200)
    ws = [penalty_weight(d, args.low_duty_floor, args.duty_cycle_threshold, args.penalty_min) for d in ds]
    ax2.plot(ds, ws, color="red", lw=2, label="penalty curve")
    ax2.axvline(args.low_duty_floor, color="green", ls="--", alpha=0.6, label=f"low_duty_floor={args.low_duty_floor}")
    ax2.axvline(args.duty_cycle_threshold, color="purple", ls="--", alpha=0.6, label=f"threshold={args.duty_cycle_threshold}")
    ax2.axhline(args.penalty_min, color="grey", ls=":", alpha=0.6, label=f"penalty_min={args.penalty_min}")
    ax2b = ax2.twinx()
    ax2b.hist(duties, bins=40, range=(0, 1), alpha=0.3, color="steelblue")
    ax2b.set_ylabel("bin count", color="steelblue")
    ax2.set_xlabel("duty cycle"); ax2.set_ylabel("penalty weight")
    ax2.set_title("Penalty curve over duty cycle + distribution of bin duty cycles")
    ax2.legend(loc="center right", fontsize=8)
    ax2.grid(True, alpha=0.3); ax2.set_xlim(0, 1); ax2.set_ylim(0, 1.05)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    out = os.path.join(args.output_dir, "stage0_histogram.png")
    fig.savefig(out, dpi=140, bbox_inches="tight")
    print(f"[stage0] Histogram -> {out}")


def nearest_bin_to(hotspot_bins, yaw, pitch):
    """Find the reported bin nearest a reference (yaw,pitch)."""
    if not hotspot_bins:
        return None, None
    best = min(hotspot_bins,
               key=lambda b: angular_distance(yaw, pitch, b["yaw_centre"], b["pitch_centre"]))
    dist = angular_distance(yaw, pitch, best["yaw_centre"], best["pitch_centre"])
    return best, dist


def nearest_region_to(regions, yaw, pitch):
    """Find the hotspot region whose centre is nearest a reference (yaw,pitch)."""
    if not regions:
        return None, None
    best = min(regions,
               key=lambda r: angular_distance(yaw, pitch, r["centre_yaw"], r["centre_pitch"]))
    dist = angular_distance(yaw, pitch, best["centre_yaw"], best["centre_pitch"])
    return best, dist


def build_report(hotspot_bins, regions, n_ts, total_cands, est_reduction, args):
    lines = []
    lines.append("=" * 70)
    lines.append("STAGE 0 — STATIC FALSE-POSITIVE SWEEP — TEST REPORT")
    lines.append("=" * 70)
    lines.append(f"Sampled timestamps      : {n_ts}")
    lines.append(f"Total candidates        : {total_cands}")
    lines.append(f"Occupied bins           : {len(hotspot_bins)}")
    lines.append(f"Hotspot regions (merged): {len(regions)}  (merge radius {args.merge_radius_deg}°)")
    lines.append(f"Est. later-stage candidate reduction : {est_reduction*100:.1f}%")
    lines.append(f"  (1 - weighted-retained / total; FINAL region-aware penalties, no hard exclusions)")
    lines.append("")
    lines.append("-" * 70)
    lines.append("MERGED HOTSPOT REGIONS (by peak duty)")
    lines.append("-" * 70)
    lines.append(f"{'cen_yaw':>8} {'cen_pitch':>9} {'radius':>7} {'peak_duty':>9} {'#bins':>6}")
    for r in regions[:15]:
        lines.append(f"{r['centre_yaw']:>8.1f} {r['centre_pitch']:>9.1f} "
                     f"{r['radius_deg']:>7.2f} {r['peak_duty']:>9.3f} {r['n_members']:>6}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("TOP HOTSPOT BINS (by duty cycle; bin vs region vs FINAL penalty)")
    lines.append("-" * 70)
    lines.append(f"{'yaw':>8} {'pitch':>8} {'duty':>7} {'binP':>6} {'regP':>6} {'finP':>6} {'#ts':>5} {'inPB':>5}")
    for b in hotspot_bins[:20]:
        lines.append(f"{b['yaw_centre']:>8.1f} {b['pitch_centre']:>8.1f} "
                     f"{b['duty_cycle']:>7.3f} {b['bin_penalty_weight']:>6.3f} "
                     f"{b['region_penalty_weight']:>6.3f} {b['penalty_weight']:>6.3f} "
                     f"{b['n_timestamps']:>5} {str(b['in_pitch_bounds']):>5}")
    lines.append("")
    lines.append("-" * 70)
    lines.append("KNOWN-REFERENCE CHECKS (region-aware)")
    lines.append("-" * 70)

    # Fence: check the merged region it belongs to
    fence_region, fence_rdist = nearest_region_to(regions, KNOWN_FENCE_YAW, KNOWN_FENCE_PITCH)
    fence_bin, fence_bdist = nearest_bin_to(hotspot_bins, KNOWN_FENCE_YAW, KNOWN_FENCE_PITCH)
    if fence_region:
        strongly = (fence_bin and fence_bin["penalty_weight"] <= 0.3 and fence_bdist <= 5.0)
        lines.append(f"Known FENCE  ({KNOWN_FENCE_YAW}, {KNOWN_FENCE_PITCH}):")
        lines.append(f"  region centre ({fence_region['centre_yaw']}, {fence_region['centre_pitch']})  "
                     f"radius={fence_region['radius_deg']:.2f}°  peak_duty={fence_region['peak_duty']:.3f}  "
                     f"members={fence_region['n_members']}  centre_dist={fence_rdist:.1f}°")
        if fence_bin:
            lines.append(f"  nearest bin ({fence_bin['yaw_centre']}, {fence_bin['pitch_centre']})  "
                         f"final_penalty={fence_bin['penalty_weight']:.3f}")
        lines.append(f"  -> STRONGLY PENALISED: {'YES' if strongly else 'NO'} "
                     f"(want YES — final penalty<=0.3 within 5°)")
    else:
        lines.append(f"Known FENCE: no regions found")

    lines.append("")
    # -77,-5 jitter neighbour check: does it now receive a penalty via region falloff?
    nb_bin, nb_dist = nearest_bin_to(hotspot_bins, -77.0, -5.0)
    if nb_bin and nb_dist <= 2.0:
        receives = nb_bin["penalty_weight"] < 1.0
        lines.append(f"Jitter neighbour (-77, -5):")
        lines.append(f"  bin ({nb_bin['yaw_centre']}, {nb_bin['pitch_centre']})  "
                     f"own_duty={nb_bin['duty_cycle']:.3f}  bin_penalty={nb_bin['bin_penalty_weight']:.3f}  "
                     f"region_penalty={nb_bin['region_penalty_weight']:.3f}  FINAL={nb_bin['penalty_weight']:.3f}")
        lines.append(f"  -> RECEIVES PENALTY VIA REGION: {'YES' if receives else 'NO'} "
                     f"(want YES — final penalty<1.0 even if own duty is low)")
    else:
        lines.append(f"Jitter neighbour (-77, -5): no bin within 2° (no detections there this run)")

    lines.append("")
    inter_region, inter_rdist = nearest_region_to(regions, KNOWN_INTERMITTENT_YAW, KNOWN_INTERMITTENT_PITCH)
    inter_bin, inter_bdist = nearest_bin_to(hotspot_bins, KNOWN_INTERMITTENT_YAW, KNOWN_INTERMITTENT_PITCH)
    if inter_bin:
        light = inter_bin["penalty_weight"] >= 0.7 or inter_bdist > 5.0
        lines.append(f"Prior INTERMITTENT  ({KNOWN_INTERMITTENT_YAW}, {KNOWN_INTERMITTENT_PITCH}):")
        lines.append(f"  nearest bin ({inter_bin['yaw_centre']}, {inter_bin['pitch_centre']})  "
                     f"dist={inter_bdist:.1f}°  duty={inter_bin['duty_cycle']:.3f}  "
                     f"final_penalty={inter_bin['penalty_weight']:.3f}")
        if inter_region:
            lines.append(f"  nearest region centre ({inter_region['centre_yaw']}, {inter_region['centre_pitch']})  "
                         f"dist={inter_rdist:.1f}°")
        lines.append(f"  -> LIGHTLY/NOT PENALISED: {'YES' if light else 'NO'} "
                     f"(want YES — final penalty>=0.7 or no nearby bin)")
    else:
        lines.append(f"Prior INTERMITTENT: no bins found (counts as not penalised)")

    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Stage 0 static false-positive sweep")
    ap.add_argument("--input",      required=True, help="equirect MP4")
    ap.add_argument("--output-dir", default="stage0_output")
    ap.add_argument("--weights",    default=os.environ.get("BALL_WEIGHTS", ""),
                    help="YOLO ball detector weights (.pt)")
    ap.add_argument("--sample-interval-s", type=float, default=DEF_SAMPLE_INTERVAL_S)
    ap.add_argument("--sphere-bin-deg",    type=float, default=DEF_SPHERE_BIN_DEG)
    ap.add_argument("--merge-radius-deg",  type=float, default=DEF_MERGE_RADIUS_DEG,
                    help="angular distance for clustering seed bins into regions")
    ap.add_argument("--low-duty-floor",       type=float, default=DEF_LOW_DUTY_FLOOR)
    ap.add_argument("--duty-cycle-threshold", type=float, default=DEF_DUTY_CYCLE_THRESHOLD)
    ap.add_argument("--penalty-min",          type=float, default=DEF_PENALTY_MIN)
    ap.add_argument("--max-frames", type=int, default=None, help="cap sampled frames (quick test)")
    ap.add_argument("--dry-run", action="store_true", help="skip detection (pipeline/IO test only)")
    args = ap.parse_args()

    args.n_label = f"{os.path.basename(args.input)}"
    run_sweep(args)


if __name__ == "__main__":
    main()
