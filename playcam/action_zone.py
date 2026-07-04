#!/usr/bin/env python3
"""
action_zone.py — Playcam Phase 3A offline heuristic scorer (design-gated, no rendering)

Computes an alternative camera-target signal (`action_zone_yaw`) alongside the
existing frozen `person_centroid_yaw` signal, using only per-player detections
already produced by Phase 1 (`play_location.jsonl`). Analysis-only:

  - Does NOT modify play_location.py, wide_safety_camera.py, crop_utils.py, or
    any ball_tracker/ file.
  - Does NOT render video or dispatch any workflow.
  - Runs entirely on CPU against existing artifacts (zero paid compute).

Two signals are reported per frame, matching design doc §B:
  - action_zone_yaw: the raw candidate signal (motion-weighted zone mean).
    Always computed and reported as-is, even in gated frames, so 3B tuning
    can see what the scorer would have suggested throughout.
  - target_yaw: centroid_yaw + a confidence-weighted bias toward
    action_zone_yaw, hard-clamped to +/-BIAS_MAX_DEG (15 deg, design doc SS B).
    This is the value that would actually drive the camera.

Behavioural guarantees (per Phase 3 design gate), applied to target_yaw only:
  - confidence < CONF_FLOOR        -> target_yaw collapses EXACTLY to centroid_yaw
                                       (reason_code=low_confidence_fallback)
  - mode == "wide" (if a wide_safety_timeline.jsonl is supplied)
                                    -> zero Action Zone influence, target_yaw
                                       collapses to centroid_yaw
                                       (reason_code=wide_mode_zero_influence)
  - otherwise                      -> target_yaw = centroid_yaw + clamped bias,
                                       reason_code=active

Known simplification (flagged, not hidden): the only design-doc sections
available when this was implemented were the scoring formula, counterattack/
switch-of-play definitions, and eval plan (fragments C, E, F). play_location.jsonl
has no separate pixel/optical-flow motion field, only per-player track_id/yaw/
pitch/conf/vel_deg_per_sec. switch_of_play_score and the confidence "agreement"
term are therefore approximated from player-level speed/position data only, not
a true motion field. This should be revisited if/when a real motion-field
source exists, and before any tuning is trusted as final.

All weights live in CONFIG below — tuning should only ever edit CONFIG values,
never this logic, per the design doc's own instruction.
"""

import argparse
import csv
import json
import math
from collections import defaultdict, deque

CONFIG = {
    "STATIC_SPEED_CAP_DEG_S": 5.0,   # beta_s down-weight saturates by this rolling speed
    "BETA_M": 1.0,                   # motion-weight boost
    "BETA_S": 0.6,                   # static-player down-weight strength
    "BETA_C": 1.5,                   # breakaway subgroup boost strength
    "SPEED_NORM_CAP_DEG_S": 30.0,    # instantaneous speed normalisation cap
    "GAP_DEG": 25.0,                 # yaw gap that splits players into separate clusters
    "SUBGROUP_MIN": 2,
    "SUBGROUP_MAX": 5,
    "K_LEAD_S": 0.4,                 # lead time constant
    "LEAD_MAX_DEG": 8.0,
    "LEAD_CONF_GATE": 0.5,
    "SMOOTH_ALPHA": 0.35,            # analysis-only EMA; NOT the frozen Phase 2 kinematic smoother
    "CONF_FLOOR": 0.35,
    "DET_NORM_CAP": 8.0,
    "DISP_NORM_CAP_DEG": 60.0,
    "SWITCH_NORM_CAP": 1.0,          # share-shift-per-second normalisation cap
    "BIAS_MAX_DEG": 15.0,            # design doc §B: max deviation of target_yaw from centroid_yaw
    "ARBITRATION_W": 1.0,            # design doc §B "w" multiplier; unspecified in available fragments, identity default
    "SEP_GROWTH_MIN_OVERLAP": 0.5,   # Phase 3A.2: min track-ID overlap ratio vs prior frame's best_sub
                                      # required to credit sep_growth; below this, different subgroup, growth=0
    "FOLLOW_FOV": 85.0,              # St Margarets venue profile (docs/ai-project-state.md)
    "WIDE_FOV": 100.0,
    "C1": 1.6, "C2": 1.6, "C3": 1.0, "C4": 1.4,             # confidence sigmoid weights
    "A1": 1.4, "A2": 1.2, "A3": 0.8, "A4": 1.0, "A5": 2.0,  # counterattack sigmoid weights
}


def sigmoid(x):
    return 1.0 / (1.0 + math.exp(-x))


def circ_mean_weighted(yaws_deg, weights, fallback=0.0):
    if not yaws_deg or sum(weights) <= 0:
        return fallback
    sx = sum(w * math.cos(math.radians(y)) for y, w in zip(yaws_deg, weights))
    sy = sum(w * math.sin(math.radians(y)) for y, w in zip(yaws_deg, weights))
    return math.degrees(math.atan2(sy, sx))


def circ_variance(dirs_rad):
    # 1 - R, where R is the mean resultant vector length: 0 = perfectly aligned, 1 = scattered
    if not dirs_rad:
        return 1.0
    sx = sum(math.cos(a) for a in dirs_rad) / len(dirs_rad)
    sy = sum(math.sin(a) for a in dirs_rad) / len(dirs_rad)
    return 1.0 - math.hypot(sx, sy)


def load_wide_safety_modes(path):
    rows = []
    with open(path) as f:
        for line in f:
            d = json.loads(line)
            rows.append((d["timestamp"], d["mode"]))
    rows.sort()
    return rows


def nearest_mode(rows, t):
    if not rows:
        return None
    lo, hi = 0, len(rows) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if rows[mid][0] < t:
            lo = mid + 1
        else:
            hi = mid
    best = rows[lo]
    if lo > 0 and abs(rows[lo - 1][0] - t) < abs(best[0] - t):
        best = rows[lo - 1]
    return best[1]


def cluster_by_yaw_gap(players, gap_deg):
    if not players:
        return []
    ordered = sorted(players, key=lambda p: p["yaw"])
    clusters, cur = [], [ordered[0]]
    for p in ordered[1:]:
        if p["yaw"] - cur[-1]["yaw"] > gap_deg:
            clusters.append(cur)
            cur = [p]
        else:
            cur.append(p)
    clusters.append(cur)
    return clusters


def process(play_location_path, wide_safety_path, out_csv):
    cfg = CONFIG
    mode_rows = load_wide_safety_modes(wide_safety_path) if wide_safety_path else None

    track_history = defaultdict(lambda: deque(maxlen=40))  # ~20s at 0.5s sampling
    prev_zone_yaw_raw = None
    prev_action_zone_yaw = None
    prev_right_share = None
    prev_right_share_t = None
    prev_separation = None
    prev_best_sub_ids = set()
    prev_t = None
    prev_track_pos = {}

    with open(play_location_path) as f:
        samples = [json.loads(l) for l in f]

    out_rows = []

    for s in samples:
        t = s["timestamp"]
        players = s.get("players", [])
        centroid_yaw = s.get("person_centroid_yaw", 0.0)
        dispersion = s.get("person_centroid_dispersion_deg", 0.0)
        total_retained = s.get("total_retained_players", len(players))

        directions = {}
        for p in players:
            tid = p["track_id"]
            if tid in prev_track_pos:
                py, pp, pt = prev_track_pos[tid]
                dt = max(t - pt, 1e-3)
                directions[tid] = math.atan2((p["pitch"] - pp) / dt, (p["yaw"] - py) / dt)
            track_history[tid].append(p.get("vel_deg_per_sec", 0.0))

        static = {}
        for p in players:
            tid = p["track_id"]
            hist = track_history[tid]
            roll_mean = sum(hist) / len(hist) if hist else 0.0
            static[tid] = min(max(0.0, 1.0 - roll_mean / cfg["STATIC_SPEED_CAP_DEG_S"]), 1.0)

        clusters = cluster_by_yaw_gap(players, cfg["GAP_DEG"])
        main_cluster = max(clusters, key=len) if clusters else []
        # Phase 3A.1: lone detections are never eligible as a breakaway candidate at all
        # (previously only penalized via A5, which a size-1 cluster's degenerate
        # coherence=1.0 could still outscore). Ineligibility, not a bigger penalty.
        candidate_subs = [c for c in clusters if c is not main_cluster and len(c) >= cfg["SUBGROUP_MIN"]]

        main_speed_norm = 0.0
        if main_cluster:
            main_speed_norm = min(
                sum(p.get("vel_deg_per_sec", 0.0) for p in main_cluster) / len(main_cluster)
                / cfg["SPEED_NORM_CAP_DEG_S"],
                1.0,
            )

        best_sub, best_score, best_sep, best_sub_ids = None, 0.0, 0.0, set()
        best_components = {"sub_speed_norm": 0.0, "coherence": 0.0, "main_speed_norm": main_speed_norm,
                            "sep_growth": 0.0, "size_penalty": 0.0}
        for sub in candidate_subs:
            size = len(sub)
            sub_speed = sum(p.get("vel_deg_per_sec", 0.0) for p in sub) / size
            sub_speed_norm = min(sub_speed / cfg["SPEED_NORM_CAP_DEG_S"], 1.0)
            sub_dirs = [directions[p["track_id"]] for p in sub if p["track_id"] in directions]
            coherence = 1.0 - circ_variance(sub_dirs) if sub_dirs else 0.0
            sub_yaw_mean = sum(p["yaw"] for p in sub) / size
            main_yaw_mean = (sum(p["yaw"] for p in main_cluster) / len(main_cluster)) if main_cluster else sub_yaw_mean
            separation = abs(sub_yaw_mean - main_yaw_mean)
            cur_sub_ids = {p["track_id"] for p in sub}

            # Phase 3A.2: sep_growth is only meaningful if this candidate is (mostly)
            # the same players as the previous frame's best_sub -- otherwise "growth"
            # is just the difference between two unrelated subgroups' separations.
            sep_growth = 0.0
            if prev_separation is not None and prev_t is not None and prev_best_sub_ids:
                min_size = min(len(cur_sub_ids), len(prev_best_sub_ids)) or 1
                overlap_ratio = len(cur_sub_ids & prev_best_sub_ids) / min_size
                if overlap_ratio >= cfg["SEP_GROWTH_MIN_OVERLAP"]:
                    dt = max(t - prev_t, 1e-3)
                    sep_growth = min(max(0.0, (separation - prev_separation) / dt) / 10.0, 1.0)
                # else: different subgroup identity than last frame -- no continuity, sep_growth stays 0

            size_penalty = 1.0 if size < cfg["SUBGROUP_MIN"] else (0.6 if size > cfg["SUBGROUP_MAX"] else 0.0)

            score = sigmoid(
                cfg["A1"] * sub_speed_norm
                + cfg["A2"] * coherence
                + cfg["A3"] * (1.0 - main_speed_norm)
                + cfg["A4"] * sep_growth
                - cfg["A5"] * size_penalty
            )
            if score > best_score:
                best_sub, best_score, best_sep, best_sub_ids = sub, score, separation, cur_sub_ids
                best_components = {"sub_speed_norm": sub_speed_norm, "coherence": coherence,
                                    "main_speed_norm": main_speed_norm, "sep_growth": sep_growth,
                                    "size_penalty": size_penalty}

        counterattack_score = best_score
        subgroup_ids = {p["track_id"] for p in best_sub} if (best_sub and counterattack_score > 0.5) else set()

        right_energy = sum(
            p.get("conf", 0.0) * min(p.get("vel_deg_per_sec", 0.0) / cfg["SPEED_NORM_CAP_DEG_S"], 1.0)
            for p in players if p["yaw"] >= 0
        )
        total_energy = sum(
            p.get("conf", 0.0) * min(p.get("vel_deg_per_sec", 0.0) / cfg["SPEED_NORM_CAP_DEG_S"], 1.0)
            for p in players
        ) or 1e-6
        right_share = right_energy / total_energy
        switch_of_play_score = 0.0
        if prev_right_share is not None and prev_right_share_t is not None:
            dt = max(t - prev_right_share_t, 1e-3)
            switch_of_play_score = min(abs(right_share - prev_right_share) / dt / cfg["SWITCH_NORM_CAP"], 1.0)

        yaws, weights = [], []
        for p in players:
            tid = p["track_id"]
            speed_norm = min(p.get("vel_deg_per_sec", 0.0) / cfg["SPEED_NORM_CAP_DEG_S"], 1.0)
            w = p.get("conf", 0.0)
            w *= (1.0 + cfg["BETA_M"] * speed_norm)
            w *= (1.0 - cfg["BETA_S"] * static.get(tid, 0.0))
            if tid in subgroup_ids:
                w *= (1.0 + cfg["BETA_C"] * counterattack_score)
            yaws.append(p["yaw"])
            weights.append(max(w, 0.0))

        zone_yaw_raw = circ_mean_weighted(yaws, weights, fallback=centroid_yaw)

        smoothed = zone_yaw_raw if prev_action_zone_yaw is None else (
            prev_action_zone_yaw + cfg["SMOOTH_ALPHA"] * (zone_yaw_raw - prev_action_zone_yaw)
        )

        zone_speed = 0.0
        if prev_zone_yaw_raw is not None and prev_t is not None:
            zone_speed = (zone_yaw_raw - prev_zone_yaw_raw) / max(t - prev_t, 1e-3)

        all_dirs = list(directions.values())
        overall_coherence = 1.0 - circ_variance(all_dirs) if all_dirs else 0.0
        speeds = [p.get("vel_deg_per_sec", 0.0) for p in players]
        if speeds and sum(speeds) > 0:
            mean_speed = sum(speeds) / len(speeds)
            var_speed = sum((v - mean_speed) ** 2 for v in speeds) / len(speeds)
            cov = (var_speed ** 0.5) / mean_speed if mean_speed > 0 else 1.0
            agreement = max(0.0, 1.0 - min(cov, 1.0))
        else:
            agreement = 0.0
        conflict = 1.0 - overall_coherence

        det_norm = min(total_retained / cfg["DET_NORM_CAP"], 1.0)
        disp_norm = min(dispersion / cfg["DISP_NORM_CAP_DEG"], 1.0)
        confidence = sigmoid(
            cfg["C1"] * det_norm + cfg["C2"] * (1.0 - disp_norm) + cfg["C3"] * agreement - cfg["C4"] * conflict
        )

        lead = 0.0
        if confidence > cfg["LEAD_CONF_GATE"]:
            lead = max(-cfg["LEAD_MAX_DEG"], min(cfg["LEAD_MAX_DEG"], cfg["K_LEAD_S"] * zone_speed))

        # action_zone_yaw is the raw candidate signal (design doc §B) — always computed and
        # reported as-is, even in frames where it will end up gated to zero below. This is
        # deliberate: Phase 3B tuning needs the full history of what the scorer *would* have
        # suggested, not just the frames where it was allowed to act.
        action_zone_yaw = smoothed
        mode = nearest_mode(mode_rows, t) if mode_rows else None

        # Arbitration into a bounded target (design doc §B):
        #   target_yaw = centroid_yaw + clamp(w * confidence * (action_zone_yaw + lead - centroid_yaw), ±BIAS_MAX)
        raw_bias = cfg["ARBITRATION_W"] * confidence * (action_zone_yaw + lead - centroid_yaw)
        bias_applied = max(-cfg["BIAS_MAX_DEG"], min(cfg["BIAS_MAX_DEG"], raw_bias))
        target_yaw = centroid_yaw + bias_applied

        reason_code = "active"
        reason = (
            f"steady-state: zone {action_zone_yaw:.1f} vs centroid {centroid_yaw:.1f}, "
            f"bias {bias_applied:+.1f}deg (conf {confidence:.2f})"
        )
        if counterattack_score > 0.5 and subgroup_ids:
            reason = (
                f"breakaway: subgroup(n={len(subgroup_ids)}, ca={counterattack_score:.2f}) "
                f"vs main cluster; bias {bias_applied:+.1f}deg applied"
            )
        elif switch_of_play_score > 0.5:
            reason = f"switch of play: score {switch_of_play_score:.2f}; bias {bias_applied:+.1f}deg applied"

        if mode == "wide":
            bias_applied = 0.0
            target_yaw = centroid_yaw
            reason_code = "wide_mode_zero_influence"
            reason = "wide mode: Action Zone suppressed (zero influence by design)"
        elif confidence < cfg["CONF_FLOOR"]:
            bias_applied = 0.0
            target_yaw = centroid_yaw
            reason_code = "low_confidence_fallback"
            reason = f"low confidence ({confidence:.2f} < {cfg['CONF_FLOOR']}): fallback to centroid"

        recommended_fov = cfg["FOLLOW_FOV"] + (cfg["WIDE_FOV"] - cfg["FOLLOW_FOV"]) * max(
            1.0 - confidence, counterattack_score * 0.5, switch_of_play_score
        )

        out_rows.append({
            "timestamp": round(t, 3),
            "frame": s.get("frame"),
            "mode": mode or "",
            "centroid_yaw": round(centroid_yaw, 2),
            "action_zone_yaw": round(action_zone_yaw, 2),
            "raw_delta_deg": round(action_zone_yaw - centroid_yaw, 2),
            "target_yaw": round(target_yaw, 2),
            "bias_applied_deg": round(bias_applied, 2),
            "confidence": round(confidence, 3),
            "counterattack_score": round(counterattack_score, 3),
            "switch_of_play_score": round(switch_of_play_score, 3),
            "lead_deg": round(lead, 2),
            "recommended_fov": round(recommended_fov, 1),
            "subgroup_size": len(subgroup_ids),
            "subgroup_track_ids": ";".join(str(i) for i in sorted(subgroup_ids)),
            "comp_sub_speed_norm": round(best_components["sub_speed_norm"], 3),
            "comp_coherence": round(best_components["coherence"], 3),
            "comp_main_speed_norm": round(best_components["main_speed_norm"], 3),
            "comp_sep_growth": round(best_components["sep_growth"], 3),
            "comp_size_penalty": round(best_components["size_penalty"], 3),
            "reason_code": reason_code,
            "reason": reason,
        })

        prev_zone_yaw_raw = zone_yaw_raw
        prev_action_zone_yaw = smoothed
        prev_right_share, prev_right_share_t = right_share, t
        prev_separation = best_sep
        prev_best_sub_ids = best_sub_ids
        prev_t = t
        prev_track_pos = {p["track_id"]: (p["yaw"], p["pitch"], t) for p in players}

    with open(out_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    return out_rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Playcam Phase 3A offline Action Zone scorer (analysis-only: no rendering, no paid compute)."
    )
    ap.add_argument("play_location_jsonl")
    ap.add_argument("--wide-safety-timeline", default=None, help="optional wide_safety_timeline.jsonl for mode-gating")
    ap.add_argument("--out", default="action_zone_comparison.csv")
    args = ap.parse_args()
    rows = process(args.play_location_jsonl, args.wide_safety_timeline, args.out)
    print(f"wrote {len(rows)} rows to {args.out}")
