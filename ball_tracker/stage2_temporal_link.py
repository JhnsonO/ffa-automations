#!/usr/bin/env python3
"""
FFA Offline Recovery Pipeline — Stage 2: Temporal Linking
=========================================================
Per docs/offline-recovery-pipeline.md §5.

Purpose
-------
Link Stage 1 candidates across frames into tracklets, classify each tracklet,
compute anchor strength, and emit gap records for Stage 3.

Inputs
------
  --stage1-candidates : stage1_candidates.json
  --hotspot-map       : hotspot_map.json
  --output-dir        : output directory

Outputs
-------
  tracklets.json   — all tracklets with status, scores, alternates
  gaps.json        — gap records between/before/after anchors
"""

import argparse
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

# ── Defaults ──────────────────────────────────────────────────────────────────
BASE_TOLERANCE_DEG       = 5.0
MAX_SPEED_DEG_PER_FRAME  = 8.0
MAX_LINK_GAP             = 5
MIN_SUPPORT_CONF         = 0.10
MIN_ANCHOR_STRENGTH      = 0.55
MIN_OBS_FOR_SCORE        = 4
MIN_OBS_FOR_ANCHOR       = 8
MIN_MEAN_CONF_FOR_ANCHOR = 0.20
MIN_COVERAGE_FOR_ANCHOR  = 0.50
ALTERNATE_MARGIN         = 0.15
STATIC_HOTSPOT_DUTY      = 0.6   # peak_duty threshold for "confirmed static"

W_CONF = 0.4
W_PROX = 0.35
W_VEL  = 0.25


# ── Geometry ──────────────────────────────────────────────────────────────────

def to_unit_vec(yaw_deg, pitch_deg):
    y = math.radians(yaw_deg)
    p = math.radians(pitch_deg)
    return np.array([
        math.cos(p) * math.sin(y),
        math.sin(p),
        math.cos(p) * math.cos(y),
    ])


def great_circle_deg(v1, v2):
    dot = float(np.clip(np.dot(v1, v2), -1.0, 1.0))
    return math.degrees(math.acos(dot))


def link_gate(delta_frames):
    return BASE_TOLERANCE_DEG + MAX_SPEED_DEG_PER_FRAME * delta_frames


def tangent_plane_vec(v_prev, v_curr, delta_frames):
    """Project motion onto tangent plane of v_prev; return unit vec (or zero)."""
    diff = v_curr - v_prev
    proj = diff - np.dot(diff, v_prev) * v_prev
    n = np.linalg.norm(proj)
    if n < 1e-9:
        return np.zeros(3)
    return proj / n


def cosine_sim(a, b):
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0))


# ── Hotspot helpers ───────────────────────────────────────────────────────────

def build_static_regions(hotspot_map):
    """Return list of confirmed-static regions from hotspot_map.json.

    Real schema (Stage 0 output):
        hotspot_map["hotspot_regions"] = [
            {"centre_yaw": ..., "centre_pitch": ..., "radius_deg": ..., "peak_duty": ...},
        ]
    The duty_cycle_threshold stored in the map is used as the confirmed-static
    threshold; falls back to STATIC_HOTSPOT_DUTY constant if absent.
    """
    threshold = hotspot_map.get("duty_cycle_threshold", STATIC_HOTSPOT_DUTY)
    regions = []
    for entry in hotspot_map.get("hotspot_regions", []):
        if entry.get("peak_duty", 0) >= threshold:
            label = f"({entry['centre_yaw']:.1f},{entry['centre_pitch']:.1f})"
            regions.append({
                "name":   label,
                "vec":    to_unit_vec(entry["centre_yaw"], entry["centre_pitch"]),
                "radius": entry.get("radius_deg", 5.0),
            })
    return regions


def in_static_region(vec, static_regions):
    for r in static_regions:
        if great_circle_deg(vec, r["vec"]) <= r["radius"]:
            return r["name"]
    return None


# ── Association score ─────────────────────────────────────────────────────────

def association_score(cand_vec, predicted_vec, predicted_vel_vec,
                      weighted_conf, delta_frames, has_velocity):
    gate = link_gate(delta_frames)
    residual = great_circle_deg(cand_vec, predicted_vec)
    if residual > gate:
        return None  # outside gate — not linkable

    norm_conf = min(1.0, weighted_conf / 0.60)
    prox = math.exp(-0.5 * (residual / (gate / 2.0)) ** 2)

    if has_velocity and np.linalg.norm(predicted_vel_vec) > 1e-9:
        # implied velocity direction from predicted → candidate
        implied = cand_vec - predicted_vec
        pi = implied - np.dot(implied, predicted_vec) * predicted_vec
        vel_score = max(0.0, cosine_sim(pi, predicted_vel_vec))
        return W_CONF * norm_conf + W_PROX * prox + W_VEL * vel_score
    else:
        # redistribute w_vel proportionally
        total_w = W_CONF + W_PROX
        wc = W_CONF / total_w
        wp = W_PROX / total_w
        return wc * norm_conf + wp * prox


# ── Tracklet class ────────────────────────────────────────────────────────────

_tracklet_counter = 0

def _new_id():
    global _tracklet_counter
    _tracklet_counter += 1
    return f"T{_tracklet_counter:04d}"


class Tracklet:
    def __init__(self, seed_frame, seed_cand):
        self.id = _new_id()
        self.start_frame = seed_frame
        self.end_frame   = seed_frame
        self.frames      = {}   # frame_idx -> {vec, weighted_conf, score, alternates}
        self.missed_run  = 0
        self.closed      = False

        vec = to_unit_vec(seed_cand["yaw"], seed_cand["pitch"])
        self.frames[seed_frame] = {
            "vec":          vec,
            "weighted_conf": seed_cand["weighted_conf"],
            "score":        None,  # seed has no association score
            "alternates":   [],
        }
        self._prev_vec = vec
        self._prev_vel = np.zeros(3)
        self._has_vel  = False

    def predict(self, delta_frames=1):
        """Extrapolate position using last known velocity (deg/frame * direction)."""
        if self._has_vel:
            speed = float(np.linalg.norm(self._prev_vel))  # deg/frame
            if speed > 1e-9:
                direction = self._prev_vel / speed
                # Angular displacement in radians
                angle_rad = math.radians(speed * delta_frames)
                # Rotate _prev_vec by angle_rad around axis perpendicular to both
                # Using Rodrigues: v' = v*cos(a) + (k×v)*sin(a) + k*(k·v)*(1-cos(a))
                # where k = direction (tangent plane, orthogonal to _prev_vec)
                v = self._prev_vec
                k = direction
                cos_a = math.cos(angle_rad)
                sin_a = math.sin(angle_rad)
                predicted = v * cos_a + np.cross(k, v) * sin_a + k * np.dot(k, v) * (1 - cos_a)
                n = np.linalg.norm(predicted)
                if n > 1e-9:
                    return predicted / n
        return self._prev_vec.copy()

    def link(self, frame_idx, cand_vec, weighted_conf, score, alternates):
        self.end_frame = frame_idx
        prev_frame = max(self.frames.keys())
        delta = frame_idx - prev_frame
        # Angular speed (deg/frame) * tangent direction = velocity vector
        angular_dist = great_circle_deg(self._prev_vec, cand_vec)
        dir_vec = tangent_plane_vec(self._prev_vec, cand_vec, delta)
        speed = angular_dist / delta if delta > 0 else 0.0
        self._prev_vel = dir_vec * speed  # deg/frame in tangent plane
        self._prev_vec = cand_vec
        self._has_vel  = True
        self.missed_run = 0
        self.frames[frame_idx] = {
            "vec":           cand_vec,
            "weighted_conf": weighted_conf,
            "score":         score,
            "alternates":    alternates,
        }

    def tick_miss(self):
        self.missed_run += 1
        if self.missed_run >= MAX_LINK_GAP:
            self.closed = True

    def finalise(self, static_regions):
        obs_frames    = sorted(self.frames.keys())
        span          = self.end_frame - self.start_frame + 1
        obs_count     = len(obs_frames)
        cov_ratio     = obs_count / span if span > 0 else 0.0

        # max internal gap
        gaps = []
        for i in range(1, len(obs_frames)):
            g = obs_frames[i] - obs_frames[i-1] - 1
            if g > 0:
                gaps.append(g)
        max_gap = max(gaps) if gaps else 0

        confs       = [self.frames[f]["weighted_conf"] for f in obs_frames]
        scores      = [self.frames[f]["score"] for f in obs_frames if self.frames[f]["score"] is not None]
        mean_conf   = float(np.mean(confs)) if confs else 0.0
        mean_score  = float(np.mean(scores)) if scores else 0.0

        vecs = [self.frames[f]["vec"] for f in obs_frames]
        # prediction residuals (skip first obs — no prediction)
        residuals = []
        vel_sims  = []
        prev_vec  = None
        prev_vel  = np.zeros(3)  # speed * direction (same as Tracklet._prev_vel)
        has_v     = False
        for i, f in enumerate(obs_frames):
            vec = self.frames[f]["vec"]
            if prev_vec is not None:
                delta = obs_frames[i] - obs_frames[i-1]
                # Rodrigues spherical prediction — identical to Tracklet.predict()
                predicted = prev_vec.copy()
                if has_v:
                    speed = float(np.linalg.norm(prev_vel))
                    if speed > 1e-9:
                        direction = prev_vel / speed
                        angle_rad = math.radians(speed * delta)
                        v, k = prev_vec, direction
                        cos_a, sin_a = math.cos(angle_rad), math.sin(angle_rad)
                        pred2 = v * cos_a + np.cross(k, v) * sin_a + k * np.dot(k, v) * (1 - cos_a)
                        n = np.linalg.norm(pred2)
                        if n > 1e-9:
                            predicted = pred2 / n
                residuals.append(great_circle_deg(predicted, vec))
                # Update velocity: angular_speed * tangent_direction
                ang_dist = great_circle_deg(prev_vec, vec)
                dir_vec  = tangent_plane_vec(prev_vec, vec, delta)
                speed_new = ang_dist / delta if delta > 0 else 0.0
                vel_vec   = dir_vec * speed_new
                if has_v and np.linalg.norm(prev_vel) > 1e-9:
                    vel_sims.append(max(0.0, cosine_sim(prev_vel, vel_vec)))
                prev_vel = vel_vec
                has_v    = True
            else:
                prev_vel = np.zeros(3)
                has_v    = False
            prev_vec = vec

        mean_residual = float(np.mean(residuals)) if residuals else 0.0
        vel_consistency = float(np.mean(vel_sims)) if vel_sims else 0.0

        # spatial spread
        if len(vecs) > 1:
            centroid = np.mean(vecs, axis=0)
            n = np.linalg.norm(centroid)
            if n > 1e-9:
                centroid /= n
            spreads = [great_circle_deg(v, centroid) for v in vecs]
            spatial_spread = float(np.mean(spreads))
        else:
            spatial_spread = 0.0

        # net displacement
        net_disp = great_circle_deg(vecs[0], vecs[-1]) if len(vecs) > 1 else 0.0

        # mean velocity
        total_angle = sum(great_circle_deg(vecs[i], vecs[i+1]) for i in range(len(vecs)-1))
        total_span  = (obs_frames[-1] - obs_frames[0]) if len(obs_frames) > 1 else 1
        mean_vel    = total_angle / total_span if total_span > 0 else 0.0

        # static hotspot membership
        sh_count = sum(1 for v in vecs if in_static_region(v, static_regions) is not None)
        sh_frac  = sh_count / obs_count if obs_count > 0 else 0.0

        # static rejection rule
        cond_disp   = net_disp < 2.0
        cond_spread = spatial_spread < 1.5
        cond_span   = (span >= 20 and cov_ratio >= 0.6)
        secondary   = sum([cond_disp, cond_spread, cond_span])

        rejected_static  = False
        static_suspect   = False
        rejection_reason = None

        if sh_frac >= 0.7 and secondary >= 2:
            rejected_static  = True
            rejection_reason = "static_hotspot_tracklet_rejected"
        elif sh_frac >= 0.5 and secondary >= 1:
            static_suspect   = True

        # motion bonus
        if net_disp >= 3.0 and mean_vel >= 0.1:
            motion_bonus = 1.0
        elif net_disp >= 3.0 or mean_vel >= 0.1:
            motion_bonus = 0.5
        else:
            motion_bonus = 0.0

        # anchor strength (compute for all scorable incl. static_suspect)
        anchor_str = None
        if obs_count >= MIN_OBS_FOR_SCORE and not rejected_static:
            anchor_str = (
                0.30 * min(1.0, mean_conf / 0.60)
              + 0.20 * min(1.0, cov_ratio)
              + 0.15 * min(1.0, obs_count / 30)
              + 0.15 * (1 - min(1.0, mean_residual / 15.0))
              + 0.10 * vel_consistency
              + 0.10 * motion_bonus
            )
            if static_suspect:
                anchor_str = min(anchor_str, 0.40)

        # best_available_score (for non-anchor tracklets)
        best_avail = None
        if obs_count >= MIN_OBS_FOR_SCORE and not rejected_static:
            best_avail = (
                0.35 * min(1.0, mean_conf / 0.60)
              + 0.25 * cov_ratio
              + 0.20 * min(1.0, 1 - mean_residual / 15.0)
              + 0.20 * vel_consistency
            )

        # status
        if rejected_static:
            status = "rejected_static"
        elif obs_count < MIN_OBS_FOR_SCORE:
            status = "fragment"
        else:
            eligible = (
                not rejected_static
                and obs_count >= MIN_OBS_FOR_ANCHOR
                and mean_conf >= MIN_MEAN_CONF_FOR_ANCHOR
                and cov_ratio >= MIN_COVERAGE_FOR_ANCHOR
            )
            if eligible and anchor_str is not None and anchor_str >= MIN_ANCHOR_STRENGTH:
                status = "anchor"
            else:
                status = "passing"

        # build frame list for output
        frame_list = []
        for f in obs_frames:
            fd = self.frames[f]
            frame_list.append({
                "frame":         f,
                "yaw":           math.degrees(math.atan2(fd["vec"][0], fd["vec"][2])),
                "pitch":         math.degrees(math.asin(float(np.clip(fd["vec"][1], -1, 1)))),
                "weighted_conf": fd["weighted_conf"],
                "score":         fd["score"],
                "alternates":    fd["alternates"],
            })

        self._summary = {
            "id":                        self.id,
            "status":                    status,
            "rejection_reason":          rejection_reason,
            "static_suspect":            static_suspect,
            "start_frame":               self.start_frame,
            "end_frame":                 self.end_frame,
            "span_frames":               span,
            "observation_count":         obs_count,
            "coverage_ratio":            round(cov_ratio, 4),
            "max_internal_gap":          max_gap,
            "mean_weighted_conf":        round(mean_conf, 4),
            "mean_prediction_residual":  round(mean_residual, 4),
            "velocity_consistency":      round(vel_consistency, 4),
            "net_displacement_deg":      round(net_disp, 4),
            "spatial_spread_deg":        round(spatial_spread, 4),
            "mean_velocity_deg_per_frame": round(mean_vel, 6),
            "confirmed_static_hotspot_frac": round(sh_frac, 4),
            "anchor_strength_candidate": round(anchor_str, 4) if anchor_str is not None else None,
            "best_available_score":      round(best_avail, 4) if best_avail is not None else None,
            "frames":                    frame_list,
        }
        return self._summary


# ── Gap builder ───────────────────────────────────────────────────────────────

def _gap_reason(frame_range, all_cands_by_frame, active_tracklets_in_range,
                static_regions):
    """Determine gap_reason for a frame range (inclusive)."""
    support_frames = 0
    reserve_frames = 0
    reserve_static = 0
    reserve_total  = 0
    dominant_region = None

    region_counts = defaultdict(int)

    for f in frame_range:
        cands = all_cands_by_frame.get(f, [])
        has_support = any(c["weighted_conf"] >= MIN_SUPPORT_CONF for c in cands)
        has_reserve = any(c["weighted_conf"] <  MIN_SUPPORT_CONF for c in cands)
        if has_support:
            support_frames += 1
        if has_reserve:
            reserve_frames += 1
        for c in cands:
            if c["weighted_conf"] < MIN_SUPPORT_CONF:
                reserve_total += 1
                vec = to_unit_vec(c["yaw"], c["pitch"])
                rname = in_static_region(vec, static_regions)
                if rname:
                    reserve_static += 1
                    region_counts[rname] += 1

    dom_reserve_static = (reserve_total >= 3 and
                          reserve_static / reserve_total > 0.60) if reserve_total > 0 else False
    if dom_reserve_static and region_counts:
        dominant_region = max(region_counts, key=region_counts.get)

    has_no_cands   = support_frames == 0 and reserve_total == 0
    reserve_only   = support_frames == 0 and reserve_total > 0
    fragment_only  = False  # determined by caller using tracklet statuses
    ambiguous      = len(active_tracklets_in_range) > 1
    insufficient   = len(active_tracklets_in_range) == 1

    # gap_reason precedence
    # check if any rejected_static tracklet present
    has_rejected = any(t.get("status") == "rejected_static" for t in active_tracklets_in_range)
    has_fragment  = all(t.get("status") == "fragment" for t in active_tracklets_in_range) if active_tracklets_in_range else False

    if has_rejected:
        reason = "static_hotspot_tracklet_rejected"
    elif ambiguous and not has_rejected:
        reason = "ambiguous_competing_candidates"
    elif insufficient:
        reason = "insufficient_tracklet_score"
    elif has_fragment:
        reason = "fragment_only"
    elif reserve_only:
        reason = "reserve_only"
    elif has_no_cands:
        reason = "no_candidates_after_stage1"
    else:
        reason = "reserve_only" if reserve_total > 0 else "no_candidates_after_stage1"

    return reason, support_frames, reserve_total, dom_reserve_static, dominant_region


# ── Main pipeline ─────────────────────────────────────────────────────────────

def run(args):
    with open(args.stage1_candidates) as f:
        stage1 = json.load(f)
    with open(args.hotspot_map) as f:
        hmap = json.load(f)

    static_regions = build_static_regions(hmap)

    # Index candidates by frame
    # Stage 1 real schema: frames is a dict keyed by string frame number
    # e.g. {"0": [...], "1": [...]}
    # Each value is directly a list of candidate dicts (not wrapped in "candidates")
    frames_raw = stage1.get("frames", {})
    if isinstance(frames_raw, dict):
        all_cands_by_frame = {
            int(k): v for k, v in frames_raw.items()
        }
    else:
        # Fallback: list of {"frame": N, "candidates": [...]} records (fixture format)
        all_cands_by_frame = {}
        for entry in frames_raw:
            fidx = entry["frame"]
            all_cands_by_frame[fidx] = entry.get("candidates", [])

    all_frames = sorted(all_cands_by_frame.keys())
    if not all_frames:
        print("No frames found in stage1_candidates.json", file=sys.stderr)
        sys.exit(1)

    # ── Linking pass ──────────────────────────────────────────────────────────
    active:  list[Tracklet] = []
    closed:  list[Tracklet] = []

    global _tracklet_counter
    _tracklet_counter = 0

    for fidx in all_frames:
        cands = all_cands_by_frame[fidx]
        support = [c for c in cands if c["weighted_conf"] >= MIN_SUPPORT_CONF]

        # Predict positions for each active tracklet
        predictions = {}
        for t in active:
            delta = fidx - t.end_frame
            predictions[t.id] = (t.predict(delta), delta)

        # Build all valid links: (score, cand_idx, tracklet_id)
        valid_links = []
        for ci, cand in enumerate(support):
            vec = to_unit_vec(cand["yaw"], cand["pitch"])
            for t in active:
                pred_vec, delta = predictions[t.id]
                gate = link_gate(delta)
                if great_circle_deg(vec, pred_vec) > gate:
                    continue
                sc = association_score(
                    vec, pred_vec, t._prev_vel,
                    cand["weighted_conf"], delta, t._has_vel
                )
                if sc is not None:
                    valid_links.append((sc, ci, t.id))

        # Greedy assignment — best score first
        valid_links.sort(key=lambda x: -x[0])
        used_cands = set()
        used_tracklets = set()
        assignments = {}  # tracklet_id -> (cand_idx, score)

        for sc, ci, tid in valid_links:
            if ci in used_cands or tid in used_tracklets:
                continue
            assignments[tid] = (ci, sc)
            used_cands.add(ci)
            used_tracklets.add(tid)

        # Collect alternates per tracklet
        alternates_per_tracklet = defaultdict(list)
        if assignments:
            top_scores = {tid: sc for tid, (_, sc) in assignments.items()}
            for sc, ci, tid in valid_links:
                if tid in assignments:
                    top_sc = top_scores[tid]
                    if ci != assignments[tid][0] and top_sc - sc <= ALTERNATE_MARGIN:
                        cand = support[ci]
                        alternates_per_tracklet[tid].append({
                            "yaw":   cand["yaw"],
                            "pitch": cand["pitch"],
                            "score": round(sc, 4),
                        })

        # Apply assignments
        matched_tracklet_ids = set()
        for tid, (ci, sc) in assignments.items():
            cand = support[ci]
            vec  = to_unit_vec(cand["yaw"], cand["pitch"])
            t    = next(x for x in active if x.id == tid)
            t.link(fidx, vec, cand["weighted_conf"], sc, alternates_per_tracklet[tid])
            matched_tracklet_ids.add(tid)

        # Tick misses for unmatched active tracklets
        newly_closed = []
        for t in active:
            if t.id not in matched_tracklet_ids:
                t.tick_miss()
                if t.closed:
                    newly_closed.append(t)

        for t in newly_closed:
            active.remove(t)
            closed.append(t)

        # Seed new tracklets from unmatched support candidates
        for ci, cand in enumerate(support):
            if ci not in used_cands:
                nt = Tracklet(fidx, cand)
                active.append(nt)

    # Close remaining active tracklets
    for t in active:
        closed.append(t)

    # ── Finalise all tracklets ────────────────────────────────────────────────
    all_tracklets = [t.finalise(static_regions) for t in closed]

    # ── Gap detection ─────────────────────────────────────────────────────────
    anchor_tracklets = [t for t in all_tracklets if t["status"] == "anchor"]
    anchor_tracklets.sort(key=lambda t: t["start_frame"])

    gaps = []
    full_range_min = all_frames[0]
    full_range_max = all_frames[-1]

    def make_gap(start, end, gtype, pre_id, post_id, all_t_in_range):
        if end < start:
            return
        frame_range = range(start, end + 1)
        reason, sup_cov, res_count, dom_res_static, dom_region = _gap_reason(
            frame_range, all_cands_by_frame, all_t_in_range, static_regions
        )
        gaps.append({
            "start_frame":                 start,
            "end_frame":                   end,
            "span_frames":                 end - start + 1,
            "support_coverage_frames":     sup_cov,
            "gap_reason":                  reason,
            "gap_type":                    gtype,
            "pre_anchor_id":               pre_id,
            "post_anchor_id":              post_id,
            "reserve_candidates_present":  res_count > 0,
            "reserve_candidate_count":     res_count,
            "dominant_reserve_static_hotspot": dom_res_static,
            "dominant_reserve_region":     dom_region,
            "competing_tracklet_count":    len(all_t_in_range),
            "best_available_score":        max(
                (t["best_available_score"] for t in all_t_in_range
                 if t.get("best_available_score") is not None),
                default=None
            ),
        })

    def tracklets_in_range(start, end):
        return [t for t in all_tracklets
                if t["start_frame"] <= end and t["end_frame"] >= start
                and t["status"] != "anchor"]

    if not anchor_tracklets:
        make_gap(full_range_min, full_range_max,
                 "before_first_anchor", None, None,
                 tracklets_in_range(full_range_min, full_range_max))
    else:
        # before first anchor
        if anchor_tracklets[0]["start_frame"] > full_range_min:
            make_gap(full_range_min,
                     anchor_tracklets[0]["start_frame"] - 1,
                     "before_first_anchor", None, anchor_tracklets[0]["id"],
                     tracklets_in_range(full_range_min, anchor_tracklets[0]["start_frame"] - 1))

        # between anchors
        for i in range(len(anchor_tracklets) - 1):
            a1 = anchor_tracklets[i]
            a2 = anchor_tracklets[i + 1]
            gap_start = a1["end_frame"] + 1
            gap_end   = a2["start_frame"] - 1
            if gap_start <= gap_end:
                make_gap(gap_start, gap_end,
                         "between_anchors", a1["id"], a2["id"],
                         tracklets_in_range(gap_start, gap_end))

        # after last anchor
        if anchor_tracklets[-1]["end_frame"] < full_range_max:
            make_gap(anchor_tracklets[-1]["end_frame"] + 1,
                     full_range_max,
                     "after_last_anchor", anchor_tracklets[-1]["id"], None,
                     tracklets_in_range(anchor_tracklets[-1]["end_frame"] + 1, full_range_max))

    # ── Write outputs ─────────────────────────────────────────────────────────
    os.makedirs(args.output_dir, exist_ok=True)

    tracklets_path = os.path.join(args.output_dir, "tracklets.json")
    gaps_path      = os.path.join(args.output_dir, "gaps.json")

    with open(tracklets_path, "w") as f:
        json.dump({"tracklets": all_tracklets}, f, indent=2)

    with open(gaps_path, "w") as f:
        json.dump({"gaps": gaps}, f, indent=2)

    # Summary
    status_counts = defaultdict(int)
    for t in all_tracklets:
        status_counts[t["status"]] += 1

    print(f"Stage 2 complete.")
    print(f"  Tracklets: {len(all_tracklets)} total — " +
          ", ".join(f"{v} {k}" for k, v in sorted(status_counts.items())))
    print(f"  Gaps: {len(gaps)}")
    print(f"  Output: {tracklets_path}, {gaps_path}")


def main():
    global MIN_SUPPORT_CONF, MAX_LINK_GAP, BASE_TOLERANCE_DEG, MAX_SPEED_DEG_PER_FRAME, MIN_ANCHOR_STRENGTH
    p = argparse.ArgumentParser(description="FFA Stage 2: Temporal Linking")
    p.add_argument("--stage1-candidates", required=True)
    p.add_argument("--hotspot-map",       required=True)
    p.add_argument("--output-dir",        default="stage2_output")
    p.add_argument("--min-support-conf",  type=float, default=MIN_SUPPORT_CONF)
    p.add_argument("--max-link-gap",      type=int,   default=MAX_LINK_GAP)
    p.add_argument("--base-tolerance",    type=float, default=BASE_TOLERANCE_DEG)
    p.add_argument("--max-speed",         type=float, default=MAX_SPEED_DEG_PER_FRAME)
    p.add_argument("--min-anchor-str",    type=float, default=MIN_ANCHOR_STRENGTH)
    args = p.parse_args()

    MIN_SUPPORT_CONF        = args.min_support_conf
    MAX_LINK_GAP            = args.max_link_gap
    BASE_TOLERANCE_DEG      = args.base_tolerance
    MAX_SPEED_DEG_PER_FRAME = args.max_speed
    MIN_ANCHOR_STRENGTH     = args.min_anchor_str

    run(args)


if __name__ == "__main__":
    main()
